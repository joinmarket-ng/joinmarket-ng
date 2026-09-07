# Threat Model

This page lays out the threats joinmarket-ng aims to mitigate, the
adversaries assumed in each scenario, and where those mitigations live in
the codebase and documentation. It complements [Security](security.md)
(controls summary), [Privacy](privacy.md) (cryptographic primitives), and
[Protocol](protocol.md) (wire-level behaviour). When in doubt, the linked
canonical page is authoritative.

## Scope

joinmarket-ng targets the JoinMarket CoinJoin protocol: a multi-party
Bitcoin transaction where a taker pays makers a small fee in exchange for
combining inputs and producing several equal-value outputs. Linkability
between inputs and equal-value outputs is the privacy property we defend.

The threat model covers:

- the maker, taker, and tumbler runtimes,
- the directory server and orderbook watcher,
- the wire protocol between them, including direct maker-taker channels,
- the wallet backends (descriptor wallet, neutrino) only insofar as they
  affect CoinJoin privacy and key custody.

It does not cover:

- the security of the underlying Bitcoin node, the operating system, or
  the Tor binary,
- physical compromise or coercion of an operator,
- attacks on the user's hardware wallet firmware itself.

## Assets

- **Funds.** Wallet UTXOs across all mixdepths, including fidelity bond
  UTXOs.
- **Privacy.** Lack of linkability between inputs and equal-value outputs
  of a CoinJoin, and between separate CoinJoin sessions.
- **Identity unlinkability.** The mapping between an operator's real-world
  identity, network endpoints (onion addresses, nicks), and on-chain UTXOs.
- **Availability.** The ability of makers and takers to complete CoinJoins
  in a reasonable time.

## Adversaries

We assume an adversary can do at least one of:

1. **Run malicious peers** as makers, takers, or both.
2. **Run or observe directory servers.** Directories see offers, bonds, and
   peerlists.
3. **Observe network traffic** outside Tor, or operate Tor relays. Hidden
   services are assumed not to leak server location, but exit-side traffic
   analysis is in scope where applicable.
4. **Observe the public Bitcoin blockchain.**
5. **Combine the above** to correlate on-chain UTXOs with directory data
   and network observations.

We do not assume the adversary has compromised the operator's wallet keys
or hardware wallet directly. If they have, the protocol cannot defend
funds; backups and mnemonic hygiene are out of scope here and live in
[Best Practices](best-practices.md).

## Threats and Mitigations

### Theft of Funds

- **Threat**: A malicious taker proposes a CoinJoin transaction that does
  not return the maker's CoinJoin output, change output, or fee.
- **Mitigation**: Makers run every proposed transaction through the strict
  pre-sign invariants in
  [Maker Verification Checklist](maker-verification-checklist.md). Any
  failed check refuses to sign. This is the most safety-critical code
  path; bugs here lose money directly.

### Sybil Orderbook Pollution

- **Threat**: One operator announces many low-quality offers to bias
  taker selection or starve real makers.
