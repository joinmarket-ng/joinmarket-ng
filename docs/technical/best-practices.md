# Protect Your Funds And Privacy

CoinJoin is one part of a spending path. What you reveal before and after it
can matter as much as the transaction itself.

## Backups

Keep recovery words offline and retain the exact BIP39 passphrase if you use
one. Preserve wallet files and private metadata before an upgrade or migration;
the seed does not restore labels, frozen state, or address reservations.
External-key fidelity bonds need the external signer's recovery material too.
Follow [backups and recovery](../recover-wallet.md).

## Mixdepth Hygiene

Keep funds with unrelated histories separate. Use a fresh receiving address
for each deposit, and do not combine CoinJoin outputs with their linkable change
in a later payment. Mixdepths help preserve this separation, but their numbers
are not a privacy score.

A sweep avoids creating taker change but links all inputs it spends.
Consolidating for lower future fees has a privacy cost. Consider who already
knows those inputs belong to you before accepting that link.

## Payments After CoinJoin

Check the destination and source coins before every payment. A recipient knows
what you paid them; an exchange may already know your identity. Distinctive
amounts, close timing, and recombining several payouts can reveal links.
Neither a round count nor a larger number of equal outputs guarantees anonymity.

For a longer strategy, the [tumbler](../README-tumbler.md) uses several
destinations and waits. Do not immediately recombine its payouts or treat its
completion as a privacy certificate.

## Fidelity Bonds

A bond is publicly associated with its maker. Consider the funding history
before locking funds, and understand when and how you can redeem them.
CoinJoin does not guarantee that a bond's origin is hidden.

A separate signer can keep the bond key away from the online maker, but adds
backup and compatibility responsibilities. Test the complete signing and
redemption workflow without valuable funds first. Use the maintained
[bond operations guide](../fidelity-bond-operations.md), not a generic hardware
wallet compatibility assumption.

## Taker Operation

Review both maker and mining fees. Use Tor and multiple directories. Repeated
attempts are not free of privacy cost: authentication reveals a proof coin to
participating makers. Stop to understand repeated failures rather than
lowering safeguards until a transaction succeeds.

## Maker Operation

Treat the online host as a hot wallet. Keep only funds you are prepared to
expose to that risk, protect its credentials, and monitor offers and completed
transactions. Earnings and uptime do not establish that an installation is safe.

## Getting Help

Do not publish addresses, transaction history, config files, or screenshots
without reviewing their contents. Use the [diagnostic reporting procedure](../troubleshooting.md#report-a-problem)
and never share recovery secrets. For the limits of these precautions, see the
[threat model](threat-model.md).
