"""
Tests for transaction builder module.
"""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from jmcore.bitcoin import (
    address_to_scriptpubkey,
    parse_transaction_bytes,
    serialize_outpoint,
    serialize_transaction,
)
from jmcore.constants import BITCOIN_DUST_THRESHOLD, DUST_THRESHOLD

from taker.tx_builder import (
    ChannelEndpointContribution,
    ChannelEndpointSettlement,
    ChannelTransactionContribution,
    CoinJoinTxBuilder,
    CoinJoinTxData,
    FinalizedRingChannelOutput,
    FinalizedRingTransactionPlan,
    TxInput,
    TxOutput,
    build_coinjoin_tx,
    calculate_tx_fee,
    compute_tx_locktime,
    varint,
)


class TestComputeTxLocktime:
    def test_uses_current_height_normally(self) -> None:
        rng = MockRandom([1])

        assert compute_tx_locktime(840_000, rng) == 840_000

    def test_randomly_backdates_within_reference_window(self) -> None:
        rng = MockRandom([0, 37])

        assert compute_tx_locktime(840_000, rng) == 839_963

    def test_backdating_is_bounded_at_one(self) -> None:
        rng = MockRandom([0, 99])

        assert compute_tx_locktime(42, rng) == 1

    def test_genesis_height_remains_final(self) -> None:
        assert compute_tx_locktime(0, MockRandom([])) == 0


class MockRandom:
    def __init__(self, values: list[int]):
        self._values = iter(values)

    def randint(self, start: int, end: int) -> int:
        value = next(self._values)
        assert start <= value <= end
        return value


class TestVarint:
    """Tests for varint encoding."""

    def test_single_byte(self) -> None:
        """Test single-byte varint (0-252)."""
        assert varint(0) == bytes([0x00])
        assert varint(1) == bytes([0x01])
        assert varint(252) == bytes([0xFC])

    def test_two_bytes(self) -> None:
        """Test two-byte varint (253-65535)."""
        result = varint(253)
        assert result[0] == 0xFD
        assert len(result) == 3

        result = varint(65535)
        assert result[0] == 0xFD
        assert len(result) == 3

    def test_four_bytes(self) -> None:
        """Test four-byte varint (65536-4294967295)."""
        result = varint(65536)
        assert result[0] == 0xFE
        assert len(result) == 5

    def test_eight_bytes(self) -> None:
        """Test eight-byte varint (> 4294967295)."""
        result = varint(4294967296)
        assert result[0] == 0xFF
        assert len(result) == 9


class TestSerializeOutpoint:
    """Tests for outpoint serialization."""

    def test_serialize_outpoint(self) -> None:
        """Test outpoint serialization reverses txid."""
        txid = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
        vout = 1

        result = serialize_outpoint(txid, vout)

        # Should be 32 bytes (reversed txid) + 4 bytes (vout)
        assert len(result) == 36

        # txid should be reversed (little-endian)
        expected_txid = bytes.fromhex(txid)[::-1]
        assert result[:32] == expected_txid

        # vout should be little-endian uint32
        assert result[32:36] == bytes([0x01, 0x00, 0x00, 0x00])


class TestAddressToScriptPubKey:
    """Tests for address to scriptPubKey conversion."""

    def test_p2wpkh_mainnet(self) -> None:
        """Test mainnet P2WPKH address."""
        # Known address from BIP-0173
        address = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"
        script = address_to_scriptpubkey(address)

        # P2WPKH: OP_0 <20-byte-hash>
        assert script[0] == 0x00
        assert script[1] == 0x14  # 20 bytes
        assert len(script) == 22

    def test_p2wpkh_testnet(self) -> None:
        """Test testnet P2WPKH address."""
        address = "tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx"
        script = address_to_scriptpubkey(address)

        assert script[0] == 0x00
        assert script[1] == 0x14
        assert len(script) == 22

    def test_p2wpkh_regtest(self) -> None:
        """Test regtest P2WPKH address."""
        address = "bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080"
        script = address_to_scriptpubkey(address)

        assert script[0] == 0x00
        assert script[1] == 0x14
        assert len(script) == 22

    def test_p2wsh_mainnet(self) -> None:
        """Test mainnet P2WSH address."""
        # 62-character bech32 address
        address = "bc1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qccfmv3"
        script = address_to_scriptpubkey(address)

        # P2WSH: OP_0 <32-byte-hash>
        assert script[0] == 0x00
        assert script[1] == 0x20  # 32 bytes
        assert len(script) == 34

    def test_p2pkh_mainnet(self) -> None:
        """Test mainnet P2PKH address."""
        address = "1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN2"
        script = address_to_scriptpubkey(address)

        # P2PKH: OP_DUP OP_HASH160 <20-byte-hash> OP_EQUALVERIFY OP_CHECKSIG
        assert script[0] == 0x76  # OP_DUP
        assert script[1] == 0xA9  # OP_HASH160
        assert script[2] == 0x14  # 20 bytes
        assert script[-2] == 0x88  # OP_EQUALVERIFY
        assert script[-1] == 0xAC  # OP_CHECKSIG
        assert len(script) == 25

    def test_p2sh_mainnet(self) -> None:
        """Test mainnet P2SH address."""
        address = "3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy"
        script = address_to_scriptpubkey(address)

        # P2SH: OP_HASH160 <20-byte-hash> OP_EQUAL
        assert script[0] == 0xA9  # OP_HASH160
        assert script[1] == 0x14  # 20 bytes
        assert script[-1] == 0x87  # OP_EQUAL
        assert len(script) == 23

    def test_invalid_bech32(self) -> None:
        """Test invalid bech32 address."""
        with pytest.raises(ValueError, match="Invalid bech32"):
            address_to_scriptpubkey("bc1invalid")

    def test_invalid_base58(self) -> None:
        """Test invalid base58 address."""
        with pytest.raises(Exception):  # base58 raises its own exception
            address_to_scriptpubkey("1InvalidAddress")


