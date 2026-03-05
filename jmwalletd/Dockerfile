# =============================================================================
# Global build args (must be before first FROM)
# Base image digests for reproducible builds
# Update with: ./scripts/update-base-images.sh
# =============================================================================
ARG PYTHON_VERSION=3.14
ARG PYTHON_SLIM_DIGEST=sha256:6a27522252aef8432841f224d9baaa6e9fce07b07584154fa0b9a96603af7456
ARG NODE_VERSION=24
ARG NODE_SLIM_DIGEST=sha256:e8e2e91b1378f83c5b2dd15f0247f34110e2fe895f6ca7719dbb780f929368eb

# JAM frontend: pinned commit hash for reproducible builds
# Update with: ./scripts/update-base-images.sh
ARG JAM_COMMIT=d5dd2add951db5d0807d4c0730dcdd2d9adfad75

# Pin build tools for reproducible builds
# setuptools version is stamped into WHEEL metadata (Generator: setuptools (x.y.z))
# Different versions produce different WHEEL/RECORD files, breaking layer reproducibility
# Update versions with: ./scripts/update-base-images.sh
ARG SETUPTOOLS_VERSION=82.0.0
ARG WHEEL_VERSION=0.46.3

# =============================================================================
# Stage 1: Build Python environment
# =============================================================================
FROM python:${PYTHON_VERSION}-slim@${PYTHON_SLIM_DIGEST} AS builder
WORKDIR /build

