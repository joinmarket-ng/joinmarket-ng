"""Mixed ordinary-maker output validation for ring manifests."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from jmcore.cofunded_ring import ChannelPolicy, RingEdge, RingKeyPair, RingManifest, manifest_hash


def _key(index: int) -> str:
    return RingKeyPair.from_secret(index.to_bytes(32, "big")).public_key


def _script(index: int) -> str:
    return "5120" + f"{index:064x}"


def _policy() -> ChannelPolicy:
    return ChannelPolicy(
        fundee_csv_delay=144,
        fundee_reserve=10_000,
        min_depth=3,
        opener_csv_delay=144,
        opener_reserve=10_000,
    )


def _mixed_manifest_data() -> dict[str, Any]:
    keys = [_key(index) for index in range(1, 5)]
    edges = [
        RingEdge(
            opener_key=keys[index],
            acceptor_key=keys[(index + 1) % len(keys)],
            pending_channel_id=f"{index + 50:064x}",
            capacity=200_000 + index,
            output_index=5 + index,
            script_pubkey=_script(100 + index),
            policy=_policy(),
        )
        for index in range(4)
    ]
    outputs = [
        {"index": index, "amount": 100_000, "script_pubkey": _script(index + 1)}
        for index in range(5)
    ]
    outputs.extend(
        {
            "index": edge.output_index,
            "amount": edge.capacity,
            "script_pubkey": edge.script_pubkey,
        }
        for edge in edges
    )
    outputs.append({"index": 9, "amount": 25_000, "script_pubkey": _script(200)})
    return {
        "network": "regtest",
        "round_nonce": "11" * 32,
        "revision": 2,
        "unsigned_tx_hash": "22" * 32,
        "unsigned_txid": "33" * 32,
        "participant_keys": keys,
        "edges": edges,
        "equal_output_indices": [0, 1, 2, 3, 4],
        "outputs": outputs,
    }


def _manifest() -> RingManifest:
    return RingManifest.model_validate(_mixed_manifest_data())


def test_mixed_manifest_accepts_four_ring_members_and_five_equal_outputs() -> None:
    manifest = _manifest()
    outputs = {output.index: output for output in manifest.outputs}
    channel_indices = {edge.output_index for edge in manifest.edges}
    ordinary_indices = set(outputs) - set(manifest.equal_output_indices) - channel_indices

    assert len(manifest.participant_keys) == len(manifest.edges) == 4
    assert len(manifest.equal_output_indices) == 5
    assert len(manifest.outputs) == 10
    assert set(outputs) == set(range(10))
    assert ordinary_indices == {9}
    assert len({output.script_pubkey for output in outputs.values()}) == len(outputs)
    assert all(output.script_pubkey.startswith("5120") for output in outputs.values())
    assert {outputs[index].amount for index in manifest.equal_output_indices} == {100_000}
    assert outputs[9].amount == 25_000
    for edge in manifest.edges:
        assert outputs[edge.output_index].amount == edge.capacity
        assert outputs[edge.output_index].script_pubkey == edge.script_pubkey


@pytest.mark.parametrize("ordinary_count", [0, 1])
def test_three_member_ring_preserves_output_shape(ordinary_count: int) -> None:
    keys = [_key(index) for index in range(1, 4)]
    equal_count = 3 + ordinary_count
    edges = [
        RingEdge(
            opener_key=key,
            acceptor_key=keys[(index + 1) % len(keys)],
            pending_channel_id=f"{index + 50:064x}",
            capacity=200_000 + index,
            output_index=equal_count + index,
            script_pubkey=_script(100 + index),
            policy=_policy(),
        )
        for index, key in enumerate(keys)
    ]
    outputs = [
        {"index": index, "amount": 100_000, "script_pubkey": _script(index + 1)}
        for index in range(equal_count)
    ]
    outputs.extend(
        {"index": edge.output_index, "amount": edge.capacity, "script_pubkey": edge.script_pubkey}
        for edge in edges
    )
    if ordinary_count:
        outputs.append({"index": len(outputs), "amount": 25_000, "script_pubkey": _script(200)})
    manifest = RingManifest.model_validate(
        {
            "network": "regtest",
            "round_nonce": "11" * 32,
            "revision": 2,
            "unsigned_tx_hash": "22" * 32,
            "unsigned_txid": "33" * 32,
            "participant_keys": keys,
            "edges": edges,
            "equal_output_indices": list(range(equal_count)),
            "outputs": outputs,
        }
    )
    assert len(manifest.outputs) == 2 * equal_count
    assert len(manifest.edges) == 3


@pytest.mark.parametrize(
    "mutation",
    [
        "non_p2tr",
        "duplicate_script",
        "missing_index",
        "extra_index",
        "ordinary_zero_amount",
        "equal_channel_overlap",
        "edge_output_mismatch",
        "unequal_equal_amount",
    ],
)
def test_mixed_manifest_rejects_invalid_output_classes(mutation: str) -> None:
    data = _mixed_manifest_data()
    if mutation == "non_p2tr":
        data["outputs"][9]["script_pubkey"] = "0014" + "44" * 20
    elif mutation == "duplicate_script":
        data["outputs"][9]["script_pubkey"] = data["outputs"][0]["script_pubkey"]
    elif mutation == "missing_index":
        data["outputs"].pop()
    elif mutation == "extra_index":
        data["outputs"][-1]["index"] = 10
    elif mutation == "ordinary_zero_amount":
        data["outputs"][9]["amount"] = 0
    elif mutation == "equal_channel_overlap":
        data["equal_output_indices"][-1] = data["edges"][0].output_index
    elif mutation == "edge_output_mismatch":
        data["outputs"][5]["amount"] += 1
    elif mutation == "unequal_equal_amount":
        data["outputs"][0]["amount"] += 1
    else:  # pragma: no cover
        raise AssertionError(mutation)

    with pytest.raises(ValidationError):
        RingManifest.model_validate(data)


def test_mixed_manifest_hash_commits_the_inferred_ordinary_output() -> None:
    manifest = _manifest()
    changed_data = _mixed_manifest_data()
    changed_data["outputs"][9]["amount"] += 1
    changed = RingManifest.model_validate(changed_data)

    assert manifest_hash(manifest).hex() == (
        "33ad6d29334f4ed51f8892792db9d5c56bf3a270adc7fe5b247f5938759c99c5"
    )
    assert manifest_hash(changed) != manifest_hash(manifest)
