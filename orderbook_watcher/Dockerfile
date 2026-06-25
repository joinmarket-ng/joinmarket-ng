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

# Install all requirements in a single pip invocation so that pip resolves the
# dependency graph once instead of re-resolving across three sequential calls.
COPY jmcore/requirements.txt /build/jmcore/
COPY jmwallet/requirements.txt /build/jmwallet/
COPY orderbook_watcher/requirements.txt /build/orderbook_watcher/

RUN --mount=type=cache,target=/root/.cache/pip \
    if [ -n "$SOURCE_DATE_EPOCH" ]; then export SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH; fi && \
    pip install --prefix=/app --build-constraint /build/coincurve-build-constraints.txt \
        -r /build/jmcore/requirements.txt \
        -r /build/jmwallet/requirements.txt \
        -r /build/orderbook_watcher/requirements.txt

# Copy and install jmcore + jmwallet source packages in a single pip invocation.
# --build-constraint pins setuptools version in PEP 517 build isolation environments
# for our packages, which declare requires=["setuptools>=68.0"].
# Note: orderbook_watcher source is copied separately below (not installed as a package).
COPY jmcore/src /build/jmcore/src
COPY jmcore/pyproject.toml /build/jmcore/
COPY jmcore/setup.py /build/jmcore/
COPY jmwallet/src /build/jmwallet/src
COPY jmwallet/pyproject.toml /build/jmwallet/

RUN --mount=type=cache,target=/root/.cache/pip \
    if [ -n "$SOURCE_DATE_EPOCH" ]; then export SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH; fi && \
    pip install --no-deps --prefix=/app --build-constraint /build/constraints.txt \
        /build/jmcore \
        /build/jmwallet

# Copy source and static files, normalize permissions and timestamps for reproducibility
# This runs as root in builder, ensuring consistent 644/755 permissions regardless of source
# Timestamps are normalized to SOURCE_DATE_EPOCH because:
# - BuildKit's rewrite-timestamp only modifies the OCI tar output, not layer content hashes
# - Layer digests are computed BEFORE rewriting, so files must have identical timestamps
# - Without this, local files (old mtime) differ from CI (fresh git clone mtime)
COPY orderbook_watcher/src /build/orderbook_watcher/src
COPY orderbook_watcher/static /build/orderbook_watcher/static
RUN find /build/orderbook_watcher/src /build/orderbook_watcher/static -type f -exec chmod 644 {} + && \
    find /build/orderbook_watcher/src /build/orderbook_watcher/static -type d -exec chmod 755 {} + && \
    if [ -n "$SOURCE_DATE_EPOCH" ]; then \
        find /build/orderbook_watcher/src /build/orderbook_watcher/static -exec touch -d @${SOURCE_DATE_EPOCH} {} +; \
    fi

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

# Copy application source and static files from builder (with normalized permissions)
COPY --from=builder --chown=jm:jm /build/orderbook_watcher/src /app/orderbook_watcher/src
COPY --from=builder --chown=jm:jm /build/orderbook_watcher/static /app/orderbook_watcher/static

# Make sure both site-packages (jmcore, jmwallet, third-party deps) and the
# orderbook_watcher source tree are importable. orderbook_watcher is shipped as
# a source tree rather than an installed package, so it needs its own entry.
ENV PYTHONPATH=/app/lib/python${PYTHON_VERSION%.*}/site-packages:/app/orderbook_watcher/src

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health').read()" || exit 1

CMD ["python", "-m", "orderbook_watcher.main"]
