### Threat Model

- **Attackers**: Malicious peers, network observers, malicious directory operators
- **Assets**: Peer privacy, network availability, user funds
- **Threats**: DDoS, privacy leaks, message tampering, eclipse attacks

### Directory Server Security

Directory servers are similar to Bitcoin DNS seeds - required for peer discovery, not message routing. However, they represent security-relevant infrastructure.

**Threats:**

| Threat | Mitigation |
|--------|------------|
| Eclipse Attack | Multi-directory fallback, peer diversity |
| Selective Censorship | Ephemeral nicks, multiple directories |
| Metadata Correlation | Tor connections, ephemeral nicks |
| DoS | Rate limiting, connection limits |

**Multi-Directory Strategy:** Connect to multiple independent directories, merge peer lists, prefer direct P2P connections.

### Message Security

**Rate Limiting (Directory Server):**

| Setting | Default | Description |
|---------|---------|-------------|
| `message_rate_limit` | 100/s | Sustained rate |
| `message_burst_limit` | 200 | Burst size |
| `max_message_size` | 2MB | Maximum message size |
| `max_line_length` | 64KB | Maximum JSON-line length |
| `max_json_nesting_depth` | 10 | Maximum nesting |

**Validation Flow:**
```
Raw Message -> Line Length Check -> JSON Parse -> Nesting Check -> Model
```

**Encryption:**

| Command | Encrypted | Notes |
|---------|-----------|-------|
| `!pubkey` | No | Initial key exchange |
| `!fill`, `!auth`, `!ioauth`, `!tx`, `!sig` | Yes (NaCl) | CoinJoin negotiation |
| `!push` | No | Transaction already public |

**Channel Consistency:** All messages in a CoinJoin session must use the same channel (direct or relay). Prevents session confusion attacks.

### Neutrino Security

Additional protections for light clients:

| Protection | Default | Description |
|------------|---------|-------------|
| `max_watched_addresses` | 10,000 | Prevents memory exhaustion |
| `max_rescan_depth` | 100,000 | Limits expensive rescans |
| Blockheight validation | SegWit activation | Rejects old heights |

**Privacy Note:** Third-party neutrino-api servers can observe query patterns. Run locally behind Tor.

### Maker DoS Defense

**Layer 1: Tor PoW Defense (Tor 0.4.9.2+)**

Clients solve computational puzzles before establishing circuits:

| Setting | Default |
|---------|---------|
| `pow_enabled` | true |
| `pow_queue_rate` | 25/s |
| `pow_queue_burst` | 200 |

Difficulty auto-adjusts based on load.

**Layer 2: Application Rate Limiting**

Per-connection limits on `!orderbook` requests:

| Setting | Default |
|---------|---------|
| `orderbook_interval` | 30s |
| `orderbook_ban_threshold` | 10 violations |
| `ban_duration` | 3600s |

### Transaction Verification

The `verify_unsigned_transaction()` function performs critical checks before signing:

1. **Input Inclusion**: All maker UTXOs present in inputs
2. **CoinJoin Output**: Exactly one output >= amount to maker's CJ address
3. **Change Output**: Exactly one output >= expected to maker's change address
4. **Positive Profit**: `cjfee - txfee > 0` (maker never pays to participate)
5. **No Duplicate Outputs**: CJ and change addresses appear exactly once
6. **Well-formed**: Parseable, valid structure

### Attack Mitigations

| Attack | Mitigation |
|--------|------------|
| DDoS | Tor PoW, rate limiting, connection limits |
| Sybil | Fidelity bonds, PoDLE |
| Replay | Session-bound state, ephemeral keys |
| MitM | End-to-end NaCl encryption |
| Rescan Abuse | Blockheight validation, depth limits |

### Critical Security Code

| Module | Purpose | Coverage |
|--------|---------|----------|
| `maker/tx_verification.py` | CoinJoin verification | 100% |
| `jmwallet/wallet/signing.py` | Transaction signing | 95% |
| `jmcore/podle.py` | Anti-sybil proof | 90%+ |
| `directory_server/rate_limiter.py` | DoS prevention | 100% |

---
