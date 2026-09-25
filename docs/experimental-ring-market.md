# Experimental Ring Market

> **Warning: experimental, not audited.** Protocols and on-disk formats may
> change incompatibly, and bugs can lose funds. Start on signet. Use mainnet
> only with a dedicated wallet, dedicated LND nodes, and small amounts you can
> afford to lose. Every feature on this page is disabled by default.
>
> When one is enabled, the process logs:
>
> ```text
> EXPERIMENTAL features enabled: <feature names>. They are not audited, their protocols and on-disk formats may change incompatibly, and bugs can lose funds. See docs/experimental-ring-market.md.
> ```
>
> On mainnet a second warning follows. Do not enable these features in a wallet
> or LND deployment holding funds, channels, journals, or credentials you cannot
> independently recover.

## Features and Dependencies

### Taproot CoinJoin Pit (JMP-0010)

The `tr0` pit runs CoinJoins whose inputs and outputs are all P2TR. A wallet with
`[wallet] address_type = "p2tr"` makes a maker advertise `tr0` offers; a taker
also sets `preferred_offer_type` to a `tr0` offer. Pits never mix, so run one
maker process per pit. P2WSH fidelity bonds are shared across pits.

### Private Lightning Channel Rings (JMP-0014)

A ring turns CoinJoin change into co-funded, unannounced Taproot Lightning
channels with SCID aliases. Each ring member uses a distinct LND node per source
mixdepth, and a ring needs at least three channel participants. The taker may
participate, or three makers can form a maker-only ring. Ordinary makers can
join the same CoinJoin without joining the ring. Rings require the Taproot pit.

### Private Channel Buyouts (JMP-0011)

A buyout spends a ring channel's funding output as an input of a later Taproot
CoinJoin. The channel peer does not join that CoinJoin; it is paid over
Lightning, and the funds are held in a 2-of-2 escrow with a timelocked split
until settlement or recovery. Buyouts need an existing ring channel, the Taproot
pit, and the patched LND build in `lnd/`.

### PoDLE and Fidelity Bond Credential Market (JMP-0012, JMP-0013)

The market sells external PoDLE openings to takers and rents delegated fidelity
bond certificates to makers. It is independent of rings and buyouts. Sellers
push signed listings that last one hour and re-announce them every 30 minutes.
Payment is Lightning-only and there is no fair exchange: the seller decides when
a payment is settled and when to release a credential.

## Prerequisites

1. A synchronized Bitcoin Core descriptor-wallet backend. Light-client backends
   cannot resolve the prevouts a `tr0` maker must sign. Buyouts additionally
   need `txindex=1`.
2. Native `libsecp256k1` with MuSig2 support (see
   [advanced installation](install-advanced.md)). The loaded library must pass:

   ```sh
   python -c 'from bitcointx.core.secp256k1 import get_secp256k1; assert get_secp256k1().cap.has_musig, "Loaded libsecp256k1 lacks MuSig support"'
   ```

