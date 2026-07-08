"""Auto-generate API reference pages for mkdocs-gen-files.

This script is executed during `properdocs build` by the mkdocs-gen-files plugin.
It scans all Python source directories and generates:
  - One .md file per Python module with a `:::` mkdocstrings directive
  - Index pages per package with a table linking to each module
  - A SUMMARY.md for the literate-nav plugin to build navigation

The generated files are virtual (written to ProperDocs' in-memory filesystem)
and never committed to git. The docs/api/ directory is gitignored.
"""

from __future__ import annotations

import logging
from pathlib import Path

import mkdocs_gen_files

log = logging.getLogger("mkdocs.plugins.gen_ref_pages")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Maps package name -> source root (relative to repo root).
PACKAGES: dict[str, str] = {
    "jmcore": "jmcore/src",
    "jmwallet": "jmwallet/src",
    "taker": "taker/src",
    "maker": "maker/src",
    "directory_server": "directory_server/src",
    "orderbook_watcher": "orderbook_watcher/src",
    "tumbler": "tumbler/src",
    "jmwalletd": "jmwalletd/src",
}

# Human-readable labels for the navigation.
PACKAGE_LABELS: dict[str, str] = {
    "jmcore": "jmcore",
    "jmwallet": "jmwallet",
    "taker": "taker",
    "maker": "maker",
    "directory_server": "directory_server",
    "orderbook_watcher": "orderbook_watcher",
    "tumbler": "tumbler",
    "jmwalletd": "jmwalletd",
}

# Packages ordered as they should appear in the nav.
PACKAGE_ORDER: list[str] = [
    "jmcore",
    "jmwallet",
    "taker",
    "maker",
    "tumbler",
    "jmwalletd",
    "orderbook_watcher",
    "directory_server",
]

# Short descriptions for the top-level API index table.
PACKAGE_DESCRIPTIONS: dict[str, str] = {
    "jmcore": "Core library: crypto, networking, protocol, configuration",
    "jmwallet": "Wallet management, coin selection, transaction signing",
    "taker": "CoinJoin taker: orderbook, negotiation, transaction building",
    "maker": "CoinJoin maker: offers, fidelity bonds, protocol handlers",
    "directory_server": "Peer discovery and message routing server",
    "orderbook_watcher": "Orderbook monitoring and aggregation",
    "tumbler": "High-level CoinJoin scheduler with YAML-persisted plans",
    "jmwalletd": "JAM-compatible HTTP/WebSocket wallet daemon",
}

# Output directory inside docs/ for generated pages.
API_DIR = "api"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _collect_modules(
    src_root: Path,
    package: str,
) -> list[tuple[str, Path]]:
    """Walk *src_root*/<package> and return (dotted_module, py_path) pairs.

    Skips ``__init__.py``, test files, and ``__pycache__`` directories.
    """
    pkg_dir = src_root / package
    if not pkg_dir.is_dir():
        log.warning("Package directory not found: %s", pkg_dir)
        return []

    modules: list[tuple[str, Path]] = []
    for py_file in sorted(pkg_dir.rglob("*.py")):
        # Skip __init__, tests, __pycache__
        if py_file.name.startswith("__") or py_file.name.startswith("test_"):
            continue
        if "__pycache__" in py_file.parts:
            continue

        # Build dotted module path relative to src_root.
        rel = py_file.relative_to(src_root)
        dotted = ".".join(rel.with_suffix("").parts)
        modules.append((dotted, py_file))

    return modules


def _module_nav_name(dotted: str, package: str) -> str:
    """Return the short display name for a module (strip the package prefix)."""
    prefix = package + "."
    if dotted.startswith(prefix):
        return dotted[len(prefix) :]
    return dotted


def _write_module_page(dotted: str, out_path: str) -> None:
    """Write a single module reference page."""
    with mkdocs_gen_files.open(out_path, "w") as fd:
        fd.write(f"# {dotted}\n\n")
        fd.write(f"::: {dotted}\n")
    mkdocs_gen_files.set_edit_path(out_path, ".")


def _subpackage_key(dotted: str, package: str) -> str | None:
    """If *dotted* is inside a sub-package, return the sub-package name.

    Example: ``jmwallet.wallet.address`` -> ``wallet``
    """
    suffix = _module_nav_name(dotted, package)
    parts = suffix.split(".")
    if len(parts) > 1:
        return parts[0]
    return None


