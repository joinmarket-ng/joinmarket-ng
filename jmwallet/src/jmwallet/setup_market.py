"""Seller-ledger readiness and strictly fresh guided activation, never recovery.

The caller owns the volatile publication witness and must consume/revoke its
eligibility before disclosure, refusal, or failure. An immutable origin record
or absent artifacts alone never supplies complete-history authority.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from jmcore.crypto import mnemonic_to_seed
from jmcore.market_keys import WalletMarketKeys
from jmcore.market_store import MarketStore
from jmcore.paths import get_market_store_path, get_used_commitments_path
from jmcore.secure_files import ensure_sensitive_directory, read_private_file
from jmcore.settings import JoinMarketSettings
from pydantic import SecretStr

from jmwallet.setup_wallet import (
    SetupWalletError,
    SetupWalletPublication,
    verify_setup_wallet_identity,
)


class SetupMarketError(Exception):
    """Seller setup is incomplete; all wallet and ledger artifacts must be retained."""


def _absent_market_artifacts(data_dir: Path) -> None:
    for path in (data_dir / "market", data_dir / "cmtdata"):
        try:
            path.lstat()
        except FileNotFoundError:
            continue
        raise SetupMarketError(
            "Existing market or commitment artifacts require explicit history verification; "
            "guided fresh activation preserved them."
        )


def _data_target(path: Path) -> Path:
    path = path.expanduser().absolute()
    for entry in (path, *path.parents):
        if entry.is_symlink() and not entry.exists():
            raise SetupMarketError("Ambiguous data-directory alias; no ledger activation attempted")
    return path.resolve(strict=False)


def prepare_seller_ledger(
    settings: JoinMarketSettings,
    mnemonic: SecretStr,
    passphrase: SecretStr,
    *,
    data_dir: Path,
    wallet_file: Path,
    publication: SetupWalletPublication | None,
    activate: bool,
    history_excluded_since_generation: bool,
) -> bool:
    """Return whether a new ledger was activated, or verify an already-ready one.

    Only a completed generation from this invocation, before disclosure or other
    wallet activity, can authorize empty-history activation. The operator must
    affirm continuous exclusion of wallet consumers and shared-history writers,
    including restarts, since generation and through activation. This is a caller
    precondition, not a distributed admission barrier. No existing state is
    adopted, imported, migrated, or recovered by this guided path.
    """
    if settings.wallet.address_type != "p2tr" or settings.lightning.mode == "off":
        raise SetupMarketError("Maker markets require explicit Taproot and enabled Lightning")
    keys: WalletMarketKeys | None = None
    try:
        try:
            verify_setup_wallet_identity(wallet_file, mnemonic, passphrase)
        except SetupWalletError as exc:
            raise SetupMarketError(str(exc)) from None
        target = _data_target(data_dir)
        network = settings.network_config.bitcoin_network or settings.network_config.network
        keys = WalletMarketKeys(
            mnemonic_to_seed(mnemonic.get_secret_value(), passphrase.get_secret_value()),
            network.value,
        )
        wallet_id = keys.ledger_identity()  # Not the managed-node identity or short fingerprint.
        store_path = get_market_store_path(target)
        commitments = get_used_commitments_path(target, create=False)
        diagnosis = MarketStore.diagnose_wallet(
            store_path,
            wallet_id=wallet_id,
            commitments_path=commitments,
        )
        if diagnosis["state"] == "ready":
            # Snapshot only. Seller startup must independently revalidate its
            # binding before issuance. This says nothing about old obligations.
            return False
        if not activate:
            raise SetupMarketError(
                "Seller ledger is not ready; no seller-enabling settings were applied. "
                "Fresh generation needs explicit activation, older wallets need verified history."
            )
        if publication is None or not (
            publication.newly_created is True
            and publication.complete is True
            and publication.origin == "generated"
            and publication.mnemonic == mnemonic
            and publication.path == wallet_file.expanduser().resolve(strict=True)
        ):
            raise SetupMarketError(
                "Only uninterrupted generation in this invocation permits guided activation. "
                "Use jm-maker activate-market-ledger only after verifying complete history."
            )
        if history_excluded_since_generation is not True:
            raise SetupMarketError(
                "Affirm continuous market-history exclusion since generation "
                "and through activation; "
                "--yes and node-initialization quiescence do not supply this authority."
            )
        if (
            hashlib.sha256(read_private_file(publication.path)).hexdigest()
            != publication.ciphertext_sha256
        ):
            raise SetupMarketError(
                "Published wallet changed; fresh activation authority is invalid"
            )
        _absent_market_artifacts(target)
        if _data_target(data_dir) != target:
            raise SetupMarketError("Data-directory alias changed; no ledger activation attempted")
        ensure_sensitive_directory(target)
        _absent_market_artifacts(target)  # Repeat immediately before the first store mutation.
        with MarketStore(store_path, wallet_id=wallet_id, create=True) as store:
            store.activate_wallet(commitments, history_confirmed=True)
            store.check_commitments_path(commitments)
        return True
    except SetupMarketError:
        raise
    except Exception:
        raise SetupMarketError(
            "Seller-ledger preparation is incomplete or unavailable. Wallet and any partial ledger "
            "were preserved; inspect history explicitly, "
            "never retry fresh activation automatically."
        ) from None
    finally:
        if keys is not None:
            keys.close()
