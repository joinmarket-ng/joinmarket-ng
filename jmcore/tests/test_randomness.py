"""Tests for :mod:`jmcore.randomness` and the secure-randomness migration.

``f2eb2c35`` moved every adversary-visible choice off the stdlib Mersenne
Twister and onto the operating-system CSPRNG. The scan test below is the part
that keeps it moved, since a single ``import random`` reintroduced into a
selection path is invisible in review and silently predictable at runtime.
"""

from __future__ import annotations

import ast
import random
from pathlib import Path

import pytest

from jmcore.randomness import secure_random

# Members of the stdlib ``random`` module that draw from the shared Mersenne
# Twister. ``Random`` and ``SystemRandom`` are constructors, not draws, so a
# deliberate reproducible generator stays allowed.
_PREDICTABLE_MEMBERS = frozenset(
    {
        "betavariate",
        "choice",
        "choices",
        "expovariate",
        "gammavariate",
        "gauss",
        "getrandbits",
        "lognormvariate",
        "normalvariate",
        "paretovariate",
        "randbytes",
        "randint",
        "random",
        "randrange",
        "sample",
        "seed",
        "shuffle",
        "triangular",
        "uniform",
        "vonmisesvariate",
        "weibullvariate",
    }
)

_REPO_ROOT = Path(__file__).resolve().parents[2]

_PACKAGE_SOURCE_DIRS = (
    "directory_server",
    "jmcore",
    "jmwallet",
    "jmwalletd",
    "maker",
    "taker",
    "tumbler",
)


def _source_files() -> list[Path]:
    files: list[Path] = []
    for package in _PACKAGE_SOURCE_DIRS:
        src = _REPO_ROOT / package / "src"
        if src.is_dir():
            files.extend(sorted(src.rglob("*.py")))
    return files


def _predictable_random_uses(tree: ast.AST) -> list[str]:
    """Return ``random.<member>`` uses that draw from the shared generator."""
    findings: list[str] = []

    for node in ast.walk(tree):
        # from random import shuffle
        if isinstance(node, ast.ImportFrom) and node.module == "random":
            findings.extend(
                f"from random import {alias.name} (line {node.lineno})"
                for alias in node.names
                if alias.name in _PREDICTABLE_MEMBERS
            )
        # random.shuffle(...)
        elif (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "random"
            and node.attr in _PREDICTABLE_MEMBERS
        ):
            findings.append(f"random.{node.attr} (line {node.lineno})")

    return findings


class TestSecureRandom:
    def test_secure_random_draws_from_the_operating_system(self) -> None:
        assert isinstance(secure_random, random.SystemRandom)

    def test_secure_random_is_not_the_shared_generator(self) -> None:
        """Seeding must not make it reproducible, unlike the module-level default."""
        secure_random.seed(1234)
        first = [secure_random.random() for _ in range(8)]
        secure_random.seed(1234)
        second = [secure_random.random() for _ in range(8)]

        assert first != second

    def test_shuffle_does_not_disturb_the_shared_generator(self) -> None:
        """A CoinJoin shuffle must not consume or perturb ``random`` module state.

        If ``secure_random`` were ever reassigned to the module-level generator,
        an observer who can sample any other ``random`` consumer in the process
        could learn about transaction ordering.
        """
        random.seed(99)
        baseline = [random.random() for _ in range(4)]

        random.seed(99)
        secure_random.shuffle(list(range(50)))
        after = [random.random() for _ in range(4)]

        assert baseline == after


class TestNoPredictableRandomnessInSources:
    """Regression guard for the f2eb2c35 migration."""

    def test_source_tree_is_scannable(self) -> None:
        """Fail loudly if the scan below silently covers nothing."""
        files = _source_files()
        assert len(files) > 100, f"expected the package sources, found {len(files)} files"

    @pytest.mark.parametrize("package", _PACKAGE_SOURCE_DIRS)
    def test_package_avoids_predictable_randomness(self, package: str) -> None:
        src = _REPO_ROOT / package / "src"
        if not src.is_dir():
            pytest.skip(f"{package} has no src directory")

        offenders: dict[str, list[str]] = {}
        for path in sorted(src.rglob("*.py")):
            uses = _predictable_random_uses(ast.parse(path.read_text(encoding="utf-8")))
            if uses:
                offenders[str(path.relative_to(_REPO_ROOT))] = uses

        assert not offenders, (
            "predictable randomness reintroduced; use jmcore.randomness.secure_random "
            f"instead: {offenders}"
        )
