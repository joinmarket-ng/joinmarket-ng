#!/usr/bin/env bash
# Test fixture only: the pre-authorizer backend for signed-session upgrade tests.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mkdir -p "$repo_root/tmp"
context="$(mktemp -d "$repo_root/tmp/lnd-legacy-build.XXXXXX")"
trap 'rm -rf "$context"' EXIT

mkdir "$context/patches"
cp "$repo_root/lnd/Dockerfile" "$context/Dockerfile"
cp "$repo_root"/lnd/patches/0001*.patch \
    "$repo_root"/lnd/patches/0002*.patch "$context/patches/"
docker build -t "${JM_BUYOUT_LEGACY_LND_IMAGE:-jm-buyout-lnd:legacy-v1}" "$context"
