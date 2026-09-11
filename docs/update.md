# Update JoinMarket NG

Review the release notes, [check your backups](recover-wallet.md), and stop
wallet processes cleanly before updating. Let any active transaction or tumbler
phase finish. Afterward, restart with the updated software and check wallet
state; [resume a saved tumble explicitly](README-tumbler.md#resume-a-failed-plan).

> **Existing users:** If you currently update by piping the `main` installer
> from `curl`, switch to the authenticated local updater. It preserves the
> trusted rollback baseline and avoids treating unreleased installer code as
> the version already installed.

```bash
bash ~/.joinmarket-ng/install.sh --update
```

If that file does not exist yet, follow [Older Installations](#older-installations)
once to create it.

## Saved Updater

After a successful verified installation, update with the installer saved in
your data directory:

```bash
bash ~/.joinmarket-ng/install.sh --update
```

The saved updater authenticates the current release installer before updating
the application. It preserves your existing `config.toml`, refreshes the nearby
`config.toml.template`, and shows the template differences when available.
Settings you omit use the new version's built-in defaults, which may change
between releases. Do not replace your
configuration with the full template. Review the relevant
[release notes](https://github.com/joinmarket-ng/joinmarket-ng/releases) before
adopting new settings.

To install a particular release, add `--version X.Y.Z`. This selects the
application release; the saved installer still refreshes to the latest
authenticated installer.

When updating from the TUI, the output is saved in a private `update.log.*` file
under your data directory's `logs` folder. After the update, a scrollable viewer
opens at the end of the log so you can review configuration-template differences
and any errors. Use the arrow keys or Page Up/Page Down to scroll, then press `q`
to continue. If `less` is unavailable, the TUI waits for Enter instead. The log
remains available after you leave the TUI. A successful update exits the TUI;
a failed update returns to the update menu after review.

## Older Installations

Installations created before the saved updater can migrate with one final HTTPS
bootstrap:

```bash
curl -sSL https://raw.githubusercontent.com/joinmarket-ng/joinmarket-ng/main/install.sh | bash -s -- --update
```

This bootstrap trusts GitHub and TLS for that run. It is not a retroactive
authenticity guarantee for the old installation. After it succeeds, use the
saved updater for subsequent updates.

## Custom Directories

If the data directory or virtual environment is custom, preserve both settings
when updating outside the generated `activate.sh` environment:

```bash
JOINMARKET_DATA_DIR=/srv/joinmarket \
  bash /srv/joinmarket/install.sh --update --venv /opt/jmng-venv
```

The generated `activate.sh` exports `JOINMARKET_DATA_DIR` and `JMNG_VENV_DIR`.
Source it before normal commands so they use the same installation.

Do not use `--dev`, `--version main`, or `--skip-verify` for a funded wallet;
those paths bypass signed release verification. See
[supply-chain security](install-advanced.md#supply-chain-security) for the
trust model and manual verification.
