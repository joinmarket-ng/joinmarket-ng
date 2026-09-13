"""Focused regression tests for the installer's non-destructive Tor setup."""

from __future__ import annotations

import shlex
import shutil
import socket
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "install.sh"


pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash not available"
)


def _configure_torrc(
    torrc_path: Path,
    service_log: Path,
    *,
    verify_config: bool = True,
    restart_config: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run ``configure_torrc`` with all privileged and service calls stubbed."""
    script = f"""
source {shlex.quote(str(INSTALL_SH))}
set +e
print_header() {{ :; }}
print_info() {{ printf 'INFO:%s\\n' "$1"; }}
print_success() {{ printf 'OK:%s\\n' "$1"; }}
print_warning() {{ printf 'WARN:%s\\n' "$1"; }}
print_error() {{ printf 'ERR:%s\\n' "$1"; }}
sudo() {{ "$@"; }}
tor() {{ return {0 if verify_config else 1}; }}
systemctl() {{
    printf '%s\\n' "$*" >> {shlex.quote(str(service_log))}
    if [[ "$1" == "restart" && {"false" if restart_config else "true"} == "true" ]]; then
        return 1
    fi
    return 0
}}
OS_TYPE=linux
configure_torrc {shlex.quote(str(torrc_path))}
printf 'EXIT:%s\\n' "$?"
"""
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _service_calls(service_log: Path) -> list[str]:
    if not service_log.exists():
        return []
    return service_log.read_text().splitlines()


def test_clean_torrc_gets_one_managed_block(tmp_path: Path) -> None:
    torrc = tmp_path / "torrc"
    service_log = tmp_path / "service.log"
    torrc.write_text("# Existing Tor configuration\n")

    result = _configure_torrc(torrc, service_log)

    assert "EXIT:0" in result.stdout, result.stdout + result.stderr
    content = torrc.read_text()
    assert content.count("## JoinMarket-NG Configuration") == 1
    assert "SocksPort 127.0.0.1:9050" in content
    assert "ControlPort 127.0.0.1:9051" in content
    assert "CookieAuthentication 1" in content
    assert "CookieAuthFile /run/tor/control.authcookie" in content
    assert _service_calls(service_log) == ["restart tor", "enable tor"]


def test_equivalent_listeners_are_preserved_and_not_duplicated(tmp_path: Path) -> None:
    torrc = tmp_path / "torrc"
    service_log = tmp_path / "service.log"
    original = (
        "  sOcKsPoRt 9050 IsolateSOCKSAuth\n"
        "  cOnTrOlPoRt localhost:9051\n"
        "# preserve this comment\n"
    )
    torrc.write_text(original)

    result = _configure_torrc(torrc, service_log)

    assert "EXIT:0" in result.stdout, result.stdout + result.stderr
    content = torrc.read_text()
    assert content.startswith(original)
    assert content.count("## JoinMarket-NG Configuration") == 1
    assert content.lower().count("socksport") == 1
    assert content.lower().count("controlport") == 1
    assert "CookieAuthentication 1" in content
    assert "CookieAuthFile /run/tor/control.authcookie" in content


def test_rerun_does_not_add_another_managed_block_or_restart(tmp_path: Path) -> None:
    torrc = tmp_path / "torrc"
    service_log = tmp_path / "service.log"
    torrc.write_text("# Existing Tor configuration\n")

    first = _configure_torrc(torrc, service_log)
    second = _configure_torrc(torrc, service_log)

    assert "EXIT:0" in first.stdout, first.stdout + first.stderr
    assert "EXIT:0" in second.stdout, second.stdout + second.stderr
    assert torrc.read_text().count("## JoinMarket-NG Configuration") == 1
    assert _service_calls(service_log) == ["restart tor", "enable tor"]


def test_complete_unmanaged_configuration_is_unchanged(tmp_path: Path) -> None:
    torrc = tmp_path / "torrc"
    service_log = tmp_path / "service.log"
    original = (
        "SocksPort localhost:9050\n"
        "ControlPort 9051\n"
        "CookieAuthentication 1\n"
        "CookieAuthFile /run/tor/control.authcookie\n"
    )
    torrc.write_text(original)

    result = _configure_torrc(torrc, service_log)

    assert "EXIT:0" in result.stdout, result.stdout + result.stderr
    assert torrc.read_text() == original
    assert _service_calls(service_log) == []


@pytest.mark.parametrize(
    "original",
    [
        "CookieAuthentication 0\n",
        "CookieAuthentication 1\nCookieAuthFile /var/lib/tor/control_auth_cookie\n",
    ],
)
def test_custom_cookie_authentication_is_not_overridden(
    tmp_path: Path, original: str
) -> None:
    torrc = tmp_path / "torrc"
    service_log = tmp_path / "service.log"
    torrc.write_text(original)

    result = _configure_torrc(torrc, service_log)

    assert "EXIT:0" in result.stdout, result.stdout + result.stderr
    assert "not be overridden" in result.stdout
    assert torrc.read_text() == original
    assert _service_calls(service_log) == []


def test_active_include_is_left_unchanged(tmp_path: Path) -> None:
    torrc = tmp_path / "torrc"
    service_log = tmp_path / "service.log"
    original = "%include /etc/tor/torrc.d/*.conf\n"
    torrc.write_text(original)

    result = _configure_torrc(torrc, service_log)

    assert "EXIT:0" in result.stdout, result.stdout + result.stderr
    assert "Active %include" in result.stdout
    assert torrc.read_text() == original
    assert _service_calls(service_log) == []


def test_invalid_candidate_leaves_original_unchanged(tmp_path: Path) -> None:
    torrc = tmp_path / "torrc"
    service_log = tmp_path / "service.log"
    original = "# Existing Tor configuration\n"
    torrc.write_text(original)

    result = _configure_torrc(torrc, service_log, verify_config=False)

    assert "EXIT:1" in result.stdout, result.stdout + result.stderr
    assert "rejected the proposed configuration" in result.stdout
    assert torrc.read_text() == original
    assert _service_calls(service_log) == []


def test_restart_failure_restores_original_and_retries_service(tmp_path: Path) -> None:
    torrc = tmp_path / "torrc"
    service_log = tmp_path / "service.log"
    original = "# Existing Tor configuration\n"
    torrc.write_text(original)

    result = _configure_torrc(torrc, service_log, restart_config=False)

    assert "EXIT:1" in result.stdout, result.stdout + result.stderr
    assert "restoring the previous configuration" in result.stdout
    assert torrc.read_text() == original
    assert _service_calls(service_log) == ["restart tor", "restart tor"]


def _tor_probe_python() -> str:
    """Exercise the standalone installer payload without requiring installed jmcore."""
    function = INSTALL_SH.read_text().split("tor_control_accessible() {", 1)[1]
    return function.split("<<'PY' 2>/dev/null\n", 1)[1].split("\nPY\n", 1)[0]


@pytest.mark.parametrize(
    "reply, status", [(b"250 OK\r\n", 0), (b"515 Bad cookie\r\n", 1), (b"", 1)]
)
@pytest.mark.parametrize(
    "cookie_path",
    ["/run/tor/control.authcookie", "/var/lib/tor/control_auth_cookie"],
)
def test_control_probe_authenticates_with_readable_cookie(
    tmp_path: Path, reply: bytes, status: int, cookie_path: str
) -> None:
    cookie = bytes(range(32))
    cookie_file = tmp_path / "cookie"
    cookie_file.write_bytes(cookie)
    payload = _tor_probe_python()
    real_path = Path
    client, server = socket.socketpair()

    def resolve_cookie(name: str) -> Path:
        return cookie_file if name == cookie_path else real_path(tmp_path / "missing")

    with server:
        server.sendall(reply)
        server.shutdown(socket.SHUT_WR)
        with (
            patch("pathlib.Path", side_effect=resolve_cookie),
            patch("socket.create_connection", return_value=client) as connect,
            pytest.raises(SystemExit) as result,
        ):
            exec(payload, {})
        assert result.value.code == status
        connect.assert_called_once_with(("127.0.0.1", 9051), timeout=2)
        assert server.recv(512) == b"AUTHENTICATE " + cookie.hex().encode() + b"\r\n"


@pytest.mark.parametrize(
    "failure", ["missing", "empty", "invalid", "unreadable", "refused", "timeout"]
)
def test_control_probe_failure_is_conservative(tmp_path: Path, failure: str) -> None:
    cookie_file = tmp_path / "cookie"
    if failure != "missing":
        cookie_file.write_bytes(
            b"" if failure == "empty" else b"a" * (31 if failure == "invalid" else 32)
        )
    payload = _tor_probe_python()
    with (
        patch("pathlib.Path", return_value=cookie_file),
        patch(
            "socket.create_connection",
            side_effect=TimeoutError()
            if failure == "timeout"
            else ConnectionRefusedError(),
        ) as connect,
    ):
        if failure == "unreadable":
            with (
                patch.object(Path, "read_bytes", side_effect=PermissionError()),
                pytest.raises(SystemExit) as result,
            ):
                exec(payload, {})
        else:
            with pytest.raises(SystemExit) as result:
                exec(payload, {})
    assert result.value.code == 1
    if failure not in {"refused", "timeout"}:
        connect.assert_not_called()


@pytest.mark.parametrize("accessible", [True, False])
def test_setup_checks_access_before_system_configuration(accessible: bool) -> None:
    script = f"""
source {shlex.quote(str(INSTALL_SH))}
tor_control_accessible() {{ return {0 if accessible else 1}; }}
detect_os() {{ printf 'FALLBACK_SETUP\n'; exit 0; }}
sudo() {{ printf 'UNEXPECTED_SUDO\n'; exit 1; }}
setup_tor
"""
    result = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=10, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "UNEXPECTED_SUDO" not in result.stdout
    assert ("existing Tor configuration can be kept" in result.stdout) is accessible
    assert ("FALLBACK_SETUP" in result.stdout) is not accessible
