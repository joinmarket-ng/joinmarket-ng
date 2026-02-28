#!/bin/bash

# =============================================================================
# Switch documentation URLs between fork and upstream
# =============================================================================
# Usage:
#   ./switch-urls.sh fork      # Use l3ftblank.github.io URLs
#   ./switch-urls.sh upstream  # Use joinmarket-ng.github.io URLs
#   ./switch-urls.sh status    # Show current URLs in all READMEs
# =============================================================================

set -e

MODE="${1:-fork}"

case "$MODE" in
    fork)
        echo "Switching to fork URLs (l3ftblank.github.io)..."
        find . -name "README.md" -type f -exec sed -i \
          's|joinmarket-ng.github.io/joinmarket-ng|l3ftblank.github.io/joinmarket-ng|g' {} \;
        echo "Done! URLs now point to your fork."
        ;;
    upstream)
        echo "Switching to upstream URLs (joinmarket-ng.github.io)..."
        find . -name "README.md" -type f -exec sed -i \
          's|l3ftblank.github.io/joinmarket-ng|joinmarket-ng.github.io/joinmarket-ng|g' {} \;
        echo "Done! URLs now point to upstream."
        ;;
    status)
        echo "Current URLs in all README.md files:"
        echo "========================================="
        find . -name "README.md" -type f | while read -r file; do
            echo ""
            echo "--- $file ---"
            grep -o 'https://[^)]*github.io/joinmarket-ng[^)]*' "$file" 2>/dev/null | head -3 || echo "No URLs found"
        done
        ;;
    *)
        echo "Usage: $0 {fork|upstream|status}"
        exit 1
        ;;
esac
