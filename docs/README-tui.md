# Use The Terminal Menu

The terminal menu uses the same native wallets and commands as the CLI. It is
not JAM, and does not open JAM or reference-implementation `.jmdat` wallet files.

## Install

Complete [installation](install.md) and [backend setup](setup.md). The menu also
needs `whiptail` (on Debian or Ubuntu, `sudo apt install whiptail`). In an
activated installation, run:

```bash
jm-ng
```

Raspiblitz installs its menu at `/home/joinmarketng/menu.sh` and manages maker
service control through its integration.

## Main Menu

![JoinMarket NG terminal menu](media/main-menu.png)

Check the active wallet in the header before an operation. Use Wallet Management
to create, import, or select a wallet. Follow the same
[backup requirements](recover-wallet.md) as with the CLI.

## Send Bitcoin

Choose a source mixdepth, amount in satoshis, and destination. Zero counterparties
means an ordinary payment, not a CoinJoin. Zero amount means a sweep, not a
zero-value payment. Review the fee and destination before confirming.

Manual coin control is limited to one mixdepth. See [wallet tasks](README-jmwallet.md)
for direct payments and [the taker guide](README-taker.md) for CoinJoins.

## Wallet Management

Use the balance, history, address, and freeze controls to inspect the selected
wallet. Viewing recovery words is sensitive; do not screen-share or capture it.
The BIP39 passphrase prompt is separate from wallet-file decryption. Verify the
derived wallet identity before accepting it, especially after recovery.

## Maker Bot Control

Start, stop, and inspect the maker here. On Raspiblitz the menu controls a
systemd service; standalone installations manage a local maker process.

### Encrypted Wallet Password Handling

Starting an encrypted wallet requires its password. Choosing to store it writes
it in plain text to `config.toml` for unattended startup. Declining permanent
storage does not remove all exposure: on Raspiblitz it is staged in a
permission-restricted `.maker.env` file while the maker runs. Other wallet
commands can reuse that staged credential. Protect the host in either case.

Use [unattended maker guidance](maker-service.md) to decide whether automatic
startup is appropriate; do not assume wallet-file encryption protects against
someone who can also read the unlock credentials.

## Fidelity Bonds

Read [bond operations](fidelity-bond-operations.md) before creating a bond. A
lock cannot be undone early. The menu's bond controls do not replace backup
and signer-compatibility checks.
