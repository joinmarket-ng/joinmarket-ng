#!/usr/bin/env bash
# =============================================================================
# JoinMarket NG Release Signing Script
#
# This script helps trusted parties sign release manifests.
#
# Usage:
#   ./scripts/sign-release.sh <version> [--key <fingerprint>]
#   ./scripts/sign-release.sh --key <fingerprint>  # Auto-detect latest unsigned
#
# Requirements:
#   - gpg (GnuPG) with a valid signing key
#   - curl or wget
#   - git
#   - gh (GitHub CLI) for auto-detection
#   - docker with buildx (for --reproduce)
#   - jq (for layer digest extraction)
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
REPO="joinmarket-ng/joinmarket-ng"
REGISTRY="ghcr.io"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# Images currently excluded from strict layer reproducibility checks.
# jam-ng: frontend bundle build (CRA/webpack) is non-deterministic across environments.
SKIP_VERIFY_IMAGES=("jam-ng")

usage() {
    cat << EOF
Usage: $(basename "$0") [version] [options]

Sign a JoinMarket NG release manifest.

Arguments:
  version         Release version to sign (e.g., 1.0.0)
                  If omitted, auto-detects latest unsigned release for your key

Options:
  --key KEY       GPG key fingerprint to use for signing (required for auto-detect)
  --manifest FILE Use a local manifest file instead of downloading from GitHub.
                  When used with build-release.sh output, reproduction is skipped
                  by default since the manifest was just built locally.
  --reproduce     Build locally and verify digests match before signing (recommended)
  --no-reproduce  Skip local build verification (not recommended)
  --no-push       Don't automatically commit and push the signature (default: push)
  --help          Show this help message

When signing a CI-built release (default, no --manifest):
  All signers should use --reproduce to independently verify that builds are
  reproducible before signing. By default, --reproduce is enabled.

When signing a locally-built manifest (--manifest):
  The manifest was just generated from a local build, so reproduction is skipped
  by default. Use --reproduce to force a rebuild and verify. The embedded
  manifest commit must match your local release tag (or HEAD if no tag exists).

The reproduce check compares layer digests (content-addressable, format-independent)
rather than manifest digests, ensuring reliable comparison regardless of build environment.
Builds are verified for the current architecture only (cross-platform builds via QEMU
produce different layer digests than native builds).

Reproduction uses Dockerfiles from the release commit to ensure strict historical accuracy.

Workflows:

  Local-first (recommended for release managers):
    1. bump_version.py patch --no-push
    2. build-release.sh
    3. sign-release.sh <version> --manifest release-manifest-<version>.txt --key <fp>
    4. git push && git push --tags   (CI verifies digests match)

  CI-first (for additional signers):
    1. Wait for CI release to complete
    2. sign-release.sh <version> --key <fp>   (downloads manifest, reproduces, signs)

Examples:
  $(basename "$0") 1.0.0 --manifest release-manifest-1.0.0.txt --key ABCD1234...
  $(basename "$0") 1.0.0 --key ABCD1234...              # Verify and sign
  $(basename "$0") 1.0.0 --key ABCD1234... --no-reproduce  # Sign without verify (not recommended)
  $(basename "$0") --key ABCD1234...                    # Auto-detect latest unsigned
  $(basename "$0") 1.0.0 --key ABCD1234... --no-push
EOF
    exit 1
}

# Detect current architecture in Docker format
detect_arch() {
    local arch
    arch=$(uname -m)
    case "$arch" in
        x86_64)  echo "amd64" ;;
        aarch64) echo "arm64" ;;
        armv7l)  echo "arm-v7" ;;
        *)       echo "$arch" ;;
    esac
}

extract_manifest_value() {
    local key="$1"
    local manifest_file="$2"

    awk -F': ' -v key="$key" '$1 == key { print $2; exit }' "$manifest_file"
}

