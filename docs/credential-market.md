# Experimental Credential Market

For a simple explanation of buying, selling, privacy, and the main risks, start
with the [user overview](credential-market-overview.md). This page covers setup
and commands.

`jm-market` is an opt-in native directory market with a scriptable JSON CLI for
external PoDLE openings and delegated fidelity-bond certificates. It does not create wallets,
send payments, run a Lightning node, broadcast Bitcoin transactions, hold
escrow, or provide fair exchange. Use it first on regtest with a small, capped
exposure. Do not run unattended mainnet sales or purchases.

The file-oriented seller commands below require a legacy, nonactivated seller
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

- Buyers should use a fresh market key bundle and Tor identity per trade. Do not use payment
  funds as forthcoming CoinJoin inputs.
- A seller knows the PoDLE commitment it sold and can correlate it with public
  `!hp2` timing. Lightning is not anonymous here: invoice-node identity and
  routing or endpoint relationships remain observable. Onchain payment funding
  can also cluster the buyer's coins.
- Verify a quoted bond through your configured Bitcoin backend before paying:
  the precise outpoint, P2WSH script, positive confirmed unspent value, and
  future locktime must all match. A listing is not evidence of stake.
- The JoinMarket messaging network and the Bitcoin network are separate. The
  market transport uses `network_config.network`; chain, payment-address, and
  credential checks use `bitcoin_network` when set, otherwise that network.
  Do not infer the Bitcoin network from a directory's messaging network.

Only two seller-signed contradictions can produce a sanction: conflicting
finalized allocations of the same resource, or a signed delivery with a
statically invalid credential. There are no interactive accusation votes or
defense timeouts. Nonpayment, payment disputes, message timeouts, withholding,
spent backing UTXOs, changed confirmations, or unavailable chain data are not
proofs. A valid proof excludes the verified bond from upgraded taker selection
from the fault period through the full following period. It does not confiscate
bond capital or create onchain slashing.

Version-1 sales support only PoDLE indices 0, 1, and 2. Portable external records
can represent other indices, but this market rejects them rather than selling
credentials that standard makers cannot use. Inspect invalid-delivery evidence
before publishing it: it includes the exact signed credential, potentially with
arbitrary seller-supplied private information.

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

Generate every payment request in an external wallet or Lightning node before
adding it. The market neither owns nor contacts that wallet or node. Queue one
fresh payment request per possible sale. This onchain `PaymentTerms` example is
**NOT WIRE** and uses a regtest placeholder, never a real mainnet address:

```json
{
  "rail": "onchain",
  "request": "<regtest-bitcoin-address, NOT WIRE>",
  "amount_sats": 1000,
  "min_confirmations": 1
}
```

For Lightning, use the same required fields with `"rail": "lightning"` and an
externally generated, exact-amount BOLT11 string in `request`. The BOLT11 value
shown as `<bolt11-invoice, NOT WIRE>` is a placeholder, not an invoice.

```bash
jm-market seller add-payment \
  --data-dir "$MARKET_DATA" \
  --terms market/payment-onchain.json \
  --ttl 300
```

`add-payment --ttl` is a 1 to 900 second validation horizon, primarily so a
BOLT11 invoice must outlive the immediate queueing check. It is distinct from
the quote lifetime. Onchain settlement requires the exact unspent
`txid:vout`, matching script and exact amount, with at least
`min_confirmations` confirmations. A transaction ID alone is insufficient.

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
  --quote-ttl 21600 \
  --direct-location "<seller-onion-host:port>" \
  --listen-host 127.0.0.1 \
  --listen-port 9735
