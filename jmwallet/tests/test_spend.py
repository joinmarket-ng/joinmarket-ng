"""Unit tests for jmwallet.wallet.spend — direct-send transaction building."""

from __future__ import annotations

import math
import time
from hashlib import sha256
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bitcointx.core.key import CKey
from jmcore.bitcoin import (
    TxOutput,
    get_txid,
    parse_transaction,
    scriptpubkey_to_address,
    serialize_transaction,
)
from jmcore.btc_script import mk_freeze_script

from jmwallet.backends.base import BlockchainBackend, MempoolSpenderLookupResult, Transaction
from jmwallet.wallet.address import pubkey_to_p2wpkh_script
from jmwallet.wallet.coin_selection import DirectSendSearchLimitError
from jmwallet.wallet.models import UTXOInfo
from jmwallet.wallet.spend import (
    DUST_THRESHOLD,
    DirectSendResult,
    DirectTxOutput,
    ExcessiveFeeRateError,
    SignedDirectTx,
    _decode_bech32_scriptpubkey,
    build_and_sign_direct_tx,
    direct_send,
    enforce_fee_rate_cap,
    estimate_fee,
    parse_outpoint,
    prepare_direct_send,
    resolve_direct_send_locktime,
    resolve_input_utxos,
    select_automatic_direct_send_inputs,
    select_spendable_utxos,
)
from jmwallet.wallet.utxo_metadata import AddressReservationError

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
REGTEST_P2TR_ADDR = "bcrt1p4w46h2at4w46h2at4w46h2at4w46h2at4w46h2at4w46h2at4w4spc6qv8"
REGTEST_P2TR_SCRIPT = "5120" + "ab" * 32

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
# Direct transaction construction policy
# ---------------------------------------------------------------------------


class TestDirectTransactionPolicy:
    def _wallet(self) -> MagicMock:
        wallet = MagicMock()
        wallet.get_locked_input_outpoints.return_value = set()
        wallet.sign_input.side_effect = lambda _tx, index, _utxo: MagicMock(
            witness=[f"sig-{index}".encode(), b"pubkey"]
        )
        return wallet

    def _output(self, value: int = 49_000, marker: int = 0xAA) -> DirectTxOutput:
        return DirectTxOutput(
            value_sats=value,
            script_pubkey=bytes([0x00, 0x14]) + bytes([marker]) * 20,
            address=f"output-{marker}",
        )

    def test_default_policy_is_version_two_locktime_and_rbf(self) -> None:
        built = build_and_sign_direct_tx(
            wallet=self._wallet(),
            utxos=[_make_utxo(value=50_000)],
            outputs=[self._output()],
            locktime=840_000,
        )

        parsed = parse_transaction(built.raw.hex())
        assert parsed.version == 2
        assert parsed.locktime == 840_000
        assert {tx_input.sequence for tx_input in parsed.inputs} == {0xFFFFFFFD}

    def test_rbf_opt_out_keeps_locktime_enabled(self) -> None:
        built = build_and_sign_direct_tx(
            wallet=self._wallet(),
            utxos=[_make_utxo(value=50_000)],
            outputs=[self._output()],
            locktime=840_000,
            rbf=False,
        )

        parsed = parse_transaction(built.raw.hex())
        assert parsed.locktime == 840_000
        assert {tx_input.sequence for tx_input in parsed.inputs} == {0xFFFFFFFE}

    def test_inputs_and_outputs_are_independently_shuffled_before_signing(self) -> None:
        first = _make_utxo(txid="aa" * 32, vout=0, value=30_000)
        second = _make_utxo(txid="bb" * 32, vout=1, value=30_000)
        wallet = self._wallet()

        with patch(
            "jmwallet.wallet.spend.secure_random.shuffle",
            side_effect=lambda values: values.reverse(),
        ) as shuffle:
            built = build_and_sign_direct_tx(
                wallet=wallet,
                utxos=[first, second],
                outputs=[self._output(20_000, 0xAA), self._output(39_000, 0xBB)],
                locktime=840_000,
            )

        parsed = parse_transaction(built.raw.hex())
        assert shuffle.call_count == 2
        assert [tx_input.txid for tx_input in parsed.inputs] == [second.txid, first.txid]
        assert [output.value for output in parsed.outputs] == [39_000, 20_000]
        assert [output.value_sats for output in built.outputs] == [39_000, 20_000]
        assert [call.args[2] for call in wallet.sign_input.call_args_list] == [second, first]

    def test_rejects_locktime_below_cltv_requirement_before_signing(self) -> None:
        wallet = self._wallet()
        utxo = _make_utxo(value=50_000, locktime=1_700_000_000)

        with pytest.raises(ValueError, match="does not satisfy input locktime"):
            build_and_sign_direct_tx(
                wallet=wallet,
                utxos=[utxo],
                outputs=[self._output()],
                locktime=840_000,
            )

        wallet.sign_input.assert_not_called()

    def test_rejects_input_leased_after_selection_before_signing(self) -> None:
        wallet = self._wallet()
        utxo = _make_utxo(value=50_000)
        wallet.get_locked_input_outpoints.return_value = {(utxo.txid, utxo.vout)}

        with pytest.raises(ValueError, match="locked by another in-flight CoinJoin"):
            build_and_sign_direct_tx(
                wallet=wallet,
                utxos=[utxo],
                outputs=[self._output()],
                locktime=840_000,
            )

        wallet.sign_input.assert_not_called()

    @pytest.mark.anyio
    async def test_resolves_height_locktime_for_regular_inputs(self) -> None:
        backend = _make_mock_backend(block_height=840_000)

        with patch("jmcore.transaction_policy.secure_random.randint", return_value=1):
            locktime = await resolve_direct_send_locktime(
                backend=backend,
                utxos=[_make_utxo()],
            )

        assert locktime == 840_000

    @pytest.mark.anyio
    async def test_rejects_unexpired_cltv_locktime(self) -> None:
        cutoff = int(time.time()) - 1000
        backend = _make_mock_backend(median_time_past=cutoff)

        with pytest.raises(ValueError, match="has not passed chain time"):
            await resolve_direct_send_locktime(
                backend=backend,
                utxos=[_make_utxo(locktime=cutoff)],
            )