validate_local_manifest_identity() {
    local manifest_file="$1"
    local version="$2"
    local manifest_commit
    local expected_commit
    local expected_ref

    manifest_commit=$(extract_manifest_value "commit" "$manifest_file")
    if [[ -z "$manifest_commit" ]]; then
        log_error "Local manifest is missing commit metadata: $manifest_file"
        return 1
    fi

    if git -C "$PROJECT_ROOT" rev-parse -q --verify "refs/tags/${version}^{commit}" >/dev/null 2>&1; then
        expected_ref="tag $version"
        expected_commit=$(git -C "$PROJECT_ROOT" rev-list -n 1 "$version")
    else
        expected_ref="current HEAD"
        expected_commit=$(git -C "$PROJECT_ROOT" rev-parse HEAD)
        log_warn "Local tag $version not found; validating local manifest against current HEAD"
    fi

    if [[ "$manifest_commit" != "$expected_commit" ]]; then
        log_error "Local manifest commit does not match $expected_ref."
        log_error "  Manifest commit: $manifest_commit"
        log_error "  Expected commit: $expected_commit"
        log_error "Rebuild the manifest from the release commit before signing."
        return 1
    fi

    log_info "Local manifest commit matches $expected_ref"
}

# Parse arguments
VERSION=""
GPG_KEY=""
REPRODUCE=""  # Will be set based on --manifest if not explicitly specified
AUTO_PUSH=true
LOCAL_MANIFEST=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --key)
            GPG_KEY="$2"
            shift 2
            ;;
        --manifest)
            LOCAL_MANIFEST="$2"
            shift 2
            ;;
        --reproduce)
            REPRODUCE=true
            shift
            ;;
        --no-reproduce)
            REPRODUCE=false
            shift
            ;;
        --no-push)
            AUTO_PUSH=false
            shift
            ;;
        --help|-h)
            usage
            ;;
        *)
            if [[ -z "$VERSION" ]]; then
                VERSION="$1"
            else
                log_error "Unknown argument: $1"
                usage
            fi
            shift
            ;;
    esac
done

# Set reproduce default based on whether a local manifest is provided
if [[ -z "$REPRODUCE" ]]; then
    if [[ -n "$LOCAL_MANIFEST" ]]; then
        REPRODUCE=false  # Local manifest = just built locally, skip re-build
    else
        REPRODUCE=true   # CI manifest = reproduce to verify
    fi
fi

# Check for jq (required for all operations now)
if ! command -v jq &> /dev/null; then
    log_error "jq is required. Please install it."
    exit 1
fi

# Check/setup buildx builder with docker-container driver for OCI export support
# The default 'docker' driver doesn't support OCI export format
setup_buildx_builder() {
    local builder_name="jmng-verify"

    # First, check if we already have a working builder by checking the driver
    # Use awk for compatibility (grep -oP not available everywhere)
    local current_driver
    current_driver=$(docker buildx inspect 2>/dev/null | awk '/^Driver:/{print $2}')

    if [[ "$current_driver" == "docker-container" ]]; then
        # Current builder already supports OCI export
        return 0
    fi

    # Check if our custom builder already exists
    if docker buildx inspect "$builder_name" &>/dev/null; then
        docker buildx use "$builder_name" >/dev/null 2>&1
        log_info "Using buildx builder: $builder_name"
        return 0
    fi

    # Need to create a new builder
    log_info "Creating buildx builder with docker-container driver..."
    log_info "The default 'docker' driver doesn't support OCI export format."

    if docker buildx create --name "$builder_name" --driver docker-container --bootstrap; then
        docker buildx use "$builder_name"
        log_info "Created and activated buildx builder: $builder_name"
        return 0
    else
        log_error "Failed to create buildx builder."
        log_error "You can manually create one with:"
        log_error "  docker buildx create --name $builder_name --driver docker-container --use"
        log_error "Or enable the containerd image store in Docker Desktop settings."
        return 1
    fi
}

# =============================================================================
# Step 0: Get GPG key early (needed for auto-detection)
# =============================================================================
if [[ -z "$GPG_KEY" ]]; then
    log_info "Available GPG secret keys:"
    gpg --list-secret-keys --keyid-format LONG
    echo ""
    read -p "Enter GPG key fingerprint to use: " GPG_KEY
