<p align="center">
  <img src="media/logo.svg" alt="JoinMarket NG Logo" width="200"/>
</p>

# JoinMarket NG

JoinMarket NG (Next Generation) is a modern implementation of [JoinMarket](https://github.com/JoinMarket-Org/joinmarket-clientserver/) - decentralized Bitcoin privacy through CoinJoin.

## What It Is

- **CoinJoin**: Mix your coins with others to break transaction history
- **Decentralized**: No central coordinator - taker coordinates peer-to-peer
- **Earn or spend**: Makers earn fees providing liquidity, takers pay fees for privacy

## What It Isn't

- Not a custodial mixer (you control your keys)
- Not a centralized tumbler service
- Not bulletproof - multiple rounds recommended for stronger privacy

## Quick Start

**Install** (Linux/macOS):

```bash
curl -sSL https://raw.githubusercontent.com/joinmarket-ng/joinmarket-ng/main/install.sh | bash
```

**Configure** (`~/.joinmarket-ng/config.toml`):

```toml
[bitcoin]
backend_type = "descriptor_wallet"  # or "neutrino" for light client
rpc_url = "http://127.0.0.1:8332"
rpc_user = "your_user"
rpc_password = "your_password"
```

**Create wallet**:

```bash
jm-wallet generate
```

**Run your first CoinJoin** (as taker):

```bash
jm-wallet info                    # Get deposit address, fund it
jm-taker coinjoin --amount 1000000 --mixdepth 0 --destination INTERNAL
```

**Or earn fees** (as maker):

```bash
jm-maker start
```

## Why JoinMarket-NG?

JoinMarket NG is a modern alternative to the reference implementation, fully compatible but with key improvements:

**Cross-compatible**: Makers running JoinMarket-NG are automatically discovered by takers using the legacy implementation, and vice versa. The wire protocol is 100% compatible, so you can seamlessly join the existing JoinMarket network.

- **No daemon** - just run commands, no background services
- **Run maker + taker simultaneously** - no suspicious gaps in offers
- **Light client support** - Neutrino backend, no full node required
- **Modern codebase** - Python 3.14+, full type hints, ~100% test coverage

See [JoinMarket-NG](technical/overview.md) for detailed comparison.

## Community

- [Telegram - JoinMarket Community](https://t.me/joinmarketorg)
- [SimpleX - JoinMarket Community](https://smp12.simplex.im/g#bx_0bFdk7OnttE0jlytSd73jGjCcHy2qCrhmEzgWXTk)
