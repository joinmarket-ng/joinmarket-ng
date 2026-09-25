"""Tests for the standalone operator configuration of a buyout service.

The configuration is the authorization boundary of the service, so the tests
below are about what a file is allowed to mean, not about how it parses: a
disabled file must stay minimal and inert, an enabled file must carry every
credential and must bind its payout to the configured network, and an operator
policy that no counterparty could agree with must fail before anything connects.
Loading is also checked to touch nothing except the file it was handed.
"""

from __future__ import annotations

import hashlib
import math
import re
from pathlib import Path

import pytest
from bitcointx.core.key import CKey
from jmcore.bitcoin import scriptpubkey_to_address

from jmswap.buyout_config import (
    BuyoutConfigError,
    BuyoutSettings,
    BuyoutSettlementOverrides,
    BuyoutSigningOverrides,
    load_buyout_settings,
)
from jmswap.buyout_settlement import SettlementPolicy
from jmswap.buyout_signing import BuyoutPolicy
from jmswap.buyout_terms import SPLIT_SAFETY_MARGIN_BLOCKS
from jmswap.lnd_peer import MAX_ROUTE_HINT_HOPS, MAX_ROUTE_HINT_PATHS, PaymentRouteHop


def _pubkey(tag: str) -> str:
    return bytes(CKey(hashlib.sha256(tag.encode()).digest()).pub).hex()


def _p2tr(tag: str, network: str) -> str:
    key = CKey(hashlib.sha256(tag.encode()).digest())
    return scriptpubkey_to_address(b"\x51\x20" + bytes(key.pub)[1:], network)


IDENTITY = _pubkey("test-identity")
PEER = _pubkey("test-peer")
OTHER_PEER = _pubkey("test-other-peer")
PAYOUT_REGTEST = _p2tr("test-payout", "regtest")
PAYOUT_MAINNET = _p2tr("test-payout", "mainnet")

ENABLED_TOML = f"""
[buyout]
enabled = true
network = "regtest"
journal = "buyout.sqlite"
lnd_endpoint = "127.0.0.1:10009"
lnd_identity = "{IDENTITY}"
lnd_tls_cert = "tls.cert"
lnd_peer_macaroon = "peer.macaroon"
lnd_escrow_macaroon = "escrow.macaroon"
bitcoin_rpc_url = "http://127.0.0.1:18443/"
bitcoin_rpc_user = "buyout"
bitcoin_rpc_password = "regtest-placeholder"
allowed_peers = ["{PEER}", "{OTHER_PEER}"]
payout_address = "{PAYOUT_REGTEST}"
mixdepth = 0
"""


def _write(tmp_path: Path, text: str, name: str = "config.toml") -> Path:
    path = tmp_path / name
    path.write_text(text)
    return path


def _enabled(
    tmp_path: Path, extra: str = "", name: str = "config.toml", **replacements: str
) -> Path:
    text = ENABLED_TOML
    for key, value in replacements.items():
        original = next(line for line in text.splitlines() if line.startswith(f"{key} = "))
        text = text.replace(original, f"{key} = {value}")
    return _write(tmp_path, text + extra, name)


