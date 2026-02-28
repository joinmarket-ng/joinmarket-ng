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
2. Maker verifies PoDLE, broadcasts `!hp2` to blacklist commitment
3. Maker sends `!ioauth`: their UTXOs + CoinJoin/change destinations

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

---
