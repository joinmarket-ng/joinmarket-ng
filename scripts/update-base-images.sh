#!/usr/bin/env bash
# =============================================================================
# Update build pinning for reproducible builds
#
# This script updates base image digests, pinned apt package versions,
# selected build-time dependency pins in Dockerfiles, and Flatpak manifest
# dependency sources/checksums to ensure reproducible builds.
# Run this periodically to get security updates while maintaining reproducibility.
#
# Usage:
#   ./scripts/update-base-images.sh [--check]
#
# Options:
#   --check   Only check for updates, don't modify files
#
# Requirements:
#   - docker with buildx
#   - sed
#   - python3
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

CHECK_ONLY=false
if [[ "${1:-}" == "--check" ]]; then
    CHECK_ONLY=true
fi

update_flatpak_deps() {
    local check_arg=()
    if [[ "$CHECK_ONLY" == true ]]; then
        check_arg=(--check)
    fi

    local script_path="$PROJECT_ROOT/scripts/update-flatpak-deps.py"
    if [[ ! -f "$script_path" ]]; then
        log_warn "Flatpak updater script not found: $script_path"
        return
    fi

    echo ""
    log_info "Phase 6: Checking Flatpak dependencies..."

    local output
    set +e
    output=$(python3 "$script_path" "${check_arg[@]}" 2>&1)
    local exit_code=$?
    set -e

    if [[ -n "$output" ]]; then
        while IFS= read -r line; do
            [[ -z "$line" ]] && continue
            if [[ "$line" =~ ^\[(ERROR|WARN)\] ]]; then
                log_warn "$line"
            else
                log_info "$line"
            fi
        done <<< "$output"
    fi

    if [[ "$CHECK_ONLY" == true ]]; then
        if [[ $exit_code -eq 1 ]]; then
            local count
            count=$(grep -oP '\[WARN\]\s+\K[0-9]+' <<< "$output" | head -1 || echo "1")
            UPDATES_NEEDED=$((UPDATES_NEEDED + count))
        elif [[ $exit_code -ne 0 ]]; then
            log_error "Flatpak dependency check failed"
            exit $exit_code
        fi
    else
        if [[ $exit_code -eq 0 ]]; then
            local count
            count=$(grep -oP '\[INFO\]\s+Applied\s+\K[0-9]+' <<< "$output" | head -1 || echo "0")
            if [[ "$count" =~ ^[0-9]+$ ]] && [[ $count -gt 0 ]]; then
                UPDATES_MADE=$((UPDATES_MADE + count))
                UPDATES_NEEDED=$((UPDATES_NEEDED + count))
            fi
        else
            log_error "Flatpak dependency update failed"
            exit $exit_code
        fi
    fi
}

# Python version to use
PYTHON_VERSION="3.14"

# Dockerfiles to update
DOCKERFILES=(
    "$PROJECT_ROOT/directory_server/Dockerfile"
    "$PROJECT_ROOT/maker/Dockerfile"
    "$PROJECT_ROOT/taker/Dockerfile"
    "$PROJECT_ROOT/orderbook_watcher/Dockerfile"
    "$PROJECT_ROOT/jmwalletd/Dockerfile"
)

UPDATES_NEEDED=0
UPDATES_MADE=0

# =============================================================================
# Phase 1: Update base image digests
# =============================================================================
log_info "Phase 1: Checking base image digests..."

SLIM_DIGEST=$(docker buildx imagetools inspect "python:${PYTHON_VERSION}-slim" --raw 2>/dev/null | \
    sha256sum | awk '{print "sha256:" $1}')
FULL_DIGEST=$(docker buildx imagetools inspect "python:${PYTHON_VERSION}" --raw 2>/dev/null | \
    sha256sum | awk '{print "sha256:" $1}')

if [[ -z "$SLIM_DIGEST" || -z "$FULL_DIGEST" ]]; then
    log_error "Failed to fetch image digests. Make sure Docker is running."
    exit 1
fi

log_info "python:${PYTHON_VERSION}-slim digest: $SLIM_DIGEST"
log_info "python:${PYTHON_VERSION} digest: $FULL_DIGEST"

