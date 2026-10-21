"""Explicit fresh managed-node initialization, without owning ongoing supervision.

Initialization publishes individual nodes durably, not atomically as a group.
The operator must exclude consumers of these nodes until it finishes, while
retaining any existing settlement monitors. Partial state is never rolled back
or silently adopted by another guided attempt.
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

import httpx
from jmcore.constants import GENESIS_BLOCK_HASHES
from jmcore.crypto import mnemonic_to_seed
from jmcore.lightning_keys import WalletLightningKeys
from jmcore.secure_files import read_sensitive_file
from jmcore.settings import JoinMarketSettings
from jmcore.tor_control import TorControlClient
from jmswap.lightning_context import LightningContext, lightning_context
from jmswap.lnd_release import default_lnd_binary, download_and_install, is_installed
from jmswap.managed_nodes import ManagedNodeError, NodeRegistry, NodeState
from jmswap.managed_provision import create_nodes
from pydantic import SecretStr

from jmwallet.setup_wallet import SetupWalletError, verify_setup_wallet_identity


class SetupLightningError(Exception):
    """Explicit initialization is refused or incomplete; retain all existing state."""


def _require_new_installation(ctx: LightningContext) -> None:
    if ctx.paths.root.is_symlink():
        raise SetupLightningError(
            "Managed-node root is an alias; inspect it before explicit initialization"
        )
    if ctx.store.load() is not None:
        raise SetupLightningError(
            "Managed-node registry already exists; inspect it or explicitly resume/recover "
            "with jm-lightning. Guided creation does not adopt existing state."
        )
    if ctx.paths.root.exists() and any(
        path.name not in {"processes.lock", "registry.lock"} for path in ctx.paths.root.iterdir()
    ):
        raise SetupLightningError(
            "Existing managed-node artifacts require inspection or recovery; they were preserved."
        )


def _core_auth(ctx: LightningContext) -> tuple[str, str]:
    if ctx.env.bitcoind_cookie is not None:
        cookie = read_sensitive_file(ctx.env.bitcoind_cookie).decode("utf-8").strip()
        user, separator, password = cookie.partition(":")
        if not separator or not user or not password:
            raise SetupLightningError("Bitcoin Core RPC cookie cannot be verified")
        return user, password
    return ctx.env.bitcoind_user or "", ctx.env.bitcoind_password or ""


async def _prerequisites(ctx: LightningContext) -> None:
    """Read-only checks matching LND's direct HTTP Core transport, not wallet RPC."""
    async with httpx.AsyncClient(
        auth=_core_auth(ctx),
        timeout=10,
        trust_env=False,
        follow_redirects=False,
    ) as client:

        async def core(method: str, params: list[object]) -> object:
            response = await client.post(
                f"http://{ctx.env.bitcoind_host}",
                json={"jsonrpc": "2.0", "id": "setup-check", "method": method, "params": params},
            )
            response.raise_for_status()
            value = response.json()
            if not isinstance(value, dict) or value.get("error"):
                raise SetupLightningError("Bitcoin Core readiness response is unavailable")
            return value.get("result")

        if await core("getblockhash", [0]) != GENESIS_BLOCK_HASHES[ctx.network]:
            raise SetupLightningError("Bitcoin Core network does not match the selected blockchain")
        info = await core("getblockchaininfo", [])
        if not isinstance(info, dict) or info.get("initialblockdownload") is not False:
            raise SetupLightningError("Bitcoin Core is still syncing or its sync state is unknown")
    settings = ctx.settings.tor
    tor = TorControlClient(
        control_host=settings.control_host,
        control_port=settings.control_port,
        cookie_path=settings.cookie_path,
        password=settings.password.get_secret_value() if settings.password is not None else None,
    )
    try:
        await tor.connect()
        await tor.authenticate()
        bootstrap = await tor.get_info("status/bootstrap-phase")
        if re.search(r"\bPROGRESS=100\b", bootstrap) is None:
            raise SetupLightningError("Tor bootstrap is not complete")
    finally:
        # __aenter__ authentication failures would otherwise skip __aexit__.
        await tor.close()


