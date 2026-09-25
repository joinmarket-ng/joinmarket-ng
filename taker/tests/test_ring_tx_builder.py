from __future__ import annotations

import random
from typing import Any

import pytest
from jmcore.bitcoin import parse_transaction_bytes, scriptpubkey_to_address
from jmcore.randomness import secure_random
from pydantic import ValidationError

from taker.tx_builder import (
    ChannelEndpointContribution,
    FinalizedOrdinaryMakerChange,
    FinalizedRingChannelOutput,
    FinalizedRingTransactionPlan,
    build_coinjoin_tx,
)

EQUAL_ADDRESS = "bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080"
CHANGE_ADDRESS = "bcrt1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qzf4jry"


def _ring_equal_address(index: int) -> str:
    script = bytes.fromhex("5120" + f"{index + 1000:064x}")
    return scriptpubkey_to_address(script, "regtest")


def _channel_output(
    index: int,
    opener_id: str,
    opener_amount: int,
    fundee_id: str,
    fundee_amount: int,
) -> FinalizedRingChannelOutput:
    script = bytes.fromhex("5120" + f"{index + 1:064x}")
    return FinalizedRingChannelOutput(
        edge_id=f"edge-{index}",
        script_pubkey=script.hex(),
        address=scriptpubkey_to_address(script, "regtest"),
        capacity=opener_amount + fundee_amount,
        opener=ChannelEndpointContribution(
            participant_id=opener_id,
            amount=opener_amount,
        ),
        fundee=ChannelEndpointContribution(
            participant_id=fundee_id,
            amount=fundee_amount,
        ),
    )


def _plan() -> FinalizedRingTransactionPlan:
    return FinalizedRingTransactionPlan(
        network="regtest",
        participant_ids=["taker", "maker-1", "maker-2", "maker-3"],
        residuals={
            "taker": 400_000,
            "maker-1": 410_000,
            "maker-2": 420_000,
            "maker-3": 430_000,
        },
        ring_participant_ids=["taker", "maker-1", "maker-2", "maker-3"],
        channel_outputs=[
            _channel_output(0, "taker", 200_000, "maker-1", 205_000),
            _channel_output(1, "maker-1", 205_000, "maker-2", 210_000),
            _channel_output(2, "maker-2", 210_000, "maker-3", 215_000),
            _channel_output(3, "maker-3", 215_000, "taker", 200_000),
        ],
    )


def _mixed_plan() -> FinalizedRingTransactionPlan:
    plan = _plan().model_dump()
    plan["participant_ids"].append("maker-4")
    plan["residuals"]["taker"] = 399_000
    plan["residuals"]["maker-4"] = 440_000
    plan["channel_outputs"][0]["opener"]["amount"] = 199_000
    plan["channel_outputs"][0]["capacity"] = 404_000
    script = bytes.fromhex("5120" + f"{2_000:064x}")
    plan["ordinary_maker_changes"] = {
        "maker-4": {
            "address": scriptpubkey_to_address(script, "regtest"),
            "script_pubkey": script.hex(),
            "amount": 440_000,
        }
    }
    return FinalizedRingTransactionPlan(**plan)


def _maker_data() -> dict[str, dict[str, object]]:
    residuals = {"maker-1": 410_000, "maker-2": 420_000, "maker-3": 430_000}
    return {
        maker_id: {
            "utxos": [
                {
                    "txid": f"{index + 2:064x}",
                    "vout": index,
                    "value": 1_001_000 + residual,
                }
            ],
            "cj_addr": _ring_equal_address(index + 1),
            "change_addr": CHANGE_ADDRESS,
            "cjfee": 1_000,
            "txfee": 2_000,
        }
        for index, (maker_id, residual) in enumerate(residuals.items())
    }


def _mixed_maker_data() -> dict[str, dict[str, object]]:
    maker_data = _maker_data()
    ordinary_change = _mixed_plan().ordinary_maker_changes["maker-4"]
    maker_data["maker-4"] = {
        "utxos": [{"txid": f"{6:064x}", "vout": 4, "value": 1_441_000}],
        "cj_addr": _ring_equal_address(4),
        "change_addr": ordinary_change.address,
        "cjfee": 1_000,
        "txfee": 2_000,
    }
    return maker_data


