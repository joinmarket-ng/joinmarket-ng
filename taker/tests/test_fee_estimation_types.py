"""
Tests for mixed script-type fee/vsize estimation in the CoinJoin session.

A taproot CoinJoin may spend a legacy P2WPKH/P2WSH fidelity-bond input. The
fee estimate must classify the real input script types so it does not
under-estimate vsize (and therefore under-pay the miner fee).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from jmcore.bitcoin import estimate_vsize
from jmcore.models import OfferType

from taker.coinjoin_session import CoinJoinSession


def _session(offer_type: OfferType) -> CoinJoinSession:
    session = CoinJoinSession()
    fake_taker = MagicMock()
    fake_taker.config.preferred_offer_type = offer_type
    session.attach(fake_taker)
    session.maker_sessions = {}
    session.is_sweep = False
    session.taker_change_address = ""
    return session


class TestClassifyScriptpubkey:
    def test_known_types(self) -> None:
        classify = CoinJoinSession._classify_scriptpubkey
        assert classify("00" + "14" + "ab" * 20, "p2wpkh") == "p2wpkh"
        assert classify("51" + "20" + "cd" * 32, "p2wpkh") == "p2tr"
        assert classify("00" + "20" + "ef" * 32, "p2wpkh") == "p2wsh"
        assert classify("76a914" + "ab" * 20 + "88ac", "p2tr") == "p2pkh"
        assert classify("a914" + "ab" * 20 + "87", "p2tr") == "p2sh"

    def test_unknown_falls_back_to_default(self) -> None:
        classify = CoinJoinSession._classify_scriptpubkey
        assert classify("", "p2tr") == "p2tr"
        assert classify("deadbeef", "p2wpkh") == "p2wpkh"


class TestBuildScriptTypeLists:
    _P2WPKH_SPK = "0014" + "ab" * 20
    _P2TR_SPK = "5120" + "cd" * 32
    _P2WSH_SPK = "0020" + "ef" * 32

    def test_taproot_coinjoin_mixed_inputs_not_underestimated(self) -> None:
        """A tr0 CoinJoin spending a legacy bond input must size that input as
        its real (larger) type, not as a taproot key-path input."""
        session = _session(OfferType.TR0_ABSOLUTE)

        # One taproot taker UTXO plus one P2WSH bond input.
        taker_utxos = [
            SimpleNamespace(scriptpubkey=self._P2TR_SPK),
            SimpleNamespace(scriptpubkey=self._P2WSH_SPK),
        ]
        # Two equal cj outputs (taker + 0 makers) and one taker change output.
        session.taker_change_address = "bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080"
        num_outputs = 1 + 0 + 1  # cj outputs + taker change

        input_types, output_types = session._build_script_type_lists(taker_utxos, num_outputs)

        assert input_types == ["p2tr", "p2wsh"]
        # Uniform-taproot estimate would treat the bond as p2tr and be smaller.
        mixed = estimate_vsize(input_types, output_types)
        uniform = estimate_vsize(["p2tr", "p2tr"], output_types)
        assert mixed > uniform

    def test_segwit_coinjoin_uniform(self) -> None:
        session = _session(OfferType.SW0_ABSOLUTE)
        taker_utxos = [SimpleNamespace(scriptpubkey=self._P2WPKH_SPK)]
        input_types, output_types = session._build_script_type_lists(taker_utxos, 2)
        assert input_types == ["p2wpkh"]

    def test_output_count_drift_falls_back_to_uniform(self) -> None:
        """If the derived output list cannot match num_outputs, fall back to a
        uniform list so the lengths never disagree."""
        session = _session(OfferType.TR0_ABSOLUTE)
        taker_utxos = [SimpleNamespace(scriptpubkey=self._P2TR_SPK)]
        # taker_change_address unset -> only cj outputs derived, but caller
        # claims an extra change output exists.
        _, output_types = session._build_script_type_lists(taker_utxos, 3)
        assert output_types == ["p2tr", "p2tr", "p2tr"]


class TestMakerInputsMatchPit:
    """Rigid pit (JMP-0010): a maker's inputs must all match the pit type."""

    _P2WPKH_SPK = "0014" + "ab" * 20
    _P2TR_SPK = "5120" + "cd" * 32
    _P2WSH_SPK = "0020" + "ef" * 32

    def test_uniform_taproot_inputs_accepted(self) -> None:
        session = _session(OfferType.TR0_ABSOLUTE)
        maker = SimpleNamespace(utxos=[{"scriptpubkey": self._P2TR_SPK}])
        assert session._maker_inputs_match_pit(maker, "p2tr") is True

    def test_mixed_input_rejected(self) -> None:
        """A taproot round with a P2WSH bond input is not a uniform pit."""
        session = _session(OfferType.TR0_ABSOLUTE)
        maker = SimpleNamespace(
            utxos=[
                {"scriptpubkey": self._P2TR_SPK},
                {"scriptpubkey": self._P2WSH_SPK},
            ]
        )
        assert session._maker_inputs_match_pit(maker, "p2tr") is False

    def test_missing_scriptpubkey_rejected(self) -> None:
        session = _session(OfferType.SW0_ABSOLUTE)
        maker = SimpleNamespace(utxos=[{"scriptpubkey": ""}])
        assert session._maker_inputs_match_pit(maker, "p2wpkh") is False

    def test_segwit_pit_rejects_taproot_input(self) -> None:
        session = _session(OfferType.SW0_ABSOLUTE)
        maker = SimpleNamespace(utxos=[{"scriptpubkey": self._P2TR_SPK}])
        assert session._maker_inputs_match_pit(maker, "p2wpkh") is False


class TestAuthPubkeyOwnsUtxo:
    """Pit-aware ioauth ownership binding (rigid pit, JMP-0010)."""

    def test_sw0_binds_p2wpkh(self) -> None:
        from types import SimpleNamespace

        from bitcointx.core.key import CKey
        from jmcore.bitcoin import pubkey_to_p2wpkh_script

        session = _session(OfferType.SW0_RELATIVE)
        pub = bytes(CKey.from_secret_bytes(b"\x11" * 32).pub)
        spk = pubkey_to_p2wpkh_script(pub).hex()
        maker = SimpleNamespace(utxos=[{"scriptpubkey": spk}])
        assert session._auth_pubkey_owns_utxo(pub.hex(), maker) is True
        # A different key does not own it.
        other = bytes(CKey.from_secret_bytes(b"\x22" * 32).pub)
        assert session._auth_pubkey_owns_utxo(other.hex(), maker) is False

    def test_tr0_binds_taproot_output_key(self) -> None:
        """For a tr0 pit the internal auth pubkey is taptweaked to the P2TR key."""
        from types import SimpleNamespace

        from bitcointx.core.key import CKey
        from jmcore.bitcoin import taproot_tweak_pubkey

        session = _session(OfferType.TR0_RELATIVE)
        internal = bytes(CKey.from_secret_bytes(b"\x33" * 32).pub)
        _parity, output_xonly = taproot_tweak_pubkey(internal[1:])
        spk = "5120" + output_xonly.hex()
        maker = SimpleNamespace(utxos=[{"scriptpubkey": spk}])
        # The untweaked internal key (as the maker sends it) must bind.
        assert session._auth_pubkey_owns_utxo(internal.hex(), maker) is True
        # Binding the raw (untweaked) key as if it were P2WPKH must NOT match.
        assert (
            session._auth_pubkey_owns_utxo(
                bytes(CKey.from_secret_bytes(b"\x44" * 32).pub).hex(), maker
            )
            is False
        )