```

Without a separately configured onion service, omit `--direct-location`,
`--listen-host`, and `--listen-port`; directory relay remains available. The
default quote lifetime is 300 seconds. Onchain quotes may be as long as 86,400
seconds; `21600` is a practical confirmation window, but each unpaid quote
reserves inventory and can be used for griefing. Lightning quotes are
automatically clamped to 900 seconds. The store permits at most 64 live quotes
and the service admits at most one new quote per second globally. These are
resource bounds, not DoS or Sybil protection.

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

For an onchain quote, use the externally observed and independently verified
settlement output. Onchain settlement verification requires a full-node
(Bitcoin Core) backend; a neutrino seller cannot resolve an arbitrary
settlement outpoint and the command fails closed. For Lightning, first obtain
a local confirmation from the external wallet or node, save the 32-byte
preimage in an owner-only file, and give the explicit acknowledgment. A
remotely claimed preimage never settles a trade.

```bash
jm-market seller settle \
  --data-dir "$MARKET_DATA" \
  --quote-id "$QUOTE_ID" \
  --keys market/seller-keys.json \
  --onchain-outpoint "$TXID:$VOUT" \
  --output market/finalized-package.json

jm-market seller settle \
  --data-dir "$MARKET_DATA" \
  --quote-id "$QUOTE_ID" \
  --keys market/seller-keys.json \
  --preimage-file market/settled-preimage.bin \
  --acknowledge-ln-settlement \
  --output market/finalized-package.json
```

Use exactly one settlement form. Finalization atomically consumes the inventory
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

The existing durable seller store is now shared as `jmcore.market_store`, with
compatible imports retained in `taker.market_store`. New stores retain the
version-1 schema until an explicit activation operation.

### Wallet Ledger Activation

The local Python API `WalletService.activate_market_ledger(history_confirmed=True)`
binds the wallet's full private identifier to the shared ledger. The equivalent
authenticated daemon endpoint is `POST /api/v1/wallet/{walletname}/market/ledger/activate`
with `{"history_confirmed":true}`. Confirmation means the operator has established
complete history, including all allocations and consumption; the API cannot
establish that fact from a seed, an empty directory, or a structurally valid backup.

Activation upgrades `market/seller.sqlite` to version 2, records a durable
`seller.sqlite.wallet-ledger` intent, binds the exact absolute
`cmtdata/commitments.json` path, and imports known consumption and external-pool
records. An `activating` state is committed before history import. Success records
`ready`; failure leaves `activating` or `recovery_required`, never a fresh empty
ledger. Repeating activation cannot clear a recovery requirement.

| Existing State | Behavior |
| --- | --- |
| No market database or intent | Existing CoinJoin behavior; no automatic market activation |
| Valid version-1 store without native artifacts | Existing seller and CoinJoin behavior; no automatic migration |
| Version-1 store with activation intent | Interrupted activation, fail closed without rebuilding |
| Valid version-2 store and ready wallet | Ledger governs market inventory and local PoDLE claims |
| Disabled wallet in a valid shared ledger | Ordinary local CoinJoin use remains allowed and recorded; seller writes are blocked |
| Activating or recovery-required wallet | No market issuance or local PoDLE use for that wallet |
| Missing or inconsistent version-2 metadata, intent, ownership, or database | Fail closed; preserve surviving state for recovery |

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

Queue inventory and payment requests explicitly after startup. The native quote
lifetime is currently 1 to 900 seconds, default 300. Settlement accepts either
`{"onchain_outpoint":"<txid:vout, NOT WIRE>"}` or
`{"preimage":"<64-lowercase-hex, NOT WIRE>","acknowledge_ln_settlement":true}`.
On-chain verification requires a full-node backend and an exact confirmed unspent
output. A Lightning preimage must come from the operator's payment wallet or node,
with explicit local settlement acknowledgment. Remote market messages cannot
settle a trade, and none of these operations sends a payment.

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
| `/ledger/recover` | `{"history_confirmed":true}` | Merge confirmed history into an intact version-2 ledger in activating or recovery-required state |
| `/ledger/rebind` | `history_confirmed: true`, `writers_stopped: true`, and `previous_commitments_path` | Rebind an intact moved ledger to this wallet directory's commitments file |

Recovery requires an existing regular commitments JSON file and matching database
and intent. It persists recovery-required before reading history, then merges and
marks ready in one transaction. It never deletes tombstones, allocations,
reservations, or ownership. Confirmed used entries can permanently consume local
holds, including holds belonging to another wallet, while preserving their owner.
A false used entry therefore burns that opening; a collision with market inventory
refuses recovery. Recovery cannot activate a disabled wallet, repair version-1
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

Generate a buyer-specific bundle. For a bond request, this creates the separate
renter certificate key bound into the request and eventual certificate.

```bash
export BUYER_DATA="$HOME/.joinmarket-ng-market-buyer"
umask 077
mkdir -p market
jm-market keygen --output market/buyer-keys.json
jm-market public --keys market/buyer-keys.json --output market/buyer-public.json

