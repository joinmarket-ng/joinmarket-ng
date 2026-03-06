### Transport Layer

All messages use JSON-line envelopes terminated with `\r\n`:

```json
{"type": <message_type>, "line": "<payload>"}
```

**Message Types:**

| Code | Name | Description |
|------|------|-------------|
| 685 | PRIVMSG | Private message between two peers |
| 687 | PUBMSG | Public broadcast to all peers |
| 789 | PEERLIST | Directory sends list of connected peers |
| 791 | GETPEERLIST | Request peer list from directory |
| 793 | HANDSHAKE | Peer handshake (sent by both sides) |
| 795 | DN_HANDSHAKE | Directory-only handshake response |
| 797 | PING | Keep-alive ping |
| 799 | PONG | Ping response |
| 801 | DISCONNECT | Graceful disconnect |

### JoinMarket Messages

Inside the `line` field of PRIVMSG/PUBMSG, JoinMarket messages follow this format:

```
!command [[field1] [field2] ...]
```

For private messages with routing:

```
{from_nick}!{to_nick}!{command} {arguments}
```

Fields are separated by **single whitespace** (multiple spaces not allowed).

### CoinJoin Flow

**Protocol Commands:**

| Command | Encrypted | Plaintext OK | Phase | Description |
|---------|-----------|--------------|-------|-------------|
| `!orderbook` | No | Yes | 1 | Request offers from makers |
| `!reloffer`, `!absoffer` | No | Yes | 1 | Maker offer responses (via PRIVMSG) |
| `!fill` | No | Yes | 2 | Taker fills offer with NaCl pubkey + PoDLE commitment |
| `!pubkey` | No | Yes | 2 | Maker responds with NaCl pubkey |
| `!error` | No | Yes | Any | Error notification |
| `!push` | No | Yes | 5 | Request maker to broadcast transaction |
| `!tbond` | No | Yes | 1 | Fidelity bond proof (with offers) |
| `!auth` | **Yes** | No | 3 | Taker reveals PoDLE proof (encrypted) |
| `!ioauth` | **Yes** | No | 3 | Maker sends UTXOs + addresses (encrypted) |
| `!tx` | **Yes** | No | 4 | Taker sends unsigned transaction (encrypted) |
| `!sig` | **Yes** | No | 4 | Maker signs inputs (encrypted, one per input) |
| `!hp2` | No | Yes | 3 | PoDLE commitment blacklist broadcast |

**Note**: Rules enforced at message_channel layer. All encrypted messages are base64-encoded.

**Phase 1: Orderbook Discovery**

1. Taker connects to directory servers
2. Sends `!orderbook` request (public broadcast)
3. Makers respond via PRIVMSG with `!reloffer` or `!absoffer`
4. Taker collects offers, filters stale/incompatible, selects makers

**Phase 2: Fill Request**

1. Taker sends `!fill` with: order ID, amount, NaCl pubkey, PoDLE commitment
2. Selected makers respond with `!pubkey` (their NaCl pubkey)
3. From here, all messages are NaCl encrypted

**Phase 3: Authentication**

1. Taker sends `!auth`: reveals PoDLE proof, UTXO info, CoinJoin destination
2. Maker verifies PoDLE proof and taker's UTXO
3. Maker sends `!ioauth`: their UTXOs + CoinJoin/change destinations
4. Maker broadcasts `!hp2` to blacklist commitment network-wide (via ephemeral identity)

**Commitment Broadcast (`!hp2`) Timing and Privacy:**

The `!hp2` broadcast is sent *after* `!ioauth`, not before. This is intentional: broadcasting early would risk other makers in the same transaction seeing the commitment and blacklisting it before they have processed the same taker's `!auth`, causing spurious rejections.

For source obfuscation, the maker does not broadcast `!hp2` on its own long-lived directory connection. Instead:

1. Maker opens new connections to all directory servers using a **fresh random nick** and **unique Tor circuit** (via SOCKS5 stream isolation with a random credential)
2. Broadcasts `!hp2 <commitment>` as **pubmsg** on each ephemeral connection
3. Closes all ephemeral connections

This prevents any party (directory servers, other peers, observers) from correlating the `!hp2` broadcast with the maker that participated in the CoinJoin. The broadcast is best-effort: connection failures are logged but do not affect the CoinJoin flow.

When a maker receives a relay request (`!hp2` via privmsg from another maker), it uses the same ephemeral identity approach to re-broadcast, rather than publishing on its own connection.

**Phase 4: Transaction Signing**

1. Taker builds unsigned transaction with all inputs/outputs
2. Sends `!tx` to each maker
3. Makers verify transaction (critical security checks), sign, return `!sig`
4. Taker assembles fully signed transaction