class TestDisabledConfiguration:
    def test_minimal_disabled_file_loads_and_creates_nothing(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "[buyout]\nenabled = false\n")

        settings = load_buyout_settings(path)

        assert settings.enabled is False
        assert settings.network is None
        assert settings.journal is None
        assert settings.bitcoin_rpc_password is None
        assert settings.allowed_peers == ()
        assert settings.policy == BuyoutSigningOverrides()
        assert settings.settlement == BuyoutSettlementOverrides()
        assert list(tmp_path.iterdir()) == [path]

    def test_absent_section_is_disabled_rather_than_an_error(self, tmp_path: Path) -> None:
        path = _write(tmp_path, '[other]\nkey = "value"\n')

        assert load_buyout_settings(path).enabled is False

    def test_disabled_configuration_authorizes_no_policy(self, tmp_path: Path) -> None:
        settings = load_buyout_settings(_write(tmp_path, "[buyout]\n"))

        with pytest.raises(BuyoutConfigError, match="disabled"):
            settings.build_signing_policy()
        with pytest.raises(BuyoutConfigError, match="disabled"):
            settings.build_settlement_policy()

    def test_missing_file_is_reported_and_not_created(self, tmp_path: Path) -> None:
        missing = tmp_path / "absent.toml"

        with pytest.raises(BuyoutConfigError, match="cannot be read"):
            load_buyout_settings(missing)
        assert not missing.exists()

    def test_malformed_toml_is_rejected_without_quoting_the_file(self, tmp_path: Path) -> None:
        path = _write(tmp_path, '[buyout]\nbitcoin_rpc_password = "unterminated\n')

        with pytest.raises(BuyoutConfigError) as error:
            load_buyout_settings(path)
        assert "unterminated" not in str(error.value)

    def test_section_must_be_a_table(self, tmp_path: Path) -> None:
        with pytest.raises(BuyoutConfigError, match="must be a table"):
            load_buyout_settings(_write(tmp_path, "buyout = 1\n"))