jm-market discover \
  --data-dir "$BUYER_DATA" \
  --timeout 30 \
  --output market/listings.json
```

`discover` writes `{"listings": [...]}`. Select one complete listing object,
including its `seller_nick`, into `market/selected-listing.json`; there is no
automatic provider selection. Check the listing network, advertised products,
and indicative price before requesting.

```bash
jm-market request \
  --data-dir "$BUYER_DATA" \
  --listing market/selected-listing.json \
  --keys market/buyer-keys.json \
  --product bond \
  --rail onchain \
  --max-price-sats 1000 \
  --request-file market/bond-request.json \
  --output market/quote.json \
  --require-experimental-risk-ack
```

For a PoDLE purchase, use `--product podle`. `--request-file` is durable
private state: retry the same intended request with that file, rather than
creating a second request. The output contains the signed quote and a
`payment_uri` (`bitcoin:` BIP21 or `lightning:` URI). Verify the quote and pay
that URI only with an external wallet. The command never sends payment.

After the seller has locally finalized the trade, poll with the original quote,
seller listing, and buyer bundle. Poll preserves the raw seller-signed package
before validating it, which retains potential evidence of invalid delivery.

```bash
jm-market poll \
  --data-dir "$BUYER_DATA" \
  --quote market/quote.json \
  --seller market/selected-listing.json \
  --keys market/buyer-keys.json \
  --raw-output market/raw-delivery.json
```

The seller listing may have expired by the time of polling. It is still accepted
only when its authorized seller key matches the quote.

Import a PoDLE package after chain verification. Pass the original quote so the
import refuses a package that was not delivered for exactly your purchase (for
example, a re-sealed delivery that belongs to another buyer's allocation):

```bash
jm-market import \
  --data-dir "$BUYER_DATA" \
  --package market/raw-delivery.json \
  --quote market/quote.json \
  --output market/import-result.json
```

For a bond package, extract the buyer bundle's `renter_certificate_key` into an
owner-only single-secret file before import. The import command intentionally
accepts a key file, not a bundle, for this role.

```bash
umask 077
jq -r '.renter_certificate_key' market/buyer-keys.json > market/renter-certificate.key

jm-market import \
  --data-dir "$BUYER_DATA" \
  --package market/raw-delivery.json \
  --quote market/quote.json \
  --certificate-key market/renter-certificate.key \
  --wallet-fingerprint "$WALLET_FINGERPRINT" \
  --output market/import-result.json
```

The wallet fingerprint is the eight-character fingerprint for the hot
JoinMarket wallet registry. Bond import verifies the owner authorization and
chain stake, refuses collateral with locally verified fault evidence, then
stores the certificate as an external registry entry with `index = -1` and
path `external`. The renter certificate key remains local.

### External-Only PoDLE Policy

After importing externally acquired PoDLE credentials, explicitly select them
for the ordinary taker. In `config.toml`:

```toml
[taker]
external_podle_mode = "only"
```

This mode uses only valid imported credentials, whose backing UTXOs are checked
against the configured Bitcoin backend and never selected as CoinJoin inputs.
If the pool is empty, invalid, exhausted, or a replacement wave needs another
credential, the CoinJoin fails rather than falling back to a local wallet-input
PoDLE. `disabled` is the default and permits the existing local behavior.

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
  taker/tests/test_market_transport.py

PYTHONPATH="jmcore/src:taker/src:jmwallet/src" \
  pytest -m e2e --fail-on-skip tests/e2e/test_credential_market_e2e.py
```

The direct transport e2e path uses a local TCP stand-in for Tor hidden-service
mapping. It tests direct-versus-relay behavior but does not demonstrate live
Tor onion reachability.
