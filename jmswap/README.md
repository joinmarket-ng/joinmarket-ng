# Private channel buyout primitives

This package implements the transaction primitives for the draft JMP-0011
private channel buyout. One CoinJoin participant spends a Taproot channel input
with a counterparty that does not join the round. The participant's entire
change remains in one escrow output until settlement or recovery.

The current implementation provides a MuSig2 escrow output, a presigned split
with a block-based relative lock, a unilateral preimage claim, and a cooperative
key-path sweep. Serialized spends are verified against the agreed outpoint,
amounts, scripts, timelock, and signatures.

The private message codec provides strict draft JMP-0011 schemas and RFC 8785
canonical JSON, with bounded inputs and diagnostics that omit payload values.
It does not authenticate peers or enforce cross-message state transitions.

The experimental signing runtime adds channel freezing, authenticated Lightning
custom-message transport with an explicit peer allowlist, and a private SQLite
journal. It persists the signed split before authorizing channel-input signing.
The backend permits only one exact parent per session.

The settlement runtime validates invoices, records payment intent before sending,
tracks uncertain payment outcomes without paying again, and supports cooperative
sweeps, preimage claims, and mature unpaid splits. Before payment it saves the
observed fully signed parent so recovery can rebroadcast that exact transaction
after eviction. Settlement requires affirmative authorization in each session;
older entries without it remain inactive. A paid session lacking the signed parent
reports `PARENT_RECOVERY_REQUIRED` if the parent disappears.

After a paid sweep is rejected or evicted, recovery replays its saved bytes until
the configured fee-bump interval elapses. It can then publish a higher-fee
preimage claim within the operator's fee budget, without requiring the rejected
transaction to be accepted first. This also applies to a rejected cooperative
sweep, exposing the preimage script on chain. A cooperative sweep still in the
mempool retains its key-path privacy until the safety deadline. Existing
authorized, paid sessions with saved spend intents use this recovery behavior
after upgrade; missing authorization never enables settlement.

The taker adapter includes channel funding as external CoinJoin inputs while
keeping ordinary wallet inputs available for authentication.

This is experimental and disabled by default. Read
[Experimental Ring Market](../docs/experimental-ring-market.md) before using it
on signet or mainnet. Automatic fee escalation of the presigned timeout split is
not implemented. Existing wallets and channel journals are never read or
migrated automatically; explicitly opened buyout journals must match the
supported schema, and unknown or unversioned files are refused unmodified.

## Mixdepth-isolated ring nodes

Each LN identity belongs to one wallet and one **source** mixdepth. Equal-amount
outputs go to the next mixdepth, not to the mixdepth associated with that node.
Every new ring edge requires a private final-Taproot channel with SCID alias;
startup rejects LND without alias feature bit 47, and the fundee rejects opens
that omit the alias. Ordinary non-ring LND calls keep their prior default.
Maker offers and input selection use only mapped source mixdepths when rings are
enabled. A taker cannot select an unmapped source, including through interactive
or explicit-input selection. This does not enable optional taker participation.

Example ring configuration (the remaining policy settings use their defaults):

```toml
[maker.channel_ring]
enabled = true
node_binding_directory = "/home/user/.joinmarket-ng/ln-node-bindings"
persistence_directory = "/home/user/.joinmarket-ng/channel-ring-maker"
mixdepth_nodes = { 0 = "md0" }

[maker.channel_ring.nodes.md0]
lnd_grpc_url = "https://127.0.0.1:10009"
lnd_tls_cert_path = "/home/user/.lnd/tls.cert"
lnd_macaroon_path = "/home/user/.lnd/data/chain/bitcoin/signet/admin.macaroon"
onion_endpoint = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.onion:9735"
```

Use `taker.channel_ring` for a taker profile. Every cooperating profile on the
host must use the **same binding directory**, but separate journal directories
for concurrently running processes. The registry identifies nodes by their live
pubkeys, not endpoint names. A lifetime exclusive journal lease prevents two
processes from recovering or updating the same ring state concurrently.