def _build_ring(
    *,
    plan: FinalizedRingTransactionPlan | None = None,
    taker_total_input: int = 1_408_000,
    taker_utxo_value: int | None = None,
    maker_data: dict[str, dict[str, object]] | None = None,
) -> tuple[bytes, dict[str, Any]]:
    return build_coinjoin_tx(
        taker_utxos=[
            {
                "txid": "01" * 32,
                "vout": 0,
                "value": taker_total_input if taker_utxo_value is None else taker_utxo_value,
            }
        ],
        taker_cj_address=_ring_equal_address(0),
        taker_change_address=CHANGE_ADDRESS,
        taker_total_input=taker_total_input,
        maker_data=_maker_data() if maker_data is None else maker_data,
        cj_amount=1_000_000,
        tx_fee=5_000,
        network="regtest",
        ring_plan=_plan() if plan is None else plan,
    )


def test_ring_transaction_has_n_equal_and_n_channel_outputs_without_change() -> None:
    random.seed(11)
    tx_bytes, metadata = _build_ring()
    transaction = parse_transaction_bytes(tx_bytes)

    assert len(transaction.outputs) == 8
    assert sum(output.value == 1_000_000 for output in transaction.outputs) == 4
    assert sum(kind == "channel" for _, kind in metadata["output_owners"]) == 4
    assert all(kind != "change" for _, kind in metadata["output_owners"])
    assert all(owner is None for owner, kind in metadata["output_owners"] if kind == "channel")


def test_mixed_ring_transaction_has_authorized_ordinary_maker_change() -> None:
    plan = _mixed_plan()
    tx_bytes, metadata = _build_ring(plan=plan, maker_data=_mixed_maker_data())
    transaction = parse_transaction_bytes(tx_bytes)

    ordinary_change = plan.ordinary_maker_changes["maker-4"]
    assert len(transaction.outputs) == 10
    assert sum(output.value == 1_000_000 for output in transaction.outputs) == 5
    assert sum(kind == "channel" for _, kind in metadata["output_owners"]) == 4
    assert metadata["output_owners"].count(("maker-4", "change")) == 1
    assert (ordinary_change.script_pubkey, ordinary_change.amount) in {
        (output.scriptpubkey, output.value) for output in transaction.outputs
    }
    assert all(
        owner != "maker-4" or kind in {"cj", "change"} for owner, kind in metadata["output_owners"]
    )
    for participant_id in plan.ring_participant_ids:
        assert (participant_id, "change") not in metadata["output_owners"]


def _coordinator_only_plan() -> FinalizedRingTransactionPlan:
    script = bytes.fromhex("5120" + f"{2000:064x}")
    return FinalizedRingTransactionPlan(
        network="regtest",
        participant_ids=["taker", "maker-1", "maker-2", "maker-3", "maker-4"],
        residuals={
            "taker": 399_000,
            "maker-1": 410_000,
            "maker-2": 420_000,
            "maker-3": 430_000,
            "maker-4": 440_000,
        },
        ring_participant_ids=["maker-1", "maker-2", "maker-3", "maker-4"],
        channel_outputs=[
            _channel_output(0, "maker-1", 205_000, "maker-2", 210_000),
            _channel_output(1, "maker-2", 210_000, "maker-3", 215_000),
            _channel_output(2, "maker-3", 215_000, "maker-4", 220_000),
            _channel_output(3, "maker-4", 220_000, "maker-1", 205_000),
        ],
        ordinary_taker_change=FinalizedOrdinaryMakerChange(
            address=scriptpubkey_to_address(script, "regtest"),
            script_pubkey=script.hex(),
            amount=399_000,
        ),
    )


