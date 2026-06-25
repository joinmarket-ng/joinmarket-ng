# =============================================================================
# Builder stage: Install dependencies and build wheels
# =============================================================================
# Base image digests for reproducible builds
# Update with: ./scripts/update-base-images.sh
ARG PYTHON_VERSION=3.14
ARG PYTHON_SLIM_DIGEST=sha256:44dd04494ee8f3b538294360e7c4b3acb87c8268e4d0a4828a6500b1eff50061
# Pin build tools for reproducible builds
# setuptools version is stamped into WHEEL metadata (Generator: setuptools (x.y.z))
# Different versions produce different WHEEL/RECORD files, breaking layer reproducibility
# Update versions with: ./scripts/update-base-images.sh
ARG SETUPTOOLS_VERSION=82.0.1
ARG WHEEL_VERSION=0.47.0
# hatchling builds coincurve's wheel from source (no cp314 wheel upstream); its
# version is recorded in WHEEL/RECORD, so pin it for reproducible layer digests.
ARG HATCHLING_VERSION=1.30.1

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
ARG HATCHLING_VERSION
RUN echo "setuptools==${SETUPTOOLS_VERSION}" > /build/constraints.txt && \
    echo "wheel==${WHEEL_VERSION}" >> /build/constraints.txt && \
    echo "hatchling==${HATCHLING_VERSION}" > /build/coincurve-build-constraints.txt

# Install jmcore dependencies (with hash verification)
# Note: a hatchling-only build constraint pins coincurve's PEP 517 backend (the only
# source-build in requirements) so its WHEEL/RECORD digests are reproducible. We do NOT
# apply the full setuptools/wheel constraint here, as that would conflict with third-party
# packages that pin their own setuptools version (e.g. httptools on armv7).
# All requirements are installed in a single pip invocation so that pip resolves the
# dependency graph once instead of re-resolving across four sequential calls.
COPY jmcore/requirements.txt /build/jmcore/requirements.txt
COPY jmwallet/requirements.txt /build/jmwallet/requirements.txt
COPY taker/requirements.txt /build/taker/requirements.txt
COPY maker/requirements.txt /build/maker/requirements.txt

RUN --mount=type=cache,target=/root/.cache/pip,id=jmng-pip-${HATCHLING_VERSION} \
    if [ -n "$SOURCE_DATE_EPOCH" ]; then export SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH; fi && \
    pip install --prefix=/app --build-constraint /build/coincurve-build-constraints.txt \
        -r /build/jmcore/requirements.txt \
        -r /build/jmwallet/requirements.txt \
        -r /build/taker/requirements.txt \
        -r /build/maker/requirements.txt

# Copy all source packages and install in a single pip invocation.
# --build-constraint pins setuptools version in PEP 517 build isolation environments
# for our packages, which declare requires=["setuptools>=68.0"].
COPY jmcore/src /build/jmcore/src
COPY jmcore/pyproject.toml /build/jmcore/
COPY jmcore/setup.py /build/jmcore/
COPY jmwallet/src /build/jmwallet/src
COPY jmwallet/pyproject.toml /build/jmwallet/
COPY taker/src /build/taker/src
COPY taker/pyproject.toml /build/taker/
COPY maker/src /build/maker/src
COPY maker/pyproject.toml /build/maker/
COPY tumbler/src /build/tumbler/src
COPY tumbler/pyproject.toml /build/tumbler/

RUN --mount=type=cache,target=/root/.cache/pip,id=jmng-pip-${HATCHLING_VERSION} \
    if [ -n "$SOURCE_DATE_EPOCH" ]; then export SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH; fi && \
    pip install --no-deps --prefix=/app --build-constraint /build/constraints.txt \
        /build/jmcore \
        /build/jmwallet \
        /build/taker \
        /build/maker \
        /build/tumbler

# Normalize installed Python environment mtimes for reproducible COPY layer digests
RUN if [ -n "$SOURCE_DATE_EPOCH" ]; then \
        find /app -exec touch -d @${SOURCE_DATE_EPOCH} {} +; \
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
    whiptail=0.52.25-1 \
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

# Copy Python packages from builder and set permissions
COPY --from=builder /app /app
RUN chmod -R a+rX /app

# Switch to non-root user
USER jm

# Make sure scripts are in PATH
ENV PATH=/app/bin:$PATH

# Make sure packages are in Python PATH
ENV PYTHONPATH=/app/lib/python${PYTHON_VERSION%.*}/site-packages

# Create data directory for JoinMarket files
# This includes commitment blacklist, history, and other persistent data
RUN mkdir -p /home/jm/.joinmarket-ng && chmod 700 /home/jm/.joinmarket-ng

# Declare volume for persistent data
VOLUME /home/jm/.joinmarket-ng

# Data directory for JoinMarket files (commitment blacklist, history)
# Users can override this with -e JOINMARKET_DATA_DIR=/custom/path
ENV JOINMARKET_DATA_DIR=/home/jm/.joinmarket-ng

CMD ["jm-taker", "--help"]
