"""Tests for the durable native credential-market seller store."""

from __future__ import annotations

import base64
import hashlib
import multiprocessing
import sqlite3
import stat
from multiprocessing.queues import Queue
from pathlib import Path

import pytest
from _taker_market_helpers import hold_quote, settle_quote
from bitcointx.core.key import CKey
from jmcore.bitcoin import hash160
from jmcore.credential_market import (
    BondCredential,
    BondReference,
    CredentialPackage,
    MarketAuthorization,
    MarketError,
    MarketQuote,
    PaymentTerms,
    bond_resource,
    decode_document,
    sign_bond_lease,
    sign_document,
)
from jmcore.crypto import bitcoin_message_hash_bytes, get_cert_msg
from jmcore.external_podle import ExternalPoDLEOutpoint
from jmcore.market_store import (
    HOLD_INVOICE_GRACE_SECONDS,
    HoldReservation,
    MarketStore,
    MarketStoreConflictError,
    MarketStoreCorruptError,
    MarketStoreError,
    MarketStoreExpiredError,
    MarketStoreUnavailableError,
)
from jmcore.podle import generate_podle
from nacl.public import PrivateKey, SealedBox


def _key(value: int) -> CKey:
    return CKey(bytes([value]) * 32)


def _pubkey(key: CKey) -> str:
    return bytes(key.pub).hex()


def _buyer_key(value: int) -> PrivateKey:
    return PrivateKey(bytes([value]) * 32)


def _request_id(value: int) -> str:
    return f"{value:032x}"


def _bond(txid_byte: int = 0xAA) -> BondReference:
    return BondReference(
        network="regtest",
        outpoint=ExternalPoDLEOutpoint(txid=f"{txid_byte:02x}" * 32, vout=1),
        pubkey=_pubkey(_key(0x11)),
        locktime=600_000_000,
    )


def _authorization(bond: BondReference | None = None):
    owner = _key(0x11)
    seller = _key(0x22)
    return sign_document(
        MarketAuthorization(bond=bond or _bond(), period=0, seller_pubkey=_pubkey(seller)),
        owner,
    )


def _podle_credential(value: int) -> dict[str, object]:
    proof = generate_podle(bytes([value]) * 32, f"{value:02x}" * 32 + ":1", index=0)
    return {
        "version": 1,
        "network": "regtest",
        "outpoint": {"txid": f"{value:02x}" * 32, "vout": 1},
        "P": proof.p.hex(),
        "P2": proof.p2.hex(),
        "sig": proof.sig.hex(),
        "e": proof.e.hex(),
        "commitment": proof.commitment.hex(),
        "index": 0,
        "scriptpubkey": (b"\x00\x14" + hash160(proof.p)).hex(),
        "blockheight": 101,
    }


def _bond_credential(bond: BondReference, certificate: CKey) -> dict[str, object]:
    owner = _key(0x11)
    cert_pubkey = bytes(certificate.pub)
    signature = owner.sign(
        bitcoin_message_hash_bytes(get_cert_msg(cert_pubkey, 1)),
        _ecdsa_sig_grind_low_r=False,
    )
    return BondCredential(
        bond=bond,
        cert_pubkey=cert_pubkey.hex(),
        cert_expiry=1,
        cert_signature=signature.hex(),
        lease=sign_bond_lease(bond, 0, cert_pubkey.hex(), owner),
    ).model_dump(mode="json")


def _create_podle_quote(
    store: MarketStore,
    *,
    credential: dict[str, object],
    payment_number: int,
    request_number: int,
    buyer: PrivateKey | None = None,
    now: int = 100,
    ttl: int = 300,
):
    store.add_inventory("podle", str(credential["commitment"]), credential)
    return hold_quote(
        store,
        _authorization(),
        _key(0x22),
        bytes((buyer or _buyer_key(0x33)).public_key).hex(),
        "podle",
        None,
        "lightning",
        10_000,
        now,
        1,
        ttl=ttl,
        request_id=_request_id(request_number),
    )


