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
from jmswap.lnd_invoices import HoldInvoiceStatus
from jmswap.lnd_peer import InvoiceState
from nacl.public import PrivateKey

import taker.wallet_market as wallet_market
from jmcore.bitcoin import hash160
from jmcore.btc_script import derive_bond_address
from jmcore.constants import GENESIS_BLOCK_HASHES
from jmcore.credential_market import BondCredential, MarketError, SignedDocument
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
from taker.market_service import open_delivery
from taker.wallet_market import WalletMarketSeller

_MNEMONIC = "all " * 11 + "all"
_WALLET_NAME = "seller.jmdat"
_LOCKTIME = 1_893_456_000
_BOND_VALUE = 100_000
_PRICE_SATS = 100
_CHAIN_HEIGHT = 2017


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
        self.get_block_height = AsyncMock(side_effect=self._get_block_height)
        self.get_median_time_past = AsyncMock(return_value=1_700_000_000)
        self.verify_bonds = AsyncMock(side_effect=self._verify_bonds)
        self.get_utxo = AsyncMock(side_effect=self._get_utxo)
        self.requires_neutrino_metadata = MagicMock(return_value=False)
        self.onchain_utxos: dict[tuple[str, int], UTXO | None] = {}
        self.chain_lookup_entered: asyncio.Event | None = None
        self.chain_lookup_release: asyncio.Event | None = None

    async def _gate_chain_lookup(self) -> None:
        """Hold the first awaited chain read so a test can interrupt settlement."""
        if self.chain_lookup_entered is not None:
            self.chain_lookup_entered.set()
            assert self.chain_lookup_release is not None
            await self.chain_lookup_release.wait()

    async def _get_block_height(self) -> int:
        await self._gate_chain_lookup()
        return _CHAIN_HEIGHT

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
        await self._gate_chain_lookup()
        return self.onchain_utxos.get((txid, vout))


class _FakeHoldInvoices:
    """In-memory LND hold-invoice node; the seller never reaches a real one."""

    def __init__(self) -> None:
        self.invoices: dict[bytes, HoldInvoiceStatus] = {}

    async def __aenter__(self) -> _FakeHoldInvoices:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def add_hold_invoice(
        self, payment_hash: bytes, amount_sats: int, expiry_seconds: int
    ) -> str:
        existing = self.invoices.get(payment_hash)
        if existing is not None:
            return existing.payment_request
        invoice = Bolt11(
            currency="bcrt",
            date=int(time.time()),
            amount_msat=MilliSatoshi(amount_sats * 1_000),
            tags=Tags(
                [
                    Tag(TagChar.payment_hash, payment_hash.hex()),
                    Tag(TagChar.payment_secret, "22" * 32),
                    Tag(TagChar.description, ""),
                    Tag(TagChar.expire_time, expiry_seconds),
                    Tag(TagChar.min_final_cltv_expiry, 18),
                ]
            ),
        )
        request = encode(invoice, private_key="11" * 32)
        self.invoices[payment_hash] = HoldInvoiceStatus(InvoiceState.OPEN, request, 0, None)
        return request

    async def lookup(self, payment_hash: bytes) -> HoldInvoiceStatus | None:
        return self.invoices.get(payment_hash)

    async def settle(self, preimage: bytes) -> None:
        payment_hash = hashlib.sha256(preimage).digest()
        assert self.invoices[payment_hash].state is InvoiceState.ACCEPTED
        self._set(payment_hash, InvoiceState.SETTLED)

    async def cancel(self, payment_hash: bytes) -> None:
        assert self.invoices[payment_hash].state is not InvoiceState.SETTLED
        self._set(payment_hash, InvoiceState.CANCELED)

    def pay(self, quote: SignedDocument, amount_sats: int = _PRICE_SATS) -> bytes:
        """Accept the buyer HTLC for the quote's invoice and return its payment hash."""
        request = str(quote.body["payment"]["request"])
        [payment_hash] = [
            key for key, value in self.invoices.items() if value.payment_request == request
        ]
        self.invoices[payment_hash] = HoldInvoiceStatus(
            InvoiceState.ACCEPTED, request, amount_sats, _CHAIN_HEIGHT + 100
        )
        return payment_hash

    def _set(self, payment_hash: bytes, state: InvoiceState) -> None:
        status = self.invoices[payment_hash]
        self.invoices[payment_hash] = HoldInvoiceStatus(
            state, status.payment_request, status.amount_paid_sats, None
        )


_INVOICES: list[_FakeHoldInvoices] = []


async def _settle_pass(seller: WalletMarketSeller) -> None:
    assert seller._settlement is not None
    await seller._settlement.reconcile_once()


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


