"""Maker-local success-spend material recovered from durable endpoint evidence."""

from __future__ import annotations

from jmswap.bitcoin_core_escrow import SuccessSpendMaterial
from jmswap.bitcoin_escrow import ChannelInputEscrowTree
from jmswap.errors import ChannelInputEscrowError

from maker.escrow_endpoint import EndpointEscrowSigner
from maker.escrow_keys import EscrowEndpointRole, WalletEscrowKeyDeriver
from maker.escrow_runtime import EndpointEscrowSessionStore


class MakerSuccessSpendMaterialResolver:
    """Restore the maker's success-spend key material without storing private keys."""

    def __init__(
        self,
        endpoint_store: EndpointEscrowSessionStore,
        key_deriver: WalletEscrowKeyDeriver,
    ) -> None:
        if not isinstance(endpoint_store, EndpointEscrowSessionStore):
            raise TypeError("endpoint_store must be an EndpointEscrowSessionStore")
        if not isinstance(key_deriver, WalletEscrowKeyDeriver):
            raise TypeError("key_deriver must be a WalletEscrowKeyDeriver")
        self._endpoint_store = endpoint_store
        self._key_deriver = key_deriver

    async def resolve_success_material(self, session_id: str) -> SuccessSpendMaterial:
        """Reconstruct maker-controlled success-spend material from durable state."""
        try:
            record = self._endpoint_store.load(session_id)
            if record is None or record.role is not EscrowEndpointRole.MAKER:
                raise ChannelInputEscrowError("maker success-spend material is unavailable")
            if record.terms is None or record.remote_keys is None:
                raise ChannelInputEscrowError("maker success-spend material is incomplete")

            local_keys = EndpointEscrowSigner(self._key_deriver).derive_keys(
                record.edge_id, EscrowEndpointRole.MAKER
            )
            local_payload = local_keys.public_payload()
            if record.local_keys != local_payload or local_keys.claim is None:
                raise ChannelInputEscrowError("maker success-spend material conflicts with wallet")

            tree = ChannelInputEscrowTree(
                maker_escrow_pubkey=bytes.fromhex(local_payload.escrow_pubkey),
                peer_escrow_pubkey=bytes.fromhex(record.remote_keys.escrow_pubkey),
                maker_claim_pubkey=bytes.fromhex(local_payload.maker_claim_pubkey or ""),
                payment_hash=bytes.fromhex(record.terms.payment_hash),
            )
            return SuccessSpendMaterial(
                tree=tree,
                maker_claim_private_key=local_keys.claim.private_key_bytes(),
                maker_output_script=bytes.fromhex(local_payload.timeout_destination_scriptpubkey),
                maker_output_is_controlled=True,
            )
        except Exception:  # noqa: BLE001 - fail closed at the private-key boundary
            raise ChannelInputEscrowError(
                "unable to resolve maker success-spend material"
            ) from None
