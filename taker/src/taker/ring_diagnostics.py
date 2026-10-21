"""Cancellation-safe ring response progress and authenticated, private failure reports.

Reports are informational. They never satisfy a response count or authorize a
lifecycle transition. Only round-local ordinals appear in the rendered summary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from jmcore.cofunded_ring import (
    MAX_RING_CIPHERTEXT_BYTES,
    RingFailurePayload,
    RingPlanPayload,
    decode_ring_message,
    ring_hash,
    verify_payload,
)


@dataclass
class RingResponseProgress:
    expected_response: str
    round_nonce: str
    revision: int
    # Authenticated participant key, acknowledged plan (when available), NaCl box.
    participants: dict[str, tuple[str | None, RingPlanPayload | None, Any]]
    expected_count: int = 1
    responses: dict[str, dict[str, Any]] = field(default_factory=dict)
    reports: dict[str, RingFailurePayload] = field(default_factory=dict)

    def include_response(self, nick: str, encoded: str) -> bool:
        """Consume valid reports before transport counting; otherwise preserve normal handling."""
        key, plan, crypto = self.participants[nick]
        if plan is None or key is None:
            return True
        try:
            ciphertext = encoded.split()[0]
            if len(ciphertext) > MAX_RING_CIPHERTEXT_BYTES:
                return True
            payload = decode_ring_message(crypto.decrypt(ciphertext))
            if not isinstance(payload, RingFailurePayload):
                return True
            edge = plan.incoming_edge if payload.leg == "incoming" else plan.outgoing_edge
            if (
                payload.round_nonce != self.round_nonce
                or payload.revision != self.revision
                or payload.signer_key != key
                or not verify_payload(payload)
                or payload.plan_hash != ring_hash("plan", plan).hex()
                or payload.pending_channel_id != edge.pending_channel_id
            ):
                return True
        except Exception:
            # Normal response validation remains responsible for rejecting bad messages.
            return True
        # A late report during cancellation/readiness must not satisfy that
        # phase either. It remains informational, without changing its deadline.
        if self.expected_response == "ring_prepared":
            self.reports.setdefault(nick, payload)
        return False

    def summary(self) -> str:
        phase = "opening" if self.expected_response == "ring_prepared" else self.expected_response
        details: list[str] = []
        for fallback, (nick, (key, plan, _crypto)) in enumerate(self.participants.items(), 1):
            ordinal = (
                plan.cycle_keys.index(key) + 1 if plan and key in plan.cycle_keys else fallback
            )
            response = self.responses.get(nick, {})
            values = response.get("data", [])
            received = len(values) if isinstance(values, list) else int(bool(values))
            if response.get("error") or received < self.expected_count:
                detail = f"participant {ordinal} ({received}/{self.expected_count})"
                if plan:
                    incoming = plan.cycle_keys.index(plan.incoming_edge.opener_key) + 1
                    outgoing = plan.cycle_keys.index(plan.outgoing_edge.opener_key) + 1
                    detail += f", candidate edges {incoming},{outgoing} (root cause unknown)"
                details.append(detail)
            if report := self.reports.get(nick):
                assert plan is not None
                edge = plan.incoming_edge if report.leg == "incoming" else plan.outgoing_edge
                edge_ordinal = plan.cycle_keys.index(edge.opener_key) + 1
                details.append(
                    f"participant-reported failure on edge {edge_ordinal}: "
                    f"participant {ordinal}, {report.leg}, {report.reason}"
                )
        return f"channel-ring phase {phase}: missing {self.expected_response}; " + "; ".join(
            details
        )