def initialize_managed_nodes(
    settings: JoinMarketSettings,
    mnemonic: SecretStr,
    passphrase: SecretStr,
    *,
    wallet_file: Path,
    data_dir: Path,
    consumers_excluded: bool,
    install_lnd: bool = False,
) -> NodeRegistry:
    """Create a new epoch only on explicit request, then stop the new children.

    Call only after the operator has excluded start/restart of consumers using
    these nodes until initialization completes. This assertion is not persisted
    and does not prove existing obligations resolved. No Supervisor is started.
    Configuration, mnemonic contents, identity selection, and data-directory
    aliases must not be edited concurrently. Existing recovery-progress writers
    may continue; this function never rewrites wallet metadata.
    Existing/partial/orphaned installations require separate explicit handling.
    """
    if consumers_excluded is not True:
        raise SetupLightningError(
            "Exclude node consumers and automatic restarts before initialization"
        )
    if settings.lightning.mode != "managed" or settings.wallet.address_type != "p2tr":
        raise SetupLightningError(
            "Fresh managed-node creation requires managed Lightning and Taproot"
        )
    phase = "wallet identity verification"
    try:
        try:
            verify_setup_wallet_identity(wallet_file, mnemonic, passphrase)
        except SetupWalletError as exc:
            raise SetupLightningError(str(exc)) from None
        seed = mnemonic_to_seed(mnemonic.get_secret_value(), passphrase.get_secret_value())
        phase = "managed launch configuration"
        runtime = settings.model_copy(
            update={"data_dir": data_dir.expanduser().resolve(strict=False)}
        )
        try:
            ctx = lightning_context(runtime, seed, with_keys=False)
            ctx.env.validate_for_launch()
        except ManagedNodeError as exc:
            # The launch-environment validator emits static, secret-free
            # diagnostics. Do not expose arbitrary provisioning/RPC errors.
            raise SetupLightningError(str(exc)) from None
        phase = "existing-state inspection"
        _require_new_installation(ctx)
        phase = "pinned binary verification"
        verified = is_installed(ctx.env.lnd_binary)
        if not verified:
            if not install_lnd:
                raise SetupLightningError(
                    "Pinned LND is unavailable; explicitly authorize --install-lnd"
                )
            if ctx.env.lnd_binary != default_lnd_binary(runtime.get_data_dir()):
                raise SetupLightningError(
                    "Unverified custom LND executable; install it explicitly outside guided setup"
                )
        elif not os.access(ctx.env.lnd_binary, os.X_OK):
            raise SetupLightningError("Verified LND is not executable; inspect its permissions")
        phase = "Core and Tor readiness"
        asyncio.run(asyncio.wait_for(_prerequisites(ctx), timeout=30))
        if not verified:
            phase = "authorized pinned LND installation"
            download_and_install(runtime.get_data_dir(), proxy=f"socks5h://{ctx.env.tor_socks}")
            if not is_installed(ctx.env.lnd_binary):
                raise SetupLightningError("Installed LND does not match the pinned release")
            if not os.access(ctx.env.lnd_binary, os.X_OK):
                raise SetupLightningError(
                    "Installed LND is not executable; inspect its permissions"
                )
        phase = "initialization ownership"
        with ctx.store.exclusive_processes():
            _require_new_installation(ctx)
            keys = WalletLightningKeys(seed, ctx.network)
            try:
                phase = "node creation and owned shutdown"
                registry = asyncio.run(
                    create_nodes(
                        ctx.store,
                        keys,
                        ctx.env,
                        wallet_identity=ctx.wallet_identity,
                        mixdepths=ctx.mixdepths,
                    )
                )
            finally:
                # create_nodes awaits owned node cleanup before cancellation can
                # propagate, so keys and ownership outlast that cleanup.
                keys.close()
        phase = "initialized record verification"
        if {node.mixdepth for node in registry.nodes} != set(ctx.mixdepths) or any(
            node.state is not NodeState.ACTIVE
            or node.identity_pubkey is None
            or node.onion_endpoint is None
            for node in registry.nodes
        ):
            raise SetupLightningError(
                "Managed-node initialization is partial; preserve and inspect it"
            )
        return registry
    except SetupLightningError:
        raise
    except (Exception, KeyboardInterrupt, asyncio.CancelledError):
        raise SetupLightningError(
            f"Managed-node initialization interrupted or incomplete during {phase}; "
            "retain all state and inspect "
            "prerequisites and recovery before retrying."
        ) from None
