# Scripts

Utility scripts live in `scripts/`.

For the maintained, full list and descriptions, use:

- repository file: `scripts/README.md`

This docs page intentionally stays minimal to avoid duplication.

## Most Common Scripts

- `scripts/run_parallel_tests.sh`: run unit + Docker-backed test suites in parallel via Docker Compose project isolation
- `scripts/generate_changelog.py`: generate release changelog entries
- `scripts/update_readme_help.py`: refresh CLI help blocks in component READMEs

The TUI menu (`jm-ng`) is bundled as package data inside **jmcore** and
available as a console entry point after installation. See [TUI Menu](README-tui.md).
