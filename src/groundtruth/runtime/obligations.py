"""Product-owned issue obligation lifecycle."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum


OBLIGATION_VERSION = "gt.runtime.obligations.v1"


class ObligationLifecycle(str, Enum):
    UNSEEN = "unseen"
    EDITED = "edited"
    TESTED = "tested"
    SATISFIED = "satisfied"
    CONTRADICTED = "contradicted"
    UNVERIFIED = "unverified"


OBL_TESTED = "tested"
OBL_EDITED_UNTESTED = "edited_untested"
OBL_UNADDRESSED = "unaddressed"


@dataclass
class ObligationRecord:
    id: int
    verbatim: str
    symbols: frozenset[str]
    status: str = ObligationLifecycle.UNSEEN.value
    evidence: list[str] = field(default_factory=list)
    last_turn: int = 0
    certainty: str = "missing"


def _is_compound_symbol(part: str) -> bool:
    return (
        "_" in part
        or "." in part
        or any(c.isdigit() for c in part)
        or (len(part) > 1 and any(c.isupper() for c in part[1:]))
    )


def obligation_tested(view, tested_tokens: set[str]) -> bool:
    sym_parts = set(getattr(view, "sym_parts", set()) or set())
    if sym_parts & tested_tokens:
        return True
    compound = [p for p in sym_parts if _is_compound_symbol(p)]
    return any(p in t for t in tested_tokens for p in compound)


def overlap(view, edited_tokens: set[str]) -> tuple[set[str], float]:
    sym_parts = set(getattr(view, "sym_parts", set()) or set())
    touched = sym_parts & set(edited_tokens or set())
    if not sym_parts:
        return touched, 0.0
    return touched, len(touched) / len(sym_parts)


def obligation_statuses(views, edited_tokens, tested_tokens):
    out = []
    edited = set(edited_tokens or ())
    tested = set(tested_tokens or ())
    for v in views:
        touched, conf = overlap(v, edited)
        if obligation_tested(v, tested):
            status = OBL_TESTED
        elif touched:
            status = OBL_EDITED_UNTESTED
        else:
            status = OBL_UNADDRESSED
        out.append((v, status, touched, conf))
    return out


def status_vector_hash(statuses) -> str:
    body = "|".join(f"{v.idx}:{s}" for v, s, _t, _c in statuses)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:8]


def order_unmet(statuses):
    unmet = [x for x in statuses if x[1] != OBL_TESTED]
    unmet.sort(key=lambda x: (0 if x[1] == OBL_EDITED_UNTESTED else 1, x[0].idx))
    return unmet


def lifecycle_to_status(lifecycle: str) -> str:
    if lifecycle in (ObligationLifecycle.TESTED.value, ObligationLifecycle.SATISFIED.value):
        return OBL_TESTED
    if lifecycle == ObligationLifecycle.EDITED.value:
        return OBL_EDITED_UNTESTED
    return OBL_UNADDRESSED


def certainty_for_status(status: str) -> str:
    return {
        ObligationLifecycle.SATISFIED.value: "explicit_satisfaction_evidence",
        ObligationLifecycle.CONTRADICTED.value: "contradicting_evidence",
        ObligationLifecycle.TESTED.value: "observed_test_token",
        ObligationLifecycle.EDITED.value: "observed_edit_token",
        ObligationLifecycle.UNVERIFIED.value: "missing_verification",
    }.get(status, "missing")


class ObligationTracker:
    def __init__(self, views):
        self._views = list(views or [])
        self.obligations = [
            ObligationRecord(
                id=v.idx,
                verbatim=v.verbatim,
                symbols=frozenset(v.sym_parts),
                status=ObligationLifecycle.UNSEEN.value,
            )
            for v in self._views
        ]

    def update(self, edited_tokens: set[str], tested_tokens: set[str], turn: int):
        statuses = obligation_statuses(self._views, edited_tokens, tested_tokens)
        transitions = []
        by_id = {o.id: o for o in self.obligations}
        for v, status, touched, _conf in statuses:
            ob = by_id.get(v.idx)
            if ob is None:
                continue
            if status == OBL_TESTED:
                new_status = ObligationLifecycle.TESTED.value
            elif status == OBL_EDITED_UNTESTED:
                new_status = ObligationLifecycle.EDITED.value
            else:
                new_status = ob.status
            if new_status != ob.status:
                transitions.append((ob.id, ob.status, new_status))
                ob.status = new_status
                ob.certainty = certainty_for_status(new_status)
                ob.last_turn = turn
                if touched:
                    ob.evidence.append(f"turn{turn}:edited={','.join(sorted(touched)[:3])}")
                if status == OBL_TESTED:
                    ob.evidence.append(f"turn{turn}:tested")
        return transitions

    def mark_satisfied(self, obligation_id: int, evidence: str, turn: int) -> bool:
        return self._set_explicit(
            obligation_id, ObligationLifecycle.SATISFIED.value, evidence, turn
        )

    def mark_contradicted(self, obligation_id: int, evidence: str, turn: int) -> bool:
        return self._set_explicit(
            obligation_id, ObligationLifecycle.CONTRADICTED.value, evidence, turn
        )

    def _set_explicit(self, obligation_id: int, status: str, evidence: str, turn: int) -> bool:
        for ob in self.obligations:
            if ob.id == obligation_id:
                ob.status = status
                ob.certainty = certainty_for_status(status)
                ob.last_turn = turn
                if evidence:
                    ob.evidence.append(f"turn{turn}:{status}={evidence[:120]}")
                return True
        return False

    def statuses_tuple(self, edited_tokens: set[str], tested_tokens: set[str]):
        edited = set(edited_tokens or ())
        tested = set(tested_tokens or ())
        by_id = {o.id: o for o in self.obligations}
        out = []
        for v in self._views:
            ob = by_id.get(v.idx)
            if ob is None:
                continue
            touched, conf = overlap(v, edited)
            if ob.status == ObligationLifecycle.CONTRADICTED.value:
                status = OBL_EDITED_UNTESTED if touched else OBL_UNADDRESSED
            elif ob.status in (
                ObligationLifecycle.TESTED.value,
                ObligationLifecycle.SATISFIED.value,
            ) or obligation_tested(v, tested):
                status = OBL_TESTED
            elif ob.status == ObligationLifecycle.EDITED.value or touched:
                status = OBL_EDITED_UNTESTED
            else:
                status = OBL_UNADDRESSED
            out.append((v, status, touched, conf))
        return out

    def unmet(self) -> list[ObligationRecord]:
        return [
            o
            for o in self.obligations
            if o.status
            not in (ObligationLifecycle.TESTED.value, ObligationLifecycle.SATISFIED.value)
        ]

    def coverage_ratio(self) -> float:
        if not self.obligations:
            return 1.0
        done = sum(
            1
            for o in self.obligations
            if o.status in (ObligationLifecycle.TESTED.value, ObligationLifecycle.SATISFIED.value)
        )
        return done / len(self.obligations)

    def snapshot(self) -> list[dict]:
        return [
            {
                "id": o.id,
                "status": o.status,
                "status_certainty": o.certainty or certainty_for_status(o.status),
                "last_turn": o.last_turn,
                "verbatim": (o.verbatim or "")[:160],
                "evidence": list(o.evidence[-3:]),
            }
            for o in self.obligations
        ]


def render_obligation_status_block(statuses, covering=None, max_listed: int | None = None) -> str:
    unmet = order_unmet(statuses)
    if not unmet:
        return ""
    h = status_vector_hash(statuses)
    tested_n = sum(1 for _v, s, _t, _c in statuses if s == OBL_TESTED)
    lines = [
        "GT: requirement status from the issue - sensed from your own edit "
        "commands and observed test output:",
    ]
    listed = unmet if max_listed is None else unmet[:max_listed]
    for v, status, _touched, _conf in listed:
        quote = v.verbatim if len(v.verbatim) <= 160 else v.verbatim[:157] + "..."
        mark = "[edited, untested]" if status == OBL_EDITED_UNTESTED else "[not addressed]"
        line = f'{mark} "{quote}"'
        if (covering or {}).get(v.idx) and status == OBL_EDITED_UNTESTED:
            line += (
                "\n    targeted verification: graph-linked covering test "
                "available; run the narrowest relevant repo test target."
            )
        lines.append(line)
    if max_listed is not None and len(unmet) > max_listed:
        lines.append(f"(+{len(unmet) - max_listed} more unverified requirement(s))")
    if tested_n:
        lines.append(f"{tested_n} requirement(s) already show test evidence.")
    lines.append(
        "Run targeted verification for each untested requirement before "
        "concluding it is met; an unverified submission cannot be fixed after "
        "submit."
    )
    return f'\n<gt-nudge reason="test_evidence_gap" h="{h}">\n' + "\n".join(lines) + "\n</gt-nudge>"
