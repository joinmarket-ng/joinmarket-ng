# JoinMarket Directory Server

Relay server for peer discovery and message routing in the JoinMarket network.

## Features

- **Peer Discovery**: Register and discover active peers
- **Message Routing**: Forward public broadcasts and private messages
- **Connection Management**: Handle peer connections and disconnections
- **Handshake Protocol**: Verify peer compatibility and network
- **High Performance**: Async I/O with optimized message handling
- **Observability**: Structured logging with loguru
- **Tor Hidden Service**: Run behind Tor for privacy (via separate container)

## Documentation

For full documentation, see [directory_server Documentation](https://joinmarket-ng.github.io/joinmarket-ng/README-directory-server/).

<!-- AUTO-GENERATED HELP START: jm-directory-ctl -->

<details>
<summary><code>jm-directory-ctl --help</code></summary>

```
usage: jm-directory-ctl [-h] [--host HOST] [--port PORT]
                        [--log-level LOG_LEVEL]
                        {status,health} ...

JoinMarket Directory Server CLI

positional arguments:
  {status,health}       Available commands
    status              Get server status
    health              Check server health

options:
  -h, --help            show this help message and exit
  --host HOST           Health check server host (default: 127.0.0.1)
  --port PORT           Health check server port (default: 8080)
  --log-level, -l LOG_LEVEL
                        Log level (default: INFO)
```

</details>

<details>
<summary><code>jm-directory-ctl status --help</code></summary>

```
usage: jm-directory-ctl status [-h] [--json]

options:
  -h, --help  show this help message and exit
  --json      Output as JSON
```

</details>

<details>
<summary><code>jm-directory-ctl health --help</code></summary>

```
usage: jm-directory-ctl health [-h] [--json]

options:
  -h, --help  show this help message and exit
  --json      Output as JSON
```

</details>


<!-- AUTO-GENERATED HELP END: jm-directory-ctl -->
