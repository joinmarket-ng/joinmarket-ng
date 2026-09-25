# Credential Market: Buyer Quickstart

Buy a **PoDLE opening**, the ownership proof makers require before a CoinJoin,
backed by somebody else's Bitcoin output, so that proof no longer points at one
of your own coins. A maker can instead rent a **fidelity bond certificate**. You
never receive anyone's spending key.

**This is experimental.** Practice on signet: a seller can take the payment and
never deliver. Selling, bond rental, troubleshooting, and protocol details are in
the [operator guide](credential-market.md).

## Before You Start

- **A seller must be online.** "No listings" is a normal market result, not a
  broken installation, and never a reason to try mainnet.
- **Tor**, with a SOCKS listener on `127.0.0.1:9050` ([Tor setup](setup.md#tor)).
- **A signet Bitcoin backend**, Bitcoin Core or neutrino
  ([backend setup](setup.md#configure-backend)). Your client verifies the
  seller's bond and the credential itself; a listing is not evidence of stake.
- **A Lightning wallet that supports signet.** Payment is Lightning-only and
  signet invoices start with `lntbs`; testnet (`lntb`) and mainnet wallets cannot
  pay them. You pay the invoice and its routing fees yourself, because
  `jm-market` never touches a wallet or a Lightning node.

Give signet its own data directory, shared by `jm-market` and the taker that will
spend the credential: an imported credential lives there and nowhere else. Create
it with `mkdir -p ~/.joinmarket-ng-signet` and write its config file:

```toml
# ~/.joinmarket-ng-signet/config.toml
[network_config]
network = "signet"

[bitcoin]
backend_type = "descriptor_wallet"
rpc_url = "http://127.0.0.1:38332"
rpc_cookie_file = "~/.bitcoin/signet/.cookie"
```

`network = "signet"` selects both the signet directory servers and the signet
chain checks. Pass `--data-dir` after every subcommand, as shown below: without
it the defaults apply, which may be mainnet.

## Buy One PoDLE Opening

**1. See who is selling.** One line per authenticated seller, with an indicative
price. Only the signed quote is binding.

```bash
jm-market discover --data-dir ~/.joinmarket-ng-signet --human
```

**2. Pick one seller.** Nothing is chosen for you.

```bash
jm-market discover --data-dir ~/.joinmarket-ng-signet \
  --seller <nick> --output seller.json
```

**3. Ask for a quote.** Listings expire about a minute after they are signed, so
run this right after step 2.

```bash
jm-market request --data-dir ~/.joinmarket-ng-signet \
  --listing seller.json --request-file buy.json \
  --max-price-sats 5000 --require-experimental-risk-ack
```

This writes `buy.json` (the request), `buy.json.keys` (the buyer key of this
purchase alone) and `buy.json.quote` (the signed quote), then prints the quote
id and the Lightning invoice. `--max-price-sats` is a hard cap.

**4. Pay the invoice yourself**, from your signet Lightning wallet, before the
quote expires (at most 15 minutes, often 5). Nothing here pays for you.

**5. Collect the delivery.** The seller settles explicitly, so expect a human
delay and repeat this command until it succeeds. It unseals the package with
`buy.json.keys` and writes the decrypted, seller-signed result to the owner-only
file `buy.json.delivery`, which is both your credential and your evidence.

```bash
jm-market poll --data-dir ~/.joinmarket-ng-signet \
  --seller seller.json --request-file buy.json
```

**6. Import it.**

```bash
jm-market import --data-dir ~/.joinmarket-ng-signet \
  --package buy.json.delivery --quote buy.json.quote
```

`--quote` makes the import refuse anything that is not the delivery for exactly
the quote you paid. It verifies the backing output against your backend and
records the seller's bond, so a maker proving that same verified bond is excluded
from the CoinJoin that uses the opening. Keep the `buy.json*` files until the
credential is used: they drive retries, recovery, and fault evidence. Rerunning
any step with the same `--request-file` retries that purchase instead of buying
twice.

## Use What You Bought

Select external openings explicitly in the same data directory's `config.toml`:

```toml
[taker]
external_podle_mode = "only"
```

With `"only"`, a CoinJoin uses purchased openings and fails when none is usable,
including when a maker replacement needs another one; it never falls back to
revealing one of your own inputs. `"disabled"` is the default. An opening is
consumed when used, not when a CoinJoin succeeds, so keep spares.

## Limits

There is no escrow and no fair exchange: payment and delivery are not atomic.
Lightning is not anonymous here, and the seller knows which opening it sold.
Signed double allocations and invalid deliveries support fault evidence. Bond
owner equivocation also requires local observation during the exclusive lease.
Silence, withholding, and a spent backing output are not evidence. A large bond
is not a promise of honesty. To rent a bond,
recover from a failed step, or sell, continue to the
[operator guide](credential-market.md).
