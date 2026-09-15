# Credential Market: Overview

The credential market lets takers buy a proof to use when starting a CoinJoin,
and lets makers rent the use of a fidelity bond. It trades proofs and
certificates, not control of anyone's bitcoin.

**This is experimental.** Start on regtest, keep purchases small, and do not
treat the market as a guarantee against losing a payment.

## What Can You Buy?

### An External CoinJoin Proof

Before revealing which coins they can contribute, makers require an ownership proof
called PoDLE. Normally, the taker creates it using one of its own CoinJoin
inputs. This tells the participating makers that this particular input belongs
to the taker.

A purchased proof comes from somebody else's Bitcoin output. That output stays
outside your CoinJoin, so this authentication step no longer identifies one of
your inputs. You do not receive its spending key or spend the provider's coins.

The proof is consumed when you attempt to use it, not only when a CoinJoin
succeeds. A failed attempt or replacing makers can require another proof, so
keep a few spares. The provider must also leave the backing output unspent.

### The Use of a Fidelity Bond

A maker can rent a certificate that lets it operate using someone else's
fidelity bond until the certificate expires. The owner keeps the bond and its
spending key; the renter supplies its own CoinJoin liquidity and keeps its own
certificate key.

Owners can earn rental income without operating a maker. Renters can operate
with a bond without locking that capital themselves. Neither rental income nor
maker income is guaranteed. The bond remains identifiable, so renting one does
not make a maker anonymous.

## A Typical Purchase

1. **Find a seller.** Sellers advertise through the existing JoinMarket
   directories. Private conversations prefer a direct Tor connection and use
   encrypted directory relay when that is unavailable.
2. **Review a quote.** Choose what you need and your maximum price. The client
   checks the seller's signed terms and backing fidelity bond. Note the quote's
   expiration time before paying.
3. **Pay with your usual wallet.** Choose onchain or Lightning. The market does
   not send the payment for you or connect to your Lightning node. Onchain
   payments need enough time for the required confirmations.
4. **Collect the delivery.** The seller checks the payment and confirms it
   locally. You receive an encrypted package that only your trade key can open.
5. **Import and use it.** Your taker can use imported PoDLE proofs in its normal
   CoinJoin flow, or your maker can use the imported bond certificate.

With the **external-only** taker setting, running out of usable purchased proofs
stops the attempt. It does not silently switch back to revealing a wallet-input
proof. Buying and importing proofs can happen separately from mixing your coins.

## What Sellers Do

Sellers prepare their inventory and payment requests, then run the market
service. The service handles discovery, private quotes, reservations, and
delivery. For a bond rental, the owner signs a certificate for the renter's
public key offline; the spending key never needs to reach the online seller.

Sellers confirm payments using their external wallet and a local market command.
The service records completed sales before releasing them, preventing accidental
resale across restarts or concurrent processes. Keep that state and the relevant
keys: restoring an old inventory backup can undo those protections.

## What Protects You?

Sellers back their market activity with a fidelity bond. Signed records can
prove certain kinds of cheating, such as selling the same proof twice or
delivering a signed, invalid credential. A buyer can publish that evidence.
Upgraded clients then temporarily stop choosing makers backed by that bond,
including for ordinary CoinJoins. The owner risks earning opportunities, not
confiscation of its locked bitcoin.

A buyer's accusation alone is not enough. Other clients verify the evidence
themselves, without requiring the seller to come online and defend itself.
A timeout or a lost connection is not proof of cheating.

**Payment and delivery are not guaranteed to happen together.** A seller can
take payment and withhold delivery, or spend a proof's backing output before
you use it. Those failures are not generally provable with this system. Keep
your exposure limited, and do not assume that a large bond guarantees honesty.

## Privacy and Everyday Use

- Use a fresh buyer key bundle for each trade. Keep payments separate from the
  coins you plan to mix.
- The provider knows which PoDLE proof it sold and may recognize when it is
  used. Lightning and Tor reduce some exposure; they do not erase every link.
- Keep quotes, delivery files, and keys private. Inspect cheating evidence
  before publishing it, because it may disclose private information.
- JSON files and command-line operations can be scripted around existing
  workflows. Buying stock and confirming payments remain explicit actions;
  there is no automatic replenishment or Lightning-node payment integration.
- Unpaid buyers can still hold reservations and tie up inventory. Sellers
  should balance longer payment windows against this risk.

For installation, configuration, and commands, continue to the
[setup and operator guide](credential-market.md).
