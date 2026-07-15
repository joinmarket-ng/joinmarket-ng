# =============================================================================
# Global build args (must be before first FROM)
# Base image digests for reproducible builds
# Update with: ./scripts/update-base-images.sh
# =============================================================================
ARG PYTHON_VERSION=3.14
ARG PYTHON_SLIM_DIGEST=sha256:b877e50bd90de10af8d82c57a022fc2e0dc731c5320d762a27986facfc3355c1

# Pin build tools for reproducible builds
# setuptools version is stamped into WHEEL metadata (Generator: setuptools (x.y.z))
# Different versions produce different WHEEL/RECORD files, breaking layer reproducibility
# Update versions with: ./scripts/update-base-images.sh
ARG SETUPTOOLS_VERSION=83.0.0
ARG WHEEL_VERSION=0.47.0
# hatchling builds coincurve's wheel from source (no cp314 wheel upstream); its
# version is recorded in WHEEL/RECORD, so pin it for reproducible layer digests.
ARG HATCHLING_VERSION=1.30.1

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
    && rm -rf /var/log/dpkg.log /var/log/apt/*

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
# Note: the full setuptools/wheel build constraint is applied only to our own package
# install steps below. For the requirements install we apply a hatchling-only constraint
# (pinning coincurve's PEP 517 backend) instead; applying the setuptools pin here would
# conflict with packages that pin their own setuptools (e.g. httptools==0.7.1 needs
# setuptools==80.9.0 exactly and has no pre-built wheel on armv7).

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# All requirements are installed in a single pip invocation so that pip resolves the
# dependency graph once instead of re-resolving and re-verifying hashes across six
# sequential calls. BuildKit cache mount avoids re-downloading packages on rebuilds
# while keeping the image layer small (the cache lives on the host, not in the image).
COPY jmcore/requirements.txt /build/requirements-jmcore.txt
COPY jmwallet/requirements.txt /build/requirements-jmwallet.txt
COPY maker/requirements.txt /build/requirements-maker.txt
COPY taker/requirements.txt /build/requirements-taker.txt
COPY orderbook_watcher/requirements.txt /build/requirements-obwatcher.txt
COPY jmwalletd/requirements.txt /build/requirements-jmwalletd.txt

RUN --mount=type=cache,target=/root/.cache/pip,id=jmng-pip-${HATCHLING_VERSION} \
    if [ -n "$SOURCE_DATE_EPOCH" ]; then export SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH; fi && \
    pip install --build-constraint /build/coincurve-build-constraints.txt \
        -r /build/requirements-jmcore.txt \
        -r /build/requirements-jmwallet.txt \
        -r /build/requirements-maker.txt \
        -r /build/requirements-taker.txt \
        -r /build/requirements-obwatcher.txt \
        -r /build/requirements-jmwalletd.txt

# Copy source packages individually to allow layer caching per component
COPY jmcore/src /build/jmcore/src
COPY jmcore/pyproject.toml /build/jmcore/
COPY jmcore/setup.py /build/jmcore/
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
# tumbler has no external runtime deps beyond those already installed by
# jmcore/jmwallet/maker/taker (loguru, pydantic, pyyaml); no requirements.txt.
COPY tumbler/src /build/tumbler/src
COPY tumbler/pyproject.toml /build/tumbler/

# Normalize file timestamps so local working-copy mtimes don't differ from CI clone
RUN if [ -n "$SOURCE_DATE_EPOCH" ]; then \
        find /build/jmcore /build/jmwallet /build/maker /build/taker \
             /build/orderbook_watcher /build/jmwalletd /build/tumbler \
             -exec touch -d @${SOURCE_DATE_EPOCH} {} +; \
    fi

RUN if [ -n "$SOURCE_DATE_EPOCH" ]; then export SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH; fi && \
    pip install --no-cache-dir --no-deps --build-constraint /build/constraints.txt \
        /build/jmcore \
        /build/jmwallet \
        /build/maker \
        /build/taker \
        /build/orderbook_watcher \
        /build/tumbler \
        /build/jmwalletd

# =============================================================================
# Stage 2: jmwalletd standalone API daemon (default target)
# =============================================================================
FROM python:${PYTHON_VERSION}-slim@${PYTHON_SLIM_DIGEST} AS jmwalletd
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    tini=0.19.0-3+b7 \
    libsodium23=1.0.18-1+deb13u1 \
    && rm -rf /var/lib/apt/lists/* \
    && rm -rf /var/log/dpkg.log /var/log/apt/* /var/cache/ldconfig/aux-cache

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

RUN mkdir -p /root/.joinmarket-ng

ENV PYTHONUNBUFFERED=1
ENV JOINMARKET_DATA_DIR=/root/.joinmarket-ng
ENV JMWALLETD_HOST=0.0.0.0

EXPOSE 28183

ENTRYPOINT ["tini", "--"]
CMD ["jmwalletd"]
