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
5. **Maker broadcasts**: `!hp2` to blacklist commitment network-wide

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
UTXO keypair (cold) -> signs -> certificate (hot) -> signs -> nick proofs
```

Allows cold storage of bond private key while hot wallet handles per-session proofs.

**Cold Wallet Setup:**

For maximum security, keep the bond UTXO private key on a hardware wallet:

1. Get public key from hardware wallet (Sparrow)
2. Create bond address: `jm-wallet create-bond-address <pubkey> --locktime-date "2026-01"`
3. Fund the bond address
4. Generate hot keypair: `jm-wallet generate-hot-keypair --bond-address <addr>`
5. Prepare certificate message: `jm-wallet prepare-certificate-message <addr>`
6. Sign message in Sparrow (Standard/Electrum format, NOT BIP322)
7. Import certificate: `jm-wallet import-certificate <addr> --cert-signature '<sig>' --cert-expiry <period>`
8. Run maker - certificate used automatically

**Spending Bonds:**

After locktime expires:

```bash
jm-wallet send <destination> --mixdepth 0 --amount 0  # Sweep
```

The wallet automatically handles P2WSH witness construction and nLockTime.

**Note:** P2WSH fidelity bond UTXOs cannot be used in CoinJoins.

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
