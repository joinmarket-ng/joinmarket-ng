# Private channel buyout primitives

This package implements the transaction primitives for the draft JMP-0011
private channel buyout. One CoinJoin participant spends a Taproot channel input
with a counterparty that does not join the round. The participant's entire
change remains in one escrow output until settlement or recovery.

The current implementation provides a MuSig2 escrow output, a presigned split
with a block-based relative lock, a unilateral preimage claim, and a cooperative
key-path sweep. Serialized spends are verified against the agreed outpoint,
amounts, scripts, timelock, and signatures. Bitcoin Core regtest tests exercise
all three paths and claim fee replacement.

The private message codec provides strict draft JMP-0011 schemas and RFC 8785
canonical JSON, with bounded inputs and diagnostics that omit payload values.
It does not authenticate peers or enforce cross-message state transitions.

The experimental signing runtime adds channel freezing, authenticated Lightning
custom-message transport with an explicit peer allowlist, and a private SQLite
journal. It persists the signed split before authorizing channel-input signing.
The LND regtest exercises private negotiation, signing, exact-parent replay after
recreating the runtime, and broadcast with an ordinary wallet input. The backend
permits only one exact parent per session.

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
keeping ordinary wallet inputs available for authentication. The LND regtests
exercise channel-funded CoinJoin construction followed by each settlement path,
and explicit force-close recovery after signing and an LND restart.

This remains experimental. Automatic fee escalation of the presigned timeout
split is not implemented, and broader reorg coverage remains incomplete.
This branch does not include cofunded ring channel creation or the credential
market. Its buyout regtests do not establish the combined lifecycle of ring
change, a later channel-funded CoinJoin, Lightning settlement, and rented PoDLE
or fidelity-bond credentials on signet.
Do not use the runtime with funded operator channels. Existing wallets
and channel journals are not automatically read or migrated. Explicitly opened
buyout journals must match the supported schema; unknown or unversioned files are
refused without modification.

## Explicit operator configuration

The standalone `jm-buyout` command reads only the file supplied with `--config`.
Start from `jmswap/config.toml.template`. Enabling its `[buyout]` section authorizes
signing and settlement for new sessions. Configure the expected LND identity,
network, peer allowlist, credential paths, payout address, and a private journal.
Bitcoin Core must already have a synchronized transaction index; startup never
builds one or imports a wallet.

The default buyout settlement CLTV limit is 360 blocks. When `cltv_limit` is
omitted, it is derived as `min(360, csv_delay - buyer_settlement_depth - 36)`.

`payment_route_hints` is optional and payer-only. Set it when this node's graph
does not advertise a route to the payee, for example through an unannounced
channel. The hints are used for buyout settlement payments only: they never
appear in an invoice, never reach a counterparty, and change no fee, CLTV, or
timeout bound. Leaving the key out keeps payments exactly as before. Hints are
not discovered; the operator states each hop (`node_id`, `chan_id`,
`fee_base_msat`, `fee_proportional_millionths`, `cltv_expiry_delta`) as LND
reports it, at most 20 hints of at most 20 hops each.

Each new session records its runtime identity before freezing any channel.
Existing entries without that binding remain inactive, including entries from
the earlier library-only runtime. Changing the node, network, mixdepth, or wallet
fingerprint does not adopt existing sessions. Keep the original configuration
available for pending sessions and use a separate journal for another deployment.

For an isolated regtest deployment, run the counterparty endpoint continuously:

```sh
jm-buyout --config counterparty.toml serve --counterparty
```

On the buyer, set `wallet_fingerprint` to the identifier reported by the
JoinMarket wallet, then prepare an explicitly selected channel:

```sh
jm-buyout --config buyer.toml prepare --peer PUBKEY --channel TXID:VOUT
jm-buyout --config buyer.toml status
```

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
wallet-signed child transaction. The real-node tests exercise fee-policy
rejection and subsequent replay using a node-local fee penalty; they do not
establish recovery through sustained mempool congestion.

## Validation

Run the unit tests from the repository root:

```sh
python -m pytest -c pytest.ini jmswap --no-cov
```

Run the consensus and relay tests with Docker available:

```sh
python -m pytest -c pytest.ini jmswap/tests/test_bitcoin_escrow_regtest.py -m docker --no-cov --fail-on-skip
```

The Docker tests create and remove their own regtest node. They do not use
operator wallets or existing Lightning nodes.

Run the two-node private signing tests after building the pinned LND backend:

```sh
docker build -t jm-buyout-lnd:v0.21.3-beta lnd
python -m pytest -c pytest.ini jmswap/tests/test_lnd_buyout_regtest.py -m docker --no-cov --fail-on-skip
```