async def _quote_response(
    seller: WalletMarketSeller,
    buyer_key: PrivateKey,
    *,
    product: str,
    rail: str,
    request_id: str,
    certificate_pubkey: str | None = None,
) -> dict[str, object]:
    return await seller._respond_callback(
        "synthetic-buyer",
        {
            "action": "quote",
            "request_id": request_id,
            "buyer_pubkey": bytes(buyer_key.public_key).hex(),
            "product": product,
            "certificate_pubkey": certificate_pubkey,
            "rail": rail,
            "max_price_sats": _PRICE_SATS,
        },
    )


async def _quote(
    seller: WalletMarketSeller,
    buyer_key: PrivateKey,
    *,
    product: str,
    rail: str,
    request_id: str,
    certificate_pubkey: str | None = None,
) -> SignedDocument:
    response = await _quote_response(
        seller,
        buyer_key,
        product=product,
        rail=rail,
        request_id=request_id,
        certificate_pubkey=certificate_pubkey,
    )
    assert "quote" in response
    return SignedDocument.model_validate(response["quote"])


@pytest.fixture(autouse=True)
def _mock_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    _MemoryTransport.instances.clear()
    monkeypatch.setattr(wallet_market, "MarketTransport", _MemoryTransport)
    _INVOICES.clear()

    def _invoice_client(_settings: JoinMarketSettings) -> _FakeHoldInvoices:
        _INVOICES.append(_FakeHoldInvoices())
        return _INVOICES[-1]

    async def _manual_settlement(_self: object, interval: float = 0) -> None:
        # Tests drive settlement passes explicitly through _settle_pass.
        return None

    monkeypatch.setattr(wallet_market, "seller_invoice_client", _invoice_client)
    monkeypatch.setattr(wallet_market.MarketSettlement, "run", _manual_settlement)


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
        assert inventory.json() == {"recorded": True}

        # Manual payment queueing and settlement endpoints no longer exist.
        for removed in ("payments", "settle/" + "01" * 32):
            response = await client.post(
                f"/api/v1/wallet/{_WALLET_NAME}/market/seller/{removed}",
                headers=_headers(token),
                json={},
            )
            assert response.status_code in (404, 405)

        # A non-Lightning rail is never quoted.
        assert await _quote_response(
            seller,
            buyer_key,
            product="bond",
            rail="onchain",
            request_id="02" * 16,
            certificate_pubkey=renter_certificate,
        ) == {"error": "unavailable"}

        quote_document = await _quote(
            seller,
            buyer_key,
            product="bond",
            rail="lightning",
            request_id="01" * 16,
            certificate_pubkey=renter_certificate,
        )
        quote_id = str(quote_document.body["quote_id"])
        pending = await client.get(
            f"/api/v1/wallet/{_WALLET_NAME}/market/seller/pending", headers=_headers(token)
        )
        assert pending.status_code == 200
        assert [SignedDocument.model_validate(item) for item in pending.json()] == [quote_document]

        # Nothing is delivered before the buyer's payment is held and settled.
        await _settle_pass(seller)
        assert seller._service is not None
        assert seller._service.store.get_delivery(quote_id) is None

        [invoices] = _INVOICES
        payment_hash = invoices.pay(quote_document)
        await _settle_pass(seller)
        assert invoices.invoices[payment_hash].state is InvoiceState.SETTLED
        package = seller._service.store.get_package(quote_id)
        assert package is not None
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
async def test_underpaid_hold_invoice_is_refunded_and_inventory_returns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wallet, backend, outpoint = _wallet(tmp_path)
    _activate_ledger(wallet)
    state, token = _state(wallet)
    monkeypatch.setattr("jmwalletd.routers.market.get_settings", lambda: _settings(tmp_path))
    transport = _app(state)
    credential = _external_podle(1)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        seller = await _start_seller(client, state, token, outpoint, ["podle"])
        recorded = await client.post(
            f"/api/v1/wallet/{_WALLET_NAME}/market/seller/inventory",
            headers=_headers(token),
            json={"product": "podle", "credential": credential.model_dump(mode="json")},
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
        [invoices] = _INVOICES
        payment_hash = invoices.pay(quote_document, _PRICE_SATS - 1)

        await _settle_pass(seller)
        assert invoices.invoices[payment_hash].state is InvoiceState.CANCELED
        assert seller._service is not None
        assert seller._service.store.get_delivery(quote_id) is None
        # The opening is for sale again (bypass the per-seller quote pacing).
        seller._service._next_quote = 0
        await _quote(
            seller,
            PrivateKey.generate(),
            product="podle",
            rail="lightning",
            request_id="45" * 16,
        )
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
@pytest.mark.parametrize("interruption", ["stop", "lock"])
async def test_settlement_cannot_finalize_after_lifecycle_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interruption: str
) -> None:
    wallet, backend, outpoint = _wallet(tmp_path)
    wallet_id = wallet.market_wallet_id
    _activate_ledger(wallet)
    state, token = _state(wallet)
    monkeypatch.setattr("jmwalletd.routers.market.get_settings", lambda: _settings(tmp_path))
    transport = _app(state)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        seller = await _start_seller(client, state, token, outpoint, ["bond"])
        recorded = await client.post(
            f"/api/v1/wallet/{_WALLET_NAME}/market/seller/inventory",
            headers=_headers(token),
            json={"product": "bond"},
        )
        assert recorded.status_code == 200
        quote_document = await _quote(
            seller,
            PrivateKey.generate(),
            product="bond",
            rail="lightning",
            request_id="55" * 16,
            certificate_pubkey=bytes(CKey(b"\x05" * 32).pub).hex(),
        )
        quote_id = str(quote_document.body["quote_id"])
        [invoices] = _INVOICES
        payment_hash = invoices.pay(quote_document)
        # Hold the first awaited chain read inside settlement, then interrupt.
        backend.chain_lookup_entered = asyncio.Event()
        backend.chain_lookup_release = asyncio.Event()
        settle_task = asyncio.create_task(_settle_pass(seller))
        await backend.chain_lookup_entered.wait()

        if interruption == "stop":
            await _stop_seller(state)
        else:
            assert await state.lock_wallet() is False
        backend.chain_lookup_release.set()
        await settle_task

        # The payment stays held (a restarted seller can still settle or refund it).
        assert invoices.invoices[payment_hash].state is InvoiceState.ACCEPTED
        with MarketStore(
            get_market_store_path(tmp_path), wallet_id=wallet_id, create=False
        ) as store:
            assert store.get_delivery(quote_id) is None
            assert store.get_package(quote_id) is None

    backend.close.assert_not_awaited()
    backend.broadcast_transaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_additional_bond_authority_sells_a_lease_of_its_own_bond(
    tmp_path: Path,
) -> None:
    """One runtime serves two bonds; each hold settles under the authority that quoted it."""
    wallet, _backend, advertised = _wallet(tmp_path)
    idle_locktime = _LOCKTIME + 31 * 86_400  # the next month start
    idle = ExternalPoDLEOutpoint(txid="22" * 32, vout=0)
    idle_key = wallet.get_fidelity_bond_key(0, idle_locktime)
    address = derive_bond_address(
        idle_key.get_public_key_bytes(compressed=True), idle_locktime, "regtest"
    )
    wallet.utxo_cache[0].append(
        UTXOInfo(
            txid=idle.txid,
            vout=idle.vout,
            value=_BOND_VALUE,
            address=address.address,
            confirmations=6,
            scriptpubkey=address.scriptpubkey.hex(),
            path=f"{wallet.root_path}/0'/2/{timestamp_to_timenumber(idle_locktime)}",
            mixdepth=0,
            locktime=idle_locktime,
            frozen=True,
        )
    )
    _activate_ledger(wallet)
    options = wallet_market.WalletMarketSellerOptions
    seller = WalletMarketSeller(
        wallet,
        _settings(tmp_path),
        options(bond=advertised, products=["podle"], price_sats=_PRICE_SATS),
        additional=[options(bond=idle, products=["bond"], price_sats=_PRICE_SATS)],
    )
    with pytest.raises(ValueError, match="own bond"):
        WalletMarketSeller(
            wallet,
            _settings(tmp_path),
            options(bond=advertised, products=["podle"], price_sats=1),
            additional=[options(bond=advertised, products=["bond"], price_sats=1)],
        )
    try:
        await seller.start()
        # Each authority relays under its own nick.
        assert len(_MemoryTransport.instances) == 2
        [record] = seller._additional
        assert record.transport is _MemoryTransport.instances[1]
        with pytest.raises(MarketError, match="Bond product"):
            seller.add_bond_inventory()  # the first authority sells PoDLEs only
        seller.add_bond_inventory(idle)

        renter_certificate = bytes(CKey(b"\x03" * 32).pub).hex()
        responder = _MemoryTransport.instances[1].kwargs["responder"]
        response = await responder(  # type: ignore[operator]
            "synthetic-buyer",
            {
                "action": "quote",
                "request_id": "01" * 16,
                "buyer_pubkey": bytes(PrivateKey.generate().public_key).hex(),
                "product": "bond",
                "certificate_pubkey": renter_certificate,
                "rail": "lightning",
                "max_price_sats": _PRICE_SATS,
            },
        )
        quote = SignedDocument.model_validate(response["quote"])
        assert seller.pending_quotes() == [quote]

        [invoices] = _INVOICES
        payment_hash = invoices.pay(quote)
        await _settle_pass(seller)
        assert invoices.invoices[payment_hash].state is InvoiceState.SETTLED
        assert seller._service is not None
        package = seller._service.store.get_package(str(quote.body["quote_id"]))
        assert package is not None
        credential = package.verify()
        assert isinstance(credential, BondCredential)
        lease = credential.verify()
        assert credential.cert_pubkey == renter_certificate
        assert lease.bond == credential.bond
        assert (credential.bond.outpoint, credential.bond.locktime) == (idle, idle_locktime)

        await seller.stop()
        assert [transport.close_calls for transport in _MemoryTransport.instances] == [1, 1]
    finally:
        await seller.stop()
        await wallet.close()
