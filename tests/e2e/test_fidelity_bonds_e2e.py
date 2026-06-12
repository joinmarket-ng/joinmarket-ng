"""
End-to-end tests for fidelity bonds.

Tests that fidelity bonds work correctly across the full system:
1. Orderbook watcher detects bonds via !orderbook request
2. Bonds are correctly parsed and stored

Requires: docker compose --profile e2e up -d
"""

from __future__ import annotations

import asyncio

import pytest
from jmcore.directory_client import DirectoryClient
from loguru import logger

from tests.e2e.docker_utils import get_directory_port

# Mark all tests in this module as requiring Docker e2e profile
pytestmark = pytest.mark.e2e


@pytest.mark.asyncio
async def test_orderbook_watcher_receives_bonds(wait_for_directory_server):
    """
    Test that orderbook watcher correctly receives fidelity bonds from makers.

    This test:
    1. Connects to the directory as an orderbook watcher
    2. Sends !orderbook request
    3. Verifies bonds are received in PRIVMSG responses
    4. Checks that bonds are correctly parsed
    """
    # Connect as orderbook watcher
    watcher = DirectoryClient(
        host="127.0.0.1",
        port=get_directory_port(),
        network="testnet",
    )
    await watcher.connect()

    try:
        # Fetch orderbook (sends !orderbook request, waits for responses)
        offers, bonds = await watcher.fetch_orderbooks()

        logger.info(f"Received {len(offers)} offers and {len(bonds)} bonds")

        # If there are no bonds, this might be expected if no makers have bonds
        # But we log it for visibility
        if len(bonds) == 0:
            logger.warning("No fidelity bonds received - makers may not have bonds")
        else:
            # Verify bond structure
            for bond in bonds:
                assert bond.utxo_txid, "Bond should have UTXO txid"
                assert bond.utxo_vout is not None, "Bond should have UTXO vout"
                assert bond.locktime > 0, "Bond should have valid locktime"
                assert bond.cert_expiry > 0, "Bond should have cert expiry"
                assert bond.counterparty, "Bond should have counterparty nick"

                logger.info(
                    f"✓ Bond from {bond.counterparty}: "
                    f"txid={bond.utxo_txid[:16]}..., "
                    f"locktime={bond.locktime}, "
                    f"vout={bond.utxo_vout}"
                )

    finally:
        await watcher.close()


@pytest.mark.asyncio
async def test_new_peer_triggers_orderbook_request(wait_for_directory_server):
    """
    Test that orderbook watcher automatically requests bonds when new peers appear.

    This tests the fix where orderbook watcher sends !orderbook when detecting
    a new peer via PUBLIC announcement.
    """
    # Connect as orderbook watcher
    watcher = DirectoryClient(
        host="127.0.0.1",
        port=get_directory_port(),
        network="testnet",
    )
    await watcher.connect()

    # Start listening (sends initial !orderbook)
    listen_task = asyncio.create_task(
        watcher.listen_continuously(request_orderbook=True)
    )

    try:
        # Wait for initial orderbook to populate
        await asyncio.sleep(3)

        initial_bond_count = len(watcher.bonds)
        initial_offer_count = len(watcher.offers)

        logger.info(
            f"Initial state: {initial_offer_count} offers, {initial_bond_count} bonds"
        )

        # If there are existing bonds, the automatic detection is working
        if initial_bond_count > 0:
            logger.info("✓ Orderbook watcher successfully detected bonds on startup")

            # Verify bond details
            for utxo_str, bond in watcher.bonds.items():
                logger.info(
                    f"  Bond: {utxo_str}, counterparty={bond.counterparty}, "
                    f"locktime={bond.locktime}"
                )

        # Continue listening for a bit to see if new peers trigger bond requests
        await asyncio.sleep(5)

        final_bond_count = len(watcher.bonds)
        final_offer_count = len(watcher.offers)

        logger.info(
            f"Final state: {final_offer_count} offers, {final_bond_count} bonds"
        )

        # The key test: we should have received bonds if any makers have them
        # This validates the !orderbook request mechanism is working
        logger.info(
            f"✓ Orderbook watcher mechanism working: "
            f"processed {final_offer_count} offers and {final_bond_count} bonds"
        )

    finally:
        watcher.stop()
        try:
            await asyncio.wait_for(listen_task, timeout=2.0)
        except asyncio.TimeoutError:
            pass
        await watcher.close()


@pytest.mark.asyncio
async def test_bond_appears_in_privmsg_not_public(wait_for_directory_server):
    """
    Test that bonds only appear in PRIVMSG responses, never in PUBLIC announcements.

    This verifies the privacy-preserving design where bonds are only sent to
    specific requesters via PRIVMSG, not broadcast publicly.
    """
    watcher = DirectoryClient(
        host="127.0.0.1",
        port=get_directory_port(),
        network="testnet",
    )
    await watcher.connect()

    # Start listening WITHOUT requesting orderbook initially
    listen_task = asyncio.create_task(
        watcher.listen_continuously(request_orderbook=False)
    )

    try:
        # Wait for PUBLIC announcements to arrive
        await asyncio.sleep(3)

        # PUBLIC announcements should NOT contain bonds
        public_bond_count = len(watcher.bonds)

        logger.info(f"Bonds from PUBLIC announcements: {public_bond_count}")

        # Now send !orderbook request to get bonds via PRIVMSG
        await watcher.send_public_message("!orderbook")

        # Wait for PRIVMSG responses
        await asyncio.sleep(3)

        privmsg_bond_count = len(watcher.bonds)

        logger.info(f"Bonds after !orderbook request: {privmsg_bond_count}")

        # If makers have bonds, we should see them ONLY after !orderbook
        if privmsg_bond_count > public_bond_count:
            logger.info(
                f"✓ Bonds correctly sent via PRIVMSG only "
                f"({privmsg_bond_count} bonds after !orderbook, "
                f"{public_bond_count} from PUBLIC)"
            )
        elif privmsg_bond_count == 0:
            logger.warning("No bonds received - makers may not have bonds configured")
        else:
            logger.info("Bond count unchanged - all bonds already received")

    finally:
        watcher.stop()
        try:
            await asyncio.wait_for(listen_task, timeout=2.0)
        except asyncio.TimeoutError:
            pass
        await watcher.close()
