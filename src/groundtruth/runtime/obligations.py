"""Product-owned issue obligation lifecycle."""

from __future__ import annotations

import ast
import hashlib
import re
from dataclasses import dataclass, field
from enum import Enum


OBLIGATION_VERSION = "gt.runtime.obligations.v1"
OBLIGATION_TRUTH_VERSION = "gt.runtime.obligation_truth.v1"


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


# Checked behavioral execution proof.  This is product truth derived solely from the
# command/result observation available to the live seam and replay child.  Assistant prose is
# deliberately absent: acknowledgment is a later SS receipt grade, not execution evidence.
_PROOF_TOKEN_RE = re.compile(r"\b[A-Za-z_]\w*(?:[.:-][A-Za-z0-9_]+)*")
_PROOF_GENERIC = frozenset(
    {
        "about",
        "addressed",
        "after",
        "all",
        "and",
        "before",
        "behavior",
        "edited",
        "exercise",
        "expect",
        "expected",
        "feature",
        "for",
        "given",
        "handled",
        "is",
        "issue",
        "must",
        "none",
        "possible",
        "requirement",
        "should",
        "submit",
        "support",
        "tested",
        "the",
        "think",
        "this",
        "to",
        "untested",
        "verify",
        "when",
        "with",
        "would",
    }
)
_STATUS_SUBJECT_RE = re.compile(
    r'\[(?:edited,\s*untested|not\s+addressed(?:\s+[^\]]*)?)\]\s*"([^"]+)"',
    re.IGNORECASE,
)
_RESURFACE_BULLET_RE = re.compile(r"^\s*-\s+(.+?)\s*$", re.MULTILINE)
_EXCEPTION_SUBJECT_RE = re.compile(r"\b([A-Z][A-Za-z0-9_]*(?:Error|Exception))\b")


@dataclass(frozen=True)
class BehavioralProof:
    """Immutable proof that one obligation subject was exercised at ``turn``."""

    subjects: frozenset[str]
    turn: int
    kind: str


@dataclass(frozen=True)
class ExerciseEvidence:
    """Immutable evidence that a subject participated in an execution.

    Exercise is success-neutral.  A failed or inconclusive related command is
    useful coverage evidence, but it is never a ``BehavioralProof``.
    """

    subjects: frozenset[str]
    turn: int
    source: str
    returncode: int


class ObligationTruthState(str, Enum):
    """Truth states shared by live delivery and aggregate metrics."""

    UNEXERCISED = "unexercised"
    EXERCISED_UNPROVEN = "exercised_unproven"
    PROVEN = "proven"
    UNVERIFIABLE = "unverifiable"


@dataclass(frozen=True)
class ObligationTruth:
    view: object
    state: ObligationTruthState
    edited: bool
    exercise: ExerciseEvidence | None = None
    proof: BehavioralProof | None = None


class BehavioralProofState:
    """Episode-local, obligation-scoped proof ledger.

    Freshness is queried rather than mutating old proof: callers pass the last relevant edit turn
    to ``covers``.  This keeps the recorded observation immutable and makes stale proof explicit.
    """

    def __init__(self) -> None:
        self._proofs: list[BehavioralProof] = []

    def record(self, proof: BehavioralProof) -> None:
        if not isinstance(proof, BehavioralProof):
            raise TypeError("proof must be a BehavioralProof")
        if proof not in self._proofs:
            self._proofs.append(proof)

    def covers(self, subjects, *, after_turn: int = -1) -> bool:
        key = _normalize_subjects(subjects)
        if not key:
            return False
        return any(proof.subjects == key and proof.turn > after_turn for proof in self._proofs)

    def snapshot(self) -> tuple[BehavioralProof, ...]:
        return tuple(self._proofs)


def _normalize_subjects(subjects) -> frozenset[str]:
    return frozenset(
        str(subject).strip().lower() for subject in (subjects or ()) if str(subject).strip()
    )