Before first use, obtain the expected node pubkey from your authenticated LND
administration interface and explicitly enroll it:

```sh
jm-maker enroll-ring-nodes --component maker \
  --config-file /path/to/config.toml --mnemonic-file /path/to/wallet.mnemonic \
  --expected-node "md0=$EXPECTED_NODE_PUBKEY" --acknowledge-prior-use
```

Repeat `--expected-node NAME=PUBKEY` for every mapped node. Use `--component taker`
to enroll the taker settings. Supply the same BIP39 passphrase used by the wallet
if applicable. Enrollment does not sync the wallet, start a maker, or open, close,
rotate, or reassign channels. Existing channels are allowed, but acknowledgment
only authorizes future use: it cannot establish historical privacy. The registry
cannot protect against other software or hosts that bypass it.

Startup only verifies claims. A missing claim, conflicting owner, or malformed
registry blocks new ring activity. Recovery uses immutable journal provenance and
the recorded identity, not the current funding mapping. Keeping a node configured
allows recovery after disabling new rings or removing its source mapping.

To inspect retained ring state, for example after a maker exits with an
operator-recovery error, run:

```sh
jm-maker ring-records --component maker --config-file /path/to/config.toml
```

It prints each record's state, local inputs, lease owner, and `retirement_action`
as JSON, without secrets and without contacting LND or the chain. Records whose
action is `blocked` must keep their inputs unspent until the exact funding
transaction or a confirmed conflict settles them.

An orphaned maker `signing` record can be retired automatically if authorization
was received but local input signing never began. Recovery checks that the exact
funding transaction is absent and every local input is unspent, retires both LND
channel attempts, and then releases only that record's wallet leases. Retirement
intent is saved before external checks, so failures and restarts resume cleanup
without allowing signing or releasing inputs early.
After durable channel retirement, release-only recovery still validates wallet
ownership but does not require the retired LND node to be available.

On upgrade, complete previous-format journals retain their explicit signing
evidence and unsigned orphans are eligible for this recovery. Missing signing
fields remain an error. Existing `retiring` or `recovery_required` records without
the new retirement intent are not automatically abandoned. Once signing might
have begun, empty signature storage or transaction absence alone does not permit
retirement. Never delete journals or manually unlock their inputs to bypass this
protection.
Older binaries reject journals written with the new retirement-intent field;
do not remove that field to force a downgrade.

### Node rotation and on-chain reserve

Once a node has no open ring channels left, it is a good time to give its
mixdepth a fresh LN identity, so later rings are not linked to earlier ones:

1. Stop the maker and taker processes that use the wallet.
2. Check `jm-maker ring-records --component maker` and
   `jm-maker ring-records --component taker`, plus `lncli listchannels`,
   `lncli pendingchannels` and `lncli closedchannels` on the old node. Settle any buyouts
   the old node started (buyout sessions stay on the node that created them).
3. Add the new node under `nodes`, point `mixdepth_nodes` at it, and keep the old
   node under `nodes` (unmapped) while any active ring record references it.
4. Enroll it for each configured component: `jm-maker enroll-ring-nodes
   --component maker --expected-node NEW=PUBKEY --acknowledge-prior-use`
   (substitute `taker` for the taker profile, and supply the usual config and
   mnemonic options).
5. Restart. Recovery keeps using the old node for its recorded rings; new rings
   use the new one.

Registry claims are permanent: a retired pubkey can never be enrolled for
another wallet or mixdepth.