def _concurrent_quote_creator(
    path: str,
    buyer_byte: int,
    request_number: int,
    queue: Queue[tuple[str, str]],
) -> None:
    """Use a fresh process-local store connection for cross-process locking coverage."""
    store = MarketStore(Path(path))
    try:
        quote = hold_quote(
            store,
            _authorization(),
            _key(0x22),
            bytes(_buyer_key(buyer_byte).public_key).hex(),
            "podle",
            None,
            "lightning",
            10_000,
            100,
            1,
            request_id=_request_id(request_number),
        )
        queue.put(("ok", quote.body["quote_id"]))
    except (MarketStoreConflictError, MarketStoreUnavailableError) as exc:
        queue.put(("error", type(exc).__name__))
    finally:
        store.close()


def test_quote_replay_persists_across_restart_and_uses_private_permissions(tmp_path: Path) -> None:
    path = tmp_path / "seller" / "market.sqlite"
    credential = _podle_credential(0x41)
    store = MarketStore(path)
    quote = _create_podle_quote(
        store,
        credential=credential,
        payment_number=1,
        request_number=1,
    )

    repeated = hold_quote(
        store,
        _authorization(),
        _key(0x22),
        bytes(_buyer_key(0x33).public_key).hex(),
        "podle",
        None,
        "lightning",
        10_000,
        100,
        1,
        request_id=_request_id(1),
    )
    assert repeated == quote
    assert store.get_quote(str(quote.body["quote_id"])) == quote
    assert store.pending(100) == [quote]
    assert "allocation" not in quote.body
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700

    with pytest.raises(MarketStoreConflictError):
        hold_quote(
            store,
            _authorization(),
            _key(0x22),
            bytes(_buyer_key(0x34).public_key).hex(),
            "podle",
            None,
            "lightning",
            10_000,
            100,
            1,
            request_id=_request_id(1),
        )
    with pytest.raises(MarketStoreConflictError):
        hold_quote(
            store,
            _authorization(),
            _key(0x22),
            bytes(_buyer_key(0x33).public_key).hex(),
            "podle",
            None,
            "lightning",
            999,
            100,
            1,
            request_id=_request_id(1),
        )

    quote_id = str(quote.body["quote_id"])
    store.close()
    restarted = MarketStore(path)
    assert restarted.get_quote(quote_id) == quote
    restarted.close()


def _second_podle_quote(store: MarketStore, now: int):
    return hold_quote(
        store,
        _authorization(),
        _key(0x22),
        bytes(_buyer_key(0x34).public_key).hex(),
        "podle",
        None,
        "lightning",
        10_000,
        now,
        1,
        request_id=_request_id(2),
    )


def test_expiry_keeps_inventory_until_hold_invoice_is_canceled(tmp_path: Path) -> None:
    store = MarketStore(tmp_path / "market.sqlite")
    first = _create_podle_quote(
        store, credential=_podle_credential(0x42), payment_number=1, request_number=1, ttl=1
    )
    first_id = str(first.body["quote_id"])
    invoice_expiry = 101 + HOLD_INVOICE_GRACE_SECONDS

    # The hold invoice outlives the quote, so its inventory stays reserved.
    assert store.pending(101) == [first]
    with pytest.raises(MarketStoreUnavailableError, match="inventory"):
        _second_podle_quote(store, 101)
    assert store.pending(invoice_expiry) == []

    store.begin_hold_cancel(first_id)
    with pytest.raises(MarketStoreExpiredError):
        store.finalize(first_id, _key(0x22), invoice_expiry, 1)
    with pytest.raises(MarketStoreUnavailableError, match="inventory"):
        _second_podle_quote(store, invoice_expiry)
    store.complete_hold_cancel(first_id)
    store.complete_hold_cancel(first_id)

    second = _second_podle_quote(store, invoice_expiry)
    assert first.body["resource"] == second.body["resource"]
    with sqlite3.connect(store.path) as connection:
        payments = dict(connection.execute("SELECT quote_id, payment_id FROM payments"))
    assert payments[first_id] != payments[str(second.body["quote_id"])]
    with pytest.raises(MarketStoreExpiredError):
        store.finalize(first_id, _key(0x22), invoice_expiry, 1)
    assert store.get_package(first_id) is None
    store.close()


