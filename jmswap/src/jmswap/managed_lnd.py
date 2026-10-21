"""One managed LND process: start, create or unlock its wallet, query, and stop."""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

from jmcore.lightning_keys import WalletLightningKeys
from jmcore.secure_files import atomic_write_private, ensure_private_directory
from loguru import logger

from jmswap.lnd_config import LndEnvironment, render_lnd_conf
from jmswap.managed_nodes import ManagedNode, ManagedNodeError, ManagedNodePaths
from jmswap.managed_quarantine import managed_launch_policy

# lnrpc.WalletState values (stateservice.proto).
NON_EXISTING, LOCKED, UNLOCKED, RPC_ACTIVE, SERVER_ACTIVE = 0, 1, 2, 3, 4
RECOVERY_WINDOW = 2500


class LndProcessError(ManagedNodeError):
    """The LND process exited or did not reach the expected state."""


def _credentials(cert: bytes, macaroon_hex: str | None) -> Any:
    import grpc

    credentials = grpc.ssl_channel_credentials(cert)
    if macaroon_hex is None:
        return credentials

    class _Macaroon(grpc.AuthMetadataPlugin):
        def __call__(self, context: Any, callback: Any) -> None:
            callback((("macaroon", macaroon_hex),), None)

    return grpc.composite_channel_credentials(
        credentials, grpc.metadata_call_credentials(_Macaroon())
    )


