"""End-to-end daemon API coverage for native credential-market seller operations."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from bitcointx.core.key import CKey
from nacl.public import PrivateKey

import taker.wallet_market as wallet_market
from jmcore.bitcoin import address_to_scriptpubkey_for_network, hash160, pubkey_to_p2wpkh_address
from jmcore.btc_script import derive_bond_address
from jmcore.constants import GENESIS_BLOCK_HASHES
from jmcore.credential_market import BondCredential, CredentialPackage, PaymentTerms, SignedDocument
from jmcore.external_podle import ExternalPoDLE, ExternalPoDLEOutpoint
from jmcore.market_store import MarketStore, MarketStoreError
from jmcore.models import NetworkType
from jmcore.paths import get_market_store_path, get_used_commitments_path
from jmcore.podle import generate_podle
from jmcore.settings import JoinMarketSettings, NetworkSettings, TorSettings
from jmcore.timenumber import timestamp_to_timenumber
from jmwallet.backends.base import UTXO, BondVerificationRequest, BondVerificationResult
from jmwallet.wallet.models import UTXOInfo
from jmwallet.wallet.service import WalletService
from jmwalletd.app import create_app
from jmwalletd.deps import set_daemon_state
from jmwalletd.state import DaemonState
from taker._vendor.bolt11 import Bolt11, MilliSatoshi, Tag, TagChar, Tags, encode
from taker.market_service import QuoteRequest, open_delivery
from taker.wallet_market import WalletMarketSeller

_MNEMONIC = "all " * 11 + "all"
_WALLET_NAME = "seller.jmdat"
_LOCKTIME = 1_893_456_000
_BOND_VALUE = 100_000
_PRICE_SATS = 100


class _MemoryTransport:
    """Transport boundary replacement that cannot create a socket."""

    instances: list[_MemoryTransport] = []

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.nick = "J5ABCDEFGHJKLMNP"
        self.close_calls = 0
        type(self).instances.append(self)

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        self.close_calls += 1


class _SyntheticRegtestBackend:
    """Minimal authoritative chain view for native seller API tests."""

    def __init__(self) -> None:
        self.close = AsyncMock()
        self.broadcast_transaction = AsyncMock(side_effect=AssertionError("seller must not send"))
        self.get_block_hash = AsyncMock(return_value=GENESIS_BLOCK_HASHES["regtest"])
        self.get_block_height = AsyncMock(return_value=2017)
        self.get_median_time_past = AsyncMock(return_value=1_700_000_000)
        self.verify_bonds = AsyncMock(side_effect=self._verify_bonds)
        self.get_utxo = AsyncMock(side_effect=self._get_utxo)
        self.requires_neutrino_metadata = MagicMock(return_value=False)
        self.onchain_utxos: dict[tuple[str, int], UTXO | None] = {}
        self.payment_lookup_entered: asyncio.Event | None = None
        self.payment_lookup_release: asyncio.Event | None = None

    async def _verify_bonds(
        self, bonds: list[BondVerificationRequest]
    ) -> list[BondVerificationResult]:
        results: list[BondVerificationResult] = []
        for bond in bonds:
            results.append(
                BondVerificationResult(
                    txid=bond.txid,
                    vout=bond.vout,
                    value=_BOND_VALUE,
                    confirmations=6,
                    block_time=1_700_000_000,
                    valid=True,
                )
            )
        return results

    async def _get_utxo(self, txid: str, vout: int) -> UTXO | None:
        if self.payment_lookup_entered is not None:
            self.payment_lookup_entered.set()
            assert self.payment_lookup_release is not None
            await self.payment_lookup_release.wait()
        return self.onchain_utxos.get((txid, vout))


def _settings(data_dir: Path) -> JoinMarketSettings:
    """Use mainnet messaging with a regtest wallet and Bitcoin chain."""
    return JoinMarketSettings(
        data_dir=data_dir,
        network_config=NetworkSettings(
            network=NetworkType.MAINNET,
            bitcoin_network=NetworkType.REGTEST,
            directory_servers=[],
        ),
        tor=TorSettings(connection_timeout=0.2),
    )


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _seller_options(outpoint: ExternalPoDLEOutpoint, products: list[str]) -> dict[str, object]:
    return {
        "bond": outpoint.model_dump(mode="json"),
        "products": products,
        "price_sats": _PRICE_SATS,
        "quote_ttl": 120,
    }


def _payment_terms(index: int, *, amount_sats: int = _PRICE_SATS) -> PaymentTerms:
    payment_key = CKey(bytes([0x40 + index]) * 32)
    return PaymentTerms(
        rail="onchain",
        request=pubkey_to_p2wpkh_address(bytes(payment_key.pub), "regtest"),
        amount_sats=amount_sats,
        min_confirmations=2,
    )


def _lightning_terms(preimage: bytes) -> PaymentTerms:
    now = int(time.time())
    invoice = Bolt11(
        currency="bcrt",
        date=now - 1,
        amount_msat=MilliSatoshi(_PRICE_SATS * 1_000),
        tags=Tags(
            [
                Tag(TagChar.payment_hash, hashlib.sha256(preimage).hexdigest()),
                Tag(TagChar.payment_secret, "22" * 32),
                Tag(TagChar.description, "native seller operation test"),
                Tag(TagChar.expire_time, 600),
                Tag(TagChar.min_final_cltv_expiry, 18),
            ]
        ),
    )
    return PaymentTerms(
        rail="lightning",
        request=encode(invoice, private_key="11" * 32),
        amount_sats=_PRICE_SATS,
        min_confirmations=1,
    )


def _external_podle(index: int, *, network: str = "regtest") -> ExternalPoDLE:
    txid = f"{0x80 + index:02x}" * 32
    proof = generate_podle(bytes([0x20 + index]) * 32, f"{txid}:1", index=0)
    return ExternalPoDLE(
        version=1,
        network=network,
        outpoint=ExternalPoDLEOutpoint(txid=txid, vout=1),
        P=proof.p.hex(),
        P2=proof.p2.hex(),
        sig=proof.sig.hex(),
        e=proof.e.hex(),
        commitment=proof.commitment.hex(),
        index=0,
        scriptpubkey=(b"\x00\x14" + hash160(proof.p)).hex(),
        blockheight=100,
    )


def _wallet(
    data_dir: Path,
) -> tuple[WalletService, _SyntheticRegtestBackend, ExternalPoDLEOutpoint]:
    backend = _SyntheticRegtestBackend()
    wallet = WalletService(_MNEMONIC, backend, network="regtest", data_dir=data_dir)
    outpoint = ExternalPoDLEOutpoint(txid="11" * 32, vout=0)
    bond_key = wallet.get_fidelity_bond_key(0, _LOCKTIME)
    address = derive_bond_address(
        bond_key.get_public_key_bytes(compressed=True), _LOCKTIME, "regtest"
    )
    wallet.utxo_cache[0] = [
        UTXOInfo(
            txid=outpoint.txid,
            vout=outpoint.vout,
            value=_BOND_VALUE,
            address=address.address,
            confirmations=6,
            scriptpubkey=address.scriptpubkey.hex(),
            path=f"{wallet.root_path}/0'/2/{timestamp_to_timenumber(_LOCKTIME)}",
            mixdepth=0,
            locktime=_LOCKTIME,
            frozen=True,
        )
    ]
    wallet.sync_all = AsyncMock(side_effect=AssertionError("seller must not sync"))  # type: ignore[method-assign]
    wallet.sync_mixdepth = AsyncMock(side_effect=AssertionError("seller must not sync"))  # type: ignore[method-assign]
    return wallet, backend, outpoint


def _activate_ledger(wallet: WalletService) -> None:
    commitments = get_used_commitments_path(wallet.data_dir)
    commitments.parent.mkdir(parents=True, exist_ok=True)
    commitments.write_text(json.dumps({"external_v1": {}, "used": []}), encoding="ascii")
    wallet.activate_market_ledger(history_confirmed=True)


def _state(wallet: WalletService) -> tuple[DaemonState, str]:
    state = DaemonState(data_dir=wallet.data_dir)
    state.wallet_service = wallet
    state.wallet_name = _WALLET_NAME
    return state, state.token_authority.issue(_WALLET_NAME).token


def _app(state: DaemonState) -> httpx.ASGITransport:
    app = create_app(data_dir=state.data_dir)
    set_daemon_state(state)
    return httpx.ASGITransport(app=app)


async def _start_seller(
    client: httpx.AsyncClient,
    state: DaemonState,
    token: str,
    outpoint: ExternalPoDLEOutpoint,
    products: list[str],
) -> WalletMarketSeller:
    response = await client.post(
        f"/api/v1/wallet/{_WALLET_NAME}/market/seller/start",
        headers=_headers(token),
        json=_seller_options(outpoint, products),
    )
    assert response.status_code == 202
    task = state._market_seller_task
    if task is not None:
        await task
    seller = state._market_seller_ref
    assert isinstance(seller, WalletMarketSeller)
    assert seller.running
    return seller


async def _stop_seller(state: DaemonState) -> None:
    async with state.wallet_lifecycle_lock:
        await state.stop_market_seller()


async def _quote(
    seller: WalletMarketSeller,
    buyer_key: PrivateKey,
    *,
    product: str,
    rail: str,
    request_id: str,
    certificate_pubkey: str | None = None,
) -> SignedDocument:
    response = await seller._respond_callback(
        "synthetic-buyer",
        QuoteRequest(
            request_id=request_id,
            buyer_pubkey=bytes(buyer_key.public_key).hex(),
            product=product,  # type: ignore[arg-type]
            certificate_pubkey=certificate_pubkey,
            rail=rail,  # type: ignore[arg-type]
            max_price_sats=_PRICE_SATS,
        ).model_dump(mode="json"),
    )
    assert "quote" in response
    return SignedDocument.model_validate(response["quote"])


@pytest.fixture(autouse=True)
def _mock_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    _MemoryTransport.instances.clear()
    monkeypatch.setattr(wallet_market, "MarketTransport", _MemoryTransport)


@pytest.mark.asyncio
async def test_authenticated_bond_sale_finalizes_and_persists_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wallet, backend, outpoint = _wallet(tmp_path)
    _activate_ledger(wallet)
    state, token = _state(wallet)
    settings = _settings(tmp_path)
    monkeypatch.setattr("jmwalletd.routers.market.get_settings", lambda: settings)
    transport = _app(state)
    terms = _payment_terms(1)
    buyer_key = PrivateKey.generate()
    renter_certificate = bytes(CKey(b"\x03" * 32).pub).hex()

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        seller = await _start_seller(client, state, token, outpoint, ["bond"])
        assert _MemoryTransport.instances[0].kwargs["network"] == "mainnet"

        inventory = await client.post(
            f"/api/v1/wallet/{_WALLET_NAME}/market/seller/inventory",
            headers=_headers(token),
            json={"product": "bond"},
        )
        payment = await client.post(
            f"/api/v1/wallet/{_WALLET_NAME}/market/seller/payments",
            headers=_headers(token),
            json=terms.model_dump(mode="json"),
        )
        assert inventory.json() == payment.json() == {"recorded": True}

        quote_document = await _quote(
            seller,
            buyer_key,
            product="bond",
            rail="onchain",
            request_id="01" * 16,
            certificate_pubkey=renter_certificate,
        )
        quote_id = str(quote_document.body["quote_id"])
        pending = await client.get(
            f"/api/v1/wallet/{_WALLET_NAME}/market/seller/pending", headers=_headers(token)
        )
        assert pending.status_code == 200
        assert [SignedDocument.model_validate(item) for item in pending.json()] == [quote_document]

        txid = "ab" * 32
        backend.onchain_utxos[(txid, 0)] = UTXO(
            txid=txid,
            vout=0,
            value=terms.amount_sats,
            address=terms.request,
            confirmations=terms.min_confirmations,
            scriptpubkey=address_to_scriptpubkey_for_network(terms.request, "regtest").hex(),
        )
        settled = await client.post(
            f"/api/v1/wallet/{_WALLET_NAME}/market/seller/settle/{quote_id}",
            headers=_headers(token),
            json={"onchain_outpoint": f"{txid}:0"},
        )
        assert settled.status_code == 200
        package = CredentialPackage.model_validate(settled.json())
        credential = package.verify()
        assert isinstance(credential, BondCredential)
        assert credential.cert_pubkey == renter_certificate
        credential.verify()

        delivery = await seller._respond_callback(
            "synthetic-buyer", {"action": "delivery", "quote_id": quote_id}
        )
        opened = open_delivery(str(delivery["delivery"]), buyer_key, quote_document)
        assert opened == package

        stopped = await client.post(
            f"/api/v1/wallet/{_WALLET_NAME}/market/seller/stop", headers=_headers(token)
        )
        assert stopped.status_code == 200

    with MarketStore(
        get_market_store_path(tmp_path), wallet_id=wallet.market_wallet_id, create=False
    ) as store:
        assert store.get_package(quote_id) == package
        assert store.get_delivery(quote_id) == delivery["delivery"]
        inventory_state = store._connection.execute(
            "SELECT state FROM inventory WHERE reservation_quote_id = ?", (quote_id,)
        ).fetchone()
        assert inventory_state is not None and inventory_state["state"] == "consumed"
    backend.close.assert_not_awaited()
    backend.broadcast_transaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_onchain_settlement_rejects_wrong_output_amount_and_confirmations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wallet, backend, outpoint = _wallet(tmp_path)
    _activate_ledger(wallet)
    state, token = _state(wallet)
    monkeypatch.setattr("jmwalletd.routers.market.get_settings", lambda: _settings(tmp_path))
    transport = _app(state)

    cases = [
        ("wrong-output", _PRICE_SATS, 2, 1),
        ("wrong-amount", _PRICE_SATS - 1, 2, 0),
        ("not-confirmed", _PRICE_SATS, 1, 0),
    ]
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        seller = await _start_seller(client, state, token, outpoint, ["podle"])
        quote_ids: list[str] = []
        for index, (case, value, confirmations, returned_vout) in enumerate(cases, start=1):
            credential = _external_podle(index)
            terms = _payment_terms(index + 10)
            for endpoint, payload in (
                (
                    "inventory",
                    {"product": "podle", "credential": credential.model_dump(mode="json")},
                ),
                ("payments", terms.model_dump(mode="json")),
            ):
                recorded = await client.post(
                    f"/api/v1/wallet/{_WALLET_NAME}/market/seller/{endpoint}",
                    headers=_headers(token),
                    json=payload,
                )
                assert recorded.status_code == 200, case
            if seller._service is not None:
                seller._service._next_quote = 0.0
            quote_document = await _quote(
                seller,
                PrivateKey.generate(),
                product="podle",
                rail="onchain",
                request_id=f"{index:02x}" * 16,
            )
            quote_id = str(quote_document.body["quote_id"])
            quote_ids.append(quote_id)
            txid = f"{0xA0 + index:02x}" * 32
            backend.onchain_utxos[(txid, 0)] = UTXO(
                txid=txid,
                vout=returned_vout,
                value=value,
                address=terms.request,
                confirmations=confirmations,
                scriptpubkey=address_to_scriptpubkey_for_network(terms.request, "regtest").hex(),
            )
            rejected = await client.post(
                f"/api/v1/wallet/{_WALLET_NAME}/market/seller/settle/{quote_id}",
                headers=_headers(token),
                json={"onchain_outpoint": f"{txid}:0"},
            )
            assert rejected.status_code == 400, case

        assert seller._service is not None
        assert all(seller._service.store.get_delivery(quote_id) is None for quote_id in quote_ids)
        await _stop_seller(state)

    backend.close.assert_not_awaited()
    backend.broadcast_transaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_lightning_settlement_needs_local_acknowledgement_and_matching_preimage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wallet, backend, outpoint = _wallet(tmp_path)
    _activate_ledger(wallet)
    state, token = _state(wallet)
    monkeypatch.setattr("jmwalletd.routers.market.get_settings", lambda: _settings(tmp_path))
    transport = _app(state)
    credential = _external_podle(1)
    preimage = bytes(range(32))

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        seller = await _start_seller(client, state, token, outpoint, ["podle"])
        for endpoint, payload in (
            ("inventory", {"product": "podle", "credential": credential.model_dump(mode="json")}),
            ("payments", _lightning_terms(preimage).model_dump(mode="json")),
        ):
            recorded = await client.post(
                f"/api/v1/wallet/{_WALLET_NAME}/market/seller/{endpoint}",
                headers=_headers(token),
                json=payload,
            )
            assert recorded.status_code == 200
        quote_document = await _quote(
            seller,
            PrivateKey.generate(),
            product="podle",
            rail="lightning",
            request_id="44" * 16,
        )
        quote_id = str(quote_document.body["quote_id"])

        no_ack = await client.post(
            f"/api/v1/wallet/{_WALLET_NAME}/market/seller/settle/{quote_id}",
            headers=_headers(token),
            json={"preimage": preimage.hex()},
        )
        wrong_preimage = await client.post(
            f"/api/v1/wallet/{_WALLET_NAME}/market/seller/settle/{quote_id}",
            headers=_headers(token),
            json={"preimage": (b"x" * 32).hex(), "acknowledge_ln_settlement": True},
        )
        assert no_ack.status_code == 422
        assert wrong_preimage.status_code == 400
        assert seller._service is not None
        assert seller._service.store.get_delivery(quote_id) is None
        await _stop_seller(state)

    backend.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_inventory_rejects_wrong_network_and_locally_held_podle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wallet, backend, outpoint = _wallet(tmp_path)
    _activate_ledger(wallet)
    state, token = _state(wallet)
    monkeypatch.setattr("jmwalletd.routers.market.get_settings", lambda: _settings(tmp_path))
    transport = _app(state)
    credential = _external_podle(2)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await _start_seller(client, state, token, outpoint, ["podle"])
        wrong_network = await client.post(
            f"/api/v1/wallet/{_WALLET_NAME}/market/seller/inventory",
            headers=_headers(token),
            json={
                "product": "podle",
                "credential": credential.model_dump(mode="json") | {"network": "mainnet"},
            },
        )
        with MarketStore(
            get_market_store_path(tmp_path), wallet_id=wallet.market_wallet_id
        ) as store:
            assert store.hold_local(credential.commitment)
        held_local = await client.post(
            f"/api/v1/wallet/{_WALLET_NAME}/market/seller/inventory",
            headers=_headers(token),
            json={"product": "podle", "credential": credential.model_dump(mode="json")},
        )
        assert wrong_network.status_code == held_local.status_code == 400
        await _stop_seller(state)

    backend.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_runtime_checks_commitments_binding_and_effective_bitcoin_network(
    tmp_path: Path,
) -> None:
    wrong_wallet, wrong_backend, outpoint = _wallet(tmp_path / "wrong-binding")
    wrong_path = tmp_path / "wrong-binding" / "other-commitments.json"
    wrong_path.parent.mkdir(parents=True, exist_ok=True)
    wrong_path.write_text(json.dumps({"external_v1": {}, "used": []}), encoding="ascii")
    with MarketStore(
        get_market_store_path(wrong_wallet.data_dir), wallet_id=wrong_wallet.market_wallet_id
    ) as store:
        store.activate_wallet(wrong_path, history_confirmed=True)
    wrong_seller = WalletMarketSeller(
        wrong_wallet,
        _settings(wrong_wallet.data_dir),
        wallet_market.WalletMarketSellerOptions.model_validate(_seller_options(outpoint, ["bond"])),
    )
    with pytest.raises(MarketStoreError, match="commitments"):
        await wrong_seller.start()
    await wrong_seller.stop()
    wrong_backend.close.assert_not_awaited()

    wallet, backend, outpoint = _wallet(tmp_path / "effective-bitcoin-network")
    _activate_ledger(wallet)
    seller = WalletMarketSeller(
        wallet,
        _settings(wallet.data_dir),
        wallet_market.WalletMarketSellerOptions.model_validate(_seller_options(outpoint, ["bond"])),
    )
    await seller.start()
    try:
        assert seller.running
        assert _MemoryTransport.instances[-1].kwargs["network"] == "mainnet"
    finally:
        await seller.stop()
    backend.close.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ["stop", "lock", "stale-token"])
async def test_settlement_cannot_finalize_after_lifecycle_change_or_return_to_stale_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interruption: str
) -> None:
    wallet, backend, outpoint = _wallet(tmp_path)
    wallet_id = wallet.market_wallet_id
    _activate_ledger(wallet)
    state, token = _state(wallet)
    monkeypatch.setattr("jmwalletd.routers.market.get_settings", lambda: _settings(tmp_path))
    transport = _app(state)
    terms = _payment_terms(30)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        seller = await _start_seller(client, state, token, outpoint, ["bond"])
        for endpoint, payload in (
            ("inventory", {"product": "bond"}),
            ("payments", terms.model_dump(mode="json")),
        ):
            recorded = await client.post(
                f"/api/v1/wallet/{_WALLET_NAME}/market/seller/{endpoint}",
                headers=_headers(token),
                json=payload,
            )
            assert recorded.status_code == 200
        quote_document = await _quote(
            seller,
            PrivateKey.generate(),
            product="bond",
            rail="onchain",
            request_id="55" * 16,
            certificate_pubkey=bytes(CKey(b"\x05" * 32).pub).hex(),
        )
        quote_id = str(quote_document.body["quote_id"])
        txid = "cd" * 32
        backend.onchain_utxos[(txid, 0)] = UTXO(
            txid=txid,
            vout=0,
            value=terms.amount_sats,
            address=terms.request,
            confirmations=terms.min_confirmations,
            scriptpubkey=address_to_scriptpubkey_for_network(terms.request, "regtest").hex(),
        )
        backend.payment_lookup_entered = asyncio.Event()
        backend.payment_lookup_release = asyncio.Event()
        settle_task = asyncio.create_task(
            client.post(
                f"/api/v1/wallet/{_WALLET_NAME}/market/seller/settle/{quote_id}",
                headers=_headers(token),
                json={"onchain_outpoint": f"{txid}:0"},
            )
        )
        await backend.payment_lookup_entered.wait()

        if interruption == "stop":
            await _stop_seller(state)
        elif interruption == "lock":
            assert await state.lock_wallet() is False
        else:
            state.token_authority.reset()
        backend.payment_lookup_release.set()
        response = await settle_task

        if interruption == "stale-token":
            assert response.status_code == 401
            assert seller._store is not None
            assert seller._store.get_delivery(quote_id) is not None
            await _stop_seller(state)
        else:
            assert response.status_code == 400
            with MarketStore(
                get_market_store_path(tmp_path), wallet_id=wallet_id, create=False
            ) as store:
                assert store.get_delivery(quote_id) is None

    backend.close.assert_not_awaited()
    backend.broadcast_transaction.assert_not_awaited()
