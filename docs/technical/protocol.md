## Protocol

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
| 798 | PING | Keep-alive ping |
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
| `!error` | No | Yes | Any | Error notification (NG makers use fixed safe messages) |
| `!push` | No | Yes | 5 | Request maker to broadcast transaction |
| `!tbond` | No | Yes | 1 | Fidelity bond proof (with offers) |
| `!auth` | **Yes** | No | 3 | Taker reveals PoDLE proof (encrypted) |
| `!ioauth` | **Yes** | No | 3 | Maker sends UTXOs + addresses (encrypted) |
| `!tx` | **Yes** | No | 4 | Taker sends unsigned transaction (encrypted) |
| `!sig` | **Yes** | No | 4 | Maker signs inputs (encrypted, one per input) |
| `!hp2` | No | Yes | 3 | PoDLE commitment blacklist broadcast |

**Note**: Rules enforced at message_channel layer. All encrypted messages are base64-encoded.

**Phase 0: Pre-flight Eligibility**

Before connecting to any directory server, the taker syncs the wallet and verifies that the chosen mixdepth holds UTXOs that can actually fund the CoinJoin: confirmed enough (`taker_utxo_age`), not frozen, not a fidelity bond, not locked by another in-flight round, large enough for the PoDLE commitment (`taker_utxo_amtpercent`), and sufficient in total for the requested amount. An ineligible wallet fails fast with a clear reason instead of after minutes of network work.

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

The taker verifies each declared maker UTXO on-chain, binds the maker's auth key to one of them, and rejects makers declaring more than `max_maker_utxos` inputs. The last check is a spending limit, not a validity check: the taker pays the mining fee for every input in the CoinJoin, so an uncapped input count would let a counterparty consolidate its UTXOs at the taker's expense. Rejected makers are dropped and replaced when enough candidates remain.

Additional per-maker checks at this stage (all cause the offending maker to be dropped, never the whole round):

- Every declared outpoint must exist in the UTXO set and be confirmed (matching the reference taker); spent, missing, or unconfirmed inputs would make the final transaction invalid or unconfirmable.
- No outpoint may appear twice: neither within one maker's list, nor across makers, nor equal to one of the taker's own preselected inputs (which makers learn from the `!auth` PoDLE revelation). Duplicate inputs are consensus-invalid.
- The maker's inputs must cover its CoinJoin output and leave change at or above the fixed JoinMarket coordination threshold of 27,300 sats. Otherwise the maker's change would be omitted and the maker would refuse to sign, killing the round after commitments were burned. This maker threshold is distinct from the reference-compatible 2,730-sat cutoff used when a taker decides whether to keep its own change output.

**Commitment Broadcast (`!hp2`) Timing and Privacy:**

The `!hp2` broadcast is sent *after* `!ioauth`, not before. This is intentional: broadcasting early would risk other makers in the same transaction seeing the commitment and blacklisting it before they have processed the same taker's `!auth`, causing spurious rejections.

If an auth pass has revealed a proof and replacements are selected, the taker
uses one fresh PoDLE commitment for that replacement wave, including any
partial mini-fill batches before the next auth pass. Already-authenticated
makers retain their established sessions and are not sent the replacement
proof. Later transaction and signature phases do not use the PoDLE commitment.
This avoids presenting a commitment that may already have propagated through
`!hp2`. A compatibility preflight that exits before sending a proof reuses the
current commitment for its replacements.

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

**Channel Selection:**
A taker pins a single channel per maker before sending `!fill` and reuses it for the whole session. The reference taker, however, routes each message opportunistically: it sends `!fill` via a directory while a direct connection is still being established, then sends `!auth`/`!tx` over the direct connection once it handshakes. Makers therefore track the channel for diagnostics but accept such switches rather than aborting the session. Replay across channels is prevented by signing every message with a fixed `hostid` (`onion-network`) shared by all onion transports, not by pinning the transport.

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
- Makers respond to `!orderbook` subject to per-peer and aggregate resource limits.
  JoinMarket NG gives directory responses a 200-request burst budget, refilling at
  20 requests per second, and direct responses an independent 20-request burst budget,
  refilling at two requests per second. Each admission covers all offers and the
  applicable directory sends. Within the normal per-nick cooldown, the first copy
  from each additional configured directory is suppressed without a spam violation.
  Requests dropped by either aggregate budget are not queued for retry.

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
| `fidelity_bond_weighted` (default) | Per-slot coin flip: each slot independently picks bonded (weighted) or uniformly from zero-fee offers based on `bondless_makers_allowance` |
| `cheapest` | Lowest fee first |
| `weighted` | Exponentially weighted by inverse fee |
| `random` | Uniform random selection |

