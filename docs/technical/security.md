# Security

This page summarizes practical security properties and controls. For the
full adversary model and per-threat mitigations, see
[Threat Model](threat-model.md).

## Threat Model (High Level)

Primary adversaries:

- malicious peers
- network observers
- malicious or degraded infrastructure nodes

Primary goals:

- protect funds from unauthorized signing
- reduce linkability between participant activity
- preserve availability under spam/DoS pressure

## Core Controls

- transaction verification before maker signing (see
  [Maker Verification Checklist](maker-verification-checklist.md))
- PoDLE anti-abuse commitments
- Tor-based transport and hidden-service support
- rate limiting and message validation in directory/maker paths
- fidelity bond weighting as Sybil-cost mechanism

## Automated Vulnerability Scanning

GitHub Actions applies complementary scanners to source, dependencies, and
published images:

- CodeQL runs extended security queries against Python, JavaScript/TypeScript,
  and GitHub Actions workflows on pull requests, main-branch updates, and a
  weekly schedule.
- Dependency review blocks pull requests that introduce high or critical
  vulnerabilities. `pip-audit` checks every production lock file and a fresh
  resolution of the package set used by unsigned development installs.
- Trivy scans every container candidate before it is promoted. Pull requests
  cover amd64; main and release promotion cover amd64, arm64, and arm/v7.
- A daily job re-scans both the moving `main` images and the current `latest`
  release images so newly published advisories are detected between builds.

Automated dependency update pull requests are intentionally disabled. Maintainers
refresh dependencies with the repository update scripts before releases, while
GitHub vulnerability alerts and scheduled audits report issues between releases.

Third-party GitHub Actions are pinned to full commit SHAs with the release
version in a trailing comment, so a moved or compromised tag cannot change the
code a workflow runs. Maintainers advance the pins deliberately, as with other
dependencies.

All high and critical image findings are retained in the workflow artifacts.
Findings with a published fix block image promotion; findings without a fix
produce warnings for maintainer review. Scanner matches identify affected
package versions, not exploitability, so maintainers must evaluate whether the
affected code is reachable in JoinMarket's runtime and threat model.

## Randomness and Key Material

Wallet mnemonics are encoded from explicit entropy bytes obtained through
Python's `secrets` API, which delegates to the operating system CSPRNG. The
command-line wallet requests 256 bits by default; jmwalletd requests 128 bits.
Both sizes are valid BIP39 entropy lengths. Entropy acquisition errors abort
wallet creation before backend initialization or wallet-file persistence, and
there is no weaker fallback.

Private wallet and TLS key files are created through owner-only temporary files
and atomically installed with mode `0600`. Daemon wallet and TLS directories are
mode `0700`. These permissions protect against other unprivileged local users;
they do not protect against the wallet process's own user, root, backups, swap,
or a compromised host.

Config, nick state, and wallet metadata are also written atomically with mode
`0600`. Reads tighten existing regular files when permitted. For these state
files, configured symlinks remain supported and updates preserve their targets.
Readable administrator-owned or read-only files remain usable if permissions
cannot be tightened; a warning asks the operator to address the exposure.
Wallet key and TLS files retain their stricter no-follow rules.

An upgrade preserves existing file contents, metadata records, and directory
permissions. Missing default data and state directories are created with mode
`0700`; existing shared or symlinked directories are not changed. This permission
hardening does not trigger rescans, metadata reconstruction, or new migrations.

PoDLE proof nonces use a domain-separated RFC 6979-style HMAC-SHA256 derivation
keyed by the UTXO private key and bound to the proof transcript. Proof generation
therefore does not depend on runtime randomness, avoiding private-key exposure
if an operating-system RNG later repeats a nonce. Maker selection, transaction
ordering, fee variation, and other adversary-relevant choices use
`secrets.SystemRandom`. Explicit tumbler seeds remain deterministic only for
reproducible plans.

## Directory and Messaging

- use multiple directory servers where possible
- prefer direct maker/taker channels when available
- enforce per-message signatures and a strict session state machine during the CoinJoin flow (takers may switch transport mid-session)

Peerlist nicknames, locations, and feature identifiers cannot contain wire
delimiters or control/whitespace characters. Unknown safe feature names remain
supported. Directory operators must upgrade as well as clients: a client cannot
distinguish a syntactically valid forged disconnect record from a genuine record
sent by an unpatched directory.

### Resource Limits

The following implementation limits bound work from untrusted peers without
changing private transaction message sizes or signed-input lock policy:

| Boundary | Limit |
| --- | --- |
| Directory pending handshakes | At most 128, or `max_peers` when smaller; separate from established capacity |
| Public broadcast ingress | 32 KiB/s per connection generation, 256 KiB burst |
| Public broadcast forwarding | 16 MiB/s across recipients, 64 MiB burst |
| Tracked directory offers | 256 per connection generation; order IDs at most 64 characters |
| Maker direct sockets | 256 across all identity generations; 60-second receive-idle and initial authentication deadlines |
| Client buffered messages | 1,024 messages and 8 MiB serialized data |
| Client message collection | 10,000 messages and 32 MiB per collection/fetch |
| Client retained state | 10,000 offers and 20,000 peers per directory; 64 features per peer |
| Watcher bond caches | 4,096 positive entries and 4,096 short-lived retry entries |
| Watcher mempool verification | At most 256 claims per update, five concurrent lookups, 60-second retry cooldown |

Excess public broadcasts are dropped. Client collection/state exhaustion closes
the connection and reports failure rather than accepting a partial authoritative
peerlist. Maker deadlines do not impose a 60-second limit on verified CoinJoin
work. These bounds reduce resource exhaustion; they do not prevent Sybil attacks
or guarantee admission while an adversary continuously occupies available slots.

### Wallet Daemon Admission

Create, recover, and unlock share a serialized lifecycle with at most eight
active or waiting requests. Excess requests receive HTTP 429. Password failures
also retain per-wallet retry backoff. Expensive wallet-file cryptography runs
outside the event loop; cancellation waits for that work to finish before
releasing lifecycle ownership. Wallet formats and KDF parameters are unchanged.

Authentication failures and filesystem errors return generic details. Config
responses mask backend tokens and password fields, including in-memory overrides;
config updates do not log submitted values. These controls do not change the
reference-compatible unauthenticated session/list/bootstrap API. Do not expose
the daemon to untrusted clients without an appropriate access-control boundary.

## Neutrino Notes

- neutrino is convenient, but full-node backends remain the strongest default for verification and compatibility
- run neutrino infrastructure you trust and route traffic with Tor where possible
- neutrino-api supports TLS with certificate pinning and bearer-token authentication, enabled by default; see [Neutrino TLS](neutrino-tls.md) for setup details
- the TLS certificate is self-signed and pinned on first use (TOFU model), so only the specific neutrino-api instance that generated it is trusted

## Operational Advice

- treat mnemonics and wallet files as high-value secrets
- keep software updated
- test operational setup on testnet/signet/regtest before production use

### Process Memory Hardening

Each long-running daemon (jmwalletd, maker, taker, directory server,
orderbook watcher) calls `jmcore.process_hardening.harden_current_process`
at startup, as does the `jm-wallet` command-line entry point. This applies
two cheap, OS-level mitigations on Linux to keep
secrets (mnemonic, BIP32 extended keys, derived private keys, NaCl session
keys, signed PSBTs) from leaking on crash or live introspection:

- `RLIMIT_CORE = 0` disables core dumps for the process, preventing tools
  like `systemd-coredump(8)` from writing the address space to disk
- `prctl(PR_SET_DUMPABLE, 0)` blocks non-privileged `ptrace` and
  `/proc/$pid/mem` reads from peer processes in the same user namespace

Set `JOINMARKET_DISABLE_PROCESS_HARDENING=1` to opt out (only useful when
debugging with gdb or rr).

`verify-password` and `showseed` warn on stderr when a password is supplied as a
command-line argument. Prefer their hidden prompt; argument values can remain
visible in process listings and shell history despite the warning. Direct
Bitcoin Core and Neutrino backends also warn for non-loopback HTTP endpoints,
including private LAN/container names. Such endpoints need a trusted transport
or HTTPS; the warning does not change routing or reject the configuration.

These mitigations do not protect anonymous pages that get paged out to
swap or written to a hibernation image. Operators who hold non-trivial
funds should also:

- enable encrypted swap (for example LUKS-backed swap with a random key
  on each boot, or zram-swap, so paged-out pages never persist plaintext)
- disable hibernation, or back the hibernation partition with the same
  encrypted volume as `/`
- avoid running daemons on systems with `kernel.yama.ptrace_scope = 0`
  (Ubuntu's default is `1`, which is fine; RHEL-style systems should
  set this in `/etc/sysctl.d/`)
- prefer running daemons under a dedicated unprivileged user account
  and, where possible, a `systemd` unit with
  `ProtectKernelTunables=`, `ProtectControlGroups=`,
  `PrivateTmp=`, and `NoNewPrivileges=`

For protocol-level details, see [Protocol](protocol.md) and [Privacy](privacy.md).
For the maker-side pre-sign checklist, see
[Maker Verification Checklist](maker-verification-checklist.md).