- **Mitigations**:
    - **Fidelity bonds.** Bond weight raises the cost of running many
      identities; takers weight maker selection by bond value. See
      [Privacy: Fidelity Bonds](privacy.md).
    - **Bondless-zero-fee policy.** Bondless offers with a non-zero
      absolute or relative fee are filtered out by default. A 5% allowance
      selects uniformly only among zero-fee offers, while the remaining slots
      retain fidelity-bond weighting ([Protocol](protocol.md)).
    - **Feature stats over bonded makers only** in the orderbook watcher
      ([issue #483](https://github.com/joinmarket-ng/joinmarket-ng/issues/483)) so a sybil cannot skew "% of makers supporting X".

### Costless UTXO Probing

- **Threat**: An attacker requests CoinJoin offers from many makers,
  collects the UTXOs each maker reveals, and aborts. With enough
  repetitions they correlate maker UTXOs without paying any fee.
- **Mitigations**:
    - **PoDLE commitments.** Takers must commit to ownership of a UTXO
      before makers reveal theirs ([Privacy: PoDLE](privacy.md)).
    - **Rate limiting** in directory and maker code paths.
    - **Operational guidance** to rotate mixdepths and treat revealed
      UTXOs as semi-public ([Best Practices](best-practices.md)). See
      also [issue #47](https://github.com/joinmarket-ng/joinmarket-ng/issues/47) for ongoing mitigation work.

### Subset-Sum Linkage of Equal-Output CoinJoins

- **Threat**: An on-chain observer enumerates input subsets whose sum
  equals an equal-value output, hoping to uniquely link inputs to outputs.
- **Mitigations**:
    - Equal-value outputs across multiple participants make multiple
      subset solutions plausible.
    - Multiple rounds and varied timing can add ambiguity, but do not guarantee
      protection from amount matching. Consider the whole spending path
      ([Privacy Practices](best-practices.md)).
    - Ongoing research in [issue #114](https://github.com/joinmarket-ng/joinmarket-ng/issues/114)
      examines subset-sum mitigations.

### Malicious or Compromised Directory

- **Threat**: A directory drops or fabricates offers, partitions the
  view of the orderbook, or silently censors specific makers.
- **Mitigations**:
    - **Multi-directory aggregation.** Run the orderbook watcher against
      several directories and treat each one's peerlist as authoritative
      only for itself ([Architecture](architecture.md)).
    - **Per-directory trust model.** Offers are dropped from a directory
      when its own peerlist stops reporting the maker, rather than relying
      on a cross-directory union that masks censorship.
    - **Direct maker reachability.** The orderbook watcher actively
      probes maker onion addresses and signals direct reachability in the
      UI ([issue #105](https://github.com/joinmarket-ng/joinmarket-ng/issues/105)) so a taker can prefer makers whose presence does
      not depend on a single directory.

### Session Confusion and Channel Hijack

- **Threat**: A peer or relay tries to mix messages from different
  CoinJoin sessions or replay messages from another channel.
- **Mitigations**:
    - **Signature binding.** Every private message is signed by the
      sender's nick key, and the maker enforces a strict state machine
      so out-of-order or replayed messages are dropped. Anti-replay uses
      a fixed `hostid` (`onion-network`) shared by all onion transports,
      so a relay cannot replay a message onto an attacker-chosen channel.
    - **Strict state machine.** Makers only accept the expected message
      for the current session state.
    - **Transport flexibility.** A taker may legitimately switch between
      directory relay and a direct connection mid-session (the reference
      taker routes each message opportunistically), so the maker tracks
      but does not reject such switches ([Protocol](protocol.md)).

### Network-Level Identity Linkage

- **Threat**: An observer correlates an operator's network endpoint
  (clearnet IP, onion address) with their on-chain UTXOs or nick.
- **Mitigations**:
    - **Tor by default** for all transport, including hidden services
      for makers ([Security](security.md)).
    - **Ephemeral onion services** for maker sessions when Tor control
      is enabled.
    - **Operational guidance** to consider bond funding history and avoid
      merging unrelated funds, since the bond UTXO is published per identity
      ([Best Practices](best-practices.md)).

### Hot Bond Key Compromise

- **Threat**: A maker's delegated certificate key is compromised; the attacker
  uses the active certificate to impersonate the maker.
- **Mitigations**:
    - **Certificate expiry bounds delegation**: reference and JoinMarket NG
      takers reject the proof once chain height exceeds the signed expiry
      boundary. Renew before that boundary to retain bond weight.
    - **Key separation protects funds**: an external bond key can remain
      offline; the certificate key alone cannot spend the bond
      ([Fidelity Bond Operations](../fidelity-bond-operations.md)).

### Denial of Service

- **Threat**: Spam connections or message floods overwhelm a directory
  or maker.
- **Mitigations**:
    - **Rate limiting and message validation** in directory and maker
      paths ([Security](security.md)).
    - **PoDLE commitments** also raise the cost of one specific DoS
      vector (probing every maker without paying).

## Out-of-Scope but Important

- **Operating-system and Bitcoin-node security.** A compromised
  Bitcoin node can lie to your wallet about UTXOs and feerates. Run a
  node you trust.
- **Mnemonic and bond backup.** The protocol cannot recover funds lost
  to a missing mnemonic. See [Best Practices](best-practices.md).
- **Hardware wallet firmware.** Where a hardware wallet cannot sign bond
  redemptions, software mnemonic signing may be required. See
  [Fidelity Bond Operations](../fidelity-bond-operations.md) for the support
  matrix.
- **External services**: mempool.space, neutrino backends, and
  third-party Tor relays. Pick infrastructure you trust and pin TLS where
  available ([Neutrino TLS](neutrino-tls.md)).

## Disclosure

Use [private vulnerability reporting](https://github.com/joinmarket-ng/joinmarket-ng/security/advisories/new)
and follow the published [security policy](https://github.com/joinmarket-ng/joinmarket-ng/security/policy).
Do not report unpatched vulnerabilities in public issues or community channels.
