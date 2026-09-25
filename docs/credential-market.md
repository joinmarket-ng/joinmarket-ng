# Experimental Credential Market

**Buyers should start with the [signet quickstart](credential-market-overview.md)**,
which is the whole purchase in six commands. This page is the operator and
protocol reference: seller setup, offline owner material, wallet-native seller
operations, recovery, and fault evidence.

`jm-market` is an opt-in native directory market with a scriptable JSON CLI for
external PoDLE openings and delegated fidelity-bond certificates. It does not create wallets,
send payments, run a Lightning node, broadcast Bitcoin transactions, hold
escrow, or provide fair exchange. Payment is Lightning-only; see
[Lightning-Only Payments](#lightning-only-payments). Use it first on signet or
regtest with a small, capped exposure. Do not run unattended mainnet sales or
purchases.

The file-oriented seller commands below require a standalone seller
store. Explicit wallet-ledger activation blocks those unbound seller writes and
`serve`. Activated wallets use the authenticated daemon seller operations described
below. Inspection and export remain available through the CLI. Activation does
not enable automatic trading.

The companion `jmp` repository contains the protocol drafts `jmp-0012.md`
(Credential Market) and `jmp-0013.md` (Signed Market Fault Evidence) on branch
`jmp-credential-market`. They are experimental, not stable interface references.
Implementation discussions are [NG #597](https://github.com/joinmarket-ng/joinmarket-ng/issues/597)
and [NG #598](https://github.com/joinmarket-ng/joinmarket-ng/issues/598).

## Before You Trade

This is experimental software with a deliberately narrow accountability model.
A seller can withhold delivery after payment, and a provider can spend a PoDLE
backing output before it is used. Buyers must set a hard price cap and pass
`--require-experimental-risk-ack` for every request.

- Buyers get a fresh market key bundle per trade automatically: `request` writes
  one beside each `--request-file`. Do not use payment funds as forthcoming
  CoinJoin inputs.
- A seller knows the PoDLE commitment it sold and can correlate it with public
  `!hp2` timing. Lightning is not anonymous here: invoice-node identity and
  routing or endpoint relationships remain observable.
- Verify a quoted bond through your configured Bitcoin backend before paying:
  the precise outpoint, P2WSH script, positive confirmed unspent value, and
  future locktime must all match. A listing is not evidence of stake.
- The JoinMarket messaging network and the Bitcoin network are separate. The
  market transport uses `network_config.network`; chain, payment-address, and
  credential checks use `bitcoin_network` when set, otherwise that network.
  Do not infer the Bitcoin network from a directory's messaging network.

Sanctions cover conflicting finalized allocations of the same resource, a signed
delivery with a statically invalid credential, and owner certificate equivocation
corroborated locally during an exclusive bond lease. There are no interactive accusation votes or
defense timeouts. Nonpayment, payment disputes, message timeouts, withholding,
spent backing UTXOs, changed confirmations, or unavailable chain data are not
proofs. With `[taker] market_fault_exclusion = true`, a valid proof excludes the
verified bond from taker selection from the fault period through the full
following period. Exclusion is opt-in during the experimental phase, independent
of `external_podle_mode`; a later release is intended to make it opt-out. When
disabled, ordinary takers neither load nor apply persisted fault evidence. Explicit
market buyers and sellers still validate evidence in their own workflows.
Exclusion does not confiscate bond capital or create onchain slashing.

Version-1 sales support only PoDLE indices 0, 1, and 2. Portable external records
can represent other indices, but this market rejects them rather than selling
credentials that standard makers cannot use. Inspect invalid-delivery evidence
before publishing it: it includes the exact signed credential, potentially with
arbitrary seller-supplied private information.

## Lightning-Only Payments

Every operational entry point refuses the on-chain rail: queueing a payment
request, quoting, deriving a payable URI, and settling. The invoice must be an
externally generated, exact-amount BOLT11 string for the configured Bitcoin
network, including the distinct signet prefix `lntbs` rather than the testnet
`lntb`.

Payment documents contain only `rail: "lightning"`, `request`, and `amount_sats`.
Other rails and unknown fields are invalid. This unpublished market has no older
payment format or schema migration path.

## Files And Roles

For a source checkout, install the CLI:

```bash
python -m pip install ./jmcore ./jmwallet ./taker
```

Invoice signatures use the maintained `python-bitcointx` fork already pinned by
JoinMarket. The system `libsecp256k1` must include its `recovery` module to validate
invoices without an explicit payee key. The BOLT11 parser is vendored with its
license and upstream revision; it does not install a separate crypto backend.

Keep all secret files owner-only. Commands create private output files with mode
`0600` and refuse to replace different existing output. The market key bundle
has exactly this JSON schema:

| Field | Purpose |
| --- | --- |
| `version` | Integer `1` |
| `seller_signing_key` | 32-byte secp256k1 secret, used for market documents |
| `encryption_key` | 32-byte X25519 secret, used for sealed market messages and buyer delivery |
| `renter_certificate_key` | 32-byte secp256k1 secret for a bond renter certificate |

`jm-market keygen` writes this bundle. `jm-market public` emits only:

```json
{
  "version": 1,
  "seller_pubkey": "<compressed-secp256k1-public-key, NOT WIRE>",
  "encryption_pubkey": "<x25519-public-key, NOT WIRE>",
  "renter_certificate_pubkey": "<compressed-secp256k1-public-key, NOT WIRE>"
}
```

The public JSON is safe to transfer as needed. Generate separate bundles for a
seller and every buyer. A bond renter owns its own `renter_certificate_key`; it
must never be a seller key or the bond owner's spending key.

Commands that accept a single secret file, such as `--owner-key` and
`--certificate-key`, read either 64 hexadecimal characters (32 bytes) or a JSON object with
a `secret_key` string. They never accept a private key as a command-line value.
Keep the bond owner's key offline. The online provider needs only its seller
bundle, authorization, inventory, payment queue, and chain read access.

### Offline Owner Material

Create the seller's online bundle and export its public metadata:

```bash
umask 077
mkdir -p market
jm-market keygen --output market/seller-keys.json
jm-market public --keys market/seller-keys.json --output market/seller-public.json
```

Create a bond-reference file on the offline owner machine. This is a shape
example only. It is **NOT WIRE** data and has no real address or mainnet value.

```json
{
  "network": "regtest",
  "outpoint": {"txid": "<64-lowercase-hex, NOT WIRE>", "vout": 0},
  "pubkey": "<33-byte-compressed-owner-public-key, NOT WIRE>",
  "locktime": 2000000000
}
```

At a confirmed chain height `HEIGHT`, the authorization period is
`(HEIGHT - 1) / 2016`, rounded down. Sign the authorization offline using the
private owner key file only:

```bash
jm-market authorize \
  --bond-ref market/bond-ref.json \
  --seller-pub market/seller-public.json \
  --owner-key /offline/owner/bond-owner.key \
  --period "$PERIOD" \
  --output market/authorization.json
```

The owner signature delegates the seller key for that period and opts that bond
into the signed-fault policy. It does not give the seller authority to spend the
bond. Use a different market signing key for each owner and period.

To make an external PoDLE credential, the backing-output owner supplies this
metadata and its private key file to the offline exporter. This input shape is
also **NOT WIRE**. `scriptpubkey` must be the real lowercase output script, and
`blockheight` must be its confirmed height on the declared network.

```json
{
  "network": "regtest",
  "outpoint": {"txid": "<64-lowercase-hex, NOT WIRE>", "vout": 0},
  "scriptpubkey": "<lowercase-scriptpubkey-hex, NOT WIRE>",
  "blockheight": 12345,
  "index": 0
}
```

```bash
jm-market export-podle \
  --owner-key /offline/podle/backing-output.key \
  --metadata market/podle-metadata.json \
  --output market/podle-credential.json
```

The output contains the public PoDLE record fields `version`, `network`,
`outpoint`, `P`, `P2`, `sig`, `e`, `commitment`, `index`, `scriptpubkey`, and
`blockheight`. It contains no backing private key and its backing UTXO is not a
CoinJoin funding input.

## Seller Workflow

`--data-dir` isolates seller state. The seller store is
`<data-dir>/market/seller.sqlite`; protect and back it up as stateful issuance
data. Restoring a stale copy can defeat local one-time-sale protection.

### Inventory And Payments

Add a PoDLE credential as inventory, or add a bond authorization now and attach
the offline owner-signed renter credential after the buyer's request:

```bash
export MARKET_DATA="$HOME/.joinmarket-ng-market-seller"

jm-market seller add-inventory \
  --data-dir "$MARKET_DATA" \
  --credential market/podle-credential.json

jm-market seller add-inventory \
  --data-dir "$MARKET_DATA" \
  --authorization market/authorization.json
```

Generate every invoice in an external Lightning wallet or node before adding it.
The market neither owns nor contacts that wallet or node. Queue one fresh
invoice per possible sale. This `PaymentTerms` example is **NOT WIRE**; the
BOLT11 value is a placeholder, never a real invoice:

```json
{
  "rail": "lightning",
  "request": "<bolt11-invoice, NOT WIRE>",
  "amount_sats": 1000
}
```

The invoice must be for the configured Bitcoin network (`lnbc`, `lntb`, signet
`lntbs`, or `lnbcrt`), carry an exact amount equal to `amount_sats`, and not
expire before the quote it will back.

```bash
jm-market seller add-payment \
  --data-dir "$MARKET_DATA" \
  --terms market/payment-lightning.json \
  --ttl 300
```

`add-payment --ttl` is a 1 to 900 second validation horizon, primarily so a
BOLT11 invoice must outlive the immediate queueing check. It is distinct from
the quote lifetime. On-chain terms are refused here, before anything is queued.

Set `--price-sats` consistently with the queued `amount_sats`. The listing
price is indicative; the buyer must rely on the signed quote's payment amount
and its own `--max-price-sats` cap.

### Serve Over Tor

The market uses existing JoinMarket directories for discovery. A buyer tries a
seller's advertised direct onion location first, then retries the same sealed
request through one end-to-end encrypted directory relay. Production direct
locations must be onion services. There is no HTTP seller endpoint and no
production clearnet fallback.

For direct service, configure Tor externally to map the advertised onion
`host:port` to a local listener. `jm-market` does not create or manage that Tor
hidden service. Then run the seller with matching values:

```bash
jm-market serve \
  --data-dir "$MARKET_DATA" \
  --authorization market/authorization.json \
  --keys market/seller-keys.json \
  --products podle,bond \
  --price-sats 1000 \
  --quote-ttl 900 \
  --direct-location "<seller-onion-host:port>" \
  --listen-host 127.0.0.1 \
  --listen-port 9735
```

Without a separately configured onion service, omit `--direct-location`,
`--listen-host`, and `--listen-port`; directory relay remains available. The
default quote lifetime is 300 seconds and every issued quote is clamped to 900
seconds, so a longer `--quote-ttl` has no effect. Each unpaid quote still
reserves inventory and can be used for griefing. Published listings expire 60
seconds after they are signed, so a buyer must request promptly after
discovery. The store permits at most 64 live quotes and the service admits at
most one new quote per second globally. These are resource bounds, not DoS or
Sybil protection.

### Settle Locally

Inspect pending live quotes locally:

```bash
jm-market seller pending --data-dir "$MARKET_DATA" --output market/pending.json
```

For a bond quote, obtain the renter's public metadata from the buyer. On the
offline owner machine, sign only that renter public key, then attach the public
credential on the seller host:

```bash
jm-market sign-bond \
  --authorization market/authorization.json \
  --certificate-pub market/renter-public.json \
  --owner-key /offline/owner/bond-owner.key \
  --output market/renter-bond-credential.json

jm-market seller attach \
  --data-dir "$MARKET_DATA" \
  --quote-id "$QUOTE_ID" \
  --credential market/renter-bond-credential.json
```

Settlement is a deliberate local act. First obtain confirmation from the
external Lightning wallet or node that the invoice really was paid, save the
32-byte preimage in an owner-only file, then give the explicit acknowledgment.
A remotely claimed preimage never settles a trade, and a preimage alone proves
neither the payer nor the payment.

```bash
jm-market seller settle \
  --data-dir "$MARKET_DATA" \
  --quote-id "$QUOTE_ID" \
  --keys market/seller-keys.json \
  --preimage-file market/settled-preimage.bin \
  --acknowledge-ln-settlement \
  --output market/finalized-package.json
```

Finalization atomically consumes the inventory
and payment request and persists the signed allocation, delivery, and
buyer-sealed package. To recover a finalized package after the quote has
expired or after a process restart:

```bash
jm-market seller export \
  --data-dir "$MARKET_DATA" \
  --quote-id "$QUOTE_ID" \
  --output market/recovered-finalized-package.json
```

## Wallet-Native Key Capability

The wallet service now constructs an in-memory market key capability directly
from the binary BIP39 seed, including its passphrase. This is separate from the
BIP32 spending tree. It exposes public keys, canonical market-document signing,
and bounded NaCl sealed-box decryption, not private key export. Wallet close and
daemon lock revoke subsequent operations through retained capability references.
This is an in-process API boundary, not a sandbox or guaranteed memory erasure.

Derivation uses [SLIP-0021](https://github.com/satoshilabs/slips/blob/master/slip-0021.md).
The first label is the ASCII string `JoinMarket NG credential market`. Subsequent
labels, each a separate child derivation, are:

```text
v1 / network / NETWORK / chain / GENESIS_HASH / role / ROLE / period / PERIOD
   / bond / BOND_LABELS / trade / TRADE_LABELS / purpose / PURPOSE / algorithm / ALGORITHM
```

Hashes are lowercase hexadecimal ASCII and integers are minimal decimal ASCII.
`BOND_LABELS` is `none`, or the three labels `outpoint / TXID / VOUT`.
`TRADE_LABELS` is `none`, or `id / TRADE_ID`. The chain hash is the genesis block
hash, never the moving tip. Inputs are canonical public context, not evidence of
bond ownership or chain verification.

Document signing uses purpose `document-signing` and algorithm
`secp256k1-ecdsa-bitcoin-message`, followed by a decimal counter starting at `0`.
Invalid secp256k1 scalars are rejected without modular reduction, trying up to
256 counter labels. Encryption uses purpose `message-encryption` and algorithm
`x25519-xsalsa20-poly1305-sealedbox`; NaCl applies the X25519 key clamping.
Both take the last 32 bytes of their final SLIP-0021 node as key material.

The wallet-native seller uses this capability for market signing and message
decryption. The wallet separately signs delegated bond credentials internally
after verifying its owned bond and the renter's public key. Buyer acquisition and
renter key management still use the file-oriented CLI. Upgrading an existing
installation does not create a market ledger, migrate key files, start a market
service, or trigger rescans. Recovering keys from a seed does not recover
allocation history; unknown or restored ledger state must not automatically enable
issuance.

The private ledger identifier is SHA256 of the key bytes of the additional
`wallet-ledger-identity-v1` child of the application node. It identifies the same
seed/passphrase across roles and networks, is not the eight-character wallet
fingerprint, and must not be published as a market identity.

The durable seller store is shared as `jmcore.market_store`. It uses schema
version 1 with an explicit `ledger_mode` of `standalone` or `wallet`.

### Wallet Ledger Activation

The local Python API `WalletService.activate_market_ledger(history_confirmed=True)`
binds the wallet's full private identifier to the shared ledger. The equivalent
authenticated daemon endpoint is `POST /api/v1/wallet/{walletname}/market/ledger/activate`
with `{"history_confirmed":true}`. Confirmation means the operator has established
complete history, including all allocations and consumption; the API cannot
establish that fact from a seed, an empty directory, or a structurally valid backup.

Activation changes `market/seller.sqlite` to wallet mode, records a durable
`seller.sqlite.wallet-ledger` intent, binds the exact absolute
`cmtdata/commitments.json` path, and imports known consumption and external-pool
records. An `activating` state is committed before history import. Success records
`ready`; failure leaves `activating` or `recovery_required`, never a fresh empty
ledger. Repeating activation cannot clear a recovery requirement.

| Existing State | Behavior |
| --- | --- |
| No market database or intent | Existing CoinJoin behavior; no automatic market activation |
| Valid standalone store without native artifacts | Standalone seller and ordinary CoinJoin behavior |
| Standalone store with activation intent | Interrupted activation, fail closed without rebuilding |
| Valid wallet-mode store and ready wallet | Ledger governs market inventory and local PoDLE claims |
| Disabled wallet in a valid shared ledger | Ordinary local CoinJoin use remains allowed and recorded; seller writes are blocked |
| Activating or recovery-required wallet | No market issuance or local PoDLE use for that wallet |
| Missing or inconsistent wallet metadata, intent, ownership, or database | Fail closed; preserve surviving state for recovery |

One commitment-ownership row prevents a PoDLE hash from being both market
inventory and locally usable, across periods and wallet switches. An external
purchase is held locally before use, not consumed on import. The taker commits
its used claim before projecting JSON and before returning the proof. A failed
JSON write can burn an opening, but cannot release it for reuse. Selection uses
a snapshot; final use rechecks the authoritative state under the shared lock.
All operations spanning the stores take the JSON sidecar lock before SQLite.

Only one wallet session is active. Its native seller can run alongside ordinary
CoinJoin activity in that wallet. Switching wallets preserves existing
ownership, including another wallet's unconsumed external holds. Closing a taker
releases its cached ledger handle without closing a shared wallet when
`close_wallet=False`.

When a wallet has an explicit data directory, its taker must use the same
canonical directory. Mismatches are rejected before taker initialization, even
before market activation, because that wallet may activate while the taker is
running. Relative paths and directory aliases resolving to the same location are
accepted. On upgrade, callers that previously split wallet and taker state across
directories must align their configuration; no history is moved, rewritten, or
rescanned. Wallet services without an explicit data directory retain the existing
taker-configured path behavior and cannot activate a wallet ledger without one.

Known live database replacement or missing artifacts permanently stop that
manager's native use. A complete, internally consistent rollback performed while
the application is stopped cannot be detected from these local files alone.
Known-restored ledgers must be marked recovery-required. Supported explicit
maintenance is described below; it cannot reconstruct missing allocations. Do not
delete metadata or run older writers concurrently to bypass these guards. The
legacy local save-error suppression remains unchanged only before native activation.

### Native Seller Operations

These experimental endpoints require the unlocked wallet's JWT bearer token.
All paths below are relative to `/api/v1/wallet/{walletname}/market`. The daemon
rechecks authentication after acquiring its wallet lifecycle lock, including
requests queued while the wallet is locked or switched.

| Method and Path | Request or Result |
| --- | --- |
| `GET /ledger` | Read-only state, schema version, and a short diagnosis; no private wallet identifier or credential data |
| `POST /seller/start` | `bond` outpoint, `products` list, positive `price_sats`, optional `quote_ttl`; returns 202 with startup status |
| `GET /seller` | Starting, running, or stopped status, public seller identity, and sanitized startup error |
| `POST /seller/stop` | Stop this seller; keep the wallet and shared backend open |
| `POST /seller/inventory` | `{"product":"bond"}` for the configured owned bond, or `{"product":"podle","credential":...}` for a validated external opening |
| `POST /seller/payments` | Externally generated `PaymentTerms`, as in the CLI workflow |
| `GET /seller/pending` | Live signed quotes for this seller authority |
| `POST /seller/settle/{quote_id}` | One locally verified settlement form, described below |

Start accepts this shape (the outpoint is a placeholder, **NOT WIRE** data):

```json
{
  "bond": {"txid": "<64-lowercase-hex, NOT WIRE>", "vout": 0},
  "products": ["podle", "bond"],
  "price_sats": 1000,
  "quote_ttl": 300
}
```

The bond must already be present in the wallet's cached mixdepth-0 state and match
its canonical bond key, address, and script. Startup checks the backend's genesis,
height, median time, confirmations, unspent value, and locktime. It opens only an
existing ready ledger with the correct commitments binding. It does not activate
the ledger, sync the wallet, or rescan to find a bond. Directory relay uses the
configured Tor connection and a fresh nickname; native direct onion hosting is
not configured by these endpoints.

Queue inventory and Lightning invoices explicitly after startup. The native quote
lifetime is currently 1 to 900 seconds, default 300. Settlement takes
`{"preimage":"<64-lowercase-hex, NOT WIRE>","acknowledge_ln_settlement":true}`.
A Lightning preimage
must come from the operator's payment wallet or node, with explicit local
settlement acknowledgment. Remote market messages cannot settle a trade, and none
of these operations sends a payment.

For a bond sale, the wallet creates the renter's delegated certificate internally
before finalization. The bond spending key stays inside the wallet. Finalization
persists the signed package and buyer-sealed delivery before returning. If an API
response is lost or authentication expires after finalization, the buyer can still
retrieve the saved delivery. Use `jm-market seller export` to recover a finalized
package, including after expiry or restart; do not pay again based on an API error.

Seller startup and settlement chain checks allow wallet lock to proceed. Lock
revokes capabilities before stopping seller resources; a failed close keeps the
wallet reserved until cleanup succeeds. Daemon shutdown also revokes market keys
and stops its seller. Each runtime is bound to one retarget period; stop and start
it explicitly for a new period. Services do not restart automatically on unlock.

### Diagnosis, Recovery, and Directory Moves

`GET /ledger` does not create directories or files, change permissions, repair
SQLite, or import history. A journal, concurrent artifact change, or unreadable
database produces an unavailable diagnosis. Missing or inconsistent authoritative
state remains blocked. Diagnosis is an observation, never permission to skip the
normal operational checks.

Stop the daemon's seller and CoinJoin services before any ledger maintenance.
All maintenance paths below use `POST` with a JSON body:

| Path | Required Body | Supported Effect |
| --- | --- | --- |
| `/ledger/block` | `{}` | Mark the current wallet recovery-required, for example when a restore is known |
| `/ledger/recover` | `{"history_confirmed":true}` | Merge confirmed history into an intact wallet ledger in activating or recovery-required state |
| `/ledger/rebind` | `history_confirmed: true`, `writers_stopped: true`, and `previous_commitments_path` | Rebind an intact moved ledger to this wallet directory's commitments file |

Recovery requires an existing regular commitments JSON file and matching database
and intent. It persists recovery-required before reading history, then merges and
marks ready in one transaction. It never deletes tombstones, allocations,
reservations, or ownership. Confirmed used entries can permanently consume local
holds, including holds belonging to another wallet, while preserving their owner.
A false used entry therefore burns that opening; a collision with market inventory
refuses recovery. Recovery cannot activate a disabled wallet, repair standalone
interrupted activation, rebuild a missing database or intent, or prove that a
restored database includes every issued allocation.

For a directory move, stop **all writers of both directories**, retain a complete
copy of the database, its intent, and commitments history, and point the unlocked
wallet and daemon at the destination. Supply the exact previous absolute
commitments path to `/ledger/rebind`. The destination commitments file must exist.
The operation does not copy files or recreate the old directory. It records a
durable transition before merging history or changing either binding. Normal
operations refuse a pending transition; retry the same explicitly confirmed
rebind after an interruption. Successful completion retains the previous and new
paths for exact retry validation and preserves each wallet's activation state.
Rebinding does not clear a recovery requirement or detect a complete offline rollback.

## Buyer Workflow

The [signet quickstart](credential-market-overview.md) is the recommended path:
no hand-edited JSON and no key files to create. This section records what those
commands do, the options they do not show, and how to recover a failed step.

```bash
export BUYER_DATA="$HOME/.joinmarket-ng-market-buyer"

jm-market discover --data-dir "$BUYER_DATA" --human
jm-market discover --data-dir "$BUYER_DATA" --seller "$NICK" --output seller.json

jm-market request \
  --data-dir "$BUYER_DATA" \
  --listing seller.json \
  --request-file buy.json \
  --max-price-sats 1000 \
  --require-experimental-risk-ack
```

`discover --human` prints one line per authenticated seller; `--seller` writes
exactly that seller's signed listing. Selection is never automatic: an unknown
nick and a nick with two accepted listings are both errors. Without either flag,
`discover` writes the full `{"listings": [...]}` set.

`request` defaults to `--product podle` and the Lightning rail. It writes three
owner-only files bound to one purchase: `buy.json` (the request), `buy.json.keys`
(a freshly generated bundle, including the renter certificate key for a bond
request) and `buy.json.quote` (the signed quote). It prints the quote id and the
`lightning:` URI to pay externally. `--request-file` is durable private state:
rerunning the command retries that same purchase instead of creating a second
one, and a persisted request whose `.keys` file is missing fails rather than
silently rotating to a key the seller never quoted. Listings expire 60 seconds after signing, so request promptly after
discovery.

After the seller has locally finalized the trade, poll. `--request-file` resolves
the quote to `buy.json.quote`. The transport carries the package sealed to the
buyer key; `poll` unseals it and writes the decrypted seller-signed plaintext to
the owner-only file `buy.json.delivery` **before** validating it, so a malformed
delivery is preserved as potential invalid-delivery evidence.

```bash
jm-market poll --data-dir "$BUYER_DATA" --seller seller.json --request-file buy.json
```

`--seller` stays explicit so a delivery is only ever requested from the seller
you bought from. That listing may have expired by then; it is still accepted
only when its authorized seller key matches the quote. Keep the request file and
its key bundle together for retries and delivery retrieval.

Import after chain verification. Pass the quote so the import refuses a package
that was not delivered for exactly your purchase (for example, a re-sealed
delivery belonging to another buyer's allocation):

```bash
jm-market import \
  --data-dir "$BUYER_DATA" \
  --package buy.json.delivery \
  --quote buy.json.quote \
  --output market/import-result.json
```

For a bond package, pass the request's own key bundle and the wallet
fingerprint. There is no private key to extract by hand:

```bash
jm-market import \
  --data-dir "$BUYER_DATA" \
  --package rent.json.delivery \
  --quote rent.json.quote \
  --certificate-key rent.json.keys \
  --wallet-fingerprint "$WALLET_FINGERPRINT" \
  --output market/import-result.json
```

The wallet fingerprint is the eight-character fingerprint of the hot JoinMarket
wallet registry, printed by `jm-wallet info`. Bond import verifies the owner
authorization and chain stake, refuses collateral with locally verified fault
evidence, then stores the certificate as an external registry entry with
`index = -1` and path `external`. The renter certificate key remains local.

### Bond Rental Limits

A rental delegates use of a bond for one retarget period. The bond stays with its
owner and stays publicly identifiable, so a renter is neither anonymous nor
entitled to any share of a Sybil-resistance guarantee.

Each bond credential includes an owner-signed exclusive lease binding the exact
bond, renter certificate key, and period N, checked against the allocation and
authorization. The owner can still
sign another certificate, but a distinct owner-signed hot key observed on an
ordinary maker offer during N can support an owner-equivocation report. Renewing
the leased hot key is not a conflict. Ordinary JoinMarket proofs remain unchanged.

While the lease is live, the renter can inspect ordinary offers and save evidence:

```bash
jm-market proof observe \
  --data-dir "$BUYER_DATA" \
  --package rent.json.delivery \
  --request-file rent.json \
  --output conflict.json
```

This uses a fresh Tor-isolated identity, fetches the orderbook, verifies collateral,
and signs the report with the renter key. It does not send `!fill` or publish the
report. Publishing remains an explicit `proof broadcast` operation. Each receiving
node must independently observe the conflicting verified bond certificate during
N before sanctioning that exact bond through N+1. A report arriving after N cannot
retrospectively sanction a legitimate successor certificate. Persisted local
observations retain their original height and cannot apply before that height.

The lease does not prevent a renter from sharing its own key. A renter-regrant
record would not prove or prevent such sharing and is not part of this protocol.

### External-Only PoDLE Policy

`import --data-dir DIR` stores the credential in that data directory, so the
taker must run with the same directory. After importing, explicitly select
external credentials for the ordinary taker in `config.toml`:

```toml
[taker]
external_podle_mode = "only"
```

This mode uses only valid imported credentials, whose backing UTXOs are checked
against the configured Bitcoin backend and never selected as CoinJoin inputs.
If the pool is empty, invalid, exhausted, or a replacement wave needs another
credential, the CoinJoin fails rather than falling back to a local wallet-input
PoDLE. `disabled` is the default and permits the existing local behavior.

### Seller Separation

`import` records the seller's fidelity bond alongside the purchased opening. A
maker offer proving that exact bond is then excluded from the CoinJoin that uses
the opening, preventing the seller from also participating under that verified
bond. The exclusion is hard: it applies to every selection
pass of the round, including maker replacement, and is never relaxed for
liquidity.

It matches only offers whose complete fidelity bond tuple (outpoint, public key,
locktime, network) was independently verified, and only on the configured
network. Other identities the same operator may run are not covered. An external
credential imported without a recorded seller, including credentials imported by
an older version, has an unknown seller and receives no exclusion.

### Buyer Troubleshooting

- **No listings.** Usually no seller is online, which is a normal market result.
  Confirm Tor is reachable and that `network` matches the network your seller
  uses. Never move to mainnet to find inventory.
- **`listing document is invalid` from `request`.** The listing expired (they
  live 60 seconds). Output files are never overwritten with different content, so
  discover again into a **new** path and rerun `request` with the same
  `--request-file`, which keeps the buyer key and request id of that purchase:

```bash
jm-market discover --data-dir "$BUYER_DATA" --seller "$NICK" --output seller-2.json
jm-market request --data-dir "$BUYER_DATA" --listing seller-2.json \
  --request-file buy.json --max-price-sats 1000 --require-experimental-risk-ack
```

- **`refusing to overwrite existing output`.** A different file already exists at
  that path. Choose another output path; never delete a `.keys`, `.quote`, or
  `.delivery` file belonging to an unfinished purchase.
- **The wallet rejects the invoice.** It probably does not support signet
  (`lntbs`). Use a signet-capable wallet or node; nothing converts the invoice.
- **`seller has no delivery for this quote`.** The seller has not settled yet.
  Wait and poll again; the quote id stays valid for the delivery request.
- **The quote expired before payment.** Do not pay a stale invoice. Start a new
  purchase with a new `--request-file`. If you already paid and the seller never
  delivers, that is the exposure this market does not remove; it is not a
  provable fault.

## Fault Evidence And Testing

Keep the raw signed packages. For a statically invalid seller delivery, use the
raw delivery saved by `poll`; for double allocation, retain two finalized
packages for the same resource:

```bash
jm-market proof build \
  --first-package market/raw-invalid-delivery.json \
  --output market/invalid-delivery-proof.json

jm-market proof build \
  --first-package market/first-package.json \
  --second-package market/second-package.json \
  --output market/double-allocation-proof.json

jm-market proof broadcast \
  --data-dir "$BUYER_DATA" \
  --proof market/invalid-delivery-proof.json \
  --output market/proof-broadcast-result.json
```

Publishing is best-effort gossip, not consensus or a global blacklist. It does
not extend the fixed current-plus-next-period exclusion interval. Invalid
delivery evidence can reveal a PoDLE opening, so treat that opening as exposed.

Focused implementation checks are:

```bash
PYTHONPATH="jmcore/src:taker/src:jmwallet/src" \
  pytest jmcore/tests/test_credential_market.py jmcore/tests/test_external_podle.py \
  jmcore/tests/test_market_faults.py taker/tests/test_market_cli.py \
  taker/tests/test_market_store.py taker/tests/test_external_podle_pool.py \
  taker/tests/test_market_transport.py taker/tests/test_market_quickstart.py \
  taker/tests/test_market_buyer_keys.py taker/tests/test_market_lightning_only.py \
  taker/tests/test_external_podle_seller_separation.py \
  jmcore/tests/test_bond_lease_conflict.py taker/tests/test_market_lease_reporting.py

PYTHONPATH="jmcore/src:taker/src:jmwallet/src" \
  pytest -m e2e --fail-on-skip tests/e2e/test_credential_market_e2e.py
```

The direct transport e2e path uses a local TCP stand-in for Tor hidden-service
mapping. It tests direct-versus-relay behavior but does not demonstrate live
Tor onion reachability.