fi

# Get full fingerprint
FULL_FINGERPRINT=$(gpg --fingerprint "$GPG_KEY" 2>/dev/null | \
                   grep -oP '[A-F0-9]{4}\s+[A-F0-9]{4}\s+[A-F0-9]{4}\s+[A-F0-9]{4}\s+[A-F0-9]{4}\s+[A-F0-9]{4}\s+[A-F0-9]{4}\s+[A-F0-9]{4}\s+[A-F0-9]{4}\s+[A-F0-9]{4}' | \
                   tr -d ' ' | head -1)

if [[ -z "$FULL_FINGERPRINT" ]]; then
    log_error "Could not find GPG key: $GPG_KEY"
    exit 1
fi

log_info "Using GPG key: $FULL_FINGERPRINT"

# =============================================================================
# Step 0.5: Auto-detect latest unsigned release if version not specified
# =============================================================================
if [[ -z "$VERSION" ]]; then
    if [[ -n "$LOCAL_MANIFEST" ]]; then
        # Extract version from local manifest filename (release-manifest-X.Y.Z.txt)
        VERSION=$(basename "$LOCAL_MANIFEST" .txt | sed 's/^release-manifest-//')
        if [[ -z "$VERSION" || "$VERSION" == "$(basename "$LOCAL_MANIFEST" .txt)" ]]; then
            log_error "Could not extract version from manifest filename: $LOCAL_MANIFEST"
            log_error "Expected format: release-manifest-X.Y.Z.txt"
            exit 1
        fi
        log_info "Extracted version from manifest filename: $VERSION"
    else
        log_info "No version specified, auto-detecting latest unsigned release..."

        if ! command -v gh &> /dev/null; then
            log_error "GitHub CLI (gh) is required for auto-detection. Please install it or specify a version."
            exit 1
        fi

        # Get all releases sorted by date (newest first)
        RELEASES=$(gh release list --repo "$REPO" --limit 20 | awk '{print $1}')

        if [[ -z "$RELEASES" ]]; then
            log_error "No releases found in repository"
            exit 1
        fi

        # Find the first release without a signature from this key
        for release in $RELEASES; do
            # Check if signature file exists for this release
            SIG_PATH="$PROJECT_ROOT/signatures/$release/${FULL_FINGERPRINT}.sig"
            if [[ ! -f "$SIG_PATH" ]]; then
                VERSION="$release"
                log_info "Found unsigned release: $VERSION"
                break
            fi
        done

        if [[ -z "$VERSION" ]]; then
            log_info "All recent releases are already signed with your key!"
            exit 0
        fi
    fi
fi

# Create temp directory
WORK_DIR=$(mktemp -d)
trap "rm -rf $WORK_DIR" EXIT

log_info "Signing JoinMarket NG release $VERSION"

# =============================================================================
# Step 1: Get release manifest (local or download)
# =============================================================================
MANIFEST_FILE="$WORK_DIR/release-manifest-${VERSION}.txt"

if [[ -n "$LOCAL_MANIFEST" ]]; then
    # Use local manifest from build-release.sh
    if [[ ! -f "$LOCAL_MANIFEST" ]]; then
        log_error "Local manifest file not found: $LOCAL_MANIFEST"
        exit 1
    fi
    log_info "Using local manifest: $LOCAL_MANIFEST"
    cp "$LOCAL_MANIFEST" "$MANIFEST_FILE"

    if ! validate_local_manifest_identity "$MANIFEST_FILE" "$VERSION"; then
        exit 1
    fi
else
    # Download from GitHub Releases
    log_info "Downloading release manifest..."

    MANIFEST_URL="https://github.com/${REPO}/releases/download/${VERSION}/release-manifest-${VERSION}.txt"

    if command -v curl &> /dev/null; then
        curl -fsSL "$MANIFEST_URL" -o "$MANIFEST_FILE" || {
            log_error "Failed to download release manifest from $MANIFEST_URL"
            exit 1
        }
    elif command -v wget &> /dev/null; then
        wget -q "$MANIFEST_URL" -O "$MANIFEST_FILE" || {
            log_error "Failed to download release manifest from $MANIFEST_URL"
            exit 1
        }
    else
        log_error "Neither curl nor wget found. Please install one of them."
        exit 1
    fi
