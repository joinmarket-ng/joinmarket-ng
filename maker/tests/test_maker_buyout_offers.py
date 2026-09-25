"""Publishing and propagating a prepared channel buyout on the maker side.

These cover the wiring an operator sees: what liquidity a bound buyout makes
the maker advertise, that an unusable buyout withdraws everything instead of
falling back to ordinary funds, that the same buyout instance reaches every
CoinJoin session and every rotated identity, and that escrow change never
enters wallet address history.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jmcore.models import NetworkType, Offer, OfferType

from maker.bot import MakerBot
from maker.coinjoin import CoinJoinState
from maker.config import MakerConfig, OfferConfig
from maker.maker_session import MakerSession
from maker.offers import OfferManager

pytestmark = pytest.mark.asyncio

FINGERPRINT = "a1b2c3d4"
BOUND_MIXDEPTH = 2
MIXDEPTH_COUNT = 5
SESSION_ID = "ab" * 16
CHANNEL_VALUE = 1_000_000
MINIMUM_CHANGE = 300_000
WALLET_BALANCE = 250_000
OTHER_BALANCE = 900_000
ESCROW_ADDRESS = "bcrt1pescrowchangeaddress"
# wallet balance + channel value - the reserve the escrow change must keep
EXPECTED_BUDGET = WALLET_BALANCE + CHANNEL_VALUE - MINIMUM_CHANGE


def _binding(
    *,
    network: str = "regtest",
    fingerprint: str = FINGERPRINT,
    mixdepth: int = BOUND_MIXDEPTH,
) -> dict[str, Any]:
    return {
        "network": network,
        "wallet_fingerprint": fingerprint,
        "mixdepth": mixdepth,
    }


class FakeBuyout:
    """The prepared buyout as the maker's offer and session wiring sees it."""

    def __init__(
        self,
        *,
        binding: dict[str, Any] | None = None,
        stored_binding: dict[str, Any] | None = None,
        state: str = "ACCEPTED",
        role: str = "buyer",
        network: str = "regtest",
        channel_value: int = CHANNEL_VALUE,
        store_error: Exception | None = None,
    ) -> None:
        binding = _binding() if binding is None else binding
        stored = dict(binding if stored_binding is None else stored_binding)
        record = SimpleNamespace(
            role=role,
            state=state,
            data={
                "runtime_binding": stored,
                "proposal": {"network": network},
            },
        )
        self.record = record

        def get(session_id: str) -> SimpleNamespace:
            if store_error is not None:
                raise store_error
            assert session_id == SESSION_ID
            return self.record

        self.buyer = SimpleNamespace(
            runtime_binding=binding,
            store=SimpleNamespace(get=get),
        )
        self.session_id = SESSION_ID
        self.terms = SimpleNamespace(proposal=SimpleNamespace(network=network))
        self.change_address = ESCROW_ADDRESS
        self.total_value = channel_value
        self.minimum_change = MINIMUM_CHANGE
        self.inputs: list[dict[str, Any]] = []

    def wallet_funding_required(self, total_required: int) -> int:
        return max(1, total_required + self.minimum_change - self.total_value)

    def begin_round(self) -> None:  # pragma: no cover - not reached by these tests
        raise AssertionError("offer publication must never consume the reservation")


def _wallet(balances: dict[int, int] | None = None) -> MagicMock:
    resolved = {md: 0 for md in range(MIXDEPTH_COUNT)}
    resolved[BOUND_MIXDEPTH] = WALLET_BALANCE
    if balances is not None:
        resolved.update(balances)
    wallet = MagicMock()
    wallet.mixdepth_count = MIXDEPTH_COUNT
    wallet.address_type = "p2tr"
    wallet.network = "regtest"
    wallet.wallet_fingerprint = FINGERPRINT
    wallet.utxo_cache = {}
    wallet.sync_all = AsyncMock()
    wallet.reconstruct_imported_state_safe = AsyncMock()
    wallet.get_total_balance = AsyncMock(return_value=sum(resolved.values()))
    wallet.get_balance_for_offers = AsyncMock(side_effect=lambda md, **kw: resolved[md])
    wallet.get_locked_input_outpoints = MagicMock(return_value=set())
    wallet.get_maker_rotation_lineage_outpoints = AsyncMock(return_value=set())
    return wallet