def test_coordinator_only_five_party_plan_keeps_taker_change_p2tr_and_hidden() -> None:
    plan = _coordinator_only_plan()
    data = _mixed_maker_data()
    tx_bytes, metadata = build_coinjoin_tx(
        taker_utxos=[{"txid": "01" * 32, "vout": 0, "value": 1_408_000}],
        taker_cj_address=_ring_equal_address(0),
        taker_change_address=plan.ordinary_taker_change.address,
        taker_total_input=1_408_000,
        maker_data=data,
        cj_amount=1_000_000,
        tx_fee=5_000,
        network="regtest",
        ring_plan=plan,
    )
    tx = parse_transaction_bytes(tx_bytes)
    assert len(tx.outputs) == 10
    assert sum(output.value == 1_000_000 for output in tx.outputs) == 5
    assert metadata["output_owners"].count(("taker", "change")) == 1
    assert sum(kind == "channel" for _, kind in metadata["output_owners"]) == 4
    assert all(output.scriptpubkey.startswith("5120") for output in tx.outputs)
    assert (plan.ordinary_taker_change.script_pubkey, 399_000) in {
        (output.scriptpubkey, output.value) for output in tx.outputs
    }


def test_three_ring_makers_keep_ordinary_taker_change_without_extra_outputs() -> None:
    script = bytes.fromhex("5120" + f"{2000:064x}")
    plan = FinalizedRingTransactionPlan(
        network="regtest",
        participant_ids=["taker", "maker-1", "maker-2", "maker-3"],
        residuals={"taker": 400_000, "maker-1": 410_000, "maker-2": 420_000, "maker-3": 430_000},
        ring_participant_ids=["maker-1", "maker-2", "maker-3"],
        channel_outputs=[
            _channel_output(0, "maker-1", 205_000, "maker-2", 205_000),
            _channel_output(1, "maker-2", 215_000, "maker-3", 215_000),
            _channel_output(2, "maker-3", 215_000, "maker-1", 205_000),
        ],
        ordinary_taker_change=FinalizedOrdinaryMakerChange(
            address=scriptpubkey_to_address(script, "regtest"),
            script_pubkey=script.hex(),
            amount=400_000,
        ),
    )
    tx_bytes, metadata = build_coinjoin_tx(
        taker_utxos=[{"txid": "01" * 32, "vout": 0, "value": 1_408_000}],
        taker_cj_address=_ring_equal_address(0),
        taker_change_address=plan.ordinary_taker_change.address,
        taker_total_input=1_408_000,
        maker_data=_maker_data(),
        cj_amount=1_000_000,
        tx_fee=5_000,
        network="regtest",
        ring_plan=plan,
    )
    tx = parse_transaction_bytes(tx_bytes)
    assert len(tx.outputs) == 8
    assert metadata["output_owners"].count(("taker", "change")) == 1
    assert sum(kind == "channel" for _, kind in metadata["output_owners"]) == 3
    assert sum(output.value == 1_000_000 for output in tx.outputs) == 4


def test_coordinator_only_requires_exact_authorized_taker_change() -> None:
    plan = _coordinator_only_plan()
    with pytest.raises(ValueError, match="taker ordinary change address or amount"):
        build_coinjoin_tx(
            taker_utxos=[{"txid": "01" * 32, "vout": 0, "value": 1_408_000}],
            taker_cj_address=_ring_equal_address(0),
            taker_change_address=_ring_equal_address(70),
            taker_total_input=1_408_000,
            maker_data=_mixed_maker_data(),
            cj_amount=1_000_000,
            tx_fee=5_000,
            network="regtest",
            ring_plan=plan,
        )


def test_shared_outputs_remain_exact_after_output_shuffle() -> None:
    plan = _plan()
    random.seed(29)
    tx_bytes, metadata = _build_ring(plan=plan)
    transaction = parse_transaction_bytes(tx_bytes)
    actual = {(output.scriptpubkey, output.value) for output in transaction.outputs}
    expected = {(output.script_pubkey, output.capacity) for output in plan.channel_outputs}
    assert expected <= actual
    assert set(edge for edge in metadata["channel_edges"] if edge is not None) == {
        output.edge_id for output in plan.channel_outputs
    }
    for endpoints, owner in zip(
        metadata["channel_endpoints"], metadata["output_owners"], strict=True
    ):
        if owner[1] == "channel":
            assert endpoints is not None
            opener, fundee = endpoints
            assert opener["participant_id"] != fundee["participant_id"]
        else:
            assert endpoints is None


