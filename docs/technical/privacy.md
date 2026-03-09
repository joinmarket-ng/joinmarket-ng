### Mixdepths

HD path: `m/84'/0'/0'/mixdepth/chain/index` (P2WPKH Native SegWit)

**Design (Default: 5 isolated accounts):**

- Inputs for a CoinJoin come from a **single mixdepth**
- CoinJoin outputs go to the **next mixdepth** (wrapping 4 -> 0)
- Change outputs stay in the **same mixdepth**

This prevents merging CoinJoin outputs with their change, blocking trivial linkage.

**Address Branches (per mixdepth):**

- External (0): Receiving addresses
- Internal (1): Change addresses

Example:
```
mixdepth 0/external: m/84'/0'/0'/0/0/0 -> bc1q... (receive)
mixdepth 0/internal: m/84'/0'/0'/0/1/0 -> bc1q... (change)
mixdepth 1/external: m/84'/0'/0'/1/0/0 -> bc1q... (CJ output from mixdepth 0)
```

### PoDLE (Proof of Discrete Log Equivalence)

PoDLE prevents Sybil attacks by requiring takers to commit to UTXO ownership before makers reveal their UTXOs.

**The Problem:** Without PoDLE, an attacker could request CoinJoins from many makers, collect their UTXO sets, then abort - linking maker UTXOs without cost.

**Protocol Flow:**

1. **Taker commits**: $C = H(P_2)$ where $P_2 = k \cdot J$ ($J$ is NUMS point)
2. **Maker accepts**: Sends encryption pubkey
3. **Taker reveals**: Sends $P$ (pubkey), $P_2$, and Schnorr-like proof
4. **Maker verifies**: $H(P_2) = C$, proof valid, UTXO exists
5. **Maker blacklists**: Adds commitment to local blacklist immediately
6. **Maker broadcasts**: Opens ephemeral connections to all directories with a fresh random nick and isolated Tor circuit, broadcasts `!hp2` publicly, then closes

The broadcast step (6) uses a completely separate identity: a new random nick, a new Tor circuit (via unique SOCKS5 credentials for stream isolation), and short-lived connections. This prevents any party -- directory servers, other peers, or observers -- from linking the `!hp2` broadcast to the maker that consumed the commitment. The broadcast is best-effort and fire-and-forget.

When a maker receives a relay request (`!hp2` via privmsg from a reference implementation peer), it uses the same ephemeral identity approach to re-broadcast, preserving source obfuscation.

**Commitment Broadcast Timing:**

The `!hp2` broadcast is sent after `!ioauth` (step 3 of Phase 3), not before. Broadcasting early would risk blacklisting a commitment that other makers participating in the same transaction have not yet processed, causing them to reject the taker's `!auth`.

The proof shows that $P = k \cdot G$ and $P_2 = k \cdot J$ use the same private key $k$ without revealing $k$.

**NUMS Point Index System:**

Each UTXO can generate multiple different commitments using different NUMS points (indices 0-9):

- Index 0: First use (preferred)
- Index 1-2: Retry after failed CoinJoins (accepted by default)
- Index 3+: Only if maker configures higher `taker_utxo_retries`

**UTXO Selection for PoDLE:**

| Criterion | Default | Rationale |
|-----------|---------|-----------|
| Min confirmations | 5 | Prevents double-spend |
| Min value | 20% of cj_amount | Economic stake |

Selection priority: confirmations (desc) -> value (desc)

**Commitment Tracking:**

- **Taker** (`cmtdata/commitments.json`): Tracks locally used commitments
- **Maker** (`cmtdata/commitmentlist`): Network-wide blacklist via `!hp2`

### Fidelity Bonds

Fidelity bonds allow makers to prove locked bitcoins, improving trust and selection probability.

**Purpose:** Makers lock bitcoin in timelocked UTXOs to gain priority in taker selection. Bond value increases with amount and time until unlock.

