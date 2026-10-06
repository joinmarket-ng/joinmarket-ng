# Back Up And Recover A Wallet

Use this procedure to restore a JoinMarket NG wallet from its BIP39 mnemonic.
Do not spend until the recovered wallet has been checked against independent
records.

## Preserve What Is Not In The Seed

Keep the mnemonic and its exact BIP39 passphrase. The passphrase selects a
different wallet and is not the same as the password that encrypts a local
wallet file. Record the file-encryption password if you still need to open that
file, but it does not derive keys.

The seed does not restore local labels, frozen UTXOs, address reservations, or
the original CoinJoin and send records. Preserve the original data directory and wallet files
when possible. An external-key fidelity bond also requires its external seed or
private key and the recorded public key, derivation path, fingerprint, network,
and locktime.

## Keep Formats Separate

Native CLI wallets use `*.mnemonic` files. JAM and `jmwalletd` use their own
encrypted `JMNG` container, which may have a `.jmdat` name. The legacy
joinmarket-clientserver `wallet.jmdat` is JMDAT, a third and incompatible
format. Do not rename, copy, or pass either `.jmdat` file to
`--mnemonic-file`; recover the BIP39 phrase with the tool appropriate to that
wallet format, then import the phrase here.

## Import And Verify

Stop the original maker, taker, wallet daemon, and any other process using this
wallet. Do not operate the old and recovered copies at the same time.

Import the mnemonic interactively into a new file:

```bash
jm-wallet import --output "$HOME/.joinmarket-ng/wallets/recovered.mnemonic"
```

The command prompts for the mnemonic and for a local file-encryption password.
It stores the words, not the BIP39 passphrase. Supply that passphrase only when
using the recovered wallet, via the hidden prompt rather than on a command line:

```bash
jm-wallet info \
  --mnemonic-file "$HOME/.joinmarket-ng/wallets/recovered.mnemonic" \
  --prompt-bip39-passphrase
```

The default-disabled `[wallet] bip39_passphrase_enabled` setting opts into
identity onboarding during import/generation, not storage of the passphrase.
A wallet imported without a passphrase is confirmed automatically the first time
it is unlocked without one. For a passphrase wallet, after verifying the intended
wallet, remember its identity; the first registered identity is also selected:

```bash
jm-wallet identity register \
  --mnemonic-file "$HOME/.joinmarket-ng/wallets/recovered.mnemonic" \
  --prompt-bip39-passphrase
```

`--mnemonic-file` is only needed for a wallet other than the configured or
default one.

When upgrading from the previous release, the first unlock compares the derived
wallet with the fingerprint that release recorded beside the mnemonic file. A
match is registered and selected automatically, with or without a passphrase, so
existing interactive and headless setups keep working without extra steps. No
rescan, reimport, or history change happens. If nothing was recorded or the
fingerprints differ, the wallet keeps working as before with a warning until you
run `jm-wallet identity register` with the intended passphrase. Recorded history
then needs no unlock, while key-deriving operations check the selected identity.
Identity metadata records that wallet and whether a passphrase is required, but
never the passphrase.

Compare the displayed wallet fingerprint, known receive addresses, and each
mixdepth balance with records from the old wallet or an independent source. A
successful import followed by `info` only proves that the phrase was accepted
and reports currently discovered data. It does not prove that all old history,
labels, freezes, address metadata, or coverage was restored.

Use `jm-wallet address --prompt-bip39-passphrase new 0` to issue a receiving
address, not the removed `jm-maker generate-address` command. The wallet command
checks address history and durably reserves the result instead of deriving a
fixed index.

Before signing, JoinMarket NG also rejects wallet-generated CoinJoin and change
outputs with locally known funding history. Fresh reservations and pending
CoinJoin records alone do not trigger this check. Intentional payment recipients
and generic PSBT signing are unchanged. This is a defense against known reuse,
not proof that an imported wallet's undiscovered history is complete. If it
rejects a transaction, inspect recovery and address allocation rather than
repeatedly retrying the same outputs.

On a pruned or assumeUTXO node, check that the blocks needed for historical
recovery are available before requesting a scan. A snapshot supplies current
unspent coins, not previously spent addresses. Finishing background validation
does not reconstruct wallet history, and a fresh seed import cannot repair
missing block data. Keep any original Core wallet database and JoinMarket NG
metadata intact. If required blocks are unavailable, recover using a node that
retains them and confirm the wallet scan succeeds. See
[pruning and assumeUTXO limitations](technical/wallet-scanning.md#pruned-nodes-and-assumeutxo).

## Check Coverage Before Rescanning

For a descriptor wallet, inspect Core's scan and coverage diagnostics first:

```bash
jm-wallet info \
  --mnemonic-file "$HOME/.joinmarket-ng/wallets/recovered.mnemonic" \
  --prompt-bip39-passphrase \
  --scan-status
```

Run an explicit `jm-wallet rescan` only when a known address or balance is still
missing and the diagnostics establish incomplete time or address-index coverage.
Use `jm-wallet rescan --help` to choose a time repair or, only for a known index
gap, `--scan-depth`. A rescan can be long-running; it is not a routine recovery
step and is not evidence that all seed-independent history was recovered.

## Recover Fidelity Bonds Only When Needed

If the old wallet held fidelity bonds, run `jm-wallet recover-bonds` with the
recovered mnemonic and the same prompted BIP39 passphrase. It explicitly scans
all 960 canonical bond locktimes and can take a long time. Do not run it merely
because recovery metadata is absent.

See [Fidelity Bond Operations](fidelity-bond-operations.md) for wallet-derived
and external-key bond recovery. A regular history scan does not establish bond
coverage.

After the address and balance comparisons match, retain the original backup and
seed-independent metadata until you have completed the intended accounting and
re-created any needed labels or freezes.
