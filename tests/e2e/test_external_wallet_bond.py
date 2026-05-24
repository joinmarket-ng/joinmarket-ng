"""
End-to-end tests for external wallet (cold storage) fidelity bonds.

Tests that fidelity bonds created from external wallets (like hardware wallets)
work correctly with both our implementation and the reference implementation.

The key feature being tested is the certificate chain:
  UTXO keypair (cold/external) -> signs -> certificate (hot) -> signs -> nick proofs

This allows keeping bond funds in cold storage while the hot wallet handles
the ongoing nick proof signing.

For full e2e tests with Docker: docker compose --profile e2e up -d
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from coincurve import PrivateKey
from jmcore.btc_script import mk_freeze_script
from jmcore.crypto import bitcoin_message_hash_bytes
from jmcore.timenumber import get_nearest_valid_locktime
from loguru import logger

from jmwallet.wallet.address import script_to_p2wsh_address
from jmwallet.wallet.bond_registry import (
    BondRegistry,
    FidelityBondInfo,
    save_registry,
)


def create_external_wallet_bond(
    tmp_data_dir: Path,
    network: str = "regtest",
) -> tuple[str, PrivateKey, bytes, PrivateKey, bytes, int, bytes]:
    """
    Simulate creating a fidelity bond from an external wallet (hardware wallet).

    This function:
    1. Generates a "cold wallet" keypair (simulating hardware wallet)
    2. Creates a bond address from the public key
    3. Generates a "hot wallet" certificate keypair
    4. Signs the certificate with the cold wallet key
    5. Saves everything to the bond registry

    Returns:
        Tuple of:
        - bond_address: The P2WSH address to fund
        - cold_privkey: The cold wallet private key (would be in HW wallet)
        - cold_pubkey: The cold wallet public key
        - hot_privkey: The hot wallet private key
        - hot_pubkey: The hot wallet public key
        - cert_expiry: Certificate expiry in periods
        - cert_signature: The certificate signature
    """
    # 1. Generate "cold wallet" keypair (simulating hardware wallet)
    cold_privkey = PrivateKey()
    cold_pubkey = cold_privkey.public_key.format(compressed=True)
    logger.info(f"Cold wallet pubkey: {cold_pubkey.hex()}")

    # 2. Create bond address with a locktime 1 year in the future
    future_time = int(time.time()) + 365 * 24 * 60 * 60
    locktime = get_nearest_valid_locktime(future_time, round_up=True)

    witness_script = mk_freeze_script(cold_pubkey.hex(), locktime)
    bond_address = script_to_p2wsh_address(witness_script, network)
    logger.info(f"Bond address: {bond_address}")
    logger.info(f"Locktime: {locktime}")

    # 3. Generate "hot wallet" certificate keypair
    hot_privkey = PrivateKey()
    hot_pubkey = hot_privkey.public_key.format(compressed=True)
    logger.info(f"Hot wallet pubkey: {hot_pubkey.hex()}")

    # 4. Sign the certificate with the cold wallet key
    cert_expiry = 52  # ~2 years in 2016-block periods
    cert_msg = (
        b"fidelity-bond-cert|" + hot_pubkey + b"|" + str(cert_expiry).encode("ascii")
    )
    msg_hash = bitcoin_message_hash_bytes(cert_msg)
    cert_signature = cold_privkey.sign(msg_hash, hasher=None)
    logger.info(f"Certificate signature: {cert_signature.hex()[:40]}...")

    # 5. Save to bond registry
    from datetime import datetime

    bond_info = FidelityBondInfo(
        address=bond_address,
        locktime=locktime,
        locktime_human=datetime.fromtimestamp(locktime).strftime("%Y-%m-%d %H:%M:%S"),
        index=-1,  # External wallet
        path="external",
        pubkey=cold_pubkey.hex(),
        witness_script_hex=witness_script.hex(),
        network=network,
        created_at=datetime.now().isoformat(),
        # Certificate fields
        cert_pubkey=hot_pubkey.hex(),
        cert_privkey=hot_privkey.secret.hex(),
        cert_signature=cert_signature.hex(),
        cert_expiry=cert_expiry,
    )

    registry = BondRegistry()
    registry.add_bond(bond_info)
    save_registry(registry, tmp_data_dir, "deadbeef")
    logger.info(f"Saved bond registry to {tmp_data_dir}")

    return (
        bond_address,
        cold_privkey,
        cold_pubkey,
        hot_privkey,
        hot_pubkey,
        cert_expiry,
        cert_signature,
    )


@pytest.mark.asyncio
async def test_external_wallet_bond_creation(tmp_path: Path):
    """
    Test that we can create a fidelity bond from an external wallet keypair.

    This tests the CLI workflow without Docker:
    1. Create bond address from public key
    2. Generate hot keypair
    3. Create certificate message
    4. Sign certificate
    5. Import certificate
    """
    (
        bond_address,
        cold_privkey,
        cold_pubkey,
        hot_privkey,
        hot_pubkey,
        cert_expiry,
        cert_signature,
    ) = create_external_wallet_bond(tmp_path)

    # Verify the bond was saved correctly
    from jmwallet.wallet.bond_registry import load_registry

    registry = load_registry(tmp_path, "deadbeef")
    assert len(registry.bonds) == 1

    bond = registry.bonds[0]
    assert bond.address == bond_address
    assert bond.has_certificate is True
    assert bond.cert_pubkey == hot_pubkey.hex()
    assert bond.cert_signature == cert_signature.hex()
    assert bond.cert_expiry == cert_expiry

    logger.info("External wallet bond creation test passed!")


@pytest.mark.asyncio
async def test_external_wallet_bond_proof_creation(tmp_path: Path):
    """
    Test that we can create a valid fidelity bond proof using the certificate chain.

    This verifies that the maker's create_fidelity_bond_proof function
    correctly uses the certificate for external wallet bonds.
    """
    from maker.fidelity import FidelityBondInfo as MakerBondInfo
    from maker.fidelity import create_fidelity_bond_proof

    (
        bond_address,
        cold_privkey,
        cold_pubkey,
        hot_privkey,
        hot_pubkey,
        cert_expiry,
        cert_signature,
    ) = create_external_wallet_bond(tmp_path)

    # Create a FidelityBondInfo with certificate data (simulating what find_fidelity_bonds returns)
    bond = MakerBondInfo(
        txid="a" * 64,  # Fake txid
        vout=0,
        value=10000000,  # 0.1 BTC
        locktime=int(time.time()) + 365 * 24 * 60 * 60,
        confirmation_time=int(time.time()) - 100000,
        bond_value=1000000,
        pubkey=cold_pubkey,
        private_key=None,  # NO private key - this is the key feature!
        cert_pubkey=hot_pubkey,
        cert_privkey=hot_privkey,
        cert_signature=cert_signature,
        cert_expiry=cert_expiry,
    )

    # Create proof
    current_block_height = 800000
    proof = create_fidelity_bond_proof(
        bond=bond,
        maker_nick="J5testmaker",
        taker_nick="J5testtaker",
        current_block_height=current_block_height,
    )

    assert proof is not None, "Proof creation should succeed with certificate"
    logger.info(f"Created proof with certificate chain: {proof[:40]}...")


@pytest.mark.asyncio
async def test_external_wallet_bond_proof_verification(tmp_path: Path):
    """
    Test that bond proofs created with certificate chain can be verified.

    This tests the full round-trip:
    1. Create bond with external wallet + certificate
    2. Create proof using certificate chain
    3. Verify the proof
    """
    from jmcore.crypto import verify_fidelity_bond_proof
    from maker.fidelity import FidelityBondInfo as MakerBondInfo
    from maker.fidelity import create_fidelity_bond_proof

    (
        bond_address,
        cold_privkey,
        cold_pubkey,
        hot_privkey,
        hot_pubkey,
        cert_expiry,
        cert_signature,
    ) = create_external_wallet_bond(tmp_path)

    maker_nick = "J5externalmaker"
    taker_nick = "J5externaltaker"

    # Create FidelityBondInfo with certificate
    locktime = int(time.time()) + 365 * 24 * 60 * 60
    bond = MakerBondInfo(
        txid="b" * 64,
        vout=0,
        value=50000000,  # 0.5 BTC
        locktime=locktime,
        confirmation_time=int(time.time()) - 200000,
        bond_value=5000000,
        pubkey=cold_pubkey,
        private_key=None,  # NO private key!
        cert_pubkey=hot_pubkey,
        cert_privkey=hot_privkey,
        cert_signature=cert_signature,
        cert_expiry=cert_expiry,
    )

    # Create proof
    current_block_height = 800000
    proof = create_fidelity_bond_proof(
        bond=bond,
        maker_nick=maker_nick,
        taker_nick=taker_nick,
        current_block_height=current_block_height,
    )

    assert proof is not None

    # Verify the proof
    is_valid, bond_data, error_msg = verify_fidelity_bond_proof(
        proof_base64=proof,
        maker_nick=maker_nick,
        taker_nick=taker_nick,
    )

    assert is_valid, f"Proof verification failed: {error_msg}"
    assert bond_data is not None

    # Verify the parsed data
    assert bond_data["utxo_pub"] == cold_pubkey.hex(), (
        "UTXO pubkey should be cold wallet"
    )
    assert bond_data["cert_pub"] == hot_pubkey.hex(), "Cert pubkey should be hot wallet"
    assert bond_data["locktime"] == locktime

    logger.info("External wallet bond proof verification passed!")
    logger.info(f"  UTXO pubkey (cold): {bond_data['utxo_pub'][:20]}...")
    logger.info(f"  Cert pubkey (hot):  {bond_data['cert_pub'][:20]}...")


@pytest.mark.asyncio
async def test_external_wallet_vs_hot_wallet_proof(tmp_path: Path):
    """
    Test that external wallet (cold storage) proofs and hot wallet proofs
    both verify correctly and both use delegated certificates.

    Hot wallet bonds use ephemeral random cert keypairs (matching the
    reference implementation), while cold storage bonds use pre-signed
    certificates from an offline key.
    """
    from jmcore.crypto import verify_fidelity_bond_proof
    from maker.fidelity import FidelityBondInfo as MakerBondInfo
    from maker.fidelity import create_fidelity_bond_proof

    maker_nick = "J5compmaker"
    taker_nick = "J5comptaker"
    current_block_height = 800000
    locktime = int(time.time()) + 365 * 24 * 60 * 60

    # Create a hot wallet bond (uses ephemeral random cert keypair)
    self_signed_privkey = PrivateKey()
    self_signed_pubkey = self_signed_privkey.public_key.format(compressed=True)

    self_signed_bond = MakerBondInfo(
        txid="c" * 64,
        vout=0,
        value=10000000,
        locktime=locktime,
        confirmation_time=int(time.time()) - 100000,
        bond_value=1000000,
        pubkey=self_signed_pubkey,
        private_key=self_signed_privkey,  # HAS private key
        cert_pubkey=None,
        cert_privkey=None,
        cert_signature=None,
        cert_expiry=None,
    )

    self_signed_proof = create_fidelity_bond_proof(
        bond=self_signed_bond,
        maker_nick=maker_nick,
        taker_nick=taker_nick,
        current_block_height=current_block_height,
    )

    # Create external wallet bond with certificate
    (
        _,
        cold_privkey,
        cold_pubkey,
        hot_privkey,
        hot_pubkey,
        cert_expiry,
        cert_signature,
    ) = create_external_wallet_bond(tmp_path)

    external_bond = MakerBondInfo(
        txid="d" * 64,
        vout=0,
        value=10000000,
        locktime=locktime,
        confirmation_time=int(time.time()) - 100000,
        bond_value=1000000,
        pubkey=cold_pubkey,
        private_key=None,  # NO private key
        cert_pubkey=hot_pubkey,
        cert_privkey=hot_privkey,
        cert_signature=cert_signature,
        cert_expiry=cert_expiry,
    )

    external_proof = create_fidelity_bond_proof(
        bond=external_bond,
        maker_nick=maker_nick,
        taker_nick=taker_nick,
        current_block_height=current_block_height,
    )

    # Both should create valid proofs
    assert self_signed_proof is not None
    assert external_proof is not None

    # Both should verify
    is_valid_ss, data_ss, _ = verify_fidelity_bond_proof(
        self_signed_proof, maker_nick, taker_nick
    )
    is_valid_ext, data_ext, _ = verify_fidelity_bond_proof(
        external_proof, maker_nick, taker_nick
    )

    assert is_valid_ss, "Self-signed proof should verify"
    assert is_valid_ext, "External wallet proof should verify"

    # Both modes use delegated certificates (utxo_pub != cert_pub)
    # Hot wallet uses ephemeral random cert key, cold wallet uses pre-signed cert
    assert data_ss["utxo_pub"] != data_ss["cert_pub"], (
        "Hot wallet: utxo_pub != cert_pub (delegated)"
    )
    assert data_ext["utxo_pub"] != data_ext["cert_pub"], (
        "External: utxo_pub != cert_pub"
    )
    assert data_ext["utxo_pub"] == cold_pubkey.hex()
    assert data_ext["cert_pub"] == hot_pubkey.hex()

    logger.info("Comparison test passed!")
    logger.info(f"  Hot wallet: utxo_pub = {data_ss['utxo_pub'][:16]}...")
    logger.info(f"  Hot wallet: cert_pub = {data_ss['cert_pub'][:16]}...")
    logger.info(f"  External: utxo_pub = {data_ext['utxo_pub'][:16]}...")
    logger.info(f"  External: cert_pub = {data_ext['cert_pub'][:16]}...")


# TODO: Add full Docker e2e test that:
# 1. Starts e2e profile with our maker using external wallet bond
# 2. Verifies taker sees and validates the bond
# 3. Verifies reference implementation also validates the bond
# This requires modifying the Docker compose setup to support external wallet bonds
