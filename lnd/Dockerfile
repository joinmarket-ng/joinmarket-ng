# Build the pinned LND release with exact-parent channel escrow support.
FROM golang:1.25.5-alpine AS builder

ARG LND_REV=848b72ce96eb68fa90fd4336523ca4c59bddcd4c

RUN apk add --no-cache alpine-sdk git make

COPY patches/ /patches/

RUN git clone https://github.com/lightningnetwork/lnd.git \
        /go/src/github.com/lightningnetwork/lnd \
    && cd /go/src/github.com/lightningnetwork/lnd \
    && git -c advice.detachedHead=false checkout "${LND_REV}" \
    && git apply /patches/0001-feat-channelescrowrpc-add-exact-parent-channel-escro.patch \
    && make release-install

FROM alpine:3.22

RUN apk add --no-cache bash ca-certificates

COPY --from=builder /go/bin/lnd /bin/lnd
COPY --from=builder /go/bin/lncli /bin/lncli

VOLUME ["/root/.lnd"]
EXPOSE 9735 10009
ENTRYPOINT ["lnd"]