**Fidelity bond selection details** (`fidelity_bond_weighted`):

The default algorithm uses a per-slot Bernoulli trial (matching the reference JoinMarket implementation):

1. **Pre-filter**: When `bondless_require_zero_fee` is enabled (default), bondless offers (no fidelity bond) that advertise a non-zero CoinJoin fee, absolute or relative, are removed. This prevents attackers from flooding the orderbook with fee-charging bondless offers.
2. **Per-slot selection**: For each of the `n` slots independently:
   - With probability `bondless_makers_allowance` (default 0.05): pick uniformly at random from the **zero-fee** offers (bonded and bondless compete equally).
   - Otherwise: pick from the bonded pool weighted by `fidelity_bond_value`.
3. **Fallback**: If the chosen pool is empty, the other pool is tried.

This design reserves about 5% of slots for uniform zero-fee selection while keeping fidelity bonds as the primary Sybil defense. Bonded makers may still charge fees and compete for the weighted slots. Set `bondless_require_zero_fee = false` to restore uniform selection from all offers for allowance slots and permit fee-charging bondless offers.

**Key Point**: Selection probability is proportional to the **maker identity (nick)**, not the number of offers. A maker with 5 offers has the same selection probability as a maker with 1 offer (assuming both pass filters).

**Maker Replacement on Non-Response:**

When makers fail to respond, the taker can automatically select replacements instead of aborting:

- Configuration: `max_maker_replacement_attempts` (default: 3, range: 0-10)
- Failed makers added to ignored list for the session
- New makers go through the full fill/auth flow
- Auth-stage replacements use one fresh PoDLE commitment after a revelation
  was sent; existing authenticated makers are not re-driven
- The requested counterparty count remains the replacement target through fill
  and auth. Partial replacement selections are used and the remaining deficit
  is retried while attempts remain.
- If attempts are exhausted or no candidates remain, the CoinJoin proceeds only
  when the latest phase has at least `minimum_makers` completed makers;
  otherwise it aborts. A majority commitment blacklist rotates the PoDLE before
  this floor fallback is considered.

Implementation: `taker/src/taker/taker.py` and `taker/src/taker/orderbook.py`

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
| `peerlist_features` | Supports extended peerlist format with feature flags in `F:` field |
| `ping` | Supports application-level PING/PONG heartbeat liveness checks |
| `neutrino_compat` | Can provide extended UTXO format with scriptPubKey and blockheight for own UTXOs |

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

Both full-node and neutrino joinmarket-ng makers advertise `neutrino_compat` because both can provide metadata for their own wallet UTXOs. Neutrino takers require this feature to verify maker UTXOs via compact block filters. Reference implementation makers do not advertise features, so neutrino takers avoid them at selection time when possible (preferring makers with confirmed `neutrino_compat` if enough exist to fill all slots), and otherwise drop and replace them during the auth phase when the missing feature is detected in the `!pubkey` handshake.

**Handshake Integration:**

```json
{
  "proto-ver": 5,
  "features": {"peerlist_features": true, "ping": true, "neutrino_compat": true}
}
```

The `features` dict is ignored by reference implementation but preserved for our peers.

### Heartbeat and Idle Eviction

Directory servers run an application-level heartbeat to detect stale Tor connections that remain open but unresponsive.

- Every peer message updates `last_seen`
- On each heartbeat sweep, peers idle past `heartbeat_idle_threshold` are probed
- Ping-capable peers receive `PING` (`type=798`) and must answer with `PONG` (`type=799`) within `heartbeat_pong_wait`
- Non-ping makers receive a unicast `!orderbook` probe as a compatibility fallback
- Peers idle past `heartbeat_hard_evict` are evicted unconditionally

Defaults match joinmarket-rs behavior for interoperability:

- Sweep interval: 60s
- Idle probe threshold: 600s (10 min)
- Hard eviction: 1500s (25 min)
- PONG wait: 30s

---