def test_ring_transaction_shuffle_uses_secure_random(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def tracking_shuffle(values: list[object]) -> None:
        nonlocal calls
        calls += 1
        values.reverse()

    monkeypatch.setattr(secure_random, "shuffle", tracking_shuffle)
    _build_ring()
    assert calls == 2


def test_plan_accounts_for_every_residual_and_includes_taker() -> None:
    plan = _plan()
    assert plan.taker_id in plan.participant_ids
    for participant_id, residual in plan.residuals.items():
        outgoing = next(
            output.opener.amount
            for output in plan.channel_outputs
            if output.opener.participant_id == participant_id
        )
        incoming = next(
            output.fundee.amount
            for output in plan.channel_outputs
            if output.fundee.participant_id == participant_id
        )
        assert outgoing + incoming == residual
    assert sum(output.capacity for output in plan.channel_outputs) == sum(plan.residuals.values())


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda data: data["ordinary_maker_changes"].pop("maker-4"),
            "ordinary maker changes",
        ),
        (
            lambda data: data["ordinary_maker_changes"].update(
                {"maker-1": data["ordinary_maker_changes"]["maker-4"]}
            ),
            "ordinary maker changes",
        ),
        (
            lambda data: data.update(
                {"ring_participant_ids": ["maker-1", "maker-2", "maker-3", "maker-4"]}
            ),
            "include the taker",
        ),
        (
            lambda data: data["ordinary_maker_changes"]["maker-4"].update(
                {"address": _ring_equal_address(0)}
            ),
            "ordinary change address and script_pubkey",
        ),
        (
            lambda data: data["ordinary_maker_changes"]["maker-4"].update(
                {"script_pubkey": "5120" + f"{2_001:064x}"}
            ),
            "ordinary change address and script_pubkey",
        ),
        (
            lambda data: data["ordinary_maker_changes"]["maker-4"].update({"amount": 439_999}),
            "ordinary maker change does not equal residual",
        ),
        (
            lambda data: data["ordinary_maker_changes"]["maker-4"].update({"amount": 27_299}),
            "at least 27300",
        ),
        (
            lambda data: data["ordinary_maker_changes"]["maker-4"].update(
                {"script_pubkey": "0020" + "00" * 32}
            ),
            "String should match pattern",
        ),
        (
            lambda data: data["ordinary_maker_changes"]["maker-4"].update(
                {
                    "script_pubkey": data["channel_outputs"][0]["script_pubkey"],
                    "address": data["channel_outputs"][0]["address"],
                }
            ),
            "duplicate output scripts",
        ),
    ],
)
def test_mixed_plan_rejects_invalid_ordinary_authorization(mutation: Any, message: str) -> None:
    data = _mixed_plan().model_dump()
    mutation(data)
    with pytest.raises(ValidationError, match=message):
        FinalizedRingTransactionPlan(**data)


def test_mixed_ring_transaction_rejects_wrong_maker_change_address() -> None:
    maker_data = _mixed_maker_data()
    maker_data["maker-4"]["change_addr"] = _ring_equal_address(0)
    with pytest.raises(ValueError, match="ordinary change address differs"):
        _build_ring(plan=_mixed_plan(), maker_data=maker_data)


def test_ring_transaction_rejects_global_fee_mismatch() -> None:
    with pytest.raises(ValueError, match="declared miner fee"):
        _build_ring(taker_utxo_value=1_408_001)


def test_duplicate_channel_script_and_edge_id_are_rejected() -> None:
    plan = _plan()
    data = plan.model_dump()
    data["channel_outputs"][1]["script_pubkey"] = data["channel_outputs"][0]["script_pubkey"]
    data["channel_outputs"][1]["address"] = data["channel_outputs"][0]["address"]
    with pytest.raises(ValidationError, match="duplicate channel scripts"):
        FinalizedRingTransactionPlan(**data)

    data = plan.model_dump()
    data["channel_outputs"][1]["edge_id"] = data["channel_outputs"][0]["edge_id"]
    with pytest.raises(ValidationError, match="duplicate edge IDs"):
        FinalizedRingTransactionPlan(**data)

    data = plan.model_dump()
    data["channel_outputs"].pop()
    with pytest.raises(ValidationError, match="requires one channel output per ring participant"):
        FinalizedRingTransactionPlan(**data)


