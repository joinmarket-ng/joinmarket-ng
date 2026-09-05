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
    sign_document,
)
from jmcore.crypto import bitcoin_message_hash_bytes, get_cert_msg
from jmcore.external_podle import ExternalPoDLEOutpoint
from jmcore.podle import generate_podle
from nacl.public import PrivateKey, SealedBox

from taker.market_store import (
    MarketStore,
    MarketStoreConflictError,
    MarketStoreCorruptError,
    MarketStoreExpiredError,
    MarketStoreUnavailableError,
)


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


def _payment(value: int) -> PaymentTerms:
    return PaymentTerms(
        rail="onchain",
        request=f"regtest-payment-{value}",
        amount_sats=1_000 + value,
        min_confirmations=1,
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
    store.add_payment(_payment(payment_number), f"payment-{payment_number}")
    return store.create_quote(
        _authorization(),
        _key(0x22),
        bytes((buyer or _buyer_key(0x33)).public_key).hex(),
        "podle",
        None,
        "onchain",
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
        quote = store.create_quote(
            _authorization(),
            _key(0x22),
            bytes(_buyer_key(buyer_byte).public_key).hex(),
            "podle",
            None,
            "onchain",
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

    repeated = store.create_quote(
        _authorization(),
        _key(0x22),
        bytes(_buyer_key(0x33).public_key).hex(),
        "podle",
        None,
        "onchain",
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
        store.create_quote(
            _authorization(),
            _key(0x22),
            bytes(_buyer_key(0x34).public_key).hex(),
            "podle",
            None,
            "onchain",
            10_000,
            100,
            1,
            request_id=_request_id(1),
        )
    with pytest.raises(MarketStoreConflictError):
        store.create_quote(
            _authorization(),
            _key(0x22),
            bytes(_buyer_key(0x33).public_key).hex(),
            "podle",
            None,
            "onchain",
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


def test_expiry_releases_only_inventory_and_late_quote_cannot_finalize(tmp_path: Path) -> None:
    store = MarketStore(tmp_path / "market.sqlite")
    credential = _podle_credential(0x42)
    first = _create_podle_quote(
        store,
        credential=credential,
        payment_number=1,
        request_number=1,
        ttl=1,
    )
    assert store.pending(101) == []
    store.add_payment(_payment(2), "payment-2")
    second = store.create_quote(
        _authorization(),
        _key(0x22),
        bytes(_buyer_key(0x34).public_key).hex(),
        "podle",
        None,
        "onchain",
        10_000,
        101,
        1,
        request_id=_request_id(2),
    )
    assert first.body["resource"] == second.body["resource"]

    with sqlite3.connect(store.path) as connection:
        payments = dict(connection.execute("SELECT payment_id, quote_id FROM payments"))
    assert payments["payment-1"] == first.body["quote_id"]
    assert payments["payment-2"] == second.body["quote_id"]
    with pytest.raises(MarketStoreExpiredError):
        store.finalize(str(first.body["quote_id"]), _key(0x22), "late-payment", 101, 1)
    assert store.get_package(str(first.body["quote_id"])) is None
    store.close()


def test_bond_attachment_filtering_and_final_delivery_are_irreversible(tmp_path: Path) -> None:
    store = MarketStore(tmp_path / "market.sqlite")
    bond = _bond()
    certificate = _key(0x44)
    store.add_inventory("bond", bond_resource(bond, 0), None)
    store.add_payment(_payment(1), "payment-1")
    buyer = _buyer_key(0x33)
    quote = store.create_quote(
        _authorization(bond),
        _key(0x22),
        bytes(buyer.public_key).hex(),
        "bond",
        _pubkey(certificate),
        "onchain",
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

    package = store.finalize(quote_id, _key(0x22), "settlement-1", 101, 1)
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
    assert store.finalize(quote_id, _key(0x22), "settlement-1", 10_000, 2) == package
    with pytest.raises(MarketStoreConflictError):
        store.finalize(quote_id, _key(0x22), "different-settlement", 10_000, 2)
    store.close()


def test_settlement_references_are_global_and_failed_quote_insert_rolls_back(
    tmp_path: Path,
) -> None:
    store = MarketStore(tmp_path / "market.sqlite")
    first_credential = _podle_credential(0x43)
    second_credential = _podle_credential(0x44)
    store.add_inventory("podle", str(first_credential["commitment"]), first_credential)
    store.add_inventory("podle", str(second_credential["commitment"]), second_credential)
    store.add_payment(_payment(1), "payment-1")
    store.add_payment(_payment(2), "payment-2")
    store._connection.execute(
        """
        CREATE TRIGGER reject_quote_insert BEFORE INSERT ON quotes
        BEGIN SELECT RAISE(ABORT, 'forced quote failure'); END
        """
    )
    with pytest.raises(sqlite3.IntegrityError):
        store.create_quote(
            _authorization(),
            _key(0x22),
            bytes(_buyer_key(0x33).public_key).hex(),
            "podle",
            None,
            "onchain",
            10_000,
            100,
            1,
            request_id=_request_id(1),
        )
    store._connection.execute("DROP TRIGGER reject_quote_insert")
    first = store.create_quote(
        _authorization(),
        _key(0x22),
        bytes(_buyer_key(0x33).public_key).hex(),
        "podle",
        None,
        "onchain",
        10_000,
        100,
        1,
        request_id=_request_id(1),
    )
    second = store.create_quote(
        _authorization(),
        _key(0x22),
        bytes(_buyer_key(0x34).public_key).hex(),
        "podle",
        None,
        "onchain",
        10_000,
        100,
        1,
        request_id=_request_id(2),
    )
    store.finalize(str(first.body["quote_id"]), _key(0x22), "shared-settlement", 101, 1)
    with pytest.raises(MarketStoreConflictError):
        store.finalize(str(second.body["quote_id"]), _key(0x22), "shared-settlement", 101, 1)
    assert store.get_package(str(second.body["quote_id"])) is None
    store.close()


def test_active_quote_capacity_is_bounded_but_expired_records_are_retained(tmp_path: Path) -> None:
    store = MarketStore(tmp_path / "market.sqlite")
    certificate = _key(0x44)
    for value in range(1, 66):
        bond = _bond(value)
        store.add_inventory("bond", bond_resource(bond, 0), None)
        store.add_payment(_payment(value), f"payment-{value}")
        if value <= 64:
            store.create_quote(
                _authorization(bond),
                _key(0x22),
                bytes(_buyer_key(0x33).public_key).hex(),
                "bond",
                _pubkey(certificate),
                "onchain",
                10_000,
                100,
                1,
                ttl=1,
                request_id=_request_id(value),
            )
    assert len(store.pending(100)) == 64
    with pytest.raises(MarketStoreUnavailableError, match="capacity"):
        bond = _bond(65)
        store.create_quote(
            _authorization(bond),
            _key(0x22),
            bytes(_buyer_key(0x33).public_key).hex(),
            "bond",
            _pubkey(certificate),
            "onchain",
            10_000,
            100,
            1,
            ttl=1,
            request_id=_request_id(65),
        )
    assert store.pending(101) == []
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
    store.add_payment(_payment(1), "payment-1")
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


def test_onchain_confirmation_window_and_lightning_hold_limit(tmp_path: Path) -> None:
    store = MarketStore(tmp_path / "seller.sqlite")
    try:
        signed = _create_podle_quote(
            store,
            credential=_podle_credential(0x61),
            payment_number=1,
            request_number=1,
            ttl=21600,
        )
        quote = signed.verified(MarketQuote, _pubkey(_key(0x22)))
        quote.check(network="regtest", height=1, now=20000, max_price_sats=10000)
        with pytest.raises(ValueError):
            quote.check(network="regtest", height=1, now=21700, max_price_sats=10000)
        with pytest.raises(ValueError, match="Lightning quote lifetime"):
            store.create_quote(
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
