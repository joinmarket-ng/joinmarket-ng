#!/bin/bash
set -eu

exec 3<>/dev/tcp/127.0.0.1/9051
printf 'AUTHENTICATE "ring-e2e"\r\nGETINFO status/bootstrap-phase\r\nQUIT\r\n' >&3
timeout 2 cat <&3 | grep -q 'PROGRESS=100'
