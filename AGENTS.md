# JoinMarket NG

## Overview

Modern, secure implementation of JoinMarket components using Python 3.14+, Pydantic v2, and AsyncIO.

**JoinMarket is a decentralized CoinJoin protocol for Bitcoin privacy.**

CoinJoin transactions combine multiple users' funds into a single transaction, making it difficult to trace the the coins. This enhances financial privacy.

How it works is by crafting a transaction with several equal amount outputs from inputs belonging to different users. This way, an outside observer cannot determine which input corresponds to which equal amount output, effectively obfuscating the transaction history.
Change outputs are also included, but they are of different amounts and can be easily identified as change and sometimes matched to inputs using heuristics. However, the equal amount outputs remain ambiguous.

## Key Constraints

- **Python**: 3.14+ required. Strict type hinting (Mypy) mandated.
- **Database**: No BerkeleyDB. Use direct RPC or Mempool API.
- **Privacy**: Tor integration is core architecture.

## Commands

- **Test (unit)**: `pytest jmcore directory_server orderbook_watcher maker taker jmwallet` (excludes Docker tests by default)
- **Test (full suite)**: `./scripts/run_all_tests.sh` - Runs all phases with Docker orchestration
- **Test (specific marker)**: `pytest -m e2e --fail-on-skip` - Uses `--fail-on-skip` to catch missing setup
- **Lint/Format**: `pre-commit run --all-files` (Recommended).
  - Manual: `ruff check .` / `ruff format .` / `mypy .`
- **Docker**: `docker-compose up -d` (several profiles available).

## Test Markers

Tests use pytest markers to organize by Docker profile:
- Default: `-m "not docker"` excludes all Docker tests
- `e2e`: Our maker/taker implementation
- `reference`: JAM compatibility tests
- `neutrino`: Light client tests
- `reference_maker`: JAM makers + our taker
- `docker`: Base marker for any Docker test

**Important:** CI and `run_all_tests.sh` use `--fail-on-skip` to fail instead of skip when setup is missing.

## Code Style
- **Formatting**: Line length 100. Follow Ruff defaults.
- **Typing**: `disallow_untyped_defs = true`. Use `typing` module or modern `|` syntax.
- **Imports**: Sorted (Stdlib → Third-party → Local). `from __future__ import annotations`.
- **Naming**: `snake_case` for functions/vars, `PascalCase` for classes/models.
- **Error Handling**: Use descriptive custom exceptions (inheriting from `Exception`).

## General Guidelines

- Check the documentation at README.md and docs/.
- Add tests and verify the new and existing tests pass, you can use the docker compose setup.
- Improve the documentation as needed.
- Don't break backwards compatibility even with the reference implementation. Use feature flags if needed.
- Don't use real mainnet transactions or addresses in tests or examples for privacy reasons.
- Use external reputable libraries when appropriate, avoid reinventing the wheel.
- If you add or change settings, update config.toml.template
- Do not manually edit CHANGELOG.md during normal development.

## Commit and Changelog Policy

- Follow Conventional Commits for all commit titles.
- Use component scopes that match the code you changed (for example, `fix(jmwallet): ...`, `fix(jmwalletd): ...`, `fix(taker): ...`). Do not use sub-scopes like `jmwallet-history`.
- Pick the commit type by intent:
  - `test:` for tests, fixtures, or changes that make tests deterministic.
  - `build:` for dependency/packaging/update-script/manifest changes.
  - `fix:` only for runtime bug fixes in product behavior.
- For `feat:` and `fix:` commits, include at least one `Changelog:` trailer in the commit body/footer.
  - Example: `Changelog: Improve reconnect handling when directory nodes flap`
- Never add `Changelog:` trailers to `docs:`, `test:`, `build:`, `refactor:`, `chore:`, or `ci:` commits.
- Changelog entries are generated at release time from commit trailers via `scripts/generate_changelog.py` (called by `scripts/bump_version.py`) to avoid merge conflicts in `CHANGELOG.md` during normal feature/fix development.

## Project Structure
Monorepo with `src/` layout. Root `pytest.ini` handles global tests.
Components: `jmcore` (Lib), `directory_server`, `jmwallet`, `maker`, `taker`, `orderbook_watcher`.

## Documentation

- Technical docs are split under docs/technical/ for architecture, protocols, and design decisions.
  - Focus on high-level concepts over implementation details.
  - Don't use more than 3 levels of headings.
- Component-specific READMEs for setup and usage.
- For docs-only fixes (typos/wording/example paths), do not add dedicated tests unless runtime behavior changes.

## References

https://github.com/JoinMarket-Org/joinmarket-clientserver/ -> reference implementation (legacy)
https://github.com/JoinMarket-Org/JoinMarket-Docs -> protocol documentation
