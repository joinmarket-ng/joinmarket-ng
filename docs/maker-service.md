# Run A Maker Unattended

Use a service only after [interactive maker startup](README-maker.md) works.
This example is for a native Linux installation with systemd. Raspiblitz manages
its own service through the [terminal menu](README-tui.md); container operators
should use the [maker Compose configuration](https://github.com/joinmarket-ng/joinmarket-ng/blob/main/maker/docker-compose.yml).

## Decide How To Unlock The Wallet

A service cannot answer an interactive password prompt. For unattended startup,
it needs the wallet-file password and, if used, the BIP39 passphrase. The
`[wallet]` settings `mnemonic_password` and `bip39_passphrase` supply them, but
store them in plain text. Restrict the config to its owner with `chmod 600` and
protect the host and its backups. An attacker able to read the encrypted wallet
and these credentials can spend its funds.

`MNEMONIC_PASSWORD` in a protected systemd `EnvironmentFile` is an alternative
to the config entry, not a way to avoid storing a secret. For interactive-only
unlocking, keep using `jm-maker start` from a terminal; do not enable boot startup.

## Create The Service

Create `/etc/systemd/system/jm-maker.service`, replacing `youruser`, paths, and
the backend/Tor unit names with those on your host:

```ini
[Unit]
Description=JoinMarket NG Maker
After=network-online.target bitcoind.service tor.service
Wants=network-online.target bitcoind.service tor.service
StartLimitIntervalSec=0

[Service]
Type=simple
User=youruser
ExecStart=/home/youruser/.joinmarket-ng/venv/bin/jm-maker start --mnemonic-file /home/youruser/.joinmarket-ng/wallets/default.mnemonic
Restart=on-failure
RestartSec=30
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

The service user needs access to its wallet, config, and Tor control cookie.
Dependencies start services in order; they do not prove the backend has synced.
Check the journal after startup and resolve repeated errors.

For a separate configuration location, add `--config-file /etc/joinmarket/config.toml`
and `--data-dir /var/lib/joinmarket` to `ExecStart`, with a matching wallet path
and ownership. Do not copy another wallet's data directory over this one.

## Start And Check

After arranging noninteractive credentials:

```bash
sudo systemctl daemon-reload
sudo systemctl start jm-maker
journalctl -u jm-maker -f
```

Confirm that offers are published. Only then enable boot startup:

```bash
sudo systemctl enable jm-maker
```

Use `sudo systemctl stop jm-maker` before maintaining or moving the wallet.
Do not start an interactive maker while the service is using it.
