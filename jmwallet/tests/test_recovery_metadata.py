"""Durable recovery claims and metadata updates across process boundaries."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from jmwallet.cli.mnemonic import (
    acknowledge_fidelity_bond_recovery_scanned,
    fidelity_bond_recovery_attempt,
    load_mnemonic_meta,
    mnemonic_requires_fidelity_bond_recovery,
    save_mnemonic_meta,
)

FINGERPRINT = "aabbccdd"
RECOVERY_KEY = f"fidelity_bond_recovery.{FINGERPRINT}"


@pytest.mark.parametrize("meta", [{}, {"fingerprint": FINGERPRINT}])
def test_unknown_legacy_metadata_does_not_request_recovery(
    tmp_path: Path, meta: dict[str, str]
) -> None:
    source = tmp_path / "legacy.mnemonic"
    save_mnemonic_meta(source, fingerprint=meta.get("fingerprint"))
    assert not mnemonic_requires_fidelity_bond_recovery(source, FINGERPRINT)


def test_started_claim_precedes_work_and_preserves_concurrent_metadata(tmp_path: Path) -> None:
    source = tmp_path / "wallet.mnemonic"
    save_mnemonic_meta(source, fidelity_bond_recovery="pending")
    with fidelity_bond_recovery_attempt(source, FINGERPRINT, automatic=True) as claimed:
        assert claimed
        assert load_mnemonic_meta(source)[RECOVERY_KEY] == "started"
        # An independent metadata writer must not block on the long recovery lock.
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from pathlib import Path; import sys; "
                "from jmwallet.cli.mnemonic import save_mnemonic_meta; "
                "save_mnemonic_meta(Path(sys.argv[1]), fingerprint='aabbccdd')",
                str(source),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
    assert load_mnemonic_meta(source) == {
        "fidelity_bond_recovery": "pending",
        "fingerprint": FINGERPRINT,
        RECOVERY_KEY: "complete",
    }


def test_concurrent_process_cannot_claim_explicit_recovery(tmp_path: Path) -> None:
    source = tmp_path / "wallet.mnemonic"
    with fidelity_bond_recovery_attempt(source, FINGERPRINT):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from pathlib import Path\n"
                "import sys\n"
                "from jmwallet.cli.mnemonic import (fidelity_bond_recovery_attempt, "
                "FidelityBondRecoveryInProgressError)\n"
                "try:\n"
                "    with fidelity_bond_recovery_attempt(Path(sys.argv[1]), 'aabbccdd'):\n"
                "        sys.exit(2)\n"
                "except FidelityBondRecoveryInProgressError:\n"
                "    sys.exit(0)\n",
                str(source),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr


def test_completion_write_failure_leaves_started_and_explicit_retry_works(tmp_path: Path) -> None:
    source = tmp_path / "wallet.mnemonic"
    save_mnemonic_meta(source, fidelity_bond_recovery="pending")
    with (
        patch(
            "jmwallet.cli.mnemonic.mark_fidelity_bond_recovery_complete",
            side_effect=OSError("disk full"),
        ),
        pytest.raises(OSError, match="disk full"),
        fidelity_bond_recovery_attempt(source, FINGERPRINT),
    ):
        pass
    assert load_mnemonic_meta(source)[RECOVERY_KEY] == "started"
    assert not mnemonic_requires_fidelity_bond_recovery(source, FINGERPRINT)
    with fidelity_bond_recovery_attempt(source, FINGERPRINT):
        pass
    assert load_mnemonic_meta(source)[RECOVERY_KEY] == "complete"


def test_failed_atomic_metadata_write_preserves_previous_contents(tmp_path: Path) -> None:
    source = tmp_path / "wallet.mnemonic"
    save_mnemonic_meta(source, fidelity_bond_recovery="pending")
    before = load_mnemonic_meta(source)
    with (
        patch("jmcore.secure_files.os.replace", side_effect=OSError("disk full")),
        pytest.raises(OSError, match="disk full"),
    ):
        save_mnemonic_meta(source, fingerprint=FINGERPRINT)
    assert load_mnemonic_meta(source) == before


def test_failed_start_write_never_enters_recovery(tmp_path: Path) -> None:
    source = tmp_path / "wallet.mnemonic"
    with (
        patch("jmwallet.cli.mnemonic._write_mnemonic_meta", side_effect=OSError("read only")),
        pytest.raises(OSError, match="read only"),
        fidelity_bond_recovery_attempt(source, FINGERPRINT),
    ):
        pytest.fail("Recovery started without durable state")


@pytest.mark.parametrize("with_fingerprint", [False, True])
def test_unknown_coverage_is_informational_and_does_not_write_metadata(
    tmp_path: Path, with_fingerprint: bool
) -> None:
    source = tmp_path / "wallet.mnemonic"
    if with_fingerprint:
        save_mnemonic_meta(source, fingerprint=FINGERPRINT)
    before = load_mnemonic_meta(source)
    with patch("jmwallet.cli.mnemonic.logger") as log:
        assert not mnemonic_requires_fidelity_bond_recovery(source, FINGERPRINT)
    log.warning.assert_not_called()
    message = log.info.call_args.args[0]
    assert "legacy" not in message
    assert "coverage is unknown" in message
    assert "No automatic recovery scan will run" in message
    assert "registered bonds still sync normally" in message
    assert "regular wallet history scan" in message
    assert "960 bond addresses" in message
    assert "jm-wallet recover-bonds --mark-scanned" in message
    assert load_mnemonic_meta(source) == before
    if not with_fingerprint:
        assert not source.with_suffix(".mnemonic.meta").exists()


@pytest.mark.parametrize(
    ("state", "scoped", "requires_recovery", "warning"),
    [
        ("pending", False, True, False),
        ("not_required", False, False, False),
        ("complete", False, False, False),
        ("complete", True, False, False),
        ("started", True, False, True),
        ("invalid", True, False, True),
    ],
)
def test_known_recovery_states_keep_existing_behavior(
    tmp_path: Path, state: str, scoped: bool, requires_recovery: bool, warning: bool
) -> None:
    import json

    source = tmp_path / "wallet.mnemonic"
    meta = {RECOVERY_KEY if scoped else "fidelity_bond_recovery": state}
    source.with_suffix(".mnemonic.meta").write_text(json.dumps(meta))
    with patch("jmwallet.cli.mnemonic.logger") as log:
        assert mnemonic_requires_fidelity_bond_recovery(source, FINGERPRINT) == requires_recovery
    assert log.warning.called == warning
    log.info.assert_not_called()
    assert load_mnemonic_meta(source) == meta


def test_manual_acknowledgment_excludes_active_recovery(tmp_path: Path) -> None:
    source = tmp_path / "wallet.mnemonic"
    with fidelity_bond_recovery_attempt(source, FINGERPRINT):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from pathlib import Path\n"
                "import sys\n"
                "from jmwallet.cli.mnemonic import (acknowledge_fidelity_bond_recovery_scanned, "
                "FidelityBondRecoveryInProgressError)\n"
                "try:\n"
                "    acknowledge_fidelity_bond_recovery_scanned(Path(sys.argv[1]), 'aabbccdd')\n"
                "    sys.exit(2)\n"
                "except FidelityBondRecoveryInProgressError:\n"
                "    sys.exit(0)\n",
                str(source),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        assert load_mnemonic_meta(source)[RECOVERY_KEY] == "started"


def test_manual_acknowledgment_write_failure_preserves_unknown_state(tmp_path: Path) -> None:
    source = tmp_path / "wallet.mnemonic"
    save_mnemonic_meta(source, fingerprint=FINGERPRINT)
    before = load_mnemonic_meta(source)
    with (
        patch("jmcore.secure_files.os.replace", side_effect=OSError("disk full")),
        pytest.raises(OSError, match="disk full"),
    ):
        acknowledge_fidelity_bond_recovery_scanned(source, FINGERPRINT)
    assert load_mnemonic_meta(source) == before