**Bond Address Generation:**

Fidelity bonds use P2WSH addresses with a timelock script:

```
<locktime> OP_CHECKLOCKTIMEVERIFY OP_DROP <pubkey> OP_CHECKSIG
```

Generate a bond address:

```bash
jm-wallet generate-bond-address \
  --mnemonic-file wallet.enc \
  --password "your-password" \
  --locktime-date "2026-01-01" \
  --index 0
```

**Bond Registry (`fidelity_bonds.json`):**

Stores bond metadata including:
- Address, locktime, derivation path
- UTXO info (txid, vout, value, confirmations)
- Certificate fields for cold storage bonds

Commands:
- `jm-wallet registry-list` - List all bonds with status
- `jm-wallet registry-show <address>` - Show bond details
- `jm-wallet registry-sync` - Update funding status from blockchain

**Bond Proof Structure (252 bytes):**

| Field | Size | Description |
|-------|------|-------------|
| nick_sig | 72 | DER signature (padded with 0xff) |
| cert_sig | 72 | DER signature (padded with 0xff) |
| cert_pubkey | 33 | Certificate public key |
| cert_expiry | 2 | Expiry period (2016-block periods, little-endian) |
| utxo_pubkey | 33 | UTXO public key |
| txid | 32 | Transaction ID |
| vout | 4 | Output index (little-endian) |
| timelock | 4 | Locktime value (little-endian) |

**DER Signature Padding**: Signatures are padded at the start with `0xff` bytes to exactly 72 bytes. The DER header byte `0x30` makes stripping padding straightforward during verification.

**Signature Purposes**:

- **Nick signature**: Proves maker controls certificate key (signs `taker_nick|maker_nick`)
- **Certificate signature**: Binds cert key to UTXO (signs `fidelity-bond-cert|cert_pub|expiry`)

**Certificate Expiry**:

- Encoding: 2-byte unsigned integer (little-endian)
- Represents: Difficulty retarget period number (period = block_height / 2016)
- Calculation: `cert_expiry = ((current_block + 2) // 2016) + 1`
- Validation: Invalid if `current_block_height > cert_expiry * 2016`

**Certificate Chain:**

```
UTXO keypair (optionally cold) -> signs -> certificate (hot) -> signs -> nick proofs
```

Allows cold storage of bond private key while hot wallet handles per-session proofs.

**Cold Wallet Setup:**

