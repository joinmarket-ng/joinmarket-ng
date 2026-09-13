#!/bin/sh
# =============================================================================
# Install Debian packages from a snapshot.debian.org archive state.
#
# Used by the Dockerfiles through a BuildKit bind mount, so this file is never
# part of an image layer:
#
#   ARG DEBIAN_SNAPSHOT
#   RUN --mount=type=bind,source=scripts/docker-apt-install.sh,target=/tmp/docker-apt-install.sh \
#       sh /tmp/docker-apt-install.sh <package>...
#
# Why a snapshot: Debian mirrors only serve the current version of each
# package, so builds that resolve against the live archive stop reproducing
# (or stop building, when a version is pinned) as soon as any package in the
# transitive closure is rebuilt. Resolving against a fixed snapshot timestamp
# pins the whole closure without per-package version pins. The timestamp is
# declared as DEBIAN_SNAPSHOT in every Dockerfile and advanced by
# scripts/update-base-images.sh.
#
# The rewritten sources live in a temporary directory: the resulting image
# keeps the base image's regular deb.debian.org sources. Valid-Until checks
# are disabled because snapshot Release files expire by design; the Release
# signature is still verified against the Debian archive keyring.
# =============================================================================
set -eu

SNAPSHOT_BASE_URL="${SNAPSHOT_BASE_URL:-https://snapshot.debian.org/archive}"
APT_SOURCES_FILE="${APT_SOURCES_FILE:-/etc/apt/sources.list.d/debian.sources}"

if [ -z "${DEBIAN_SNAPSHOT:-}" ]; then
    echo "docker-apt-install: DEBIAN_SNAPSHOT is not set (declare ARG DEBIAN_SNAPSHOT in this stage)" >&2
    exit 1
fi
case "$DEBIAN_SNAPSHOT" in
    [0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]T[0-9][0-9][0-9][0-9][0-9][0-9]Z) ;;
    *)
        echo "docker-apt-install: DEBIAN_SNAPSHOT must look like 20260101T000000Z, got '$DEBIAN_SNAPSHOT'" >&2
        exit 1
        ;;
esac
if [ "$#" -eq 0 ]; then
    echo "usage: docker-apt-install.sh <package>..." >&2
    exit 1
fi

work_dir=$(mktemp -d)
mkdir -p "$work_dir/sources.d"

# Point every deb.debian.org stanza at the same snapshot timestamp. Each
# archive (debian, debian-security) resolves it to its own latest snapshot at
# or before that instant.
sed \
    -e "s|^URIs: http://deb.debian.org/debian-security\$|URIs: ${SNAPSHOT_BASE_URL}/debian-security/${DEBIAN_SNAPSHOT}|" \
    -e "s|^URIs: http://deb.debian.org/debian\$|URIs: ${SNAPSHOT_BASE_URL}/debian/${DEBIAN_SNAPSHOT}|" \
    "$APT_SOURCES_FILE" > "$work_dir/snapshot.sources"

if grep -q "deb.debian.org" "$work_dir/snapshot.sources"; then
    echo "docker-apt-install: unrecognized apt sources in $APT_SOURCES_FILE:" >&2
    cat "$work_dir/snapshot.sources" >&2
    exit 1
fi

# mktemp paths contain no whitespace, so a plain string is safe in POSIX sh.
apt_opts="-o Dir::Etc::SourceList=$work_dir/snapshot.sources \
    -o Dir::Etc::SourceParts=$work_dir/sources.d \
    -o Acquire::Check-Valid-Until=false \
    -o Acquire::Retries=5"

# shellcheck disable=SC2086
apt-get $apt_opts update
# shellcheck disable=SC2086
DEBIAN_FRONTEND=noninteractive apt-get $apt_opts install -y --no-install-recommends "$@"

# Drop build-time apt state that would otherwise vary between builds.
rm -rf /var/lib/apt/lists/* /var/log/dpkg.log /var/log/apt/* /var/cache/ldconfig/aux-cache \
    "$work_dir"