for dockerfile in "${DOCKERFILES[@]}"; do
    if [[ ! -f "$dockerfile" ]]; then
        log_warn "Dockerfile not found: $dockerfile"
        continue
    fi

    relative_path="${dockerfile#$PROJECT_ROOT/}"

    current_slim=$(grep -oP 'PYTHON_SLIM_DIGEST=\Ksha256:[a-f0-9]+' "$dockerfile" 2>/dev/null || echo "")
    current_full=$(grep -oP 'PYTHON_FULL_DIGEST=\Ksha256:[a-f0-9]+' "$dockerfile" 2>/dev/null || echo "")

    needs_update=false

    if [[ -n "$current_slim" && "$current_slim" != "$SLIM_DIGEST" ]]; then
        log_info "$relative_path: PYTHON_SLIM_DIGEST needs update"
        log_info "  Current: $current_slim"
        log_info "  New:     $SLIM_DIGEST"
        needs_update=true
        UPDATES_NEEDED=$((UPDATES_NEEDED + 1))
    fi

    if [[ -n "$current_full" && "$current_full" != "$FULL_DIGEST" ]]; then
        log_info "$relative_path: PYTHON_FULL_DIGEST needs update"
        log_info "  Current: $current_full"
        log_info "  New:     $FULL_DIGEST"
        needs_update=true
        UPDATES_NEEDED=$((UPDATES_NEEDED + 1))
    fi

    if [[ "$needs_update" == true && "$CHECK_ONLY" == false ]]; then
        if [[ -n "$current_slim" ]]; then
            sed -i "s|PYTHON_SLIM_DIGEST=sha256:[a-f0-9]*|PYTHON_SLIM_DIGEST=$SLIM_DIGEST|g" "$dockerfile"
            UPDATES_MADE=$((UPDATES_MADE + 1))
        fi
        if [[ -n "$current_full" ]]; then
            sed -i "s|PYTHON_FULL_DIGEST=sha256:[a-f0-9]*|PYTHON_FULL_DIGEST=$FULL_DIGEST|g" "$dockerfile"
            UPDATES_MADE=$((UPDATES_MADE + 1))
        fi
        log_info "$relative_path: Digests updated"
    elif [[ "$needs_update" == false ]]; then
        log_info "$relative_path: Digests up to date"
    fi
done

# =============================================================================
# Phase 2: Update pinned apt package versions
# =============================================================================
echo ""
log_info "Phase 2: Checking pinned apt package versions..."

# Collect all unique pinned packages from all Dockerfiles
# Matches patterns like: package=version or package=epoch:version
declare -A CURRENT_VERSIONS
for dockerfile in "${DOCKERFILES[@]}"; do
    [[ -f "$dockerfile" ]] || continue
    while IFS= read -r match; do
        pkg="${match%%=*}"
        ver="${match#*=}"
        # Remove trailing whitespace and backslash continuations
        ver="${ver%% *}"
        ver="${ver%\\}"
        CURRENT_VERSIONS["$pkg"]="$ver"
    done < <(grep -oP '^\s+\K[a-z][a-z0-9.+-]+=\S+' "$dockerfile" 2>/dev/null | \
        sed 's/ *\\$//' || true)
done

