FROM jam_ng_base

ARG JM_NG_REVISION=local
LABEL server-version="${JM_NG_REVISION}"

# Replace the base image's remote checkout with packages from the current
# build context so CI and local runs exercise the exact working tree.
COPY jmcore /build/jmcore
COPY jmwallet /build/jmwallet
COPY maker /build/maker
COPY taker /build/taker
COPY orderbook_watcher /build/orderbook_watcher
COPY jmwalletd /build/jmwalletd
COPY tumbler /build/tumbler

# New checkout dependencies may not exist in the pinned remote base.
RUN /opt/venv/bin/pip install --no-cache-dir \
        --require-hashes -r /build/taker/requirements.txt \
    && /opt/venv/bin/pip install --no-cache-dir --no-deps --force-reinstall \
        /build/jmcore \
        /build/jmwallet \
        /build/maker \
        /build/taker \
        /build/orderbook_watcher \
        /build/jmwalletd \
        /build/tumbler \
    && /opt/venv/bin/pip check \
    && rm --recursive --force /build

COPY jmwalletd/rootfs/etc/s6-overlay /etc/s6-overlay
RUN chmod +x \
    /etc/s6-overlay/s6-rc.d/tor/run \
    /etc/s6-overlay/s6-rc.d/tor-ready/up \
    /etc/s6-overlay/s6-rc.d/tor-log/run \
    /etc/s6-overlay/s6-rc.d/jmwalletd-svc/run \
    /etc/s6-overlay/s6-rc.d/jmwalletd-svc-log/run \
    /etc/s6-overlay/s6-rc.d/obwatcher/run \
    /etc/s6-overlay/s6-rc.d/obwatcher-log/run
