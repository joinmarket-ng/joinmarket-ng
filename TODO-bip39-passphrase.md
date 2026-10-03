# BIP39 Passphrase Support — Task List

Working document for branch `pr/tui-bip39-passphrase-session`.
Remove or convert into docs before merge.

## Done

- [x] TUI BIP39 passphrase flow with fingerprint confirmation (`dd705044`)
- [x] Keep TUI-managed config keys inside their chapter (`a13c2718`)
- [x] SETPP — offer BIP39 passphrase storage in config.toml via Config Center (`ca2373fb`)
- [x] DELPP — Config Center entry to delete stored BIP39 passphrase (`00670083`)

## Open

- [x] `wallet_with_passphrase` flag — gates passphrase prompting/offers; wire through
      `config.toml.template`, settings + component CLIs (`build_*_config`), add a
      settings→config round-trip test; separate commit
- [ ] **CLI create-with-passphrase gap (discuss with maintainer)** — `jm-wallet generate`
      and `jm-wallet import` have no BIP39 passphrase option; the passphrase is only
      resolved at wallet-open time via `resolve_mnemonic()` in
      `jmcore/src/jmcore/cli_common.py` (priority: `--bip39-passphrase` CLI arg →
      `BIP39_PASSPHRASE` env → `wallet.bip39_passphrase` config →
      `--prompt-bip39-passphrase` interactive prompt → empty).
      Options:
      - A) TUI prompts after generation and stores the passphrase config-side (SETPP-style)
      - B) add `--bip39-passphrase` / `--prompt-bip39-passphrase` to `jm-wallet generate`
           so the fingerprint, wallet name, and creation-height metadata are established
           with the passphrase from the start
- [ ] Optional: session caching of the passphrase (`BIP39_PASSPHRASE` env in the TUI
      subshell so the user is not re-prompted per command)
- [ ] R4: physical RaspiBlitz verification
