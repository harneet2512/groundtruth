"""Behavior-neutral causal telemetry for evidence-producer invocations.

The production return value is authoritative.  This module only mirrors what happened:
one ``entered`` row and exactly one terminal row for every attached invocation.  Recorder
faults are swallowed, exception messages are never persisted, and no audit field is read
by evidence selection, rendering, or delivery.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping

from groundtruth.runtime.fact_registry import (
    producer_matches,
    required_event,
)

PRODUCER_AUDIT_SCHEMA = "gt.producer_invocation.v1"


def _context_value(context: object, key: str, default: Any = None) -> Any:
    if isinstance(context, Mapping):
        return context.get(key, default)
    return getattr(context, key, default)


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list, set, frozenset)):
        return ()
    return tuple(
        str(item.value if hasattr(item, "value") else item)
        for item in value
        if str(item.value if hasattr(item, "value") else item)
    )


def _emit(recorder: object, row: dict[str, Any]) -> None:
    if not callable(recorder):
        return
    try:
        recorder(dict(row))
    except Exception:
        # Telemetry may never change producer or agent behavior.
        return


@dataclass
class ProducerAudit:
    """One producer invocation and its immutable causal breadcrumbs."""

    recorder: Callable[[dict[str, Any]], None] | None
    producer: str
    evidence_types: tuple[str, ...]
    invocation_site: str
    event_type: str
    subject: str
    action_index: int
    context: object = field(default_factory=dict)
    delivered_keys: Iterable[str] = ()
    _reasons: list[dict[str, Any]] = field(default_factory=list, init=False)
    _terminal: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        self.evidence_types = tuple(
            dict.fromkeys(str(item) for item in self.evidence_types if str(item))
        )
        observation_id = str(
            _context_value(self.context, "observation_id", "") or ""
        )
        identity = {
            "schema": PRODUCER_AUDIT_SCHEMA,
            "observation_id": observation_id,
            "producer": self.producer,
            "evidence_types": self.evidence_types,
            "invocation_site": self.invocation_site,
            "event_type": self.event_type,
            "subject": self.subject,
            "action_index": int(self.action_index),
        }
        self.invocation_id = hashlib.sha256(
            json.dumps(
                identity,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        _emit(self.recorder, self._base_row("entered"))

    def _registry_checks(self) -> list[dict[str, Any]]:
        checks: list[dict[str, Any]] = []
        for evidence_type in self.evidence_types:
            wanted = required_event(evidence_type)
            checks.append(
                {
                    "evidence_type": evidence_type,
                    "producer_allowed": producer_matches(
                        evidence_type, self.producer
                    ),
                    "required_event": wanted or "",
                    "event_allowed": bool(wanted and wanted == self.event_type),
                }
            )
        return checks

    def _base_row(self, outcome: str) -> dict[str, Any]:
        required_roles = _string_tuple(
            _context_value(self.context, "required_roles", ())
        )
        present_roles = _string_tuple(
            _context_value(self.context, "roles_present_before", ())
        )
        checks = self._registry_checks()
        return {
            "schema": PRODUCER_AUDIT_SCHEMA,
            "layer": "producer.invocation",
            "outcome": outcome,
            "invocation_id": self.invocation_id,
            "producer": self.producer,
            "evidence_types": list(self.evidence_types),
            "invocation_site": self.invocation_site,
            "event_type": self.event_type,
            "subject": self.subject,
            "action_index": int(self.action_index),
            "observation_id": str(
                _context_value(self.context, "observation_id", "") or ""
            ),
            "decision_id": str(
                _context_value(self.context, "decision_id", "") or ""
            ),
            "decision_context": str(
                _context_value(self.context, "decision_context", "") or ""
            ),
            "decision_open": bool(
                _context_value(self.context, "decision_open", False)
            ),
            "required_roles": list(required_roles),
            "roles_present_before": list(present_roles),
            "required_roles_satisfied_before": bool(
                _context_value(
                    self.context,
                    "required_roles_satisfied_before",
                    bool(required_roles)
                    and set(required_roles).issubset(present_roles),
                )
            ),
            "registry_checks": checks,
            "registry_allowed": bool(checks)
            and any(
                item["producer_allowed"] and item["event_allowed"]
                for item in checks
            ),
            "returned_fact": False,
            "returned_nothing": False,
            "confidence": [],
            "authority_result": "not_evaluated",
            "dedup_result": "not_evaluated",
            "cooldown": "not_evaluated",
            "dependency_failure": [],
            "suppression_reason": [],
            "abstention_reasons": [],
            "facts": [],
        }

    def note(
        self,
        reason: str,
        *,
        category: str,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        """Attach one exact branch result without affecting the producer return."""

        item = {
            "category": str(category),
            "reason": str(reason),
            "detail": dict(detail or {}),
        }
        if item not in self._reasons:
            self._reasons.append(item)

    def _facts(self, value: object) -> list[object]:
        if value is None:
            return []
        if isinstance(value, (tuple, list)):
            return [
                item
                for item in value
                if getattr(item, "evidence_type", None)
            ]
        return [value] if getattr(value, "evidence_type", None) else []

    def finish(self, value: Any) -> Any:
        """Record the terminal producer result and return ``value`` by identity."""

        if self._terminal:
            return value
        self._terminal = True
        facts = self._facts(value)
        outcome = "returned_fact" if facts else "returned_nothing"
        if not facts and not self._reasons:
            self.note(
                "unexplained_abstention",
                category="instrumentation_gap",
            )
        row = self._base_row(outcome)
        row["returned_fact"] = bool(facts)
        row["returned_nothing"] = not facts
        row["abstention_reasons"] = list(self._reasons)
        row["dependency_failure"] = [
            item
            for item in self._reasons
            if item["category"] == "dependency_failure"
        ]
        row["suppression_reason"] = [
            item
            for item in self._reasons
            if item["category"]
            in {
                "suppression",
                "registry",
                "authority",
                "dedup",
                "cooldown",
            }
        ]
        row["confidence"] = [
            float(getattr(item, "confidence"))
            for item in facts
            if isinstance(getattr(item, "confidence", None), (int, float))
        ]
        delivered = set(self.delivered_keys or ())
        candidate_ids = [
            str(getattr(item, "dedup_key", "") or "") for item in facts
        ]
        spent = [candidate_id in delivered for candidate_id in candidate_ids]
        if not facts:
            row["dedup_result"] = "not_evaluated"
            row["cooldown"] = "not_evaluated"
        elif all(spent):
            row["dedup_result"] = "already_delivered"
            row["cooldown"] = "dedup_spent"
        elif any(spent):
            row["dedup_result"] = "mixed"
            row["cooldown"] = "partially_spent"
        else:
            row["dedup_result"] = "novel"
            row["cooldown"] = "open"

        producer_authorized = [
            producer_matches(
                str(getattr(item, "evidence_type", "") or ""),
                str(getattr(item, "producer", "") or ""),
            )
            for item in facts
        ]
        if facts and all(producer_authorized):
            row["authority_result"] = "producer_registered"
        elif facts:
            row["authority_result"] = "producer_registration_mismatch"
        row["facts"] = [
            {
                "candidate_id": candidate_id,
                "evidence_type": str(
                    getattr(item, "evidence_type", "") or ""
                ),
                "producer": str(getattr(item, "producer", "") or ""),
                "confidence": getattr(item, "confidence", None),
                "tier": str(getattr(item, "tier", "") or ""),
                "target": str(getattr(item, "target", "") or ""),
            }
            for item, candidate_id in zip(facts, candidate_ids)
        ]
        _emit(self.recorder, row)
        return value

    def fault(self, exc: BaseException) -> None:
        """Record a terminal fault without persisting exception text."""

        if self._terminal:
            return
        self._terminal = True
        row = self._base_row("fault")
        row["fault_type"] = type(exc).__name__
        _emit(self.recorder, row)


__all__ = ["PRODUCER_AUDIT_SCHEMA", "ProducerAudit"]

