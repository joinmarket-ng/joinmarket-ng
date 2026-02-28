# jmcore - JoinMarket Core Library

Shared library providing core functionality for JoinMarket components.

## Features

- **Protocol**: Message types, serialization, and protocol constants
- **Crypto**: Encryption, signing, and key management
- **Network**: Connection management and Tor integration
- **Models**: Pydantic models for type-safe data structures

## Installation

```bash
pip install -e .

# Development
pip install -e ".[dev]"
```

## Usage

```python
from jmcore.protocol import MessageType, ProtocolMessage
from jmcore.models import PeerInfo, MessageEnvelope

# Create a message
msg = ProtocolMessage(
    type=MessageType.HANDSHAKE,
    payload={"app": "joinmarket", "version": "2.1.0"}
)

# Serialize
data = msg.to_json()
```

## Development

```bash
# Run tests
pytest

# Lint
ruff check src tests

# Format
ruff format src tests

# Type check
mypy src
```

## Architecture

### Protocol Layer (`jmcore.protocol`)
- Message types and protocol constants
- Serialization/deserialization
- Protocol version management

### Crypto Layer (`jmcore.crypto`)
- NaCl-based encryption (libsodium)
- Bitcoin key management
- Signature verification

### Network Layer (`jmcore.network`)
- TCP connection abstractions
- Async I/O primitives
- Note: Tor anonymization is provided by separate Tor containers, not directly by jmcore

### Models (`jmcore.models`)
- Pydantic models for all data structures
- Validation and serialization
- Type safety