class TestTxInput:
    """Tests for TxInput dataclass."""

    def test_default_values(self) -> None:
        """Test default values."""
        inp = TxInput.from_hex(txid="a" * 64, vout=0, value=100000)
        assert inp.scriptpubkey_hex == ""
        assert inp.sequence == 0xFFFFFFFF

    def test_custom_sequence(self) -> None:
        """Test custom sequence number."""
        inp = TxInput.from_hex(txid="a" * 64, vout=1, value=50000, sequence=0xFFFFFFFE)
        assert inp.sequence == 0xFFFFFFFE


class TestTxOutput:
    """Tests for TxOutput dataclass."""

    def test_basic_output(self) -> None:
        """Test basic output creation."""
        out = TxOutput.from_address("bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4", 100000)
        assert out.address("mainnet") == "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"
        assert out.value == 100000


class TestCalculateTxFee:
    """Tests for transaction fee calculation."""

    def test_simple_fee_calculation(self) -> None:
        """Test simple fee calculation."""
        # 1 taker input, 2 maker inputs, 5 outputs (3 CJ + 2 change)
        fee = calculate_tx_fee(
            num_taker_inputs=1,
            num_maker_inputs=2,
            num_outputs=5,
            fee_rate=10,
        )

        # Expected: (3 * 68) + (5 * 31) + 11 = 204 + 155 + 11 = 370 vbytes
        # 370 * 10 = 3700 sats
        assert fee == 3700

    def test_larger_coinjoin(self) -> None:
        """Test fee for larger CoinJoin."""
        # 2 taker inputs, 8 maker inputs, 12 outputs
        fee = calculate_tx_fee(
            num_taker_inputs=2,
            num_maker_inputs=8,
            num_outputs=12,
            fee_rate=5,
        )

        # Expected: (10 * 68) + (12 * 31) + 11 = 680 + 372 + 11 = 1063 vbytes
        # 1063 * 5 = 5315 sats
        assert fee == 5315

    def test_taproot_fee_differs_from_segwit(self) -> None:
        """Taproot (p2tr) inputs/outputs are sized differently from segwit."""
        segwit = calculate_tx_fee(
            num_taker_inputs=1,
            num_maker_inputs=2,
            num_outputs=5,
            fee_rate=10,
            script_type="p2wpkh",
        )
        taproot = calculate_tx_fee(
            num_taker_inputs=1,
            num_maker_inputs=2,
            num_outputs=5,
            fee_rate=10,
            script_type="p2tr",
        )
        # p2tr key-path inputs are ~57.5 vbytes (vs 68) but p2tr outputs are
        # 43 vbytes (vs 31); the two families produce different estimates.
        assert taproot != segwit
        assert taproot > 0


