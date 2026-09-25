"""Operator configuration for a standalone private buyout service.

An operator runs the buyout endpoint next to its own Lightning node and its own
Bitcoin Core, so the choices that decide whether that endpoint may sign and
settle at all (which network, which peers, which payout, which economic bounds)
are configuration, not protocol. This module is the whole configuration surface:
:func:`load_buyout_settings` reads one explicit TOML file, validates its
``[buyout]`` table, and returns a :class:`BuyoutSettings` that a runtime can act
on without re-checking anything.

Loading is inert. Nothing here discovers a config file, reads an environment
variable, creates or migrates a file, opens a socket, or touches a wallet or a
node. The certificate and macaroon paths are validated as paths and never read:
what they point at is the runtime's business, and an unreadable macaroon must
fail where it is used, not where it is named. The only file this module opens is
the TOML file it was given.

Enabling is authorization
-------------------------

``enabled`` defaults to ``False`` and a disabled configuration may be a single
key. Turning it on is an affirmative operator action, and it is the only thing
that sets :attr:`~jmswap.buyout_signing.BuyoutPolicy.settlement_enabled`: an
operator who starts this service authorizes settlement for the sessions it
creates from then on. It authorizes nothing retroactively. A session that was
recorded without ``settlement_authorized`` stays unauthorized forever, because
that marker lives in the journal and
:class:`~jmswap.buyout_settlement.BuyoutSettlement` reads it per session; no
configuration change can grant it. Disabling the service likewise does not
revoke an authorization already written for a live session.

Policy overrides are validated before anything connects
-------------------------------------------------------

``[buyout.policy]`` and ``[buyout.settlement]`` override the economic defaults
of :class:`~jmswap.buyout_signing.BuyoutPolicy` and
:class:`~jmswap.buyout_settlement.SettlementPolicy`. Their bounds are the wire
bounds of :mod:`jmswap.buyout_messages` and the agreement invariants of
:mod:`jmswap.buyout_terms`, restated here so an impossible policy is a startup
error rather than a proposal that every counterparty rejects, or an acceptance
this node can never honor. The two cross-field rules that hold without attempt
data are checked: the CSV delay must cover the buyer's settlement window, its
CLTV limit and the split safety margin, and the counterparty's settlement depth
must fit inside the buyer's. Rules that need a concrete attempt (the exact split
fee for a measured split, the sweep reserve for a measured claim, the split
minimum against a real entitlement) stay where they are enforced, in
:func:`~jmswap.buyout_terms.validate_parent`.

``network`` and ``settlement_enabled`` are not policy keys. They are decided by
``[buyout]`` itself, and naming them in the policy table is rejected rather than
silently reconciled.

Errors
------

Every failure raises :class:`BuyoutConfigError`. Models hide their inputs in
validation errors, so a rejected ``bitcoin_rpc_password`` (wrong type, say)
reports the rule and not the value, and the underlying exception chain is
suppressed so a TOML parse error cannot carry a line of the file into a
traceback. ``bitcoin_rpc_password`` is a :class:`~pydantic.SecretStr`, so
neither the repr of the settings nor a log of the whole model shows it; callers
should still log named fields rather than the model.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from jmcore.bitcoin import address_to_scriptpubkey_for_network
from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    ValidationInfo,
    model_validator,
)
from pydantic_core import PydanticCustomError

from jmswap.bitcoin_escrow import MAX_CSV_DELAY, MIN_CSV_DELAY, MIN_SPLIT_OUTPUT_SATS
from jmswap.buyout_messages import (
    MAX_MONEY,
    MAX_SETTLEMENT_DEPTH,
    MAX_SWEEP_RESPONSE_BLOCKS,
    MAX_UINT32,
    MIN_MAX_FREEZE_BLOCKS,
    MIN_PARENT_WAIT_BLOCKS,
    CompressedPubKey,
    FeeRateSatVb,
    Satoshis,
)
from jmswap.buyout_settlement import SettlementPolicy
from jmswap.buyout_signing import BuyoutPolicy
from jmswap.buyout_terms import SPLIT_SAFETY_MARGIN_BLOCKS
from jmswap.lnd_peer import MAX_ROUTE_HINT_HOPS, MAX_ROUTE_HINT_PATHS, PaymentRouteHop

CONFIG_SECTION = "buyout"
"""The only table of the TOML document this module reads."""

MAX_PROPOSAL_LIFETIME_SECONDS = 600
"""Longest proposal lifetime a counterparty accepts (see ``_propose``)."""

MAX_PORT = 65_535

_DEFAULT_POLICY = BuyoutPolicy()
_DEFAULT_SETTLEMENT = SettlementPolicy()

# host:port or [v6]:port, with no scheme and no path: a gRPC channel target.
_ENDPOINT_SHAPE = re.compile(
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?|\[[0-9A-Fa-f:.]+\]):(\d{1,5})"
)

_PATH_FIELDS = ("journal", "lnd_tls_cert", "lnd_peer_macaroon", "lnd_escrow_macaroon")

_REQUIRED_WHEN_ENABLED = (
    "network",
    "journal",
    "lnd_endpoint",
    "lnd_identity",
    "lnd_tls_cert",
    "lnd_peer_macaroon",
    "lnd_escrow_macaroon",
    "bitcoin_rpc_url",
    "bitcoin_rpc_user",
    "bitcoin_rpc_password",
    "payout_address",
    "mixdepth",
)

_P2TR_SCRIPT_LENGTH = 34

BuyoutNetwork = Literal["regtest", "signet", "testnet", "mainnet"]

WalletFingerprint = Annotated[str, Field(pattern=r"^[0-9a-f]{8}$")]
"""The JoinMarket wallet identifier as 8 lowercase hex characters."""


class BuyoutConfigError(Exception):
    """The buyout configuration file is unreadable, malformed or invalid.

    The message names the rule that failed. It never quotes a configured value,
    so a caller can log it without logging a credential.
    """


def _invalid(code: str, message: str) -> PydanticCustomError:
    return PydanticCustomError(code, message)


class _ConfigModel(BaseModel):
    """Frozen, strict, closed model that never echoes its input in an error."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, hide_input_in_errors=True)


