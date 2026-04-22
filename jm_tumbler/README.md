# jm-tumbler

High-level CoinJoin scheduler for joinmarket-ng. Plans a role-mixed tumble
across destinations and persists progress to a human-readable YAML file.

See [`docs/technical/tumbler-redesign.md`](../docs/technical/tumbler-redesign.md)
for the full design.

## Install (editable)

```
pip install -e jmcore jmwallet taker maker
pip install -e jm_tumbler[dev]
```

## Tests

```
pytest jm_tumbler/tests
```
