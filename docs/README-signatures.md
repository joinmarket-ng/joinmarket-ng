# GPG Signatures for JoinMarket NG Releases

This directory contains GPG signatures from trusted parties who have verified
and attested to specific releases of JoinMarket NG.

## Structure

```
signatures/
  pubkeys/
    <fingerprint>.asc      # Full GPG public keys
  <version>/
    <fingerprint>.sig      # Detached signature of the release manifest
    <fingerprint>.install.sh.sig  # Detached signature of the release installer asset
    <fingerprint>-manifest.txt  # Local manifest (local-first workflow only)
```

## How Signing Works

1. A release is created with Docker images and a release manifest
2. The release manifest contains:
   - Git commit hash
   - SOURCE_DATE_EPOCH used for reproducible builds
   - Docker image digests (sha256)
3. Trusted parties independently:
   - Verify they can reproduce the same image digests from source
   - Sign the release manifest with their GPG key
   - Submit a PR with their signature

### Signing Workflows

**Local-first** (recommended for release managers): Build images locally using
`build-release.sh`, sign the local manifest with `sign-release.sh --manifest`,
then push the tag to trigger CI. CI verifies its builds match the signed manifest.

**CI-first** (for additional signers): Wait for CI to complete, then run
`sign-release.sh` which downloads the manifest, reproduces locally, and signs.
CI-first signatures sign the shared GitHub Release asset named
`release-manifest-<version>.txt`; they do not need a per-signer manifest in the
repository. The installer tries that shared manifest first, then falls back to
`<fingerprint>-manifest.txt` for local-first and historical releases. In both
cases it requires the signed `commit:` value to match the commit it installs.

## Release Lifecycle

Tag pushes create GitHub releases as **pre-releases**, which are excluded
from `releases/latest` and therefore invisible to the installer, the TUI,
and the update check. When signature commits land on `main`, the
`promote-release` workflow runs `scripts/verify-release.sh` against every
pending pre-release and promotes those that reach the quorum (manifest and
installer signatures, installer asset matching the release commit, registry
digests) to the published latest release.

Promotion is publication automation, not a security boundary: `install.sh`
enforces the same signature quorum on every user machine regardless of the
release state on GitHub.

## Installer Signatures

The initial installer bootstrap downloads the versioned GitHub Release
`install.sh` asset. Its detached signatures are committed on `main` at
`signatures/<version>/<fingerprint>.install.sh.sig`, rather than uploaded as
release assets. A saved installer accepts only signatures made by its embedded
primary fingerprints; downloaded public-key bytes and signature filenames never
choose trusted identities.

The release signing workflow obtains `install.sh` from the Git commit attested
by the manifest and automatically writes `<fingerprint>.install.sh.sig`. CI
publishes that commit-derived `install.sh` as the release asset. A release is
ready for trusted installation only when the asset and the required installer
and manifest signature quorum are available; missing signatures fail closed.

## For Signers

See [Sign](technical/development.md#sign-a-release) for instructions on how to sign a release.

## For Verifiers

See [Verify](technical/development.md#verify-a-release) for instructions on how to verify signatures.
Installation and release verification require two valid signatures from distinct trusted keys by default.
Users can deliberately select another threshold with `--min-sigs N`.

## Trusted Keys

| Fingerprint | Name | Since |
|-------------|------|-------|
| 1C53A412D11EF3051704419C44912E1E03005B31 | m0wer | 2026-01-17 |
| 9253062A4F92D63459085CA62D230520212A5901 | /dev/fd0 | 2026-07-13 |

`trusted-keys.txt` lists release signers for the maintainer tools. Installed
copies of `install.sh` pin their own fingerprints; editing this list alone
cannot authorize a new installer signer.
`verify-release.sh` uses a temporary GnuPG keyring, so keys in your personal
keyring do not count toward the quorum.

Full public keys are stored in `pubkeys/<fingerprint>.asc` for convenience.

## Notes

- Signature files are committed under `signatures/<version>/`.
- Verification scripts are in `scripts/verify-release.sh` and `scripts/sign-release.sh`.
- Release reproducibility details are documented in [Development](technical/development.md).

## Trust Scope and Key Changes

The local installer's fingerprint list is the trust anchor. Verification does
not protect a compromised local machine, a compromised signing quorum, or a
release/service freeze. Key migration is planned to require the old quorum's
signatures, with manual recovery until then; there is no automatic custom key
rotation mechanism.
