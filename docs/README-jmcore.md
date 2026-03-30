# jmcore

`jmcore` is the shared library used by all JoinMarket NG components.

It provides protocol primitives, networking, models, and common utilities.

## What It Contains

- Protocol message formats and helpers
- Shared Pydantic models
- Directory client and networking helpers
- Cryptographic helpers (PoDLE, signatures, encryption utilities)
- Configuration loading and path handling

## Who Uses It

- `jmwallet`
- `maker`
- `taker`
- `directory_server`
- `orderbook_watcher`

## Installation

```bash
python -m pip install -e ./jmcore
```

Development install:

```bash
python -m pip install -e "./jmcore[dev]"
```

## API Docs

Use the generated API reference for module-level details:

- [API / jmcore](api/jmcore/index.md)

## Related Docs

- [Architecture](technical/architecture.md)
- [Protocol](technical/protocol.md)
- [Configuration](technical/configuration.md)