class TestCoinJoinTxBuilder:
    """Tests for CoinJoinTxBuilder class."""

    @pytest.fixture
    def builder(self) -> CoinJoinTxBuilder:
        """Create a builder for tests."""
        return CoinJoinTxBuilder(network="regtest")

    @pytest.fixture
    def sample_tx_data(self) -> CoinJoinTxData:
        """Create sample transaction data."""
        return CoinJoinTxData(
            taker_inputs=[
                TxInput.from_hex(txid="a" * 64, vout=0, value=2_000_000),
            ],
            taker_cj_output=TxOutput.from_address(
                "bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080",
                1_000_000,
            ),
            taker_change_output=TxOutput.from_address(
                "bcrt1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qzf4jry",
                990_000,
            ),
            maker_inputs={
                "maker1": [TxInput.from_hex(txid="b" * 64, vout=1, value=1_500_000)],
                "maker2": [TxInput.from_hex(txid="c" * 64, vout=2, value=1_200_000)],
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

    def test_build_unsigned_tx(
        self, builder: CoinJoinTxBuilder, sample_tx_data: CoinJoinTxData
    ) -> None:
        """Test building an unsigned transaction."""
        tx_bytes, metadata = builder.build_unsigned_tx(sample_tx_data)

        # Check that we got bytes
        assert isinstance(tx_bytes, bytes)
        assert len(tx_bytes) > 0

        # Check metadata
        assert "input_owners" in metadata
        assert "output_owners" in metadata
        assert "input_values" in metadata

        # Should have 3 inputs (1 taker + 2 makers)
        assert len(metadata["input_owners"]) == 3

        # Should have 6 outputs (3 CJ + 3 change)
        assert len(metadata["output_owners"]) == 6

    def test_tx_has_correct_version(
        self, builder: CoinJoinTxBuilder, sample_tx_data: CoinJoinTxData
    ) -> None:
        """Test that transaction has version 2."""
        tx_bytes, _ = builder.build_unsigned_tx(sample_tx_data)

        # Version is first 4 bytes (little-endian)
        version = int.from_bytes(tx_bytes[:4], "little")
        assert version == 2

    def test_unsigned_tx_has_no_segwit_marker(
        self, builder: CoinJoinTxBuilder, sample_tx_data: CoinJoinTxData
    ) -> None:
        """Test that unsigned transaction has NO SegWit marker (non-SegWit format).

        Unsigned transactions use the traditional format without marker/flag/witness.
        The SegWit marker (0x00, 0x01) is only added when signatures/witnesses are present.
        This is required for compatibility with reference JoinMarket implementation.
        """
        tx_bytes, _ = builder.build_unsigned_tx(sample_tx_data)

        # After version (4 bytes), the next byte should be the input count (3 in our test data)
        # NOT the SegWit marker (0x00)
        assert tx_bytes[4] == 3  # Input count, not marker

    def test_parse_tx_roundtrip(
        self, builder: CoinJoinTxBuilder, sample_tx_data: CoinJoinTxData
    ) -> None:
        """Test that parsing and re-serializing produces same result."""
        from jmcore.bitcoin import parse_transaction_bytes

        tx_bytes, _ = builder.build_unsigned_tx(sample_tx_data)

        # Parse
        parsed = parse_transaction_bytes(tx_bytes)

        # Verify counts
        assert len(parsed.inputs) == 3
        assert len(parsed.outputs) == 6
        assert parsed.version == 2
        assert parsed.locktime == 0

    def test_nonzero_locktime_enables_all_inputs_without_signaling_rbf(
        self, sample_tx_data: CoinJoinTxData
    ) -> None:
        builder = CoinJoinTxBuilder(network="regtest", locktime=840_000)

        tx_bytes, _ = builder.build_unsigned_tx(sample_tx_data)
        parsed = parse_transaction_bytes(tx_bytes)

        assert parsed.locktime == 840_000
        assert {tx_input.sequence for tx_input in parsed.inputs} == {0xFFFFFFFE}
        assert sample_tx_data.taker_inputs[0].sequence == 0xFFFFFFFF

    def test_get_txid(self, builder: CoinJoinTxBuilder, sample_tx_data: CoinJoinTxData) -> None:
        """Test txid calculation."""
        tx_bytes, _ = builder.build_unsigned_tx(sample_tx_data)
        txid = builder.get_txid(tx_bytes)

        # Should be 64 hex characters
        assert len(txid) == 64
        assert all(c in "0123456789abcdef" for c in txid)


class TestChannelInputTransactionContributions:
    """Tests for channel funding inputs and endpoint settlement outputs."""

    @staticmethod
    def _p2tr_output(marker: int, value: int) -> TxOutput:
        return TxOutput.from_hex("5120" + f"{marker:02x}" * 32, value)

    def _contribution(
        self,
        channel_id: str = "channel-1",
        funding_marker: int = 0xD1,
        first_marker: int = 0xE1,
        second_marker: int = 0xE2,
        funding_value: int = 1_000,
    ) -> ChannelTransactionContribution:
        return ChannelTransactionContribution(
            channel_id=channel_id,
            funding_input=TxInput.from_hex(
                txid=f"{funding_marker:02x}" * 32,
                vout=7,
                value=funding_value,
                scriptpubkey="5120" + f"{funding_marker:02x}" * 32,
            ),
            first_endpoint=ChannelEndpointSettlement(
                endpoint_id=f"{channel_id}-maker",
                participant_key="maker-participant-key",
                output=self._p2tr_output(first_marker, 300),
            ),
            second_endpoint=ChannelEndpointSettlement(
                endpoint_id=f"{channel_id}-taker",
                participant_key="taker-participant-key",
                output=self._p2tr_output(second_marker, 400),
            ),
        )

    def _tx_data(
        self, contributions: tuple[ChannelTransactionContribution, ...] = ()
    ) -> CoinJoinTxData:
        return CoinJoinTxData(
            taker_inputs=[TxInput.from_hex(txid="a" * 64, vout=0, value=1_000)],
            taker_cj_output=self._p2tr_output(0xA1, 400),
            taker_change_output=self._p2tr_output(0xA2, 1_000),
            maker_inputs={
                "maker1": [TxInput.from_hex(txid="b" * 64, vout=1, value=2_000)],
            },
            maker_cj_outputs={"maker1": self._p2tr_output(0xA3, 400)},
            maker_change_outputs={},
            cj_amount=400,
            total_maker_fee=0,
            tx_fee=42,
            channel_contributions=contributions,
        )

    def _ring_plan(self) -> FinalizedRingTransactionPlan:
        participants = ("taker", "maker1", "maker2", "maker3")
        outputs = []
        for index, opener in enumerate(participants):
            fundee = participants[(index + 1) % len(participants)]
            script_pubkey = "5120" + f"{0xC1 + index:02x}" * 32
            outputs.append(
                FinalizedRingChannelOutput(
                    edge_id=f"edge-{index}",
                    script_pubkey=script_pubkey,
                    address=TxOutput.from_hex(script_pubkey, 2).address("regtest"),
                    capacity=2,
                    opener=ChannelEndpointContribution(participant_id=opener, amount=1),
                    fundee=ChannelEndpointContribution(participant_id=fundee, amount=1),
                )
            )
        return FinalizedRingTransactionPlan(
            network="regtest",
            participant_ids=list(participants),
            residuals={participant: 2 for participant in participants},
            channel_outputs=outputs,
        )

    def test_ordinary_transaction_keeps_compatibility_metadata(self) -> None:
        tx_data = self._tx_data()

        _, metadata = CoinJoinTxBuilder("regtest").build_unsigned_tx(tx_data)

        assert metadata["fee"] == tx_data.tx_fee
        assert metadata["actual_fee"] == 1_200
        assert metadata["channel_input_ids"] == [None, None]
        assert metadata["channel_endpoint_outputs"] == [None, None, None]
        assert len(metadata["input_scriptpubkeys"]) == len(metadata["input_owners"])

    def test_channel_contributions_survive_shared_shuffle_with_exact_metadata(self) -> None:
        first = self._contribution()
        second = self._contribution("channel-2", 0xD2, 0xE3, 0xE4, 1_100)
        tx_data = self._tx_data((first, second))

        tx_bytes, metadata = CoinJoinTxBuilder("regtest").build_unsigned_tx(tx_data)
        parsed = parse_transaction_bytes(tx_bytes)
        contributions = {contribution.channel_id: contribution for contribution in (first, second)}

        assert len(parsed.inputs) == 4
        assert len(parsed.outputs) == 7
        assert metadata["fee"] == tx_data.tx_fee
        assert metadata["actual_fee"] == 1_900
        for index, channel_id in enumerate(metadata["channel_input_ids"]):
            if channel_id is None:
                continue
            funding_input = contributions[channel_id].funding_input
            assert parsed.inputs[index].txid == funding_input.txid
            assert parsed.inputs[index].vout == funding_input.vout
            assert metadata["input_scriptpubkeys"][index] == funding_input.scriptpubkey_hex
            assert metadata["input_owners"][index] == f"__channel_input__:{channel_id}"

        endpoint_outputs = [
            descriptor
            for descriptor in metadata["channel_endpoint_outputs"]
            if descriptor is not None
        ]
        assert len(endpoint_outputs) == 4
        for index, descriptor in enumerate(metadata["channel_endpoint_outputs"]):
            if descriptor is None:
                continue
            output = parsed.outputs[index]
            assert output.scriptpubkey == descriptor["script_pubkey"]
            assert output.value == descriptor["value"]
            assert descriptor["channel_id"] in contributions
            assert descriptor["endpoint_id"] in {
                contributions[descriptor["channel_id"]].first_endpoint.endpoint_id,
                contributions[descriptor["channel_id"]].second_endpoint.endpoint_id,
            }
        json.dumps(metadata["channel_endpoint_outputs"])

    def test_channel_contributions_use_the_existing_single_shuffle_stage(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        contribution = self._contribution()
        shuffle_sizes: list[int] = []

        def reverse_shuffle(items: list[object]) -> None:
            shuffle_sizes.append(len(items))
            items.reverse()

        monkeypatch.setattr(
            "taker.tx_builder.secure_random", SimpleNamespace(shuffle=reverse_shuffle)
        )
        tx_bytes, metadata = CoinJoinTxBuilder("regtest").build_unsigned_tx(
            self._tx_data((contribution,))
        )
        parsed = parse_transaction_bytes(tx_bytes)

        assert shuffle_sizes == [3, 5]
        assert [input_.txid for input_ in parsed.inputs] == [
            contribution.funding_input.txid,
            "b" * 64,
            "a" * 64,
        ]
        assert [output.scriptpubkey for output in parsed.outputs] == [
            contribution.second_endpoint.output.scriptpubkey,
            contribution.first_endpoint.output.scriptpubkey,
            self._p2tr_output(0xA2, 1_000).scriptpubkey,
            self._p2tr_output(0xA3, 400).scriptpubkey,
            self._p2tr_output(0xA1, 400).scriptpubkey,
        ]
        assert metadata["channel_input_ids"] == ["channel-1", None, None]
        assert [
            descriptor["endpoint_id"] if descriptor is not None else None
            for descriptor in metadata["channel_endpoint_outputs"]
        ] == ["channel-1-taker", "channel-1-maker", None, None, None]

    def test_build_coinjoin_convenience_forwards_channel_contributions(self) -> None:
        contribution = self._contribution()
        taker_script = self._p2tr_output(0xA5, 400).scriptpubkey

        tx_bytes, metadata = build_coinjoin_tx(
            taker_utxos=[
                {
                    "txid": "a" * 64,
                    "vout": 0,
                    "value": 1_000,
                    "scriptpubkey": taker_script,
                }
            ],
            taker_cj_address=TxOutput.from_hex(taker_script, 400).address("regtest"),
            taker_change_address="",
            taker_total_input=1_000,
            maker_data={},
            cj_amount=400,
            tx_fee=600,
            network="regtest",
            channel_contributions=(contribution,),
        )

        parsed = parse_transaction_bytes(tx_bytes)
        assert len(parsed.inputs) == 2
        assert len(parsed.outputs) == 3
        assert contribution.channel_id in metadata["channel_input_ids"]

    def test_rejects_ring_and_channel_input_modes_together(self) -> None:
        tx_data = self._tx_data((self._contribution(),))
        tx_data = replace(tx_data, ring_plan=self._ring_plan())

        with pytest.raises(ValueError, match="cannot be ring transactions"):
            CoinJoinTxBuilder("regtest").build_unsigned_tx(tx_data)

    @pytest.mark.parametrize(
        ("contributions", "match"),
        [
            (("",), "nonempty channel ID"),
            (("channel-1", "channel-1"), "duplicate channel IDs"),
        ],
    )
    def test_rejects_invalid_channel_ids(self, contributions: tuple[str, ...], match: str) -> None:
        channel_contributions = tuple(
            self._contribution(channel_id, 0xD1 + index, 0xE1 + index * 2, 0xE2 + index * 2)
            for index, channel_id in enumerate(contributions)
        )

        with pytest.raises(ValueError, match=match):
            CoinJoinTxBuilder("regtest").build_unsigned_tx(self._tx_data(channel_contributions))

    def test_rejects_duplicate_or_colliding_channel_funding_outpoints(self) -> None:
        duplicate = self._contribution("channel-2", 0xD1, 0xE3, 0xE4)
        with pytest.raises(ValueError, match="duplicate funding outpoints"):
            CoinJoinTxBuilder("regtest").build_unsigned_tx(
                self._tx_data((self._contribution(), duplicate))
            )

        colliding = replace(
            self._contribution(),
            funding_input=TxInput.from_hex(
                txid="a" * 64,
                vout=0,
                value=1_000,
                scriptpubkey="5120" + "d1" * 32,
            ),
        )
        with pytest.raises(ValueError, match="collides with an ordinary input"):
            CoinJoinTxBuilder("regtest").build_unsigned_tx(self._tx_data((colliding,)))

    @pytest.mark.parametrize(
        ("funding_input", "match"),
        [
            (
                TxInput.from_hex(txid="d1" * 32, vout=7, value=0, scriptpubkey="5120" + "d1" * 32),
                "positive value",
            ),
            (
                TxInput.from_hex(
                    txid="d1" * 32, vout=7, value=1_000, scriptpubkey="0014" + "d1" * 20
                ),
                "P2TR",
            ),
        ],
    )
    def test_rejects_invalid_channel_funding_input(
        self, funding_input: TxInput, match: str
    ) -> None:
        contribution = replace(self._contribution(), funding_input=funding_input)

        with pytest.raises(ValueError, match=match):
            CoinJoinTxBuilder("regtest").build_unsigned_tx(self._tx_data((contribution,)))

    def test_rejects_invalid_channel_endpoint_settlements(self) -> None:
        contribution = self._contribution()
        duplicate_endpoint = replace(
            contribution,
            second_endpoint=replace(contribution.second_endpoint, endpoint_id="channel-1-maker"),
        )
        with pytest.raises(ValueError, match="distinct endpoint IDs"):
            CoinJoinTxBuilder("regtest").build_unsigned_tx(self._tx_data((duplicate_endpoint,)))

        missing_participant = replace(
            contribution,
            first_endpoint=replace(contribution.first_endpoint, participant_key=""),
        )
        with pytest.raises(ValueError, match="nonempty participant key"):
            CoinJoinTxBuilder("regtest").build_unsigned_tx(self._tx_data((missing_participant,)))

        missing_endpoint_id = replace(
            contribution,
            first_endpoint=replace(contribution.first_endpoint, endpoint_id=""),
        )
        with pytest.raises(ValueError, match="nonempty endpoint ID"):
            CoinJoinTxBuilder("regtest").build_unsigned_tx(self._tx_data((missing_endpoint_id,)))

        invalid_output = replace(
            contribution,
            first_endpoint=replace(
                contribution.first_endpoint,
                output=TxOutput.from_hex("0014" + "e1" * 20, 300),
            ),
        )
        with pytest.raises(ValueError, match="P2TR"):
            CoinJoinTxBuilder("regtest").build_unsigned_tx(self._tx_data((invalid_output,)))

        zero_value = replace(
            contribution,
            first_endpoint=replace(
                contribution.first_endpoint,
                output=self._p2tr_output(0xE1, 0),
            ),
        )
        with pytest.raises(ValueError, match="positive value"):
            CoinJoinTxBuilder("regtest").build_unsigned_tx(self._tx_data((zero_value,)))

    def test_rejects_duplicate_output_scripts_in_channel_mode(self) -> None:
        contribution = replace(
            self._contribution(),
            first_endpoint=ChannelEndpointSettlement(
                endpoint_id="channel-1-maker",
                participant_key="maker-participant-key",
                output=self._p2tr_output(0xA1, 300),
            ),
        )

        with pytest.raises(ValueError, match="duplicate output scripts"):
            CoinJoinTxBuilder("regtest").build_unsigned_tx(self._tx_data((contribution,)))

        ordinary_duplicate = self._tx_data((self._contribution(),))
        ordinary_duplicate = replace(
            ordinary_duplicate,
            maker_cj_outputs={"maker1": self._p2tr_output(0xA1, 400)},
        )
        with pytest.raises(ValueError, match="duplicate output scripts"):
            CoinJoinTxBuilder("regtest").build_unsigned_tx(ordinary_duplicate)


class TestBuildCoinjoinTx:
    """Tests for build_coinjoin_tx convenience function."""

    def test_build_simple_coinjoin(self) -> None:
        """Test building a simple CoinJoin transaction."""
        taker_utxos = [
            {"txid": "a" * 64, "vout": 0, "value": 2_000_000},
        ]
        maker_data = {
            "maker1": {
                "utxos": [{"txid": "b" * 64, "vout": 1, "value": 1_500_000}],
                "cj_addr": "bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080",
                "change_addr": "bcrt1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qzf4jry",
                "cjfee": 1000,
            },
        }

        tx_bytes, metadata = build_coinjoin_tx(
            taker_utxos=taker_utxos,
            taker_cj_address="bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080",
            taker_change_address="bcrt1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qzf4jry",
            taker_total_input=2_000_000,
            maker_data=maker_data,
            cj_amount=1_000_000,
            tx_fee=5000,
            network="regtest",
        )

        assert isinstance(tx_bytes, bytes)
        assert len(tx_bytes) > 0

        # Should have 2 inputs
        assert len(metadata["input_owners"]) == 2

        # Should have 4 outputs (2 CJ + 2 change)
        assert len(metadata["output_owners"]) == 4

    def test_build_coinjoin_dust_change_excluded(self) -> None:
        """Test that dust change outputs are excluded."""
        taker_utxos = [
            {
                "txid": "a" * 64,
                "vout": 0,
                "value": 1_001_500,
            },  # Just enough for CJ + fee + tiny change
        ]
        maker_data = {
            "maker1": {
                "utxos": [{"txid": "b" * 64, "vout": 1, "value": 1_000_500}],  # Just enough
                "cj_addr": "bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080",
                "change_addr": "bcrt1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qzf4jry",
                "cjfee": 500,  # Maker gets this fee
            },
        }

        tx_bytes, metadata = build_coinjoin_tx(
            taker_utxos=taker_utxos,
            taker_cj_address="bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080",
            taker_change_address="bcrt1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qzf4jry",
            taker_total_input=1_001_500,
            maker_data=maker_data,
            cj_amount=1_000_000,
            tx_fee=500,
            network="regtest",
        )

        # Taker change: 1_001_500 - 1_000_000 - 500 - 500 = 500 (dust, excluded)
        # Maker change: 1_000_500 - 1_000_000 + 500 = 1000 (dust, excluded with 27300 threshold)
        # So only 2 outputs: 2 CJ outputs
        change_outputs = [o for o in metadata["output_owners"] if o[1] == "change"]
        assert len(change_outputs) == 0

    def test_build_coinjoin_negative_maker_change_raises_error(self) -> None:
        """Test that negative maker change raises ValueError.

        This can happen when maker UTXO verification fails (value=0)
        or maker's UTXOs were spent between offer and coinjoin.
        """
        taker_utxos = [
            {
                "txid": "a" * 64,
                "vout": 0,
                "value": 2_000_000,
            },
        ]
        # Maker has 0 value UTXOs (verification failed)
        maker_data = {
            "maker1": {
                "utxos": [{"txid": "b" * 64, "vout": 1, "value": 0}],  # Verification failed!
                "cj_addr": "bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080",
                "change_addr": "bcrt1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qzf4jry",
                "cjfee": 500,
            },
        }

        import pytest

        with pytest.raises(ValueError, match="has insufficient funds"):
            build_coinjoin_tx(
                taker_utxos=taker_utxos,
                taker_cj_address="bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080",
                taker_change_address="bcrt1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qzf4jry",
                taker_total_input=2_000_000,
                maker_data=maker_data,
                cj_amount=1_000_000,
                tx_fee=5000,
                network="regtest",
            )

    def test_build_coinjoin_keeps_taker_change_below_maker_threshold(self) -> None:
        """The maker threshold must not discard economical taker change."""
        taker_utxos = [
            {
                "txid": "a" * 64,
                "vout": 0,
                "value": 1_004_500,
            },
        ]
        maker_data = {
            "maker1": {
                "utxos": [{"txid": "b" * 64, "vout": 1, "value": 1_026_799}],
                "cj_addr": "bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080",
                "change_addr": "bcrt1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qzf4jry",
                "cjfee": 500,
            },
        }

        _, metadata = build_coinjoin_tx(
            taker_utxos=taker_utxos,
            taker_cj_address="bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080",
            taker_change_address="bcrt1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qzf4jry",
            taker_total_input=1_004_500,
            maker_data=maker_data,
            cj_amount=1_000_000,
            tx_fee=1000,
            network="regtest",
        )

        # Taker change is 3,000 sats and maker change is 27,299 sats. The taker
        # output remains because it uses the lower 2,730-sat cutoff, while the
        # maker output is below the fixed 27,300-sat coordination threshold.
        change_outputs = [o for o in metadata["output_owners"] if o[1] == "change"]
        assert change_outputs == [("taker", "change")]

    @pytest.mark.parametrize(
        ("change_value", "expected_change_outputs"),
        [
            (BITCOIN_DUST_THRESHOLD, 0),
            (BITCOIN_DUST_THRESHOLD + 1, 1),
            (DUST_THRESHOLD, 1),
        ],
    )
    def test_build_coinjoin_taker_change_boundary(
        self, change_value: int, expected_change_outputs: int
    ) -> None:
        """Taker change follows the reference implementation's 2,730-sat cutoff."""
        _, metadata = build_coinjoin_tx(
            taker_utxos=[
                {
                    "txid": "a" * 64,
                    "vout": 0,
                    "value": 1_000_000 + 1000 + change_value,
                }
            ],
            taker_cj_address="bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080",
            taker_change_address=(
                "bcrt1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qzf4jry"
            ),
            taker_total_input=1_000_000 + 1000 + change_value,
            maker_data={},
            cj_amount=1_000_000,
            tx_fee=1000,
            network="regtest",
        )

        change_outputs = [o for o in metadata["output_owners"] if o[1] == "change"]
        assert len(change_outputs) == expected_change_outputs

    def test_build_coinjoin_rejects_nonstandard_maker_threshold(self) -> None:
        """The legacy builder keyword cannot change the coordination constant."""
        with pytest.raises(ValueError, match="fixed at 27300"):
            build_coinjoin_tx(
                taker_utxos=[{"txid": "a" * 64, "vout": 0, "value": 1_001_000}],
                taker_cj_address="bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080",
                taker_change_address="",
                taker_total_input=1_001_000,
                maker_data={},
                cj_amount=1_000_000,
                tx_fee=1000,
                network="regtest",
                dust_threshold=DUST_THRESHOLD + 1,
            )

    @pytest.mark.parametrize(
        ("change_value", "expected_change_outputs"),
        [(DUST_THRESHOLD - 1, 0), (DUST_THRESHOLD, 1), (DUST_THRESHOLD + 1, 1)],
    )
    def test_build_coinjoin_maker_change_boundary(
        self, change_value: int, expected_change_outputs: int
    ) -> None:
        """Maker change follows the fixed reference coordination threshold."""
        _, metadata = build_coinjoin_tx(
            taker_utxos=[{"txid": "a" * 64, "vout": 0, "value": 1_001_500}],
            taker_cj_address="bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080",
            taker_change_address="",
            taker_total_input=1_001_500,
            maker_data={
                "maker1": {
                    "utxos": [
                        {
                            "txid": "b" * 64,
                            "vout": 1,
                            "value": 1_000_000 - 500 + change_value,
                        }
                    ],
                    "cj_addr": "bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080",
                    "change_addr": (
                        "bcrt1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qzf4jry"
                    ),
                    "cjfee": 500,
                }
            },
            cj_amount=1_000_000,
            tx_fee=1000,
            network="regtest",
        )

        maker_change_outputs = [
            owner for owner in metadata["output_owners"] if owner == ("maker1", "change")
        ]
        assert len(maker_change_outputs) == expected_change_outputs


class TestAddSignaturesValidation:
    """Tests that add_signatures enforces complete signature coverage.

    A CoinJoin transaction is only valid when every input has a signature.
    The add_signatures method must reject any attempt to assemble a
    transaction with missing signatures rather than silently producing
    an invalid transaction.
    """

    @pytest.fixture
    def builder(self) -> CoinJoinTxBuilder:
        return CoinJoinTxBuilder(network="regtest", locktime=840_000)

    @pytest.fixture
    def two_maker_tx(self, builder: CoinJoinTxBuilder) -> tuple[bytes, dict]:
        """Build a transaction with 1 taker + 2 makers (3 inputs)."""
        tx_data = CoinJoinTxData(
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
                "maker1": [TxInput.from_hex(txid="b" * 64, vout=1, value=1_500_000)],
                "maker2": [TxInput.from_hex(txid="c" * 64, vout=2, value=1_200_000)],
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
        return builder.build_unsigned_tx(tx_data)

    def _make_fake_sig(self, txid: str, vout: int) -> dict:
        """Create a fake signature dict for testing assembly logic."""
        fake_sig = "30" + "44" * 35  # fake DER-ish hex
        fake_pubkey = "02" + "ab" * 32  # fake compressed pubkey hex
        return {
            "txid": txid,
            "vout": vout,
            "witness": [fake_sig, fake_pubkey],
        }

    def test_raises_when_no_signatures_provided(
        self, builder: CoinJoinTxBuilder, two_maker_tx: tuple[bytes, dict]
    ) -> None:
        """Empty signature dict must be rejected."""
        tx_bytes, metadata = two_maker_tx
        with pytest.raises(ValueError, match="missing signatures"):
            builder.add_signatures(tx_bytes, {}, metadata)

    def test_raises_when_maker_missing(
        self, builder: CoinJoinTxBuilder, two_maker_tx: tuple[bytes, dict]
    ) -> None:
        """Transaction with one maker's signatures missing must be rejected."""
        tx_bytes, metadata = two_maker_tx

        # Provide taker + maker1 only, maker2 is missing
        signatures = {
            "taker": [self._make_fake_sig("a" * 64, 0)],
            "maker1": [self._make_fake_sig("b" * 64, 1)],
            # maker2 deliberately omitted
        }
        with pytest.raises(ValueError, match="missing signatures"):
            builder.add_signatures(tx_bytes, signatures, metadata)

    def test_raises_when_taker_missing(
        self, builder: CoinJoinTxBuilder, two_maker_tx: tuple[bytes, dict]
    ) -> None:
        """Transaction with taker's signature missing must be rejected."""
        tx_bytes, metadata = two_maker_tx

        signatures = {
            # taker deliberately omitted
            "maker1": [self._make_fake_sig("b" * 64, 1)],
            "maker2": [self._make_fake_sig("c" * 64, 2)],
        }
        with pytest.raises(ValueError, match="missing signatures"):
            builder.add_signatures(tx_bytes, signatures, metadata)

    def test_raises_when_maker_provides_empty_list(
        self, builder: CoinJoinTxBuilder, two_maker_tx: tuple[bytes, dict]
    ) -> None:
        """Maker present in dict but with empty signature list must be rejected."""
        tx_bytes, metadata = two_maker_tx

        signatures = {
            "taker": [self._make_fake_sig("a" * 64, 0)],
            "maker1": [self._make_fake_sig("b" * 64, 1)],
            "maker2": [],  # present but empty
        }
        with pytest.raises(ValueError, match="missing signatures"):
            builder.add_signatures(tx_bytes, signatures, metadata)

    def test_raises_when_maker_provides_wrong_utxo(
        self, builder: CoinJoinTxBuilder, two_maker_tx: tuple[bytes, dict]
    ) -> None:
        """Maker signature for a different UTXO (not in the transaction) must be rejected."""
        tx_bytes, metadata = two_maker_tx

        signatures = {
            "taker": [self._make_fake_sig("a" * 64, 0)],
            "maker1": [self._make_fake_sig("b" * 64, 1)],
            "maker2": [self._make_fake_sig("f" * 64, 99)],  # wrong UTXO
        }
        with pytest.raises(ValueError, match="missing signatures"):
            builder.add_signatures(tx_bytes, signatures, metadata)

    def test_succeeds_when_all_signatures_present(
        self, builder: CoinJoinTxBuilder, two_maker_tx: tuple[bytes, dict]
    ) -> None:
        """Transaction with all signatures present must succeed."""
        tx_bytes, metadata = two_maker_tx

        signatures = {
            "taker": [self._make_fake_sig("a" * 64, 0)],
            "maker1": [self._make_fake_sig("b" * 64, 1)],
            "maker2": [self._make_fake_sig("c" * 64, 2)],
        }
        signed_tx = builder.add_signatures(tx_bytes, signatures, metadata)
        parsed = parse_transaction_bytes(signed_tx)
        non_witness_tx = serialize_transaction(
            parsed.version,
            parsed.inputs,
            parsed.outputs,
            parsed.locktime,
        )

        assert non_witness_tx == tx_bytes
        assert parsed.locktime == 840_000
        assert {tx_input.sequence for tx_input in parsed.inputs} == {0xFFFFFFFE}