class BuyoutSigningOverrides(_ConfigModel):
    """``[buyout.policy]``: the economic terms this node proposes and accepts.

    Defaults are :class:`~jmswap.buyout_signing.BuyoutPolicy`'s own, so an
    absent table and an empty table mean the same thing. ``network`` and
    ``settlement_enabled`` are deliberately absent: they come from ``[buyout]``.
    """

    csv_delay: Annotated[int, Field(ge=MIN_CSV_DELAY, le=MAX_CSV_DELAY)] = _DEFAULT_POLICY.csv_delay
    split_fee: Satoshis = _DEFAULT_POLICY.split_fee
    split_fee_rate_sat_vb: FeeRateSatVb = _DEFAULT_POLICY.split_fee_rate_sat_vb
    min_split_output: Annotated[int, Field(ge=MIN_SPLIT_OUTPUT_SATS, le=MAX_MONEY)] = (
        _DEFAULT_POLICY.min_split_output
    )
    sweep_fee_reserve: Satoshis = _DEFAULT_POLICY.sweep_fee_reserve
    buyer_settlement_depth: Annotated[int, Field(ge=3, le=MAX_SETTLEMENT_DEPTH)] = (
        _DEFAULT_POLICY.buyer_settlement_depth
    )
    cltv_limit: Annotated[int, Field(ge=1, le=MAX_CSV_DELAY)] = _DEFAULT_POLICY.cltv_limit
    sweep_response_blocks: Annotated[int, Field(ge=0, le=MAX_SWEEP_RESPONSE_BLOCKS)] = (
        _DEFAULT_POLICY.sweep_response_blocks
    )
    buyout_fee: Satoshis = _DEFAULT_POLICY.buyout_fee
    timeout_compensation: Satoshis = _DEFAULT_POLICY.timeout_compensation
    settlement_depth: Annotated[int, Field(ge=1, le=MAX_SETTLEMENT_DEPTH)] = (
        _DEFAULT_POLICY.settlement_depth
    )
    parent_wait_blocks: Annotated[int, Field(ge=MIN_PARENT_WAIT_BLOCKS, le=MAX_UINT32)] = (
        _DEFAULT_POLICY.parent_wait_blocks
    )
    max_freeze_blocks: Annotated[int, Field(ge=MIN_MAX_FREEZE_BLOCKS, le=MAX_UINT32)] = (
        _DEFAULT_POLICY.max_freeze_blocks
    )
    freeze_ttl_blocks: Annotated[int, Field(ge=1, le=MAX_UINT32)] = (
        _DEFAULT_POLICY.freeze_ttl_blocks
    )
    proposal_lifetime_seconds: Annotated[int, Field(ge=1, le=MAX_PROPOSAL_LIFETIME_SECONDS)] = (
        _DEFAULT_POLICY.proposal_lifetime_seconds
    )

    @model_validator(mode="after")
    def _check_agreement_invariants(self) -> Self:
        """Reject a policy no counterparty could ever agree with.

        Both rules are :class:`~jmswap.buyout_terms.BuyoutTerms`' own economic
        checks, restated on the numbers this node would put on the wire.
        """
        if "cltv_limit" not in self.model_fields_set:
            cltv_limit = min(
                _DEFAULT_POLICY.cltv_limit,
                self.csv_delay - self.buyer_settlement_depth - SPLIT_SAFETY_MARGIN_BLOCKS,
            )
            if cltv_limit < 1:
                required_csv = self.buyer_settlement_depth + 1 + SPLIT_SAFETY_MARGIN_BLOCKS
                raise _invalid(
                    "csv_delay_too_short",
                    "csv_delay must cover buyer_settlement_depth, cltv_limit and the "
                    f"split safety margin (at least {required_csv})",
                )
            object.__setattr__(self, "cltv_limit", cltv_limit)
        required_csv = self.buyer_settlement_depth + self.cltv_limit + SPLIT_SAFETY_MARGIN_BLOCKS
        if self.csv_delay < required_csv:
            raise _invalid(
                "csv_delay_too_short",
                "csv_delay must cover buyer_settlement_depth, cltv_limit and the "
                f"split safety margin (at least {required_csv})",
            )
        if self.settlement_depth > self.buyer_settlement_depth:
            raise _invalid(
                "settlement_depth_too_deep",
                "settlement_depth must not exceed buyer_settlement_depth",
            )
        return self


