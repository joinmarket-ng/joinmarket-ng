#!/usr/bin/env bash
# =============================================================================
# JoinMarket NG Release Verification Script
#
# This script verifies:
# 1. GPG signatures on release manifests
# 2. Docker image digests match the signed manifest
# 3. Optionally reproduces the build to verify reproducibility
#
# Usage:
#   ./scripts/verify-release.sh <version>
#   ./scripts/verify-release.sh <version> --reproduce
#
# Requirements:
#   - gpg (GnuPG)
#   - docker with buildx (for image verification and reproduction)
#   - curl or wget
#   - git
#   - jq (for digest extraction)
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
REGISTRY="ghcr.io"
REPO="joinmarket-ng/joinmarket-ng"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

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

usage() {
    cat << EOF
Usage: $(basename "$0") <version> [options]

Verify JoinMarket NG release signatures and optionally reproduce builds.

Arguments:
  version         Release version to verify (e.g., 1.0.0)

Options:
  --reproduce       Attempt to reproduce the Docker builds locally
  --min-sigs N      Require at least N valid signatures (default: 1)
  --skip-signatures Skip GPG signature verification (for testing reproducibility)
  --help            Show this help message

The --reproduce flag builds images for your current architecture only and
compares layer digests against the release manifest. Layer digests are
content-addressable and identical regardless of manifest format.

Reproduction uses Dockerfiles from the release commit to ensure strict
historical accuracy.

Examples:
  $(basename "$0") 1.0.0
  $(basename "$0") 1.0.0 --reproduce
  $(basename "$0") 1.0.0 --min-sigs 2
EOF
    exit 1
}

# Parse arguments
VERSION=""
REPRODUCE=false
MIN_SIGS=1
SKIP_SIGNATURES=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --reproduce)
            REPRODUCE=true
            shift
            ;;
        --min-sigs)
            MIN_SIGS="$2"
            shift 2
            ;;
        --skip-signatures)
            SKIP_SIGNATURES=true
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

if [[ -z "$VERSION" ]]; then
    log_error "Version is required"
    usage
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

# Create temp directory for verification
WORK_DIR=$(mktemp -d)
trap "rm -rf $WORK_DIR" EXIT

log_info "Verifying JoinMarket NG release $VERSION"
log_info "Working directory: $WORK_DIR"

# =============================================================================
# Step 1: Download release manifest
# =============================================================================
log_info "Downloading release manifest..."

MANIFEST_URL="https://github.com/${REPO}/releases/download/${VERSION}/release-manifest-${VERSION}.txt"
MANIFEST_FILE="$WORK_DIR/release-manifest-${VERSION}.txt"

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

log_info "Downloaded release manifest"

# =============================================================================
# Step 2: Fetch and verify GPG signatures
# =============================================================================
if [[ "$SKIP_SIGNATURES" == true ]]; then
    log_warn "Skipping GPG signature verification (--skip-signatures)"
