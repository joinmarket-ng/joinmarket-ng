"""Tests for on-chain swap-provider fidelity-bond verification."""

from __future__ import annotations

from decimal import Decimal

from coincurve import PrivateKey
from jmcore.btc_script import derive_bond_address
from jmcore.crypto import ecdsa_sign, get_cert_msg

from jmswap.bond import verify_provider_bond
from jmswap.chain import FakeChainBackend
from jmswap.models import ProviderTerms

PROVIDER_PUBKEY = "0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"
LOCKTIME = 1_900_000_000
CONF_TIME = 1_600_000_000
NOW = 1_700_000_000
CERT_EXPIRY = 1_000


def _bonded_terms(chain: FakeChainBackend, *, bond_value_sats: int = 1_000_000_000):
    bond_key = PrivateKey()
    bond_pub = bond_key.public_key.format(compressed=True)
    info = derive_bond_address(bond_pub, LOCKTIME, "regtest")
    txid, vout = chain.fund_address(
        info.address,
        bond_value_sats,
        bytes.fromhex(info.scriptpubkey)
        if isinstance(info.scriptpubkey, str)
        else info.scriptpubkey,
        confirmations=5,
    )
    cert_msg = get_cert_msg(bytes.fromhex(PROVIDER_PUBKEY), CERT_EXPIRY).decode("latin-1")
    cert_sig = ecdsa_sign(cert_msg, bond_key.secret)
    terms = ProviderTerms(
        provider_id="p",
        provider_pubkey=PROVIDER_PUBKEY,
        network="regtest",
        percentage_fee=Decimal("0.1"),
        bond_utxo_pub=bond_pub.hex(),
        bond_locktime=LOCKTIME,
        bond_txid=txid,
        bond_vout=vout,
        bond_confirmation_time=CONF_TIME,
        bond_cert_sig=cert_sig,
        bond_cert_expiry=CERT_EXPIRY,
    )
    return terms


async def test_verify_valid_bond_returns_positive_value() -> None:
    chain = FakeChainBackend()
    terms = _bonded_terms(chain)
    value = await verify_provider_bond(terms, chain, current_time=NOW)
    assert value > 0


async def test_verify_rejects_forged_certificate() -> None:
    chain = FakeChainBackend()
    terms = _bonded_terms(chain)
    # tamper: sign the cert with a different (attacker) key
    attacker = PrivateKey()
    cert_msg = get_cert_msg(bytes.fromhex(PROVIDER_PUBKEY), CERT_EXPIRY).decode("latin-1")
    forged = terms.model_copy(update={"bond_cert_sig": ecdsa_sign(cert_msg, attacker.secret)})
    assert await verify_provider_bond(forged, chain, current_time=NOW) == 0


async def test_verify_rejects_missing_utxo() -> None:
    chain = FakeChainBackend()
    terms = _bonded_terms(chain)
    # a bond claiming an outpoint that is not on-chain
    fake = terms.model_copy(update={"bond_txid": "ff" * 32})
    assert await verify_provider_bond(fake, chain, current_time=NOW) == 0


async def test_verify_no_bond_returns_zero() -> None:
    chain = FakeChainBackend()
    terms = ProviderTerms(
        provider_id="p", provider_pubkey=PROVIDER_PUBKEY, network="regtest", bond_value=9_999_999
    )
    assert not terms.has_bond_proof
    assert await verify_provider_bond(terms, chain, current_time=NOW) == 0