class BuyoutSettlementOverrides(_ConfigModel):
    """``[buyout.settlement]``: the payment and chain-fee budget of settlement.

    Defaults are :class:`~jmswap.buyout_settlement.SettlementPolicy`'s own.
    """

    payment_fee_limit_sat: Satoshis = _DEFAULT_SETTLEMENT.payment_fee_limit_sat
    payment_timeout_seconds: Annotated[int, Field(ge=1, le=MAX_UINT32)] = (
        _DEFAULT_SETTLEMENT.payment_timeout_seconds
    )
    invoice_expiry_seconds: Annotated[int, Field(ge=1, le=MAX_UINT32)] = (
        _DEFAULT_SETTLEMENT.invoice_expiry_seconds
    )
    max_chain_fee_sat: Annotated[int, Field(ge=1, le=MAX_MONEY)] = (
        _DEFAULT_SETTLEMENT.max_chain_fee_sat
    )
    bump_after_blocks: Annotated[int, Field(ge=1, le=MAX_UINT32)] = (
        _DEFAULT_SETTLEMENT.bump_after_blocks
    )

    @model_validator(mode="after")
    def _check_payment_window(self) -> Self:
        """An invoice must outlive the payment attempt it is created for.

        A buyer refuses an invoice whose remaining life is not longer than its
        own payment timeout, so an operator whose two sides are configured from
        one file would otherwise issue invoices it would itself reject.
        """
        if self.invoice_expiry_seconds <= self.payment_timeout_seconds:
            raise _invalid(
                "invoice_expiry_too_short",
                "invoice_expiry_seconds must be greater than payment_timeout_seconds",
            )
        return self


