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

## For Signers

See [Sign](technical/development.md#sign-a-release) for instructions on how to sign a release.

## For Verifiers

See [Verify](technical/development.md#verify-a-release) for instructions on how to verify signatures.

## Trusted Keys

| Fingerprint | Name | Since |
|-------------|------|-------|
| 1C53A412D11EF3051704419C44912E1E03005B31 | m0wer | 2026-01-17 |

Note: The list of trusted keys is maintained in `trusted-keys.txt` for automated verification.

Full public keys are stored in `pubkeys/<fingerprint>.asc` for convenience.

## Notes

- Signature files are committed under `signatures/<version>/`.
- Verification scripts are in `scripts/verify-release.sh` and `scripts/sign-release.sh`.
- Release reproducibility details are documented in [Development](technical/development.md).
