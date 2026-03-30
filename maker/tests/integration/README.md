# Maker Integration Tests

This directory contains Docker-marked integration tests for maker/jmwallet behavior with a
real Bitcoin Core regtest backend.

## Preferred Way To Run

From repository root, use the orchestrated suite:

```bash
./scripts/run_all_tests.sh
```

It already runs `maker/tests/integration/` in the Docker integration phase.

## Run Only These Tests

If you only want this subset:

```bash
# start local integration stack from this directory
docker compose up -d

# run test module from repository root
pytest -m docker maker/tests/integration/test_wallet_bitcoin_core.py --fail-on-skip

# cleanup
docker compose down -v
```

## Notes

- Tests are marked `docker` and excluded by default by root `pytest.ini`.
- They expect Bitcoin RPC on `http://127.0.0.1:18443` with `test/test`.
- The local compose file here starts `bitcoin`, `miner`, and `directory` containers.

## Troubleshooting

- Bitcoin unreachable: `docker compose ps` and `docker compose logs bitcoin`
- Dirty state between runs: `docker compose down -v` and start again
