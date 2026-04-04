"""
Maker health checking via direct onion connection.

This module provides functionality to verify maker availability by connecting
directly to their onion addresses when possible, performing handshakes to
extract features, and tracking reachability status.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from dataclasses import dataclass

from jmcore.crypto import NickIdentity
from jmcore.network import connect_via_tor
from jmcore.protocol import (
    FEATURE_PEERLIST_FEATURES,
    JM_VERSION,
    FeatureSet,
    create_handshake_request,
)
from loguru import logger


@dataclass
class MakerHealthStatus:
    """Health status for a maker."""

    location: str  # onion:port
    nick: str
    reachable: bool
    last_check_time: float
    last_success_time: float | None
    consecutive_failures: int
    features: FeatureSet
    error: str | None = None

    def is_healthy(self, max_consecutive_failures: int = 3) -> bool:
        """Check if maker is considered healthy."""
        return self.reachable and self.consecutive_failures < max_consecutive_failures


class MakerHealthChecker:
    """
    Checks maker reachability via direct onion connections.

    This class performs periodic health checks on makers by:
    1. Connecting directly to their onion addresses
    2. Performing handshake to verify they're online
    3. Extracting feature flags from handshake response
    4. Tracking reachability history

    Health checks are rate-limited to avoid stressing makers.
    """

    def __init__(
        self,
        network: str,
        socks_host: str = "127.0.0.1",
        socks_port: int = 9050,
        timeout: float = 15.0,
        check_interval: float = 600.0,  # 10 minutes
        max_concurrent_checks: int = 10,
        socks_username: str | None = None,
        socks_password: str | None = None,
    ) -> None:
        """
        Initialize MakerHealthChecker.

        Args:
            network: Bitcoin network (mainnet, testnet, signet, regtest)
            socks_host: SOCKS proxy host for Tor
            socks_port: SOCKS proxy port for Tor
            timeout: Connection timeout in seconds
            check_interval: Minimum seconds between checks for same maker
            max_concurrent_checks: Maximum concurrent health checks
            socks_username: SOCKS5 username for Tor stream isolation (optional)
            socks_password: SOCKS5 password for Tor stream isolation (optional)
        """
        self.network = network
        self.socks_host = socks_host
        self.socks_port = socks_port
        self.socks_username = socks_username
        self.socks_password = socks_password
        self.timeout = timeout
        self.check_interval = check_interval
        self.max_concurrent_checks = max_concurrent_checks

        # Health status tracking: location -> status
        self.health_status: dict[str, MakerHealthStatus] = {}

        # Semaphore to limit concurrent checks
        self._check_semaphore = asyncio.Semaphore(max_concurrent_checks)

        # Nick identity for handshake (ephemeral)
        self.nick_identity = NickIdentity(JM_VERSION)

    async def check_maker(self, nick: str, location: str, force: bool = False) -> MakerHealthStatus:
        """
        Check if a maker is reachable via direct connection.

        Args:
            nick: Maker's nick
            location: Maker's onion address (format: onion:port)
            force: Force check even if recently checked

        Returns:
            MakerHealthStatus with reachability info and features
        """
        # Check if we should skip this check (rate limiting)
        current_time = time.time()
        if not force and location in self.health_status:
            last_check = self.health_status[location].last_check_time
            if current_time - last_check < self.check_interval:
                logger.debug(
                    f"Skipping health check for {nick} at {location} "
                    f"(checked {current_time - last_check:.0f}s ago)"
                )
                return self.health_status[location]

        async with self._check_semaphore:
            return await self._check_maker_impl(nick, location, current_time)

    async def _check_maker_impl(
        self, nick: str, location: str, current_time: float
    ) -> MakerHealthStatus:
        """Internal implementation of maker health check."""
        # Parse location
        if location == "NOT-SERVING-ONION":
            # Cannot check makers that don't serve onion
            status = MakerHealthStatus(
                location=location,
                nick=nick,
                reachable=False,
                last_check_time=current_time,
                last_success_time=None,
                consecutive_failures=0,
                features=FeatureSet(),
                error="NOT-SERVING-ONION",
            )
            self.health_status[location] = status
            return status

        try:
            host, port_str = location.split(":")
            port = int(port_str)
        except (ValueError, AttributeError) as e:
            logger.warning(f"Invalid location format: {location}: {e}")
            status = MakerHealthStatus(
                location=location,
                nick=nick,
                reachable=False,
                last_check_time=current_time,
                last_success_time=None,
                consecutive_failures=self.health_status.get(
                    location,
                    MakerHealthStatus(
                        location=location,
                        nick=nick,
                        reachable=False,
                        last_check_time=0,
                        last_success_time=None,
                        consecutive_failures=0,
                        features=FeatureSet(),
                    ),
                ).consecutive_failures
                + 1,
                features=FeatureSet(),
                error=f"Invalid location: {e}",
            )
            self.health_status[location] = status
            return status

        # Try to connect and perform handshake
        logger.debug(f"Health check: connecting to {nick} at {location}")
        connection = None
        try:
            # Connect via Tor
            connection = await connect_via_tor(
                host,
                port,
                self.socks_host,
                self.socks_port,
                max_message_size=2097152,
                timeout=self.timeout,
                socks_username=self.socks_username,
                socks_password=self.socks_password,
            )

            # Perform handshake
            # Request peerlist_features support to get maker's features from handshake

            our_features = FeatureSet(features={FEATURE_PEERLIST_FEATURES})
            handshake_data = create_handshake_request(
                nick=self.nick_identity.nick,
                location="NOT-SERVING-ONION",
                network=self.network,
                directory=False,
                features=our_features,
            )

            handshake_msg = {
                "type": 793,  # MessageType.HANDSHAKE
                "line": json.dumps(handshake_data),
            }
            await connection.send(json.dumps(handshake_msg).encode("utf-8"))

            # Wait for response
            response_data = await asyncio.wait_for(connection.receive(), timeout=self.timeout)
            response = json.loads(response_data.decode("utf-8"))

            if response.get("type") not in (793, 795):  # HANDSHAKE or DN_HANDSHAKE
                raise Exception(f"Unexpected response type: {response.get('type')}")

            handshake_response = json.loads(response["line"])

            # DN_HANDSHAKE (795) responses from directories have an "accepted" field.
            # HANDSHAKE (793) responses from non-directory peers (makers) use the
            # client handshake format which does NOT have "accepted" -- receiving a
            # valid handshake back implicitly means acceptance.
            response_type = response.get("type")
            if response_type == 795 and not handshake_response.get("accepted", False):
                raise Exception("Handshake rejected")
            # For type 793 (peer handshake), receiving a valid response = accepted

            # Extract features from handshake
            features = FeatureSet.from_handshake(handshake_response)

            # Maker is reachable!
            status = MakerHealthStatus(
                location=location,
                nick=nick,
                reachable=True,
                last_check_time=current_time,
                last_success_time=current_time,
                consecutive_failures=0,
                features=features,
                error=None,
            )
            self.health_status[location] = status
            logger.info(
                f"Health check: {nick} at {location} is REACHABLE "
                f"(features: {features.to_comma_string() or 'none'})"
            )
            return status

        except TimeoutError:
            error = "Connection timeout"
            logger.debug(f"Health check: {nick} at {location} timed out")
        except Exception as e:
            error = str(e)
            logger.debug(f"Health check: {nick} at {location} failed: {e}")

        finally:
            if connection:
                with contextlib.suppress(Exception):
                    await connection.close()

        # Maker is unreachable
        old_status = self.health_status.get(location)
        consecutive_failures = (old_status.consecutive_failures + 1) if old_status else 1
        last_success = old_status.last_success_time if old_status else None

        status = MakerHealthStatus(
            location=location,
            nick=nick,
            reachable=False,
            last_check_time=current_time,
            last_success_time=last_success,
            consecutive_failures=consecutive_failures,
            features=old_status.features if old_status else FeatureSet(),
            error=error,
        )
        self.health_status[location] = status

        if consecutive_failures >= 3:
            logger.warning(
                f"Health check: {nick} at {location} is UNREACHABLE "
                f"({consecutive_failures} consecutive failures, error: {error})"
            )

        return status

    async def check_makers_batch(
        self, makers: list[tuple[str, str]], force: bool = False
    ) -> dict[str, MakerHealthStatus]:
        """
        Check health of multiple makers in priority-ordered chunks.

        Makers are processed in the order provided.  Each chunk of
        ``max_concurrent_checks`` makers runs in parallel; the next chunk
        starts only after the previous one completes.  This guarantees that
        high-priority makers (e.g. bonded, low-fee) are checked before
        low-priority ones when the caller pre-sorts the list.

        Args:
            makers: List of (nick, location) tuples, ideally pre-sorted by
                priority (bonded first, then fee-ascending).
            force: Force check even if recently checked.

        Returns:
            Dict mapping location to MakerHealthStatus.
        """
        status_map: dict[str, MakerHealthStatus] = {}
        chunk_size = self.max_concurrent_checks

        for chunk_start in range(0, len(makers), chunk_size):
            chunk = makers[chunk_start : chunk_start + chunk_size]
            tasks = [self.check_maker(nick, location, force) for nick, location in chunk]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for (nick, location), result in zip(chunk, results, strict=True):
                if isinstance(result, BaseException):
                    logger.error(
                        f"Health check for {nick} at {location} raised exception: {result}"
                    )
                    status_map[location] = MakerHealthStatus(
                        location=location,
                        nick=nick,
                        reachable=False,
                        last_check_time=time.time(),
                        last_success_time=None,
                        consecutive_failures=self.health_status.get(
                            location,
                            MakerHealthStatus(
                                location=location,
                                nick=nick,
                                reachable=False,
                                last_check_time=0,
                                last_success_time=None,
                                consecutive_failures=0,
                                features=FeatureSet(),
                            ),
                        ).consecutive_failures
                        + 1,
                        features=FeatureSet(),
                        error=str(result),
                    )
                else:
                    status_map[location] = result

        return status_map

    def get_unreachable_locations(self, max_consecutive_failures: int = 3) -> set[str]:
        """
        Get set of locations that are considered unreachable.

        Args:
            max_consecutive_failures: Number of failures before marking unreachable

        Returns:
            Set of location strings for unreachable makers
        """
        return {
            location
            for location, status in self.health_status.items()
            if not status.is_healthy(max_consecutive_failures)
        }

    def get_feature_map(self) -> dict[str, FeatureSet]:
        """
        Get map of locations to their feature sets.

        Only includes makers that have been successfully checked.

        Returns:
            Dict mapping location to FeatureSet
        """
        return {
            location: status.features
            for location, status in self.health_status.items()
            if status.last_success_time is not None
        }

    def clear_status(self, location: str) -> None:
        """Clear health status for a location (e.g., when maker reconnects)."""
        self.health_status.pop(location, None)