def test_payment_accepted_before_expiry_completes_after_quote_expiry(tmp_path: Path) -> None:
    buyer = _buyer_key(0x33)
    store = MarketStore(tmp_path / "market.sqlite")
    quote = _create_podle_quote(
        store, credential=_podle_credential(0x47), payment_number=1, request_number=1, ttl=1
    )
    quote_id = str(quote.body["quote_id"])

    package = store.finalize(quote_id, _key(0x22), 101 + 60, 1)
    # Nothing is exposed until LND confirms the settlement.
    assert store.get_delivery(quote_id) is None
    assert store.get_package(quote_id) is None
    with pytest.raises(MarketStoreConflictError):
        store.begin_hold_cancel(quote_id)
    assert store.finalize(quote_id, _key(0x22), 101 + 60, 1) == package

    store.complete_hold_settle(quote_id)
    sealed = store.get_delivery(quote_id)
    assert sealed is not None
    plaintext = SealedBox(buyer).decrypt(base64.b64decode(sealed))
    assert CredentialPackage.model_validate(decode_document(plaintext)) == package
    assert store.hold_invoices(200) == []
    store.close()


def test_lost_payment_after_finalize_keeps_allocation_consumed_and_undelivered(
    tmp_path: Path,
) -> None:
    store = MarketStore(tmp_path / "market.sqlite")
    quote = _create_podle_quote(
        store, credential=_podle_credential(0x48), payment_number=1, request_number=1
    )
    quote_id = str(quote.body["quote_id"])
    store.finalize(quote_id, _key(0x22), 101, 1)
    [hold] = store.hold_invoices(101)
    assert (hold.state, hold.quote_state) == ("settling", "finalized")

    # LND reports the accepted HTLC canceled, so settlement can never happen.
    store.complete_hold_cancel(quote_id)
    assert store.get_delivery(quote_id) is None
    assert store.get_package(quote_id) is None
    with pytest.raises(MarketStoreUnavailableError, match="inventory"):
        _second_podle_quote(store, 102)
    store.close()


def test_hold_reservation_is_durable_before_the_invoice_exists(tmp_path: Path) -> None:
    path = tmp_path / "market.sqlite"
    store = MarketStore(path)
    credential = _podle_credential(0x49)
    store.add_inventory("podle", str(credential["commitment"]), credential)
    arguments = (
        _authorization(),
        _key(0x22),
        bytes(_buyer_key(0x33).public_key).hex(),
        "podle",
        None,
        "lightning",
        10_000,
        1_500,
        100,
        1,
    )
    reservation = store.reserve_hold_quote(*arguments, request_id=_request_id(1))
    assert isinstance(reservation, HoldReservation)
    assert reservation.amount_sats == 1_500
    assert reservation.invoice_expires_at == 400 + HOLD_INVOICE_GRACE_SECONDS
    store.close()

    restarted = MarketStore(path)
    assert restarted.reserve_hold_quote(*arguments, request_id=_request_id(1)) == reservation
    [hold] = restarted.hold_invoices(100)
    assert (hold.state, hold.quote_state) == ("reserved", None)
    assert hashlib.sha256(hold.preimage).digest() == reservation.payment_hash
    with pytest.raises(MarketStoreUnavailableError, match="price"):
        restarted.reserve_hold_quote(
            *arguments[:6], 1_000, *arguments[7:], request_id=_request_id(2)
        )

    terms = PaymentTerms(rail="lightning", request="regtest-hold-other", amount_sats=1_500)
    with pytest.raises(MarketStoreConflictError):
        restarted.issue_hold_quote(reservation.quote_id, _key(0x22), terms, "ln:" + "00" * 32, 100)
    payment_id = f"ln:{reservation.payment_hash.hex()}"
    with pytest.raises(MarketStoreConflictError):
        restarted.issue_hold_quote(
            reservation.quote_id,
            _key(0x22),
            terms.model_copy(update={"amount_sats": 1_400}),
            payment_id,
            100,
        )
    with pytest.raises(MarketStoreExpiredError):
        restarted.issue_hold_quote(reservation.quote_id, _key(0x22), terms, payment_id, 400)
    quote = restarted.issue_hold_quote(reservation.quote_id, _key(0x22), terms, payment_id, 100)
    assert (
        restarted.issue_hold_quote(reservation.quote_id, _key(0x22), terms, payment_id, 100)
        == quote
    )
    assert restarted.reserve_hold_quote(*arguments, request_id=_request_id(1)) == quote

    # A canceled reservation that never got a quote cannot be replayed.
    other = _podle_credential(0x4A)
    restarted.add_inventory("podle", str(other["commitment"]), other)
    abandoned = restarted.reserve_hold_quote(*arguments, request_id=_request_id(3))
    assert isinstance(abandoned, HoldReservation)
    restarted.begin_hold_cancel(abandoned.quote_id)
    restarted.complete_hold_cancel(abandoned.quote_id)
    with pytest.raises(MarketStoreUnavailableError):
        restarted.reserve_hold_quote(*arguments, request_id=_request_id(3))
    restarted.close()