# ---------------------------------------------------------------------------
# direct_send (integration with mocked wallet + backend)
# ---------------------------------------------------------------------------


def _make_mock_key(pubkey_hex: str = "02" + "ab" * 32) -> MagicMock:
    """Create a mock HDKey with a deterministic public key."""
    key = MagicMock()
    key.get_public_key_bytes.return_value = bytes.fromhex(pubkey_hex)
    key.private_key = CKey.from_secret_bytes(b"\x01" * 32)
    return key


def _make_mock_wallet(utxos: list[UTXOInfo], change_addr: str = REGTEST_P2WPKH_ADDR) -> MagicMock:
    """Create a mock WalletService for direct_send tests."""
    wallet = MagicMock()
    wallet.network = "regtest"
    wallet.get_utxos = AsyncMock(return_value=utxos)
    # Real WalletService always exposes a dict here; explicit-input resolution
    # reads it to tell "wrong mixdepth" apart from "not found".
    wallet.utxo_cache = {0: utxos}
    # Raise ValueError so direct_send falls back to get_utxos for coin selection
    wallet.select_utxos = MagicMock(side_effect=ValueError("no coin selection in tests"))
    wallet.get_key_for_address = MagicMock(return_value=_make_mock_key())
    wallet.get_locked_input_outpoints = MagicMock(return_value=set())
    wallet.get_new_internal_address = MagicMock(return_value=change_addr)
    wallet.sign_input.return_value = MagicMock(witness=[b"signature", b"pubkey"])
    return wallet


def _bond_scriptpubkey(locktime: int, pubkey_hex: str = "02" + "ab" * 32) -> str:
    witness_script = mk_freeze_script(pubkey_hex, locktime)
    return (b"\x00\x20" + sha256(witness_script).digest()).hex()


def _make_mock_backend(
    fee_rate: float = 1.0,
    txid: str = "cc" * 32,
    median_time_past: int | None = None,
    block_height: int = 840_000,
) -> MagicMock:
    """Create a mock BlockchainBackend."""
    backend = MagicMock()
    backend.estimate_fee = AsyncMock(return_value=fee_rate)
    backend.broadcast_transaction = AsyncMock(return_value=txid)
    backend.get_median_time_past = AsyncMock(return_value=median_time_past or int(time.time()))
    backend.get_block_height = AsyncMock(return_value=block_height)
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
    async def test_change_reservation_failure_prevents_transaction_build_and_broadcast(
        self,
    ) -> None:
        utxos = [_make_utxo(value=200_000)]
        wallet = _make_mock_wallet(utxos)
        wallet.get_new_internal_address.side_effect = AddressReservationError(
            "metadata unavailable"
        )
        backend = _make_mock_backend()

        with pytest.raises(AddressReservationError, match="metadata unavailable"):
            await direct_send(
                wallet=wallet,
                backend=backend,
                mixdepth=0,
                amount_sats=50_000,
                destination=REGTEST_P2WPKH_ADDR,
                fee_rate=1.0,
            )

        backend.broadcast_transaction.assert_not_called()

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
        assert 839_901 <= int.from_bytes(bytes.fromhex(result.tx_hex)[-4:], "little") <= 840_000
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
        assert 839_901 <= int.from_bytes(tx_bytes[-4:], "little") <= 840_000

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

        with pytest.raises(ValueError, match="Insufficient eligible funds"):
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

        with pytest.raises(ValueError, match="No eligible direct-send UTXOs"):
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
        assert result.txid == get_txid(result.tx_hex)
        assert result.num_inputs == 1
        assert result.num_outputs == 2  # send + change
        assert len(result.inputs) == 1
        assert len(result.outputs) == 2
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

        with patch("jmcore.randomness.secure_random.uniform", side_effect=lambda _low, high: high):
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