def obligation_subject_terms(subject: str) -> frozenset[str]:
    """Canonical subject terms shared by product delivery and offline grading."""
    tokens = {
        token.lower()
        for token in _PROOF_TOKEN_RE.findall(subject or "")
        if len(token) >= 4 or (len(token) >= 2 and any(char.isdigit() for char in token))
    }
    return frozenset(token for token in tokens if token not in _PROOF_GENERIC)


def rendered_obligation_subject_groups(payload: str) -> tuple[frozenset[str], ...]:
    """Extract only model-facing obligation subjects, never tag/control metadata."""
    subjects = _STATUS_SUBJECT_RE.findall(payload or "")
    if subjects:
        # Each rendered status row is an independent obligation.  No neighboring proposal,
        # comparison, or exception row is allowed to erase or narrow another row.
        pass
    else:
        # ``obligation.resurface`` renders the same issue obligations as native bullets rather
        # than status rows.  Parse only list-item lines, never the surrounding control prose.
        subjects = _RESURFACE_BULLET_RE.findall(payload or "")
    groups = tuple(obligation_subject_terms(subject) for subject in subjects)
    return tuple(group for group in groups if group)


def _terms_covered(terms: frozenset[str], command: str, output: str) -> bool:
    if not terms:
        return False
    evidence = {
        token.lower() for token in _PROOF_TOKEN_RE.findall((command or "") + "\n" + (output or ""))
    }
    for term in terms:
        if term in evidence:
            continue
        if "_" in term and any(term in token for token in evidence):
            continue
        if any(term in re.split(r"[_\d]+", token) for token in evidence if "_" in token):
            continue
        if len(term) >= 5 and any(
            token in {term + "s", term + "es", term + "ed", term + "ing"} for token in evidence
        ):
            continue
        return False
    return True


def _subject_bound_receivers(command: str, subjects: frozenset[str]) -> dict[str, set[str]]:
    """Receivers constructed from a subject-named constructor/argument expression.

    This covers the common product shape ``matcher = IgnoreMatcher(conanignore_path)`` followed
    by checked ``matcher.matches(...)`` calls.  The relationship comes from assignment data flow,
    never from a subject word printed in an output label.
    """
    bound = {subject: set() for subject in subjects}
    for line in (command or "").splitlines():
        assignment = re.match(r"^\s*([A-Za-z_]\w*)\s*=\s*(.+?)\s*$", line)
        if not assignment:
            continue
        receiver, expression = assignment.groups()
        if not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*\([^{}]*\)", expression):
            continue
        identifiers = {token.lower() for token in _PROOF_TOKEN_RE.findall(expression)}
        for subject in subjects:
            if len(subject) >= 4 and any(subject in identifier for identifier in identifiers):
                bound[subject].add(receiver)
    return bound


def _has_direct_checked_calls(command: str, output: str, subjects: frozenset[str]) -> bool:
    checked: list[tuple[str, str]] = []
    for line in (command or "").splitlines():
        marker = re.search(r"#\s*(True|False)\b", line)
        if marker:
            checked.append((line[: marker.start()], marker.group(1)))
    expected = [value for _expression, value in checked]
    observed = re.findall(r":\s*(True|False)\s*$", output or "", re.MULTILINE)
    direct_calls: list[str] = []
    for expression, _expected in checked:
        fields = re.findall(r"\{([^{}]+)\}", expression)
        calls = [
            field
            for field in fields
            if re.fullmatch(r"\s*[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*\([^{}]*\)\s*", field)
        ]
        if not calls:
            return False
        direct_calls.extend(calls)
    bound_receivers = _subject_bound_receivers(command, subjects)
    related_subjects: set[str] = set()
    for subject in subjects:
        if _terms_covered(frozenset({subject}), "\n".join(direct_calls), ""):
            related_subjects.add(subject)
            continue
        for call in direct_calls:
            receiver = call.strip().split(".", 1)[0]
            if receiver in bound_receivers[subject]:
                related_subjects.add(subject)
                break
    return bool(
        len(expected) >= 2
        and observed == expected
        and direct_calls
        and related_subjects == set(subjects)
    )