def test_one_seller_runtime_per_store(tmp_path: Path) -> None:
    store = MarketStore(tmp_path / "market.sqlite")
    other = MarketStore(tmp_path / "market.sqlite")
    with store.seller_runtime():
        with pytest.raises(MarketStoreError, match="another seller runtime"):
            with other.seller_runtime():
                pass
    with other.seller_runtime():
        pass
    store.close()
    other.close()


def test_bond_attachment_filtering_and_final_delivery_are_irreversible(tmp_path: Path) -> None:
    store = MarketStore(tmp_path / "market.sqlite")
    bond = _bond()
    certificate = _key(0x44)
    store.add_inventory("bond", bond_resource(bond, 0), None)
    buyer = _buyer_key(0x33)
    quote = hold_quote(
        store,
        _authorization(bond),
        _key(0x22),
        bytes(buyer.public_key).hex(),
        "bond",
        _pubkey(certificate),
        "lightning",
        10_000,
        100,
        1,
        request_id=_request_id(1),
    )
    quote_id = str(quote.body["quote_id"])
    assert store.get_delivery(quote_id) is None
    with pytest.raises(MarketError):
        store.attach_credential(quote_id, _bond_credential(bond, _key(0x45)))
    store.attach_credential(quote_id, _bond_credential(bond, certificate))
    store.attach_credential(quote_id, _bond_credential(bond, certificate))

    package = settle_quote(store, quote_id, _key(0x22), 101, 1)
    assert package.verify() == BondCredential.model_validate(_bond_credential(bond, certificate))
    assert package.allocation.body["allocation_id"] == quote_id
    assert (
        package.allocation.body["buyer_tag"] == hashlib.sha256(bytes(buyer.public_key)).hexdigest()
    )
    sealed = store.get_delivery(quote_id)
    assert sealed is not None
    plaintext = SealedBox(buyer).decrypt(base64.b64decode(sealed))
    assert CredentialPackage.model_validate(decode_document(plaintext)) == package
    assert store.get_delivery(quote_id) == sealed
    assert store.get_package(quote_id) == package
    assert settle_quote(store, quote_id, _key(0x22), 10_000, 2) == package
    with pytest.raises(MarketStoreConflictError):
        store.begin_hold_cancel(quote_id)
    store.close()


