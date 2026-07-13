#!/usr/bin/env bash
# Run install.sh in the container and verify the result.
#
# Steps:
#   1. Run ``./install.sh`` against a requested profile. The default profile
#      exercises the recommended maker+taker+tumbler installation; the taker
#      profile protects the minimal single-role installation.
#   2. Source the venv it created and run its expected CLI entry points to
#      prove they are wired up.
#   3. Print a clear PASS/FAIL marker so the calling pytest can grep
#      for it without depending on exit codes alone.
#
# The script intentionally passes --skip-verify so the smoke test does
# not require a release signature on whatever branch / commit we are
# testing. CI overrides JMNG_INSTALL_REF to the workflow head SHA so the
# install exercises the exact code under review (otherwise install.sh
# would pull from ``main`` which would not contain the proposed changes).
set -euo pipefail

INSTALL_REF="${JMNG_INSTALL_REF:-main}"
INSTALL_PROFILE="${JMNG_INSTALL_PROFILE:-default}"
echo "=== running install.sh against ref ${INSTALL_REF} ==="
install_args=(-y --skip-tor --skip-verify --version "${INSTALL_REF}")
case "${INSTALL_PROFILE}" in
    default)
        expected_commands=(jm-wallet jm-tumbler)
        ;;
    taker)
        install_args+=(--taker)
        expected_commands=(jm-wallet jm-taker)
        ;;
    *)
        echo "FAIL: unsupported install smoke profile ${INSTALL_PROFILE}"
        exit 1
        ;;
esac
# Use ``-x`` only for the install step so failures show the offending
# line without polluting the rest of the log.
bash -x ./install.sh "${install_args[@]}"

VENV="${HOME}/.joinmarket-ng/venv"
if [[ ! -f "${VENV}/bin/activate" ]]; then
    echo "FAIL: venv not created at ${VENV}"
    exit 1
fi

echo ""
echo "=== sourcing venv and running CLI help ==="
# shellcheck disable=SC1091
source "${VENV}/bin/activate"

for command in "${expected_commands[@]}"; do
    if ! command -v "${command}" &> /dev/null; then
        echo "FAIL: ${command} not on PATH after install"
        exit 1
    fi

    # A successful ``--help`` exits 0 and prints usage. Capture the output
    # to a variable rather than piping into ``grep -q`` so SIGPIPE from grep
    # closing its stdin does not trip pipefail and mask a real pass.
    help_output="$("${command}" --help 2>&1)"
    if ! grep -qi "usage" <<< "${help_output}"; then
        echo "FAIL: '${command} --help' did not print a usage banner"
        echo "--- captured output ---"
        echo "${help_output}"
        exit 1
    fi
done

echo ""
echo "=== INSTALL_SMOKE_PASS ==="
