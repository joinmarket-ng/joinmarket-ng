"""
On-chain fidelity-bond verification for swap providers.

Advertised bond values cannot be trusted (a sybil sets ``bond_value`` to
anything). A provider therefore advertises a real JoinMarket-style timelocked
fidelity bond, and the client verifies it before swapping:

1. recompute the bond's P2WSH address from the timelocked bond pubkey + locktime
   (``jmcore.btc_script.derive_bond_address``);
2. confirm the bond UTXO exists at that address on-chain with a real value, via
   the :class:`~jmswap.chain.ChainBackend`;
3. verify a certificate signed by the bond key over the provider's swap identity
   (so a sybil cannot claim someone else's bond);
4. compute the bond value from the locked value and locktime
   (``jmcore.bond_calc``).

This reuses JoinMarket's coordinator-free, symmetric bond machinery: the client
verifies the provider's bond exactly as a maker verifies a peer's bond.
"""

from __future__ import annotations

from jmcore.bond_calc import calculate_timelocked_fidelity_bond_value
from jmcore.btc_script import derive_bond_address
from jmcore.crypto import ecdsa_verify, get_cert_msg

from jmswap.chain import ChainBackend
from jmswap.models import ProviderTerms


def provider_bond_cert_message(provider_pubkey_hex: str, cert_expiry: int) -> bytes:
    """The certificate the bond key signs to bind a bond to a provider identity."""
    return get_cert_msg(bytes.fromhex(provider_pubkey_hex), cert_expiry)


async def verify_provider_bond(
    terms: ProviderTerms,
    chain: ChainBackend,
    *,
    current_time: int | None = None,
    min_confirmations: int = 1,
) -> int:
    """Verify a provider's advertised fidelity bond on-chain.

    Returns the *verified* bond value in sats (the locked-coin-value-weighted bond
    score), or ``0`` if the bond is absent, the certificate is invalid, or the
    UTXO does not exist on-chain. Clients should filter providers on this value,
    not on the self-advertised ``bond_value``.
    """
    if not terms.has_bond_proof:
        return 0
    assert terms.bond_utxo_pub is not None and terms.bond_txid is not None
    assert terms.bond_cert_sig is not None

    # 1) the certificate must bind the bond key to this provider's swap identity.
    cert_msg = provider_bond_cert_message(terms.provider_pubkey, terms.bond_cert_expiry)
    if not ecdsa_verify(
        cert_msg.decode("latin-1"), terms.bond_cert_sig, bytes.fromhex(terms.bond_utxo_pub)
    ):
        return 0

    # 2) the bond UTXO must exist on-chain at the timelocked address.
    try:
        info = derive_bond_address(
            bytes.fromhex(terms.bond_utxo_pub), terms.bond_locktime, terms.network
        )
    except Exception:  # noqa: BLE001 - malformed bond params
        return 0
    address = info.address if hasattr(info, "address") else str(info)
    utxos = await chain.get_address_utxos(address)
    match = next(
        (
            u
            for u in utxos
            if u.txid == terms.bond_txid
            and u.vout == terms.bond_vout
            and u.confirmations >= min_confirmations
        ),
        None,
    )
    if match is None:
        return 0

    # 3) compute the bond value from the verified locked value and locktime.
    return calculate_timelocked_fidelity_bond_value(
        utxo_value=match.value,
        confirmation_time=terms.bond_confirmation_time,
        locktime=terms.bond_locktime,
        current_time=current_time,
    )


async def select_bonded_providers(
    providers: list[ProviderTerms],
    chain: ChainBackend,
    min_bond_value: int,
    *,
    current_time: int | None = None,
) -> list[tuple[ProviderTerms, int]]:
    """Return ``(terms, verified_bond_value)`` for providers whose *on-chain*
    bond meets ``min_bond_value``, sorted by verified bond descending.

    Use this instead of trusting the self-advertised ``bond_value``: it filters
    out sybils that have no real timelocked UTXO bound to their identity.
    """
    out: list[tuple[ProviderTerms, int]] = []
    for terms in providers:
        value = await verify_provider_bond(terms, chain, current_time=current_time)
        if value >= min_bond_value and value > 0:
            out.append((terms, value))
    out.sort(key=lambda pair: pair[1], reverse=True)
    return out