def test_missing_participant_and_overpaid_residual_are_rejected() -> None:
    maker_data = _maker_data()
    del maker_data["maker-3"]
    with pytest.raises(ValueError, match="participants do not match"):
        _build_ring(maker_data=maker_data)

    with pytest.raises(ValueError, match="finalized residuals"):
        _build_ring(taker_total_input=1_408_001)


def test_endpoint_overpayment_is_rejected_by_finalized_plan() -> None:
    plan = _plan()
    data = plan.model_dump()
    data["residuals"]["taker"] += 1
    with pytest.raises(ValidationError, match="do not equal residual"):
        FinalizedRingTransactionPlan(**data)


def test_disjoint_channel_cycles_are_rejected() -> None:
    data = _plan().model_dump()
    second = data["channel_outputs"][1]
    second["fundee"] = {"participant_id": "taker", "amount": 200_000}
    second["capacity"] = second["opener"]["amount"] + second["fundee"]["amount"]
    fourth = data["channel_outputs"][3]
    fourth["fundee"] = {"participant_id": "maker-2", "amount": 210_000}
    fourth["capacity"] = fourth["opener"]["amount"] + fourth["fundee"]["amount"]
    with pytest.raises(ValidationError, match="directed cycle"):
        FinalizedRingTransactionPlan(**data)


def test_wrong_network_channel_address_is_rejected() -> None:
    plan = _plan()
    data = plan.model_dump()
    script = bytes.fromhex(data["channel_outputs"][0]["script_pubkey"])
    data["channel_outputs"][0]["address"] = scriptpubkey_to_address(script, "mainnet")
    with pytest.raises(ValidationError, match="plan network"):
        FinalizedRingTransactionPlan(**data)


def test_ring_transaction_rejects_non_tr0_equal_outputs() -> None:
    maker_data = _maker_data()
    for data in maker_data.values():
        data["cj_addr"] = EQUAL_ADDRESS
    with pytest.raises(ValueError, match="tr0 P2TR equal outputs"):
        build_coinjoin_tx(
            taker_utxos=[{"txid": "01" * 32, "vout": 0, "value": 1_408_000}],
            taker_cj_address=EQUAL_ADDRESS,
            taker_change_address=CHANGE_ADDRESS,
            taker_total_input=1_408_000,
            maker_data=maker_data,
            cj_amount=1_000_000,
            tx_fee=5_000,
            network="regtest",
            ring_plan=_plan(),
        )


def test_ordinary_transaction_path_keeps_fixed_byte_vector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deterministic_random = random.Random(12345)
    monkeypatch.setattr(secure_random, "shuffle", deterministic_random.shuffle)
    tx_bytes, metadata = build_coinjoin_tx(
        taker_utxos=[{"txid": "aa" * 32, "vout": 0, "value": 1_500_000}],
        taker_cj_address=EQUAL_ADDRESS,
        taker_change_address=CHANGE_ADDRESS,
        taker_total_input=1_500_000,
        maker_data={
            "maker-1": {
                "utxos": [{"txid": "bb" * 32, "vout": 1, "value": 1_300_000}],
                "cj_addr": EQUAL_ADDRESS,
                "change_addr": CHANGE_ADDRESS,
                "cjfee": 1_000,
                "txfee": 2_000,
            }
        },
        cj_amount=1_000_000,
        tx_fee=5_000,
        network="regtest",
    )
    assert tx_bytes.hex() == (
        "0200000002aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa0000000000"
        "ffffffffbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb0100000000"
        "ffffffff04f88f0400000000002200201863143c14c5166804bd19203356da136c985678cd4d27a1"
        "b8c6329604903262b0890700000000002200201863143c14c5166804bd19203356da136c985678cd"
        "4d27a1b8c632960490326240420f0000000000160014751e76e8199196d454941c45d1b3a323f143"
        "3bd640420f0000000000160014751e76e8199196d454941c45d1b3a323f1433bd600000000"
    )
    assert "channel_edges" not in metadata
    assert "channel_endpoints" not in metadata
