FROM jam_ng_base

ARG JM_NG_REVISION=local
LABEL server-version="${JM_NG_REVISION}"

# Replace the base image's remote checkout with packages from the current
# build context so CI and local runs exercise the exact working tree.
COPY jmcore/requirements.txt /build/requirements-jmcore.txt
COPY jmwallet/requirements.txt /build/requirements-jmwallet.txt
COPY jmswap/requirements.txt /build/requirements-jmswap.txt
COPY maker/requirements.txt /build/requirements-maker.txt
COPY taker/requirements.txt /build/requirements-taker.txt
COPY orderbook_watcher/requirements.txt /build/requirements-orderbook-watcher.txt
COPY jmwalletd/requirements.txt /build/requirements-jmwalletd.txt

RUN /opt/venv/bin/pip install --no-cache-dir --force-reinstall --require-hashes \
        -r /build/requirements-jmcore.txt \
        -r /build/requirements-jmwallet.txt \
        -r /build/requirements-jmswap.txt \
        -r /build/requirements-maker.txt \
        -r /build/requirements-taker.txt \
        -r /build/requirements-orderbook-watcher.txt \
        -r /build/requirements-jmwalletd.txt

COPY jmcore /build/jmcore
COPY jmwallet /build/jmwallet
COPY jmswap /build/jmswap
COPY maker /build/maker
COPY taker /build/taker
COPY orderbook_watcher /build/orderbook_watcher
COPY jmwalletd /build/jmwalletd
COPY tumbler /build/tumbler

RUN /opt/venv/bin/pip install --no-cache-dir --no-deps --force-reinstall \
        /build/jmcore \
        /build/jmwallet \
        /build/jmswap \
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