fi

log_info "Downloaded release manifest:"
echo ""
cat "$MANIFEST_FILE"
echo ""

# =============================================================================
# Step 2: Optionally reproduce builds (recommended for all signers)
# =============================================================================
REPRODUCE_ERRORS=0
REPRODUCE_SUCCESS=0

if [[ "$REPRODUCE" == true ]]; then
    # Ensure buildx builder supports OCI export
    if ! setup_buildx_builder; then
        exit 1
    fi

    # Detect current architecture
    CURRENT_ARCH=$(detect_arch)
    case "$CURRENT_ARCH" in
        amd64)  PLATFORM="linux/amd64" ;;
        arm64)  PLATFORM="linux/arm64" ;;
        arm-v7) PLATFORM="linux/arm/v7" ;;
        *)
            log_error "Unsupported architecture: $CURRENT_ARCH"
            exit 1
            ;;
    esac

    log_info "Reproducing Docker builds for $CURRENT_ARCH..."
    log_info "Comparing layer digests (content-addressable, format-independent)"

    # Extract commit and SOURCE_DATE_EPOCH from manifest
    COMMIT=$(grep "^commit:" "$MANIFEST_FILE" | cut -d' ' -f2)
    SOURCE_DATE_EPOCH=$(grep "^source_date_epoch:" "$MANIFEST_FILE" | cut -d' ' -f2)

    if [[ -z "$COMMIT" || -z "$SOURCE_DATE_EPOCH" ]]; then
        log_error "Could not extract commit or SOURCE_DATE_EPOCH from manifest"
        exit 1
    fi

    log_info "Commit: $COMMIT"
    log_info "SOURCE_DATE_EPOCH: $SOURCE_DATE_EPOCH"
    log_info "Platform: $PLATFORM"

    # Use git worktree from local repo (faster and more secure than cloning)
    # This uses locally verified code instead of trusting remote blindly
    REPO_DIR="$WORK_DIR/repo"

    # Check if commit exists locally
    if ! git -C "$PROJECT_ROOT" cat-file -e "$COMMIT^{commit}" 2>/dev/null; then
        log_error "Commit $COMMIT not found locally."
        log_error "Please fetch it first: git fetch origin"
        log_error "Or fetch the specific tag: git fetch origin tag $VERSION"
        exit 1
    fi

    # Create worktree at the specific commit
    log_info "Creating git worktree at commit $COMMIT..."
    git -C "$PROJECT_ROOT" worktree add --detach "$REPO_DIR" "$COMMIT"
    # Clean up worktree on exit
    trap "rm -rf '$WORK_DIR'; git -C '$PROJECT_ROOT' worktree remove --force '$REPO_DIR' 2>/dev/null || true" EXIT

    cd "$REPO_DIR"

    # Build images for current architecture only
    # Images and their corresponding targets (must match CI workflow matrix)
    IMAGES=("directory-server" "maker" "taker" "orderbook-watcher" "jmwalletd" "jam-ng")
    DOCKERFILES=("./directory_server/Dockerfile" "./maker/Dockerfile" "./taker/Dockerfile" "./orderbook_watcher/Dockerfile" "./jmwalletd/Dockerfile" "./jmwalletd/Dockerfile")
    TARGETS=("production" "" "" "" "jmwalletd" "jam-ng")  # Empty string means no --target (uses default)

    # Create OCI output directory
    OCI_DIR="$WORK_DIR/oci"
    mkdir -p "$OCI_DIR"

    for i in "${!IMAGES[@]}"; do
        image="${IMAGES[$i]}"
        dockerfile="${DOCKERFILES[$i]}"
        target="${TARGETS[$i]}"
        layers_key="${image}-${CURRENT_ARCH}-layers"

        log_info "Building $image for $PLATFORM..."

        # Build to OCI tar format
        OCI_TAR="$OCI_DIR/${image}.tar"
        OCI_EXTRACT="$OCI_DIR/${image}"
        mkdir -p "$OCI_EXTRACT"

        # Build command with optional --target
        # Use rewrite-timestamp to clamp file timestamps to SOURCE_DATE_EPOCH for reproducibility
        BUILD_CMD=(docker buildx build
            --file "$dockerfile"
            --build-arg SOURCE_DATE_EPOCH="$SOURCE_DATE_EPOCH"
            --build-arg VERSION="$VERSION"
            --platform "$PLATFORM"
            --output "type=oci,dest=${OCI_TAR},rewrite-timestamp=true"
            --no-cache)
        if [[ -n "$target" ]]; then
            BUILD_CMD+=(--target "$target")
        fi

        if ! SOURCE_DATE_EPOCH="$SOURCE_DATE_EPOCH" "${BUILD_CMD[@]}" \
            . 2>&1 | tee "$WORK_DIR/${image}-build.log"; then
            log_error "  Build failed for $image"
            REPRODUCE_ERRORS=$((REPRODUCE_ERRORS + 1))
            continue
        fi

        # Extract OCI tar and get layer digests
        tar -xf "$OCI_TAR" -C "$OCI_EXTRACT"

        # Get the manifest digest from OCI index.json
        manifest_digest=$(jq -r '.manifests[0].digest' "$OCI_EXTRACT/index.json")
        manifest_file="$OCI_EXTRACT/blobs/sha256/${manifest_digest#sha256:}"

        # Extract layer digests from the manifest
        built_layers=$(jq -r '.layers[].digest' "$manifest_file" | sort)
        built_layers_file="$WORK_DIR/${image}-built-layers.txt"
        echo "$built_layers" > "$built_layers_file"

        # Extract expected layers from manifest file
        # Look for section starting with "### ${image}-${CURRENT_ARCH}-layers"
        expected_layers_file="$WORK_DIR/${image}-expected-layers.txt"
        sed -n "/^### ${image}-${CURRENT_ARCH}-layers\$/,/^###/{/^sha256:/p}" "$MANIFEST_FILE" | \
            sort > "$expected_layers_file"

        # Compare layer digests
        # Check if this image is excluded from verification
        skip_verify=false
        for skip in "${SKIP_VERIFY_IMAGES[@]}"; do
            if [[ "$image" == "$skip" ]]; then
                skip_verify=true
                break
            fi
        done

        if $skip_verify; then
            log_warn "  Skipping layer verification for $image (non-reproducible upstream build)"
            REPRODUCE_SUCCESS=$((REPRODUCE_SUCCESS + 1))
        elif [[ -s "$expected_layers_file" ]]; then
            if diff -q "$expected_layers_file" "$built_layers_file" > /dev/null 2>&1; then
                layer_count=$(wc -l < "$built_layers_file")
                log_info "  Reproduced successfully! ($layer_count layers match)"
                REPRODUCE_SUCCESS=$((REPRODUCE_SUCCESS + 1))
            else
                log_error "  Layer digest mismatch!"
                log_error "  Expected layers:"
                cat "$expected_layers_file" | sed 's/^/    /'
                log_error "  Built layers:"
                cat "$built_layers_file" | sed 's/^/    /'
                REPRODUCE_ERRORS=$((REPRODUCE_ERRORS + 1))
            fi
        else
            log_warn "  No layer digests found in manifest for $layers_key"
            log_warn "  Manifest may be old format - skipping layer comparison"
        fi

        # Clean up OCI files
        rm -rf "$OCI_TAR" "$OCI_EXTRACT"
    done

    cd "$PROJECT_ROOT"

    echo ""
    log_info "Reproducibility Summary: $REPRODUCE_SUCCESS succeeded, $REPRODUCE_ERRORS failed"

    if [[ $REPRODUCE_ERRORS -gt 0 ]]; then
        log_error "Cannot sign: builds do not reproduce!"
        log_error "The locally built images have different layer digests than the manifest."
        log_error "Possible causes:"
        log_error "  - Different BuildKit version than CI"
        log_error "  - Platform differences"
        log_error "  - Non-deterministic build steps"
        exit 1
    fi

    log_info "All builds reproduced successfully!"
    echo ""
