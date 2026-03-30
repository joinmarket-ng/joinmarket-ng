# Development

## Local Setup

From repository root:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip

for d in jmcore jmwallet maker taker directory_server orderbook_watcher jmwalletd; do
  python -m pip install -e "./${d}[dev]"
done
```

## Tests

Fast unit test run:

```bash
pytest jmcore directory_server orderbook_watcher maker taker jmwallet
```

Full orchestrated suite (unit + Docker-backed phases):

```bash
./scripts/run_all_tests.sh
```

When selecting Docker-marked tests manually, use `--fail-on-skip`.

## Lint / Format / Type Check

Preferred:

```bash
prek run --all-files
```

Fallback:

```bash
pre-commit run --all-files
```

## Documentation

Build docs locally from repository root:

```bash
python scripts/build_docs.py
```

What this does:

- installs docs dependencies from `requirements-docs.txt`
- installs editable project packages used by API doc generation
- runs `mkdocs build` and writes output to `site/`

If you want to run the steps manually:

```bash
python -m pip install -r requirements-docs.txt
python -m pip install -e jmcore -e jmwallet -e taker -e maker -e directory_server -e orderbook_watcher
python -m mkdocs build
```

For local preview:

```bash
python -m mkdocs serve
```

## Reference Compatibility Tests

Some e2e tests require a local clone of the reference implementation at repository root:

```bash
git clone --depth 1 https://github.com/JoinMarket-Org/joinmarket-clientserver.git
```

Then run marker-specific tests as needed (`-m reference`, `-m reference_maker`).

## Releases and Signatures

Reproducible release verification and signing workflows:

- verify: `scripts/verify-release.sh`
- sign: `scripts/sign-release.sh`

See [Signatures](../README-signatures.md) for repository signature layout.

## Verify a Release

```bash
./scripts/verify-release.sh <version>

# with local reproduction check
./scripts/verify-release.sh <version> --reproduce
```

## Sign a Release

```bash
./scripts/sign-release.sh <version> --key <gpg-key-id>
```