if [[ ${#CURRENT_VERSIONS[@]} -eq 0 ]]; then
    log_warn "No pinned apt packages found in Dockerfiles"
else
    # Build the list of packages to query
    PACKAGES=()
    for pkg in "${!CURRENT_VERSIONS[@]}"; do
        PACKAGES+=("$pkg")
    done

    log_info "Found ${#PACKAGES[@]} pinned packages: ${PACKAGES[*]}"
    log_info "Querying latest versions from python:${PYTHON_VERSION}-slim..."

    # Query latest candidate versions from the base image
    # We use the slim image since that's what production stages use
    APT_OUTPUT=$(docker run --rm "python:${PYTHON_VERSION}-slim" sh -c \
        "apt-get update -qq 2>/dev/null && apt-cache policy ${PACKAGES[*]} 2>/dev/null" 2>/dev/null)

    if [[ -z "$APT_OUTPUT" ]]; then
        log_error "Failed to query apt package versions from base image"
        exit 1
    fi

    # Parse apt-cache policy output to extract candidate versions
    declare -A LATEST_VERSIONS
    current_pkg=""
    while IFS= read -r line; do
        if [[ "$line" =~ ^([a-z][a-z0-9.+-]*): ]]; then
            current_pkg="${BASH_REMATCH[1]}"
        elif [[ "$line" =~ Candidate:\ (.+) ]]; then
            if [[ -n "$current_pkg" ]]; then
                LATEST_VERSIONS["$current_pkg"]="${BASH_REMATCH[1]}"
            fi
        fi
    done <<< "$APT_OUTPUT"

    # Compare and update versions
    for pkg in "${!CURRENT_VERSIONS[@]}"; do
        current_ver="${CURRENT_VERSIONS[$pkg]}"
        latest_ver="${LATEST_VERSIONS[$pkg]:-}"

        if [[ -z "$latest_ver" ]]; then
            log_warn "$pkg: Could not determine latest version (package may not exist)"
            continue
        fi

        if [[ "$current_ver" != "$latest_ver" ]]; then
            log_info "$pkg: version update available"
            log_info "  Current: $current_ver"
            log_info "  Latest:  $latest_ver"
            UPDATES_NEEDED=$((UPDATES_NEEDED + 1))

            if [[ "$CHECK_ONLY" == false ]]; then
                # Escape special regex characters in version strings
                escaped_current=$(printf '%s' "$current_ver" | sed 's/[.+]/\\&/g')
                escaped_latest=$(printf '%s' "$latest_ver" | sed 's/[&/\\]/\\&/g')
                for dockerfile in "${DOCKERFILES[@]}"; do
                    [[ -f "$dockerfile" ]] || continue
                    if grep -q "${pkg}=${current_ver}" "$dockerfile" 2>/dev/null; then
                        sed -i "s|${pkg}=${escaped_current}|${pkg}=${escaped_latest}|g" "$dockerfile"
                    fi
                done
                UPDATES_MADE=$((UPDATES_MADE + 1))
                log_info "$pkg: Updated to $latest_ver in all Dockerfiles"
            fi
        else
            log_info "$pkg: Up to date ($current_ver)"
        fi
    done
fi

# =============================================================================
# Phase 2b: Update node base image digest (jmwalletd jam-builder stage)
# =============================================================================
echo ""
log_info "Phase 2b: Checking node base image digest..."

NODE_VERSION="24"
NODE_SLIM_DIGEST=$(docker buildx imagetools inspect "node:${NODE_VERSION}-slim" --raw 2>/dev/null | \
    sha256sum | awk '{print "sha256:" $1}')

if [[ -z "$NODE_SLIM_DIGEST" ]]; then
    log_warn "Failed to fetch node:${NODE_VERSION}-slim digest, skipping"
else
    log_info "node:${NODE_VERSION}-slim digest: $NODE_SLIM_DIGEST"

    for dockerfile in "${DOCKERFILES[@]}"; do
        [[ -f "$dockerfile" ]] || continue
        relative_path="${dockerfile#$PROJECT_ROOT/}"
        current_node_digest=$(grep -oP 'NODE_SLIM_DIGEST=\Ksha256:[a-f0-9]+' "$dockerfile" 2>/dev/null || echo "")
        [[ -z "$current_node_digest" ]] && continue

        if [[ "$current_node_digest" != "$NODE_SLIM_DIGEST" ]]; then
            log_info "$relative_path: NODE_SLIM_DIGEST needs update"
            log_info "  Current: $current_node_digest"
            log_info "  New:     $NODE_SLIM_DIGEST"
            UPDATES_NEEDED=$((UPDATES_NEEDED + 1))
            if [[ "$CHECK_ONLY" == false ]]; then
                sed -i "s|NODE_SLIM_DIGEST=sha256:[a-f0-9]*|NODE_SLIM_DIGEST=$NODE_SLIM_DIGEST|g" "$dockerfile"
                UPDATES_MADE=$((UPDATES_MADE + 1))
                log_info "$relative_path: NODE_SLIM_DIGEST updated"
            fi
        else
            log_info "$relative_path: NODE_SLIM_DIGEST up to date"
        fi
    done
fi

# =============================================================================
# Phase 3: Update pinned Python build tool versions (setuptools, wheel)
# =============================================================================
echo ""
log_info "Phase 3: Checking pinned Python build tool versions..."

# Build tools pinned as ARGs in Dockerfiles for reproducible builds
declare -A BUILD_TOOLS=(
    ["SETUPTOOLS_VERSION"]="setuptools"
    ["WHEEL_VERSION"]="wheel"
)

for arg_name in "${!BUILD_TOOLS[@]}"; do
    pip_pkg="${BUILD_TOOLS[$arg_name]}"

    # Get current pinned version from first Dockerfile that has it
    current_ver=""
    for dockerfile in "${DOCKERFILES[@]}"; do
        [[ -f "$dockerfile" ]] || continue
        current_ver=$(grep -oP "ARG ${arg_name}=\K[0-9][0-9.]*" "$dockerfile" 2>/dev/null | head -1 || echo "")
        [[ -n "$current_ver" ]] && break
    done

    if [[ -z "$current_ver" ]]; then
        log_warn "$arg_name: Not found in any Dockerfile"
        continue
    fi

    # Query latest version from PyPI
    latest_ver=$(pip index versions "$pip_pkg" 2>/dev/null | head -1 | grep -oP '\((\K[0-9][0-9.]*)' || echo "")

    if [[ -z "$latest_ver" ]]; then
        log_warn "$pip_pkg: Could not determine latest version from PyPI"
        continue
    fi

    if [[ "$current_ver" != "$latest_ver" ]]; then
        log_info "$pip_pkg ($arg_name): version update available"
        log_info "  Current: $current_ver"
        log_info "  Latest:  $latest_ver"
        UPDATES_NEEDED=$((UPDATES_NEEDED + 1))

        if [[ "$CHECK_ONLY" == false ]]; then
            for dockerfile in "${DOCKERFILES[@]}"; do
                [[ -f "$dockerfile" ]] || continue
                if grep -q "ARG ${arg_name}=" "$dockerfile" 2>/dev/null; then
                    sed -i "s|ARG ${arg_name}=${current_ver}|ARG ${arg_name}=${latest_ver}|g" "$dockerfile"
                fi
            done
            UPDATES_MADE=$((UPDATES_MADE + 1))
            log_info "$pip_pkg: Updated to $latest_ver in all Dockerfiles"
        fi
    else
        log_info "$pip_pkg ($arg_name): Up to date ($current_ver)"
    fi
done

# =============================================================================
# Phase 4: Update s6-overlay version and checksums
# =============================================================================
echo ""
log_info "Phase 4: Checking s6-overlay version..."

JMWALLETD_DOCKERFILE="$PROJECT_ROOT/jmwalletd/Dockerfile"

if [[ -f "$JMWALLETD_DOCKERFILE" ]]; then
    current_s6_ver=$(grep -oP 'S6_OVERLAY_VERSION=\K[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+' "$JMWALLETD_DOCKERFILE" 2>/dev/null | head -1 || echo "")

    if [[ -z "$current_s6_ver" ]]; then
        log_warn "S6_OVERLAY_VERSION not found in jmwalletd/Dockerfile, skipping"
    else
        latest_s6_ver=$(curl -s https://api.github.com/repos/just-containers/s6-overlay/releases/latest \
            | python3 -c "import sys,json; print(json.load(sys.stdin)['tag_name'].lstrip('v'))" 2>/dev/null || echo "")

        if [[ -z "$latest_s6_ver" ]]; then
            log_warn "Could not determine latest s6-overlay version from GitHub API, skipping"
        elif [[ "$current_s6_ver" == "$latest_s6_ver" ]]; then
            log_info "s6-overlay: Up to date ($current_s6_ver)"
        else
            log_info "s6-overlay: version update available"
            log_info "  Current: $current_s6_ver"
            log_info "  Latest:  $latest_s6_ver"
            UPDATES_NEEDED=$((UPDATES_NEEDED + 1))

            if [[ "$CHECK_ONLY" == false ]]; then
                # Fetch checksums for the new version
                log_info "s6-overlay: Fetching checksums for v${latest_s6_ver}..."
                S6_BASE="https://github.com/just-containers/s6-overlay/releases/download/v${latest_s6_ver}"

                fetch_s6_checksum() {
                    local arch="$1"
                    curl -sfL "${S6_BASE}/s6-overlay-${arch}.tar.xz.sha256" | awk '{print $1}'
                }

                s6_noarch_sha=$(fetch_s6_checksum "noarch")
                s6_amd64_sha=$(fetch_s6_checksum "x86_64")
                s6_arm64_sha=$(fetch_s6_checksum "aarch64")
                s6_armhf_sha=$(fetch_s6_checksum "armhf")

                if [[ -z "$s6_noarch_sha" || -z "$s6_amd64_sha" || -z "$s6_arm64_sha" || -z "$s6_armhf_sha" ]]; then
                    log_error "Failed to fetch s6-overlay checksums for v${latest_s6_ver}"
                    exit 1
                fi

                # Get old checksums from current Dockerfile
                old_noarch_sha=$(grep -oP '(?<=ADD --checksum=sha256:)[a-f0-9]+' "$JMWALLETD_DOCKERFILE" | head -1 || echo "")
                old_amd64_sha=$(grep -oP 'amd64.*S6_SHA256=\K[a-f0-9]+' "$JMWALLETD_DOCKERFILE" || echo "")
                old_arm64_sha=$(grep -oP 'arm64.*S6_SHA256=\K[a-f0-9]+' "$JMWALLETD_DOCKERFILE" || echo "")
                old_armhf_sha=$(grep -oP 'arm/v7.*S6_SHA256=\K[a-f0-9]+' "$JMWALLETD_DOCKERFILE" || echo "")

                # Update version
                sed -i "s|ARG S6_OVERLAY_VERSION=${current_s6_ver}|ARG S6_OVERLAY_VERSION=${latest_s6_ver}|g" "$JMWALLETD_DOCKERFILE"

                # Update noarch ADD --checksum
                [[ -n "$old_noarch_sha" ]] && \
                    sed -i "s|ADD --checksum=sha256:${old_noarch_sha}|ADD --checksum=sha256:${s6_noarch_sha}|g" "$JMWALLETD_DOCKERFILE"

                # Update arch-specific checksums in case statement
                [[ -n "$old_amd64_sha" ]] && \
                    sed -i "s|S6_SHA256=${old_amd64_sha}|S6_SHA256=${s6_amd64_sha}|g" "$JMWALLETD_DOCKERFILE"
                # Replace arm64 SHA (appears after amd64 in the case statement)
                # Use python for context-aware replacement since same pattern structure
                if [[ -n "$old_arm64_sha" && "$old_arm64_sha" != "$old_amd64_sha" ]]; then
                    sed -i "s|S6_SHA256=${old_arm64_sha}|S6_SHA256=${s6_arm64_sha}|g" "$JMWALLETD_DOCKERFILE"
                fi
                if [[ -n "$old_armhf_sha" && "$old_armhf_sha" != "$old_amd64_sha" && "$old_armhf_sha" != "$old_arm64_sha" ]]; then
                    sed -i "s|S6_SHA256=${old_armhf_sha}|S6_SHA256=${s6_armhf_sha}|g" "$JMWALLETD_DOCKERFILE"
                fi

                UPDATES_MADE=$((UPDATES_MADE + 1))
                log_info "s6-overlay: Updated to v${latest_s6_ver} with new checksums"
            fi
        fi
    fi
else
    log_warn "jmwalletd/Dockerfile not found, skipping s6-overlay update"
fi

# =============================================================================
# Phase 5: Update JAM commit hash (v2 branch)
# =============================================================================
echo ""
log_info "Phase 5: Checking JAM commit hash (v2 branch)..."

if [[ -f "$JMWALLETD_DOCKERFILE" ]]; then
    current_jam_commit=$(grep -oP 'JAM_COMMIT=\K[a-f0-9]+' "$JMWALLETD_DOCKERFILE" 2>/dev/null | head -1 || echo "")

    if [[ -z "$current_jam_commit" ]]; then
        log_warn "JAM_COMMIT not found in jmwalletd/Dockerfile, skipping"
    else
        latest_jam_commit=$(git ls-remote https://github.com/joinmarket-webui/jam.git refs/heads/v2 2>/dev/null | awk '{print $1}' || echo "")

        if [[ -z "$latest_jam_commit" ]]; then
            log_warn "Could not fetch latest JAM v2 commit hash from GitHub, skipping"
        elif [[ "$current_jam_commit" == "$latest_jam_commit" ]]; then
            log_info "JAM v2 commit: Up to date ($current_jam_commit)"
        else
            log_info "JAM v2 commit: update available"
            log_info "  Current: $current_jam_commit"
            log_info "  Latest:  $latest_jam_commit"
            UPDATES_NEEDED=$((UPDATES_NEEDED + 1))

            if [[ "$CHECK_ONLY" == false ]]; then
                sed -i "s|JAM_COMMIT=${current_jam_commit}|JAM_COMMIT=${latest_jam_commit}|g" "$JMWALLETD_DOCKERFILE"
                UPDATES_MADE=$((UPDATES_MADE + 1))
                log_info "JAM v2 commit: Updated to $latest_jam_commit"
            fi
        fi
    fi
else
    log_warn "jmwalletd/Dockerfile not found, skipping JAM commit update"
fi

# =============================================================================
# Summary
# =============================================================================

update_flatpak_deps

echo ""
if [[ "$CHECK_ONLY" == true ]]; then
    if [[ $UPDATES_NEEDED -gt 0 ]]; then
        log_warn "$UPDATES_NEEDED update(s) available"
        log_info "Run without --check to apply updates"
        exit 1
    else
        log_info "All base images and package versions are up to date"
    fi
else
    if [[ $UPDATES_MADE -gt 0 ]]; then
        log_info "Applied $UPDATES_MADE update(s)"
        log_info "Don't forget to:"
        log_info "  1. Test the builds locally"
        log_info "  2. Commit the changes"
        log_info "  3. Create a new release"
    else
        log_info "No updates needed"
    fi
fi
