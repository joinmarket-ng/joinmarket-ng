# The final jam-standalone images still contain joinmarket-clientserver v0.9.11.
# Build the signed final clientserver release separately, then layer it onto the
# pinned Jam runtime so reference tests exercise maintained maker behavior.
ARG JAM_STANDALONE_IMAGE=ghcr.io/joinmarket-webui/jam-standalone:v0.4.0-clientserver-v0.9.11@sha256:3742e15734b91ea520ae7b78c2e6b82aed715bdcda5a786c92efe77ffd89ecc7
ARG JM_SERVER_TAG=v0.9.12
ARG JM_SERVER_COMMIT=77a426c504cbf6fa56ba0316dfd8da63a76d2bbf
FROM alpine:3.21.2@sha256:56fa17d2a7e7f168a043a2712e63aed1f8543aeafdcee47c58dcffe38ed51099 AS server-builder

ARG JM_SERVER_TAG
ARG JM_SERVER_COMMIT
ARG JM_PRIMARY_FINGERPRINT=0F1C7345D9193D1C8E3F21810C44134F93234873
ARG JM_SIGNING_FINGERPRINT=088495423873FE5EDE77062273E6558DA7CB887A

RUN apk add --no-cache curl git gnupg \
    && install -d -m 0700 /root/.gnupg

# The pinned Jam image has expired Tor signing-subkey metadata. Fetch the
# refreshed key in this stage, which does not depend on the broken APT source.
# Export only the pinned primary key, never an entire downloaded key bundle.
ARG TOR_ARCHIVE_FINGERPRINT=A3C4F0F979CAA22CDBA8F512EE8CBC9E886DDD89
RUN curl --fail --location --silent --show-error \
        "https://deb.torproject.org/torproject.org/${TOR_ARCHIVE_FINGERPRINT}.asc" \
        --output /tmp/tor-archive-keyring.asc \
    && gpg --batch --import /tmp/tor-archive-keyring.asc \
    && gpg --batch --export "${TOR_ARCHIVE_FINGERPRINT}" > /tmp/tor-archive-keyring.gpg \
    && test -s /tmp/tor-archive-keyring.gpg

WORKDIR /src
RUN git init \
    && git remote add origin https://github.com/JoinMarket-Org/joinmarket-clientserver.git \
    && git fetch --depth=1 origin "refs/tags/${JM_SERVER_TAG}:refs/tags/${JM_SERVER_TAG}" \
    && test "$(git rev-parse "${JM_SERVER_TAG}^{commit}")" = "${JM_SERVER_COMMIT}" \
    && git checkout --detach "${JM_SERVER_COMMIT}" \
    && curl --fail --location --silent --show-error \
        https://github.com/AdamISZ.gpg --output /tmp/AdamISZ.gpg \
    && gpg --batch --with-colons --import-options show-only --dry-run \
        --import /tmp/AdamISZ.gpg \
        | awk -F: '$1 == "fpr" {print $10}' \
        | grep -Fx "${JM_PRIMARY_FINGERPRINT}" \
    && gpg --batch --import /tmp/AdamISZ.gpg \
    && git verify-tag --raw "${JM_SERVER_TAG}" > /tmp/tag-verification 2>&1 \
    && grep -F "[GNUPG:] VALIDSIG ${JM_SIGNING_FINGERPRINT} " /tmp/tag-verification

FROM ${JAM_STANDALONE_IMAGE}

ARG JM_SERVER_TAG
ARG JM_SERVER_COMMIT
LABEL server-version="${JM_SERVER_TAG}" \
    server-revision="${JM_SERVER_COMMIT}"

USER root
RUN rm -rf /src
COPY --from=server-builder /src /src
# Match the signed-by path in the pinned image's /etc/apt/sources.list.d/tor.list.
COPY --from=server-builder /tmp/tor-archive-keyring.gpg /usr/share/keyrings/tor-archive-keyring.gpg

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        autoconf \
        automake \
        build-essential \
        curl \
        expect \
        gnupg \
        libffi-dev \
        libffi8 \
        libltdl-dev \
        libtool \
        pkg-config \
        python3-dev \
        python3-pip \
        python3-setuptools \
        python3-venv \
    && pip3 config set global.break-system-packages true \
    && cd /src \
    && ./install.sh --docker-install --without-qt \
    && ldconfig \
    && rm -rf install.sh deps/cache test .git .gitignore .github .coveragerc \
        joinmarket-qt.desktop \
    && apt-get purge -y --auto-remove gnupg python3-pip \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

COPY tests/e2e/reference/check_directory_onion.py /usr/local/bin/check-directory-onion
RUN chmod 755 /usr/local/bin/check-directory-onion
