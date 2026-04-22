"""Pin operationIds emitted in /openapi.json.

The OpenAPI ``operationId`` is the contract every generated client (TypeScript,
Rust, Python, etc.) keys off when naming SDK functions. FastAPI's default
``<func>__<path>__<method>`` form is noisy and changes whenever a route is
renamed or moved, which would silently break downstream clients on every
refactor. We therefore pin a stable, hand-curated id on every public route and
guard against accidental drift here.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

# Map (HTTP method, path) -> expected operationId.
#
# Names mirror the long-standing ``joinmarket-clientserver`` wallet-rpc spec so
# that the Jam web UI keeps working with the same SDK calls. New routes added
# in joinmarket-ng (the tumbler suite) follow the same lowercase-noun
# convention.
EXPECTED_OPERATION_IDS: dict[tuple[str, str], str] = {
    # wallet.py
    ("get", "/api/v1/getinfo"): "version",
    ("get", "/api/v1/session"): "session",
    ("get", "/api/v1/wallet/all"): "listwallets",
    ("post", "/api/v1/wallet/create"): "createwallet",
    ("post", "/api/v1/wallet/recover"): "recoverwallet",
    ("post", "/api/v1/wallet/{walletname}/unlock"): "unlockwallet",
    ("get", "/api/v1/wallet/{walletname}/lock"): "lockwallet",
    ("post", "/api/v1/token"): "token",
    # wallet_data.py
    ("get", "/api/v1/wallet/{walletname}/display"): "displaywallet",
    ("get", "/api/v1/wallet/{walletname}/utxos"): "listutxos",
    ("get", "/api/v1/wallet/{walletname}/address/new/{mixdepth}"): "getaddress",
    (
        "get",
        "/api/v1/wallet/{walletname}/address/timelock/new/{lockdate}",
    ): "gettimelockaddress",
    ("get", "/api/v1/wallet/{walletname}/getseed"): "getseed",
    ("post", "/api/v1/wallet/{walletname}/freeze"): "freeze",
    ("post", "/api/v1/wallet/{walletname}/configget"): "configget",
    ("post", "/api/v1/wallet/{walletname}/configset"): "configsetting",
    (
        "get",
        "/api/v1/wallet/{walletname}/rescanblockchain/{blockheight}",
    ): "rescanblockchain",
    ("get", "/api/v1/wallet/{walletname}/getrescaninfo"): "getrescaninfo",
    ("post", "/api/v1/wallet/{walletname}/signmessage"): "signmessage",
    ("get", "/api/v1/wallet/yieldgen/report"): "yieldgenreport",
    # coinjoin.py
    ("post", "/api/v1/wallet/{walletname}/taker/direct-send"): "directsend",
    ("post", "/api/v1/wallet/{walletname}/taker/coinjoin"): "docoinjoin",
    ("get", "/api/v1/wallet/{walletname}/taker/stop"): "stopcoinjoin",
    ("post", "/api/v1/wallet/{walletname}/maker/start"): "startmaker",
    ("get", "/api/v1/wallet/{walletname}/maker/stop"): "stopmaker",
    # tumbler.py (new in jm-ng; clean names so SDKs read naturally)
    ("post", "/api/v1/wallet/{walletname}/tumbler/plan"): "tumblerplan",
    ("get", "/api/v1/wallet/{walletname}/tumbler/status"): "tumblerstatus",
    ("post", "/api/v1/wallet/{walletname}/tumbler/start"): "tumblerstart",
    ("post", "/api/v1/wallet/{walletname}/tumbler/stop"): "tumblerstop",
    ("delete", "/api/v1/wallet/{walletname}/tumbler/plan"): "tumblerplandelete",
}


def test_operation_ids_match_expected(app: TestClient) -> None:
    """Every documented route exposes the expected stable operationId."""
    resp = app.get("/openapi.json")
    assert resp.status_code == 200
    spec = resp.json()
    paths = spec["paths"]

    actual: dict[tuple[str, str], str] = {}
    for path, methods in paths.items():
        for method, op in methods.items():
            if method not in {"get", "post", "put", "delete", "patch"}:
                continue
            actual[(method, path)] = op.get("operationId", "")

    missing = {key: name for key, name in EXPECTED_OPERATION_IDS.items() if key not in actual}
    assert not missing, f"Routes disappeared from the API: {missing}"

    mismatched = {
        key: (actual[key], expected)
        for key, expected in EXPECTED_OPERATION_IDS.items()
        if actual[key] != expected
    }
    assert not mismatched, (
        "operationId drift detected (actual, expected): "
        f"{mismatched}. Update routers and EXPECTED_OPERATION_IDS together."
    )


def test_no_duplicate_operation_ids(app: TestClient) -> None:
    """Generated SDKs require globally-unique operationIds."""
    resp = app.get("/openapi.json")
    spec = resp.json()
    seen: dict[str, tuple[str, str]] = {}
    for path, methods in spec["paths"].items():
        for method, op in methods.items():
            if method not in {"get", "post", "put", "delete", "patch"}:
                continue
            op_id = op.get("operationId")
            if not op_id:
                continue
            if op_id in seen:
                raise AssertionError(
                    f"Duplicate operationId {op_id!r}: {seen[op_id]} and ({method}, {path})"
                )
            seen[op_id] = (method, path)
