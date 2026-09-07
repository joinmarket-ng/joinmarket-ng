# Set Up JoinMarket NG

Configure a backend and Tor before creating or using a wallet. New installs
place a commented starter configuration at `~/.joinmarket-ng/config.toml`; the
full reference is `~/.joinmarket-ng/config.toml.template`. Uncomment only the
settings you need.

## Configure Backend

### Bitcoin Core (Recommended)

Use a Bitcoin Core node that you control, has completed initial block download,
and has wallet support enabled. Do not run it with `disablewallet=1`. Confirm
the node is caught up and can serve wallet RPCs before continuing:

```bash
bitcoin-cli getblockchaininfo
bitcoin-cli listwallets
```

`initialblockdownload` should be `false` and `listwallets` should return a JSON
array. For an imported wallet with old transactions, confirm your node retains
the blocks needed for recovery; a pruned node may not have them.

Choose exactly one authentication method. Cookie authentication avoids storing
an RPC password in the JoinMarket configuration:

```toml
[bitcoin]
backend_type = "descriptor_wallet"
rpc_url = "http://127.0.0.1:8332"
rpc_cookie_file = "~/.bitcoin/.cookie"
```

Alternatively, configure the RPC credentials you set in Bitcoin Core:

```toml
[bitcoin]
backend_type = "descriptor_wallet"
rpc_url = "http://127.0.0.1:8332"
rpc_user = "your_rpc_user"
rpc_password = "your_rpc_password"
```

Keep Bitcoin Core RPC on the local host or otherwise protect it as an
administrative interface. `rpc_cookie_file` is mutually exclusive with
`rpc_user` and `rpc_password`.

### Neutrino (Lightweight Alternative)

Neutrino is an alternative when you do not run Bitcoin Core. Start the
[Neutrino service](install.md#neutrino-service), which requires Docker and disk
space for its initial sync. It creates a TLS certificate and bearer token on
first start. Keep the API bound to the loopback interface, not the public network.

Wait for compact-filter prefetch to finish in the service logs before starting
a maker or taker. Copy the generated TLS certificate and token into the
JoinMarket data directory, then restrict their permissions:

```bash
mkdir -p ~/.joinmarket-ng/neutrino
chmod 700 ~/.joinmarket-ng/neutrino
docker cp neutrino:/data/neutrino/tls.cert ~/.joinmarket-ng/neutrino/tls.cert
docker cp neutrino:/data/neutrino/auth_token ~/.joinmarket-ng/neutrino/auth_token
chmod 600 ~/.joinmarket-ng/neutrino/tls.cert ~/.joinmarket-ng/neutrino/auth_token
```

Configure HTTPS, certificate pinning, and token authentication:

```toml
[bitcoin]
backend_type = "neutrino"
neutrino_url = "https://127.0.0.1:8334"
neutrino_tls_cert = "~/.joinmarket-ng/neutrino/tls.cert"
neutrino_auth_token_file = "~/.joinmarket-ng/neutrino/auth_token"
```

See [Neutrino TLS](technical/neutrino-tls.md) for credential rotation or a
non-Docker service.

## Tor

Takers and the orderbook watcher need a SOCKS listener at `127.0.0.1:9050`.
Makers also need a cookie-authenticated Tor control listener at
`127.0.0.1:9051` for ephemeral onion services. Do not bind either listener to
an untrusted network.

Unless `--skip-tor` was used, the installer offers to install Tor and add only
missing local listeners and cookie authentication. It leaves existing custom
Tor authentication or `%include` configurations unchanged. In those cases,
configure the listeners and cookie authentication yourself, restart Tor, and
set the cookie path when automatic detection cannot find it:

```toml
[tor]
cookie_path = "/run/tor/control.authcookie"
```

On Debian or Ubuntu, the account running a maker often needs permission to read
the Tor control cookie. Add it to the `debian-tor` group, then start a new login
session:

```bash
sudo usermod -aG debian-tor "$USER"
```

## Next

[Create or import a wallet](getting-started.md). For connection or sync
problems, see [troubleshooting](troubleshooting.md).
