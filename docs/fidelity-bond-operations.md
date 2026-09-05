# Fidelity Bond Operations

This guide covers creating, operating, renewing, and redeeming fidelity bonds.
For the privacy model, bond-value calculation, and proof format, see
[Privacy: Fidelity Bonds](technical/privacy.md#fidelity-bonds).

Fidelity bonds lock funds with `OP_CHECKLOCKTIMEVERIFY`. A wrong key, locktime,
derivation path, or unsupported signer can make redemption difficult. Complete
an unfunded signing test before sending funds to a bond address.

## Choose A Custody Model

| Model | Create | Redeem | Operational tradeoff |
|---|---|---|---|
| Wallet-derived bond | `generate-bond-address` | `jm-wallet send --select-utxos` | Simpler, but the active wallet seed controls the bond |
| External-key bond | `create-bond-address` | PSBT signed by the external key | Bond funds can remain separate from the online maker |

An external-key bond uses two keypairs:

- The **bond key** controls the locked UTXO and signs a delegated certificate.
- The **certificate key** stays with the online maker and signs per-session nick proofs.

Compromise of the certificate key can enable maker impersonation through the
signed certificate expiry boundary. Reference and JoinMarket NG takers reject
the proof once chain height exceeds that boundary. Spending the bond removes its
value; the certificate key does not control the bond funds.

## Backend And Identity

There is no separate cold-storage backend. Commands that need chain state use
the normal `[bitcoin]` backend from `config.toml`, either `descriptor_wallet` or
`neutrino`. See [Configure Backend](install.md#configure-backend).

| Operation | Backend requirement |
|---|---|
| Create an external bond address | Offline; pass `--network` explicitly outside mainnet |
| Generate the hot certificate key | Offline |
| Prepare or import a certificate | Configured backend, or `--current-block <height>` offline |
| Build a redemption PSBT | Offline after funded UTXO data has been synced into the registry |
| `sync-bonds` / `recover-bonds` | Configured backend and the JoinMarket wallet |

Two fingerprints appear in the external-key workflow:

- `--wallet-fingerprint` is the 8-character fingerprint printed by
  `jm-wallet info` for the **hot JoinMarket wallet**. It selects that wallet's
  `fidelity_bonds_<fingerprint>.json` registry.
- `--master-fingerprint` is the 4-byte BIP32 fingerprint of the **external
  signing wallet**. It is stored in the redemption PSBT with the derivation
  path so a signer can locate the bond key.

They are often different. Do not substitute one for the other.

## Wallet-Derived Bonds

Create a future timelock using the active wallet:

```bash
jm-wallet generate-bond-address \
  --locktime-date <future-YYYY-MM> \
  --mnemonic-file <hot-wallet.mnemonic>
```

Fund the printed P2WSH address once, wait for confirmation, then refresh the
registry:

```bash
jm-wallet sync-bonds --mnemonic-file <hot-wallet.mnemonic>
jm-wallet list-bonds --mnemonic-file <hot-wallet.mnemonic>
```

A fidelity bond is one UTXO. Sending funds to the same address again creates an
additional locked UTXO; it does not increase the advertised bond's value. The
registry records those extras, but only the largest UTXO at the address is
announced.

After expiry, select and spend a wallet-derived bond directly:

```bash
jm-wallet send <destination> \
  --mnemonic-file <hot-wallet.mnemonic> \
  --select-utxos \
  --amount 0
```

The wallet sets the CLTV transaction fields and witness automatically.
Unexpired bonds cannot be selected, and automatic selection always excludes
bonds.

## External-Key Bonds

Use a dedicated seed or hardware wallet for the bond key. A standard BIP84
receive or change key, such as `m/84'/0'/0'/0/0` on mainnet, is suitable. The
bond script is built from the public key itself; the reference implementation's
nonstandard `/2` branch is not required.

Before starting, record:

- the compressed public key (33-byte hex),
- its full BIP32 derivation path,
- the external wallet's 4-byte master fingerprint,
- the hot JoinMarket wallet's 8-character fingerprint,
- the network and intended future locktime.

### SeedSigner BIP46 Registration

SeedSigner can export a BIP46 bond registration payload for its canonical
fidelity-bond branch. Import that payload before funding. The importer accepts
one compact JSON argument or exactly one `--file` option:

```bash
jm-wallet import-bond-registration \
  --file seedsigner-bip46-registration.json \
  --wallet-fingerprint <hot-wallet-fingerprint>
```

`--wallet-fingerprint` still selects the hot JoinMarket wallet registry. It is
not the `master_fingerprint` inside the payload, which identifies the SeedSigner
bond key. The import validates the compressed public key and canonical compact
ASCII JSON contract, then independently reconstructs the timelock script and
P2WSH address. It rejects old xpub-shaped exports, unrecognized fields, and any
payload whose address does not match that reconstruction. No xpub is stored.
The master fingerprint and derivation path are signer-routing metadata, not
proof that the leaf public key belongs to that master key. The signer verifies
that relationship when it processes the redemption PSBT.

The imported entry stores the full signer derivation path and signer master
fingerprint. For that entry, `spend-bond` automatically embeds both values in
the redemption PSBT when neither `--master-fingerprint` nor `--derivation-path`
is supplied. Supplying one of those options still requires supplying the other.
Manual `create-bond-address` entries remain supported and continue to require
the explicit paired options when signer origin data is not stored.

Normal `create-bond-address` use rejects past and current locktimes. The
`--allow-expired` override exists only for recovery and deterministic testing.

Use this order for a SeedSigner bond:

1. Export and import/register the payload without funding the address.
2. Generate the certificate key, prepare the certificate message, sign it with
   the SeedSigner bond key, and import the certificate.
3. Run `spend-bond --test-unfunded`, sign the PSBT with the exact signer, and
   run the finalizer successfully.
4. Only then fund the address independently reconstructed from the SeedSigner
   payload, once.

### Signer Compatibility

Most hardware wallets reject custom CLTV P2WSH scripts. Compatibility changes
with firmware, host software, and Bitcoin app versions, so test the exact device
and setup before funding.

Known paths include:

| Signer | Status |
|---|---|
| Blockstream Jade via HWI | **Verified signing** on the classic Jade with HWI 3.2.0 and cbor2 5.9.0; always run the finalizer to verify the returned signature |
| Specter DIY | QR PSBT exchange; does not depend on HWI support |
| Ledger legacy Bitcoin app | Historically supported; current apps have been reported to reject bond PSBTs with `0x6a80` ([issue #552](https://github.com/joinmarket-ng/joinmarket-ng/issues/552)) |
| Digital BitBox / BitBox01 | **Expected, untested**: [HWI supports arbitrary witness scripts](https://hwi.readthedocs.io/en/latest/devices/index.html#support-matrix) and computes BIP143 `SIGHASH_ALL` host-side; the [EOL device](https://blog.bitbox.swiss/en/the-end-of-the-road-for-the-bitbox01/) signs raw digests but cannot display transaction details |
| Trezor, Coldcard, BitBox02, KeepKey | Known to reject or not support this custom script path; plan for mnemonic signing |

For Jade, the HWI host environment matters. `cbor2` 5.8.0 changed buffered
stream reads in a way that breaks Jade serial responses ([HWI issue
#817](https://github.com/bitcoin-core/HWI/issues/817)). A device may complete
confirmation and signing, then HWI fails with `error decoding unicode string`
before returning the signed PSBT. This is a host transport failure, not a
firmware policy rejection; retry the original unsigned PSBT after fixing the
environment. `sign_bond_psbt.py` reports the installed version and refuses this
known-broken combination before signing.

HWI 3.2.0 with `cbor2` 5.9.0 successfully returned the signed test PSBT from a
classic Jade. HWI 3.2.0's published metadata still requires `cbor2<5.8`, so a
normal install selects 5.7.1; that version works with Jade but has a known
[security issue](https://github.com/advisories/GHSA-3c37-wwvx-h642). Until upstream [PR #832](https://github.com/bitcoin-core/HWI/pull/832)
is released, use a dedicated Python 3.9 through 3.12 environment and knowingly
override only this dependency:

```bash
python3.12 -m venv .hwi-venv
. .hwi-venv/bin/activate
python -m pip install 'hwi==3.2.0'
python -m pip install --no-deps 'cbor2>=5.9,<6'
```

This override conflicts with HWI 3.2.0's stale package metadata, but 5.9.0 is
covered by upstream hardware-in-the-loop testing and the classic Jade test
above. Recheck upstream constraints when a newer HWI release is available.

BitBox01 requires its host-side device password before HWI can enumerate or
sign. The helper prompts without echo when both options are supplied:

```bash
python scripts/sign_bond_psbt.py \
  --chain <main|test|signet|regtest> \
  --device-type digitalbitbox \
  --device-password \
  --no-broadcast \
  --file unsigned-bond-test.psbt > signed-bond-test.psbt
```

This makes the expected path testable but does not change its evidence level;
the physical device and this CLTV script combination have not been tested.

Sparrow is useful for key management and certificate message signing, but it
does not finalize this CLTV witness script. The repository finalizer handles a
signed PSBT returned by HWI or a QR signer.

### Create And Certify

1. Obtain a compressed public key from the external wallet. In Sparrow, use the
   Addresses tab, record the derivation path, and find the master fingerprint
   under Settings and Keystores.

2. Create the bond address without funding it. Outside mainnet, pass the network
   explicitly.

   ```bash
   jm-wallet create-bond-address <compressed-pubkey> \
     --locktime-date <future-YYYY-MM> \
     --network <mainnet|testnet|signet|regtest> \
     --wallet-fingerprint <hot-wallet-fingerprint>
   ```

3. Generate the online certificate key and associate it with the registry
   entry.

   ```bash
   jm-wallet generate-hot-keypair \
     --bond-address <bond-address> \
     --wallet-fingerprint <hot-wallet-fingerprint>
   ```

4. Prepare the certificate message. This uses the configured backend to get the
   current block height:

   ```bash
   jm-wallet prepare-certificate-message <bond-address> \
     --validity-periods 52 \
     --wallet-fingerprint <hot-wallet-fingerprint>
   ```

   For an offline machine, obtain the current height from a trusted source and
   add `--current-block <height>`. Record the absolute `Cert Expiry` period and
   copy the complete `fidelity-bond-cert|...` message.

5. Sign the message with the external bond key. In Sparrow, use Tools and
   Sign/Verify Message, select the P2WPKH address corresponding to the recorded
   public key, choose Standard (Electrum), and sign the exact message.

6. Import the signature with the same absolute period:

   ```bash
   jm-wallet import-certificate <bond-address> \
     --cert-signature '<base64-signature>' \
     --cert-expiry <absolute-period> \
     --wallet-fingerprint <hot-wallet-fingerprint>
   ```

   Use the same `--current-block <height>` option when importing fully offline.

### Test Redemption Before Funding

Generate a synthetic, non-broadcastable PSBT using the exact external key
origin you recorded:

```bash
jm-wallet spend-bond <bond-address> <destination-you-control> \
  --fee-rate <current-sat-vB> \
  --test-unfunded \
  --master-fingerprint <external-master-fingerprint> \
  --derivation-path "<external-key-path>" \
  --wallet-fingerprint <hot-wallet-fingerprint> \
  --output unsigned-bond-test.psbt
```

For HWI, specify the chain and request a signed PSBT without broadcast:

```bash
python scripts/sign_bond_psbt.py \
  --chain <main|test|signet|regtest> \
  --no-broadcast \
  --file unsigned-bond-test.psbt > signed-bond-test.psbt

python scripts/finalize_bond_psbt.py --file signed-bond-test.psbt
```

For a QR signer, transfer `unsigned-bond-test.psbt`, sign it, save the returned
PSBT as `signed-bond-test.psbt`, then run the same finalizer.

For mnemonic signing:

```bash
python scripts/sign_bond_mnemonic.py --file unsigned-bond-test.psbt
```

Add `--passphrase` only to `sign_bond_mnemonic.py` when the external seed uses a
BIP39 passphrase. Before requesting the mnemonic, the signer displays the
locktime, every output script and amount, total output, and fee. Review those
values and explicitly confirm the transaction. It then prints final transaction
hex directly.

The signer refuses noninteractive use unless `--force` is supplied for
controlled automation. This bypasses only the confirmation prompt, not the
PSBT, CLTV, amount, or key validation.

The test input does not exist and cannot be broadcast. Success means the signer
derived the intended key and produced a signature that verifies against the
CLTV witness script.

### Fund And Operate

Only after the synthetic test succeeds, send funds once to the bond P2WSH
address. Wait for confirmation, then refresh the hot wallet's registry:

```bash
jm-wallet sync-bonds --mnemonic-file <hot-wallet.mnemonic>
jm-wallet list-bonds --mnemonic-file <hot-wallet.mnemonic>
jm-maker start --mnemonic-file <hot-wallet.mnemonic>
```

The maker loads the delegated certificate from the registry. The external bond
key is not needed for normal maker operation.

For the experimental credential market, a separate owner-signed authorization
can delegate a market signing key without placing the bond spending key on the
provider. See [Experimental Credential Market](credential-market.md).

## Renew A Certificate

Certificate expiry does not spend or unlock the bond. Renew before expiry to
retain interoperability with reference takers: repeat `prepare-certificate-message`,
sign the new message with the bond key, and run `import-certificate` with the new
absolute period.

The maker refuses to start with an expired certificate. A running maker shuts
down when its next chain-state rescan observes certificate expiry. After
importing the renewed certificate, restart the maker so it loads the new
certificate from the registry.

The protocol field is an absolute 2016-block period. `--validity-periods`
controls issuance policy by adding a duration to the current period. The field
is covered by the bond-key signature. Reference and JoinMarket NG takers accept
the proof at the exact boundary block and reject it after that block.

## Redeem An External-Key Bond

CLTV uses chain median-time-past. Wait until the chain considers the locktime
satisfied, not merely until a local wall clock passes the date.

Refresh the registry before moving it to an offline machine:

```bash
jm-wallet sync-bonds --mnemonic-file <hot-wallet.mnemonic>
```

Build the PSBT with a current fee rate:

```bash
jm-wallet spend-bond <bond-address> <destination> \
  --fee-rate <current-sat-vB> \
  --master-fingerprint <external-master-fingerprint> \
  --derivation-path "<external-key-path>" \
  --wallet-fingerprint <hot-wallet-fingerprint> \
  --output unsigned-bond.psbt
```

Before using a blind signer such as BitBox01, decode `unsigned-bond.psbt` on a
separate trusted system and verify the destination, amount, fee, and locktime.
The finalizer verifies the signature and script key, but it cannot determine
whether those transaction details match your intent.

HWI flow:

```bash
python scripts/sign_bond_psbt.py \
  --chain <main|test|signet|regtest> \
  --no-broadcast \
  --file unsigned-bond.psbt > signed-bond.psbt
python scripts/finalize_bond_psbt.py --file signed-bond.psbt
```

QR flow: sign `unsigned-bond.psbt` on the device, save the returned signed PSBT,
and run `finalize_bond_psbt.py`.

For a canonical JoinMarket wallet-derived bond, the offline wallet command can
review the complete transaction and add the bond-key partial signature:

```bash
jm-wallet sign-psbt \
  --input unsigned-bond.psbt \
  --output signed-bond.psbt \
  --mnemonic-file <external-wallet.mnemonic>
python scripts/finalize_bond_psbt.py --file signed-bond.psbt
```

For an arbitrary external key path that is not the JoinMarket fidelity bond
path, use the standalone mnemonic fallback with an explicit derivation path:

```bash
python scripts/sign_bond_mnemonic.py \
  --file unsigned-bond.psbt \
  --derivation-path "<external-key-path>"
```

The explicit derivation path is sufficient when the external PSBT does not
include BIP32 derivation metadata. Review and confirm the transaction details
shown by the signer before entering the mnemonic.

Inspect the finalized transaction before broadcasting it:

```bash
bitcoin-cli decoderawtransaction <signed-transaction-hex>
bitcoin-cli sendrawtransaction <signed-transaction-hex>
```

P2WSH bond UTXOs cannot participate directly in CoinJoins. Redeem first to a
regular wallet output, then CoinJoin those funds.

## Backups And Compromise

For wallet-derived bonds, back up the JoinMarket mnemonic and record the bond
locktime and derivation details.

For external-key bonds, recovery requires all of:

- the external signer seed or key,
- any BIP39 passphrase,
- the external derivation path and master fingerprint,
- the locktime and bond address,
- the hot wallet fingerprint and registry backup for operational recovery.

The registry stores the delegated certificate private key and bond metadata. It
does not replace the external seed. If a general-purpose seed is entered into
software for fallback signing, treat that seed as exposed. A dedicated bond
mnemonic limits the blast radius; an offline signer reduces exposure but does
not make an entered seed secret again.

## Reference Implementation Migration

Reference JoinMarket bonds use
`m/84'/0'/0'/2/<timenumber>`, where the final index encodes the monthly
locktime. To register an existing bond:

1. Obtain the account or `/2` branch xpub from `wallet-tool.py display`.
2. Derive and verify the public key:

   ```bash
   python scripts/derive_bond_pubkey.py \
     --xpub <account-or-branch-xpub> \
     --locktime <YYYY-MM> \
     [--branch-xpub]
   ```

3. Run `create-bond-address` with the printed key and verify that the address
   exactly matches the existing reference bond.
4. Generate a hot certificate key and prepare a certificate message as above.
5. Sign it with the reference wallet seed:

   ```bash
   python scripts/sign_bond_cert_reference.py \
     --locktime <YYYY-MM> \
     --cert-pubkey <certificate-pubkey> \
     --cert-expiry <absolute-period>
   ```

   Add `--passphrase` if that reference wallet uses a BIP39 passphrase.

6. Import the resulting base64 signature. Entering the reference mnemonic into
   software exposes it; use an offline machine and move remaining funds if the
   seed also protects other holdings.

## Testing device compatibility with the public test mnemonic

This deterministic vector checks whether a signer can handle the CLTV P2WSH
input. The mnemonic is public. Never send funds to it or use it outside testing.

```text
Mnemonic:            abandon abandon abandon abandon abandon abandon
                     abandon abandon abandon abandon abandon about
Master fingerprint:  73c5da0a
Signing key path:    m/84'/0'/0'/0/0
Public key:          0330d54fd0dd420a6e5f8d3624f5f3482cae350f79d5f0753bf5beef9c2d91af3c
Bond locktime:       2026-02-01 00:00 UTC (1769904000)
Bond address:        bc1qrd0yehles4ppg66tl823yylw654ksfftsy9y79d2uqk59jtlqjhqd7zpam
Destination:         bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu
```

The fixed date is intentionally retained for reproducibility; the synthetic
input does not exist. Save this unsigned PSBT as `unsigned-bond-test.psbt`:

```text
cHNidP8BAFICAAAAARERERERERERERERERERERERERERERERERERERERERERAAAAAAD+////ATCGAQAAAAAAFgAUwM681sPTyox13F7GLr5VMw75EOKAl35pAAEBK6CGAQAAAAAAIgAgG15M3/mFQhRrS/nVEhPu1StoJSuBCk8VquAtQsl/BK4BAwQBAAAAAQUqBICXfmmxdSEDMNVP0N1CCm5fjTYk9fNILK41D3nV8HU79b7vnC2RrzysIgYDMNVP0N1CCm5fjTYk9fNILK41D3nV8HU79b7vnC2RrzwYc8XaClQAAIAAAACAAAAAgAAAAAAAAAAAAAA=
```

Sign and verify it with the HWI or QR flow above. For HWI use `--chain main`.
The classic Jade has completed this exact HWI signing flow with HWI 3.2.0 and
`cbor2` 5.9.0. With 5.8.0 the device completed its confirmation screens, but
HWI lost the returned signed PSBT while decoding the chunked serial response.
You can regenerate the exact vector with:

```bash
jm-wallet create-bond-address 0330d54fd0dd420a6e5f8d3624f5f3482cae350f79d5f0753bf5beef9c2d91af3c \
  --locktime-date "2026-02" \
  --network mainnet \
  --wallet-fingerprint 73c5da0a
jm-wallet spend-bond bc1qrd0yehles4ppg66tl823yylw654ksfftsy9y79d2uqk59jtlqjhqd7zpam \
  bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu \
  --fee-rate 1.0 \
  --test-unfunded \
  --master-fingerprint 73c5da0a \
  --derivation-path "m/84'/0'/0'/0/0" \
  --wallet-fingerprint 73c5da0a \
  --output unsigned-bond-test.psbt
```
