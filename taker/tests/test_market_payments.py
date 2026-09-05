"""Tests for credential-market payment request and settlement validation."""

from __future__ import annotations

import hashlib
from unittest.mock import AsyncMock, Mock

import pytest
from bech32 import bech32_decode, bech32_encode
from bitcointx.core.key import CKey, CPubKey
from jmcore.bitcoin import address_to_scriptpubkey_for_network, pubkey_to_p2wpkh_address
from jmcore.credential_market import PaymentTerms
from jmcore.models import NetworkType
from jmwallet.backends.base import UTXO

from taker._vendor.bolt11 import Bolt11, MilliSatoshi, Tag, TagChar, Tags, encode
from taker._vendor.bolt11.models.features import FeatureExtra, Features, FeatureState
from taker.market_payments import (
    PaymentValidationError,
    payment_uri,
    validate_payment_terms,
    verify_lightning_preimage,
    verify_onchain_payment,
)

NOW = 1_700_000_000
QUOTE_EXPIRY = NOW + 300
PREIMAGE = bytes(range(32))
PAYMENT_HASH = hashlib.sha256(PREIMAGE).hexdigest()
INVOICE_KEY = "11" * 32
REGTEST_ADDRESS = pubkey_to_p2wpkh_address(b"\x02" + b"\x42" * 32, "regtest")


def make_invoice(
    *,
    amount_msat: int = 100_000,
    currency: str = "bcrt",
    date: int = NOW - 60,
    expiry: int = 600,
    payment_hash: str = PAYMENT_HASH,
    include_payee: bool = False,
    extra_tags: list[Tag] | None = None,
) -> str:
    """Create a locally signed synthetic invoice with no node interaction."""
    tags = [
        Tag(TagChar.payment_hash, payment_hash),
        Tag(TagChar.payment_secret, "22" * 32),
        Tag(TagChar.description, "credential market test"),
        Tag(TagChar.expire_time, expiry),
        Tag(TagChar.min_final_cltv_expiry, 18),
    ]
    if include_payee:
        payee = bytes(CKey(bytes.fromhex(INVOICE_KEY)).pub).hex()
        tags.append(Tag(TagChar.payee, payee))
    if extra_tags:
        tags.extend(extra_tags)
    invoice = Bolt11(
        currency=currency,
        date=date,
        amount_msat=MilliSatoshi(amount_msat),
        tags=Tags(tags),
    )
    return encode(invoice, private_key=INVOICE_KEY, keep_payee=include_payee)


def lightning_terms(invoice: str, amount_sats: int = 100) -> PaymentTerms:
    return PaymentTerms(
        rail="lightning",
        request=invoice,
        amount_sats=amount_sats,
        min_confirmations=1,
    )


def onchain_terms(
    *, amount_sats: int = 100, min_confirmations: int = 2, address: str = REGTEST_ADDRESS
) -> PaymentTerms:
    return PaymentTerms(
        rail="onchain",
        request=address,
        amount_sats=amount_sats,
        min_confirmations=min_confirmations,
    )


def test_validate_lightning_payment_terms_and_uri() -> None:
    invoice = make_invoice()
    terms = lightning_terms(invoice)

    assert validate_payment_terms(terms, NetworkType.REGTEST, NOW, QUOTE_EXPIRY) == (
        f"ln:{PAYMENT_HASH}"
    )
    assert payment_uri(terms) == f"lightning:{invoice}"


def test_validate_onchain_payment_terms_and_uri() -> None:
    terms = onchain_terms()
    scriptpubkey = address_to_scriptpubkey_for_network(REGTEST_ADDRESS, "regtest").hex()

    assert validate_payment_terms(terms, "regtest", NOW, QUOTE_EXPIRY) == (
        f"chain:regtest:{scriptpubkey}"
    )
    assert payment_uri(terms) == f"bitcoin:{REGTEST_ADDRESS}?amount=0.000001"


def test_validate_lightning_rejects_amount_and_network_mismatches() -> None:
    amount_mismatch = lightning_terms(make_invoice(amount_msat=101_000))
    sub_sat_mismatch = lightning_terms(make_invoice(amount_msat=100_001))
    network_mismatch = lightning_terms(make_invoice(currency="tb"))

    for terms in (amount_mismatch, sub_sat_mismatch, network_mismatch):
        with pytest.raises(PaymentValidationError):
            validate_payment_terms(terms, "regtest", NOW, QUOTE_EXPIRY)


def test_validate_lightning_rejects_expired_future_and_short_lived_invoices() -> None:
    expired = lightning_terms(make_invoice(date=NOW - 700, expiry=600))
    future = lightning_terms(make_invoice(date=NOW + 1))
    short_lived = lightning_terms(make_invoice(expiry=QUOTE_EXPIRY - (NOW - 60) - 1))

    for terms in (expired, future, short_lived):
        with pytest.raises(PaymentValidationError):
            validate_payment_terms(terms, "regtest", NOW, QUOTE_EXPIRY)


