# Mock swap provider for regtest testing
# Lightweight Python image with FastAPI
ARG PYTHON_VERSION=3.14
ARG PYTHON_SLIM_DIGEST=sha256:6a27522252aef8432841f224d9baaa6e9fce07b07584154fa0b9a96603af7456
# Pin build tools for reproducible builds
ARG SETUPTOOLS_VERSION=82.0.0
ARG WHEEL_VERSION=0.46.3

FROM python:${PYTHON_VERSION}-slim@${PYTHON_SLIM_DIGEST} AS builder

WORKDIR /build

# Install build dependencies for coincurve (C extension)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential=12.12 \
    libffi-dev=3.4.8-2 \
    pkg-config=1.8.1-4 \
    && rm -rf /var/lib/apt/lists/*

ARG SETUPTOOLS_VERSION
ARG WHEEL_VERSION
RUN echo "setuptools==${SETUPTOOLS_VERSION}" > /build/constraints.txt && \
    echo "wheel==${WHEEL_VERSION}" >> /build/constraints.txt

COPY mock_swap_provider/pyproject.toml /build/mock_swap_provider/pyproject.toml
COPY mock_swap_provider/src /build/mock_swap_provider/src
RUN pip install --no-cache-dir --user /build/mock_swap_provider

FROM python:${PYTHON_VERSION}-slim@${PYTHON_SLIM_DIGEST}

WORKDIR /app

RUN useradd -m -u 1000 swap

COPY --from=builder --chown=swap:swap /root/.local /home/swap/.local

USER swap

ENV PATH=/home/swap/.local/bin:$PATH

EXPOSE 9999

HEALTHCHECK --interval=10s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:9999/health')"]

CMD ["mock-swap-provider"]