`opener_reserve` and `fundee_reserve` are negotiated with the peer and checked
against LND before signing; a mismatch aborts the ring. They do not fund LND's
own wallet. LND v0.21.3-beta skips its public-channel on-chain reserve check
for **private** ring channels, even though an anchor-channel force close may
still need spendable LND-wallet funds for CPFP fee bumping. As an advisory
target, keep at least 100,000 sats confirmed in each ring node's LND wallet
(`lncli newaddress p2tr`), from a source that does not link it to your
JoinMarket mixdepths. Startup warns below this target. A higher confirmed
balance is **not** proof that fee-bump funds are available: LND's balance can
include locked outputs and accounts other than its readily spendable default
account. Monitor usable LND wallet UTXOs and fee conditions independently;
the 100,000-sat target is not a safety guarantee.

**Ring miner-fee policy limit:** Maker ring signing verifies the exact funding
transaction and LND's PSBT checks, but does not apply the ordinary maker's
configured `min_fee_rate_sat_vb` / `min_fee_block_target` floor. An authenticated
taker can propose a valid ring parent below the maker's ordinary fee floor;
pending channels and reserved inputs could then be delayed. Do not treat those
settings as a guarantee for ring rounds. Monitor the actual parent feerate and
confirmation status, and retain the signed journals for recovery.

**Upgrading from the previous single-node configuration:** replace the flat LND
settings with named nodes and explicitly enroll identities. Previous journals
without ownership provenance are preserved and refused for automatic recovery.
No automatic migration, rebinding, channel recreation, or node rotation occurs.
Do not delete unresolved journals or registry claims to bypass a startup failure.

## Explicit operator configuration

The standalone `jm-buyout` command reads only the file supplied with `--config`.
Start from `jmswap/config.toml.template`. Enabling its `[buyout]` section authorizes
signing and settlement for new sessions. Configure the expected LND identity,
network, peer allowlist, credential paths, fresh payout addresses, and a private journal.
Bitcoin Core must already have a synchronized transaction index; startup never
builds one or imports a wallet.

Use one buyout journal per LN node. The journal refuses a buyer session for a
channel whose funding transaction also funds a channel this node already
bought out (or is buying out). A node's two ring edges share one funding
transaction, so a node buys out at most one edge per ring; buying out both would
tie them to one owner on chain. The same journal also reserves each channel
point at most once after signing. These checks apply only within that journal;
do not create another journal for the same LN node to bypass them.

`payout_addresses` lists fresh P2TR addresses of the node's source mixdepth,
reserved so the wallet never hands them out elsewhere:

```sh
jm-wallet address new 0 --label buyout   # repeat for each address
```

Every session, buyer or counterparty, records the first address no earlier
session used, so no two buyouts pay the same address. When all are used, new
sessions are refused until more are added.

The default buyout settlement CLTV limit is 360 blocks. When `cltv_limit` is
omitted, it is derived as `min(360, csv_delay - buyer_settlement_depth - 36)`.

The conservative defaults create the counterparty invoice after 3 parent
confirmations and permit buyer payment after 6. For a **new** session,
operators willing to accept greater parent-reorg
risk can configure both parties with:

```toml
[buyout.policy]
settlement_depth = 1
buyer_settlement_depth = 3
```

The counterparty must keep its settlement monitor running to create the
invoice at one confirmation; buyer payment still waits for three and checks
the exact confirmed, unspent escrow before recording payment intent. Neither
change affects already negotiated sessions, and invoice-at-zero is not
supported by the current protocol. A 2-of-2 escrow does not prevent another
CoinJoin participant from invalidating the parent with a conflicting spend of
its own input. Do not make this an untrusted-market default without a separate
security review, or lower a live session's depth to bypass stale status.

`payment_route_hints` is optional and payer-only. Set it when this node's graph
does not advertise a route to the payee, for example through an unannounced
channel. The hints are used for buyout settlement payments only: they never
appear in an invoice, never reach a counterparty, and change no fee, CLTV, or
timeout bound. Leaving the key out keeps payments exactly as before. Hints are
not discovered; the operator states each hop (`node_id`, `chan_id`,
`fee_base_msat`, `fee_proportional_millionths`, `cltv_expiry_delta`) as LND
reports it, at most 20 hints of at most 20 hops each.

