#!/bin/bash
# Update dependency lock files and Flatpak sources in the JoinMarket NG monorepo
#
# Usage:
#   ./scripts/update-deps.sh              # Update all dependencies
#   ./scripts/update-deps.sh --dev-only   # Update only dev dependencies
#   ./scripts/update-deps.sh --prod-only  # Update only production dependencies

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
FLATPAK_UPDATER="$SCRIPT_DIR/update-flatpak-deps.py"

run_pip_compile() {
    if command -v pip-compile >/dev/null 2>&1; then
        pip-compile "$@"
    elif command -v python3 >/dev/null 2>&1; then
        python3 -m piptools compile "$@"
    else
        python -m piptools compile "$@"
    fi
}

PACKAGES="jmcore directory_server orderbook_watcher jmwallet maker taker jmwalletd"
UPDATE_PROD=true
UPDATE_DEV=true

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --dev-only)
            UPDATE_PROD=false
            shift
            ;;
        --prod-only)
            UPDATE_DEV=false
            shift
            ;;
        *)
            echo "Unknown option: $1"
            echo "Usage: $0 [--dev-only|--prod-only]"
            exit 1
            ;;
    esac
done

echo "========================================="
echo "Updating JoinMarket NG Dependencies"
echo "========================================="
echo ""

if [ "$UPDATE_PROD" = true ]; then
    echo "Updating production dependencies..."
    echo ""

    for dir in $PACKAGES; do
        echo "=== $dir ==="
        cd "$dir"

        if [ "$dir" = "jmcore" ]; then
            # jmcore has no local deps, compile directly
            run_pip_compile -U --strip-extras --generate-hashes pyproject.toml -o requirements.txt
        else
            # For other packages, we need to temporarily remove local package references
            # from pyproject.toml because pip-compile can't resolve them from PyPI.
            cp pyproject.toml pyproject.toml.bak

            # Remove jmcore and jmwallet lines
            # REQUIRE INDENTATION to avoid removing 'name = "jmwallet"'
            sed -i '/^[[:space:]][[:space:]]*"jmcore"/d' pyproject.toml
            sed -i '/^[[:space:]][[:space:]]*"jmwallet"/d' pyproject.toml

            # Compile
            run_pip_compile -U --strip-extras --generate-hashes pyproject.toml -o requirements.txt

            # Restore original pyproject.toml
            mv pyproject.toml.bak pyproject.toml
        fi

        cd ..
        echo ""
    done
fi

if [ "$UPDATE_DEV" = true ]; then
    echo "Updating development dependencies..."
    echo ""

    for dir in $PACKAGES; do
        echo "=== $dir (dev) ==="
        cd "$dir"

        if [ "$dir" = "jmcore" ]; then
            run_pip_compile -U --strip-extras --generate-hashes --extra dev pyproject.toml -o requirements-dev.txt
        else
            cp pyproject.toml pyproject.toml.bak
            sed -i '/^[[:space:]][[:space:]]*"jmcore"/d' pyproject.toml
            sed -i '/^[[:space:]][[:space:]]*"jmwallet"/d' pyproject.toml

            # Compile dev deps
            run_pip_compile -U --strip-extras --generate-hashes --extra dev pyproject.toml -o requirements-dev.txt

            # Restore original
            mv pyproject.toml.bak pyproject.toml
        fi

        cd ..
        echo ""
    done

    # Documentation dependencies (root-level requirements-docs.in)
    echo "=== docs ==="
    run_pip_compile -U --strip-extras --generate-hashes requirements-docs.in -o requirements-docs.txt
    echo ""
fi

if [ "$UPDATE_PROD" = true ]; then
    echo "Updating Flatpak dependencies..."
    echo ""

    if [ -f "$FLATPAK_UPDATER" ]; then
        if command -v python3 >/dev/null 2>&1; then
            python3 "$FLATPAK_UPDATER" --manifest "$PROJECT_ROOT/flatpak/org.joinmarketng.JamNG.yml"
        else
            python "$FLATPAK_UPDATER" --manifest "$PROJECT_ROOT/flatpak/org.joinmarketng.JamNG.yml"
        fi
    else
        echo "Warning: Flatpak updater script not found at $FLATPAK_UPDATER"
    fi

    echo ""
fi

echo "========================================="
echo "All dependencies updated successfully"
echo "========================================="
echo ""
echo "Next steps:"
echo "  1. Review changes: git diff */requirements*.txt requirements-docs.txt flatpak/org.joinmarketng.JamNG.yml"
echo "  2. Test locally: pip install -r <package>/requirements-dev.txt"
echo "  3. Run tests: pytest"
echo "  4. Commit: git add */requirements*.txt requirements-docs.txt flatpak/org.joinmarketng.JamNG.yml && git commit"
