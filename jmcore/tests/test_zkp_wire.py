"""Wire-format tests for :mod:`jmcore.zkp_wire`."""

from __future__ import annotations

import os

import pytest

from jmcore.zkp_wire import (
    EPOCH_ID_LEN,
    ZkpParamsPayload,
    ZkpWireError,
    decode_zkpparams,
    encode_zkpparams,
)


def _payload(params_len: int = 66) -> ZkpParamsPayload:
    return ZkpParamsPayload(
        epoch_id=os.urandom(EPOCH_ID_LEN),
        issuer_params=os.urandom(params_len),
    )


def test_roundtrip() -> None:
    p = _payload()
    wire = encode_zkpparams(p)
    decoded = decode_zkpparams(wire)
    assert decoded.epoch_id == p.epoch_id
    assert decoded.issuer_params == p.issuer_params


def test_roundtrip_accepts_arbitrary_param_blob_size() -> None:
    # The codec is intentionally agnostic about the inner KVAC structure
    # so future nwabisabi versions changing param layout don't require a
    # wire-codec bump.
    for params_len in (33, 66, 128, 1024):
        p = _payload(params_len)
        decoded = decode_zkpparams(encode_zkpparams(p))
        assert decoded.issuer_params == p.issuer_params
        assert len(decoded.issuer_params) == params_len


def test_wire_format_is_two_hex_tokens() -> None:
    p = _payload()
    wire = encode_zkpparams(p)
    tokens = wire.split()
    assert len(tokens) == 2
    assert tokens[0] == p.epoch_id.hex()
    assert tokens[1] == p.issuer_params.hex()
    assert " " in wire
    assert "\n" not in wire


def test_encode_rejects_wrong_epoch_length() -> None:
    bad = ZkpParamsPayload(epoch_id=b"\x00" * 31, issuer_params=b"\x01" * 33)
    with pytest.raises(ZkpWireError, match="epoch_id must be 32 bytes"):
        encode_zkpparams(bad)


def test_encode_rejects_empty_issuer_params() -> None:
    bad = ZkpParamsPayload(epoch_id=os.urandom(EPOCH_ID_LEN), issuer_params=b"")
    with pytest.raises(ZkpWireError, match="non-empty"):
        encode_zkpparams(bad)


def test_decode_rejects_empty_body() -> None:
    with pytest.raises(ZkpWireError, match="empty"):
        decode_zkpparams("")
    with pytest.raises(ZkpWireError, match="empty"):
        decode_zkpparams("   ")


def test_decode_rejects_wrong_token_count() -> None:
    p = _payload()
    wire = encode_zkpparams(p)
    with pytest.raises(ZkpWireError, match="2 tokens"):
        decode_zkpparams(wire + " extra")
    with pytest.raises(ZkpWireError, match="2 tokens"):
        decode_zkpparams(wire.split()[0])


def test_decode_rejects_wrong_epoch_hex_length() -> None:
    short = "ab" * 31  # 62 chars, not 64
    params_hex = "00" * 33
    with pytest.raises(ZkpWireError, match="epoch_id_hex must be 64 chars"):
        decode_zkpparams(f"{short} {params_hex}")


def test_decode_rejects_non_hex_epoch() -> None:
    bad_epoch = "zz" * 32
    params_hex = "00" * 33
    with pytest.raises(ZkpWireError, match="epoch_id_hex not valid hex"):
        decode_zkpparams(f"{bad_epoch} {params_hex}")


def test_decode_rejects_non_hex_params() -> None:
    epoch_hex = "00" * 32
    with pytest.raises(ZkpWireError, match="issuer_pubkey_hex not valid hex"):
        decode_zkpparams(f"{epoch_hex} zzzz")


def test_decode_rejects_empty_params_token() -> None:
    # Two tokens separated by extra whitespace where the second token
    # is missing must not silently parse as a single-token line.
    with pytest.raises(ZkpWireError, match="2 tokens"):
        decode_zkpparams(("00" * 32) + " ")


def test_decode_collapses_whitespace() -> None:
    # str.split() with no argument collapses runs of whitespace; verify
    # that property holds end-to-end so peers using tabs or multiple
    # spaces interop.
    p = _payload()
    wire = f"{p.epoch_id.hex()}\t\t  {p.issuer_params.hex()}"
    decoded = decode_zkpparams(wire)
    assert decoded.epoch_id == p.epoch_id
    assert decoded.issuer_params == p.issuer_params


def test_real_nwabisabi_params_roundtrip() -> None:
    # Sanity check against the actual nwabisabi PyO3 surface so a
    # future param-blob-size bump there doesn't silently break the
    # wire layer. Skip if the binding is unavailable.
    nwabisabi = pytest.importorskip("nwabisabi")
    sk = nwabisabi.generate_issuer_secret_key()
    params = nwabisabi.derive_issuer_parameters(sk)
    payload = ZkpParamsPayload(epoch_id=os.urandom(EPOCH_ID_LEN), issuer_params=params)
    decoded = decode_zkpparams(encode_zkpparams(payload))
    assert decoded.issuer_params == params