class TestEnabledConfiguration:
    def test_valid_file_round_trips_into_both_policies(self, tmp_path: Path) -> None:
        path = _enabled(
            tmp_path,
            extra=(
                "\n[buyout.policy]\n"
                "csv_delay = 500\n"
                "buyer_settlement_depth = 8\n"
                "cltv_limit = 200\n"
                "settlement_depth = 4\n"
                "buyout_fee = 250\n"
                "proposal_lifetime_seconds = 300\n"
                "\n[buyout.settlement]\n"
                "payment_fee_limit_sat = 500\n"
                "payment_timeout_seconds = 45\n"
                "invoice_expiry_seconds = 1800\n"
                "max_chain_fee_sat = 25000\n"
                "bump_after_blocks = 2\n"
            ),
        )

        settings = load_buyout_settings(path)

        assert settings.build_signing_policy() == BuyoutPolicy(
            network="regtest",
            csv_delay=500,
            buyer_settlement_depth=8,
            cltv_limit=200,
            settlement_depth=4,
            buyout_fee=250,
            proposal_lifetime_seconds=300,
            settlement_enabled=True,
        )
        assert settings.build_settlement_policy() == SettlementPolicy(
            payment_fee_limit_sat=500,
            payment_timeout_seconds=45,
            invoice_expiry_seconds=1800,
            max_chain_fee_sat=25000,
            bump_after_blocks=2,
        )

    def test_defaults_match_the_runtime_policies(self, tmp_path: Path) -> None:
        settings = load_buyout_settings(_enabled(tmp_path))

        assert settings.build_signing_policy() == BuyoutPolicy(
            network="regtest", settlement_enabled=True
        )
        assert settings.build_settlement_policy() == SettlementPolicy()
        assert settings.poll_interval_seconds == 5.0
        assert settings.wallet_fingerprint is None

    def test_three_block_payment_and_one_block_invoice_are_explicit_opt_in(
        self, tmp_path: Path
    ) -> None:
        settings = load_buyout_settings(
            _enabled(
                tmp_path,
                extra=("\n[buyout.policy]\nbuyer_settlement_depth = 3\nsettlement_depth = 1\n"),
            )
        )

        policy = settings.build_signing_policy()
        assert policy.buyer_settlement_depth == 3
        assert policy.settlement_depth == 1
        assert policy.csv_delay >= (
            policy.buyer_settlement_depth + policy.cltv_limit + SPLIT_SAFETY_MARGIN_BLOCKS
        )
        assert BuyoutPolicy().buyer_settlement_depth == 6
        assert BuyoutPolicy().settlement_depth == 3

    def test_bare_default_cltv_limit_fits_the_shipped_csv_delay(self, tmp_path: Path) -> None:
        policy = load_buyout_settings(_enabled(tmp_path)).build_signing_policy()

        assert policy.cltv_limit == 360
        assert policy.csv_delay >= (
            policy.buyer_settlement_depth + policy.cltv_limit + SPLIT_SAFETY_MARGIN_BLOCKS
        )

    @pytest.mark.parametrize(
        ("csv_delay", "buyer_settlement_depth", "expected_cltv_limit"),
        [(300, 6, 258), (144, 6, 102), (500, 100, 360)],
    )
    def test_omitted_cltv_limit_is_derived_within_the_csv_budget(
        self,
        tmp_path: Path,
        csv_delay: int,
        buyer_settlement_depth: int,
        expected_cltv_limit: int,
    ) -> None:
        settings = load_buyout_settings(
            _enabled(
                tmp_path,
                extra=(
                    "\n[buyout.policy]\n"
                    f"csv_delay = {csv_delay}\n"
                    f"buyer_settlement_depth = {buyer_settlement_depth}\n"
                ),
            )
        )
        policy = settings.build_signing_policy()

        assert policy.cltv_limit == expected_cltv_limit
        assert policy.cltv_limit <= 360
        assert policy.cltv_limit <= (
            policy.csv_delay - policy.buyer_settlement_depth - SPLIT_SAFETY_MARGIN_BLOCKS
        )

    def test_explicit_cltv_limit_keeps_its_existing_validation(self, tmp_path: Path) -> None:
        valid = load_buyout_settings(
            _enabled(tmp_path, extra="\n[buyout.policy]\ncsv_delay = 300\ncltv_limit = 200\n")
        )

        assert valid.build_signing_policy().cltv_limit == 200

        with pytest.raises(BuyoutConfigError) as error:
            load_buyout_settings(
                _enabled(
                    tmp_path,
                    extra="\n[buyout.policy]\ncsv_delay = 300\ncltv_limit = 300\n",
                    name="invalid.toml",
                )
            )
        assert "[type=csv_delay_too_short]" in str(error.value)

    def test_enabling_the_service_is_the_settlement_authorization(self, tmp_path: Path) -> None:
        enabled = load_buyout_settings(_enabled(tmp_path))
        disabled = load_buyout_settings(_enabled(tmp_path, enabled="false", name="off.toml"))

        assert enabled.build_signing_policy().settlement_enabled is True
        with pytest.raises(BuyoutConfigError, match="disabled"):
            disabled.build_signing_policy()

    def test_payout_address_binds_to_the_configured_network(self, tmp_path: Path) -> None:
        settings = load_buyout_settings(_enabled(tmp_path))
        expected = bytes(CKey(hashlib.sha256(b"test-payout").digest()).pub)[1:]

        assert settings.payout_script() == (b"\x51\x20" + expected).hex()
        assert scriptpubkey_to_address(b"\x51\x20" + expected, "regtest") == PAYOUT_REGTEST

    @pytest.mark.parametrize("name", ["network", "journal", "lnd_identity", "payout_address"])
    def test_enabled_service_requires_every_connection_field(
        self, tmp_path: Path, name: str
    ) -> None:
        text = "\n".join(
            line for line in ENABLED_TOML.splitlines() if not line.startswith(f"{name} = ")
        )

        with pytest.raises(BuyoutConfigError, match="requires"):
            load_buyout_settings(_write(tmp_path, text))

    def test_enabled_service_requires_an_allowed_peer(self, tmp_path: Path) -> None:
        with pytest.raises(BuyoutConfigError, match="at least one allowed peer"):
            load_buyout_settings(_enabled(tmp_path, allowed_peers="[]"))


