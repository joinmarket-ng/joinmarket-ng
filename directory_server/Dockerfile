# =============================================================================
# Builder stage: Install dependencies and build wheels
# =============================================================================
# Base image digests for reproducible builds
# Update with: ./scripts/update-base-images.sh
ARG PYTHON_VERSION=3.14
ARG PYTHON_SLIM_DIGEST=sha256:bc389f7dfcb21413e72a28f491985326994795e34d2b86c8ae2f417b4e7818aa
ARG PYTHON_FULL_DIGEST=sha256:3de5d55a43a58c09ed47952b15d87ea8d18d82bc616a9277b9ddd0e6dd25b3f0
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
# coincurve (the only source-build here) uses hatchling, not setuptools, so the setuptools
# constraint is not needed for this step.
COPY jmcore/requirements.txt /build/jmcore/requirements.txt
RUN if [ -n "$SOURCE_DATE_EPOCH" ]; then export SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH; fi && \
    pip install --no-cache-dir --user -r /build/jmcore/requirements.txt

# Copy jmcore source and install (with notifications support)
COPY jmcore/src /build/jmcore/src
COPY jmcore/pyproject.toml /build/jmcore/
COPY jmcore/setup.py /build/jmcore/
RUN if [ -n "$SOURCE_DATE_EPOCH" ]; then export SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH; fi && \
    pip install --no-cache-dir --user --build-constraint /build/constraints.txt "/build/jmcore"

# Install directory_server dependencies
# stem (in this requirements file) has no pre-built wheel and uses legacy setup.py/setuptools.
# --build-constraint pins the setuptools version stamped into stem's WHEEL metadata, ensuring
# the resulting layer digest is identical across builds.
COPY directory_server/requirements.txt /build/directory_server/requirements.txt
RUN if [ -n "$SOURCE_DATE_EPOCH" ]; then export SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH; fi && \
    pip install --no-cache-dir --user --build-constraint /build/constraints.txt -r /build/directory_server/requirements.txt

# Copy directory_server source and install
COPY directory_server/src /build/directory_server/src
COPY directory_server/pyproject.toml /build/directory_server/
RUN if [ -n "$SOURCE_DATE_EPOCH" ]; then export SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH; fi && \
    pip install --no-cache-dir --user --build-constraint /build/constraints.txt /build/directory_server

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

EXPOSE 5222 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD ["jm-directory-ctl", "health"]

CMD ["jm-directory-server"]

# =============================================================================
# Debug stage: Full Python image with debug symbols and tools
# =============================================================================
FROM python:${PYTHON_VERSION}@${PYTHON_FULL_DIGEST} AS debug-builder

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

# Install jmcore dependencies (with hash verification)
# coincurve (the only source-build here) uses hatchling, not setuptools, so the setuptools
# constraint is not needed for this step.
COPY jmcore/requirements.txt /build/jmcore/requirements.txt
RUN if [ -n "$SOURCE_DATE_EPOCH" ]; then export SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH; fi && \
    pip install --no-cache-dir --user -r /build/jmcore/requirements.txt

# Copy jmcore source and install (with notifications support)
# --build-constraint pins setuptools so its version is stamped identically into WHEEL metadata
# across builds. Applied only to our packages to avoid conflicting with third-party build deps.
COPY jmcore/src /build/jmcore/src
COPY jmcore/pyproject.toml /build/jmcore/
COPY jmcore/setup.py /build/jmcore/
RUN if [ -n "$SOURCE_DATE_EPOCH" ]; then export SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH; fi && \
    pip install --no-cache-dir --user --build-constraint /build/constraints.txt "/build/jmcore"

# Install directory_server dependencies
# stem (in this requirements file) has no pre-built wheel and uses legacy setup.py/setuptools.
# --build-constraint pins the setuptools version stamped into stem's WHEEL metadata.
COPY directory_server/requirements.txt /build/directory_server/requirements.txt
RUN if [ -n "$SOURCE_DATE_EPOCH" ]; then export SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH; fi && \
    pip install --no-cache-dir --user --build-constraint /build/constraints.txt -r /build/directory_server/requirements.txt

# Copy directory_server source and install
COPY directory_server/src /build/directory_server/src
COPY directory_server/pyproject.toml /build/directory_server/
RUN if [ -n "$SOURCE_DATE_EPOCH" ]; then export SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH; fi && \
    pip install --no-cache-dir --user --build-constraint /build/constraints.txt /build/directory_server

# Normalize installed Python environment mtimes for reproducible COPY layer digests
RUN if [ -n "$SOURCE_DATE_EPOCH" ]; then \
        find /root/.local -exec touch -d @${SOURCE_DATE_EPOCH} {} +; \
    fi

FROM python:${PYTHON_VERSION}@${PYTHON_FULL_DIGEST} AS debug

WORKDIR /app

# Install runtime and debug dependencies (sorted alphabetically, versions pinned for reproducibility)
# Delete apt/dpkg logs and ldconfig cache for reproducible builds
RUN apt-get update && apt-get install -y --no-install-recommends \
    gdb=16.3-1 \
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

# Copy Python packages from debug builder and set ownership
COPY --from=debug-builder --chown=jm:jm /root/.local /home/jm/.local

# Switch to non-root user
USER jm

# Make sure scripts are in PATH
ENV PATH=/home/jm/.local/bin:$PATH

EXPOSE 5222 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD ["jm-directory-ctl", "health"]

CMD ["jm-directory-server"]
