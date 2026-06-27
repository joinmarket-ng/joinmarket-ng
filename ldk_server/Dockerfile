# Self-contained build of LDK Server for the swap ecosystem.
#
# Clones lightningdevkit/ldk-server and builds the node + CLI. Pin LDK_SERVER_REF
# to a tag/commit for reproducibility. Use a published image instead by setting
# LDK_SERVER_IMAGE in docker-compose.yml when one is available.
FROM rust:1.85-bookworm AS builder

ARG LDK_SERVER_REF=main
WORKDIR /src

RUN apt-get update && apt-get install -y --no-install-recommends \
    git protobuf-compiler ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN git clone --depth 1 --branch "${LDK_SERVER_REF}" \
    https://github.com/lightningdevkit/ldk-server.git . \
    || git clone https://github.com/lightningdevkit/ldk-server.git . \
    && git -c advice.detachedHead=false checkout "${LDK_SERVER_REF}" || true

RUN cargo build --release --locked -p ldk-server -p ldk-server-cli

FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /src/target/release/ldk-server /usr/local/bin/ldk-server
COPY --from=builder /src/target/release/ldk-server-cli /usr/local/bin/ldk-server-cli

EXPOSE 9735 3536
ENTRYPOINT ["ldk-server"]
