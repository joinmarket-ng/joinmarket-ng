# =============================================================================
# Builder stage: Install dependencies and build wheels
# =============================================================================
# Base image digests for reproducible builds
# Update with: ./scripts/update-base-images.sh
ARG PYTHON_VERSION=3.14
ARG PYTHON_SLIM_DIGEST=sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6
ARG PYTHON_FULL_DIGEST=sha256:e06cc1111ed84189e91866447f562b89faadbfbbb9937cd67e6bf4172cdb45df
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

FROM python:${PYTHON_VERSION}-slim@${PYTHON_SLIM_DIGEST} AS builder

WORKDIR /build

# Install build dependencies (sorted alphabetically) from the pinned Debian snapshot
ARG DEBIAN_SNAPSHOT
RUN --mount=type=bind,source=scripts/docker-apt-install.sh,target=/tmp/docker-apt-install.sh \
    sh /tmp/docker-apt-install.sh \
    build-essential \
    git \
    libffi-dev \
    libsodium-dev \
    pkg-config

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

# Install jmcore and directory_server dependencies in a single pip invocation.
# --build-constraint is included because stem (in directory_server requirements) uses
# legacy setup.py/setuptools and needs its build isolation environment pinned.
COPY jmcore/requirements.txt /build/jmcore/requirements.txt
COPY directory_server/requirements.txt /build/directory_server/requirements.txt

RUN --mount=type=cache,target=/root/.cache/pip,id=jmng-pip-${SETUPTOOLS_VERSION} \
    if [ -n "$SOURCE_DATE_EPOCH" ]; then export SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH; fi && \
    pip install --prefix=/app --build-constraint /build/constraints.txt \
        -r /build/jmcore/requirements.txt \
        -r /build/directory_server/requirements.txt

# Copy all source packages and install in a single pip invocation.
COPY jmcore/src /build/jmcore/src
COPY jmcore/pyproject.toml /build/jmcore/
COPY jmcore/setup.py /build/jmcore/
COPY directory_server/src /build/directory_server/src
COPY directory_server/pyproject.toml /build/directory_server/

RUN --mount=type=cache,target=/root/.cache/pip,id=jmng-pip-${SETUPTOOLS_VERSION} \
    if [ -n "$SOURCE_DATE_EPOCH" ]; then export SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH; fi && \
    pip install --no-deps --prefix=/app --build-constraint /build/constraints.txt \
        /build/jmcore \
        /build/directory_server

# Normalize installed Python environment mtimes for reproducible COPY layer digests
RUN if [ -n "$SOURCE_DATE_EPOCH" ]; then \
        find /app -exec touch -d @${SOURCE_DATE_EPOCH} {} +; \
    fi

# =============================================================================
# Production stage: Minimal runtime image
# =============================================================================
FROM python:${PYTHON_VERSION}-slim@${PYTHON_SLIM_DIGEST} AS production

WORKDIR /app

# Install only runtime dependencies (no build tools) from the pinned Debian snapshot
# Package installers are build-only; remove pip and its vendored libraries.
ARG DEBIAN_SNAPSHOT
RUN --mount=type=bind,source=scripts/docker-apt-install.sh,target=/tmp/docker-apt-install.sh \
    sh /tmp/docker-apt-install.sh \
    libsecp256k1-2 \
    libsodium23 \
    && rm -rf \
        /usr/local/bin/pip* \
        /usr/local/lib/python${PYTHON_VERSION%.*}/ensurepip \
        /usr/local/lib/python${PYTHON_VERSION%.*}/site-packages/pip \
        /usr/local/lib/python${PYTHON_VERSION%.*}/site-packages/pip-*.dist-info

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

EXPOSE 5222 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD ["jm-directory-ctl", "health"]

CMD ["jm-directory-server"]

# =============================================================================
# Debug stage: Full Python image with debug symbols and tools
# =============================================================================
FROM python:${PYTHON_VERSION}@${PYTHON_FULL_DIGEST} AS debug-builder

WORKDIR /build

# Install build dependencies (sorted alphabetically) from the pinned Debian snapshot
ARG DEBIAN_SNAPSHOT
RUN --mount=type=bind,source=scripts/docker-apt-install.sh,target=/tmp/docker-apt-install.sh \
    sh /tmp/docker-apt-install.sh \
    build-essential \
    git \
    libffi-dev \
    libsodium-dev \
    pkg-config