The counterparty's invoice carries LND's own route hints. LND adds a hint only
for an active private channel whose peer is in the public graph, so a peer
with only private channels is never named; a buyer with a direct channel to
the counterparty pays without a hint. A hint shows the buyer the hinted peer's
pubkey, a channel identifier (an alias when negotiated) and its forwarding policy.

Each new session records its runtime identity before freezing any channel.
Existing entries without that binding remain inactive, including entries from
the earlier library-only runtime. Changing the node, network, mixdepth, or wallet
fingerprint does not adopt existing sessions. Keep the original configuration
available for pending sessions and use a separate journal for another deployment.

Run the counterparty endpoint continuously:

```sh
jm-buyout --config counterparty.toml serve --counterparty
```

On the buyer, set `wallet_fingerprint` to the identifier reported by the
JoinMarket wallet, then prepare an explicitly selected channel:

```sh
jm-buyout --config buyer.toml prepare --peer PUBKEY --channel TXID:VOUT
jm-buyout --config buyer.toml status
```

`status` is an offline view of the last journal poll, not a live chain query.
`PARENT_OBSERVED` means that a poll verified the authorized parent bytes on
chain or in the mempool; it does **not** prove confirmations, that the escrow
remains unspent, or payment eligibility. If a later poll no longer finds the
parent, the journal returns to `PARENT_MISSING`. Old authorized sessions may
acquire the observation label on their next poll, without migrating or
changing any agreed payment policy.

The prepare command prints a session ID. Supply it to a non-sweep Taproot taker
round using `--buyout-config buyer.toml --buyout-session SESSION_ID` alongside
the normal `jm-taker coinjoin` arguments. The configured wallet fingerprint and
mixdepth must match the taker wallet. Ordinary wallet inputs still provide the
PoDLE commitment for the full CoinJoin amount. The CLI monitors settlement after
broadcast; interruption leaves the journal intact.

For a maker, supply the same pair of options to `jm-maker start`. The wallet
must use a Taproot pit and a backend capable of resolving arbitrary UTXOs.
Offers use only the bound mixdepth, require an ordinary wallet input for
authentication, and reserve enough channel value for escrow change. A prepared
session funds at most one round. Once reserved or canceled, its offers are
withdrawn through the normal publication delay; the maker keeps monitoring
settlement without falling back to ordinary liquidity. Channel inputs and escrow
change stay outside wallet UTXO and address history.

Keep `jm-buyout --config buyer.toml serve` running to monitor recovery, including
reorganizations after completion. It does not accept incoming buyout proposals
unless `--counterparty` is supplied. `cancel --session SESSION_ID` explicitly
cancels a buyer session only before parent signing has started; it never recreates
lost nonces. `status` inspects an existing journal offline and omits secrets.

New sessions authorize cancellation of unsigned channel locks at the negotiated
freeze deadline. Automatic force-close after signing requires
`automatic_force_close = true` when the session is created and waits until the
agreed maximum freeze interval. It incurs LND on-chain fees and can invalidate
an unconfirmed parent. Existing sessions do not inherit this setting on restart.
Journals without affirmative recovery authorization remain inactive for automatic
recovery, and a prior close-request marker alone does not authorize force-close.

For a bound, signed session, `force-close --session SESSION_ID` explicitly
authorizes force-close, including retry after an uncertain RPC result. Automatic
polling never repeats an uncertain close request. Paid or uncertain-payment
sessions cannot be force-closed through this recovery path. If the parent
confirms without payment, an authorized buyer can broadcast the presigned split
after its CSV delay, even if the counterparty is unavailable.

The timeout split has the fee agreed before parent signing. If Core rejects it
under fee pressure, the runtime retains the broadcast intent and retries the
same signed bytes; it neither raises the fee nor starts another payment. The
split can remain blocked until fee conditions improve. Recovery with increased
fees needs an additional mechanism such as pre-signed fee variants or a
wallet-signed child transaction.
