# Thin wrapper around the upstream jam-standalone image that adds the
# `expect` package, which the reference-coinjoin and reference-bond-import
# tests need to drive the interactive `wallet-tool generate` prompts.
#
# Pre-installing expect here avoids a runtime `apt-get install` inside the
# jam container, which is fragile in CI (network restrictions, debian repo
# lag, etc.) and was the root cause of test-reference-legacy failures.
ARG JAM_STANDALONE_IMAGE=ghcr.io/joinmarket-webui/jam-standalone:v0.4.0-clientserver-v0.9.11
FROM ${JAM_STANDALONE_IMAGE}

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends expect \
    && rm -rf /var/lib/apt/lists/*

COPY tests/e2e/reference/check_directory_onion.py /usr/local/bin/check-directory-onion
RUN chmod 755 /usr/local/bin/check-directory-onion
