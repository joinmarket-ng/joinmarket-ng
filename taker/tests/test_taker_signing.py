"""
Tests for taker transaction signing functionality.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jmwallet.wallet.bip32 import HDKey, mnemonic_to_seed
from jmwallet.wallet.models import UTXOInfo
from jmwallet.wallet.signing import (
    deserialize_transaction,
)

from taker.tx_builder import CoinJoinTxBuilder, CoinJoinTxData, TxInput, TxOutput


@pytest.fixture
def test_mnemonic() -> str:
    """Test mnemonic (BIP39 test vector)."""
    return (
        "abandon abandon abandon abandon abandon abandon "
        "abandon abandon abandon abandon abandon about"
    )


@pytest.fixture
def test_seed(test_mnemonic: str) -> bytes:
    """Get test seed from mnemonic."""
    return mnemonic_to_seed(test_mnemonic)


@pytest.fixture
def test_master_key(test_seed: bytes) -> HDKey:
    """Get test master key."""
    return HDKey.from_seed(test_seed)


@pytest.fixture
def taker_utxos(test_master_key: HDKey) -> list[UTXOInfo]:
    """Create test taker UTXOs with known addresses."""
    # Derive addresses for regtest (coin_type=1)
    key0 = test_master_key.derive("m/84'/1'/0'/0/0")
    addr0 = key0.get_address("regtest")

    key1 = test_master_key.derive("m/84'/1'/0'/0/1")
    addr1 = key1.get_address("regtest")

    return [
        UTXOInfo(
            txid="a" * 64,
            vout=0,
            value=1_000_000,
            address=addr0,
            confirmations=10,
            scriptpubkey="0014" + "00" * 20,  # P2WPKH placeholder
            path="m/84'/1'/0'/0/0",
            mixdepth=0,
        ),
        UTXOInfo(
            txid="b" * 64,
            vout=1,
            value=500_000,
            address=addr1,
            confirmations=5,
            scriptpubkey="0014" + "11" * 20,  # P2WPKH placeholder
            path="m/84'/1'/0'/0/1",
            mixdepth=0,
        ),
    ]


@pytest.fixture
def maker_utxos() -> list[dict[str, Any]]:
    """Create test maker UTXOs."""
    return [
        {"txid": "c" * 64, "vout": 0, "value": 1_200_000},
        {"txid": "d" * 64, "vout": 2, "value": 800_000},
    ]


@pytest.fixture
def sample_coinjoin_tx_data(
    taker_utxos: list[UTXOInfo], maker_utxos: list[dict[str, Any]]
) -> CoinJoinTxData:
    """Create sample CoinJoin transaction data."""
    return CoinJoinTxData(
        taker_inputs=[
            TxInput.from_hex(txid=u.txid, vout=u.vout, value=u.value) for u in taker_utxos
        ],
        taker_cj_output=TxOutput.from_address(
            "bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080",
            1_000_000,
        ),
        taker_change_output=TxOutput.from_address(
            "bcrt1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qzf4jry",
            490_000,
        ),
        maker_inputs={
            "maker1": [
                TxInput.from_hex(txid=u["txid"], vout=u["vout"], value=u["value"])
                for u in maker_utxos
            ],
        },
        maker_cj_outputs={
            "maker1": TxOutput.from_address(
                "bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080",
                1_000_000,
            ),
        },
        maker_change_outputs={
            "maker1": TxOutput.from_address(
                "bcrt1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qzf4jry",
                990_000,
            ),
        },
        cj_amount=1_000_000,
        total_maker_fee=10_000,
        tx_fee=5_000,
    )


class TestTakerInputIndexMapping:
    """Tests for correct input index mapping in shuffled transactions."""

    def test_input_index_map_creation(self, sample_coinjoin_tx_data: CoinJoinTxData) -> None:
        """Test that we can correctly map UTXOs to transaction input indices."""
        builder = CoinJoinTxBuilder(network="regtest")
        tx_bytes, metadata = builder.build_unsigned_tx(sample_coinjoin_tx_data)

        # Deserialize the transaction
        tx = deserialize_transaction(tx_bytes)

        # Build the input index map like _sign_our_inputs does
        input_index_map: dict[tuple[str, int], int] = {}
        for idx, tx_input in enumerate(tx.inputs):
            txid_hex = tx_input.txid_le[::-1].hex()
            input_index_map[(txid_hex, tx_input.vout)] = idx

        # Verify all taker inputs are in the map
        taker_txids = [("a" * 64, 0), ("b" * 64, 1)]
        for txid, vout in taker_txids:
            assert (txid, vout) in input_index_map, f"Taker UTXO {txid}:{vout} not found in map"

        # Verify maker inputs are also in the map
        maker_txids = [("c" * 64, 0), ("d" * 64, 2)]
        for txid, vout in maker_txids:
            assert (txid, vout) in input_index_map, f"Maker UTXO {txid}:{vout} not found in map"

    def test_input_owners_match_metadata(self, sample_coinjoin_tx_data: CoinJoinTxData) -> None:
        """Test that input owners in metadata correctly identify taker vs maker."""
        builder = CoinJoinTxBuilder(network="regtest")
        tx_bytes, metadata = builder.build_unsigned_tx(sample_coinjoin_tx_data)

        input_owners = metadata["input_owners"]

        # Should have 4 inputs total (2 taker + 2 maker)
        assert len(input_owners) == 4

        # Count owners
        taker_count = sum(1 for owner in input_owners if owner == "taker")
        maker_count = sum(1 for owner in input_owners if owner == "maker1")

        assert taker_count == 2, f"Expected 2 taker inputs, got {taker_count}"
        assert maker_count == 2, f"Expected 2 maker inputs, got {maker_count}"


class TestTakerSigning:
    """Tests for the taker signing implementation."""

    @pytest.fixture
    def mock_wallet(self, test_master_key: HDKey) -> MagicMock:
        """Create a mock wallet service."""
        wallet = MagicMock()
        wallet.network = "regtest"
        wallet.mixdepth_count = 5

        # Mock get_key_for_address to return proper HD keys
        def get_key_for_address(address: str) -> HDKey | None:
            # Map test addresses to their derivation paths
            key0 = test_master_key.derive("m/84'/1'/0'/0/0")
            key1 = test_master_key.derive("m/84'/1'/0'/0/1")

            if address == key0.get_address("regtest"):
                return key0
            elif address == key1.get_address("regtest"):
                return key1
            return None

        wallet.get_key_for_address = get_key_for_address
        return wallet

    @pytest.fixture
    def mock_backend(self) -> AsyncMock:
        """Create a mock blockchain backend."""
        backend = AsyncMock()
        backend.broadcast = AsyncMock(return_value="txid123")
        return backend

    @pytest.fixture
    def mock_config(self) -> MagicMock:
        """Create a mock taker config."""
        from jmcore.models import NetworkType

        config = MagicMock()
        config.network = NetworkType.REGTEST
        config.directory_servers = ["localhost:5222"]
        config.max_cj_fee = 0.01
        config.counterparty_count = 4
        config.minimum_makers = 2
        config.maker_timeout_sec = 60
        config.order_wait_time = 10
        config.taker_utxo_age = 5
        config.taker_utxo_amtpercent = 20
        config.tx_fee_factor = 1.0
        return config

    @pytest.mark.asyncio
    async def test_sign_our_inputs_basic(
        self,
        mock_wallet: MagicMock,
        mock_backend: AsyncMock,
        mock_config: MagicMock,
        taker_utxos: list[UTXOInfo],
        sample_coinjoin_tx_data: CoinJoinTxData,
    ) -> None:
        """Test that _sign_our_inputs produces valid signatures."""
        from taker.taker import Taker

        # Create taker instance
        with patch.object(Taker, "__init__", lambda self, *args, **kwargs: None):
            taker = Taker.__new__(Taker)
            taker.wallet = mock_wallet
            taker.backend = mock_backend
            taker.config = mock_config
            taker.maker_sessions = {}
            taker.selected_utxos = taker_utxos

            # Build the transaction
            builder = CoinJoinTxBuilder(network="regtest")
            tx_bytes, metadata = builder.build_unsigned_tx(sample_coinjoin_tx_data)
            taker.unsigned_tx = tx_bytes
            taker.tx_metadata = metadata

            # Sign the inputs
            signatures = await taker._sign_our_inputs()

            # Should have 2 signatures (one per taker UTXO)
            assert len(signatures) == 2

            # Verify signature structure
            for sig_info in signatures:
                assert "txid" in sig_info
                assert "vout" in sig_info
                assert "signature" in sig_info
                assert "pubkey" in sig_info
                assert "witness" in sig_info

                # Witness should have 2 items: signature and pubkey
                assert len(sig_info["witness"]) == 2

                # Signature should be hex string
                assert all(c in "0123456789abcdef" for c in sig_info["signature"])

                # Pubkey should be 33 bytes compressed (66 hex chars)
                assert len(sig_info["pubkey"]) == 66

    @pytest.mark.asyncio
    async def test_sign_our_inputs_correct_indices(
        self,
        mock_wallet: MagicMock,
        mock_backend: AsyncMock,
        mock_config: MagicMock,
        taker_utxos: list[UTXOInfo],
        sample_coinjoin_tx_data: CoinJoinTxData,
    ) -> None:
        """Test that signatures are created for correct input indices."""
        from taker.taker import Taker

        with patch.object(Taker, "__init__", lambda self, *args, **kwargs: None):
            taker = Taker.__new__(Taker)
            taker.wallet = mock_wallet
            taker.backend = mock_backend
            taker.config = mock_config
            taker.maker_sessions = {}
            taker.selected_utxos = taker_utxos

            builder = CoinJoinTxBuilder(network="regtest")
            tx_bytes, metadata = builder.build_unsigned_tx(sample_coinjoin_tx_data)
            taker.unsigned_tx = tx_bytes
            taker.tx_metadata = metadata

            signatures = await taker._sign_our_inputs()

            # Verify each signature corresponds to a taker UTXO
            signed_utxos = {(s["txid"], s["vout"]) for s in signatures}
            expected_utxos = {(u.txid, u.vout) for u in taker_utxos}

            assert signed_utxos == expected_utxos

    @pytest.mark.asyncio
    async def test_sign_our_inputs_empty_utxos(
        self,
        mock_wallet: MagicMock,
        mock_backend: AsyncMock,
        mock_config: MagicMock,
    ) -> None:
        """Test that signing with no UTXOs returns empty list."""
        from taker.taker import Taker

        with patch.object(Taker, "__init__", lambda self, *args, **kwargs: None):
            taker = Taker.__new__(Taker)
            taker.wallet = mock_wallet
            taker.backend = mock_backend
            taker.config = mock_config
            taker.selected_utxos = []
            taker.unsigned_tx = b"\x02\x00\x00\x00\x00\x01\x00\x00\x00\x00\x00\x00"

            signatures = await taker._sign_our_inputs()

            assert signatures == []

    @pytest.mark.asyncio
    async def test_sign_our_inputs_no_transaction(
        self,
        mock_wallet: MagicMock,
        mock_backend: AsyncMock,
        mock_config: MagicMock,
        taker_utxos: list[UTXOInfo],
    ) -> None:
        """Test that signing with no transaction returns empty list."""
        from taker.taker import Taker

        with patch.object(Taker, "__init__", lambda self, *args, **kwargs: None):
            taker = Taker.__new__(Taker)
            taker.wallet = mock_wallet
            taker.backend = mock_backend
            taker.config = mock_config
            taker.selected_utxos = taker_utxos
            taker.unsigned_tx = b""

            signatures = await taker._sign_our_inputs()

            assert signatures == []


class TestSignatureIntegration:
    """Integration tests for signature creation and application."""

    def test_add_signatures_raises_on_incomplete(
        self,
        test_master_key: HDKey,
        sample_coinjoin_tx_data: CoinJoinTxData,
    ) -> None:
        """Test that add_signatures raises ValueError when signatures are incomplete.

        A CoinJoin transaction is invalid unless every input is signed.
        Providing only the taker's signature while maker signatures are missing
        must be rejected.
        """
        from jmwallet.wallet.signing import (
            create_p2wpkh_script_code,
            create_witness_stack,
            deserialize_transaction,
            sign_p2wpkh_input,
        )

        builder = CoinJoinTxBuilder(network="regtest")
        tx_bytes, metadata = builder.build_unsigned_tx(sample_coinjoin_tx_data)

        tx = deserialize_transaction(tx_bytes)

        # Build input index map
        input_index_map: dict[tuple[str, int], int] = {}
        for idx, tx_input in enumerate(tx.inputs):
            txid_hex = tx_input.txid_le[::-1].hex()
            input_index_map[(txid_hex, tx_input.vout)] = idx

        # Get taker key and sign only the taker's first input
        key0 = test_master_key.derive("m/84'/1'/0'/0/0")
        pubkey_bytes = key0.get_public_key_bytes(compressed=True)
        script_code = create_p2wpkh_script_code(pubkey_bytes)

        taker_txid = "a" * 64
        assert (taker_txid, 0) in input_index_map
        input_index = input_index_map[(taker_txid, 0)]

        signature = sign_p2wpkh_input(
            tx=tx,
            input_index=input_index,
            script_code=script_code,
            value=1_000_000,
            private_key=key0.private_key,
        )

        witness = create_witness_stack(signature, pubkey_bytes)

        # Signature should be valid DER + sighash
        assert len(signature) > 64
        assert signature[-1] == 1  # SIGHASH_ALL

        # Witness stack should have 2 items
        assert len(witness) == 2

        # Only provide taker signature -- maker signatures are missing
        signatures = {
            "taker": [
                {
                    "txid": taker_txid,
                    "vout": 0,
                    "signature": signature.hex(),
                    "pubkey": pubkey_bytes.hex(),
                    "witness": [item.hex() for item in witness],
                }
            ]
        }

        # Must raise because maker inputs are unsigned
        with pytest.raises(ValueError, match="missing signatures"):
            builder.add_signatures(tx_bytes, signatures, metadata)


class TestEdgeCases:
    """Edge case tests for taker signing."""

    @pytest.mark.asyncio
    async def test_sign_with_missing_key(
        self,
        mock_backend: AsyncMock,
        mock_config: MagicMock,
        sample_coinjoin_tx_data: CoinJoinTxData,
    ) -> None:
        """Test handling when wallet doesn't have key for an address."""
        from taker.taker import Taker

        # Create wallet that returns None for get_key_for_address
        wallet = MagicMock()
        wallet.get_key_for_address = MagicMock(return_value=None)

        utxos = [
            UTXOInfo(
                txid="a" * 64,
                vout=0,
                value=1_000_000,
                address="unknown_address",
                confirmations=10,
                scriptpubkey="0014" + "00" * 20,
                path="m/84'/1'/0'/0/0",
                mixdepth=0,
            )
        ]

        with patch.object(Taker, "__init__", lambda self, *args, **kwargs: None):
            taker = Taker.__new__(Taker)
            taker.wallet = wallet
            taker.backend = mock_backend
            taker.config = mock_config
            taker.selected_utxos = utxos

            builder = CoinJoinTxBuilder(network="regtest")
            tx_bytes, metadata = builder.build_unsigned_tx(sample_coinjoin_tx_data)
            taker.unsigned_tx = tx_bytes
            taker.tx_metadata = metadata

            # Should return empty list when key not found (error logged)
            signatures = await taker._sign_our_inputs()

            # Should return empty due to missing key
            assert signatures == []

    @pytest.mark.asyncio
    async def test_sign_utxo_not_in_transaction(
        self,
        test_master_key: HDKey,
        mock_backend: AsyncMock,
        mock_config: MagicMock,
        sample_coinjoin_tx_data: CoinJoinTxData,
    ) -> None:
        """Test handling when UTXO is not found in transaction inputs."""
        from taker.taker import Taker

        key0 = test_master_key.derive("m/84'/1'/0'/0/0")
        addr0 = key0.get_address("regtest")

        # Create UTXO that won't be in the transaction
        utxos = [
            UTXOInfo(
                txid="z" * 64,  # Not in the transaction
                vout=99,
                value=1_000_000,
                address=addr0,
                confirmations=10,
                scriptpubkey="0014" + "00" * 20,
                path="m/84'/1'/0'/0/0",
                mixdepth=0,
            )
        ]

        wallet = MagicMock()
        wallet.get_key_for_address = MagicMock(return_value=key0)

        with patch.object(Taker, "__init__", lambda self, *args, **kwargs: None):
            taker = Taker.__new__(Taker)
            taker.wallet = wallet
            taker.backend = mock_backend
            taker.config = mock_config
            taker.selected_utxos = utxos

            builder = CoinJoinTxBuilder(network="regtest")
            tx_bytes, metadata = builder.build_unsigned_tx(sample_coinjoin_tx_data)
            taker.unsigned_tx = tx_bytes
            taker.tx_metadata = metadata

            # Should return empty list (UTXO not found in transaction)
            signatures = await taker._sign_our_inputs()

            assert signatures == []