def _backend(*, arbitrary_utxos: bool = True) -> MagicMock:
    backend = MagicMock()
    backend.can_provide_neutrino_metadata = MagicMock(return_value=False)
    backend.requires_neutrino_metadata = MagicMock(return_value=False)
    backend.can_resolve_foreign_prevouts = MagicMock(return_value=True)
    backend.can_lookup_arbitrary_utxos = MagicMock(return_value=arbitrary_utxos)
    backend.get_block_height = AsyncMock(return_value=800)
    return backend


def _config() -> MakerConfig:
    return MakerConfig(
        mnemonic="test " * 12,
        directory_servers=["localhost:5222"],
        network=NetworkType.REGTEST,
        address_type="p2tr",
        offer_configs=[
            OfferConfig(
                offer_type=OfferType.TR0_RELATIVE,
                min_size=10_000,
                cj_fee_relative="0.001",
                size_factor=0.0,
                cjfee_factor=0.0,
                txfee_contribution_factor=0.0,
            )
        ],
    )


def _manager(wallet: MagicMock, buyout: FakeBuyout | None) -> OfferManager:
    return OfferManager(
        wallet,
        _config(),
        "J5Maker",
        buyout=buyout,
        buyout_mixdepth=-1 if buyout is None else BOUND_MIXDEPTH,
    )


# -- Advertised liquidity ------------------------------------------------


async def test_bound_mixdepth_advertises_the_channel_budget() -> None:
    """Channel value raises the advertised budget of the one bound mixdepth."""
    wallet = _wallet({0: OTHER_BALANCE, 4: OTHER_BALANCE})
    manager = _manager(wallet, FakeBuyout())

    balances = await manager.get_mixdepth_offer_balances()

    assert balances == {0: 0, 1: 0, BOUND_MIXDEPTH: EXPECTED_BUDGET, 3: 0, 4: 0}
    assert await manager.get_max_offer_balance() == EXPECTED_BUDGET


async def test_offers_never_leave_the_bound_mixdepth() -> None:
    """A richer unbound mixdepth must not be advertised or selected."""
    wallet = _wallet({0: 50_000_000})
    manager = _manager(wallet, FakeBuyout())

    offers = await manager.create_offers()

    assert offers
    assert manager.offer_balance == EXPECTED_BUDGET
    assert max(offer.maxsize for offer in offers) < EXPECTED_BUDGET


async def test_bound_mixdepth_without_ordinary_liquidity_withdraws_offers() -> None:
    """Channel value alone cannot fill a round: an ordinary input is required."""
    wallet = _wallet({BOUND_MIXDEPTH: 0, 0: OTHER_BALANCE})
    manager = _manager(wallet, FakeBuyout())

    assert await manager.get_mixdepth_offer_balances() == dict.fromkeys(range(MIXDEPTH_COUNT), 0)
    assert await manager.create_offers() == []


@pytest.mark.parametrize("state", ["ROUND_RESERVED", "CANCELED", "SETTLED"])
async def test_buyout_no_longer_accepted_withdraws_offers(state: str) -> None:
    """A consumed or resolved buyout never falls back to ordinary funds."""
    wallet = _wallet({0: OTHER_BALANCE})
    manager = _manager(wallet, FakeBuyout(state=state))

    assert await manager.get_mixdepth_offer_balances() == dict.fromkeys(range(MIXDEPTH_COUNT), 0)
    assert await manager.create_offers() == []


@pytest.mark.parametrize(
    "buyout",
    [
        pytest.param(FakeBuyout(stored_binding=_binding(mixdepth=3)), id="other-mixdepth"),
        pytest.param(
            FakeBuyout(stored_binding=_binding(fingerprint="deadbeef")), id="other-wallet"
        ),
        pytest.param(FakeBuyout(role="seller"), id="other-role"),
        pytest.param(FakeBuyout(store_error=RuntimeError("journal locked")), id="unreadable"),
    ],
)
async def test_unusable_durable_record_withdraws_offers(buyout: FakeBuyout) -> None:
    """Anything but an accepted record bound to this runtime means no offers."""
    wallet = _wallet({0: OTHER_BALANCE})
    manager = _manager(wallet, buyout)

    assert await manager.get_mixdepth_offer_balances() == dict.fromkeys(range(MIXDEPTH_COUNT), 0)


