"""Configuration responses and logs must not expose backend credentials."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from loguru import logger

from jmwalletd.deps import get_daemon_state


@pytest.fixture
def authed_client(app_with_wallet: TestClient, auth_token: str) -> tuple[TestClient, str]:
    return app_with_wallet, auth_token


@pytest.mark.parametrize("section", ["BITCOIN", "BLOCKCHAIN", "bitcoin"])
def test_neutrino_token_from_settings_is_masked(
    authed_client: tuple[TestClient, str], section: str
) -> None:
    client, token = authed_client
    settings = SimpleNamespace(bitcoin=SimpleNamespace(neutrino_auth_token="backend-secret"))
    with patch("jmcore.settings.get_settings", return_value=settings):
        response = client.post(
            "/api/v1/wallet/test_wallet.jmdat/configget",
            json={"section": section, "field": "NEUTRINO_AUTH_TOKEN"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 200
    assert response.json()["configvalue"] == "**********"
    assert "backend-secret" not in response.text


@pytest.mark.parametrize(
    "field",
    [
        "password",
        "rpc_password",
        "rpcpassword",
        "mnemonic_password",
        "bip39_passphrase",
        "neutrino_auth_token",
    ],
)
def test_secret_override_is_preserved_but_not_returned_or_logged(
    authed_client: tuple[TestClient, str], field: str
) -> None:
    client, token = authed_client
    headers = {"Authorization": f"Bearer {token}"}
    logs: list[str] = []
    sink_id = logger.add(lambda message: logs.append(str(message)), level="TRACE")
    try:
        response = client.post(
            "/api/v1/wallet/test_wallet.jmdat/configset",
            json={"section": "bitcoin", "field": field.upper(), "value": "private-value"},
            headers=headers,
        )
        assert response.status_code == 200
        response = client.post(
            "/api/v1/wallet/test_wallet.jmdat/configget",
            json={"section": "BITCOIN", "field": field},
            headers=headers,
        )
    finally:
        logger.remove(sink_id)
    assert response.status_code == 200
    assert response.json()["configvalue"] == "**********"
    assert get_daemon_state().config_overrides["BITCOIN"][field] == "private-value"
    assert "private-value" not in "".join(logs)
    assert "Config override updated" in "".join(logs)


def test_nonsecret_override_still_round_trips(authed_client: tuple[TestClient, str]) -> None:
    client, token = authed_client
    headers = {"Authorization": f"Bearer {token}"}
    response = client.post(
        "/api/v1/wallet/test_wallet.jmdat/configset",
        json={"section": "POLICY", "field": "tx_fees", "value": "7000"},
        headers=headers,
    )
    assert response.status_code == 200
    response = client.post(
        "/api/v1/wallet/test_wallet.jmdat/configget",
        json={"section": "POLICY", "field": "tx_fees"},
        headers=headers,
    )
    assert response.json()["configvalue"] == "7000"