class TestPrepareDirectSend:
    """Unit tests for prepare_direct_send."""

    @pytest.mark.anyio
    async def test_prepare_returns_signed_tx_without_broadcasting(self) -> None:
        """prepare_direct_send builds and signs but does not broadcast."""

        utxos = [_make_utxo(value=200_000, address="bcrt1qinput")]
        wallet = _make_mock_wallet(utxos, change_addr=REGTEST_P2WPKH_ADDR)
        backend = _make_mock_backend()

        result = await prepare_direct_send(
            wallet=wallet,
            backend=backend,
            mixdepth=0,
            amount_sats=50_000,
            destination=REGTEST_P2WPKH_ADDR,
            fee_rate=1.0,
        )

        assert isinstance(result, SignedDirectTx)
        assert result.tx_hex
        assert result.txid == get_txid(result.tx_hex)
        assert result.change_address == REGTEST_P2WPKH_ADDR
        assert result.selected_utxos == [(utxos[0].txid, utxos[0].vout)]
        assert result.source_addresses == ["bcrt1qinput"]
        assert len(result.outputs) == 2
        assert result.version == 2
        assert 839_901 <= result.locktime <= 840_000
        parsed = parse_transaction(result.tx_hex)
        assert parsed.locktime == result.locktime
        assert {tx_input.sequence for tx_input in parsed.inputs} == {0xFFFFFFFD}
        backend.broadcast_transaction.assert_not_awaited()

    @pytest.mark.anyio
    async def test_prepare_can_disable_rbf_without_disabling_locktime(self) -> None:
        wallet = _make_mock_wallet([_make_utxo(value=200_000)])
        backend = _make_mock_backend(block_height=840_000)

        result = await prepare_direct_send(
            wallet=wallet,
            backend=backend,
            mixdepth=0,
            amount_sats=50_000,
            destination=REGTEST_P2WPKH_ADDR,
            fee_rate=1.0,
            rbf=False,
        )

        parsed = parse_transaction(result.tx_hex)
        assert parsed.locktime == result.locktime
        assert {tx_input.sequence for tx_input in parsed.inputs} == {0xFFFFFFFE}

    @pytest.mark.anyio
    async def test_change_output_script_matches_reported_change_address(self) -> None:
        """The serialized change output must pay the address the wallet reports.

        A Taproot change address previously received a hardcoded P2WPKH
        script, so the funds landed on a script the wallet's tr() descriptors
        could not see.
        """
        utxos = [_make_utxo(value=200_000, address="bcrt1qinput")]
        wallet = _make_mock_wallet(utxos, change_addr=REGTEST_P2TR_ADDR)
        backend = _make_mock_backend()

        result = await prepare_direct_send(
            wallet=wallet,
            backend=backend,
            mixdepth=0,
            amount_sats=50_000,
            destination=REGTEST_P2WPKH_ADDR,
            fee_rate=1.0,
        )

        assert result.change_address == REGTEST_P2TR_ADDR
        parsed = parse_transaction(result.tx_hex)
        change_outputs = [
            output for output in parsed.outputs if output.value == result.change_amount
        ]
        assert len(change_outputs) == 1
        assert change_outputs[0].script.hex() == REGTEST_P2TR_SCRIPT
        # The recorded output metadata must agree with the serialized script.
        recorded = [output for output in result.outputs if output["address"] == REGTEST_P2TR_ADDR]
        assert len(recorded) == 1
        assert recorded[0]["scriptPubKey"] == REGTEST_P2TR_SCRIPT

    @pytest.mark.anyio
    async def test_p2wpkh_change_output_script_is_unchanged(self) -> None:
        """P2WPKH change keeps paying the witness-v0 script for its address."""
        utxos = [_make_utxo(value=200_000, address="bcrt1qinput")]
        wallet = _make_mock_wallet(utxos, change_addr=REGTEST_P2WPKH_ADDR)
        backend = _make_mock_backend()

        result = await prepare_direct_send(
            wallet=wallet,
            backend=backend,
            mixdepth=0,
            amount_sats=50_000,
            destination=REGTEST_P2TR_ADDR,
            fee_rate=1.0,
        )

        assert result.change_address == REGTEST_P2WPKH_ADDR
        expected_script = _decode_bech32_scriptpubkey(REGTEST_P2WPKH_ADDR, network="regtest")
        parsed = parse_transaction(result.tx_hex)
        change_outputs = [
            output for output in parsed.outputs if output.value == result.change_amount
        ]
        assert len(change_outputs) == 1
        assert change_outputs[0].script == expected_script

    @pytest.mark.anyio
    async def test_prepare_sweep_has_empty_change_address(self) -> None:
        """Sweep (amount_sats=0) produces no change output; change_address must be empty."""
        utxos = [_make_utxo(value=100_000, address="bcrt1qinput")]
        wallet = _make_mock_wallet(utxos)
        backend = _make_mock_backend()

        result = await prepare_direct_send(
            wallet=wallet,
            backend=backend,
            mixdepth=0,
            amount_sats=0,
            destination=REGTEST_P2WPKH_ADDR,
            fee_rate=1.0,
        )

        assert result.change_address == ""
        assert result.source_addresses == ["bcrt1qinput"]
        backend.broadcast_transaction.assert_not_awaited()

    @pytest.mark.anyio
    async def test_prepare_sweep_excludes_leased_script_cluster(self) -> None:
        leased = _make_utxo(txid="aa" * 32, vout=0, value=100_000)
        sibling = _make_utxo(txid="bb" * 32, vout=1, value=100_000)
        alternative = _make_utxo(
            txid="cc" * 32,
            vout=2,
            value=100_000,
            scriptpubkey="0014" + "cc" * 20,
        )
        wallet = _make_mock_wallet([leased, sibling, alternative])
        wallet.get_locked_input_outpoints.return_value = {(leased.txid, leased.vout)}

        result = await prepare_direct_send(
            wallet=wallet,
            backend=_make_mock_backend(),
            mixdepth=0,
            amount_sats=0,
            destination=REGTEST_P2WPKH_ADDR,
            fee_rate=1.0,
        )

        assert result.selected_utxos == [(alternative.txid, alternative.vout)]