else
    log_info "Checking GPG signatures..."

    SIG_DIR="$PROJECT_ROOT/signatures/$VERSION"
    VALID_SIGS=0
    SIGNERS=()

    if [[ -d "$SIG_DIR" ]]; then
        # Import trusted keys
        TRUSTED_KEYS="$PROJECT_ROOT/signatures/trusted-keys.txt"
        if [[ -f "$TRUSTED_KEYS" ]]; then
            log_info "Importing trusted keys..."
            while IFS=' ' read -r fingerprint name || [[ -n "$fingerprint" ]]; do
                # Skip comments and empty lines
                [[ "$fingerprint" =~ ^#.*$ || -z "$fingerprint" ]] && continue

                # Try to import from keyserver
                gpg --keyserver hkps://keys.openpgp.org --recv-keys "$fingerprint" 2>/dev/null || \
                gpg --keyserver hkps://keyserver.ubuntu.com --recv-keys "$fingerprint" 2>/dev/null || \
                log_warn "Could not import key $fingerprint ($name)"
            done < "$TRUSTED_KEYS"
        fi

        # Verify each signature
        for sig_file in "$SIG_DIR"/*.sig; do
            [[ -f "$sig_file" ]] || continue

            fingerprint=$(basename "$sig_file" .sig)
            log_info "Verifying signature from $fingerprint..."

            if gpg --verify "$sig_file" "$MANIFEST_FILE" 2>/dev/null; then
                log_info "Valid signature from $fingerprint"
                VALID_SIGS=$((VALID_SIGS + 1))
                SIGNERS+=("$fingerprint")
            else
                log_warn "Invalid signature from $fingerprint"
            fi
        done
    else
        log_warn "No signatures found for version $VERSION"
        log_warn "Signature directory: $SIG_DIR"
    fi

    log_info "Valid signatures: $VALID_SIGS"

    if [[ $VALID_SIGS -lt $MIN_SIGS ]]; then
        log_error "Insufficient valid signatures. Required: $MIN_SIGS, Found: $VALID_SIGS"
        log_error "This release has not been verified by enough trusted parties."
        exit 1
    fi
fi

# =============================================================================
# Step 3: Verify Docker image digests from registry
# =============================================================================
log_info "Verifying Docker image manifest digests from registry..."

# Extract manifest digests from manifest file
declare -A EXPECTED_MANIFEST_DIGESTS
while IFS=': ' read -r key value || [[ -n "$key" ]]; do
    # Skip comments and non-digest lines
    [[ "$key" =~ ^#.*$ || -z "$value" ]] && continue
    [[ "$key" == "commit" || "$key" == "source_date_epoch" ]] && continue

    if [[ "$key" =~ -manifest$ && "$value" =~ ^sha256: ]]; then
        EXPECTED_MANIFEST_DIGESTS["$key"]="$value"
    fi
done < "$MANIFEST_FILE"

DIGEST_ERRORS=0

for key in "${!EXPECTED_MANIFEST_DIGESTS[@]}"; do
    expected="${EXPECTED_MANIFEST_DIGESTS[$key]}"
    # Extract image name from key (remove -manifest suffix)
    image="${key%-manifest}"

    full_image="${REGISTRY}/${REPO}/${image}:${VERSION}"
    log_info "Checking $image..."

    # Get actual manifest list digest from registry
    if actual=$(docker buildx imagetools inspect "$full_image" --raw 2>/dev/null | \
                sha256sum | cut -d' ' -f1 | sed 's/^/sha256:/'); then
        if [[ "$actual" == "$expected" ]]; then
            log_info "  Manifest digest matches: $expected"
        else
            log_error "  Manifest digest mismatch!"
            log_error "    Expected: $expected"
            log_error "    Actual:   $actual"
            DIGEST_ERRORS=$((DIGEST_ERRORS + 1))
        fi
    else
        log_warn "  Could not fetch digest from registry"
        log_warn "  Image may not exist or you may need to authenticate"
    fi
done

if [[ $DIGEST_ERRORS -gt 0 ]]; then
    log_error "Found $DIGEST_ERRORS digest mismatches!"
    exit 1
fi

# =============================================================================
# Step 4: Optionally reproduce the build (current architecture only)
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

    # Images excluded from layer-digest verification (still built).
    # jam-ng: the jam-builder stage runs react-scripts (CRA/webpack) and the
    # resulting static bundle is currently non-deterministic across environments.
    SKIP_VERIFY=("jam-ng")

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
        skip_verify=false
        for skip in "${SKIP_VERIFY[@]}"; do
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
fi

# =============================================================================
# Summary
# =============================================================================
echo ""
echo "=============================================="
log_info "Verification Summary for $VERSION"
echo "=============================================="
if [[ "$SKIP_SIGNATURES" == true ]]; then
    echo "GPG signature verification: SKIPPED"
else
    echo "Valid GPG signatures: $VALID_SIGS"
    if [[ ${#SIGNERS[@]} -gt 0 ]]; then
        echo "Signers:"
        for signer in "${SIGNERS[@]}"; do
            echo "  - $signer"
        done
    fi
fi
echo "Digest verification: PASSED"
if [[ "$REPRODUCE" == true ]]; then
    if [[ $REPRODUCE_ERRORS -gt 0 ]]; then
        echo "Reproducibility check: FAILED ($REPRODUCE_ERRORS errors, $REPRODUCE_SUCCESS succeeded)"
    else
        echo "Reproducibility check: PASSED ($REPRODUCE_SUCCESS images reproduced)"
    fi
fi
echo ""

# Fail if reproducibility was requested and failed
if [[ "$REPRODUCE" == true && $REPRODUCE_ERRORS -gt 0 ]]; then
    log_error "Reproducibility verification failed!"
    log_error "The builds could not be reproduced locally."
    log_error "Possible causes:"
    log_error "  - Different BuildKit version than CI"
    log_error "  - Platform differences"
    log_error "  - Non-deterministic build steps in Dockerfiles"
    exit 1
fi

log_info "Release verification completed successfully!"