def test_validate_lightning_rejects_invalid_signature() -> None:
    invoice = make_invoice(include_payee=True)
    hrp, data = bech32_decode(invoice)
    assert hrp is not None and data is not None
    data[-10] ^= 1
    forged_invoice = bech32_encode(hrp, data)

    with pytest.raises(PaymentValidationError):
        validate_payment_terms(lightning_terms(forged_invoice), "regtest", NOW, QUOTE_EXPIRY)


def test_validate_lightning_fails_closed_without_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    invoice = make_invoice()
    unavailable = RuntimeError("secp256k1 compiled without pubkey recovery functions")
    monkeypatch.setattr(CPubKey, "recover_compact", Mock(side_effect=unavailable))

    with pytest.raises(PaymentValidationError, match="Invalid BOLT11 invoice") as error:
        validate_payment_terms(lightning_terms(invoice), "regtest", NOW, QUOTE_EXPIRY)
    assert error.value.__cause__ is unavailable


def test_validate_lightning_rejects_malformed_hash_duplicate_tags_and_required_feature() -> None:
    malformed_hash = make_invoice(payment_hash="33" * 31)
    duplicate_hash = make_invoice(extra_tags=[Tag(TagChar.payment_hash, "33" * 32)])
    unsupported_feature = Features.from_feature_list({FeatureExtra(18): FeatureState.required})
    required_feature = make_invoice(extra_tags=[Tag(TagChar.features, unsupported_feature)])

    for invoice in (malformed_hash, duplicate_hash, required_feature):
        with pytest.raises(PaymentValidationError):
            validate_payment_terms(lightning_terms(invoice), "regtest", NOW, QUOTE_EXPIRY)


def test_validate_onchain_rejects_wrong_network_and_confirmation_policy() -> None:
    with pytest.raises(PaymentValidationError):
        validate_payment_terms(onchain_terms(), "mainnet", NOW, QUOTE_EXPIRY)

    invalid_terms = PaymentTerms.model_construct(
        rail="onchain",
        request=REGTEST_ADDRESS,
        amount_sats=100,
        min_confirmations=0,
    )
    with pytest.raises(PaymentValidationError):
        validate_payment_terms(invalid_terms, "regtest", NOW, QUOTE_EXPIRY)


@pytest.mark.asyncio
async def test_verify_onchain_payment_uses_exact_authoritative_utxo() -> None:
    terms = onchain_terms()
    txid = "ab" * 32
    vout = 3
    scriptpubkey = address_to_scriptpubkey_for_network(terms.request, "regtest").hex()
    backend = Mock()
    backend.get_utxo = AsyncMock(
        return_value=UTXO(
            txid=txid,
            vout=vout,
            value=terms.amount_sats,
            address=terms.request,
            confirmations=terms.min_confirmations,
            scriptpubkey=scriptpubkey,
        )
    )

    settlement = await verify_onchain_payment(backend, terms, "regtest", f"{txid}:{vout}")

    assert settlement == f"chain:regtest:{txid}:{vout}"
    backend.get_utxo.assert_awaited_once_with(txid, vout)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("value", "confirmations", "scriptpubkey"),
    [
        (99, 2, None),
        (100, 1, None),
        (100, 2, "0014" + "00" * 20),
    ],
)
async def test_verify_onchain_payment_rejects_wrong_output_value_or_confirmations(
    value: int, confirmations: int, scriptpubkey: str | None
) -> None:
    terms = onchain_terms()
    txid = "cd" * 32
    expected_scriptpubkey = address_to_scriptpubkey_for_network(terms.request, "regtest").hex()
    backend = Mock()
    backend.get_utxo = AsyncMock(
        return_value=UTXO(
            txid=txid,
            vout=0,
            value=value,
            address=terms.request,
            confirmations=confirmations,
            scriptpubkey=scriptpubkey or expected_scriptpubkey,
        )
    )

    with pytest.raises(PaymentValidationError):
        await verify_onchain_payment(backend, terms, "regtest", f"{txid}:0")


@pytest.mark.asyncio
async def test_verify_onchain_payment_rejects_forged_or_unavailable_reference() -> None:
    terms = onchain_terms()
    txid = "ef" * 32
    backend = Mock()
    backend.get_utxo = AsyncMock(return_value=None)

    for reference in (txid, f"chain:regtest:{txid}:0"):
        with pytest.raises(PaymentValidationError):
            await verify_onchain_payment(backend, terms, "regtest", reference)

    with pytest.raises(PaymentValidationError):
        await verify_onchain_payment(backend, terms, "regtest", f"{txid}:0")


def test_verify_lightning_preimage_requires_matching_32_byte_preimage() -> None:
    terms = lightning_terms(make_invoice())

    assert verify_lightning_preimage(terms, "regtest", PREIMAGE, NOW, QUOTE_EXPIRY) == (
        f"ln:{PAYMENT_HASH}"
    )
    for preimage in (b"x" * 31, b"x" * 32):
        with pytest.raises(PaymentValidationError):
            verify_lightning_preimage(terms, "regtest", preimage, NOW, QUOTE_EXPIRY)
