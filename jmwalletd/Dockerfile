# =============================================================================
# Global build args (must be before first FROM)
# Base image digests for reproducible builds
# Update with: ./scripts/update-base-images.sh
# =============================================================================
ARG PYTHON_VERSION=3.14
ARG PYTHON_SLIM_DIGEST=sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6

# Pin build tools for reproducible builds
# setuptools version is stamped into WHEEL metadata (Generator: setuptools (x.y.z))
# Different versions produce different WHEEL/RECORD files, breaking layer reproducibility
# Update versions with: ./scripts/update-base-images.sh
ARG SETUPTOOLS_VERSION=84.0.0
ARG WHEEL_VERSION=0.48.0

# Debian archive snapshot (snapshot.debian.org) that apt resolves against.
# Pins the whole transitive package closure so the build keeps reproducing
# after the live archive rotates. See scripts/docker-apt-install.sh.
# Update with: ./scripts/update-base-images.sh
ARG DEBIAN_SNAPSHOT=20260913T082142Z

# =============================================================================
# Stage 1: Build Python environment
# =============================================================================
FROM python:${PYTHON_VERSION}-slim@${PYTHON_SLIM_DIGEST} AS builder
WORKDIR /build

# Build dependencies for Python packages without platform wheels.
ARG DEBIAN_SNAPSHOT
RUN --mount=type=bind,source=scripts/docker-apt-install.sh,target=/tmp/docker-apt-install.sh \
    sh /tmp/docker-apt-install.sh \
    build-essential \
    cmake \
    libffi-dev \
    libsodium-dev \
    pkg-config

# Reproducible builds: SOURCE_DATE_EPOCH must be set via --build-arg for reproducible builds
# When not provided, pip install runs without it (non-reproducible but works)
# Usage: docker build --build-arg SOURCE_DATE_EPOCH=$(git log -1 --format=%ct) ...
ARG SOURCE_DATE_EPOCH

# Keep the MuSig2-capable native library inside the copied runtime environment.
RUN --mount=type=bind,source=scripts/build_secp256k1.py,target=/tmp/build_secp256k1.py \
    python /tmp/build_secp256k1.py --prefix /opt/venv --build-dir /build/secp256k1

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
# Note: the full setuptools/wheel build constraint is applied only to our own package
# install steps below. Applying the setuptools pin to third-party requirements would conflict
# with packages that pin their own setuptools (for example, httptools on armv7).

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

RUN --mount=type=cache,target=/root/.cache/pip,id=jmng-pip-${SETUPTOOLS_VERSION} \
    if [ -n "$SOURCE_DATE_EPOCH" ]; then export SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH; fi && \
    pip install \
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

# Static bash completions shipped in the runtime image (normalized below too).
COPY completions/*.bash completions/jm-completions-loader.sh /build/completions/

# Normalize file timestamps so local working-copy mtimes don't differ from CI clone
RUN if [ -n "$SOURCE_DATE_EPOCH" ]; then \
        find /build/jmcore /build/jmwallet /build/maker /build/taker \
             /build/orderbook_watcher /build/jmwalletd /build/tumbler \
             /build/completions \
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
        /build/jmwalletd \
    && rm -rf \
        /opt/venv/bin/pip* \
        /opt/venv/lib/python${PYTHON_VERSION%.*}/site-packages/pip \
        /opt/venv/lib/python${PYTHON_VERSION%.*}/site-packages/pip-*.dist-info

# =============================================================================
# Stage 2: jmwalletd standalone API daemon (default target)
# =============================================================================
FROM python:${PYTHON_VERSION}-slim@${PYTHON_SLIM_DIGEST} AS jmwalletd
WORKDIR /app

# Package installers are build-only; remove pip and its vendored libraries.
ARG DEBIAN_SNAPSHOT
RUN --mount=type=bind,source=scripts/docker-apt-install.sh,target=/tmp/docker-apt-install.sh \
    sh /tmp/docker-apt-install.sh \
    libsecp256k1-2 \
    libsodium23 \
    tini \
    && rm -rf \
        /usr/local/bin/pip* \
        /usr/local/lib/python${PYTHON_VERSION%.*}/ensurepip \
        /usr/local/lib/python${PYTHON_VERSION%.*}/site-packages/pip \
        /usr/local/lib/python${PYTHON_VERSION%.*}/site-packages/pip-*.dist-info

COPY --from=builder /opt/venv /opt/venv
RUN printf '/opt/venv/lib\n' > /etc/ld.so.conf.d/jmng-secp256k1.conf && ldconfig
ENV PATH="/opt/venv/bin:$PATH"
RUN python -c 'from bitcointx.core.secp256k1 import get_secp256k1; assert get_secp256k1().cap.has_musig'

# Reproducible builds: SOURCE_DATE_EPOCH for consistent timestamps
ARG SOURCE_DATE_EPOCH

# Enable tab completion for the bundled CLIs in interactive shells
# (docker compose exec jmwalletd bash). The bash-completion package is not
# installed, so /etc/bash.bashrc sources the generated scripts directly.
COPY --from=builder /build/completions /etc/bash_completion.d
RUN echo '. /etc/bash_completion.d/jm-completions-loader.sh' >> /etc/bash.bashrc && \
    if [ -n "$SOURCE_DATE_EPOCH" ]; then \
        touch -d @${SOURCE_DATE_EPOCH} /etc/bash.bashrc; \
    fi

RUN mkdir -p /root/.joinmarket-ng

ENV PYTHONUNBUFFERED=1
ENV JOINMARKET_DATA_DIR=/root/.joinmarket-ng
ENV JMWALLETD_HOST=0.0.0.0

EXPOSE 28183

ENTRYPOINT ["tini", "--"]
CMD ["jmwalletd"]