class BuyoutSettings(_ConfigModel):
    """The validated ``[buyout]`` table.

    Every field that the service needs to run is optional at the model level and
    required once ``enabled`` is true, which is what lets a disabled
    configuration be a single key. When ``enabled`` is true, all of ``network``,
    ``journal``, the five LND and Bitcoin Core connection fields, the two RPC
    credentials, ``payout_address``, ``mixdepth`` and a non-empty
    ``allowed_peers`` are present and mutually consistent.

    ``wallet_fingerprint`` is optional here on purpose: a counterparty-only
    deployment never touches a JoinMarket wallet. The buyer integration that
    does will require it separately.
    """

    enabled: bool = False
    automatic_force_close: bool = False
    network: BuyoutNetwork | None = None
    journal: Path | None = None
    lnd_endpoint: str | None = None
    lnd_identity: CompressedPubKey | None = None
    lnd_tls_cert: Path | None = None
    lnd_peer_macaroon: Path | None = None
    lnd_escrow_macaroon: Path | None = None
    bitcoin_rpc_url: AnyHttpUrl | None = None
    bitcoin_rpc_user: str | None = None
    bitcoin_rpc_password: SecretStr | None = None
    allowed_peers: tuple[CompressedPubKey, ...] = ()
    payment_route_hints: Annotated[
        tuple[
            Annotated[
                tuple[PaymentRouteHop, ...],
                Field(min_length=1, max_length=MAX_ROUTE_HINT_HOPS),
            ],
            ...,
        ],
        Field(max_length=MAX_ROUTE_HINT_PATHS, repr=False),
    ] = ()
    """Route hints this node pays buyout invoices through, as arrays of hops.

    Payer-only and optional: an empty value (the default) pays exactly as
    before. Each hint is an array of ``lnrpc.HopHint`` tables, written inline
    because TOML has no array-of-array-of-tables syntax::

        payment_route_hints = [
          [ { node_id = "02...", chan_id = 1, fee_base_msat = 0,
              fee_proportional_millionths = 0, cltv_expiry_delta = 40 } ],
        ]

    It is excluded from the model repr: a hint names channels of this operator's
    node and is not something a log of the settings should carry.
    """
    payout_address: str | None = None
    mixdepth: Annotated[int, Field(ge=0)] | None = None
    wallet_fingerprint: WalletFingerprint | None = None
    poll_interval_seconds: Annotated[float, Field(gt=0, allow_inf_nan=False)] = 5.0
    policy: BuyoutSigningOverrides = BuyoutSigningOverrides()
    settlement: BuyoutSettlementOverrides = BuyoutSettlementOverrides()

    @model_validator(mode="before")
    @classmethod
    def _prepare(cls, data: Any, info: ValidationInfo) -> Any:
        """Turn TOML scalars into the types the strict model expects.

        Path-valued keys become :class:`~pathlib.Path`, resolved against the
        directory of the configuration file when the loader supplied it, so a
        relative path means "next to my config" and not "next to whatever
        directory the service happened to start in". No path is read, stat-ed or
        created here.
        """
        if not isinstance(data, dict):
            return data
        context = info.context if isinstance(info.context, dict) else {}
        base = context.get("config_dir")
        prepared = dict(data)
        for name in _PATH_FIELDS:
            value = prepared.get(name)
            if isinstance(value, str):
                candidate = Path(value).expanduser()
                if base is not None and not candidate.is_absolute():
                    candidate = Path(base) / candidate
                prepared[name] = candidate
        peers = prepared.get("allowed_peers")
        if isinstance(peers, list):
            prepared["allowed_peers"] = tuple(peers)
        hints = prepared.get("payment_route_hints")
        if isinstance(hints, list):
            # Only the TOML arrays become tuples; the hop tables keep their own
            # scalars, so a hop written with the wrong type stays a strict
            # validation error instead of being coerced into a valid hint.
            prepared["payment_route_hints"] = tuple(
                tuple(path) if isinstance(path, list) else path for path in hints
            )
        return prepared

    @model_validator(mode="after")
    def _check_runtime_requirements(self) -> Self:
        self._check_endpoint()
        self._check_rpc_url()
        self._check_peers()
        if not self.enabled:
            return self
        missing = [name for name in _REQUIRED_WHEN_ENABLED if getattr(self, name) is None]
        if missing:
            raise _invalid(
                "buyout_setting_required",
                f"an enabled buyout service requires {', '.join(sorted(missing))}",
            )
        if not self.allowed_peers:
            raise _invalid(
                "allowed_peers_required",
                "an enabled buyout service requires at least one allowed peer",
            )
        self._check_payout_address()
        return self

    def _check_endpoint(self) -> None:
        endpoint = self.lnd_endpoint
        if endpoint is None:
            return
        match = _ENDPOINT_SHAPE.fullmatch(endpoint)
        if match is None or not 0 < int(match.group(1)) <= MAX_PORT:
            raise _invalid(
                "lnd_endpoint_invalid",
                "lnd_endpoint must be a host:port gRPC target with no scheme or path",
            )

    def _check_rpc_url(self) -> None:
        url = self.bitcoin_rpc_url
        if url is None:
            return
        if url.username is not None or url.password is not None:
            # The message must not quote the URL: it holds the credential.
            raise _invalid(
                "bitcoin_rpc_url_has_credentials",
                "bitcoin_rpc_url must not embed credentials; use bitcoin_rpc_user "
                "and bitcoin_rpc_password",
            )

    def _check_peers(self) -> None:
        if len(set(self.allowed_peers)) != len(self.allowed_peers):
            raise _invalid("allowed_peers_duplicate", "allowed_peers contains a duplicate identity")
        if self.lnd_identity is not None and self.lnd_identity in self.allowed_peers:
            raise _invalid(
                "allowed_peers_contains_self",
                "allowed_peers must not contain this node's own lnd_identity",
            )

    def _check_payout_address(self) -> None:
        try:
            self.payout_script()
        except BuyoutConfigError as exc:
            raise _invalid("payout_address_invalid", str(exc)) from None

    def payout_script(self) -> str:
        """The scriptPubKey of ``payout_address`` as lowercase hex.

        This is the form the signing protocol puts on the wire
        (``split_script_B`` / ``split_script_C``), and deriving it here is what
        binds the address to the configured network: an address for another
        network, or one that is not a P2TR output, cannot produce a script.
        """
        if self.payout_address is None or self.network is None:
            raise BuyoutConfigError("payout_address and network are not configured")
        try:
            script = address_to_scriptpubkey_for_network(self.payout_address, self.network)
        except ValueError:
            raise BuyoutConfigError(
                f"payout_address is not a valid {self.network} address"
            ) from None
        if len(script) != _P2TR_SCRIPT_LENGTH or script[:2] != b"\x51\x20":
            raise BuyoutConfigError("payout_address must be a P2TR (witness v1) address")
        return script.hex()

    def build_signing_policy(self) -> BuyoutPolicy:
        """The :class:`~jmswap.buyout_signing.BuyoutPolicy` this file describes.

        ``network`` and ``settlement_enabled`` come from ``[buyout]``: enabling
        the service is the operator's authorization to settle the sessions it
        creates from now on, and nothing else in the file can grant or withhold
        it. Sessions already in the journal keep the authorization they were
        recorded with.
        """
        return BuyoutPolicy(
            network=self._enabled_network(),
            settlement_enabled=self.enabled,
            **self.policy.model_dump(),
        )

    def build_settlement_policy(self) -> SettlementPolicy:
        """The :class:`~jmswap.buyout_settlement.SettlementPolicy` this file describes."""
        self._enabled_network()
        return SettlementPolicy(**self.settlement.model_dump())

    def _enabled_network(self) -> BuyoutNetwork:
        if not self.enabled or self.network is None:
            raise BuyoutConfigError("the buyout service is disabled; no policy is authorized")
        return self.network


