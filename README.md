<p align="center">
  <img src="media/logo.svg" alt="JoinMarket NG Logo" width="200"/>
</p>

# JoinMarket NG

JoinMarket NG is a modern implementation of the JoinMarket CoinJoin protocol for Bitcoin privacy.

It is wire-compatible with the reference JoinMarket network and supports both liquidity taking (`jm-taker`) and liquidity making (`jm-maker`).

## Start Here

- Documentation home: https://joinmarket-ng.github.io/joinmarket-ng/
- Installation guide: https://joinmarket-ng.github.io/joinmarket-ng/install/
- Technical docs: https://joinmarket-ng.github.io/joinmarket-ng/technical/

## Quick Start

1. Install (Linux/macOS):

```bash
curl -sSL https://raw.githubusercontent.com/joinmarket-ng/joinmarket-ng/main/install.sh | bash
source ~/.joinmarket-ng/activate.sh
```

2. Edit `~/.joinmarket-ng/config.toml` and set your backend (`descriptor_wallet` for Bitcoin Core, or `neutrino`):

```toml
[bitcoin]
backend_type = "descriptor_wallet"
rpc_url = "http://127.0.0.1:8332"
rpc_user = "your_rpc_user"
rpc_password = "your_rpc_password"
```

3. Create wallet and get deposit addresses:

```bash
jm-wallet generate
jm-wallet info
```

4. Run CoinJoin as a taker, or start earning fees as a maker:

```bash
jm-taker coinjoin --amount 1000000 --destination INTERNAL
# or
jm-maker start
```

## Module Docs

- `jmcore`: https://joinmarket-ng.github.io/joinmarket-ng/README-jmcore/
- `jmwallet`: https://joinmarket-ng.github.io/joinmarket-ng/README-jmwallet/
- `taker`: https://joinmarket-ng.github.io/joinmarket-ng/README-taker/
- `maker`: https://joinmarket-ng.github.io/joinmarket-ng/README-maker/
- `orderbook_watcher`: https://joinmarket-ng.github.io/joinmarket-ng/README-orderbook-watcher/
- `directory_server`: https://joinmarket-ng.github.io/joinmarket-ng/README-directory-server/
- `signatures`: https://joinmarket-ng.github.io/joinmarket-ng/README-signatures/
- `scripts`: https://joinmarket-ng.github.io/joinmarket-ng/README-scripts/

## Community

- Telegram: https://t.me/joinmarketorg
- SimpleX: https://smp12.simplex.im/g#bx_0bFdk7OnttE0jlytSd73jGjCcHy2qCrhmEzgWXTk

## License

MIT: https://joinmarket-ng.github.io/joinmarket-ng/license/
