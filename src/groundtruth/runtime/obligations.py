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
    return ("_" in part or "." in part or any(c.isdigit() for c in part)
            or (len(part) > 1 and any(c.isupper() for c in part[1:])))


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
        return self._set_explicit(obligation_id, ObligationLifecycle.SATISFIED.value, evidence, turn)

    def mark_contradicted(self, obligation_id: int, evidence: str, turn: int) -> bool:
        return self._set_explicit(obligation_id, ObligationLifecycle.CONTRADICTED.value, evidence, turn)

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
            elif ob.status in (ObligationLifecycle.TESTED.value, ObligationLifecycle.SATISFIED.value) or obligation_tested(v, tested):
                status = OBL_TESTED
            elif ob.status == ObligationLifecycle.EDITED.value or touched:
                status = OBL_EDITED_UNTESTED
            else:
                status = OBL_UNADDRESSED
            out.append((v, status, touched, conf))
        return out

    def unmet(self) -> list[ObligationRecord]:
        return [
            o for o in self.obligations
            if o.status not in (ObligationLifecycle.TESTED.value, ObligationLifecycle.SATISFIED.value)
        ]

    def coverage_ratio(self) -> float:
        if not self.obligations:
            return 1.0
        done = sum(
            1 for o in self.obligations
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


# ═══════════════════════════ GT_OBLIGATIONS_V2 ══════════════════════════════
# Exercised-clause primitives (plan §5). Discipline deliberately STRICTER than
# v1 `obligation_tested` (whose ANY-token overlap is a false-pass channel: one
# common token like `Response` in any test output marks a whole clause tested):
#   - credit pool is the TEST-EVIDENCE tokens only (test-runner command +
#     result-bearing output). Views/edits never credit exercise.
#   - a COMPOUND subject symbol (snake/dotted/digit/CamelCase-internal) credits
#     on exact token or case-insensitive substring containment
#     (`filterMap` inside `test_filterMap` credits);
#   - a NON-compound symbol credits ONLY on case-sensitive exact token match
#     (`maybe` is not credited by "Maybe" in output);
#   - CamelCase SPLITTING never credits (`filter`+`map` != `filterMap`).
# "Unexercised clause" is the requirements-coverage analogue of a surviving
# mutant: no executed test distinguishes the clause's implementation from its
# absence.

CLAUSE_EXERCISED = "exercised"
CLAUSE_EDITED_UNEXERCISED = "edited_unexercised"
CLAUSE_UNADDRESSED = "unaddressed"
CLAUSE_UNVERIFIABLE = "unverifiable"


def _credit_eligible_symbols(view) -> set[str]:
    """Subject symbols a deterministic exercise check can be RIGHT about."""
    subj = set(getattr(view, "subject_symbols", set()) or set())
    if not subj:
        return set()
    return {s for s in subj if len(s) >= 3}


def clause_exercised(view, tested_tokens: set[str]) -> bool:
    """True iff test evidence names one of the clause's subject symbols,
    under the strict credit discipline above."""
    tested = set(tested_tokens or ())
    if not tested:
        return False
    lowered = [t.lower() for t in tested]
    for sym in _credit_eligible_symbols(view):
        if _is_compound_symbol(sym):
            sl = sym.lower()
            if any(sl == t or sl in t for t in lowered):
                return True
        else:
            if sym in tested:  # case-sensitive exact token only
                return True
    return False


def exercise_statuses(views, edited_tokens, tested_tokens):
    """[(view, status)] with the v2 tiers; `unverifiable` = zero
    credit-eligible subject symbols (reported honestly, never guessed)."""
    edited = set(edited_tokens or ())
    tested = set(tested_tokens or ())
    out = []
    for v in views:
        if not _credit_eligible_symbols(v):
            out.append((v, CLAUSE_UNVERIFIABLE))
            continue
        if clause_exercised(v, tested):
            out.append((v, CLAUSE_EXERCISED))
            continue
        touched, _conf = overlap(v, edited)
        out.append((v, CLAUSE_EDITED_UNEXERCISED if touched else CLAUSE_UNADDRESSED))
    return out


def coverage_summary(statuses) -> dict:
    counts = {
        CLAUSE_EXERCISED: 0,
        CLAUSE_EDITED_UNEXERCISED: 0,
        CLAUSE_UNADDRESSED: 0,
        CLAUSE_UNVERIFIABLE: 0,
    }
    for _v, s in statuses:
        counts[s] = counts.get(s, 0) + 1
    checkable = sum(
        counts[k]
        for k in (CLAUSE_EXERCISED, CLAUSE_EDITED_UNEXERCISED, CLAUSE_UNADDRESSED)
    )
    return {
        "coverage_version": 2,
        "n_clauses": len(list(statuses)),
        "n_exercised": counts[CLAUSE_EXERCISED],
        "n_edited_unexercised": counts[CLAUSE_EDITED_UNEXERCISED],
        "n_unaddressed": counts[CLAUSE_UNADDRESSED],
        "n_unverifiable": counts[CLAUSE_UNVERIFIABLE],
        "coverage_exercised": (
            counts[CLAUSE_EXERCISED] / checkable if checkable else 1.0
        ),
    }


def render_unexercised_block(
    statuses,
    max_listed: int = 6,
    artifact_rel: str = ".groundtruth/obligations.md",
    leak_screen=None,
) -> str:
    """Violations-only submit-time block. SILENT ("") when every checkable
    clause is exercised and nothing is unverifiable — correct-or-quiet.

    ``leak_screen(text) -> bool`` (True = leaky) is applied per row; a leaky
    row drops WHOLE (fail closed). All-dropped -> silent.
    """
    violations = [
        (v, s) for v, s in statuses
        if s in (CLAUSE_EDITED_UNEXERCISED, CLAUSE_UNADDRESSED)
    ]
    n_unverifiable = sum(1 for _v, s in statuses if s == CLAUSE_UNVERIFIABLE)
    if not violations and not n_unverifiable:
        return ""
    lines: list[str] = []
    shown = 0
    for v, s in violations:
        if shown >= max_listed:
            break
        quote = (v.verbatim or "")[:160]
        syms = sorted(_credit_eligible_symbols(v))[:2]
        symtxt = "/".join(f"`{x}`" for x in syms) or "its subject"
        if s == CLAUSE_EDITED_UNEXERCISED:
            mark = f"[edited, never exercised — no test/run output has mentioned {symtxt}]"
        else:
            mark = "[not addressed — no edit or execution evidence]"
        row = f'{mark} "{quote}"'
        if leak_screen is not None and leak_screen(row):
            continue  # fail closed: leaky row drops whole
        lines.append(row)
        shown += 1
    hidden = len(violations) - shown
    if hidden > 0 and lines:
        lines.append(f"(+{hidden} more unexercised requirement(s))")
    if n_unverifiable:
        lines.append(
            f"{n_unverifiable} requirement(s) could not be auto-checked "
            f"(no code identifier) — re-read {artifact_rel} and verify each."
        )
    if not lines:
        return ""
    header = (
        "GT: requirements from the issue with NO execution evidence — "
        "each was never exercised by any test/command you ran:"
    )
    footer = (
        "Run one targeted test per requirement before submitting; an "
        "unexercised requirement is indistinguishable from an unimplemented one."
    )
    return (
        '\n<gt-nudge reason="unexercised_clauses">\n'
        + "\n".join([header, *lines, footer])
        + "\n</gt-nudge>"
    )


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
        line = f"{mark} \"{quote}\""
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
        "submit.")
    return f'\n<gt-nudge reason="test_evidence_gap" h="{h}">\n' + "\n".join(lines) + "\n</gt-nudge>"