class TestPhaseCollectSignaturesCompleteness:
    """Tests that _phase_collect_signatures requires ALL maker signatures.

    Once a transaction is built with specific maker inputs, every single
    maker must provide valid signatures. The transaction is cryptographically
    invalid if any input is unsigned. The minimum_makers threshold only
    applies during the initial maker selection (filling phase), not here.
    """

    @pytest.fixture
    def two_maker_tx_data(self) -> CoinJoinTxData:
        """CoinJoin with 2 makers (3 inputs total)."""
        return CoinJoinTxData(
            taker_inputs=[TxInput.from_hex(txid="a" * 64, vout=0, value=2_000_000)],
            taker_cj_output=TxOutput.from_address(
                "bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080",
                1_000_000,
            ),
            taker_change_output=TxOutput.from_address(
                "bcrt1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qzf4jry",
                990_000,
            ),
            maker_inputs={
                "maker1": [TxInput.from_hex(txid="b" * 64, vout=0, value=1_500_000)],
                "maker2": [TxInput.from_hex(txid="c" * 64, vout=0, value=1_200_000)],
            },
            maker_cj_outputs={
                "maker1": TxOutput.from_address(
                    "bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080",
                    1_000_000,
                ),
                "maker2": TxOutput.from_address(
                    "bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080",
                    1_000_000,
                ),
            },
            maker_change_outputs={
                "maker1": TxOutput.from_address(
                    "bcrt1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qzf4jry",
                    501_000,
                ),
                "maker2": TxOutput.from_address(
                    "bcrt1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qzf4jry",
                    201_000,
                ),
            },
            cj_amount=1_000_000,
            total_maker_fee=2_000,
            tx_fee=8_000,
        )

    @staticmethod
    def _make_maker_session(nick: str, offer: Any, utxos: list[dict[str, Any]]) -> Any:
        """Create a MakerSession with a mocked crypto field.

        MakerSession is a Pydantic dataclass that validates `crypto` as
        CryptoSession | None. We construct with crypto=None then monkey-patch
        it to a MagicMock so the encryption calls in _phase_collect_signatures
        work without real NaCl keys.
        """
        from taker.taker import MakerSession

        session = MakerSession(nick=nick, offer=offer, utxos=utxos)
        crypto = MagicMock()
        crypto.encrypt = MagicMock(return_value="encrypted")
        crypto.decrypt = MagicMock(return_value="decrypted")
        object.__setattr__(session, "crypto", crypto)
        return session

    def _build_taker_with_tx(
        self,
        tx_data: CoinJoinTxData,
        *,
        maker_sessions: dict[str, Any] | None = None,
    ) -> Any:
        """Create a Taker instance with a built transaction and mocked dependencies."""
        from taker.taker import Taker

        builder = CoinJoinTxBuilder(network="regtest")
        tx_bytes, metadata = builder.build_unsigned_tx(tx_data)

        with patch.object(Taker, "__init__", lambda self, *args, **kwargs: None):
            taker = Taker.__new__(Taker)
            taker.wallet = MagicMock()
            taker.backend = AsyncMock()
            taker.config = MagicMock()
            taker.config.network.value = "regtest"
            taker.config.maker_timeout_sec = 5
            taker.config.minimum_makers = 1  # Low threshold -- should NOT matter
            taker.config.data_dir = "/tmp/test"
            taker.unsigned_tx = tx_bytes
            taker.tx_metadata = metadata
            taker.selected_utxos = []
            taker.cj_amount = tx_data.cj_amount
            taker.cj_destination = "bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080"
            taker.taker_change_address = ""

            # Set up directory client mock
            taker.directory_client = MagicMock()
            taker.directory_client.send_privmsg = AsyncMock()

            if maker_sessions is not None:
                taker.maker_sessions = maker_sessions
            else:
                taker.maker_sessions = {}

            return taker

    @pytest.mark.asyncio
    async def test_rejects_when_one_maker_fails_to_respond(
        self,
        two_maker_tx_data: CoinJoinTxData,
    ) -> None:
        """Must fail if one maker doesn't respond, even if minimum_makers is met.

        Even with minimum_makers=1, once the tx is built with 2 makers,
        both must sign. A single missing maker means an invalid transaction.
        """
        from jmcore.models import NetworkType, Offer, OfferType

        offer = Offer(
            counterparty="maker1",
            oid=0,
            ordertype=OfferType.SW0_RELATIVE,
            minsize=100_000,
            maxsize=10_000_000,
            txfee=0,
            cjfee="0.001",
            fidelity_bond_value=0,
        )

        maker_sessions = {
            "maker1": self._make_maker_session(
                "maker1", offer, [{"txid": "b" * 64, "vout": 0, "value": 1_500_000}]
            ),
            "maker2": self._make_maker_session(
                "maker2", offer, [{"txid": "c" * 64, "vout": 0, "value": 1_200_000}]
            ),
        }

        taker = self._build_taker_with_tx(two_maker_tx_data, maker_sessions=maker_sessions)
        taker.config.network = NetworkType.REGTEST

        # Neither maker responds
        taker.directory_client.wait_for_responses = AsyncMock(return_value={})

        result = await taker._phase_collect_signatures()
        assert result is False, (
            "_phase_collect_signatures must fail when a maker whose inputs are "
            "in the transaction doesn't respond"
        )

    @pytest.mark.asyncio
    async def test_rejects_when_maker_provides_invalid_signature(
        self,
        two_maker_tx_data: CoinJoinTxData,
    ) -> None:
        """Must fail when a maker's signature fails verification.

        Even if both makers respond, if one provides an invalid signature
        that fails cryptographic verification, the transaction cannot proceed.
        """
        import base64

        from jmcore.models import NetworkType, Offer, OfferType

        offer = Offer(
            counterparty="maker1",
            oid=0,
            ordertype=OfferType.SW0_RELATIVE,
            minsize=100_000,
            maxsize=10_000_000,
            txfee=0,
            cjfee="0.001",
            fidelity_bond_value=0,
        )

        # Build a fake !sig response that will fail verification.
        # Format: sig_len(1) + sig + pub_len(1) + pubkey
        fake_sig = b"\x30" + b"\x44" * 70  # 71 bytes
        fake_pubkey = b"\x02" + b"\xab" * 32  # 33 bytes
        fake_payload = bytes([len(fake_sig)]) + fake_sig + bytes([len(fake_pubkey)]) + fake_pubkey
        fake_b64 = base64.b64encode(fake_payload).decode()

        maker_sessions = {
            "maker1": self._make_maker_session(
                "maker1", offer, [{"txid": "b" * 64, "vout": 0, "value": 1_500_000}]
            ),
            "maker2": self._make_maker_session(
                "maker2", offer, [{"txid": "c" * 64, "vout": 0, "value": 1_200_000}]
            ),
        }
        # Override decrypt to return the fake payload
        for session in maker_sessions.values():
            session.crypto.decrypt = MagicMock(return_value=fake_b64)

        taker = self._build_taker_with_tx(two_maker_tx_data, maker_sessions=maker_sessions)
        taker.config.network = NetworkType.REGTEST

        # Both makers respond, but their signatures are garbage
        taker.directory_client.wait_for_responses = AsyncMock(
            return_value={
                "maker1": {"data": [fake_b64]},
                "maker2": {"data": [fake_b64]},
            }
        )

        result = await taker._phase_collect_signatures()
        assert result is False, (
            "_phase_collect_signatures must fail when maker signatures "
            "fail cryptographic verification"
        )

    @pytest.mark.asyncio
    async def test_minimum_makers_is_irrelevant_after_tx_built(
        self,
        two_maker_tx_data: CoinJoinTxData,
    ) -> None:
        """minimum_makers=1 must not allow proceeding with only 1 of 2 makers.

        This is the core bug scenario: the old code checked
        len(maker_sessions) >= minimum_makers, which would pass with
        minimum_makers=1 even when 2 makers are needed.
        """
        from jmcore.models import NetworkType, Offer, OfferType

        offer = Offer(
            counterparty="maker1",
            oid=0,
            ordertype=OfferType.SW0_RELATIVE,
            minsize=100_000,
            maxsize=10_000_000,
            txfee=0,
            cjfee="0.001",
            fidelity_bond_value=0,
        )

        maker_sessions = {
            "maker1": self._make_maker_session(
                "maker1", offer, [{"txid": "b" * 64, "vout": 0, "value": 1_500_000}]
            ),
            "maker2": self._make_maker_session(
                "maker2", offer, [{"txid": "c" * 64, "vout": 0, "value": 1_200_000}]
            ),
        }

        taker = self._build_taker_with_tx(two_maker_tx_data, maker_sessions=maker_sessions)
        taker.config.network = NetworkType.REGTEST
        taker.config.minimum_makers = 1  # Explicitly low threshold

        # Neither maker responds
        taker.directory_client.wait_for_responses = AsyncMock(return_value={})

        result = await taker._phase_collect_signatures()
        assert result is False, (
            "With minimum_makers=1 and 2 makers in the transaction, "
            "_phase_collect_signatures must still fail when one maker "
            "doesn't respond. The old minimum_makers check would have "
            "incorrectly allowed this."
        )


# Re-export fixtures for use in conftest
@pytest.fixture
def mock_backend() -> AsyncMock:
    """Create a mock blockchain backend."""
    backend = AsyncMock()
    backend.broadcast = AsyncMock(return_value="txid123")
    return backend


@pytest.fixture
def mock_config() -> MagicMock:
    """Create a mock taker config."""
    from jmcore.models import NetworkType

    config = MagicMock()
    config.network = NetworkType.REGTEST
    config.directory_servers = ["localhost:5222"]
    config.max_cj_fee = 0.01
    config.counterparty_count = 4
    config.minimum_makers = 2
    config.maker_timeout_sec = 60
    config.order_wait_time = 120
    config.taker_utxo_age = 5
    config.taker_utxo_amtpercent = 20
    config.tx_fee_factor = 1.0
    return config
