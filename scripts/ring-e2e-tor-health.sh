#!/bin/bash
set -eu

for service in lnd-maker1 lnd-maker2 lnd-maker3 lnd-maker4 lnd-taker; do
    test -s "/var/lib/tor/${service}/hostname"
done

exec /bin/bash /scripts/tor-bootstrap-health.sh
