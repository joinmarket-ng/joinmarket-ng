#!/usr/bin/env bash
# =============================================================================
# Update build pinning for reproducible builds
#
# This script updates base image digests, the pinned Debian archive snapshot
# (DEBIAN_SNAPSHOT, which fixes every apt package version transitively),
# selected build-time dependency pins in Dockerfiles, JAM Docker sources, and
# Flatpak manifest dependency sources/checksums to ensure reproducible builds.
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
#   - curl
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
    log_info "Phase 4: Checking external and Flatpak dependencies..."

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
# Phase 2: Update the pinned Debian archive snapshot
#
# Dockerfiles install apt packages from snapshot.debian.org at the timestamp in
# ARG DEBIAN_SNAPSHOT (see scripts/docker-apt-install.sh), which pins the whole
# transitive package closure. The pin is advanced only when the closure that
# the Dockerfiles install actually differs at the latest snapshot, so a
# no-op run leaves the Dockerfiles untouched.
# =============================================================================
echo ""
log_info "Phase 2: Checking pinned Debian archive snapshot..."

APT_INSTALL_HELPER="$PROJECT_ROOT/scripts/docker-apt-install.sh"
SNAPSHOT_ARCHIVE_URL="https://snapshot.debian.org/archive/debian"

# The pin must be identical across Dockerfiles: the update script and the
# release scripts treat it as one value.
CURRENT_SNAPSHOT=""
for dockerfile in "${DOCKERFILES[@]}"; do
    [[ -f "$dockerfile" ]] || continue
    pinned=$(grep -oP '^ARG DEBIAN_SNAPSHOT=\K[0-9]{8}T[0-9]{6}Z' "$dockerfile" | head -1 || true)
    if [[ -z "$pinned" ]]; then
        log_error "${dockerfile#$PROJECT_ROOT/}: missing ARG DEBIAN_SNAPSHOT=<timestamp>"
        exit 1
    fi
    if [[ -n "$CURRENT_SNAPSHOT" && "$pinned" != "$CURRENT_SNAPSHOT" ]]; then
        log_error "DEBIAN_SNAPSHOT differs between Dockerfiles ($CURRENT_SNAPSHOT vs $pinned)"
        exit 1
    fi
    CURRENT_SNAPSHOT="$pinned"
done
log_info "Pinned snapshot: $CURRENT_SNAPSHOT"

# Union of the packages every docker-apt-install.sh invocation installs: the
# package names follow the helper call, one per continuation line.
mapfile -t APT_PACKAGES < <(
    awk '
        /docker-apt-install\.sh \\$/ { collecting = 1; next }
        collecting && match($0, /^[[:space:]]+[a-z0-9][a-z0-9.+-]*[[:space:]]*\\?$/) {
            name = $1; sub(/\\$/, "", name); print name; next
        }
        { collecting = 0 }
    ' "${DOCKERFILES[@]}" | sort -u
)
if [[ ${#APT_PACKAGES[@]} -eq 0 ]]; then
    log_error "No docker-apt-install.sh package lists found in Dockerfiles"
    exit 1
fi
log_info "Installed packages (${#APT_PACKAGES[@]}): ${APT_PACKAGES[*]}"

# snapshot.debian.org redirects any timestamp to the latest snapshot taken at
# or before it, so resolving "now" yields the newest existing snapshot ID.
LATEST_SNAPSHOT=$(curl -fsS -o /dev/null -w '%{redirect_url}' \
    "${SNAPSHOT_ARCHIVE_URL}/$(date -u +%Y%m%dT%H%M%SZ)/" | \
    grep -oP '/archive/debian/\K[0-9]{8}T[0-9]{6}Z' || true)
if [[ -z "$LATEST_SNAPSHOT" ]]; then
    log_error "Could not resolve the latest snapshot from $SNAPSHOT_ARCHIVE_URL"
    exit 1
fi
log_info "Latest snapshot: $LATEST_SNAPSHOT"

# Installs the Dockerfile package set from a snapshot inside the slim base image
# using the same helper the Dockerfiles run, then lists the resulting package
# versions. Base-image packages are identical on both sides, so a diff shows
# only what the snapshot change affects.
installed_packages_at_snapshot() {
    local snapshot="$1"
    docker run --rm \
        -e "DEBIAN_SNAPSHOT=$snapshot" \
        -v "$APT_INSTALL_HELPER:/tmp/docker-apt-install.sh:ro" \
        "python:${PYTHON_VERSION}-slim@${SLIM_DIGEST}" sh -c \
        'sh /tmp/docker-apt-install.sh "$@" >/dev/null 2>&1 \
            && dpkg-query -W -f "\${Package} \${Version}\n"' \
        sh "${APT_PACKAGES[@]}"
}

if [[ "$CURRENT_SNAPSHOT" == "$LATEST_SNAPSHOT" ]]; then
    log_info "Snapshot pin is already the latest snapshot"
else
    log_info "Comparing package closure at $CURRENT_SNAPSHOT and $LATEST_SNAPSHOT..."
    if ! LATEST_PACKAGES=$(installed_packages_at_snapshot "$LATEST_SNAPSHOT"); then
        log_error "Installing the Dockerfile package set from snapshot $LATEST_SNAPSHOT failed"
        exit 1
    fi

    snapshot_needs_update=false
    if ! CURRENT_PACKAGES=$(installed_packages_at_snapshot "$CURRENT_SNAPSHOT"); then
        # Typically the pinned snapshot predates the (just updated) base image
        # and an exact-version dependency can no longer be satisfied.
        log_warn "Installing the Dockerfile package set from snapshot $CURRENT_SNAPSHOT failed"
        snapshot_needs_update=true
    elif ! CLOSURE_DIFF=$(diff <(echo "$CURRENT_PACKAGES") <(echo "$LATEST_PACKAGES")); then
        log_info "Package changes between snapshots:"
        grep -E '^[<>]' <<< "$CLOSURE_DIFF" | sed 's/^</  -/; s/^>/  +/'
        snapshot_needs_update=true
    fi

    if [[ "$snapshot_needs_update" == true ]]; then
        log_info "DEBIAN_SNAPSHOT: update available"
        log_info "  Current: $CURRENT_SNAPSHOT"
        log_info "  Latest:  $LATEST_SNAPSHOT"
        UPDATES_NEEDED=$((UPDATES_NEEDED + 1))
        if [[ "$CHECK_ONLY" == false ]]; then
            for dockerfile in "${DOCKERFILES[@]}"; do
                [[ -f "$dockerfile" ]] || continue
                sed -i "s|^ARG DEBIAN_SNAPSHOT=${CURRENT_SNAPSHOT}$|ARG DEBIAN_SNAPSHOT=${LATEST_SNAPSHOT}|" "$dockerfile"
            done
            UPDATES_MADE=$((UPDATES_MADE + 1))
            log_info "DEBIAN_SNAPSHOT: Updated to $LATEST_SNAPSHOT in all Dockerfiles"
        fi
    else
        log_info "DEBIAN_SNAPSHOT: Up to date (no package changes since $CURRENT_SNAPSHOT)"
    fi
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