> **IMPORTANT -- HARDWARE WALLET LIMITATIONS:**
>
> Most hardware wallets **cannot sign** fidelity bond spending transactions. Bond UTXOs are P2WSH outputs with CLTV timelock witness scripts, and most firmware rejects custom witness scripts. **Only Ledger Nano S/X and Blockstream Jade** support this (see [HWI support matrix](https://hwi.readthedocs.io/en/latest/devices/index.html#support-matrix)). Trezor (all models), Coldcard, BitBox02, and KeepKey **cannot** sign bond redemptions ([Trezor firmware issue #416](https://github.com/trezor/trezor-firmware/issues/416), open since 2019).
>
> If your hardware wallet cannot sign CLTV scripts, you will need to enter your BIP39 mnemonic into the `sign_bond_mnemonic.py` script to spend the bond. This does not mean funds are lost -- it is an inconvenience that degrades security from "hardware wallet cold storage" to "software signing on a (potentially offline) computer". **Plan ahead**: use a CLTV compatible hardware wallet for full cold storage, or create a dedicated mnemonic/passphrase specifically for the bond so that mnemonic exposure does not risk your main wallet.
>
> **Before locking real funds**, complete the full workflow end-to-end including a test spend (without broadcasting) to confirm your tooling works. See "Test the full flow" below.

For maximum security, keep the bond UTXO private key on a hardware wallet. The bond private key never touches any internet-connected device.

1. **Get public key from Sparrow Wallet**:
   - Open Sparrow Wallet with your hardware wallet connected
   - Go to the **Addresses** tab
   - Choose any address from the Deposit (`m/84'/0'/0'/0/x`) or Change (`m/84'/0'/0'/1/x`) account -- use index 0 for simplicity
   - Right-click the address and select **"Copy Outpur Descriptor Key"**. **Important**: Sparrow may wrap the key as `wpkh(03abcd...)` -- if so, remove the `wpkh(` prefix and trailing `)` to get just the raw hex. The CLI will also strip this automatically.
   - **Note the derivation path**: double-click the address (or click the receive arrow icon) to see the full derivation path (e.g., `m/84'/0'/0'/0/0`). You will need this later when spending the bond.
   - **Note the master fingerprint**: go to **Settings** (bottom-left) -> **Keystores** section. The master fingerprint (4 bytes hex, e.g., `aabbccdd`) is shown there. You will need this later when spending the bond.
   - **Note**: The `/2` fidelity bond derivation path is not available in Sparrow. Using `/0` or `/1` addresses works fine -- the bond address is derived from the public key itself, not the derivation path.

2. **Create bond address** (on online machine -- NO private keys needed):
   ```bash
   jm-wallet create-bond-address "<pubkey_from_step_1>" \
     --locktime-date "2026-01"
   ```
   This saves the bond to the registry automatically. The output shows both the bond P2WSH address (for funding) and the P2WPKH "Signing Address" (used later in Sparrow for message signing). Fund the bond P2WSH address with Bitcoin.

3. **Generate hot wallet keypair** (on online machine):
   ```bash
   jm-wallet generate-hot-keypair --bond-address <bond_address>
   ```
   This creates a random keypair and saves it to the bond registry automatically. The keypair will be loaded automatically in subsequent steps.

4. **Prepare certificate message** (on online machine):
   ```bash
   jm-wallet prepare-certificate-message <bond_address> \
     --validity-periods 52  # ~2 years
   ```
   This fetches the current block height and outputs the message to sign. **Important**: Note the `Cert Expiry: period XXX` value shown -- you will need this exact number in step 6.

   Example output:
   ```
   Current Block:         933047 (period 462)
   Cert Expiry:           period 514 (block 1036224)   <-- USE THIS NUMBER!
   Validity:              ~102 weeks (103177 blocks)

   MESSAGE TO SIGN (copy this EXACTLY into Sparrow):
   fidelity-bond-cert|03250c574fe8a2ea...|514
   ```

5. **Sign the message in Sparrow**:
   - Open Sparrow Wallet and connect your hardware wallet
   - Go to **Tools -> Sign/Verify Message**
   - In the **Address** field, enter or select the **Signing Address** shown in step 2 (the P2WPKH `bc1q...` address, NOT the bond P2WSH address)
   - Copy the **entire message** from step 4 (e.g., `fidelity-bond-cert|02abc...|514`) and paste it into the 'Message' field
   - **Important**: Select **'Standard (Electrum)'** format, NOT BIP322
   - Click 'Sign Message' -- your hardware wallet will prompt for confirmation
   - Copy the resulting base64 signature

   **Note on hardware wallets**: Different hardware wallets encode the signature header byte differently. Trezor uses the extended Electrum format that encodes the address type (P2WPKH) in the header byte. This is handled automatically by the import command.

6. **Import certificate** (on online machine):
   ```bash
   jm-wallet import-certificate <bond_address> \
     --cert-signature '<base64_signature_from_sparrow>' \
     --cert-expiry 514   # <-- USE THE PERIOD NUMBER FROM STEP 4!
   ```
   **Critical**: The `--cert-expiry` value MUST match the period number shown in step 4. This is an ABSOLUTE period number, not a relative duration. Using the wrong value will cause the certificate to be rejected as expired.

   The certificate pubkey and private key are loaded from the registry automatically (from step 3).

7. **Test the full flow** -- generate a test spend PSBT (do NOT broadcast):
   ```bash
   jm-wallet spend-bond <bond_address> <any_address_you_control> \
     --fee-rate 1.0 \
     --test-unfunded \
     --master-fingerprint <fingerprint_from_step_1> \
     --derivation-path "<path_from_step_1>"
   ```
   Then verify you can sign it:
   ```bash
   # Ledger/Jade users -- test HWI signing:
   python scripts/sign_bond_psbt.py <psbt_base64>

   # All other devices -- test mnemonic signing:
   python scripts/sign_bond_mnemonic.py <psbt_base64>
   ```
   **Do not broadcast** the test transaction -- just confirm that signing succeeds and produces valid output. Use `bitcoin-cli decoderawtransaction <signed_hex>` to inspect. `--test-unfunded` uses a synthetic input so you can validate derivation path, signer compatibility, and the full signing toolchain **before funding**.

8. **Fund the bond** -- only after confirming the full flow works, send Bitcoin to the bond P2WSH address.

9. **Run maker**: The maker automatically detects certificates and uses them.
   ```bash
   jm-maker start
   ```

**Security benefits:**
- Bond UTXO private key NEVER leaves the hardware wallet (with CLTV compatible devices)
- No mnemonic exposure to online systems (when using HWI signing on an offline machine)
- Certificate expires after configurable period (~2 years default)
- If hot wallet is compromised, attacker can only impersonate bond until expiry
- Bond funds remain safe in cold storage

**Certificate expiry explained:**

The `cert_expiry` is an **absolute** period number that indicates when the certificate becomes invalid. The reference implementation validates: `current_block_height < cert_expiry * 2016`.

- **Validity periods**: The `--validity-periods` option (default 52 = ~2 years) specifies how long the certificate should be valid from now
- **Absolute period**: The command calculates `cert_expiry = current_period + validity_periods`
- **Protocol limits**: The cert_expiry field is an unsigned 16-bit integer (max 65535)
- **Practical range**: 1 to 52 periods (2 weeks to 2 years) validity is recommended

**Renewing an expired certificate:**

When your certificate expires, repeat steps 4-6 with a new message. The bond funds remain unaffected -- only the certificate needs re-signing.

**Spending Bonds:**

> **Tested:** Bond creation verified with Sparrow Wallet and a hardware wallet (HWI >= 3.1.0). Bond redemption verified with `sign_bond_mnemonic.py`. Trezor cannot sign the redemption transaction due to the CLTV firmware limitation.

After locktime expires, generate a PSBT for external signing:

```bash
jm-wallet spend-bond <bond_address> <destination_address> \
  --fee-rate 1.0 \
  --master-fingerprint <4_byte_hex> \
  --derivation-path "m/84'/0'/0'/0/0"
```

The `--derivation-path` should match the address whose public key was used in `create-bond-address` (in Sparrow: double-click the address or click the receive arrow to see the path). The `--master-fingerprint` is found in Sparrow under Settings -> Keystores. Both are embedded as BIP32 key origin info in the PSBT, which helps signing tools derive the correct key.

Use `--output psbt.txt` to save the PSBT to a file for transfer to a signing tool.

For pre-funding dry-run signer tests, add `--test-unfunded` (optionally `--test-utxo-value`) to generate a non-broadcastable PSBT with synthetic UTXO metadata.

**Hardware wallet compatibility:**

| Device | Can sign CLTV bonds? | Notes |
|--------|:--------------------:|-------|
| Ledger Nano S/X | Yes | Bitcoin App 2.1+ requires standard BIP44/49/84/86 derivation for the key |
| Blockstream Jade | Yes | Fully supported |
| BitBox01 | Yes | Discontinued; not recommended for new setups |
| Trezor (all models) | **No** | Firmware rejects non-multisig P2WSH; [issue #416](https://github.com/trezor/trezor-firmware/issues/416) open since 2019 |
| Coldcard | **No** | Firmware only supports single-key and multisig |
| BitBox02 | **No** | |
| KeepKey | **No** | |

**Option A -- HWI signing (Ledger and Jade only):**

Ledger and Blockstream Jade support arbitrary witnessScript inputs, so they can sign CLTV bonds directly via HWI:

```bash
pip install -U hwi  # >= 3.1.0 for newer device models
python scripts/sign_bond_psbt.py <psbt_base64>
```

Connect and unlock your device first. Close Sparrow or other wallet software that holds the USB connection. The script enumerates devices, signs, and outputs the transaction hex. If your wallet uses a BIP39 passphrase, add `--passphrase` to the script invocation.

**Option B -- Mnemonic signing (works with any device):**

For Trezor, Coldcard, BitBox02, KeepKey, or if HWI signing fails:

```bash
python scripts/sign_bond_mnemonic.py <psbt_base64>
```

The script prompts for your BIP39 mnemonic (hidden input), derives the key from the PSBT's BIP32 derivation info (or a `--derivation-path` argument), signs the CLTV witness script, and outputs the fully signed transaction hex.

If your wallet uses a **BIP39 passphrase**:
```bash
python scripts/sign_bond_mnemonic.py --passphrase <psbt_base64>
```

Broadcast the signed transaction:
```bash
bitcoin-cli sendrawtransaction <signed_hex>
```

**Reducing mnemonic exposure:**

Entering your BIP39 mnemonic into software exposes your entire wallet. The best approach is to **plan ahead** and avoid needing mnemonic signing entirely -- use a CLTV compatible HW wallet. If that is not an option, these strategies limit the blast radius:

- **Dedicated mnemonic**: Generate a fresh 12- or 24-word seed used exclusively for fidelity bonds. This mnemonic holds only bond funds, so exposing it during signing cannot compromise your main wallet. The downside is managing a separate seed backup.

- **[BIP-85](https://github.com/bitcoin/bips/blob/master/bip-0085.mediawiki) derived key** (Coldcard): Coldcard supports BIP-85 on-device, which can deterministically derive a child seed or WIF private key from your master seed. Go to `Advanced/Tools > Derive Seed B85 > WIF (private key)` and choose an index. The derived key cannot be used to recover the master seed. Use the derived public key when creating the bond address, and import the WIF for signing. The key is deterministic and can always be regenerated from the same seed + index. This is the ideal approach when using a Coldcard -- the master mnemonic is never exposed.

- **Air-gapped signing**: Run `sign_bond_mnemonic.py` on an offline machine. A bootable [Tails](https://tails.net/) USB drive is a practical option -- it runs from RAM, routes all traffic through Tor by default, and leaves no trace after shutdown. Copy the PSBT to the Tails machine via a second USB drive, sign, copy the signed hex back. After entering the mnemonic on any machine (even Tails), consider that mnemonic compromised for high-value wallets. This is why a dedicated mnemonic is strongly preferred.

**Note:** Sparrow Wallet cannot sign CLTV timelock scripts (P2WSH with custom witness scripts). It is used for key management, message signing (certificates), and can broadcast finalized transactions.

**Note:** P2WSH fidelity bond UTXOs cannot be used in CoinJoins, just spend it first to a regular P2WPKH address you control, then use those funds in CoinJoins.

**Migrating from the reference implementation:**

If you have an existing fidelity bond in the [reference JoinMarket implementation](https://github.com/JoinMarket-Org/joinmarket-clientserver/) (hot wallet), you can register it in joinmarket-ng by signing a certificate with the bond's private key. Both helper scripts below are self-contained and only require `coincurve` (`pip install coincurve`).

The reference implementation uses derivation path `m/84'/0'/0'/2/<timenumber>` for fidelity bond addresses, where `<timenumber>` is a monthly index (0 = Jan 2020, 1 = Feb 2020, ..., 959 = Dec 2099). Both the branch `/2` and the child `/<timenumber>` are **unhardened**, so the public key can be derived from the account xpub alone -- but signing requires the mnemonic.

**Note:** The reference implementation's `wallet-tool.py signmessage` command **cannot** sign messages with fidelity bond paths. This is a bug in the reference code: `BTC_Timelocked_P2WSH` does not override the inherited `sign_message()` method, causing a type error when the `(privkey_bytes, locktime)` tuple is passed where raw bytes are expected. The `sign_bond_cert_reference.py` script below works around this by deriving the private key directly from the mnemonic.

1. **Extract the fidelity bond xpub** from the reference wallet:
   ```bash
   python wallet-tool.py wallet.jmdat display
   ```
   Look for the `fbonds-mpk-xpub...` line under mixdepth 0. This is the account xpub at `m/84'/0'/0'`. Alternatively, use the xpub from the `m/84'/0'/0'/2` sub-header (the branch xpub).

   **Note:** `wallet-tool.py display` shows fidelity bond **addresses** but not individual public keys. You need the xpub to derive the pubkey.

2. **Derive the bond public key** using our helper script:
   ```bash
   # From the fbonds-mpk line (account xpub):
   python scripts/derive_bond_pubkey.py \
     --xpub <account_xpub> \
     --locktime 2026-02

   # Or from the /2 branch sub-header xpub:
   python scripts/derive_bond_pubkey.py \
     --xpub <branch_xpub> \
     --locktime 2026-02 \
     --branch-xpub

   # To check the timenumber for a locktime without an xpub:
   python scripts/derive_bond_pubkey.py --locktime 2026-02 --info
   ```
   The script outputs the 33-byte compressed public key hex and the exact `create-bond-address` command to run.

3. **Create the bond address in joinmarket-ng** using the derived pubkey:
   ```bash
   jm-wallet create-bond-address <pubkey_hex> --locktime-date YYYY-MM
   ```
   Use the exact command printed by `derive_bond_pubkey.py`. Verify the generated address matches the bond address shown in the reference wallet.

4. **Generate hot keypair and prepare certificate** (steps 3-4 from the cold wallet setup above).

5. **Sign the certificate** using the bond mnemonic:
   ```bash
   python scripts/sign_bond_cert_reference.py \
     --locktime 2026-02 \
     --cert-pubkey <cert_pubkey_hex> \
     --cert-expiry <period_number>
   ```
   The script prompts for your BIP39 mnemonic (hidden input), derives the private key at `m/84'/0'/0'/2/<timenumber>`, and signs the certificate message in Electrum recoverable format. If your wallet uses a BIP39 passphrase, add `--passphrase`. The base64 signature is printed to stdout.

   **Security:** Entering your mnemonic into software exposes it. Use a dedicated mnemonic/passphrase for the bond (see "Reducing mnemonic exposure" above), or run the script on an air-gapped machine. After entering the mnemonic on any internet-connected device, consider it compromised.

6. **Import the certificate** in joinmarket-ng:
   ```bash
   jm-wallet import-certificate <bond_address> \
     --cert-signature '<base64_signature>' \
     --cert-expiry <period_number>
   ```
   The `import-certificate` command automatically handles recoverable-to-DER signature conversion.

### Cryptographic Foundations

**Introductory Video**: For a visual introduction to elliptic curves and how they work in Bitcoin, watch [Curves which make Bitcoin possible](https://www.youtube.com/watch?v=qCafMW4OG7s) by MetaMaths.

**secp256k1 Elliptic Curve:**

JoinMarket uses the secp256k1 elliptic curve, the same curve used by Bitcoin. The curve is defined by:

$$y^2 = x^3 + 7 \pmod{p}$$

Where:
- Field prime: `p = 2^256 - 2^32 - 2^9 - 2^8 - 2^7 - 2^6 - 2^4 - 1`
- In hex: `p = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F`
- Group order: `n = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141`
- All arithmetic modulo `n` for scalars, modulo `p` for field elements

**Reference**: [SEC 2: Recommended Elliptic Curve Domain Parameters](https://www.secg.org/sec2-v2.pdf), Section 2.4.1

**Generator Point G:**

The generator point G is a specific point on secp256k1 with known coordinates. All Bitcoin and JoinMarket public keys are derived as scalar multiples of G.

Coordinates (from SEC 2 v2.0 Section 2.4.1):
```
Gx = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
Gy = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8
```

Compressed form (33 bytes): `0279BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798`

**NUMS Points:**

NUMS (Nothing Up My Sleeve) points are alternative generator points $J_0, J_1, \ldots, J_{255}$ that have no known discrete logarithm relationship to $G$. This property is crucial - if someone knew $k$ such that $J_i = k \cdot G$, they could forge PoDLE proofs.

The NUMS points are generated deterministically from G using a transparent algorithm that leaves no room for hidden backdoors. Anyone can verify the generation process.

**Generation Algorithm:**

```
for G in [G_compressed, G_uncompressed]:
    seed = G || i (as single byte)
    for counter in [0, 1, ..., 255]:
        seed_c = seed || counter (as single byte)
        x = SHA256(seed_c)
        point = 0x02 || x  (compressed point with even y)
        if point is valid on curve:
            return point
```

Python implementation:

```python
def generate_nums_point(index: int) -> Point:
    for G in [G_COMPRESSED, G_UNCOMPRESSED]:
        seed = G + bytes([index])
        for counter in range(256):
            seed_c = seed + bytes([counter])
            x = sha256(seed_c)
            claimed_point = b'\x02' + x
            if is_valid_curve_point(claimed_point):
                return claimed_point
```

**Reference**: [PoDLE Specification](https://gist.github.com/AdamISZ/9cbba5e9408d23813ca8) by Adam Gibson (waxwing)

Test vectors (from joinmarket-clientserver):

| Index | NUMS Point (hex) |
|------:|:-----------------|
| 0 | `0296f47ec8e6d6a9c3379c2ce983a6752bcfa88d46f2a6ffe0dd12c9ae76d01a1f` |
| 1 | `023f9976b86d3f1426638da600348d96dc1f1eb0bd5614cc50db9e9a067c0464a2` |
| 5 | `02bbc5c4393395a38446e2bd4d638b7bfd864afb5ffaf4bed4caf797df0e657434` |
| 9 | `021b739f21b981c2dcbaf9af4d89223a282939a92aee079e94a46c273759e5b42e` |
| 100 | `02aacc3145d04972d0527c4458629d328219feda92bef6ef6025878e3a252e105a` |
| 255 | `02a0a8694820c794852110e5939a2c03f8482f81ed57396042c6b34557f6eb430a` |

**Implementation**: `jmcore/src/jmcore/podle.py`

### PoDLE Mathematics

The PoDLE proves that two public keys $P = k \cdot G$ and $P_2 = k \cdot J$ share the same private key $k$:

1. **Commitment**: Taker computes $C = \textrm{SHA256}(P_2)$ and sends to maker

2. **Revelation**: After maker commits, taker reveals $(P, P_2, s, e)$ where:
   - $K_G = r \cdot G$, $K_J = r \cdot J$ (commitments using random nonce $r$)
   - $e = \textrm{SHA256}(K_G \| K_J \| P \| P_2)$ (challenge hash)
   - $s = r + e \cdot k \pmod{n}$ (Schnorr-like response)

3. **Verification**: Maker checks:
   - $\textrm{SHA256}(P_2) \stackrel{?}{=} C$ (commitment opens correctly)
   - $e \stackrel{?}{=} \textrm{SHA256}((s \cdot G - e \cdot P) \| (s \cdot J - e \cdot P_2) \| P \| P_2)$

This ensures the taker controls a real UTXO without revealing which one until makers have committed, preventing costless Sybil attacks on the orderbook.

---
