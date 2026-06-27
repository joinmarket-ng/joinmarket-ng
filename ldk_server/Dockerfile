# Build LDK Server with external (CoinJoin) channel-funding support for the
# JoinMarket swap ecosystem.
#
# Clones the pinned upstream lightningdevkit/ldk-server and lightningdevkit/
# ldk-node and applies the patches in ./patches/, which add an
# externally-funded channel-open flow (open_channel_external_funding /
# get_funding_output / provide_funding_outpoint) so a channel's funding output
# can come from a JoinMarket CoinJoin rather than the node's on-chain wallet.
FROM rust:1.85-bookworm AS builder

# Pinned upstream revisions the patches are generated against.
ARG LDK_SERVER_REV=8163f4fe139368613959bf4f10b19ee6a5b9b4ab
ARG LDK_NODE_REV=00dba456a10cb60172eaa74e0a8b11c5e0473dcc

WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends \
    git protobuf-compiler ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY patches/ /patches/

# ldk-node fork (../ldk-node-src is referenced by ldk-server's [patch] section).
RUN git clone https://github.com/lightningdevkit/ldk-node.git ldk-node-src \
    && cd ldk-node-src \
    && git -c advice.detachedHead=false checkout "${LDK_NODE_REV}" \
    && git apply /patches/ldk-node-external-funding.patch

# ldk-server fork.
RUN git clone https://github.com/lightningdevkit/ldk-server.git ldk-server-src \
    && cd ldk-server-src \
    && git -c advice.detachedHead=false checkout "${LDK_SERVER_REV}" \
    && git apply /patches/ldk-server-external-funding.patch

WORKDIR /build/ldk-server-src
RUN cargo build --release -p ldk-server -p ldk-server-cli

FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /build/ldk-server-src/target/release/ldk-server /usr/local/bin/ldk-server
COPY --from=builder /build/ldk-server-src/target/release/ldk-server-cli /usr/local/bin/ldk-server-cli

EXPOSE 9735 3536
ENTRYPOINT ["ldk-server"]
