# Build the pinned LND release with exact-parent channel escrow support.
#
# Base is the v0.21.3-beta release commit. Go 1.26.6 matches GO_VERSION in that
# commit's Makefile (go.mod requires at least go 1.25.13), so the build uses the
# same toolchain upstream uses for its own release binaries.
FROM golang:1.26.6-alpine AS builder

# v0.21.3-beta (annotated tag 84d2392c15355b986ca1ef565029f48d6aebce44).
ARG LND_REV=572b561bf05f03dfe6135110970c4d858c3482dc

# Force Go to use the cgo based DNS resolver so DNS queries to linked
# containers succeed.
ENV GODEBUG=netdns=cgo

RUN apk add --no-cache alpine-sdk git make

COPY patches/ /patches/

# The patches are applied to the working tree (not committed), so the version
# string reported by lnd is "v0.21.3-beta-dirty". release-install builds with
# RELEASE_TAGS, which the escrow patch extends with `channelescrowrpc`; the
# explicit -tags on the test commands keeps the subserver compiled in there too.
RUN git clone https://github.com/lightningnetwork/lnd.git \
        /go/src/github.com/lightningnetwork/lnd \
    && cd /go/src/github.com/lightningnetwork/lnd \
    && git -c advice.detachedHead=false checkout "${LND_REV}" \
    && git apply /patches/*.patch \
    && go test -tags=channelescrowrpc -count=1 ./lnrpc/channelescrowrpc/... \
    && go test -count=1 -run 'TestEscrow' ./channeldb \
    && go test -count=1 -run 'TestQuiesc' ./htlcswitch \
    && make release-install

FROM alpine:3.22

RUN apk add --no-cache bash ca-certificates

COPY --from=builder /go/bin/lnd /bin/lnd
COPY --from=builder /go/bin/lncli /bin/lncli

VOLUME ["/root/.lnd"]
EXPOSE 9735 10009
ENTRYPOINT ["lnd"]