def load_buyout_settings(path: Path) -> BuyoutSettings:
    """Read and validate the ``[buyout]`` table of one explicit TOML file.

    ``path`` is the only file opened: nothing is discovered, written, created or
    connected to. A file with no ``[buyout]`` table yields the disabled
    defaults, because an absent table is not evidence that a service should run.
    Relative paths inside the table are resolved against ``path``'s directory.

    Raises :class:`BuyoutConfigError` if the file cannot be read, is not TOML,
    or its ``[buyout]`` table is not a valid configuration.
    """
    if not isinstance(path, Path):
        raise BuyoutConfigError("path must be a pathlib.Path")
    try:
        with path.open("rb") as handle:
            document = tomllib.load(handle)
    except OSError:
        raise BuyoutConfigError(f"buyout configuration file cannot be read: {path}") from None
    except tomllib.TOMLDecodeError:
        # Chaining would carry a quoted line of the file into the traceback.
        raise BuyoutConfigError(f"buyout configuration file is not valid TOML: {path}") from None

    section = document.get(CONFIG_SECTION)
    if section is None:
        return BuyoutSettings()
    if not isinstance(section, dict):
        raise BuyoutConfigError(f"[{CONFIG_SECTION}] must be a table")
    try:
        return BuyoutSettings.model_validate(section, context={"config_dir": path.parent})
    except ValidationError as exc:
        raise BuyoutConfigError(
            f"[{CONFIG_SECTION}] is not a valid configuration:\n{exc}"
        ) from None


__all__ = (
    "CONFIG_SECTION",
    "MAX_PROPOSAL_LIFETIME_SECONDS",
    "BuyoutConfigError",
    "BuyoutNetwork",
    "BuyoutSettings",
    "BuyoutSettlementOverrides",
    "BuyoutSigningOverrides",
    "load_buyout_settings",
)