fi

# =============================================================================
# Step 3: Sign the manifest
# =============================================================================
log_info "Using GPG key: $FULL_FINGERPRINT"
log_info "Signing release manifest..."

SIG_DIR="$PROJECT_ROOT/signatures/$VERSION"
mkdir -p "$SIG_DIR"

SIG_FILE="$SIG_DIR/${FULL_FINGERPRINT}.sig"

gpg --local-user "$GPG_KEY" --armor --detach-sign --output "$SIG_FILE" "$MANIFEST_FILE"

log_info "Signature created: $SIG_FILE"

# =============================================================================
# Step 4: Verify the signature
# =============================================================================
log_info "Verifying signature..."

if gpg --verify "$SIG_FILE" "$MANIFEST_FILE"; then
    log_info "Signature verified successfully!"
else
    log_error "Signature verification failed!"
    rm -f "$SIG_FILE"
    exit 1
fi

# =============================================================================
# Summary and next steps
# =============================================================================
echo ""
echo "=============================================="
log_info "Signing Complete!"
echo "=============================================="
echo ""
echo "Signature file: $SIG_FILE"

# If using a local manifest, copy it alongside the signature for CI verification
LOCAL_MANIFEST_COPY=""
if [[ -n "$LOCAL_MANIFEST" ]]; then
    LOCAL_MANIFEST_COPY="$SIG_DIR/${FULL_FINGERPRINT}-manifest.txt"
    cp "$MANIFEST_FILE" "$LOCAL_MANIFEST_COPY"
    log_info "Local manifest saved: $LOCAL_MANIFEST_COPY"
    echo "  (CI will verify its layer digests match this manifest)"
