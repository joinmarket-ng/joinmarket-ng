#!/usr/bin/env bash
# Run install.sh in the container and verify the result.
#
# Steps:
#   1. Run ``./install.sh -y --skip-tor --taker --version main`` so the
#      install is unattended, skips the Tor configuration prompt, only
#      installs the taker component (faster), and uses the main branch
#      (avoids the need for a tagged release matching the script).
#   2. Source the venv it created and run ``jm-wallet --help`` to prove
#      the entry point is wired up.
#   3. Print a clear PASS/FAIL marker so the calling pytest can grep
#      for it without depending on exit codes alone.
#
# The script intentionally uses --version main + --skip-verify (implied
# by --version main per install.sh) so the smoke test does not require
# a release signature on whatever branch we are testing.
set -euo pipefail

echo "=== running install.sh ==="
# Use ``-x`` only for the install step so failures show the offending
# line without polluting the rest of the log.
bash -x ./install.sh -y --skip-tor --taker --version main

VENV="${HOME}/.joinmarket-ng/venv"
if [[ ! -f "${VENV}/bin/activate" ]]; then
    echo "FAIL: venv not created at ${VENV}"
    exit 1
fi

echo ""
echo "=== sourcing venv and running jm-wallet --help ==="
# shellcheck disable=SC1091
source "${VENV}/bin/activate"

if ! command -v jm-wallet &> /dev/null; then
    echo "FAIL: jm-wallet not on PATH after install"
    exit 1
fi

# A successful ``--help`` exits 0 and prints usage. Capture the output
# to a variable rather than piping into ``grep -q`` so SIGPIPE from
# grep closing its stdin does not trip ``pipefail`` and mask a real
# pass. ``--no-color`` would be cleaner but the wallet CLI does not
# expose it; ANSI escapes around "Usage" do not affect a substring
# match.
help_output="$(jm-wallet --help 2>&1)"
if ! grep -qi "usage" <<< "${help_output}"; then
    echo "FAIL: 'jm-wallet --help' did not print a usage banner"
    echo "--- captured output ---"
    echo "${help_output}"
    exit 1
fi

echo ""
echo "=== INSTALL_SMOKE_PASS ==="