def test_failed_quote_insert_keeps_reservation_and_settlements_are_per_invoice(
    tmp_path: Path,
) -> None:
    store = MarketStore(tmp_path / "market.sqlite")
    first_credential = _podle_credential(0x43)
    second_credential = _podle_credential(0x44)
    store.add_inventory("podle", str(first_credential["commitment"]), first_credential)
    store.add_inventory("podle", str(second_credential["commitment"]), second_credential)
    store._connection.execute(
        """
        CREATE TRIGGER reject_quote_insert BEFORE INSERT ON quotes
        BEGIN SELECT RAISE(ABORT, 'forced quote failure'); END
        """
    )
    with pytest.raises(sqlite3.IntegrityError):
        hold_quote(
            store,
            _authorization(),
            _key(0x22),
            bytes(_buyer_key(0x33).public_key).hex(),
            "podle",
            None,
            "lightning",
            10_000,
            100,
            1,
            request_id=_request_id(1),
        )
    store._connection.execute("DROP TRIGGER reject_quote_insert")
    first = hold_quote(
        store,
        _authorization(),
        _key(0x22),
        bytes(_buyer_key(0x33).public_key).hex(),
        "podle",
        None,
        "lightning",
        10_000,
        100,
        1,
        request_id=_request_id(1),
    )
    second = hold_quote(
        store,
        _authorization(),
        _key(0x22),
        bytes(_buyer_key(0x34).public_key).hex(),
        "podle",
        None,
        "lightning",
        10_000,
        100,
        1,
        request_id=_request_id(2),
    )
    settle_quote(store, str(first.body["quote_id"]), _key(0x22), 101, 1)
    settle_quote(store, str(second.body["quote_id"]), _key(0x22), 101, 1)
    with sqlite3.connect(store.path) as connection:
        references = [row[0] for row in connection.execute("SELECT settlement_ref FROM quotes")]
    assert len(set(references)) == 2
    assert all(reference.startswith("ln:") for reference in references)
    store.close()


def test_active_quote_capacity_is_bounded_but_expired_records_are_retained(tmp_path: Path) -> None:
    store = MarketStore(tmp_path / "market.sqlite")
    certificate = _key(0x44)
    for value in range(1, 66):
        bond = _bond(value)
        store.add_inventory("bond", bond_resource(bond, 0), None)
        if value <= 64:
            hold_quote(
                store,
                _authorization(bond),
                _key(0x22),
                bytes(_buyer_key(0x33).public_key).hex(),
                "bond",
                _pubkey(certificate),
                "lightning",
                10_000,
                100,
                1,
                ttl=1,
                request_id=_request_id(value),
            )
    assert len(store.pending(100)) == 64
    with pytest.raises(MarketStoreUnavailableError, match="capacity"):
        bond = _bond(65)
        hold_quote(
            store,
            _authorization(bond),
            _key(0x22),
            bytes(_buyer_key(0x33).public_key).hex(),
            "bond",
            _pubkey(certificate),
            "lightning",
            10_000,
            100,
            1,
            ttl=1,
            request_id=_request_id(65),
        )
    assert store.pending(101 + HOLD_INVOICE_GRACE_SECONDS) == []
    with sqlite3.connect(store.path) as connection:
        expired = connection.execute(
            "SELECT COUNT(*) FROM quotes WHERE state = 'expired'"
        ).fetchone()
    assert expired is not None
    assert expired[0] == 64
    store.close()


def test_cross_process_creators_have_exactly_one_reservation_winner(tmp_path: Path) -> None:
    path = tmp_path / "market.sqlite"
    store = MarketStore(path)
    credential = _podle_credential(0x46)
    store.add_inventory("podle", str(credential["commitment"]), credential)
    store.close()

    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    processes = [
        context.Process(
            target=_concurrent_quote_creator,
            args=(str(path), 0x33 + index, index + 1, queue),
        )
        for index in range(2)
    ]
    for process in processes:
        process.start()
    results = [queue.get(timeout=30) for _ in processes]
    for process in processes:
        process.join(timeout=30)
        assert process.exitcode == 0
    assert [result[0] for result in results].count("ok") == 1
    assert [result[0] for result in results].count("error") == 1
    restarted = MarketStore(path)
    assert len(restarted.pending(100)) == 1
    restarted.close()


def test_corrupt_database_is_not_silently_reset(tmp_path: Path) -> None:
    path = tmp_path / "market.sqlite"
    path.write_bytes(b"this is not a sqlite database")
    with pytest.raises(MarketStoreCorruptError):
        MarketStore(path)
    assert path.read_bytes() == b"this is not a sqlite database"


def test_lightning_hold_limit_bounds_every_quote(tmp_path: Path) -> None:
    store = MarketStore(tmp_path / "seller.sqlite")
    try:
        signed = _create_podle_quote(
            store,
            credential=_podle_credential(0x61),
            payment_number=1,
            request_number=1,
            ttl=900,
        )
        quote = signed.verified(MarketQuote, _pubkey(_key(0x22)))
        quote.check(network="regtest", height=1, now=800, max_price_sats=10000)
        with pytest.raises(ValueError):
            quote.check(network="regtest", height=1, now=1000, max_price_sats=10000)
        with pytest.raises(ValueError, match="ttl"):
            hold_quote(
                store,
                _authorization(),
                _key(0x22),
                bytes(_buyer_key(0x44).public_key).hex(),
                "podle",
                None,
                "lightning",
                10000,
                100,
                1,
                ttl=901,
                request_id=_request_id(2),
            )
    finally:
        store.close()


