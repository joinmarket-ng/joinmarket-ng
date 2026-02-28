# JoinMarket NG vs. Reference Implementation

This is a modern alternative implementation of the JoinMarket protocol, maintaining **full wire protocol compatibility** with the [reference implementation](https://github.com/JoinMarket-Org/joinmarket-clientserver/) while offering significant improvements.

### Key Advantages

**Architectural Improvements:**

- **Stateless, no daemon**: Simpler deployment and operation
- **Run multiple roles simultaneously**: Act as maker and taker at the same time without stopping/restarting - huge privacy win by avoiding suspicious orderbook gaps
- **Light client support**: Full Neutrino/BIP157 integration - no full node required
- **No wallet daemon**: Direct wallet access without RPC overhead or remote wallet complexity
- **Modern async stack**: Python 3.14+, Pydantic v2, AsyncIO with full type hints

**Quality & Maintainability:**

- **~100% unit test coverage**: Every component thoroughly tested in isolation
- **E2E compatibility tests**: Full CoinJoin flows tested against reference implementation
- **Type safety**: Strict type hints enforced with Mypy (static type checker) and Pydantic (runtime data validation)
- **Clean, auditable code**: Easy to understand, review, and contribute to
- **Modern tooling**: Ruff formatting, pre-commit hooks, comprehensive CI/CD

### Why a New Implementation

The reference implementation has served the community well, but faces challenges that make improvements difficult:
- Limited active development (maintenance mode)
- 181+ open issues and 41+ open pull requests
- Technical debt requiring full rewrites
- Tight coupling to Bitcoin Core's BerkeleyDB

Starting fresh let us build on modern foundations while honoring the protocol's proven design. This project currently lacks peer review (contributions welcome!), but the extensive test suite and clear documentation make auditing straightforward.

**We see this as our turn to take JoinMarket to the next level while honoring the foundation built by the original contributors.**

### Compatibility

This implementation uses protocol v5 and maintains **full wire protocol compatibility** with the reference implementation. New features like Neutrino support are negotiated via the handshake features dict, not protocol version bumps.

**Design principles:**

- **Smooth rollout**: Features are adopted gradually without requiring network-wide upgrades
- **No fragmentation**: All peers use protocol v5, avoiding version-based compatibility issues
- **Backwards compatible**: New peers work seamlessly with existing JoinMarket makers and takers

**Feature negotiation via handshake:**

- During the CoinJoin handshake, peers exchange a features dict (e.g., `{"neutrino_compat": true}`)
- Takers adapt their UTXO format based on maker capabilities
- Legacy peers that don't advertise features receive legacy format

**Compatibility matrix:**

| Taker Backend | Maker Features | Status |
|--------------|----------------|--------|
| Full node | No `neutrino_compat` (legacy) | Works - sends legacy UTXO format |
| Full node | Has `neutrino_compat` | Works - sends extended UTXO format |
| Neutrino | No `neutrino_compat` (legacy) | Incompatible - taker filters out |
| Neutrino | Has `neutrino_compat` | Works - both use extended format |

Neutrino takers automatically filter out makers that don't advertise `neutrino_compat` since they require extended UTXO metadata for verification.

### Roadmap

All components are fully implemented. Future work will focus on improvements, optimizations, and protocol extensions:

- Nostr relays for offer broadcasting
- [CoinJoinXT and Lightning Network integration](https://www.youtube.com/watch?v=YS0MksuMl9k)

---