async def test_offers_are_unchanged_without_a_buyout() -> None:
    """An ordinary maker still advertises every mixdepth's own balance."""
    wallet = _wallet({0: OTHER_BALANCE})
    manager = _manager(wallet, None)

    balances = await manager.get_mixdepth_offer_balances()

    assert balances == {0: OTHER_BALANCE, 1: 0, BOUND_MIXDEPTH: WALLET_BALANCE, 3: 0, 4: 0}
    assert await manager.get_max_offer_balance() == OTHER_BALANCE


# -- Startup binding ------------------------------------------------------


def _bot(buyout: FakeBuyout | None, *, backend: MagicMock | None = None) -> MakerBot:
    return MakerBot(
        wallet=_wallet(),
        backend=_backend() if backend is None else backend,
        config=_config(),
        buyout=buyout,
    )


async def test_startup_accepts_a_bound_buyout() -> None:
    buyout = FakeBuyout()
    bot = _bot(buyout)

    assert bot.buyout is buyout
    assert bot.buyout_mixdepth == BOUND_MIXDEPTH
    assert bot.offer_manager.buyout is buyout
    assert bot.offer_manager.buyout_mixdepth == BOUND_MIXDEPTH


@pytest.mark.parametrize(
    ("buyout", "backend", "message"),
    [
        pytest.param(
            FakeBuyout(binding=_binding(fingerprint="deadbeef")),
            None,
            "not bound to this wallet",
            id="other-wallet",
        ),
        pytest.param(
            FakeBuyout(binding=_binding(network="signet"), network="signet"),
            None,
            "networks differ",
            id="other-network",
        ),
        pytest.param(
            FakeBuyout(binding=_binding(mixdepth=MIXDEPTH_COUNT)),
            None,
            "outside this wallet",
            id="mixdepth-out-of-range",
        ),
        pytest.param(
            FakeBuyout(binding={"network": "regtest", "wallet_fingerprint": FINGERPRINT}),
            None,
            "not bound to a wallet mixdepth",
            id="absent-mixdepth",
        ),
        pytest.param(
            FakeBuyout(),
            _backend(arbitrary_utxos=False),
            "look up arbitrary UTXOs",
            id="unsupported-backend",
        ),
    ],
)
async def test_startup_rejects_an_unbound_buyout(
    buyout: FakeBuyout, backend: MagicMock | None, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _bot(buyout, backend=backend)


async def test_startup_rejects_a_non_taproot_wallet() -> None:
    wallet = _wallet()
    wallet.address_type = "p2wpkh"
    config = MakerConfig(
        mnemonic="test " * 12,
        directory_servers=["localhost:5222"],
        network=NetworkType.REGTEST,
        address_type="p2wpkh",
        offer_configs=[OfferConfig(offer_type=OfferType.SW0_RELATIVE, min_size=10_000)],
    )

    with pytest.raises(ValueError, match="Taproot wallet and a Taproot pit"):
        MakerBot(wallet=wallet, backend=_backend(), config=config, buyout=FakeBuyout())


# -- Propagation ----------------------------------------------------------


async def test_fill_passes_the_same_buyout_to_the_new_session() -> None:
    buyout = FakeBuyout()
    bot = _bot(buyout)
    bot.current_offers = [
        Offer(
            counterparty=bot.nick,
            oid=0,
            ordertype=OfferType.TR0_RELATIVE,
            minsize=10_000,
            maxsize=900_000,
            txfee=0,
            cjfee="0.001",
        )
    ]
    bot.generations[0].current_offers = bot.current_offers

    async def handle_fill(amount: int, commitment: str, taker_pk: str) -> tuple[bool, dict]:
        return True, {"nacl_pubkey": "abc123", "features": []}

    with (
        patch("maker.protocol_handlers.CoinJoinSession") as session_class,
        patch("maker.protocol_handlers.check_commitment", return_value=True),
        patch.object(bot, "_send_response", new=AsyncMock()),
    ):
        session = MagicMock()
        session.handle_fill = handle_fill
        session.validate_channel = MagicMock(return_value=True)
        session_class.return_value = session

        await bot._handle_fill("J5Taker123", f"fill 0 500000 taker_pk_hex P{'aa' * 32}")

    session_class.assert_called_once()
    assert session_class.call_args.kwargs["buyout"] is buyout


async def test_fill_without_a_buyout_keeps_ordinary_sessions() -> None:
    bot = _bot(None)
    bot.current_offers = [
        Offer(
            counterparty=bot.nick,
            oid=0,
            ordertype=OfferType.TR0_RELATIVE,
            minsize=10_000,
            maxsize=900_000,
            txfee=0,
            cjfee="0.001",
        )
    ]
    bot.generations[0].current_offers = bot.current_offers

    async def handle_fill(amount: int, commitment: str, taker_pk: str) -> tuple[bool, dict]:
        return True, {"nacl_pubkey": "abc123", "features": []}

    with (
        patch("maker.protocol_handlers.CoinJoinSession") as session_class,
        patch("maker.protocol_handlers.check_commitment", return_value=True),
        patch.object(bot, "_send_response", new=AsyncMock()),
    ):
        session = MagicMock()
        session.handle_fill = handle_fill
        session.validate_channel = MagicMock(return_value=True)
        session_class.return_value = session

        await bot._handle_fill("J5Taker123", f"fill 0 500000 taker_pk_hex P{'aa' * 32}")

    assert session_class.call_args.kwargs["buyout"] is None


async def test_rotated_identity_keeps_the_same_buyout() -> None:
    """The durable record is single-use, so every identity shares one buyout."""
    buyout = FakeBuyout()
    bot = _bot(buyout)
    bot.config.tor_control.enabled = False

    generation = await bot._create_replacement_generation()

    assert generation is not None
    assert generation.offer_manager.buyout is buyout
    assert generation.offer_manager.buyout_mixdepth == BOUND_MIXDEPTH
    assert generation.offer_manager.offer_balance == EXPECTED_BUDGET


# -- History --------------------------------------------------------------


def _history_session(*, escrow_change: bool) -> MakerSession:
    inner = MagicMock()
    inner.taker_nick = "J5Taker123"
    inner.session_timeout_sec = 60
    inner.state = CoinJoinState.PUBKEY_SENT
    inner.commitment = bytes.fromhex("ab" * 32)
    inner.our_utxos = {("ce" * 32, 1): MagicMock(address="bcrt1pmakerinput", value=WALLET_BALANCE)}
    inner.amount = 500_000
    inner.cj_address = "bcrt1pcoinjoin"
    inner.change_address = ESCROW_ADDRESS if escrow_change else "bcrt1pwalletchange"
    inner.wallet_change_address = "" if escrow_change else "bcrt1pwalletchange"
    inner.crypto.is_encrypted = True
    inner.crypto.decrypt.return_value = f"{'bb' * 32}:0|02{'cc' * 32}|02{'dd' * 32}|11|22"
    inner.wallet.renew_coinjoin_inputs.return_value = True
    inner.handle_auth = AsyncMock(
        return_value=(
            True,
            {
                "utxo_list": "cc:0",
                "auth_pub": "02" + "ee" * 32,
                "cj_addr": "bcrt1pcoinjoin",
                "change_addr": inner.change_address,
                "btc_sig": "signature",
            },
        )
    )
    return MakerSession(inner)


@pytest.mark.parametrize(
    ("escrow_change", "recorded"),
    [(True, ""), (False, "bcrt1pwalletchange")],
)
async def test_history_records_only_wallet_change(escrow_change: bool, recorded: str) -> None:
    """Escrow change is buyout journal state, never a wallet history address."""
    session = _history_session(escrow_change=escrow_change)
    session.send_response = AsyncMock(return_value=True)

    bot = MagicMock()
    bot.active_sessions = {(0, session.taker_nick): session}
    bot.directory_clients = {}
    bot.config.network.value = "regtest"
    bot.wallet.wallet_fingerprint = FINGERPRINT
    bot._broadcast_commitment = AsyncMock(return_value=True)

    with (
        patch("maker.maker_session.UTXOMetadata.from_str"),
        patch(
            "maker.maker_session.create_maker_history_entry", return_value=MagicMock()
        ) as create_history,
        patch("maker.maker_session.append_history_entry"),
        patch("maker.maker_session.get_notifier", return_value=MagicMock()),
        patch("maker.maker_session.spawn_task"),
    ):
        await session.on_auth(bot, "auth ciphertext", "dir:test")

    kwargs = create_history.call_args.kwargs
    assert kwargs["change_address"] == recorded
    # The wire response still discloses the real change output to the taker.
    assert session.change_address == (ESCROW_ADDRESS if escrow_change else "bcrt1pwalletchange")
    assert kwargs["our_utxos"] == [("ce" * 32, 1)]
