# =============================================================================
# Builder stage: Install dependencies and build wheels
# =============================================================================
# Base image digests for reproducible builds
# Update with: ./scripts/update-base-images.sh
ARG PYTHON_VERSION=3.14
ARG PYTHON_SLIM_DIGEST=sha256:bc389f7dfcb21413e72a28f491985326994795e34d2b86c8ae2f417b4e7818aa
# Pin build tools for reproducible builds
# setuptools version is stamped into WHEEL metadata (Generator: setuptools (x.y.z))
# Different versions produce different WHEEL/RECORD files, breaking layer reproducibility
# Update versions with: ./scripts/update-base-images.sh
ARG SETUPTOOLS_VERSION=82.0.1
ARG WHEEL_VERSION=0.46.3

FROM python:${PYTHON_VERSION}-slim@${PYTHON_SLIM_DIGEST} AS builder

WORKDIR /build

# Install build dependencies (sorted alphabetically, versions pinned for reproducibility)
# Update versions with: ./scripts/update-base-images.sh
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential=12.12 \
    git=1:2.47.3-0+deb13u1 \
    libffi-dev=3.4.8-2 \
    libsodium-dev=1.0.18-1+deb13u1 \
    pkg-config=1.8.1-4 \
    && rm -rf /var/lib/apt/lists/*

# Reproducible builds: SOURCE_DATE_EPOCH must be set via --build-arg for reproducible builds
# When not provided, pip install runs without it (non-reproducible but works)
# Usage: docker build --build-arg SOURCE_DATE_EPOCH=$(git log -1 --format=%ct) ...
ARG SOURCE_DATE_EPOCH

# Stamp the commit/ref into the built wheels (issue #451).
ARG JOINMARKET_BUILD_COMMIT
ARG JOINMARKET_BUILD_REF
ENV JOINMARKET_BUILD_COMMIT=${JOINMARKET_BUILD_COMMIT}
ENV JOINMARKET_BUILD_REF=${JOINMARKET_BUILD_REF}

# Pin build tools for reproducible builds (re-declare to inherit global ARGs)
ARG SETUPTOOLS_VERSION
ARG WHEEL_VERSION
RUN echo "setuptools==${SETUPTOOLS_VERSION}" > /build/constraints.txt && \
    echo "wheel==${WHEEL_VERSION}" >> /build/constraints.txt

# Install jmcore dependencies (with hash verification)
# Note: --build-constraint is NOT used here; it is only needed for our own packages below.
# coincurve (the only source-build in requirements) uses hatchling, not setuptools, so our
# setuptools constraint does not apply. Using --build-constraint here would conflict with
# third-party packages that pin their own exact setuptools version (e.g. httptools on armv7).
COPY jmcore/requirements.txt /build/jmcore/requirements.txt
RUN if [ -n "$SOURCE_DATE_EPOCH" ]; then export SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH; fi && \
    pip install --no-cache-dir --user -r /build/jmcore/requirements.txt

# Copy jmcore source and install (with notifications support)
# --build-constraint pins setuptools version in PEP 517 build isolation environments for our
# packages, which declare requires=["setuptools>=68.0"] and thus accept the latest compatible
# version. Without pinning, pip downloads the latest setuptools, stamping its version into
# WHEEL metadata (Generator: setuptools x.y.z) and breaking layer reproducibility.
COPY jmcore/src /build/jmcore/src
COPY jmcore/pyproject.toml /build/jmcore/
COPY jmcore/setup.py /build/jmcore/
RUN if [ -n "$SOURCE_DATE_EPOCH" ]; then export SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH; fi && \
    pip install --no-cache-dir --user --build-constraint /build/constraints.txt "/build/jmcore"

# Install jmwallet dependencies
COPY jmwallet/requirements.txt /build/jmwallet/requirements.txt
RUN if [ -n "$SOURCE_DATE_EPOCH" ]; then export SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH; fi && \
    pip install --no-cache-dir --user -r /build/jmwallet/requirements.txt

# Copy jmwallet source and install
COPY jmwallet/pyproject.toml /build/jmwallet/pyproject.toml
COPY jmwallet/src /build/jmwallet/src
RUN if [ -n "$SOURCE_DATE_EPOCH" ]; then export SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH; fi && \
    pip install --no-cache-dir --user --build-constraint /build/constraints.txt /build/jmwallet

# Install taker dependencies
COPY taker/requirements.txt /build/taker/requirements.txt
RUN if [ -n "$SOURCE_DATE_EPOCH" ]; then export SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH; fi && \
    pip install --no-cache-dir --user -r /build/taker/requirements.txt

# Copy taker source and install
COPY taker/src /build/taker/src
COPY taker/pyproject.toml /build/taker/
RUN if [ -n "$SOURCE_DATE_EPOCH" ]; then export SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH; fi && \
    pip install --no-cache-dir --user --build-constraint /build/constraints.txt /build/taker

# Copy maker source and install (required by jm-tumbler)
COPY maker/requirements.txt /build/maker/requirements.txt
RUN if [ -n "$SOURCE_DATE_EPOCH" ]; then export SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH; fi && \
    pip install --no-cache-dir --user -r /build/maker/requirements.txt

COPY maker/src /build/maker/src
COPY maker/pyproject.toml /build/maker/
RUN if [ -n "$SOURCE_DATE_EPOCH" ]; then export SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH; fi && \
    pip install --no-cache-dir --user --build-constraint /build/constraints.txt /build/maker

# Copy tumbler source and install so the taker image also provides jm-tumbler
COPY tumbler/src /build/tumbler/src
COPY tumbler/pyproject.toml /build/tumbler/
RUN if [ -n "$SOURCE_DATE_EPOCH" ]; then export SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH; fi && \
    pip install --no-cache-dir --user --build-constraint /build/constraints.txt /build/tumbler

# Normalize installed Python environment mtimes for reproducible COPY layer digests
RUN if [ -n "$SOURCE_DATE_EPOCH" ]; then \
        find /root/.local -exec touch -d @${SOURCE_DATE_EPOCH} {} +; \
    fi

# =============================================================================
# Production stage: Minimal runtime image
# =============================================================================
FROM python:${PYTHON_VERSION}-slim@${PYTHON_SLIM_DIGEST} AS production

WORKDIR /app

# Install only runtime dependencies (no build tools)
# Versions pinned for reproducible builds
# Delete apt/dpkg logs and ldconfig cache for reproducible builds
RUN apt-get update && apt-get install -y --no-install-recommends \
    libsodium23=1.0.18-1+deb13u1 \
    && rm -rf /var/lib/apt/lists/* \
    && rm -rf /var/log/dpkg.log /var/log/apt/* \
    && rm -rf /var/cache/ldconfig/aux-cache

# Reproducible builds: SOURCE_DATE_EPOCH for consistent timestamps
ARG SOURCE_DATE_EPOCH

# Create non-root user with reproducible shadow entry
# useradd sets "last password change" to current day, breaking reproducibility
# We fix it by calculating days since epoch from SOURCE_DATE_EPOCH
RUN useradd -m -u 1000 jm && \
    if [ -n "$SOURCE_DATE_EPOCH" ]; then \
        DAYS_SINCE_EPOCH=$((SOURCE_DATE_EPOCH / 86400)) && \
        sed -i "s/^jm:!:[0-9]*:/jm:!:${DAYS_SINCE_EPOCH}:/" /etc/shadow; \
    fi

# Copy Python packages from builder and set ownership
COPY --from=builder --chown=jm:jm /root/.local /home/jm/.local

# Switch to non-root user
USER jm

# Make sure scripts are in PATH
ENV PATH=/home/jm/.local/bin:$PATH

# Create data directory for JoinMarket files
# This includes commitment blacklist, history, and other persistent data
RUN mkdir -p /home/jm/.joinmarket-ng && chmod 700 /home/jm/.joinmarket-ng

# Declare volume for persistent data
VOLUME /home/jm/.joinmarket-ng

# Data directory for JoinMarket files (commitment blacklist, history)
# Users can override this with -e JOINMARKET_DATA_DIR=/custom/path
ENV JOINMARKET_DATA_DIR=/home/jm/.joinmarket-ng

CMD ["jm-taker", "--help"]
