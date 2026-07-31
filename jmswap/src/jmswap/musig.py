# Copyright (C) 2026 The Electrum developers
# Distributed under the MIT software license.
"""Single-use BIP327 MuSig2 sessions backed by Electrum's libsecp wrapper."""

from __future__ import annotations

from typing import Any, cast

from coincurve import PrivateKey, PublicKeyXOnly
from electrum_ecc import musig

from jmswap.contract import TaprootSwapContract, taproot_tweak_hash


class MuSig2Session:
    def __init__(
        self,
        *,
        contract: TaprootSwapContract,
        local_seckey: bytes,
        msg32: bytes,
        session_id32: bytes | None = None,
    ) -> None:
        if len(local_seckey) != 32 or len(msg32) != 32:
            raise ValueError("secret key and message must be 32 bytes")
        if session_id32 is not None and len(session_id32) != 32:
            raise ValueError("session ID must be 32 bytes")
        try:
            local_pubkey = PrivateKey(local_seckey).public_key.format(compressed=True)
        except ValueError as exc:
            raise ValueError("invalid local secret key") from exc
        if local_pubkey == contract.provider_pubkey:
            counterparty_pubkey = contract.user_pubkey
        elif local_pubkey == contract.user_pubkey:
            counterparty_pubkey = contract.provider_pubkey
        else:
            raise ValueError("local secret key does not belong to the contract")

        cache = contract.keyagg_cache()
        cache.apply_xonly_tweak(
            taproot_tweak_hash(cache.aggregate_xonly_pubkey(), contract.merkle_root)
        )
        secnonce, pubnonce = musig.nonce_gen(
            pubkey=local_pubkey,
            seckey=local_seckey,
            msg32=msg32,
            keyagg_cache=cache,
            extra_input32=session_id32,
        )
        self._contract = contract
        self._local_seckey: bytes | None = local_seckey
        self._local_pubkey = local_pubkey
        self._counterparty_pubkey = counterparty_pubkey
        self._msg32 = msg32
        self._cache = cache
        self._secnonce: Any | None = secnonce
        self._pubnonce = pubnonce
        self._counterparty_pubnonce: Any | None = None
        self._session: Any | None = None
        self._local_partial: Any | None = None
        self._aggregate_signature: bytes | None = None

    @property
    def public_nonce(self) -> bytes:
        return cast(bytes, self._pubnonce.to_bytes())

    def sign_partial(self, counterparty_public_nonce: bytes) -> bytes:
        if self._session is not None:
            raise RuntimeError("this MuSig2 session has already signed")
        counterparty = musig.PubNonce(counterparty_public_nonce)
        pubnonces = (
            [self._pubnonce, counterparty]
            if self._local_pubkey == self._contract.provider_pubkey
            else [counterparty, self._pubnonce]
        )
        session = musig.nonce_process(
            aggnonce=musig.nonce_agg(pubnonces), msg32=self._msg32, keyagg_cache=self._cache
        )
        self._counterparty_pubnonce = counterparty
        self._session = session
        secnonce, self._secnonce = self._secnonce, None
        seckey, self._local_seckey = self._local_seckey, None
        assert secnonce is not None and seckey is not None
        try:
            partial = musig.partial_sign(
                secnonce=secnonce, seckey=seckey, keyagg_cache=self._cache, session=session
            )
        finally:
            del secnonce, seckey
        self._local_partial = partial
        return cast(bytes, partial.to_bytes())

    def aggregate(self, counterparty_partial_signature: bytes) -> bytes:
        if self._aggregate_signature is not None:
            raise RuntimeError("this MuSig2 session has already aggregated")
        if (
            self._session is None
            or self._counterparty_pubnonce is None
            or self._local_partial is None
        ):
            raise RuntimeError("sign_partial must be called before aggregate")
        counterparty = musig.PartialSig(counterparty_partial_signature)
        if not musig.partial_sig_verify(
            partial_sig=counterparty,
            pubnonce=self._counterparty_pubnonce,
            pubkey=self._counterparty_pubkey,
            keyagg_cache=self._cache,
            session=self._session,
        ):
            raise ValueError("counterparty partial signature is invalid")
        partials = (
            [self._local_partial, counterparty]
            if self._local_pubkey == self._contract.provider_pubkey
            else [counterparty, self._local_partial]
        )
        signature = musig.partial_sig_agg(session=self._session, partial_sigs=partials)
        if not PublicKeyXOnly(self._contract.output_script[2:]).verify(signature, self._msg32):
            raise ValueError("aggregate signature does not verify for the swap output")
        self._aggregate_signature = signature
        return cast(bytes, signature)