class ManagedLnd:
    """A supervised ``lnd`` child process for one registry node."""

    def __init__(self, node: ManagedNode, paths: ManagedNodePaths, env: LndEnvironment) -> None:
        self.node = node
        self.paths = paths
        self.env = env
        self._process: asyncio.subprocess.Process | None = None

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    @property
    def returncode(self) -> int | None:
        return None if self._process is None else self._process.returncode

    async def start(self) -> None:
        if self.running:
            return
        self.env.validate_for_launch()
        if not self.env.lnd_binary.is_file():
            raise LndProcessError(
                f"lnd not found at {self.env.lnd_binary}; run `jm-lightning install-lnd`"
            )
        async with managed_launch_policy(self.paths, self.node, network=self.env.network) as (
            current,
            quarantined,
        ):
            self.node = current
            await self._stop_stale_instance()
            node_dir = self.paths.node_dir(self.node)
            ensure_private_directory(node_dir)
            conf = render_lnd_conf(self.node, self.env, quarantined=quarantined)
            atomic_write_private(self.paths.lnd_conf(self.node), conf.encode())
            # Console logging is disabled; this only captures early startup failures.
            console = open(node_dir / "lnd-console.log", "wb")  # noqa: SIM115
            try:
                spawn = asyncio.create_task(
                    asyncio.create_subprocess_exec(
                        str(self.env.lnd_binary),
                        f"--lnddir={node_dir}",
                        stdin=asyncio.subprocess.DEVNULL,
                        stdout=console,
                        stderr=console,
                        start_new_session=True,
                    )
                )
                try:
                    self._process = await asyncio.shield(spawn)
                except asyncio.CancelledError:
                    # Spawning can finish after caller cancellation. Recover its
                    # handle and stop it before releasing launch ownership, even
                    # if the caller repeats cancellation while cleanup runs.
                    cleanup = asyncio.create_task(self._recover_canceled_spawn(spawn))
                    while not cleanup.done():
                        with contextlib.suppress(asyncio.CancelledError):
                            await asyncio.shield(cleanup)
                    cleanup.result()
                    raise
            finally:
                console.close()

    async def _recover_canceled_spawn(
        self, spawn: asyncio.Task[asyncio.subprocess.Process]
    ) -> None:
        try:
            self._process = await spawn
        except Exception as exc:
            logger.warning("Canceled managed LND spawn failed ({})", type(exc).__name__)
        else:
            await self.stop()

    async def _responds(self) -> bool:
        if not self.paths.tls_cert(self.node).exists():
            return False
        try:
            await self.wallet_state()
        except Exception:  # noqa: BLE001 - nothing (of ours) is listening
            return False
        return True

    async def _stop_stale_instance(self, timeout: float = 60.0) -> None:
        """Stop an lnd of this node left running by a killed supervisor.

        Its RPC port answers with this node's TLS certificate, so it is ours.
        """
        if not await self._responds():
            return
        logger.warning("An earlier lnd of {} is still running; stopping it", self.node.name)
        from jmswap.lndrpc import lightning_pb2

        if self.paths.macaroon(self.node).exists():
            with contextlib.suppress(Exception):
                await self.call("StopDaemon", lightning_pb2.StopRequest(), timeout=10)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not await self._responds():
                return
            await asyncio.sleep(1)
        raise LndProcessError(
            f"an earlier lnd of {self.node.name} still runs on port {self.node.rpc_port}; "
            "stop it before starting the managed nodes"
        )

    def _channel(self, *, macaroon: bool) -> Any:
        import grpc

        cert = self.paths.tls_cert(self.node).read_bytes()
        macaroon_hex = self.paths.macaroon(self.node).read_bytes().hex() if macaroon else None
        return grpc.aio.secure_channel(
            f"127.0.0.1:{self.node.rpc_port}", _credentials(cert, macaroon_hex)
        )

    def console_tail(self, lines: int = 5) -> str:
        path = self.paths.node_dir(self.node) / "lnd-console.log"
        with contextlib.suppress(OSError):
            return "\n".join(path.read_text(errors="replace").splitlines()[-lines:])
        return ""

    def _check_alive(self) -> None:
        if self._process is not None and self._process.returncode is not None:
            tail = self.console_tail()
            raise LndProcessError(
                f"lnd for {self.node.name} exited with code {self._process.returncode}"
                + (f": {tail}" if tail else "")
            )

    async def wallet_state(self) -> int:
        from jmswap.lndrpc import stateservice_pb2, stateservice_pb2_grpc

        async with self._channel(macaroon=False) as channel:
            stub = stateservice_pb2_grpc.StateStub(channel)  # type: ignore[no-untyped-call]
            response = await stub.GetState(stateservice_pb2.GetStateRequest(), timeout=5)
            return int(response.state)

    async def wait_for_state(self, accepted: set[int], *, timeout: float = 120.0) -> int:
        deadline = time.monotonic() + timeout
        last_error: str = "TLS certificate not created yet"
        while time.monotonic() < deadline:
            self._check_alive()
            if self.paths.tls_cert(self.node).exists():
                try:
                    state = await self.wallet_state()
                except Exception as exc:  # noqa: BLE001 - LND is still starting
                    last_error = type(exc).__name__
                else:
                    if state in accepted:
                        return state
                    last_error = f"wallet state {state}"
            await asyncio.sleep(0.5)
        raise LndProcessError(f"lnd for {self.node.name} did not become ready ({last_error})")

    async def ensure_unlocked(
        self,
        keys: WalletLightningKeys,
        *,
        allow_create: bool,
        channel_backups: bytes | None = None,
        timeout: float = 180.0,
    ) -> None:
        """Create (only when allowed) or unlock the wallet, then wait for the server."""
        from jmswap.lndrpc import lightning_pb2, walletunlocker_pb2, walletunlocker_pb2_grpc

        state = await self.wait_for_state(
            {NON_EXISTING, LOCKED, RPC_ACTIVE, SERVER_ACTIVE}, timeout=timeout
        )
        scope = self.node.scope(self.env.network)
        if state == NON_EXISTING:
            if not allow_create:
                raise LndProcessError(
                    f"{self.node.name} has no LND wallet; it needs explicit recovery"
                )
            request = walletunlocker_pb2.InitWalletRequest(
                wallet_password=keys.unlock_password(scope),
                extended_master_key=keys.node_master_key(scope),
                extended_master_key_birthday_timestamp=self.node.birthday,
                recovery_window=RECOVERY_WINDOW if channel_backups is not None else 0,
            )
            if channel_backups is not None:
                request.channel_backups.CopyFrom(
                    lightning_pb2.ChanBackupSnapshot(
                        multi_chan_backup=lightning_pb2.MultiChanBackup(
                            multi_chan_backup=channel_backups
                        )
                    )
                )
            async with self._channel(macaroon=False) as channel:
                stub = walletunlocker_pb2_grpc.WalletUnlockerStub(channel)  # type: ignore[no-untyped-call]
                await stub.InitWallet(request, timeout=60)
            logger.info("Created the LND wallet of {}", self.node.name)
        elif state == LOCKED:
            async with self._channel(macaroon=False) as channel:
                stub = walletunlocker_pb2_grpc.WalletUnlockerStub(channel)  # type: ignore[no-untyped-call]
                await stub.UnlockWallet(
                    walletunlocker_pb2.UnlockWalletRequest(
                        wallet_password=keys.unlock_password(scope)
                    ),
                    timeout=60,
                )
        await self.wait_for_state({SERVER_ACTIVE}, timeout=timeout)
        deadline = time.monotonic() + 30
        while not self.paths.macaroon(self.node).exists():
            self._check_alive()
            if time.monotonic() > deadline:
                raise LndProcessError(f"{self.node.name} wrote no admin macaroon")
            await asyncio.sleep(0.5)

    async def call(self, method: str, request: Any, *, timeout: float = 15.0) -> Any:
        """Call one ``lnrpc.Lightning`` method with the admin macaroon."""
        from jmswap.lndrpc import lightning_pb2_grpc

        async with self._channel(macaroon=True) as channel:
            stub = lightning_pb2_grpc.LightningStub(channel)  # type: ignore[no-untyped-call]
            return await getattr(stub, method)(request, timeout=timeout)

    async def stop(self, *, timeout: float = 60.0) -> None:
        if not self.running:
            return
        assert self._process is not None
        from jmswap.lndrpc import lightning_pb2

        if self.paths.macaroon(self.node).exists():
            with contextlib.suppress(Exception):
                await self.call("StopDaemon", lightning_pb2.StopRequest(), timeout=10)
        try:
            await asyncio.wait_for(self._process.wait(), timeout)
        except TimeoutError:
            logger.warning("lnd for {} did not stop in time; terminating it", self.node.name)
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), 15)
            except TimeoutError:
                self._process.kill()
                await self._process.wait()


def onion_endpoint_of(identity_pubkey: str, uris: list[str]) -> str | None:
    """The node's own v3 onion ``host:port`` from GetInfo URIs."""
    for uri in uris:
        node_id, _, endpoint = uri.partition("@")
        if node_id == identity_pubkey and ".onion:" in endpoint:
            return endpoint
    return None