# ---------------------------------------------------------------------------
# parse_outpoint
# ---------------------------------------------------------------------------


class TestParseOutpoint:
    """Unit tests for txid:vout parsing (issue #587)."""

    def test_valid(self) -> None:
        assert parse_outpoint(f"{'aa' * 32}:3") == ("aa" * 32, 3)

    def test_uppercase_txid_is_normalized(self) -> None:
        assert parse_outpoint(f"{'AB' * 32}:0") == ("ab" * 32, 0)

    def test_surrounding_whitespace_is_tolerated(self) -> None:
        assert parse_outpoint(f"  {'aa' * 32}:1  ") == ("aa" * 32, 1)

    @pytest.mark.parametrize(
        "raw",
        [
            "aa" * 32,  # no separator
            f"{'aa' * 31}:0",  # txid too short
            f"{'aa' * 33}:0",  # txid too long
            f"{'zz' * 32}:0",  # not hex
            f"{'aa' * 32}:",  # missing vout
            f"{'aa' * 32}:-1",  # negative vout
            f"{'aa' * 32}:x",  # non-numeric vout
            f"{'aa' * 32}:1.0",  # non-integer vout
            "",
        ],
    )
    def test_malformed_raises(self, raw: str) -> None:
        with pytest.raises(ValueError, match="Invalid input UTXO"):
            parse_outpoint(raw)


# ---------------------------------------------------------------------------
# prepare_direct_send with explicit input_utxos (issue #587)
# ---------------------------------------------------------------------------


class TestPrepareDirectSendExplicitInputs:
    """Explicit coin control: spend exactly the listed UTXOs, or fail loudly."""

    @pytest.mark.anyio
    async def test_spends_only_the_listed_utxos(self) -> None:
        chosen = _make_utxo(txid="aa" * 32, vout=0, value=200_000)
        other = _make_utxo(txid="bb" * 32, vout=1, value=500_000)
        wallet = _make_mock_wallet([chosen, other])
        backend = _make_mock_backend()

        result = await prepare_direct_send(
            wallet=wallet,
            backend=backend,
            mixdepth=0,
            amount_sats=50_000,
            destination=REGTEST_P2WPKH_ADDR,
            fee_rate=1.0,
            input_utxos=[f"{'aa' * 32}:0"],
        )

        assert result.selected_utxos == [("aa" * 32, 0)]
        assert result.num_inputs == 1
        # Automatic coin selection must not run at all.
        wallet.select_utxos.assert_not_called()

    @pytest.mark.anyio
    async def test_input_order_is_randomized(self) -> None:
        first = _make_utxo(txid="aa" * 32, vout=0, value=200_000)
        second = _make_utxo(txid="bb" * 32, vout=1, value=200_000)
        wallet = _make_mock_wallet([first, second])
        backend = _make_mock_backend()

        with patch(
            "jmwallet.wallet.spend.secure_random.shuffle",
            side_effect=lambda values: values.reverse(),
        ):
            result = await prepare_direct_send(
                wallet=wallet,
                backend=backend,
                mixdepth=0,
                amount_sats=50_000,
                destination=REGTEST_P2WPKH_ADDR,
                fee_rate=1.0,
                input_utxos=[f"{'bb' * 32}:1", f"{'aa' * 32}:0"],
            )

        assert result.selected_utxos == [("aa" * 32, 0), ("bb" * 32, 1)]

    @pytest.mark.anyio
    async def test_sweep_spends_exactly_the_listed_utxos(self) -> None:
        """amount_sats=0 + explicit inputs sweeps those inputs, not the mixdepth."""
        chosen = _make_utxo(txid="aa" * 32, vout=0, value=200_000)
        untouched = _make_utxo(txid="bb" * 32, vout=1, value=500_000)
        wallet = _make_mock_wallet([chosen, untouched])
        backend = _make_mock_backend()

        result = await prepare_direct_send(
            wallet=wallet,
            backend=backend,
            mixdepth=0,
            amount_sats=0,
            destination=REGTEST_P2WPKH_ADDR,
            fee_rate=1.0,
            input_utxos=[f"{'aa' * 32}:0"],
        )

        assert result.selected_utxos == [("aa" * 32, 0)]
        assert result.change_amount == 0
        assert result.send_amount == 200_000 - result.fee