# ---------------------------------------------------------------------------
# Main generation
# ---------------------------------------------------------------------------

nav_lines: list[str] = []  # SUMMARY.md lines for literate-nav

# Top-level API index page
with mkdocs_gen_files.open(f"{API_DIR}/index.md", "w") as fd:
    fd.write("---\ntitle: API Reference\n---\n\n")
    fd.write("# API Reference\n\n")
    fd.write("Overview of all packages in JoinMarket NG.\n\n")
    fd.write("| Package | Description |\n")
    fd.write("|---------|-------------|\n")
    for pkg in PACKAGE_ORDER:
        desc = PACKAGE_DESCRIPTIONS.get(pkg, "")
        fd.write(f"| [{pkg}]({pkg}/index.md) | {desc} |\n")
mkdocs_gen_files.set_edit_path(f"{API_DIR}/index.md", ".")

nav_lines.append("* [API Reference](index.md)")

for pkg in PACKAGE_ORDER:
    src_root = Path(PACKAGES[pkg])
    modules = _collect_modules(src_root, pkg)
    if not modules:
        continue

    label = PACKAGE_LABELS[pkg]
    pkg_dir = f"{API_DIR}/{pkg}"

    # Group modules by sub-package (None = top-level).
    groups: dict[str | None, list[tuple[str, str]]] = {}
    for dotted, _ in modules:
        subpkg = _subpackage_key(dotted, pkg)
        nav_name = _module_nav_name(dotted, pkg)
        # Output path for this module's .md file.
        parts = nav_name.split(".")
        md_path = f"{pkg_dir}/{'/'.join(parts)}.md"
        groups.setdefault(subpkg, []).append((dotted, md_path))

        _write_module_page(dotted, md_path)

    # --- Package index page ---
    with mkdocs_gen_files.open(f"{pkg_dir}/index.md", "w") as fd:
        desc = PACKAGE_DESCRIPTIONS.get(pkg, "")
        fd.write(f"# {label}\n\n")
        if desc:
            fd.write(f"{desc}\n\n")

        # Top-level modules
        top_level = groups.get(None, [])
        if top_level:
            fd.write("| Module | Description |\n")
            fd.write("|--------|-------------|\n")
            for dotted, md_path in top_level:
                short = _module_nav_name(dotted, pkg)
                rel_link = Path(md_path).name
                fd.write(f"| [{short}]({rel_link}) | |\n")
            fd.write("\n")

        # Sub-package sections
        for subpkg_name in sorted(k for k in groups if k is not None):
            entries = groups[subpkg_name]
            fd.write(f"## {subpkg_name}\n\n")
            fd.write("| Module | Description |\n")
            fd.write("|--------|-------------|\n")
            for dotted, md_path in entries:
                short = dotted.split(".")[-1]
                rel_link = "/".join(md_path.split("/")[2:])  # relative to pkg_dir
                fd.write(f"| [{short}]({rel_link}) | |\n")
            fd.write("\n")
    mkdocs_gen_files.set_edit_path(f"{pkg_dir}/index.md", ".")

    # --- SUMMARY.md nav entries ---
    # Paths are relative to api/ (where SUMMARY.md lives).
    nav_lines.append(f"* {label}:")
    nav_lines.append(f"    * [Overview]({pkg}/index.md)")

    # Emit nav entries grouped by sub-package
    subpkg_keys: list[str | None] = [None, *sorted(k for k in groups if k is not None)]
    for nav_subpkg in subpkg_keys:
        entries = groups.get(nav_subpkg, [])
        if nav_subpkg is not None:
            nav_lines.append(f"    * {nav_subpkg}:")
        for dotted, md_path in entries:
            short = dotted.split(".")[-1]
            indent = "        " if nav_subpkg is not None else "    "
            # md_path is e.g. "api/jmcore/bitcoin.md" -> strip "api/" prefix
            rel_to_api = md_path.removeprefix(f"{API_DIR}/")
            nav_lines.append(f"{indent}* [{short}]({rel_to_api})")

# Write the SUMMARY.md for literate-nav
with mkdocs_gen_files.open(f"{API_DIR}/SUMMARY.md", "w") as fd:
    fd.write("\n".join(nav_lines) + "\n")