# build-essential + cmake + pkg-config needed to compile coincurve (no wheels for 3.14 yet)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential=12.12 \
    cmake=3.31.6-2 \
    pkg-config=1.8.1-4 \
    libffi-dev=3.4.8-2 \
    libsodium-dev=1.0.18-1+deb13u1 \
    && rm -rf /var/lib/apt/lists/* \
    && rm -f /var/log/dpkg.log /var/log/apt/*.log

# Reproducible builds: SOURCE_DATE_EPOCH must be set via --build-arg for reproducible builds
# When not provided, pip install runs without it (non-reproducible but works)
# Usage: docker build --build-arg SOURCE_DATE_EPOCH=$(git log -1 --format=%ct) ...
ARG SOURCE_DATE_EPOCH

# Pin build tools for reproducible builds (re-declare to inherit global ARGs)
ARG SETUPTOOLS_VERSION
ARG WHEEL_VERSION
RUN echo "setuptools==${SETUPTOOLS_VERSION}" > /build/constraints.txt && \
    echo "wheel==${WHEEL_VERSION}" >> /build/constraints.txt
# NOTE: We do NOT set PIP_CONSTRAINT as an environment variable here.
# PIP_CONSTRAINT is inherited by pip subprocess invocations (e.g. build-dep resolution
# when compiling source packages like httptools on armv7 where no wheel exists). This
# causes a conflict when the package's own build-deps request a different setuptools
# version. Instead we pass --constraint explicitly only on our own installs.

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY jmcore/requirements.txt /build/requirements-jmcore.txt
RUN if [ -n "$SOURCE_DATE_EPOCH" ]; then export SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH; fi && \
    pip install --no-cache-dir --constraint /build/constraints.txt -r /build/requirements-jmcore.txt

COPY jmwallet/requirements.txt /build/requirements-jmwallet.txt
RUN if [ -n "$SOURCE_DATE_EPOCH" ]; then export SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH; fi && \
    pip install --no-cache-dir --constraint /build/constraints.txt -r /build/requirements-jmwallet.txt

COPY maker/requirements.txt /build/requirements-maker.txt
RUN if [ -n "$SOURCE_DATE_EPOCH" ]; then export SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH; fi && \
    pip install --no-cache-dir --constraint /build/constraints.txt -r /build/requirements-maker.txt

COPY taker/requirements.txt /build/requirements-taker.txt
RUN if [ -n "$SOURCE_DATE_EPOCH" ]; then export SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH; fi && \
    pip install --no-cache-dir --constraint /build/constraints.txt -r /build/requirements-taker.txt

COPY orderbook_watcher/requirements.txt /build/requirements-obwatcher.txt
RUN if [ -n "$SOURCE_DATE_EPOCH" ]; then export SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH; fi && \
    pip install --no-cache-dir --constraint /build/constraints.txt -r /build/requirements-obwatcher.txt

COPY jmwalletd/requirements.txt /build/requirements-jmwalletd.txt
RUN if [ -n "$SOURCE_DATE_EPOCH" ]; then export SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH; fi && \
    pip install --no-cache-dir --constraint /build/constraints.txt -r /build/requirements-jmwalletd.txt

# Copy source packages individually to allow layer caching per component
COPY jmcore/src /build/jmcore/src
COPY jmcore/pyproject.toml /build/jmcore/
COPY jmwallet/src /build/jmwallet/src
COPY jmwallet/pyproject.toml /build/jmwallet/
COPY maker/src /build/maker/src
COPY maker/pyproject.toml /build/maker/
COPY taker/src /build/taker/src
COPY taker/pyproject.toml /build/taker/
COPY orderbook_watcher/src /build/orderbook_watcher/src
COPY orderbook_watcher/pyproject.toml /build/orderbook_watcher/
COPY jmwalletd/src /build/jmwalletd/src
COPY jmwalletd/pyproject.toml /build/jmwalletd/

# Normalize file timestamps so local working-copy mtimes don't differ from CI clone
RUN if [ -n "$SOURCE_DATE_EPOCH" ]; then \
        find /build/jmcore /build/jmwallet /build/maker /build/taker \
             /build/orderbook_watcher /build/jmwalletd \
             -exec touch -d @${SOURCE_DATE_EPOCH} {} +; \
    fi

RUN if [ -n "$SOURCE_DATE_EPOCH" ]; then export SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH; fi && \
    pip install --no-cache-dir --no-deps --constraint /build/constraints.txt \
        /build/jmcore \
        /build/jmwallet \
        /build/maker \
        /build/taker \
        /build/orderbook_watcher \
        /build/jmwalletd

# =============================================================================
# Stage 2: jmwalletd — standalone API daemon (default target)
# =============================================================================
FROM python:${PYTHON_VERSION}-slim@${PYTHON_SLIM_DIGEST} AS jmwalletd
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    tini=0.19.0-3+b5 \
    libsodium23=1.0.18-1+deb13u1 \
    && rm -rf /var/lib/apt/lists/* \
    && rm -f /var/log/dpkg.log /var/log/apt/*.log /var/cache/ldconfig/aux-cache

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

RUN mkdir -p /root/.joinmarket-ng

ENV PYTHONUNBUFFERED=1
ENV JOINMARKET_DATA_DIR=/root/.joinmarket-ng
ENV JMWALLETD_HOST=0.0.0.0

EXPOSE 28183

ENTRYPOINT ["tini", "--"]
CMD ["jmwalletd", "--no-tls"]

# =============================================================================
# Stage 3: Build JAM frontend (cloned from GitHub, no submodule required)
# Always built for amd64: the output is static JS that runs in the browser,
# so the build platform doesn't matter. node:24-slim has no linux/arm/v7 image.
# Pinned to a specific commit for reproducible builds.
# Update JAM_COMMIT with: ./scripts/update-base-images.sh
#
# Reproducibility notes for this stage:
# - webpack 5 (used by CRA/react-scripts) hashes outputs using [contenthash],
#   which is derived purely from file content, not timestamps.
# - GENERATE_SOURCEMAP=false eliminates source map files from the output, which
#   removes any risk from path or metadata variance in source map generation.
# - git clone sets all file mtimes to the current wall-clock time. We normalize
#   them to SOURCE_DATE_EPOCH before building so babel-loader cache keys are
#   deterministic across builds (even though cache misses don't affect output
#   content, identical cache keys prevent any mtime-driven divergence).
# - npm ci is deterministic from a pinned package-lock.json with integrity hashes.
# =============================================================================
FROM --platform=linux/amd64 node:${NODE_VERSION}-slim@${NODE_SLIM_DIGEST} AS jam-builder
ARG JAM_COMMIT
ARG SOURCE_DATE_EPOCH

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates=20230311+deb12u1 \
    git=1:2.39.5-0+deb12u3 \
    && rm -rf /var/lib/apt/lists/*

RUN git clone https://github.com/joinmarket-webui/jam.git /app \
    && git -C /app checkout "${JAM_COMMIT}" \
    && if [ -n "$SOURCE_DATE_EPOCH" ]; then \
        find /app -exec touch -d "@${SOURCE_DATE_EPOCH}" {} +; \
    fi

WORKDIR /app
# GENERATE_SOURCEMAP=false: skip source map generation for smaller output and to
# avoid any path/metadata variance in source map content across build environments.
RUN npm ci && GENERATE_SOURCEMAP=false npm run build

# =============================================================================
# Stage 4: jam-ng — self-contained: jmwalletd + JAM frontend + Tor + obwatcher
# Managed by s6-overlay for multi-process supervision
# =============================================================================
FROM jmwalletd AS jam-ng

# s6-overlay version (pinned for reproducibility)
# Update with: ./scripts/update-base-images.sh
ARG S6_OVERLAY_VERSION=3.2.2.0

# Install xz-utils (for extracting s6-overlay), wget (for downloading s6-overlay), and Tor daemon
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget=1.25.0-2 \
    xz-utils=5.8.1-1 \
    tor=0.4.8.16-1 \
    && rm -rf /var/lib/apt/lists/* \
    && rm -f /var/log/dpkg.log /var/log/apt/*.log /var/cache/ldconfig/aux-cache

# Install s6-overlay (noarch + arch-specific, checksums pinned)
# noarch tarball is the same for all platforms
ADD --checksum=sha256:85848f6baab49fb7832a5557644c73c066899ed458dd1601035cf18e7c759f26 \
    https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-noarch.tar.xz /tmp
# Arch-specific tarball: select based on TARGETARCH/TARGETVARIANT at build time
ARG TARGETARCH
ARG TARGETVARIANT
RUN set -eux; \
    case "${TARGETARCH}/${TARGETVARIANT:-}" in \
        amd64/)   S6_ARCH=x86_64;  S6_SHA256=5a09e2f1878dc5f7f0211dd7bafed3eee1afe4f813e872fff2ab1957f266c7c0 ;; \
        arm64/)   S6_ARCH=aarch64; S6_SHA256=50a5d4919e688fafc95ce9cf0055a46f74847517bcf08174bac811de234ec7d2 ;; \
        arm/v7)   S6_ARCH=armhf;   S6_SHA256=c6a9fa7c41c80d4a285cd5e1fc80ed4bc44b318ac16febffb0788a949aef35d4 ;; \
        *) echo "Unsupported platform: ${TARGETARCH}/${TARGETVARIANT:-}" >&2; exit 1 ;; \
    esac; \
    wget -qO /tmp/s6-overlay-arch.tar.xz \
        "https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-${S6_ARCH}.tar.xz"; \
    echo "${S6_SHA256}  /tmp/s6-overlay-arch.tar.xz" | sha256sum -c -; \
    tar -C / -Jxpf /tmp/s6-overlay-noarch.tar.xz; \
    tar -C / -Jxpf /tmp/s6-overlay-arch.tar.xz; \
    rm -f /tmp/s6-overlay-*.tar.xz

# Copy s6 service definitions and torrc
COPY jmwalletd/rootfs/ /

# Ensure run scripts are executable
RUN chmod +x /etc/s6-overlay/s6-rc.d/tor/run \
    && chmod +x /etc/s6-overlay/s6-rc.d/tor-ready/up \
    && chmod +x /etc/s6-overlay/s6-rc.d/obwatcher/run \
    && chmod +x /etc/s6-overlay/s6-rc.d/jmwalletd-svc/run

# Tor data directory (writable at runtime)
RUN mkdir -p /var/lib/tor && chown root:root /var/lib/tor && chmod 700 /var/lib/tor

# Copy JAM frontend
COPY --from=jam-builder /app/dist /usr/share/jmwalletd/jam

# Default env: point services at the local Tor and obwatcher
ENV TOR__SOCKS_HOST=127.0.0.1
ENV TOR__SOCKS_PORT=9050
ENV TOR__CONTROL_HOST=127.0.0.1
ENV TOR__CONTROL_PORT=9051
ENV OBWATCH_URL=http://127.0.0.1:8000

# s6-overlay init replaces tini as PID 1
ENTRYPOINT ["/init"]
# No CMD — s6-overlay starts all services via s6-rc