3. Tor for JoinMarket, and a v3 onion endpoint for every ring LND node.
4. For buyouts, the patched LND image. Read [lnd/README.md](https://github.com/joinmarket-ng/joinmarket-ng/blob/main/lnd/README.md)
   before replacing any LND binary:

   ```sh
   docker build -t jm-buyout-lnd:v0.21.3-beta lnd
   ```

5. Owner-only directories for mnemonics, LND TLS certificates and macaroons,
   market key bundles, credential packages, and buyout journals. Never paste
   invoices, preimages, credentials, or journal contents into logs or tickets.

## Step by Step

### 1. Taproot Pit Maker and Taker

Use a new dedicated configuration and wallet:

```toml
[network_config]
network = "signet"
bitcoin_network = "signet"

[wallet]
address_type = "p2tr"

[taker]
preferred_offer_type = "tr0reloffer"
```

```sh
jm-maker start --data-dir "$MAKER_DATA" --config-file "$MAKER_CONFIG" \
  --mnemonic-file "$MAKER_MNEMONIC"

jm-taker coinjoin --data-dir "$TAKER_DATA" --config-file "$TAKER_CONFIG" \
  --mnemonic-file "$TAKER_MNEMONIC" --amount "$AMOUNT_SATS" \
  --destination INTERNAL --mixdepth 0 --counterparties "$MAKER_COUNT"
```

Check that the maker advertises `tr0reloffer` or `tr0absoffer` and that the
taker only selects `tr0` offers. A P2WPKH wallet with a `tr0` preference is
rejected at startup.

### 2. Channel Rings

Map each source mixdepth to its own LND node. Cooperating processes on one host
share `node_binding_directory`; each running process has its own
`persistence_directory`.

```toml
[maker.channel_ring]
enabled = true
node_binding_directory = "/secure/jm/ln-node-bindings"
persistence_directory = "/secure/jm/ring-maker"
mixdepth_nodes = { 0 = "maker-md0" }

[maker.channel_ring.nodes.maker-md0]
lnd_grpc_url = "https://127.0.0.1:10009"
lnd_tls_cert_path = "/secure/lnd/maker-md0/tls.cert"
lnd_macaroon_path = "/secure/lnd/maker-md0/admin.macaroon"
onion_endpoint = "<maker-lnd-onion>:9735"
```

A participating taker uses `[taker.channel_ring]` with the same keys plus
`taker_participates = true` (`false` coordinates a maker-only ring). The
remaining capacity, reserve, and timeout settings are listed with their defaults
in `config.toml.template`.

Enroll every configured node once, with the pubkey read from the node's own
authenticated interface:

```sh
jm-maker enroll-ring-nodes --component maker --config-file "$MAKER_CONFIG" \
  --data-dir "$MAKER_DATA" --mnemonic-file "$MAKER_MNEMONIC" \
  --expected-node "maker-md0=$MAKER_NODE_PUBKEY" --acknowledge-prior-use
```

Use `--component taker` for the taker. Enrollment binds a node to one wallet
mixdepth; it does not prove the node was never used elsewhere.

Keep at least 100,000 confirmed sats in each ring node's on-chain LND wallet for
anchor fee bumping, funded from a source unrelated to the mixdepth. After a ring
CoinJoin confirms, check in LND that each new channel is private, active,
Taproot, and has an SCID alias.

### 3. Channel Buyout

Each LND node gets its own buyout configuration and journal. Start from
[jmswap/config.toml.template](https://github.com/joinmarket-ng/joinmarket-ng/blob/main/jmswap/config.toml.template):

```toml
[buyout]
enabled = true
network = "signet"
journal = "/secure/jm/buyout/buyer.sqlite"
lnd_endpoint = "127.0.0.1:10009"
lnd_identity = "<BUYER_LND_PUBKEY>"
lnd_tls_cert = "/secure/lnd/buyer/tls.cert"
lnd_peer_macaroon = "/secure/lnd/buyer/buyout-peer.macaroon"
lnd_escrow_macaroon = "/secure/lnd/buyer/buyout-escrow.macaroon"
bitcoin_rpc_url = "http://127.0.0.1:38332/"
bitcoin_rpc_user = "<RPC_USER>"
bitcoin_rpc_password = "<RPC_PASSWORD>"
allowed_peers = ["<COUNTERPARTY_LND_PUBKEY>"]
payout_addresses = ["<FRESH_P2TR_ADDRESS>"]
mixdepth = 0
wallet_fingerprint = "<WALLET_FINGERPRINT>"
```

Reserve one fresh payout address per intended session with
`jm-wallet address new 0 --label buyout`.

```sh
# Counterparty node, kept running:
jm-buyout --config "$COUNTERPARTY_BUYOUT_CONFIG" serve --counterparty

# Buyer node: prepare one channel and note the printed session id.
jm-buyout --config "$BUYER_BUYOUT_CONFIG" prepare \
  --peer "$COUNTERPARTY_LND_PUBKEY" --channel "$CHANNEL_TXID:$CHANNEL_VOUT"
jm-buyout --config "$BUYER_BUYOUT_CONFIG" status

# Spend it in a Taproot CoinJoin as the taker...
jm-taker coinjoin ... --buyout-config "$BUYER_BUYOUT_CONFIG" \
  --buyout-session "$SESSION_ID"
# ...or offer it as a maker input.
jm-maker start ... --buyout-config "$BUYER_BUYOUT_CONFIG" \
  --buyout-session "$SESSION_ID"

# After broadcast, keep the buyer monitor running until settlement.
jm-buyout --config "$BUYER_BUYOUT_CONFIG" serve
```

`status` reads only the local journal. Confirm chain and LND state independently.
Buyout and ring change cannot fund the same round.

### 4. Buy a PoDLE or Rent a Bond

A taker that must use purchased openings sets:

```toml
[taker]
external_podle_mode = "only"
```

With `"only"` the taker never falls back to a wallet PoDLE; with no usable
opening the CoinJoin stops. Use the taker's data directory as the market data
directory.

```sh
jm-market discover --data-dir "$DATA" --human
jm-market discover --data-dir "$DATA" --seller "$SELLER_NICK" --output seller.json
jm-market request --data-dir "$DATA" --listing seller.json --product podle \
  --max-price-sats "$MAX_PRICE_SATS" --request-file podle.json \
  --require-experimental-risk-ack
```

Verify the signed quote and invoice (network, amount, seller) and pay it from
your own Lightning wallet. Then:

```sh
jm-market poll --data-dir "$DATA" --seller seller.json --request-file podle.json
jm-market import --data-dir "$DATA" --package podle.json.delivery \
  --quote podle.json.quote
```

For a bond rental use `--product bond`, and import with
`--certificate-key bond.json.keys --wallet-fingerprint "$WALLET_FINGERPRINT"`.

### 5. Sell Credentials

Selling needs seller keys, an offline-signed bond authorization, inventory, and
pre-generated Lightning invoices. The full sequence (`keygen`, `public`,
`authorize`, `export-podle`, `seller add-inventory`, `seller add-payment`,
`serve`, `seller settle`) and the wallet-native `jmwalletd` seller API are in the
[credential market reference](credential-market.md). Never release a delivery
before verifying payment in your own Lightning node.

## Mainnet Notes

- Move to mainnet only after the same workflow succeeded on signet, never
  because signet lacks liquidity.
- Use a new wallet and dedicated LND nodes. Do not share an LND node between
  mixdepths or use a ring node for other activity.
- Budget mining fees, LND reserves, routing fees, and buyout recovery fees
  separately from the CoinJoin amount.
- Check that JoinMarket, Bitcoin Core, LND, invoices, and addresses all use the
  same network.
- Never trade unattended on the credential market.

## Stop Conditions and Recovery

Stop and investigate if a ring record is corrupt, blocked, or unresolved. Never
delete ring journals, binding registries, wallet metadata, commitment files,
market stores, LND state, or buyout journals to retry.

```sh
jm-maker ring-records --component maker --config-file "$MAKER_CONFIG"
jm-maker ring-records --component taker --config-file "$TAKER_CONFIG"
```

If `retirement_action` is `blocked`, keep the listed inputs unspent and stop
every process sharing that wallet until the funding transaction or a confirmed
conflict resolves it.

For buyouts, keep the original configuration, journal, LND node, and payout
addresses:

```sh
jm-buyout --config "$CONFIG" status
jm-buyout --config "$CONFIG" cancel --session "$SESSION_ID"
jm-buyout --config "$CONFIG" force-close --session "$SESSION_ID"
```

`cancel` works only before parent signing. `force-close` is for an unpaid signed
session and costs on-chain fees. Never force-close, or pay again, after an
uncertain Lightning payment; keep `serve` running instead.

For market purchases keep the request file and its `.keys`, `.quote`, and
`.delivery` sidecars. Retry with the same request file; never pay twice because
a response was lost.

## Known Limitations

- Ring makers do not apply the ordinary maker minimum fee rate to ring
  transactions. Watch the actual fee rate.
- In maker-only rings, ring makers can infer the taker's ordinary change.
- Ring participants learn more than chain observers. Lightning nodes, route
  hints, and payments are not anonymous.
- Market payment and delivery are not a fair exchange; withheld delivery is not
  provable.
- A rented fidelity bond is public and valid for one period.
- Buyout timeout-split fee bumping is not automatic.
- The LND patches are not upstream and their database records may change.

Further reading: [credential market reference](credential-market.md),
[buyout reference](https://github.com/joinmarket-ng/joinmarket-ng/blob/main/jmswap/README.md), [patched LND](https://github.com/joinmarket-ng/joinmarket-ng/blob/main/lnd/README.md),
[privacy](technical/privacy.md), [protocol](technical/protocol.md), and the
JMP-0010 to JMP-0014 drafts.