def _has_checked_result_matrix(command: str, output: str, subjects: frozenset[str]) -> bool:
    assigned_from_call = set(
        re.findall(
            r"^[ \t]+([A-Za-z_]\w*)\s*=\s*[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+\([^\n]*\)\s*$",
            command or "",
            re.MULTILINE,
        )
    )
    printed_values: set[str] = set()
    print_lines: list[str] = []
    for line in (command or "").splitlines():
        if re.search(r"\bprint\s*\(", line):
            printed_values.update(re.findall(r"\{\s*([A-Za-z_]\w*)[^{}]*\}", line))
            print_lines.append(line)
    observed_values = re.findall(r"^.+\s+->\s+(.+)$", output or "", re.MULTILINE)
    live_matrix_vars: set[str] = set()
    for variable in assigned_from_call & printed_values:
        assignments = re.findall(
            rf"^\s*{re.escape(variable)}\s*(?:[+\-*/%&|^]?=|:=)", command or "", re.MULTILINE
        )
        sole_rhs = any(
            re.search(
                rf"->\s*\{{\s*{re.escape(variable)}(?:![rsa])?(?::[^{{}}]+)?\s*\}}\s*['\"]\s*\)",
                line,
            )
            for line in print_lines
        )
        if len(assignments) == 1 and sole_rhs:
            live_matrix_vars.add(variable)
    return bool(
        live_matrix_vars
        and len(observed_values) >= 2
        and re.search(r"^\s*for\s+", command or "", re.MULTILINE)
        and _terms_covered(subjects, "", "\n".join(observed_values))
    )


_TEST_RUNNER_RE = re.compile(
    r"(?:^|\s)(?:python(?:3)?\s+-m\s+)?pytest\b|(?:^|\s)go\s+test\b"
    r"|(?:^|\s)cargo\s+test\b|(?:^|\s)(?:npm|pnpm|yarn)\s+(?:run\s+)?test\b",
    re.IGNORECASE,
)
_FORMAL_GREEN_RE = re.compile(r"\b\d+\s+passed\b", re.IGNORECASE)
_FORMAL_RED_RE = re.compile(
    r"\b(?:\d+\s+failed|\d+\s+errors?|failures?|traceback)\b", re.IGNORECASE
)


def _has_subject_bound_test_runner(command: str, output: str, subjects: frozenset[str]) -> bool:
    """A clean formal green bound to the rendered requirement, never any-green."""
    if not _TEST_RUNNER_RE.search(command or ""):
        return False
    if not _FORMAL_GREEN_RE.search(output or "") or _FORMAL_RED_RE.search(output or ""):
        return False
    return _terms_covered(subjects, command, output)


def _python_heredoc_body(command: str) -> str | None:
    lines = (command or "").splitlines()
    if len(lines) < 3 or "<<" not in lines[0]:
        return None
    marker_match = re.search(r"<<\s*['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?", lines[0])
    if not marker_match:
        return None
    marker = marker_match.group(1)
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == marker)
    except StopIteration:
        return None
    return "\n".join(lines[1:end])


def _call_name(call: ast.Call) -> str:
    parts: list[str] = []
    node = call.func
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _non_print_calls(nodes: list[ast.stmt]) -> set[str]:
    names: set[str] = set()
    for statement in nodes:
        for node in ast.walk(statement):
            if isinstance(node, ast.Call):
                name = _call_name(node)
                if name and name != "print" and not name.startswith("type"):
                    names.add(name)
    return names


