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
    log_info "Phase 4: Checking Flatpak dependencies..."

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

# Collect all unique pinned packages from all Dockerfiles.
# Matches patterns like: package=version or package=epoch:version.
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
                # Escape special regex characters in version strings for sed BRE.
                # '.' -> '\.'  for literal dot.
                # '+' -> '[+]' because '\+' in GNU sed BRE is a one-or-more quantifier,
                #         not a literal '+'.  Using a bracket expression avoids that.
                escaped_current=$(printf '%s' "$current_ver" | sed 's/\./\\./g; s/+/[+]/g')
                # Replacement side: only '&', '/', '\' are special in sed replacements.
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