# Reproducible builds: SOURCE_DATE_EPOCH must be set via --build-arg for reproducible builds
ARG SOURCE_DATE_EPOCH

# Stamp the commit/ref into the built wheels (issue #451).
ARG JOINMARKET_BUILD_COMMIT
ARG JOINMARKET_BUILD_REF
ENV JOINMARKET_BUILD_COMMIT=${JOINMARKET_BUILD_COMMIT}
ENV JOINMARKET_BUILD_REF=${JOINMARKET_BUILD_REF}

# Pin build tools for reproducible builds (same versions as main builder)
ARG SETUPTOOLS_VERSION
ARG WHEEL_VERSION
RUN echo "setuptools==${SETUPTOOLS_VERSION}" > /build/constraints.txt && \
    echo "wheel==${WHEEL_VERSION}" >> /build/constraints.txt

# Install jmcore and directory_server dependencies in a single pip invocation
# (debug-builder variant). --build-constraint is included because stem uses legacy
# setup.py/setuptools and needs its build isolation environment pinned.
COPY jmcore/requirements.txt /build/jmcore/requirements.txt
COPY directory_server/requirements.txt /build/directory_server/requirements.txt

RUN --mount=type=cache,target=/root/.cache/pip,id=jmng-pip-${SETUPTOOLS_VERSION} \
    if [ -n "$SOURCE_DATE_EPOCH" ]; then export SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH; fi && \
    pip install --prefix=/app --build-constraint /build/constraints.txt \
        -r /build/jmcore/requirements.txt \
        -r /build/directory_server/requirements.txt

# Copy all source packages and install in a single pip invocation (debug-builder variant).
COPY jmcore/src /build/jmcore/src
COPY jmcore/pyproject.toml /build/jmcore/
COPY jmcore/setup.py /build/jmcore/
COPY directory_server/src /build/directory_server/src
COPY directory_server/pyproject.toml /build/directory_server/

RUN --mount=type=cache,target=/root/.cache/pip,id=jmng-pip-${SETUPTOOLS_VERSION} \
    if [ -n "$SOURCE_DATE_EPOCH" ]; then export SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH; fi && \
    pip install --no-deps --prefix=/app --build-constraint /build/constraints.txt \
        /build/jmcore \
        /build/directory_server

# Normalize installed Python environment mtimes for reproducible COPY layer digests
RUN if [ -n "$SOURCE_DATE_EPOCH" ]; then \
        find /app -exec touch -d @${SOURCE_DATE_EPOCH} {} +; \
    fi

FROM python:${PYTHON_VERSION}@${PYTHON_FULL_DIGEST} AS debug

WORKDIR /app

# Install runtime and debug dependencies (sorted alphabetically) from the pinned
# Debian snapshot. Packages already present in the full base image (openssl,
# linux-libc-dev) are listed so they are upgraded to the snapshot's version.
# Package installers are build-only; remove pip and its vendored libraries.
ARG DEBIAN_SNAPSHOT
RUN --mount=type=bind,source=scripts/docker-apt-install.sh,target=/tmp/docker-apt-install.sh \
    sh /tmp/docker-apt-install.sh \
    gdb \
    libsecp256k1-2 \
    libsodium23 \
    libssl-dev \
    libssl3t64 \
    linux-libc-dev \
    openssl \
    openssl-provider-legacy \
    && rm -rf \
        /usr/local/bin/pip* \
        /usr/local/lib/python${PYTHON_VERSION%.*}/ensurepip \
        /usr/local/lib/python${PYTHON_VERSION%.*}/site-packages/pip \
        /usr/local/lib/python${PYTHON_VERSION%.*}/site-packages/pip-*.dist-info

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

# Copy Python packages from debug builder and set permissions
COPY --from=debug-builder /app /app
RUN chmod -R a+rX /app

# Switch to non-root user
USER jm

# Make sure scripts are in PATH
ENV PATH=/app/bin:$PATH

# Make sure packages are in Python PATH
ENV PYTHONPATH=/app/lib/python${PYTHON_VERSION%.*}/site-packages

EXPOSE 5222 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD ["jm-directory-ctl", "health"]

CMD ["jm-directory-server"]
