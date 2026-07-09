"""E2E test for reserved/issued deposit-address persistence.

Validates the real production path against a regtest bitcoind:

* Repeated ``get_new_address_verified`` calls hand out distinct addresses.
* Issued addresses are persisted (BIP-329 ``jm:reserved`` records) so a fresh
  ``WalletService`` over the same data dir never reissues them, even though
  none of them was funded.
* A user label attached with ``reserve_address`` survives a restart and the
  unfunded reserved address shows up with ``reserved`` status and its label.

Requires: ``docker compose --profile e2e up -d`` (or the default regtest
bitcoind). Run with:
``pytest tests/e2e/test_reserved_addresses_e2e.py -m e2e``.
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from jmwallet.backends.descriptor_wallet import (
    DescriptorWalletBackend,
    generate_wallet_name,
    get_mnemonic_fingerprint,
)
from jmwallet.cli.mnemonic import generate_mnemonic_secure
from jmwallet.wallet.service import WalletService

pytestmark = pytest.mark.e2e


def _make_wallet(
    cfg: dict[str, str], mnemonic: str, data_dir: Path
) -> tuple[WalletService, DescriptorWalletBackend]:
    fingerprint = get_mnemonic_fingerprint(mnemonic, "")
    backend = DescriptorWalletBackend(
        rpc_url=cfg["rpc_url"],
        rpc_user=cfg["rpc_user"],
        rpc_password=cfg["rpc_password"],
        wallet_name=generate_wallet_name(fingerprint, "regtest"),
    )
    wallet = WalletService(
        mnemonic=mnemonic,
        backend=backend,
        network="regtest",
        mixdepth_count=5,
        scan_range=1000,
        data_dir=data_dir,
        max_sats_freeze_reuse=-1,
    )
    return wallet, backend


@pytest.mark.asyncio
async def test_issued_addresses_persist_across_restart(
    bitcoin_rpc_config: dict[str, str],
    ensure_blockchain_ready: None,
) -> None:
    """A fresh WalletService must not reissue a previously handed-out address."""
    cfg = bitcoin_rpc_config
    mnemonic = generate_mnemonic_secure(word_count=12)

    with TemporaryDirectory() as tmp:
        data_dir = Path(tmp)

        wallet, _ = _make_wallet(cfg, mnemonic, data_dir)
        try:
            await wallet.setup_descriptor_wallet(scan_range=1000, rescan=True)
            await wallet.sync_with_descriptor_wallet()

            first = await wallet.get_new_address_verified(0)
            second = await wallet.get_new_address_verified(0)
            assert first != second
            assert first == wallet.get_receive_address(0, 0)
            assert second == wallet.get_receive_address(0, 1)

            # Attach a label to a further, unfunded address.
            labeled = wallet.get_receive_address(0, 5)
            wallet.reserve_address(labeled, "Alice rent")
        finally:
            await wallet.close()

        # Restart: a brand-new service over the same data dir.
        wallet2, _ = _make_wallet(cfg, mnemonic, data_dir)
        try:
            await wallet2.sync_with_descriptor_wallet()

            reserved = wallet2.get_reserved_addresses()
            assert first in reserved
            assert second in reserved
            assert reserved.get(labeled) == "Alice rent"

            # The next handed-out address must skip all reserved ones. The
            # picker returns (highest reserved index + 1), and index 5 is
            # reserved, so the next deposit address is index 6.
            third = await wallet2.get_new_address_verified(0)
            assert third not in {first, second, labeled}
            assert third == wallet2.get_receive_address(0, 6)

            # The unfunded labeled address is shown as reserved with its label.
            infos = wallet2.get_address_info_for_mixdepth(
                0, 0, gap_limit=6, used_addresses=set()
            )
            info = next(i for i in infos if i.address == labeled)
            assert info.status == "reserved"
            assert info.label == "Alice rent"
        finally:
            await wallet2.close()