class TestResolveConflictedInputs:
    """Conflict reconstruction admits only a proven, signable wallet prevout."""

    @pytest.mark.anyio
    @pytest.mark.parametrize("wrong_key", [False, True])
    async def test_taproot_conflict_requires_exact_wallet_key(
        self, test_mnemonic: str, wrong_key: bool
    ) -> None:
        from jmcore.bitcoin import TxInput, address_to_scriptpubkey

        from jmwallet.wallet.service import WalletService

        backend = AsyncMock(spec=BlockchainBackend)
        wallet = WalletService(test_mnemonic, backend, network="regtest", address_type="p2tr")
        wallet.get_utxos = AsyncMock(return_value=[])
        address = wallet.get_address(0, 0, 7)
        script = address_to_scriptpubkey(address)
        raw = serialize_transaction(
            2, [TxInput(bytes(32), 0, b"", 0xFFFFFFFF)], [TxOutput(123_456, script)], 0
        )
        txid = get_txid(raw.hex())
        backend.get_mempool_spender.return_value = MempoolSpenderLookupResult(
            spending_txid="cc" * 32
        )
        backend.get_wallet_transaction.return_value = Transaction(
            txid=txid, raw=raw.hex(), confirmations=3
        )
        if wrong_key:
            wallet.address_cache[address] = (0, 0, 8)
            with pytest.raises(ValueError, match="does not match this wallet's signing key"):
                await resolve_input_utxos(
                    wallet=wallet,
                    backend=backend,
                    mixdepth=0,
                    input_utxos=[f"{txid}:0"],
                    allow_conflicts=True,
                )
        else:
            utxos, _ = await resolve_input_utxos(
                wallet=wallet,
                backend=backend,
                mixdepth=0,
                input_utxos=[f"{txid}:0"],
                allow_conflicts=True,
            )
            assert len(utxos) == 1
            assert utxos[0].path == "m/86'/1'/0'/0/7"
            assert utxos[0].scriptpubkey == script.hex()

    def _wallet_and_parent(self) -> tuple[MagicMock, MagicMock, str, str]:
        key = _make_mock_key()
        script = pubkey_to_p2wpkh_script(key.get_public_key_bytes(compressed=True).hex())
        address = scriptpubkey_to_address(script, "regtest")
        wallet = MagicMock()
        wallet.network = "regtest"
        wallet.root_path = "m/84'/1'"
        wallet.address_cache = {address: (0, 0, 7)}
        wallet.get_utxos = AsyncMock(return_value=[])
        wallet.get_locked_input_outpoints.return_value = set()
        wallet.is_utxo_frozen.return_value = False
        wallet.get_key_for_address.return_value = key
        backend = MagicMock()
        backend.get_mempool_spender = AsyncMock(
            return_value=MempoolSpenderLookupResult(spending_txid="cc" * 32)
        )
        backend.get_wallet_transaction = AsyncMock(
            return_value=Transaction(txid="aa" * 32, raw="00", confirmations=3)
        )
        return wallet, backend, script.hex(), address

    @pytest.mark.anyio
    async def test_reconstructs_confirmed_wallet_p2wpkh_with_live_spender(self) -> None:
        wallet, backend, script, address = self._wallet_and_parent()
        parsed = MagicMock(outputs=[MagicMock(value=123_456, script=bytes.fromhex(script))])

        with (
            patch("jmwallet.wallet.spend.get_txid", return_value="aa" * 32),
            patch("jmwallet.wallet.spend.deserialize_transaction", return_value=parsed),
        ):
            utxos, _ = await resolve_input_utxos(
                wallet=wallet,
                backend=backend,
                mixdepth=0,
                input_utxos=[f"{'aa' * 32}:0"],
                allow_conflicts=True,
            )

        assert len(utxos) == 1
        assert utxos[0].value == 123_456
        assert utxos[0].address == address
        assert utxos[0].path == "m/84'/1'/0'/0/7"
        assert utxos[0].scriptpubkey == script
        backend.get_mempool_spender.assert_awaited_once_with("aa" * 32, 0)

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        "spender, message",
        [
            (MempoolSpenderLookupResult(), "has no current mempool spender"),
            (
                MempoolSpenderLookupResult(spending_txid="cc" * 32, blockhash="dd" * 32),
                "was spent in confirmed block",
            ),
        ],
    )
    async def test_rejects_non_live_conflict(
        self, spender: MempoolSpenderLookupResult, message: str
    ) -> None:
        wallet, backend, _script, _address = self._wallet_and_parent()
        backend.get_mempool_spender.return_value = spender

        with pytest.raises(ValueError, match=message):
            await resolve_input_utxos(
                wallet=wallet,
                backend=backend,
                mixdepth=0,
                input_utxos=[f"{'aa' * 32}:0"],
                allow_conflicts=True,
            )
        backend.get_wallet_transaction.assert_not_awaited()

    @pytest.mark.anyio
    async def test_rejects_unsupported_conflict_lookup(self) -> None:
        wallet, backend, _script, _address = self._wallet_and_parent()
        backend.get_mempool_spender.side_effect = NotImplementedError()

        with pytest.raises(ValueError, match="does not support authoritative"):
            await resolve_input_utxos(
                wallet=wallet,
                backend=backend,
                mixdepth=0,
                input_utxos=[f"{'aa' * 32}:0"],
                allow_conflicts=True,
            )

    @pytest.mark.anyio
    async def test_rejects_unconfirmed_parent_and_wrong_script_key(self) -> None:
        wallet, backend, script, _address = self._wallet_and_parent()
        backend.get_wallet_transaction.return_value = Transaction(
            txid="aa" * 32, raw="00", confirmations=0
        )

        with pytest.raises(ValueError, match="parent transaction is not confirmed"):
            await resolve_input_utxos(
                wallet=wallet,
                backend=backend,
                mixdepth=0,
                input_utxos=[f"{'aa' * 32}:0"],
                allow_conflicts=True,
            )

        backend.get_wallet_transaction.return_value = Transaction(
            txid="aa" * 32, raw="00", confirmations=1
        )
        parsed = MagicMock(outputs=[MagicMock(value=1, script=bytes.fromhex(script))])
        wallet.get_key_for_address.return_value = _make_mock_key("02" + "cd" * 32)
        with (
            patch("jmwallet.wallet.spend.get_txid", return_value="aa" * 32),
            patch("jmwallet.wallet.spend.deserialize_transaction", return_value=parsed),
        ):
            with pytest.raises(ValueError, match="does not match this wallet's signing key"):
                await resolve_input_utxos(
                    wallet=wallet,
                    backend=backend,
                    mixdepth=0,
                    input_utxos=[f"{'aa' * 32}:0"],
                    allow_conflicts=True,
                )

    @pytest.mark.anyio
    async def test_rejects_parent_bytes_for_a_different_txid(self) -> None:
        wallet, backend, script, _address = self._wallet_and_parent()
        other_parent = serialize_transaction(
            version=2,
            inputs=[],
            outputs=[TxOutput(value=123_456, script=bytes.fromhex(script))],
            locktime=0,
        ).hex()
        backend.get_wallet_transaction.return_value = Transaction(
            txid="aa" * 32,
            raw=other_parent,
            confirmations=1,
        )

        with pytest.raises(ValueError, match="parent transaction is invalid"):
            await resolve_input_utxos(
                wallet=wallet,
                backend=backend,
                mixdepth=0,
                input_utxos=[f"{'aa' * 32}:0"],
                allow_conflicts=True,
            )

    @pytest.mark.anyio
    async def test_requires_at_least_one_reconstructed_conflict(self) -> None:
        regular = _make_utxo(value=200_000)
        wallet = _make_mock_wallet([regular])
        backend = _make_mock_backend()

        with pytest.raises(ValueError, match="requires at least one named input"):
            await resolve_input_utxos(
                wallet=wallet,
                backend=backend,
                mixdepth=0,
                input_utxos=[regular.outpoint],
                allow_conflicts=True,
            )

    @pytest.mark.anyio
    async def test_rejects_leased_input_before_conflict_reconstruction(self) -> None:
        txid = "aa" * 32
        wallet, backend, _script, _address = self._wallet_and_parent()
        wallet.get_locked_input_outpoints.return_value = {(txid, 0)}

        with pytest.raises(
            ValueError,
            match=f"Input UTXO {txid}:0 is locked by another in-flight CoinJoin",
        ):
            await resolve_input_utxos(
                wallet=wallet,
                backend=backend,
                mixdepth=0,
                input_utxos=[f"{txid}:0"],
                allow_conflicts=True,
            )

        backend.get_mempool_spender.assert_not_awaited()

    @pytest.mark.anyio
    async def test_empty_list_is_an_error(self) -> None:
        wallet = _make_mock_wallet([_make_utxo(value=200_000)])
        backend = _make_mock_backend()

        with pytest.raises(ValueError, match="must not be empty"):
            await prepare_direct_send(
                wallet=wallet,
                backend=backend,
                mixdepth=0,
                amount_sats=50_000,
                destination=REGTEST_P2WPKH_ADDR,
                fee_rate=1.0,
                input_utxos=[],
            )

    @pytest.mark.anyio
    async def test_frozen_utxo_is_rejected(self) -> None:
        frozen = _make_utxo(txid="aa" * 32, vout=0, value=200_000, frozen=True)
        wallet = _make_mock_wallet([frozen])
        backend = _make_mock_backend()

        with pytest.raises(ValueError, match="is frozen"):
            await prepare_direct_send(
                wallet=wallet,
                backend=backend,
                mixdepth=0,
                amount_sats=50_000,
                destination=REGTEST_P2WPKH_ADDR,
                fee_rate=1.0,
                input_utxos=[f"{'aa' * 32}:0"],
            )

    @pytest.mark.anyio
    async def test_unknown_utxo_is_rejected(self) -> None:
        wallet = _make_mock_wallet([_make_utxo(txid="aa" * 32, vout=0, value=200_000)])
        backend = _make_mock_backend()

        with pytest.raises(ValueError, match="not found in mixdepth 0"):
            await prepare_direct_send(
                wallet=wallet,
                backend=backend,
                mixdepth=0,
                amount_sats=50_000,
                destination=REGTEST_P2WPKH_ADDR,
                fee_rate=1.0,
                input_utxos=[f"{'cc' * 32}:0"],
            )

    @pytest.mark.anyio
    async def test_utxo_from_another_mixdepth_names_that_mixdepth(self) -> None:
        in_md0 = _make_utxo(txid="aa" * 32, vout=0, value=200_000)
        in_md2 = _make_utxo(txid="bb" * 32, vout=0, value=200_000, mixdepth=2)
        wallet = _make_mock_wallet([in_md0])
        wallet.utxo_cache = {0: [in_md0], 2: [in_md2]}
        backend = _make_mock_backend()

        with pytest.raises(ValueError, match="is in mixdepth 2, not the requested mixdepth 0"):
            await prepare_direct_send(
                wallet=wallet,
                backend=backend,
                mixdepth=0,
                amount_sats=50_000,
                destination=REGTEST_P2WPKH_ADDR,
                fee_rate=1.0,
                input_utxos=[f"{'bb' * 32}:0"],
            )

    @pytest.mark.anyio
    async def test_duplicate_outpoint_is_rejected(self) -> None:
        wallet = _make_mock_wallet([_make_utxo(txid="aa" * 32, vout=0, value=200_000)])
        backend = _make_mock_backend()

        with pytest.raises(ValueError, match="Duplicate input UTXO"):
            await prepare_direct_send(
                wallet=wallet,
                backend=backend,
                mixdepth=0,
                amount_sats=50_000,
                destination=REGTEST_P2WPKH_ADDR,
                fee_rate=1.0,
                input_utxos=[f"{'aa' * 32}:0", f"{'AA' * 32}:0"],
            )

    @pytest.mark.anyio
    async def test_insufficient_value_is_rejected_without_fallback(self) -> None:
        """A too-small explicit input must error, not silently pull in the big one."""
        small = _make_utxo(txid="aa" * 32, vout=0, value=10_000)
        big = _make_utxo(txid="bb" * 32, vout=1, value=5_000_000)
        wallet = _make_mock_wallet([small, big])
        backend = _make_mock_backend()

        with pytest.raises(ValueError, match="Insufficient funds"):
            await prepare_direct_send(
                wallet=wallet,
                backend=backend,
                mixdepth=0,
                amount_sats=1_000_000,
                destination=REGTEST_P2WPKH_ADDR,
                fee_rate=1.0,
                input_utxos=[f"{'aa' * 32}:0"],
            )

    @pytest.mark.anyio
    async def test_expired_fidelity_bond_can_be_selected_explicitly(self) -> None:
        past_locktime = int(time.time()) - 100_000
        bond = _make_utxo(
            txid="aa" * 32,
            vout=0,
            value=500_000,
            scriptpubkey=_bond_scriptpubkey(past_locktime),
            locktime=past_locktime,
        )
        wallet = _make_mock_wallet([bond, _make_utxo(txid="bb" * 32, vout=1, value=100_000)])
        backend = _make_mock_backend()

        result = await prepare_direct_send(
            wallet=wallet,
            backend=backend,
            mixdepth=0,
            amount_sats=0,
            destination=REGTEST_P2WPKH_ADDR,
            fee_rate=1.0,
            input_utxos=[f"{'aa' * 32}:0"],
        )

        assert result.selected_utxos == [("aa" * 32, 0)]
        assert int.from_bytes(bytes.fromhex(result.tx_hex)[-4:], "little") == past_locktime

    @pytest.mark.anyio
    async def test_unexpired_fidelity_bond_is_rejected(self) -> None:
        future_locktime = int(time.time()) + 100_000
        bond = _make_utxo(
            txid="aa" * 32,
            vout=0,
            value=500_000,
            scriptpubkey=_bond_scriptpubkey(future_locktime),
            locktime=future_locktime,
        )
        wallet = _make_mock_wallet([bond])
        backend = _make_mock_backend()

        with pytest.raises(ValueError, match="timelock .* has not passed chain time"):
            await prepare_direct_send(
                wallet=wallet,
                backend=backend,
                mixdepth=0,
                amount_sats=0,
                destination=REGTEST_P2WPKH_ADDR,
                fee_rate=1.0,
                input_utxos=[f"{'aa' * 32}:0"],
            )

    @pytest.mark.anyio
    async def test_unsignable_fidelity_bond_is_rejected(self) -> None:
        """An expired bond whose script this wallet does not derive is refused."""
        past_locktime = int(time.time()) - 100_000
        bond = _make_utxo(
            txid="aa" * 32,
            vout=0,
            value=500_000,
            scriptpubkey="0020" + "cd" * 32,  # does not match mk_freeze_script
            locktime=past_locktime,
        )
        wallet = _make_mock_wallet([bond])
        backend = _make_mock_backend()

        with pytest.raises(ValueError, match="cannot sign"):
            await prepare_direct_send(
                wallet=wallet,
                backend=backend,
                mixdepth=0,
                amount_sats=0,
                destination=REGTEST_P2WPKH_ADDR,
                fee_rate=1.0,
                input_utxos=[f"{'aa' * 32}:0"],
            )

    @pytest.mark.anyio
    async def test_no_median_time_past_lookup_without_bonds(self) -> None:
        wallet = _make_mock_wallet([_make_utxo(txid="aa" * 32, vout=0, value=200_000)])
        backend = _make_mock_backend()

        await prepare_direct_send(
            wallet=wallet,
            backend=backend,
            mixdepth=0,
            amount_sats=50_000,
            destination=REGTEST_P2WPKH_ADDR,
            fee_rate=1.0,
            input_utxos=[f"{'aa' * 32}:0"],
        )

        backend.get_median_time_past.assert_not_awaited()

    @pytest.mark.anyio
    async def test_omitting_input_utxos_keeps_automatic_selection(self) -> None:
        """Omitting explicit inputs uses the privacy-aware selector."""
        utxos = [_make_utxo(txid="aa" * 32, vout=0, value=200_000)]
        wallet = _make_mock_wallet(utxos)
        backend = _make_mock_backend()

        result = await prepare_direct_send(
            wallet=wallet,
            backend=backend,
            mixdepth=0,
            amount_sats=50_000,
            destination=REGTEST_P2WPKH_ADDR,
            fee_rate=1.0,
        )

        assert result.selected_utxos == [("aa" * 32, 0)]
        wallet.get_utxos.assert_awaited_once_with(0)
        wallet.select_utxos.assert_not_called()