fi
echo ""

# Check if key is in trusted-keys.txt
TRUSTED_KEYS="$PROJECT_ROOT/signatures/trusted-keys.txt"
if ! grep -q "$FULL_FINGERPRINT" "$TRUSTED_KEYS" 2>/dev/null; then
    log_warn "Your key is not in trusted-keys.txt"
    echo "To be included in automated verification, add your key:"
    echo ""
    echo "  echo '$FULL_FINGERPRINT Your Name' >> signatures/trusted-keys.txt"
    echo ""
fi

# =============================================================================
# Step 5: Auto commit and push (unless --no-push)
# =============================================================================
if [[ "$AUTO_PUSH" == true ]]; then
    log_info "Committing signature..."

    cd "$PROJECT_ROOT"
    git add "$SIG_FILE"
    if [[ -n "$LOCAL_MANIFEST_COPY" ]]; then
        git add "$LOCAL_MANIFEST_COPY"
    fi
    git commit -m "build: add GPG signature for release $VERSION"

    if [[ -n "$LOCAL_MANIFEST" ]]; then
        log_info "Signature committed (not pushing - push tag manually to trigger CI)"
        echo ""
        echo "Next steps:"
        echo "  git push && git push --tags"
        echo ""
        echo "CI will build images independently and verify layer digests match"
        echo "your local manifest."
    else
        git push
        log_info "Signature committed and pushed successfully!"
    fi
else
    echo "Next steps:"
    echo "1. Review the signature file"
    echo "2. Commit and push your signature:"
    if [[ -n "$LOCAL_MANIFEST_COPY" ]]; then
        echo "   git add $SIG_FILE $LOCAL_MANIFEST_COPY"
    else
        echo "   git add $SIG_FILE"
    fi
    echo "   git commit -m 'build: add GPG signature for release $VERSION'"
    echo "   git push"
    echo ""
    if [[ -n "$LOCAL_MANIFEST" ]]; then
        echo "3. Push the tag to trigger CI:"
        echo "   git push --tags"
        echo ""
        echo "CI will verify its layer digests match your local manifest."
    else
        echo "3. Or create a PR with your signature if you don't have write access"
        echo ""
    fi
fi
