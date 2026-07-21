"""Unit tests for jmwallet.wallet.spend — direct-send transaction building."""

from __future__ import annotations

import math
import time
from hashlib import sha256
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jmcore.btc_script import mk_freeze_script

from jmwallet.backends.base import BlockchainBackend
from jmwallet.wallet.models import UTXOInfo
from jmwallet.wallet.spend import (
    DUST_THRESHOLD,
    DirectSendResult,
    ExcessiveFeeRateError,
    _build_unsigned_tx,
    _decode_bech32_scriptpubkey,
    direct_send,
    enforce_fee_rate_cap,
    estimate_fee,
    select_spendable_utxos,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_utxo(
    *,
    txid: str = "aa" * 32,
    vout: int = 0,
    value: int = 100_000,
    address: str = "bcrt1qq6hag67dl53wl99vzg42z8eyzfz2xlkvwk6f7m",
    confirmations: int = 10,
    scriptpubkey: str = "0014" + "bb" * 20,
    path: str = "m/84'/0'/0'/0/0",
    mixdepth: int = 0,
    frozen: bool = False,
    locktime: int | None = None,
) -> UTXOInfo:
    return UTXOInfo(
        txid=txid,
        vout=vout,
        value=value,
        address=address,
        confirmations=confirmations,
        scriptpubkey=scriptpubkey,
        path=path,
        mixdepth=mixdepth,
        frozen=frozen,
        locktime=locktime,
    )


REGTEST_P2WPKH_ADDR = "bcrt1qq6hag67dl53wl99vzg42z8eyzfz2xlkvwk6f7m"

# ---------------------------------------------------------------------------
# BlockchainBackend.get_median_time_past
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_median_time_past_uses_last_eleven_blocks() -> None:
    backend = MagicMock(spec=BlockchainBackend)
    backend.get_block_height = AsyncMock(return_value=20)
    timestamps = [500, 100, 900, 300, 700, 200, 600, 400, 1_000, 800, 1_100]
    backend.get_block_time = AsyncMock(side_effect=lambda height: timestamps[height - 10])

    median = await BlockchainBackend.get_median_time_past(backend)

    assert median == 600
    assert [call.args[0] for call in backend.get_block_time.await_args_list] == list(range(10, 21))


# ---------------------------------------------------------------------------
# _decode_bech32_scriptpubkey
# ---------------------------------------------------------------------------


class TestDecodeBech32Scriptpubkey:
    """Test bech32 address → scriptPubKey decoding."""

    def test_p2wpkh_regtest(self) -> None:
        """Decode a standard P2WPKH regtest address."""
        script = _decode_bech32_scriptpubkey(REGTEST_P2WPKH_ADDR, network="regtest")
        # P2WPKH: OP_0 PUSH20 <20-byte-hash>
        assert script[0:2] == bytes([0x00, 0x14])
        assert len(script) == 22

    def test_mainnet_p2wpkh(self) -> None:
        """Decode a mainnet P2WPKH address."""
        addr = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"
        script = _decode_bech32_scriptpubkey(addr, network="mainnet")
        assert script[0:2] == bytes([0x00, 0x14])
        assert len(script) == 22

    def test_signet_p2wpkh(self) -> None:
        """Decode a signet (tb1) P2WPKH address."""
        addr = "tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx"
        script = _decode_bech32_scriptpubkey(addr, network="signet")
        assert script[0:2] == bytes([0x00, 0x14])
        assert len(script) == 22

    def test_mainnet_p2tr_taproot(self) -> None:
        """Decode a mainnet P2TR (bech32m) address."""
        # BIP350 test vector.
        addr = "bc1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vqzk5jj0"
        script = _decode_bech32_scriptpubkey(addr, network="mainnet")
        # P2TR: OP_1 PUSH32 <32-byte-x-only-pubkey>
        assert script[0:2] == bytes([0x51, 0x20])
        assert len(script) == 34

    def test_rejects_bad_checksum(self) -> None:
        """A single-character substitution in the checksum must be rejected."""
        # Flip the last character of a valid mainnet P2WPKH address.
        valid = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"
        # 'q' is in the bech32 charset; the surrounding chars remain valid
        # bech32 chars so the only difference is checksum failure.
        bad = valid[:-1] + "q"
        assert bad != valid
        with pytest.raises(ValueError):
            _decode_bech32_scriptpubkey(bad, network="mainnet")

    def test_rejects_bad_checksum_one_char_typo(self) -> None:
        """A single-char typo in the data part must fail the checksum."""
        valid = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"
        # Flip one data character (not in the HRP).
        bad = valid[:5] + "p" + valid[6:]
        assert bad != valid
        with pytest.raises(ValueError):
            _decode_bech32_scriptpubkey(bad, network="mainnet")

    def test_rejects_wrong_network_mainnet_on_regtest(self) -> None:
        """Mainnet address pasted into a regtest wallet must be rejected."""
        mainnet_addr = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"
        with pytest.raises(ValueError):
            _decode_bech32_scriptpubkey(mainnet_addr, network="regtest")

    def test_rejects_wrong_network_regtest_on_mainnet(self) -> None:
        """Regtest address pasted into a mainnet wallet must be rejected."""
        with pytest.raises(ValueError):
            _decode_bech32_scriptpubkey(REGTEST_P2WPKH_ADDR, network="mainnet")

    def test_accepts_matching_network(self) -> None:
        """Network match passes through to validated decoding."""
        script = _decode_bech32_scriptpubkey(REGTEST_P2WPKH_ADDR, network="regtest")
        assert script[0:2] == bytes([0x00, 0x14])

    def test_rejects_unknown_network(self) -> None:
        """An unsupported network name must error rather than silently pass."""
        with pytest.raises(ValueError, match="Unsupported network"):
            _decode_bech32_scriptpubkey(REGTEST_P2WPKH_ADDR, network="liquid")

    def test_rejects_p2tr_with_bech32_not_bech32m(self) -> None:
        """A v1 (taproot) address truncated or with wrong checksum must fail."""
        # Drop a single char from a valid P2TR address. Result will fail
        # either the bech32m checksum or the witness-program length check.
        valid_p2tr = "bc1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vqzk5jj0"
        with pytest.raises(ValueError):
            _decode_bech32_scriptpubkey(valid_p2tr[:-1], network="mainnet")


# ---------------------------------------------------------------------------
# select_spendable_utxos
# ---------------------------------------------------------------------------


class TestSelectSpendableUtxos:
    """Test UTXO filtering logic."""

    def test_excludes_frozen(self) -> None:
        utxos = [_make_utxo(frozen=False), _make_utxo(frozen=True, vout=1)]
        result = select_spendable_utxos(utxos)
        assert len(result) == 1
        assert result[0].vout == 0

    def test_includes_frozen_when_requested(self) -> None:
        utxos = [_make_utxo(frozen=True)]
        result = select_spendable_utxos(utxos, include_frozen=True)
        assert len(result) == 1

    def test_excludes_locked_fidelity_bonds(self) -> None:
        utxos = [
            _make_utxo(),
            _make_utxo(locktime=int(time.time()) + 100_000, vout=1),
        ]
        result = select_spendable_utxos(utxos)
        assert len(result) == 1
        assert result[0].vout == 0

    def test_excludes_expired_fidelity_bonds_by_default(self) -> None:
        utxos = [
            _make_utxo(),
            _make_utxo(locktime=int(time.time()) - 1000, vout=1),
        ]
        result = select_spendable_utxos(utxos)
        assert len(result) == 1
        assert result[0].vout == 0

    def test_includes_expired_fidelity_bonds_when_requested(self) -> None:
        cutoff = int(time.time())
        utxos = [_make_utxo(locktime=cutoff - 1)]
        result = select_spendable_utxos(utxos, include_fidelity_bonds=True, locktime_cutoff=cutoff)
        assert len(result) == 1

    def test_excludes_locked_fidelity_bonds_when_requested(self) -> None:
        utxos = [_make_utxo(locktime=int(time.time()) + 100_000)]
        result = select_spendable_utxos(utxos, include_fidelity_bonds=True)
        assert result == []

    def test_excludes_bond_at_chain_time_cutoff(self) -> None:
        cutoff = int(time.time()) - 1000
        utxos = [_make_utxo(locktime=cutoff)]
        result = select_spendable_utxos(utxos, include_fidelity_bonds=True, locktime_cutoff=cutoff)
        assert result == []

    def test_excludes_frozen_expired_fidelity_bond(self) -> None:
        """Frozen wins: even an expired bond stays excluded while frozen."""
        utxos = [_make_utxo(locktime=int(time.time()) - 1000, frozen=True)]
        assert select_spendable_utxos(utxos) == []

    def test_empty_input(self) -> None:
        assert select_spendable_utxos([]) == []

    def test_all_frozen_returns_empty(self) -> None:
        utxos = [_make_utxo(frozen=True), _make_utxo(frozen=True, vout=1)]
        assert select_spendable_utxos(utxos) == []


# ---------------------------------------------------------------------------
# estimate_fee
# ---------------------------------------------------------------------------


class TestEstimateFee:
    """Test fee estimation."""

    def test_basic_no_change(self) -> None:
        utxos = [_make_utxo()]
        fee, vsize = estimate_fee(utxos, REGTEST_P2WPKH_ADDR, 1.0, has_change=False)
        assert fee > 0
        assert vsize > 0
        assert fee == math.ceil(vsize * 1.0)

    def test_with_change(self) -> None:
        utxos = [_make_utxo()]
        fee_no_change, _ = estimate_fee(utxos, REGTEST_P2WPKH_ADDR, 1.0, has_change=False)
        fee_change, _ = estimate_fee(utxos, REGTEST_P2WPKH_ADDR, 1.0, has_change=True)
        # Change output adds vbytes
        assert fee_change > fee_no_change

    def test_higher_fee_rate(self) -> None:
        utxos = [_make_utxo()]
        fee_low, _ = estimate_fee(utxos, REGTEST_P2WPKH_ADDR, 1.0, has_change=False)
        fee_high, _ = estimate_fee(utxos, REGTEST_P2WPKH_ADDR, 10.0, has_change=False)
        assert fee_high > fee_low

    def test_more_inputs_higher_fee(self) -> None:
        utxos_1 = [_make_utxo()]
        utxos_3 = [_make_utxo(vout=i) for i in range(3)]
        fee_1, _ = estimate_fee(utxos_1, REGTEST_P2WPKH_ADDR, 1.0, has_change=False)
        fee_3, _ = estimate_fee(utxos_3, REGTEST_P2WPKH_ADDR, 1.0, has_change=False)
        assert fee_3 > fee_1

    def test_p2wsh_input_has_higher_fee_than_p2wpkh(self) -> None:
        p2wpkh = _make_utxo()
        p2wsh = _make_utxo(scriptpubkey="0020" + "cc" * 32)
        p2wpkh_fee, _ = estimate_fee([p2wpkh], REGTEST_P2WPKH_ADDR, 1.0, has_change=False)
        p2wsh_fee, _ = estimate_fee([p2wsh], REGTEST_P2WPKH_ADDR, 1.0, has_change=False)
        assert p2wsh_fee > p2wpkh_fee


# ---------------------------------------------------------------------------
# _build_unsigned_tx
# ---------------------------------------------------------------------------


class TestBuildUnsignedTx:
    """Test raw unsigned transaction construction."""

    def test_single_input_no_change(self) -> None:
        utxos = [_make_utxo(value=50_000)]
        dest_script = bytes([0x00, 0x14]) + b"\xaa" * 20
        tx, version, inputs_data, outputs_data, num_outputs = _build_unsigned_tx(
            utxos,
            dest_script,
            49_000,
            None,
            0,
        )
        assert version == (2).to_bytes(4, "little")
        assert num_outputs == 1
        # TX starts with version
        assert tx[:4] == version
        # Should contain the dest amount
        assert (49_000).to_bytes(8, "little") in outputs_data

    def test_single_input_with_change(self) -> None:
        utxos = [_make_utxo(value=100_000)]
        dest_script = bytes([0x00, 0x14]) + b"\xaa" * 20
        change_script = bytes([0x00, 0x14]) + b"\xbb" * 20
        tx, version, inputs_data, outputs_data, num_outputs = _build_unsigned_tx(
            utxos,
            dest_script,
            50_000,
            change_script,
            49_000,
        )
        assert num_outputs == 2
        assert (50_000).to_bytes(8, "little") in outputs_data
        assert (49_000).to_bytes(8, "little") in outputs_data

    def test_locktime_from_timelocked_utxo(self) -> None:
        past_time = int(time.time()) - 10_000
        utxos = [_make_utxo(value=100_000, locktime=past_time)]
        dest_script = bytes([0x00, 0x14]) + b"\xaa" * 20
        tx, _, _, _, _ = _build_unsigned_tx(utxos, dest_script, 99_000, None, 0)
        # Last 4 bytes are locktime
        locktime_bytes = tx[-4:]
        assert int.from_bytes(locktime_bytes, "little") == past_time

    def test_future_locktime_raises(self) -> None:
        future_time = int(time.time()) + 100_000
        utxos = [_make_utxo(value=100_000, locktime=future_time)]
        dest_script = bytes([0x00, 0x14]) + b"\xaa" * 20
        with pytest.raises(ValueError, match="has not passed chain time"):
            _build_unsigned_tx(utxos, dest_script, 99_000, None, 0)

    def test_locktime_equal_to_chain_time_raises(self) -> None:
        cutoff = int(time.time()) - 1000
        utxos = [_make_utxo(value=100_000, locktime=cutoff)]
        dest_script = bytes([0x00, 0x14]) + b"\xaa" * 20
        with pytest.raises(ValueError, match="has not passed chain time"):
            _build_unsigned_tx(
                utxos,
                dest_script,
                99_000,
                None,
                0,
                locktime_cutoff=cutoff,
            )

    def test_sequence_fffffffe_when_timelocked(self) -> None:
        past_time = int(time.time()) - 10_000
        utxos = [_make_utxo(value=100_000, locktime=past_time)]
        dest_script = bytes([0x00, 0x14]) + b"\xaa" * 20
        _, _, inputs_data, _, _ = _build_unsigned_tx(utxos, dest_script, 99_000, None, 0)
        # Input: 32-byte txid + 4-byte vout + 1-byte empty scriptsig + 4-byte sequence
        seq_bytes = inputs_data[37:41]
        assert int.from_bytes(seq_bytes, "little") == 0xFFFFFFFE

    def test_sequence_ffffffff_when_not_timelocked(self) -> None:
        utxos = [_make_utxo(value=100_000)]
        dest_script = bytes([0x00, 0x14]) + b"\xaa" * 20
        _, _, inputs_data, _, _ = _build_unsigned_tx(utxos, dest_script, 99_000, None, 0)
        seq_bytes = inputs_data[37:41]
        assert int.from_bytes(seq_bytes, "little") == 0xFFFFFFFF


# ---------------------------------------------------------------------------
# direct_send (integration with mocked wallet + backend)
# ---------------------------------------------------------------------------


def _make_mock_key(pubkey_hex: str = "02" + "ab" * 32) -> MagicMock:
    """Create a mock HDKey with a deterministic public key."""
    key = MagicMock()
    key.get_public_key_bytes.return_value = bytes.fromhex(pubkey_hex)
    # Private key needs to be a real coincurve key for signing
    # Use a deterministic 32-byte secret
    from coincurve import PrivateKey

    key.private_key = PrivateKey(b"\x01" * 32)
    return key


def _make_mock_wallet(utxos: list[UTXOInfo], change_addr: str = REGTEST_P2WPKH_ADDR) -> MagicMock:
    """Create a mock WalletService for direct_send tests."""
    wallet = MagicMock()
    wallet.network = "regtest"
    wallet.get_utxos = AsyncMock(return_value=utxos)
    # Raise ValueError so direct_send falls back to get_utxos for coin selection
    wallet.select_utxos = MagicMock(side_effect=ValueError("no coin selection in tests"))
    wallet.get_key_for_address = MagicMock(return_value=_make_mock_key())
    wallet.get_next_address_index = MagicMock(return_value=0)
    wallet.get_change_address = MagicMock(return_value=change_addr)
    return wallet


def _bond_scriptpubkey(locktime: int, pubkey_hex: str = "02" + "ab" * 32) -> str:
    witness_script = mk_freeze_script(pubkey_hex, locktime)
    return (b"\x00\x20" + sha256(witness_script).digest()).hex()


def _make_mock_backend(
    fee_rate: float = 1.0,
    txid: str = "cc" * 32,
    median_time_past: int | None = None,
) -> MagicMock:
    """Create a mock BlockchainBackend."""
    backend = MagicMock()
    backend.estimate_fee = AsyncMock(return_value=fee_rate)
    backend.broadcast_transaction = AsyncMock(return_value=txid)
    backend.get_median_time_past = AsyncMock(return_value=median_time_past or int(time.time()))
    return backend


class TestDirectSend:
    """Integration tests for the full direct_send flow."""

    @pytest.mark.anyio
    async def test_basic_send(self) -> None:
        utxos = [_make_utxo(value=200_000)]
        wallet = _make_mock_wallet(utxos)
        backend = _make_mock_backend()

        result = await direct_send(
            wallet=wallet,
            backend=backend,
            mixdepth=0,
            amount_sats=50_000,
            destination=REGTEST_P2WPKH_ADDR,
            fee_rate=1.0,
        )
        assert isinstance(result, DirectSendResult)
        assert result.send_amount == 50_000
        assert result.fee > 0
        assert result.num_inputs == 1
        assert result.tx_hex
        backend.broadcast_transaction.assert_called_once()

    @pytest.mark.anyio
    async def test_sweep(self) -> None:
        """amount_sats=0 should sweep the entire mixdepth."""
        utxos = [_make_utxo(value=100_000)]
        wallet = _make_mock_wallet(utxos)
        backend = _make_mock_backend()

        result = await direct_send(
            wallet=wallet,
            backend=backend,
            mixdepth=0,
            amount_sats=0,
            destination=REGTEST_P2WPKH_ADDR,
            fee_rate=1.0,
        )
        assert result.change_amount == 0
        assert result.send_amount == 100_000 - result.fee
        assert result.num_outputs == 1

    @pytest.mark.anyio
    async def test_sweep_includes_expired_fidelity_bond(self) -> None:
        """Regression: an expired bond must be spendable via sweep.

        This is the JAM "move bond to jar" flow: all other UTXOs are frozen
        and the mixdepth is swept, so the expired bond has to be included.
        The resulting transaction must carry the bond's locktime as
        nLockTime so OP_CLTV validates.
        """
        past_locktime = int(time.time()) - 100_000
        bond = _make_utxo(
            value=500_000,
            vout=1,
            scriptpubkey=_bond_scriptpubkey(past_locktime),
            locktime=past_locktime,
            path=f"m/84'/0'/0'/2/12:{past_locktime}",
        )
        utxos = [_make_utxo(value=100_000, frozen=True), bond]
        wallet = _make_mock_wallet(utxos)
        backend = _make_mock_backend()

        result = await direct_send(
            wallet=wallet,
            backend=backend,
            mixdepth=0,
            amount_sats=0,
            destination=REGTEST_P2WPKH_ADDR,
            fee_rate=1.0,
        )
        assert result.num_inputs == 1
        assert result.send_amount == 500_000 - result.fee
        # nLockTime (last 4 bytes) must equal the bond's script locktime.
        tx_bytes = bytes.fromhex(result.tx_hex)
        assert int.from_bytes(tx_bytes[-4:], "little") == past_locktime

    @pytest.mark.anyio
    async def test_sweep_does_not_merge_expired_bond_with_regular_coin(self) -> None:
        past_locktime = int(time.time()) - 100_000
        bond = _make_utxo(
            value=500_000,
            vout=1,
            scriptpubkey=_bond_scriptpubkey(past_locktime),
            locktime=past_locktime,
        )
        wallet = _make_mock_wallet([_make_utxo(value=100_000), bond])
        backend = _make_mock_backend()

        result = await direct_send(
            wallet=wallet,
            backend=backend,
            mixdepth=0,
            amount_sats=0,
            destination=REGTEST_P2WPKH_ADDR,
            fee_rate=1.0,
        )

        assert result.num_inputs == 1
        assert result.send_amount == 100_000 - result.fee
        assert int.from_bytes(bytes.fromhex(result.tx_hex)[-4:], "little") == 0
        backend.get_median_time_past.assert_not_awaited()

    @pytest.mark.anyio
    async def test_sweep_rejects_bond_at_chain_time_cutoff(self) -> None:
        locktime = int(time.time()) - 100_000
        bond = _make_utxo(
            value=500_000,
            scriptpubkey=_bond_scriptpubkey(locktime),
            locktime=locktime,
        )
        wallet = _make_mock_wallet([bond])
        backend = _make_mock_backend(median_time_past=locktime)

        with pytest.raises(ValueError, match="No spendable UTXOs"):
            await direct_send(
                wallet=wallet,
                backend=backend,
                mixdepth=0,
                amount_sats=0,
                destination=REGTEST_P2WPKH_ADDR,
                fee_rate=1.0,
            )

    @pytest.mark.anyio
    async def test_sweep_rejects_bond_with_mismatched_wallet_key(self) -> None:
        locktime = int(time.time()) - 100_000
        bond = _make_utxo(
            value=500_000,
            scriptpubkey="0020" + "cc" * 32,
            locktime=locktime,
        )
        wallet = _make_mock_wallet([bond])

        with pytest.raises(ValueError, match="No spendable UTXOs"):
            await direct_send(
                wallet=wallet,
                backend=_make_mock_backend(),
                mixdepth=0,
                amount_sats=0,
                destination=REGTEST_P2WPKH_ADDR,
                fee_rate=1.0,
            )

    @pytest.mark.anyio
    async def test_sweep_excludes_locked_fidelity_bond(self) -> None:
        """A bond whose timelock has not expired must never be swept."""
        future_locktime = int(time.time()) + 100_000
        bond = _make_utxo(
            value=500_000,
            vout=1,
            scriptpubkey="0020" + "cc" * 32,
            locktime=future_locktime,
        )
        utxos = [_make_utxo(value=100_000), bond]
        wallet = _make_mock_wallet(utxos)
        backend = _make_mock_backend()

        result = await direct_send(
            wallet=wallet,
            backend=backend,
            mixdepth=0,
            amount_sats=0,
            destination=REGTEST_P2WPKH_ADDR,
            fee_rate=1.0,
        )
        assert result.num_inputs == 1
        assert result.send_amount == 100_000 - result.fee
        tx_bytes = bytes.fromhex(result.tx_hex)
        assert int.from_bytes(tx_bytes[-4:], "little") == 0

    @pytest.mark.anyio
    async def test_change_below_dust_added_to_fee(self) -> None:
        """When change would be below dust threshold, it's folded into the fee."""
        # Choose values so that change = total - send - fee < DUST_THRESHOLD
        # With 1 input P2WPKH -> 1 output P2WPKH, fee ~ 110 at 1 sat/vB
        utxos = [_make_utxo(value=50_000 + 110 + DUST_THRESHOLD - 1)]
        wallet = _make_mock_wallet(utxos)
        backend = _make_mock_backend()

        result = await direct_send(
            wallet=wallet,
            backend=backend,
            mixdepth=0,
            amount_sats=50_000,
            destination=REGTEST_P2WPKH_ADDR,
            fee_rate=1.0,
        )
        assert result.change_amount == 0
        # Fee absorbs the dust and reports the transaction's actual value delta.
        assert result.fee == utxos[0].value - result.send_amount

    @pytest.mark.anyio
    async def test_insufficient_funds_raises(self) -> None:
        utxos = [_make_utxo(value=1_000)]
        wallet = _make_mock_wallet(utxos)
        backend = _make_mock_backend()

        with pytest.raises(ValueError, match="Insufficient funds"):
            await direct_send(
                wallet=wallet,
                backend=backend,
                mixdepth=0,
                amount_sats=500_000,
                destination=REGTEST_P2WPKH_ADDR,
                fee_rate=1.0,
            )

    @pytest.mark.anyio
    async def test_no_utxos_raises(self) -> None:
        wallet = _make_mock_wallet([])
        backend = _make_mock_backend()

        with pytest.raises(ValueError, match="No spendable UTXOs"):
            await direct_send(
                wallet=wallet,
                backend=backend,
                mixdepth=0,
                amount_sats=50_000,
                destination=REGTEST_P2WPKH_ADDR,
                fee_rate=1.0,
            )

    @pytest.mark.anyio
    async def test_non_bech32_address_raises(self) -> None:
        wallet = _make_mock_wallet([_make_utxo()])
        backend = _make_mock_backend()

        with pytest.raises(ValueError, match="bech32"):
            await direct_send(
                wallet=wallet,
                backend=backend,
                mixdepth=0,
                amount_sats=50_000,
                destination="1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa",
                fee_rate=1.0,
            )

    @pytest.mark.anyio
    async def test_bad_checksum_address_raises(self) -> None:
        """Address with a flipped checksum char must be rejected before broadcast."""
        wallet = _make_mock_wallet([_make_utxo(value=200_000)])
        backend = _make_mock_backend()

        # Flip the last char of a valid regtest address. Result is still
        # entirely in the bech32 charset, only the checksum changes.
        bad = REGTEST_P2WPKH_ADDR[:-1] + ("p" if REGTEST_P2WPKH_ADDR[-1] != "p" else "q")
        assert bad != REGTEST_P2WPKH_ADDR

        with pytest.raises(ValueError):
            await direct_send(
                wallet=wallet,
                backend=backend,
                mixdepth=0,
                amount_sats=50_000,
                destination=bad,
                fee_rate=1.0,
            )
        # And nothing got broadcast.
        backend.broadcast_transaction.assert_not_awaited()

    @pytest.mark.anyio
    async def test_wrong_network_address_raises(self) -> None:
        """Mainnet address on a regtest wallet must be rejected before broadcast."""
        wallet = _make_mock_wallet([_make_utxo(value=200_000)])
        backend = _make_mock_backend()

        mainnet_addr = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"

        with pytest.raises(ValueError):
            await direct_send(
                wallet=wallet,
                backend=backend,
                mixdepth=0,
                amount_sats=50_000,
                destination=mainnet_addr,
                fee_rate=1.0,
            )
        backend.broadcast_transaction.assert_not_awaited()

    @pytest.mark.anyio
    async def test_uses_backend_fee_estimate_when_no_rate(self) -> None:
        """When fee_rate is None, should query the backend."""
        utxos = [_make_utxo(value=200_000)]
        wallet = _make_mock_wallet(utxos)
        backend = _make_mock_backend(fee_rate=5.0)

        result = await direct_send(
            wallet=wallet,
            backend=backend,
            mixdepth=0,
            amount_sats=50_000,
            destination=REGTEST_P2WPKH_ADDR,
            fee_rate=None,
            fee_target_blocks=3,
        )
        backend.estimate_fee.assert_called_once_with(target_blocks=3)
        # Fee should be based on 5.0 sat/vB (higher than default 1.0)
        assert result.fee_rate == 5.0

    @pytest.mark.anyio
    async def test_result_has_correct_structure(self) -> None:
        utxos = [_make_utxo(value=200_000)]
        wallet = _make_mock_wallet(utxos)
        backend = _make_mock_backend(txid="dd" * 32)

        result = await direct_send(
            wallet=wallet,
            backend=backend,
            mixdepth=0,
            amount_sats=50_000,
            destination=REGTEST_P2WPKH_ADDR,
            fee_rate=1.0,
        )
        assert result.txid == "dd" * 32
        assert result.num_inputs == 1
        assert result.num_outputs == 2  # send + change
        assert len(result.inputs) == 1
        assert len(result.outputs) >= 1
        assert result.inputs[0]["outpoint"] == f"{'aa' * 32}:0"

    @pytest.mark.anyio
    async def test_sweep_insufficient_after_fee_raises(self) -> None:
        """Sweeping a tiny UTXO that can't cover fees should raise."""
        utxos = [_make_utxo(value=50)]  # way too small
        wallet = _make_mock_wallet(utxos)
        backend = _make_mock_backend()

        with pytest.raises(ValueError, match="Insufficient funds after fee"):
            await direct_send(
                wallet=wallet,
                backend=backend,
                mixdepth=0,
                amount_sats=0,  # sweep
                destination=REGTEST_P2WPKH_ADDR,
                fee_rate=1.0,
            )

    @pytest.mark.anyio
    async def test_multiple_inputs(self) -> None:
        """All UTXOs in the mixdepth are consumed."""
        utxos = [
            _make_utxo(value=50_000, vout=0, txid="aa" * 32),
            _make_utxo(value=60_000, vout=1, txid="bb" * 32),
        ]
        wallet = _make_mock_wallet(utxos)
        backend = _make_mock_backend()

        result = await direct_send(
            wallet=wallet,
            backend=backend,
            mixdepth=0,
            amount_sats=80_000,
            destination=REGTEST_P2WPKH_ADDR,
            fee_rate=1.0,
        )
        assert result.num_inputs == 2
        assert result.send_amount == 80_000


# ---------------------------------------------------------------------------
# enforce_fee_rate_cap + direct_send fee-rate cap
# ---------------------------------------------------------------------------


class TestEnforceFeeRateCap:
    """Direct unit tests for enforce_fee_rate_cap."""

    def test_below_cap_passes(self) -> None:
        enforce_fee_rate_cap(10.0, 1_000.0, source="manual")

    def test_at_cap_passes(self) -> None:
        # The cap is inclusive: exactly the cap is still acceptable.
        enforce_fee_rate_cap(1_000.0, 1_000.0, source="manual")

    def test_above_cap_raises(self) -> None:
        with pytest.raises(ExcessiveFeeRateError, match="exceeds safety cap"):
            enforce_fee_rate_cap(1_000.01, 1_000.0, source="manual")

    def test_zero_raises(self) -> None:
        with pytest.raises(ExcessiveFeeRateError, match="finite positive"):
            enforce_fee_rate_cap(0.0, 1_000.0, source="manual")

    def test_negative_raises(self) -> None:
        with pytest.raises(ExcessiveFeeRateError, match="finite positive"):
            enforce_fee_rate_cap(-1.0, 1_000.0, source="manual")

    def test_nan_raises(self) -> None:
        with pytest.raises(ExcessiveFeeRateError, match="finite positive"):
            enforce_fee_rate_cap(math.nan, 1_000.0, source="manual")

    def test_inf_raises(self) -> None:
        with pytest.raises(ExcessiveFeeRateError, match="finite positive"):
            enforce_fee_rate_cap(math.inf, 1_000.0, source="manual")

    def test_subclasses_value_error(self) -> None:
        # Required so existing ``except ValueError`` handlers in CLI / HTTP
        # code keep refusing the transaction without needing to know about
        # the new exception type.
        assert issubclass(ExcessiveFeeRateError, ValueError)

    def test_source_label_in_message(self) -> None:
        with pytest.raises(ExcessiveFeeRateError, match="backend estimate fee rate"):
            enforce_fee_rate_cap(2_000.0, 1_000.0, source="backend estimate")


class TestDirectSendFeeRateCap:
    """Integration tests asserting direct_send refuses excessive fee rates
    *before* signing or broadcasting."""

    @pytest.mark.anyio
    async def test_manual_fee_rate_above_cap_rejected_before_broadcast(self) -> None:
        utxos = [_make_utxo(value=1_000_000)]
        wallet = _make_mock_wallet(utxos)
        backend = _make_mock_backend()

        with pytest.raises(ExcessiveFeeRateError, match="exceeds safety cap"):
            await direct_send(
                wallet=wallet,
                backend=backend,
                mixdepth=0,
                amount_sats=50_000,
                destination=REGTEST_P2WPKH_ADDR,
                fee_rate=1_500.0,
                max_fee_rate_sat_vb=1_000.0,
            )
        # The transaction must NOT have been broadcast.
        backend.broadcast_transaction.assert_not_awaited()
        # And no UTXO selection / signing should have happened either.
        wallet.get_utxos.assert_not_called()

    @pytest.mark.anyio
    async def test_estimated_fee_rate_above_cap_rejected_before_broadcast(self) -> None:
        utxos = [_make_utxo(value=1_000_000)]
        wallet = _make_mock_wallet(utxos)
        # Backend reports a wildly inflated estimate (e.g. compromised / buggy).
        backend = _make_mock_backend(fee_rate=50_000.0)

        with pytest.raises(ExcessiveFeeRateError, match="backend estimate"):
            await direct_send(
                wallet=wallet,
                backend=backend,
                mixdepth=0,
                amount_sats=50_000,
                destination=REGTEST_P2WPKH_ADDR,
                fee_rate=None,  # force estimation path
                max_fee_rate_sat_vb=1_000.0,
            )
        backend.broadcast_transaction.assert_not_awaited()

    @pytest.mark.anyio
    async def test_estimated_fee_rate_below_cap_succeeds(self) -> None:
        utxos = [_make_utxo(value=1_000_000)]
        wallet = _make_mock_wallet(utxos)
        backend = _make_mock_backend(fee_rate=5.0)

        result = await direct_send(
            wallet=wallet,
            backend=backend,
            mixdepth=0,
            amount_sats=50_000,
            destination=REGTEST_P2WPKH_ADDR,
            fee_rate=None,
            max_fee_rate_sat_vb=1_000.0,
        )
        assert result.fee_rate == 5.0
        backend.broadcast_transaction.assert_called_once()

    @pytest.mark.anyio
    async def test_caller_can_lower_cap(self) -> None:
        """Callers can tighten the cap below the default."""
        utxos = [_make_utxo(value=1_000_000)]
        wallet = _make_mock_wallet(utxos)
        backend = _make_mock_backend()

        with pytest.raises(ExcessiveFeeRateError, match="exceeds safety cap"):
            await direct_send(
                wallet=wallet,
                backend=backend,
                mixdepth=0,
                amount_sats=50_000,
                destination=REGTEST_P2WPKH_ADDR,
                fee_rate=20.0,
                max_fee_rate_sat_vb=10.0,
            )
        backend.broadcast_transaction.assert_not_awaited()

    @pytest.mark.anyio
    async def test_randomized_rate_is_limited_by_cap(self) -> None:
        utxos = [_make_utxo(value=1_000_000)]
        wallet = _make_mock_wallet(utxos)
        backend = _make_mock_backend()

        with patch("jmwallet.wallet.spend.random.uniform", side_effect=lambda _low, high: high):
            result = await direct_send(
                wallet=wallet,
                backend=backend,
                mixdepth=0,
                amount_sats=50_000,
                destination=REGTEST_P2WPKH_ADDR,
                fee_rate=900.0,
                tx_fee_factor=1.0,
                max_fee_rate_sat_vb=1_000.0,
            )

        assert result.fee_rate == 1_000.0