class TestAutomaticDirectSendSource:
    @pytest.mark.anyio
    async def test_excludes_leased_input_in_favor_of_unleased_alternative(self) -> None:
        leased = _make_utxo(txid="cc" * 32, value=100_000, scriptpubkey="0014" + "cc" * 20)
        alternative = _make_utxo(
            txid="bb" * 32,
            value=100_000,
            scriptpubkey="0014" + "bb" * 20,
        )
        wallet = MagicMock()
        wallet.mixdepth_count = 1
        wallet.get_utxos = AsyncMock(return_value=[leased, alternative])
        wallet.get_locked_input_outpoints.return_value = {(leased.txid, leased.vout)}

        selection, mixdepth = await select_automatic_direct_send_inputs(
            wallet=wallet,
            amount_sats=50_000,
            destination=REGTEST_P2WPKH_ADDR,
            fee_rate=1.0,
            mixdepth=0,
        )

        assert mixdepth == 0
        assert selection.utxos == [alternative]

    @pytest.mark.anyio
    async def test_uses_highest_sufficient_mixdepth(self) -> None:
        high = _make_utxo(txid="cc" * 32, value=100_000, mixdepth=2)
        lower = _make_utxo(txid="bb" * 32, value=500_000, mixdepth=1)
        wallet = MagicMock()
        wallet.mixdepth_count = 3
        wallet.get_utxos = AsyncMock(side_effect=lambda md: {2: [high], 1: [lower]}[md])
        wallet.get_locked_input_outpoints.return_value = set()

        selection, mixdepth = await select_automatic_direct_send_inputs(
            wallet=wallet,
            amount_sats=50_000,
            destination=REGTEST_P2WPKH_ADDR,
            fee_rate=1.0,
            mixdepth=None,
        )

        assert mixdepth == 2
        assert selection.utxos == [high]
        assert [call.args[0] for call in wallet.get_utxos.await_args_list] == [2]

    @pytest.mark.anyio
    async def test_skips_insufficient_higher_mixdepth(self) -> None:
        high = _make_utxo(txid="cc" * 32, value=1_000, mixdepth=2)
        middle = _make_utxo(txid="bb" * 32, value=100_000, mixdepth=1)
        wallet = MagicMock()
        wallet.mixdepth_count = 3
        wallet.get_utxos = AsyncMock(side_effect=lambda md: {2: [high], 1: [middle]}[md])
        wallet.get_locked_input_outpoints.return_value = set()

        selection, mixdepth = await select_automatic_direct_send_inputs(
            wallet=wallet,
            amount_sats=50_000,
            destination=REGTEST_P2WPKH_ADDR,
            fee_rate=1.0,
            mixdepth=None,
        )

        assert mixdepth == 1
        assert selection.utxos == [middle]
        assert [call.args[0] for call in wallet.get_utxos.await_args_list] == [2, 1]

    @pytest.mark.anyio
    async def test_explicit_mixdepth_does_not_fall_back(self) -> None:
        wallet = MagicMock()
        wallet.mixdepth_count = 3
        wallet.get_utxos = AsyncMock(return_value=[_make_utxo(value=1_000, mixdepth=2)])
        wallet.get_locked_input_outpoints.return_value = set()

        with pytest.raises(ValueError, match="mixdepth 2"):
            await select_automatic_direct_send_inputs(
                wallet=wallet,
                amount_sats=50_000,
                destination=REGTEST_P2WPKH_ADDR,
                fee_rate=1.0,
                mixdepth=2,
            )

        wallet.get_utxos.assert_awaited_once_with(2)

    @pytest.mark.anyio
    async def test_search_limit_does_not_skip_higher_mixdepth(self) -> None:
        wallet = MagicMock()
        wallet.mixdepth_count = 3
        wallet.get_utxos = AsyncMock(return_value=[])
        wallet.get_locked_input_outpoints.return_value = set()

        with (
            patch(
                "jmwallet.wallet.coin_selection.select_direct_send_utxos",
                side_effect=DirectSendSearchLimitError("search capped"),
            ),
            pytest.raises(DirectSendSearchLimitError, match="search capped"),
        ):
            await select_automatic_direct_send_inputs(
                wallet=wallet,
                amount_sats=50_000,
                destination=REGTEST_P2WPKH_ADDR,
                fee_rate=1.0,
                mixdepth=None,
            )

        wallet.get_utxos.assert_awaited_once_with(2)


class TestDirectSendExplicitInputs:
    """input_utxos must reach prepare_direct_send through direct_send."""

    @pytest.mark.anyio
    async def test_passthrough(self) -> None:
        chosen = _make_utxo(txid="aa" * 32, vout=0, value=200_000)
        other = _make_utxo(txid="bb" * 32, vout=1, value=900_000)
        wallet = _make_mock_wallet([chosen, other])
        backend = _make_mock_backend()

        result = await direct_send(
            wallet=wallet,
            backend=backend,
            mixdepth=0,
            amount_sats=50_000,
            destination=REGTEST_P2WPKH_ADDR,
            fee_rate=1.0,
            input_utxos=[f"{'aa' * 32}:0"],
        )

        assert result.num_inputs == 1
        assert [i["outpoint"] for i in result.inputs] == [f"{'aa' * 32}:0"]
        backend.broadcast_transaction.assert_called_once()
