### What is CoinJoin

CoinJoin transactions combine multiple users' funds into a single transaction, making it difficult to trace coins. This enhances financial privacy.

The transaction includes several equal amount outputs from inputs belonging to different users. An outside observer cannot determine which input corresponds to which equal amount output, effectively obfuscating the transaction history.

Change outputs are also included, but they are of different amounts and can be easily identified as change and sometimes matched to inputs using heuristics. However, the equal amount outputs remain ambiguous.

One round of CoinJoin increases privacy, but generally multiple rounds are needed to achieve strong anonymity.

### Makers and Takers

JoinMarket connects users who want to mix their coins (takers) with those willing to provide liquidity for a fee (makers):

- **Makers**: Liquidity providers who offer their UTXOs for CoinJoin and earn fees. They run bots that automatically participate when selected.
- **Takers**: Users who initiate CoinJoins by selecting makers and coordinating the transaction. They pay fees for the privacy service.

### Why JoinMarket is Different

Unlike other CoinJoin implementations (Wasabi, Whirlpool), JoinMarket has **no central coordinator**:

- **Taker acts as coordinator**: Chooses peers, gains maximum privacy (doesn't share inputs/outputs with a centralized party)
- **Most censorship-resistant**: Directory servers are easily replaceable and don't route communications, only host the orderbook
- **Multiple fallbacks**: Works with Tor hidden services, can easily move to alternatives like Nostr relays
- **Peer-to-peer**: Direct encrypted communication between participants

### Key Design Principles

1. **Trustless**: No central coordinator; the taker constructs the transaction
2. **Privacy-preserving**: End-to-end encryption for sensitive data
3. **Sybil-resistant**: PoDLE commitments prevent costless DOS attacks
4. **Decentralized**: Multiple redundant directory servers for message routing

### Why Financial Privacy Matters

Just as you wouldn't want your employer to see your bank balance when paying you, or a friend to know your net worth when splitting a bill, Bitcoin users deserve financial privacy. JoinMarket helps individuals exercise their right to financial freedom without promoting illegal activities.

---