def _print_prefixes(nodes: list[ast.stmt]) -> set[str]:
    prefixes: set[str] = set()
    for statement in nodes:
        for node in ast.walk(statement):
            if not isinstance(node, ast.Call) or _call_name(node) != "print" or not node.args:
                continue
            value = node.args[0]
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                prefixes.add(value.value)
            elif isinstance(value, ast.JoinedStr) and value.values:
                first = value.values[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    prefixes.add(first.value)
    return {prefix for prefix in prefixes if prefix}


def _handler_name(handler: ast.ExceptHandler) -> str:
    node = handler.type
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parts: list[str] = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if isinstance(node, ast.Name):
            parts.append(node.id)
        return ".".join(reversed(parts))
    return ""


def _has_expected_exception_control(command: str, output: str, subjects: frozenset[str]) -> bool:
    """Recognize an observed expected-exception probe plus same-call success control.

    The proof is branch-bound: the expected handler's print must be observed, every
    no-exception/other-exception print from that probe must be absent, and a second try of the
    same callable must observe its normal branch with all exception prints absent.
    """
    body = _python_heredoc_body(command)
    if not body:
        return False
    try:
        tree = ast.parse(body)
    except (SyntaxError, ValueError):
        return False
    tries = [node for node in tree.body if isinstance(node, ast.Try)]
    condition_bound = _terms_covered(subjects, command, output)
    expected: list[tuple[set[str], set[str], set[str]]] = []
    controls: list[tuple[set[str], set[str], set[str]]] = []
    for node in tries:
        calls = _non_print_calls(node.body)
        normal = _print_prefixes(node.body + node.orelse)
        matching_handlers = [
            handler for handler in node.handlers if _handler_name(handler).lower() in subjects
        ]
        if not matching_handlers and condition_bound:
            # A requirement may name the failing input/operation rather than the
            # exception class.  Admit only typed exception handlers and only when
            # at least two requirement terms are present in the executed probe.
            matching_handlers = [
                handler
                for handler in node.handlers
                if _EXCEPTION_SUBJECT_RE.fullmatch(_handler_name(handler).rsplit(".", 1)[-1])
            ]
        handler_prints = {
            prefix for handler in matching_handlers for prefix in _print_prefixes(handler.body)
        }
        all_exception_prints = {
            prefix for handler in node.handlers for prefix in _print_prefixes(handler.body)
        }
        if calls and matching_handlers and handler_prints:
            expected.append(
                (calls, handler_prints, normal | (all_exception_prints - handler_prints))
            )
        elif calls and normal:
            controls.append((calls, normal, all_exception_prints))
    for expected_calls, success, failure in expected:
        if not any(prefix in output for prefix in success):
            continue
        if any(prefix in output for prefix in failure):
            continue
        for control_calls, control_success, control_failure in controls:
            if not (expected_calls & control_calls):
                continue
            if any(prefix in output for prefix in control_success) and not any(
                prefix in output for prefix in control_failure
            ):
                return True
    return False


def classify_checked_behavioral_proof(
    command: str, output: str, returncode, subjects, *, turn: int
) -> BehavioralProof | None:
    """Return immutable proof only for a checked, related, successful dynamic execution.

    ``returncode`` is intentionally exact: bool/string/coercible zero values are not an observed
    process rc.  ``output`` is the original tool output; callers must not synthesize PASS prose.
    """
    if type(returncode) is not int or returncode != 0:  # bool is an int subclass: reject it
        return None
    key = _normalize_subjects(subjects)
    if not key or not (command or "").strip() or not (output or "").strip():
        return None
    if _has_direct_checked_calls(command, output, key):
        return BehavioralProof(key, int(turn), "direct_checked_calls")
    if _has_checked_result_matrix(command, output, key):
        return BehavioralProof(key, int(turn), "checked_result_matrix")
    if _has_subject_bound_test_runner(command, output, key):
        return BehavioralProof(key, int(turn), "subject_bound_test_runner")
    if _has_expected_exception_control(command, output, key):
        return BehavioralProof(key, int(turn), "expected_exception_control")
    return None


def classify_exercise_evidence(
    command: str, output: str, returncode, subjects, *, turn: int
) -> ExerciseEvidence | None:
    """Return related-execution evidence without making a success claim.

    Exact integer process status is retained as observed.  Subject coverage is
    required across the original command/result bytes; empty, prose-only, or
    unrelated observations remain quiet.
    """
    if type(returncode) is not int:  # bool is not an observed process status
        return None
    key = _normalize_subjects(subjects)
    if not key or not (command or "").strip():
        return None
    if not _terms_covered(key, command, output):
        return None
    return ExerciseEvidence(key, int(turn), "related_execution", returncode)


@dataclass
class ObligationRecord:
    id: int
    verbatim: str
    symbols: frozenset[str]
    status: str = ObligationLifecycle.UNSEEN.value
    evidence: list[str] = field(default_factory=list)
    last_turn: int = 0
    certainty: str = "missing"
    last_tested_turn: int = 0  # O-2: turn this obligation last reached TESTED (freshness)


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
    # O-3 (2026-07-10, D-2 family): 16 hex — the resurface change-latch keys on this
    # hash, so an 8-hex collision was a MISSED resurface. Widen to match the other
    # correctness-bearing dedup hashes (block/lane/gate all [:16]).
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


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
            # O-2: record the turn each obligation is TESTED (even when the status did
            # not otherwise change) so apply_edit_freshness can tell a later edit apart
            # from a pre-test one.
            if status == OBL_TESTED:
                ob.last_tested_turn = turn
        return transitions

    def apply_edit_freshness(self, edited_symbols_this_turn, turn: int):
        """O-2 (2026-07-10) — FRESHNESS LAW (doctrine: a stale PASS -> UNKNOWN). A
        TESTED obligation whose symbol is edited AGAIN, at a turn STRICTLY LATER than it
        was last tested, is no longer verified: the prior green result predates the new
        edit. Demote TESTED -> EDITED (needs re-testing), returning the transitions.

        ``edited_symbols_this_turn`` MUST be the symbols edited ON THIS TURN (a per-turn
        delta), never the cumulative episode set — a cumulative set would demote on the
        pre-test edit too. SATISFIED (explicit human/evidence satisfaction) is NOT
        demoted here; only the observed-test TESTED status is freshness-bound. Pure /
        deterministic; caller-gated (default-off in the live seam)."""
        syms = {str(s).strip().lower() for s in (edited_symbols_this_turn or ()) if str(s).strip()}
        if not syms:
            return []
        transitions = []
        for ob in self.obligations:
            if ob.status != ObligationLifecycle.TESTED.value:
                continue
            if turn <= ob.last_tested_turn:
                continue  # the edit is not strictly newer than the test -> still fresh
            if {s.lower() for s in ob.symbols} & syms:
                transitions.append((ob.id, ob.status, ObligationLifecycle.EDITED.value))
                ob.status = ObligationLifecycle.EDITED.value
                ob.certainty = certainty_for_status(ObligationLifecycle.EDITED.value)
                ob.last_turn = turn
                ob.evidence.append(f"turn{turn}:stale_edit_after_test")
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
                "verbatim": _truncate_at_word(o.verbatim or "", 160),
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


def obligation_truth_statuses(
    views,
    edited_tokens,
    exercise_evidence=(),
    behavioral_proofs=(),
    *,
    after_turn_by_id=None,
) -> list[ObligationTruth]:
    """Project one authoritative normative-obligation lifecycle.

    Delivery and metrics receive the same immutable rows.  Process/evidence
    regions are excluded before the denominator is formed.  Proof wins over
    exercise; exercise never promotes itself to proof.
    """
    edited = set(edited_tokens or ())
    evidence = tuple(
        item for item in (exercise_evidence or ()) if isinstance(item, ExerciseEvidence)
    )
    proofs = tuple(item for item in (behavioral_proofs or ()) if isinstance(item, BehavioralProof))
    freshness = dict(after_turn_by_id or {})
    rows: list[ObligationTruth] = []
    for view in views or ():
        if getattr(view, "region", "normative") != "normative":
            continue
        eligible_symbols = _credit_eligible_symbols(view)
        subjects = _normalize_subjects(eligible_symbols)
        after_turn = int(freshness.get(getattr(view, "idx", -1), -1))

        touched, _confidence = overlap(view, edited)
        # SUBJECT-SCOPED touch, distinct from ``touched`` above.  ``overlap`` reads
        # ``view.sym_parts``, which in production is EVERY >=3-char identifier-shaped
        # token of the clause's English sentence (``must``, ``input``, ``behavior``),
        # intersected with edit-command tokens accumulated across the whole episode.
        # As a counterweight that is near-vacuous: a clause reached PROVEN because the
        # word ``input`` appeared in some edit command.  ``touched`` still feeds the
        # v1-compatible ``ObligationTruth.edited`` field below, where its loose
        # sentence-level meaning is the intended one; only the PROVEN gate uses this.
        subject_touched = bool(subjects & _normalize_subjects(edited))

        def covers(item_subjects: frozenset[str]) -> bool:
            """Credit only evidence that names EVERY subject of this clause.

            Deliberately DIRECTIONAL, and deliberately not set equality: evidence
            NARROWER than the clause (naming ``parse_item`` for a clause about
            ``parse_item`` AND ``EmptyValue``) is a partial subject match and still
            credits nothing.

            WHAT THE WIDENING ACTUALLY BUYS (corrected 2026-07-28 — the original
            rationale here was wrong).  It does NOT repair a same-clause ``len<3``
            asymmetry: ``subjects`` is filtered by ``_credit_eligible_symbols`` while
            the evidence key is not, but the sole producer of ``subject_symbols``
            (``pretask/spec.py:593-615``) already enforces >=3 on every branch, so the
            two sets do not diverge in production.  The live channel is
            CROSS-CLAUSE POOLING: ``gt_mini_patch.py:8677-8709`` builds evidence per
            clause, appends it all to ONE flat list, and hands that list plus every
            view to this function.  Under ``==`` a clause could only be credited by
            evidence keyed to its own subject set.  Under ``<=`` a narrow clause is
            credited by a broad SIBLING clause's evidence whenever the sibling's test
            named every symbol the narrow clause is about — which is the ordinary
            shape of two clauses drawn from one issue paragraph.
            """
            return bool(subjects) and subjects <= item_subjects

        def proof_covers(item_subjects: frozenset[str]) -> bool:
            """Proof-grade credit; widened credit needs a second, independent signal.

            An exactly-matching proof keeps its prior force — no edit precondition,
            because a clause can legitimately be proven with zero edits
            (``tests/test_obligations_v2_t3_integration.py:122``).  A WIDER proof —
            one earned for a sibling clause — may only reach PROVEN when THIS
            clause's own subject symbols were edited: changed symbols AND validation
            evidence.  Without that the clause degrades to EXERCISED_UNPROVEN via
            ``covers`` — visible, honest, and never a silent promotion.

            The counterweight is ``subject_touched``, NOT ``touched``.  ``touched``
            is sentence-word-level and a real trajectory satisfies it almost always,
            so gating on it let clause A reach PROVEN on clause B's proof by virtue
            of the word ``input`` appearing in an edit command.
            """
            if not covers(item_subjects):
                return False
            return item_subjects == subjects or subject_touched

        proof = next(
            (
                item
                for item in reversed(proofs)
                if proof_covers(item.subjects) and item.turn > after_turn
            ),
            None,
        )
        exercise = next(
            (
                item
                for item in reversed(evidence)
                if covers(item.subjects) and item.turn > after_turn
            ),
            None,
        )
        if not subjects:
            state = ObligationTruthState.UNVERIFIABLE
        elif proof is not None:
            state = ObligationTruthState.PROVEN
        elif exercise is not None:
            state = ObligationTruthState.EXERCISED_UNPROVEN
        else:
            state = ObligationTruthState.UNEXERCISED
        rows.append(ObligationTruth(view, state, bool(touched), exercise, proof))
    return rows


def obligation_truth_summary(rows) -> dict:
    """Aggregate the exact lifecycle rows used by the renderer."""
    truth = tuple(row for row in (rows or ()) if isinstance(row, ObligationTruth))
    counts = {state: 0 for state in ObligationTruthState}
    for row in truth:
        counts[row.state] += 1
    total = len(truth)
    unverifiable = counts[ObligationTruthState.UNVERIFIABLE]
    verifiable = total - unverifiable
    return {
        "coverage_version": 3,
        "truth_version": OBLIGATION_TRUTH_VERSION,
        "n_normative": total,
        "n_unexercised": counts[ObligationTruthState.UNEXERCISED],
        "n_exercised_unproven": counts[ObligationTruthState.EXERCISED_UNPROVEN],
        "n_proven": counts[ObligationTruthState.PROVEN],
        "n_unverifiable": unverifiable,
        "n_verifiable": verifiable,
        "proof_coverage": (counts[ObligationTruthState.PROVEN] / verifiable if verifiable else 1.0),
    }


def _truncate_at_word(text: str, limit: int) -> str:
    """Truncate to <= ``limit`` chars at a WORD boundary, not mid-character.

    D-10 (run6 audit, cited in 5 batches): a hard ``text[:160]`` slice severed
    requirements mid-word ("unimodal d…", "first g…", "take xy as single
    parameter…"), cutting exactly the discriminating clause the model needed.
    Cut at the last whitespace within the budget and mark with a single ellipsis
    (reserving its char so the result never exceeds ``limit``). No boundary in the
    last ~25% -> fall back to a hard cut (a single long token). Pure, deterministic.
    """
    t = text or ""
    if len(t) <= limit or limit <= 1:
        return t[:limit]
    head = t[: limit - 1]  # reserve one char for the ellipsis
    cut = head.rsplit(None, 1)[0] if " " in head or "\t" in head or "\n" in head else head
    if len(cut) < (limit - 1) * 0.75:  # boundary too early -> single long token
        cut = head
    return cut.rstrip() + "…"


def render_obligation_truth_block(rows, *, max_listed: int = 6, leak_screen=None) -> str:
    """Render only unmet normative truth, distinguishing attempt from proof."""
    truth = tuple(row for row in (rows or ()) if isinstance(row, ObligationTruth))
    unmet = tuple(row for row in truth if row.state is not ObligationTruthState.PROVEN)
    if not unmet:
        return ""
    lines: list[str] = []
    for row in unmet:
        if len(lines) >= max_listed:
            break
        quote = _truncate_at_word(getattr(row.view, "verbatim", "") or "", 160)
        if row.state is ObligationTruthState.EXERCISED_UNPROVEN:
            mark = "[exercised, result unproven]"
        elif row.state is ObligationTruthState.UNVERIFIABLE:
            mark = "[could not be verified automatically]"
        else:
            mark = "[not exercised]"
        rendered = f'{mark} "{quote}"'
        if leak_screen is not None and leak_screen(rendered):
            continue
        lines.append(rendered)
    hidden = len(unmet) - len(lines)
    if hidden > 0 and lines:
        lines.append(f"(+{hidden} more unproven requirement(s))")
    if not lines:
        return ""
    return "\n".join(
        [
            "GT: normative requirement truth from observed execution:",
            *lines,
            "Exercise each requirement and require successful behavioral proof before submit.",
        ]
    )


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
        counts[k] for k in (CLAUSE_EXERCISED, CLAUSE_EDITED_UNEXERCISED, CLAUSE_UNADDRESSED)
    )
    return {
        "coverage_version": 2,
        "n_clauses": len(list(statuses)),
        "n_exercised": counts[CLAUSE_EXERCISED],
        "n_edited_unexercised": counts[CLAUSE_EDITED_UNEXERCISED],
        "n_unaddressed": counts[CLAUSE_UNADDRESSED],
        "n_unverifiable": counts[CLAUSE_UNVERIFIABLE],
        "coverage_exercised": (counts[CLAUSE_EXERCISED] / checkable if checkable else 1.0),
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
        (v, s) for v, s in statuses if s in (CLAUSE_EDITED_UNEXERCISED, CLAUSE_UNADDRESSED)
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
