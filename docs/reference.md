# Reference

Use reference material for an exact option, specialized operation, or protocol
question. For everyday tasks, start with the [wallet guide](README-jmwallet.md).

## Commands And Settings

Run a command with `--help` for the options supported by your installed version,
for example `jm-wallet send --help` or `jm-maker start --help`.
The component READMEs contain generated help for the current source checkout:

- [Wallet commands](https://github.com/joinmarket-ng/joinmarket-ng/blob/main/jmwallet/README.md)
- [Taker commands](https://github.com/joinmarket-ng/joinmarket-ng/blob/main/taker/README.md)
- [Maker commands](https://github.com/joinmarket-ng/joinmarket-ng/blob/main/maker/README.md)
- [Tumbler commands](https://github.com/joinmarket-ng/joinmarket-ng/blob/main/tumbler/README.md)

For settings, use the `config.toml.template` alongside your installed config.
[Configuration](technical/configuration.md) explains file locations, precedence,
and updates. Do not copy the whole template into an existing customized config.

## Specialized Operations

- [Fidelity bonds](fidelity-bond-operations.md): lock, certify, recover, and redeem.
- [Unattended makers](maker-service.md): service setup and credential tradeoffs.
- [Advanced installation](install-advanced.md): manual verification, source, Windows, and removal.
- [Wallet daemon and JAM API](README-jmwalletd.md).
- [Experimental JAM NG Flatpak](flatpak.md): bundled integration for testing.
- [Orderbook watcher](README-orderbook-watcher.md) and [directory server](README-directory-server.md).
- [Release signatures](README-signatures.md).

## Contributor Reference

[Technical documentation](technical/index.md) covers protocol, wallet scanning,
security assumptions, and architecture. [Python API reference](api/index.md) is
generated from source; it describes internals, not the user command-line interface.