**Phase 5: Broadcast**

Broadcast policies (configurable):

| Policy | Behavior |
|--------|----------|
| `SELF` | Broadcast via own backend |
| `RANDOM_PEER` | Try makers sequentially, fall back to self |
| `MULTIPLE_PEERS` | Broadcast to N random makers (default 3), fall back to self |
| `NOT_SELF` | Try makers only, no fallback |

Default is `MULTIPLE_PEERS` for redundancy.

### Direct vs Relay Connections

JoinMarket supports two routing modes:

**Direct Peer Connections (Preferred):**

- Taker connects directly to maker's onion address
- Bypasses directory for private messages
- Better privacy (directory doesn't see message metadata)
- Lower latency after initial connection
- Default behavior (`prefer_direct_connections=True`)

**Directory Relay (Fallback):**

- Messages routed through directory servers
- Works when direct connection fails
- Higher latency, directory sees metadata
- Reliable fallback for restrictive networks

**Channel Consistency:**
Once a channel is established for a session, all subsequent messages must use the same channel. This prevents session confusion attacks.

### Handshake Protocol

There are two distinct handshake flows depending on whether the remote peer is a directory or a regular peer (maker):

**Client -> Directory:**

1. Client sends HANDSHAKE (793) with client format (`"directory": false`, `"proto-ver"`, `"location-string"`)
2. Directory responds with DN_HANDSHAKE (795) with server format (`"directory": true`, `"proto-ver-min"`, `"proto-ver-max"`, `"accepted"`)

**Taker -> Maker (Direct Connection):**

1. Taker connects to maker's onion service
2. Both sides send HANDSHAKE (793) to each other with client format -- this is a **symmetric** exchange
3. Both sides process the received handshake and mark the peer as handshaked
4. **Important:** Makers must NOT send DN_HANDSHAKE (795) -- only directories use that message type. The reference implementation taker rejects DN_HANDSHAKE from non-directory peers.

### Nick Format

Nicks are derived from ephemeral keypairs:

```
J + version + base58(sha256(pubkey)[:14])
```

Example: `J54JdT1AFotjmpmH` (16 chars, v5 peer)

This enables:
- Anti-spoofing via message signatures
- Nick recovery across message channels

**Anti-Replay Protection:** All private messages include `<pubkey> <signature>` fields. The signed plaintext includes `hostid` (directory onion or `onion-network` for direct) preventing replay across channels.

### Multi-part Messages

- Unencrypted messages may contain multiple commands (split on `!`)
- Used for `!reloffer` and `!absoffer` combined announcements
- **NOT allowed** for encrypted messages

### Reference Implementation Compatibility

**Orderbook Request Behavior:**

- Reference orderbook watcher requests offers once at startup
- Our implementation requests on startup + periodically
- Makers should respond to every `!orderbook` request

**Stale Offer Filtering:**

- Offers older than `max_offer_age` (default 1 hour) are filtered
- Maker disconnects are tracked for filtering

**Known Directory Servers:**

| Network | Type | Address |
|---------|------|---------|
| Mainnet | Reference | `jmarketxf5wc4aldf3slm5u6726zsky52bqnfv6qyxe5hnafgly6yuyd.onion:5222` |
| Mainnet | JM-NG | `nakamotourflxwjnjpnrk7yc2nhkf6r62ed4gdfxmmn5f4saw5q5qoyd.onion:5222` |

### Maker Selection Algorithm

After collecting offers, the taker selects makers through three phases:

**Phase 1 - Filtering**: Remove offers that don't meet criteria (amount range, fee limits, offer type, ignored makers).

**Phase 2 - Deduplication**: If a maker advertises multiple offers under the same nick, only the cheapest offer is kept. This ensures makers cannot game selection by flooding the orderbook.

**Phase 3 - Selection**: Choose `n` makers from the deduplicated list using one of these algorithms:

| Algorithm | Behavior |
|-----------|----------|
| `fidelity_bond_weighted` (default) | Mixed strategy: ~87.5% slots filled by bond-weighted selection, remaining slots randomly from all offers |
| `cheapest` | Lowest fee first |
| `weighted` | Exponentially weighted by inverse fee |
| `random` | Uniform random selection |

**Key Point**: Selection probability is proportional to the **maker identity (nick)**, not the number of offers. A maker with 5 offers has the same selection probability as a maker with 1 offer (assuming both pass filters).

**Maker Replacement on Non-Response:**

When makers fail to respond, the taker can automatically select replacements instead of aborting:

- Configuration: `max_maker_replacement_attempts` (default: 3, range: 0-10)
- Failed makers added to ignored list for the session
- New makers go through the full fill/auth flow
- If not enough replacements available, CoinJoin aborts

Implementation: `taker/src/taker/orderbook.py`

### Multi-Channel Message Deduplication

When connected to N directory servers, each message is received N times. The deduplication system prevents:

1. Processing the same protocol message multiple times (expensive operations like `!auth`, `!tx`)
2. Rate limiter counting duplicates as violations
3. Log spam from duplicate messages

**Message Fingerprinting**: Messages identified by `from_nick:command:first_arg`:
- `alice:fill:order123` - Fill request for order 123
- `bob:pubkey:abc123` - Pubkey response

**Time-Based Window**: Duplicates within a 30-second window are dropped.

**Implementation**:

- **Maker** (`maker/bot.py`): Uses `MessageDeduplicator` to filter incoming messages
- **Taker** (`taker/taker.py`): Uses `ResponseDeduplicator` in `wait_for_responses()`
- **Orderbook**: Uses `(counterparty, oid)` as key for offer deduplication

### Feature Flags System

This implementation uses feature flags instead of protocol version bumps to enable progressive capability adoption while maintaining backward compatibility.

**Why feature flags?**

1. Reference implementation only accepts `proto-ver=5` - version bumps would break interoperability
2. Features can be adopted independently without "all or nothing" upgrades
3. Peers advertise what they support; both sides negotiate per-session

**Available Features:**

| Feature | Description |
|---------|-------------|
| `extended_peerlist` | Supports extended peerlist format with feature flags in `F:` field |
| `neutrino_compat` | Supports extended UTXO format with scriptPubKey and blockheight |
| `taproot` | Supports Taproot (P2TR) address type negotiation in CoinJoin |

**Extended Peerlist Format:**

```
nick;location;F:feature1+feature2
```

The `+` separator (not `,`) avoids ambiguity since peerlist entries are comma-separated.

**Neutrino Compatibility:**

Extended UTXO format includes scriptPubKey + block height for verification:

| Format | Example |
|--------|---------|
| Legacy | `txid:vout` |
| Extended | `txid:vout:scriptpubkey:height` |

**Handshake Integration:**

```json
{
  "proto-ver": 5,
  "features": {"extended_peerlist": true, "neutrino_compat": true}
}
```

The `features` dict is ignored by reference implementation but preserved for our peers.

### Taproot Address-Type Negotiation

Taproot (P2TR / BIP86) support is negotiated per-CoinJoin session via an optional `address_type` field in the `!fill` message. This design ensures backward compatibility -- legacy peers that don't understand the field simply ignore it.

**Negotiation Flow:**

1. Taker sends `!fill` with optional `address_type=p2tr` field
2. Maker checks if it supports the requested type
3. If supported: maker generates P2TR output addresses (bech32m)
4. If not supported or field absent: maker defaults to P2WPKH (bech32)

**Key Design Decisions:**

- The `address_type` field defaults to `p2wpkh` when absent, ensuring backward compatibility
- A P2TR-configured maker always falls back to `p2wpkh` when the taker doesn't request `p2tr`, so legacy takers work with P2TR makers
- All equal-amount CoinJoin outputs use the same address type for privacy (mixing P2TR and P2WPKH outputs would be a privacy leak)
- Change outputs follow the same address type as the CoinJoin outputs

**Interoperability Matrix:**

| Our Role | Peer | Address Type | Result |
|----------|------|-------------|--------|
| P2WPKH Taker | Reference Maker | p2wpkh | Works (standard flow) |
| P2TR Taker | Reference Maker | p2tr | Taker filters out incompatible makers |
| P2WPKH Taker | Our P2TR Maker | p2wpkh | Works (maker falls back to p2wpkh) |
| P2TR Taker | Our P2TR Maker | p2tr | Works (full taproot CoinJoin) |
| Reference Taker | Our P2WPKH Maker | p2wpkh | Works (standard flow) |
| Reference Taker | Our P2TR Maker | p2wpkh | Works (maker falls back to p2wpkh) |

**Signing Differences:**

P2TR inputs use BIP341 key-path spending with Schnorr signatures:

- Sighash: BIP341 (single SHA-256 + tagged hash, commits to ALL prevout values/scripts)
- Signature: 64-byte Schnorr (SIGHASH_DEFAULT) or 65-byte (with explicit sighash byte)
- Witness: Single element `[signature]` (no pubkey, unlike P2WPKH's `[signature, pubkey]`)

P2WPKH inputs continue to use BIP143 sighash with DER-encoded ECDSA signatures.

---