class TestRejectedConfiguration:
    def test_unknown_option_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(BuyoutConfigError, match="Extra inputs are not permitted"):
            load_buyout_settings(_enabled(tmp_path, extra='\nlnd_password = "oops"\n'))

    def test_unknown_policy_option_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(BuyoutConfigError, match="Extra inputs are not permitted"):
            load_buyout_settings(_enabled(tmp_path, extra="\n[buyout.policy]\nmax_fee = 1\n"))

    def test_unknown_settlement_option_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(BuyoutConfigError, match="Extra inputs are not permitted"):
            load_buyout_settings(_enabled(tmp_path, extra="\n[buyout.settlement]\nretries = 1\n"))

    @pytest.mark.parametrize(
        "option", ['network = "mainnet"', "settlement_enabled = true", "settlement_enabled = false"]
    )
    def test_policy_may_not_restate_network_or_settlement_authorization(
        self, tmp_path: Path, option: str
    ) -> None:
        with pytest.raises(BuyoutConfigError, match="Extra inputs are not permitted"):
            load_buyout_settings(_enabled(tmp_path, extra=f"\n[buyout.policy]\n{option}\n"))

    def test_payout_address_for_another_network_is_rejected(self, tmp_path: Path) -> None:
        path = _enabled(tmp_path, payout_address=f'"{PAYOUT_MAINNET}"')

        with pytest.raises(BuyoutConfigError, match="not a valid regtest address"):
            load_buyout_settings(path)

    def test_non_taproot_payout_address_is_rejected(self, tmp_path: Path) -> None:
        witness = hashlib.sha256(b"test-payout-p2wpkh").digest()[:20]
        p2wpkh = scriptpubkey_to_address(b"\x00\x14" + witness, "regtest")

        with pytest.raises(BuyoutConfigError, match="must be a P2TR"):
            load_buyout_settings(_enabled(tmp_path, payout_address=f'"{p2wpkh}"'))

    def test_invalid_peer_identity_is_rejected(self, tmp_path: Path) -> None:
        off_curve = "02" + "ff" * 32

        with pytest.raises(BuyoutConfigError, match="secp256k1"):
            load_buyout_settings(_enabled(tmp_path, allowed_peers=f'["{off_curve}"]'))

    def test_uncompressed_peer_identity_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(BuyoutConfigError, match="String should match pattern"):
            load_buyout_settings(_enabled(tmp_path, allowed_peers='["04' + "ab" * 32 + '"]'))

    def test_duplicate_and_self_referencing_peers_are_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(BuyoutConfigError, match="duplicate identity"):
            load_buyout_settings(_enabled(tmp_path, allowed_peers=f'["{PEER}", "{PEER}"]'))
        with pytest.raises(BuyoutConfigError, match="own lnd_identity"):
            load_buyout_settings(_enabled(tmp_path, allowed_peers=f'["{IDENTITY}"]'))

    @pytest.mark.parametrize("value", ["true", "-1", "0.5", '"0"'])
    def test_mixdepth_must_be_a_non_negative_integer(self, tmp_path: Path, value: str) -> None:
        with pytest.raises(BuyoutConfigError):
            load_buyout_settings(_enabled(tmp_path, mixdepth=value))

    @pytest.mark.parametrize("value", ["true", "0", "-5", "nan", "inf", '"5"'])
    def test_poll_interval_must_be_positive_and_finite(self, tmp_path: Path, value: str) -> None:
        with pytest.raises(BuyoutConfigError):
            load_buyout_settings(_enabled(tmp_path, extra=f"\npoll_interval_seconds = {value}\n"))

    def test_poll_interval_accepts_an_integral_number_of_seconds(self, tmp_path: Path) -> None:
        settings = load_buyout_settings(_enabled(tmp_path, extra="\npoll_interval_seconds = 30\n"))

        assert settings.poll_interval_seconds == 30.0
        assert math.isfinite(settings.poll_interval_seconds)

    @pytest.mark.parametrize(
        "option",
        [
            "csv_delay = 5",
            "csv_delay = true",
            "split_fee = -1",
            "split_fee_rate_sat_vb = 0",
            "min_split_output = 1",
            "buyer_settlement_depth = 2",
            "cltv_limit = 0",
            "sweep_response_blocks = 7",
            "parent_wait_blocks = 5",
            "max_freeze_blocks = 143",
            "freeze_ttl_blocks = 0",
            "proposal_lifetime_seconds = 601",
        ],
    )
    def test_policy_bounds_mirror_the_wire_bounds(self, tmp_path: Path, option: str) -> None:
        with pytest.raises(BuyoutConfigError):
            load_buyout_settings(_enabled(tmp_path, extra=f"\n[buyout.policy]\n{option}\n"))

    def test_csv_delay_must_cover_the_settlement_window(self, tmp_path: Path) -> None:
        overrides = (
            "\n[buyout.policy]\ncsv_delay = 185\nbuyer_settlement_depth = 6\ncltv_limit = 144\n"
        )

        with pytest.raises(BuyoutConfigError, match="csv_delay must cover"):
            load_buyout_settings(_enabled(tmp_path, extra=overrides))

    def test_omitted_cltv_limit_rejects_a_csv_budget_below_its_minimum(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(BuyoutConfigError) as error:
            load_buyout_settings(
                _enabled(
                    tmp_path,
                    extra="\n[buyout.policy]\ncsv_delay = 144\nbuyer_settlement_depth = 144\n",
                )
            )

        assert "[type=csv_delay_too_short]" in str(error.value)

    def test_settlement_depth_must_fit_the_buyer_window(self, tmp_path: Path) -> None:
        overrides = "\n[buyout.policy]\nbuyer_settlement_depth = 3\nsettlement_depth = 4\n"

        with pytest.raises(BuyoutConfigError, match="settlement_depth must not exceed"):
            load_buyout_settings(_enabled(tmp_path, extra=overrides))

    @pytest.mark.parametrize(
        "option",
        [
            "payment_fee_limit_sat = -1",
            "payment_timeout_seconds = 0",
            "invoice_expiry_seconds = 0",
            "max_chain_fee_sat = 0",
            "bump_after_blocks = 0",
            "bump_after_blocks = true",
        ],
    )
    def test_settlement_bounds_mirror_the_runtime_policy(self, tmp_path: Path, option: str) -> None:
        with pytest.raises(BuyoutConfigError):
            load_buyout_settings(_enabled(tmp_path, extra=f"\n[buyout.settlement]\n{option}\n"))

    def test_invoice_must_outlive_the_payment_attempt(self, tmp_path: Path) -> None:
        overrides = (
            "\n[buyout.settlement]\npayment_timeout_seconds = 60\ninvoice_expiry_seconds = 60\n"
        )

        with pytest.raises(BuyoutConfigError, match="invoice_expiry_seconds must be greater"):
            load_buyout_settings(_enabled(tmp_path, extra=overrides))

    @pytest.mark.parametrize(
        "endpoint",
        ['"https://127.0.0.1:10009"', '"127.0.0.1"', '"127.0.0.1:0"', '"127.0.0.1:70000"', '""'],
    )
    def test_lnd_endpoint_must_be_a_host_port_target(self, tmp_path: Path, endpoint: str) -> None:
        with pytest.raises(BuyoutConfigError, match="lnd_endpoint"):
            load_buyout_settings(_enabled(tmp_path, lnd_endpoint=endpoint))

    def test_bitcoin_rpc_url_must_be_http(self, tmp_path: Path) -> None:
        with pytest.raises(BuyoutConfigError, match="URL scheme"):
            load_buyout_settings(_enabled(tmp_path, bitcoin_rpc_url='"ftp://127.0.0.1:18443/"'))

    def test_bitcoin_rpc_url_must_not_embed_credentials(self, tmp_path: Path) -> None:
        url = '"http://buyout:hunter2@127.0.0.1:18443/"'

        with pytest.raises(BuyoutConfigError) as error:
            load_buyout_settings(_enabled(tmp_path, bitcoin_rpc_url=url))
        assert "must not embed credentials" in str(error.value)
        assert "hunter2" not in str(error.value)


class TestSecretsAndPaths:
    def test_password_is_not_exposed_by_a_validation_failure(self, tmp_path: Path) -> None:
        path = _enabled(tmp_path, bitcoin_rpc_password="1234567890")

        with pytest.raises(BuyoutConfigError) as error:
            load_buyout_settings(path)
        assert "1234567890" not in str(error.value)
        assert "bitcoin_rpc_password" in str(error.value)

    def test_password_is_not_exposed_by_the_settings_repr(self, tmp_path: Path) -> None:
        settings = load_buyout_settings(_enabled(tmp_path))

        assert "regtest-placeholder" not in repr(settings)
        assert "regtest-placeholder" not in str(settings.model_dump())
        assert settings.bitcoin_rpc_password is not None
        assert settings.bitcoin_rpc_password.get_secret_value() == "regtest-placeholder"

    def test_relative_paths_resolve_against_the_configuration_directory(
        self, tmp_path: Path
    ) -> None:
        directory = tmp_path / "operator"
        directory.mkdir()
        path = _write(
            directory,
            ENABLED_TOML.replace('journal = "buyout.sqlite"', 'journal = "state/buyout.sqlite"'),
        )

        settings = load_buyout_settings(path)

        assert settings.journal == directory / "state" / "buyout.sqlite"
        assert settings.lnd_tls_cert == directory / "tls.cert"
        assert settings.lnd_peer_macaroon == directory / "peer.macaroon"
        assert settings.lnd_escrow_macaroon == directory / "escrow.macaroon"
        assert not (directory / "state").exists()

    def test_absolute_paths_are_left_alone(self, tmp_path: Path) -> None:
        absolute = tmp_path / "elsewhere" / "buyout.sqlite"
        path = _enabled(tmp_path, journal=f'"{absolute}"')

        assert load_buyout_settings(path).journal == absolute

    def test_loader_requires_a_path(self) -> None:
        with pytest.raises(BuyoutConfigError, match="pathlib.Path"):
            load_buyout_settings("config.toml")  # type: ignore[arg-type]

    def test_direct_construction_keeps_the_disabled_default(self) -> None:
        settings = BuyoutSettings()

        assert settings.enabled is False
        assert settings.payout_address is None
        with pytest.raises(BuyoutConfigError):
            settings.payout_script()


def _hint_toml(**overrides: str) -> str:
    fields: dict[str, str] = {
        "node_id": f'"{PEER}"',
        "chan_id": "123456789",
        "fee_base_msat": "1000",
        "fee_proportional_millionths": "1",
        "cltv_expiry_delta": "80",
    }
    fields.update(overrides)
    hop = ", ".join(f"{name} = {value}" for name, value in fields.items())
    return f"\npayment_route_hints = [[{{ {hop} }}]]\n"


class TestPaymentRouteHints:
    def test_absent_hints_stay_empty(self, tmp_path: Path) -> None:
        assert load_buyout_settings(_enabled(tmp_path)).payment_route_hints == ()
        assert BuyoutSettings().payment_route_hints == ()

    def test_a_configured_hint_loads_as_typed_hops(self, tmp_path: Path) -> None:
        settings = load_buyout_settings(_enabled(tmp_path, extra=_hint_toml()))

        assert settings.payment_route_hints == (
            (
                PaymentRouteHop(
                    node_id=PEER,
                    chan_id=123456789,
                    fee_base_msat=1000,
                    fee_proportional_millionths=1,
                    cltv_expiry_delta=80,
                ),
            ),
        )

    def test_hints_are_not_part_of_the_settings_repr(self, tmp_path: Path) -> None:
        settings = load_buyout_settings(_enabled(tmp_path, extra=_hint_toml()))

        assert "payment_route_hints" not in repr(settings)
        assert "123456789" not in repr(settings)

    @pytest.mark.parametrize(
        "overrides",
        [
            {"node_id": '"not-a-key"'},
            {"chan_id": "0"},
            {"chan_id": "-1"},
            {"chan_id": '"123456789"'},
            {"fee_base_msat": "-1"},
            {"fee_base_msat": "4294967296"},
            {"fee_proportional_millionths": "1.5"},
            {"cltv_expiry_delta": "0"},
            {"cltv_expiry_delta": "65536"},
            {"cltv_expiry_delta": "true"},
        ],
    )
    def test_a_hop_outside_lnds_ranges_is_refused(
        self, tmp_path: Path, overrides: dict[str, str]
    ) -> None:
        path = _enabled(tmp_path, extra=_hint_toml(**overrides))

        with pytest.raises(BuyoutConfigError):
            load_buyout_settings(path)

    @pytest.mark.parametrize(
        "extra",
        [
            "\npayment_route_hints = [[]]\n",
            f'\npayment_route_hints = [{{ node_id = "{PEER}", chan_id = 1, '
            "fee_base_msat = 0, fee_proportional_millionths = 0, cltv_expiry_delta = 9 }]\n",
            f'\npayment_route_hints = [[{{ node_id = "{PEER}", chan_id = 1, '
            "fee_base_msat = 0, fee_proportional_millionths = 0 }]]\n",
            f'\npayment_route_hints = [[{{ node_id = "{PEER}", chan_id = 1, '
            "fee_base_msat = 0, fee_proportional_millionths = 0, cltv_expiry_delta = 9, "
            "extra = 1 }]]\n",
        ],
    )
    def test_a_malformed_hint_is_refused(self, tmp_path: Path, extra: str) -> None:
        with pytest.raises(BuyoutConfigError):
            load_buyout_settings(_enabled(tmp_path, extra=extra))

    @pytest.mark.parametrize("count", [MAX_ROUTE_HINT_PATHS + 1, 1])
    def test_more_hints_than_the_bound_are_refused(self, tmp_path: Path, count: int) -> None:
        hop = (
            f'{{ node_id = "{PEER}", chan_id = 1, fee_base_msat = 0, '
            "fee_proportional_millionths = 0, cltv_expiry_delta = 9 }"
        )
        path = _enabled(tmp_path, extra=f"\npayment_route_hints = [{f'[{hop}],' * count}]\n")

        if count > MAX_ROUTE_HINT_PATHS:
            with pytest.raises(BuyoutConfigError):
                load_buyout_settings(path)
        else:
            assert len(load_buyout_settings(path).payment_route_hints) == count

    def test_more_hops_than_the_bound_are_refused(self, tmp_path: Path) -> None:
        hop = (
            f'{{ node_id = "{PEER}", chan_id = 1, fee_base_msat = 0, '
            "fee_proportional_millionths = 0, cltv_expiry_delta = 9 }"
        )
        hops = f"{hop}," * (MAX_ROUTE_HINT_HOPS + 1)

        with pytest.raises(BuyoutConfigError):
            load_buyout_settings(_enabled(tmp_path, extra=f"\npayment_route_hints = [[{hops}]]\n"))

    def test_hints_do_not_change_the_derived_policies(self, tmp_path: Path) -> None:
        settings = load_buyout_settings(_enabled(tmp_path, extra=_hint_toml()))

        assert settings.build_settlement_policy() == SettlementPolicy()
        assert settings.build_signing_policy() == BuyoutPolicy(
            network="regtest", settlement_enabled=True
        )


TEMPLATE = Path(__file__).resolve().parents[1] / "config.toml.template"


class TestTemplate:
    def test_template_documents_every_setting(self) -> None:
        text = TEMPLATE.read_text()
        names = [
            *BuyoutSettings.model_fields,
            *BuyoutSigningOverrides.model_fields,
            *BuyoutSettlementOverrides.model_fields,
        ]

        for name in names:
            if name in {"policy", "settlement"}:
                continue
            assert re.search(rf"^#?\s*{name} = ", text, re.MULTILINE), name

    def test_template_ships_the_disabled_defaults_and_enables_cleanly(self, tmp_path: Path) -> None:
        text = TEMPLATE.read_text()

        assert load_buyout_settings(_write(tmp_path, text)).enabled is False

        enabled = _write(tmp_path, text.replace("enabled = false", "enabled = true", 1), "on.toml")
        settings = load_buyout_settings(enabled)

        assert settings.network == "regtest"
        assert settings.journal == tmp_path / "buyout-sessions.sqlite"
        assert settings.payout_script().startswith("5120")
        assert settings.build_signing_policy() == BuyoutPolicy(
            network="regtest", settlement_enabled=True
        )
        assert settings.build_settlement_policy() == SettlementPolicy()