def test_minimum_backing_selects_the_first_large_enough_podle(tmp_path: Path) -> None:
    path = tmp_path / "market.sqlite"
    store = MarketStore(path)
    small, large, unknown = (_podle_credential(value) for value in (0x51, 0x52, 0x53))
    for credential in (unknown, small, large):
        store.add_inventory("podle", str(credential["commitment"]), credential)
    values = {f"{0x51:02x}" * 32: 1_000, f"{0x52:02x}" * 32: 50_000}

    def lookup(txid: str, vout: int) -> int | None:
        return values.get(txid) if vout == 1 else None

    arguments = (
        _authorization(),
        _key(0x22),
        bytes(_buyer_key(0x33).public_key).hex(),
        "podle",
        None,
        "lightning",
        10_000,
        1_500,
        100,
        1,
    )

    def reserved() -> set[str]:
        with sqlite3.connect(path) as connection:
            rows = connection.execute("SELECT resource FROM inventory WHERE state = 'reserved'")
            return {row[0] for row in rows}

    with pytest.raises(MarketStoreUnavailableError, match="inventory"):
        store.reserve_hold_quote(*arguments, request_id=_request_id(1), min_backing_sats=10_000)
    store.reserve_hold_quote(
        *arguments, request_id=_request_id(2), min_backing_sats=10_000, backing_sats=lookup
    )
    assert reserved() == {large["commitment"]}
    with pytest.raises(MarketStoreUnavailableError, match="inventory"):
        store.reserve_hold_quote(
            *arguments, request_id=_request_id(3), min_backing_sats=10_000, backing_sats=lookup
        )
    # A request without a minimum keeps taking the oldest row, whatever it backs.
    store.reserve_hold_quote(*arguments, request_id=_request_id(4), backing_sats=lookup)
    assert reserved() == {large["commitment"], unknown["commitment"]}
    with pytest.raises(MarketStoreConflictError, match="different quote parameters"):
        store.reserve_hold_quote(
            *arguments, request_id=_request_id(2), min_backing_sats=20_000, backing_sats=lookup
        )
    store.close()


def test_required_backing_sells_only_inventory_the_lookup_values(tmp_path: Path) -> None:
    path = tmp_path / "market.sqlite"
    store = MarketStore(path)
    elsewhere, current = (_podle_credential(value) for value in (0x61, 0x62))
    for credential in (elsewhere, current):
        store.add_inventory("podle", str(credential["commitment"]), credential)
    # Only the coin of the mixdepth being sold from has a known value.
    values = {f"{0x62:02x}" * 32: 30_000}

    def lookup(txid: str, vout: int) -> int | None:
        return values.get(txid) if vout == 1 else None

    arguments = (
        _authorization(),
        _key(0x22),
        bytes(_buyer_key(0x33).public_key).hex(),
        "podle",
        None,
        "lightning",
        10_000,
        1_500,
        100,
        1,
    )
    store.reserve_hold_quote(
        *arguments, request_id=_request_id(1), backing_sats=lookup, require_backing=True
    )
    with sqlite3.connect(path) as connection:
        rows = connection.execute("SELECT resource FROM inventory WHERE state = 'reserved'")
        assert {row[0] for row in rows} == {current["commitment"]}
    # The older row is never sold while the lookup does not value it.
    with pytest.raises(MarketStoreUnavailableError, match="inventory"):
        store.reserve_hold_quote(
            *arguments, request_id=_request_id(2), backing_sats=lookup, require_backing=True
        )
    with pytest.raises(MarketStoreUnavailableError, match="inventory"):
        store.reserve_hold_quote(*arguments, request_id=_request_id(3), require_backing=True)
    store.close()
