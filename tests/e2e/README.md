# End-to-End Tests

Docker-backed end-to-end tests live in `tests/e2e/` and are controlled by pytest markers.

## Recommended Path

Use the orchestrated runner from repository root:

```bash
./scripts/run_all_tests.sh
```

This script handles startup order, waits, profile transitions, and `--fail-on-skip`.

## Manual Profile Runs

If you want to run only one profile locally, use the root `docker-compose.yml`.

### E2E profile (our implementation)

```bash
docker compose --profile e2e down -v
docker compose --profile e2e up -d --build
pytest -m e2e --fail-on-skip
docker compose --profile e2e down -v
```

### Reference profile (JAM compatibility)

```bash
docker compose --profile reference down -v
docker compose --profile reference up -d --build
pytest -m reference --fail-on-skip
docker compose --profile reference down -v
```

### Neutrino profile

```bash
docker compose --profile neutrino down -v
docker compose --profile neutrino up -d --build
pytest -m neutrino --fail-on-skip
docker compose --profile neutrino down -v
```

### Reference-maker profile (our taker vs JAM makers)

```bash
docker compose --profile reference-maker down -v
docker compose --profile reference-maker up -d --build
pytest -m reference_maker --fail-on-skip
docker compose --profile reference-maker down -v
```

## Marker Reference

Main markers in `pytest.ini`:

- `docker`: any Docker-dependent test (excluded by default)
- `e2e`: our maker/taker stack
- `reference`: JAM reference compatibility
- `neutrino`: BIP157/BIP158 backend tests
- `reference_maker`: JAM makers with our taker
- `slow`: long-running tests

Examples:

```bash
# default behavior: non-Docker tests only
pytest

# one profile
pytest -m e2e --fail-on-skip

# profile without slow tests
pytest -m "neutrino and not slow" --fail-on-skip
```

## Reference-Test Prerequisite

Some `reference` tests import the upstream reference implementation. Clone it at repo root:

```bash
git clone --depth 1 https://github.com/JoinMarket-Org/joinmarket-clientserver.git
```

`scripts/run_all_tests.sh` will also set this up automatically.

## Practical Troubleshooting

- Always start clean when debugging flaky state: `docker compose --profile <profile> down -v`
- Check status/logs: `docker compose --profile <profile> ps` and `docker compose logs <service>`
- If Neutrino tests stall, verify sync: `curl -s http://localhost:8334/v1/status`
- If makers look stale after refunding, restart them: `docker compose restart maker1 maker2 maker3 maker-neutrino`

## Safety Note

These stacks are for regtest development only. Do not reuse test mnemonics or settings on mainnet.
