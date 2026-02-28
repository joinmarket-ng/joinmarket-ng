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

## For Signers

See [DOCS.md](../DOCS.md#signing-a-release) for instructions on how to sign a release.

## For Verifiers

See [DOCS.md](../DOCS.md#verifying-signatures) for instructions on how to verify signatures.

## Trusted Keys

| Fingerprint | Name | Since |
|-------------|------|-------|
| 1C53A412D11EF3051704419C44912E1E03005B31 | m0wer | 2026-01-17 |

Note: The list of trusted keys is maintained in `trusted-keys.txt` for automated verification.

Full public keys are stored in `pubkeys/<fingerprint>.asc` for convenience.
