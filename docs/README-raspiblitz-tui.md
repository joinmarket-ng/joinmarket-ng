# Raspiblitz TUI Script

JoinMarket-NG ships the Raspiblitz JoinMarket-NG text UI script at:

- `scripts/menu.joinmarket-ng.sh`

This is the same interactive menu used by Raspiblitz for JoinMarket-NG operations. Keeping it in this repository allows Raspiblitz to fetch and run a version that is aligned with the installed JoinMarket-NG release.

## Install

Raspiblitz installs this script automatically as `/home/joinmarketng/menu.sh` from the corresponding JoinMarket-NG release (or commit), and marks it executable.

For manual setup in a non-Raspiblitz environment:

```bash
curl -fsSL https://raw.githubusercontent.com/joinmarket-ng/joinmarket-ng/main/scripts/menu.joinmarket-ng.sh -o "$HOME/menu.sh"
chmod +x "$HOME/menu.sh"
```

## Usage

The script is intended to run in an environment where:

- JoinMarket-NG CLI tools are installed (`jm-wallet`, `jm-maker`, `jm-taker`)
- Data directory and config are at `~/.joinmarket-ng/`
- The `joinmarketng` user can call the Raspiblitz bonus script for maker service controls

Run:

```bash
./menu.sh
```

Main menu actions include:

- Send bitcoin (normal transaction or CoinJoin)
- Wallet management (create/import/select/inspect/freeze/history)
- Maker bot control (start/stop/restart/status/logs)
- Configuration editing and quick info

## Compatibility Goal

Behavior and prompts are intentionally preserved for Raspiblitz users. Changes to this script should maintain the same end-user UI/UX unless there is a strong, explicit reason to change it.

For script inventory and related tooling, see [Scripts](README-scripts.md).
