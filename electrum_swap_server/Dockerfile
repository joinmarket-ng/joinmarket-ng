# Electrum swap server for regtest e2e testing
#
# Builds Electrum from source with the swapserver plugin enabled.
# The swap server runs as an Electrum daemon with a Lightning-enabled wallet,
# communicating with clients via Nostr DMs (kind 25582, NIP-04 encrypted).
#
# This container does NOT expose any HTTP ports externally — all swap RPCs
# go through the Nostr relay.
#
# Python 3.13: required by electrum_ecc ctypes bindings (supports 3.10-3.13)
ARG PYTHON_VERSION=3.13
ARG PYTHON_SLIM_DIGEST=sha256:8bc60ca09afaa8ea0d6d1220bde073bacfedd66a4bf8129cbdc8ef0e16c8a952

FROM python:${PYTHON_VERSION}-slim@${PYTHON_SLIM_DIGEST} AS builder

WORKDIR /build

# Install build dependencies:
# - build-essential: C compiler for native extensions
# - libffi-dev: foreign function interface (cryptography, cffi)
# - pkg-config: build configuration
# - libsecp256k1-dev: Bitcoin elliptic curve library (electrum_ecc ctypes)
# - git: clone Electrum source
# - libgmp-dev: GNU Multiple Precision (for cryptographic operations)
# - autoconf, automake, libtool: for building libsecp256k1 from source (required by electrum_ecc)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libffi-dev \
    pkg-config \
    libsecp256k1-dev \
    git \
    libgmp-dev \
    autoconf \
    automake \
    libtool \
    && rm -rf /var/lib/apt/lists/*

# Clone Electrum from source (shallow clone for speed)
RUN git clone --depth=1 https://github.com/spesmilo/electrum.git /build/electrum

# Install Electrum and its dependencies
# Note: we install without GUI deps (--no-deps first, then deps manually)
WORKDIR /build/electrum
RUN pip install --no-cache-dir --user \
    -r contrib/requirements/requirements.txt \
    && pip install --no-cache-dir --user \
    cryptography \
    pycryptodomex \
    && pip install --no-cache-dir --user --no-deps .

# ---- Runtime stage ----
FROM python:${PYTHON_VERSION}-slim@${PYTHON_SLIM_DIGEST}

# Install runtime dependencies only
RUN apt-get update && apt-get install -y --no-install-recommends \
    libsecp256k1-2 \
    curl \
    jq \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 electrum && \
    mkdir -p /home/electrum/.electrum && \
    chown -R electrum:electrum /home/electrum/.electrum

COPY --from=builder --chown=electrum:electrum /root/.local /home/electrum/.local

# Copy entrypoint script (runs as root so it can chmod /shared before switching user)
COPY entrypoint.sh /home/electrum/entrypoint.sh
RUN chmod +x /home/electrum/entrypoint.sh

# Run as root so the entrypoint can fix /shared permissions on startup.
# The entrypoint drops to the electrum user for all Electrum commands.
# This is a regtest-only test container; privilege is not a concern.
ENV PATH=/home/electrum/.local/bin:$PATH
# Electrum data directory
ENV ELECTRUM_DATA=/home/electrum/.electrum

# The swap server HTTP port (localhost only, not exposed externally)
# All external communication goes through Nostr relay
EXPOSE 5455
# Lightning Network peer listening port (BOLT-08 transport)
EXPOSE 9735

ENTRYPOINT ["/home/electrum/entrypoint.sh"]
