#!/usr/bin/env python3
"""SPEC-J3 — the timing JOIN: trajectory + delivered ledger rows -> the six exact
message indices of :class:`chronological_adjudication.Chronology`, adjudicated into
per-fact-class timing verdicts feeding the grader's ``correct_rl_adhered_time`` gate.

The pure timing authority (``groundtruth.runtime.chronological_adjudication.adjudicate``)
already exists and is fail-closed; NOTHING fed it. This module is that feed. It is
PURE / DETERMINISTIC / stdlib-only and **fail-closed by construction**: every index is an
EXACT message index or ``None`` — never a guess — and a missing join yields ``None`` so the
adjudicator returns ``UNMEASURED``.

REUSE, NOT REINVENT (the reader/writer-mismatch discipline). Every join reuses the
``consumption_ledger`` machinery already trusted by the grader:

* ``delivery_index`` — the ledger row's exact rendered bytes located in the trajectory via
  :func:`consumption_ledger._locate_seal_spans` (the SAME seal join
  ``consumption_ledger._build_v2`` uses for SS-LIVE Gate 1; ambiguous == unjoined, exactly
  as that path drops an ambiguous seal). When a row predates seals, the legacy
  ``iteration -> tool_ordinal`` mapping (the running count of ``role=='tool'`` messages,
  the mapping ``_build_v2`` uses for ``legacy_iteration``) is the documented fallback — but
  a seal-less row cannot be graded anyway (``adjudicate`` requires a 16-hex delivery seal),
  so it is ``UNMEASURED`` regardless.
* entity extraction for ``native_acquisition_index`` reuses
  :func:`consumption_ledger._block_entities` (the grader-side files+symbols extractor). The
  RUNTIME ack scanner's own extractor (``_ss_extract_paths`` / ``_ss_extract_symbols`` in
  ``artifact_deepswe.gt_mini_patch``) is NOT importable into a pure grader — importing the
  seam pulls its whole runtime with side effects — so the grader's pure equivalent is used.
* event classification for ``decision_open_index`` reuses
  ``gt_performance_metrics._parse_timeline`` (the grader's ONE source of truth for
  is_search / is_edit / is_test / viewed_file), imported lazily so this module stays cheap.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from groundtruth.runtime.chronological_adjudication import (
    EARLY,
    EXPIRED,
    LATE,
    LATE_CORRECTIVE,
    ON_TIME,
    STEP_BEHIND,
    UNMEASURED,
    WRONG_EVENT,
    Chronology,
    adjudicate,
)
from groundtruth.runtime.fact_registry import EVENTS, registration_for, required_event
from groundtruth.runtime.feature_lineage import build_lineage, lineage_to_dict

# REUSE the consumption-ledger join machinery (do NOT reinvent a second divergent join).
from consumption_ledger import (
    PHYSICAL_DELIVERY_BOUND,
    _action_kind,
    _assistant_prose,
    _block_entities,
    _emitted_commands,
    _entity_patterns,
    _locate_seal_spans,
    _named_in,
    build_consumption_ledger,
    physical_delivery_authority,
)

TIMING_JOIN_SCHEMA = "gt.chronological_timing_join.v1"

_SHA16_WIDTH = 16


def _valid_seal(value: object) -> bool:
    """True iff ``value`` is exactly 16 lowercase-hex chars (the adjudicator's precondition)."""
    return (
        isinstance(value, str)
        and len(value) == _SHA16_WIDTH
        and all(char in "0123456789abcdef" for char in value)
    )


@dataclass(frozen=True)
class ExtractedChronology:
    """One delivered row's exact chronology + its adjudicated timing, for audit."""

    ledger_row_index: int
    evidence_type: str
    fact_class: str | None
    delivery_seal: str
    actual_event: str
    chronology: Chronology
    timing_verdict: str
    unmeasured_reason: str | None
    physical_id: str | None = None
    physical_join_state: str | None = None
    # D (block-level chronology): set for a per-BLOCK adjudication carved out of a COMPOUND brief
    # row (one declared_fact_class per block). ``None`` for an ordinary whole-row adjudication.
    block_id: str | None = None
    block_candidate_id: str | None = None
    # R1: exact block identity inside the byte-proven parent compound observation. These fields
    # are populated only after the declared span/length/seal and typed lineage validate.
    block_char_span: tuple[int, int] | None = None
    block_chars_delivered: int | None = None
    parent_physical_id: str | None = None
    block_lineage_validated: bool = False


# --------------------------------------------------------------------------- #
# trajectory helpers (message-index space — the SAME space consumption_ledger joins in)
# --------------------------------------------------------------------------- #
def _messages(trajectory: Any) -> list[dict]:
    if isinstance(trajectory, dict):
        msgs = trajectory.get("messages")
        return [m for m in msgs if isinstance(m, dict)] if isinstance(msgs, list) else []
    if isinstance(trajectory, list):
        return [m for m in trajectory if isinstance(m, dict)]
    return []


def _tool_ordinal_to_index(messages: list[dict]) -> dict[int, int]:
    """``iteration -> message index`` via the running count of ``role=='tool'`` messages —
    the exact ``tool_ordinal`` mapping consumption_ledger._build_v2 uses for its legacy
    ``iteration`` join. The Nth tool message carries iteration N."""
    out: dict[int, int] = {}
    ordinal = 0
    for i, m in enumerate(messages):
        if m.get("role") == "tool":
            ordinal += 1
            out.setdefault(ordinal, i)
    return out


# B-BND (b1/b3): the SEAM is the writer/authority for what an observation IS. Each runtime
# ledger row records the coarse HOOK PHASE the seam classified for its ``iteration`` (the
# submit interception it recorded, the post_edit it recorded), and the seam's classifier
# (``gt_mini_patch._classify`` / ``_classify_action``) can DISAGREE with the grader-side
# ``_parse_timeline`` edit/submit detection. Where they disagree the SEAM wins: its recorded
# phase is a REAL event the seam actually observed, so the boundary is DERIVED from that
# recorded event (mapped to a message index via the SAME iteration -> tool_ordinal join the
# delivery uses), never synthesized. This map bridges the coarse seam phase -> the fine
# fact_registry boundary EVENTS it opens. ``submit`` is handled from the ``submit_gate`` layer
# (below), NOT event_type, because a submit interception carries event_type='' — the
# interception identity lives in the ``layer``.
_SEAM_PHASE_TO_BOUNDARIES: dict[str, tuple[str, ...]] = {
    "post_edit": ("edit_result", "first_view_edit"),
}
# b1: the ledger ``layer`` that identifies a real submit INTERCEPTION the seam recorded (the
# submit_gate evaluates every submit attempt; its row carries event_type='' so the identity is
# the layer). Each such row opens a ``submit`` boundary at its iteration.
_SUBMIT_INTERCEPTION_LAYER = "submit_gate"


def _boundary_indices(
    messages: list[dict],
    ledger_rows: list[dict] | None = None,
    tool_ordinal_index: dict[int, int] | None = None,
) -> dict[str, list[int]]:
    """Message indices of each fine observation boundary, keyed by the fact_registry EVENTS
    name. Reuses gt_performance_metrics._parse_timeline (one source of truth for command
    classification): each observation carries the RESULT of its preceding assistant action,
    so the boundary kind is read from that action's is_search / is_edit / is_test / view.

    B-BND (b1/b3): when ``ledger_rows`` + ``tool_ordinal_index`` are supplied, the boundary set
    is ADDITIVELY reconciled with the SEAM's own recorded events (the authority). A
    ``submit_gate`` ledger row opens a ``submit`` boundary at its interception (b1: a mid-run
    submit refusal has no terminal-exit boundary <= its delivery); a ``post_edit`` ledger row
    opens an ``edit_result`` boundary at the edit the seam recorded (b3: the grader's
    ``_parse_timeline`` edit detection can miss an edit the seam's ``_classify`` caught). Every
    added boundary is a REAL recorded event mapped through the SAME iteration -> tool_ordinal
    join the delivery uses, so a boundary is never synthesized and is guaranteed <= the delivery
    it answers (the delivery's own eligible window is >= that same tool_ordinal). Fail-closed: a
    row whose iteration has no tool message is skipped. Byte-identical to the old signature when
    the two optional args are omitted (the fair_probe ``_boundary_indices(messages)`` path)."""
    try:
        from gt_performance_metrics import _parse_timeline
    except Exception:  # pragma: no cover - grader always has scripts/swebench on path
        return {}
    timeline = _parse_timeline(messages)
    out: dict[str, list[int]] = {ev: [] for ev in EVENTS}
    # task_start = the first model-visible user message (the step-0 brief boundary).
    for i, m in enumerate(messages):
        if m.get("role") == "user":
            out["task_start"].append(i)
            break
    prev: dict | None = None
    last_obs_index: int | None = None
    for entry in timeline:
        role = entry.get("role")
        idx = entry.get("msg_index")
        if not isinstance(idx, int):
            prev = entry if role == "assistant" else prev
            continue
        if role == "observation":
            last_obs_index = idx
            if prev is not None and prev.get("role") == "assistant":
                if prev.get("is_search"):
                    out["search_result"].append(idx)
                    # a search observation is also the failed_search/ADD boundary candidate.
                    out["failed_search"].append(idx)
                elif prev.get("is_edit"):
                    out["edit_result"].append(idx)
                    out["first_view_edit"].append(idx)
                elif prev.get("is_test"):
                    out["test_result"].append(idx)
                elif prev.get("viewed_file"):
                    out["file_view"].append(idx)
                    out["first_view_edit"].append(idx)
            if entry.get("has_build_fail"):
                out["failure_obs"].append(idx)
        else:
            prev = entry
    # submit = the last observation boundary (the completion/submit interception window),
    # or an explicit exit message when present.
    exit_index = next(
        (i for i, m in enumerate(messages) if m.get("role") == "exit"), None
    )
    submit_index = exit_index if exit_index is not None else last_obs_index
    if submit_index is not None:
        out["submit"].append(submit_index)

    # B-BND (b1/b3): reconcile with the SEAM's recorded events (the authority). Additive only —
    # a real recorded phase mapped to a message index via the delivery's OWN iteration ->
    # tool_ordinal join; never removes a trajectory-parsed boundary, never synthesizes one.
    if ledger_rows and tool_ordinal_index:
        for row in ledger_rows:
            if not isinstance(row, dict):
                continue
            it = row.get("iteration")
            if not isinstance(it, int) or isinstance(it, bool):
                continue
            mi = tool_ordinal_index.get(it)
            if mi is None:
                continue
            # b1: every submit interception the seam recorded is a submit boundary. The
            # interception identity is the ``submit_gate`` layer (event_type=''), so a mid-run
            # refusal delivered BEFORE the terminal exit now has a boundary <= its delivery.
            if row.get("layer") == _SUBMIT_INTERCEPTION_LAYER:
                out["submit"].append(mi)
            # b3: an edit the seam's post_edit hook recorded opens an edit_result boundary even
            # when the grader's _parse_timeline edit detection missed it (seam is authority).
            for ev in _SEAM_PHASE_TO_BOUNDARIES.get(str(row.get("event_type") or ""), ()):  # noqa: E501
                out[ev].append(mi)
            # b5: a governor recovery steer is the seam's authority that a ``failure_obs``
            # occurred at its iteration — the governor fires ONLY on stuck/no-progress
            # detection, a REAL observed failure the grader-side ``_parse_timeline``
            # (has_build_fail) can MISS (it flags only explicit build-failure text). The
            # registry pins recovery's boundary to failure_obs (required_event ==
            # failure_obs), so the row's OWN recorded event opens that boundary at the same
            # iteration->tool_ordinal message the delivery uses. Registry-driven (any
            # failure_obs class inherits it — never a class-string key) and additive.
            if required_event(_row_evidence_type(row)) == "failure_obs":
                out["failure_obs"].append(mi)

    for ev in out:
        out[ev] = sorted(set(out[ev]))
    return out


# --------------------------------------------------------------------------- #
# per-row evidence identity
# --------------------------------------------------------------------------- #
def _row_evidence_type(row: dict) -> str:
    """The finest shipped evidence_type the row carries (lineage ``evidence_type`` first,
    then a ``gateway.<type>`` layer, then the bare layer). Resolution to a registration is
    the adjudicator's job — an unresolved type simply fails closed to UNMEASURED."""
    et = row.get("evidence_type")
    if isinstance(et, str) and et.strip() and registration_for(et) is not None:
        return et.strip()
    layer = str(row.get("layer") or "").strip()
    if layer.startswith("gateway."):
        candidate = layer.split(".", 1)[1]
        if registration_for(candidate) is not None:
            return candidate
    if isinstance(et, str) and et.strip():
        return et.strip()
    return layer


def _row_fact_class(evidence_type: str, row: dict) -> str | None:
    """Canonical registered fact class for the row, or ``None`` when it resolves to none."""
    reg = registration_for(evidence_type)
    if reg is not None:
        return reg.fact_class
    fc = row.get("fact_class")
    return fc if isinstance(fc, str) and registration_for(fc) is not None else None


# Coarse seam observation phase -> fine fact_registry EVENTS token. The seam writes the
# FINE registered event into ``actual_event`` for most classes (via its
# ``_EXACT_PROFILE_ACTUAL_EVENTS`` / ``build_lineage`` contract — equal to the registry
# ``required_event`` by construction), but the gateway search-family direct-emit path stamps
# the COARSE observation phase (``search`` / ``post_view`` …) instead of the registry's fine
# token. This map bridges ONLY the coarse phases that map UNAMBIGUOUSLY to exactly one fine
# EVENTS token, per the ``≈`` correspondence documented on each EVENT constant
# (``fact_registry.py`` lines 100-108). It is an EXPLICIT, closed table — never fuzzy: a token
# not listed here (or already a fine token) is returned verbatim so an unregistered/unknown
# event still fails closed downstream. The COMPOSITE boundaries (``first_view_edit`` /
# ``failed_search`` / ``failure_obs``) have NO coarse ``≈`` equivalent and are deliberately
# ABSENT — they are never a normalization target.
#
#   fact_registry.py:101  EVENT_SEARCH_RESULT = "search_result"  (≈ search)
#   fact_registry.py:102  EVENT_FILE_VIEW     = "file_view"      (≈ view)
#   fact_registry.py:103  EVENT_EDIT_RESULT   = "edit_result"    (≈ edit)
#   fact_registry.py:104  EVENT_TEST_RESULT   = "test_result"    (≈ test)
_COARSE_TO_FINE_EVENT: dict[str, str] = {
    "search": "search_result",
    "post_search": "search_result",
    "view": "file_view",
    "post_view": "file_view",
    "edit": "edit_result",
    "post_edit": "edit_result",
    "test": "test_result",
    "post_test": "test_result",
}


def _normalize_actual_event(event: str) -> str:
    """Bridge a COARSE seam observation phase to its UNAMBIGUOUS fine fact_registry event.

    Consults only the explicit, closed :data:`_COARSE_TO_FINE_EVENT` table; a token already
    fine (or genuinely unknown) is returned VERBATIM so the fail-closed vocabulary gate in
    ``chronological_adjudication._timing`` (``actual_event not in EVENTS`` / the non-reactive
    ``actual_event != required_event`` check) still rejects it — normalization never coerces an
    unregistered token into the vocabulary."""
    return _COARSE_TO_FINE_EVENT.get(event, event)


def _row_actual_event(row: dict) -> str:
    """The row's fine observation event.

    The seam writes TWO fields that can DIVERGE: the coarse HOOK PHASE in ``event_type``
    (``post_edit`` / ``post_view`` / ``search`` — the delivery hook that carried the fact) and
    the FINE registered event in ``actual_event`` (built from the seam's
    ``_EXACT_PROFILE_ACTUAL_EVENTS`` + ``build_lineage`` contract in
    ``artifact_deepswe.gt_mini_patch``, equal to the fact's registry ``required_event`` by
    construction). The adjudicator grades against the FINE registry vocabulary, so prefer the
    fine ``actual_event`` — normalized through :func:`_normalize_actual_event` for the gateway
    search-family coarse drift (``search`` -> ``search_result``). Fall back to ``event_type``
    ONLY when ``actual_event`` is absent/empty; that path is left UNNORMALIZED (a legacy /
    unregistered row that fails closed regardless of its event label, preserved byte-for-byte)."""
    ev = row.get("actual_event")
    if isinstance(ev, str) and ev.strip():
        return _normalize_actual_event(ev.strip())
    ev2 = row.get("event_type")
    return ev2.strip() if isinstance(ev2, str) else ""


# --------------------------------------------------------------------------- #
# the six index derivations
# --------------------------------------------------------------------------- #
def _delivery_entity_patterns(payload: str, file_path: str) -> list:
    """Word-boundary matchers for every entity the delivered fact names — the ONE entity
    model shared by native-acquisition, decision-commit, and action detection. Entities are
    extracted from the delivered payload bytes with consumption_ledger._block_entities
    (files+symbols); when the payload names none, fall back to the row's file_path (the
    existing J3 fallback). Reuses consumption_ledger._entity_patterns so there is never a
    second, divergent matcher."""
    files, symbols = _block_entities(payload) if payload else (set(), set())
    if not files and not symbols and file_path:
        import os as _os

        files = {file_path, _os.path.basename(file_path)}
    return _entity_patterns(files, symbols)


def _decision_open_index(
    required: str | None, delivery_index: int | None, boundaries: dict[str, list[int]]
) -> int | None:
    """The message index where the row's target decision opened: the GREATEST boundary of the
    fact class's required event that is at-or-before delivery (the boundary the delivery is
    answering). ``None`` (fail-closed) when the boundary never opened before delivery."""
    if not required or delivery_index is None:
        return None
    opens = boundaries.get(required) or []
    prior = [b for b in opens if b <= delivery_index]
    return max(prior) if prior else None


# A (per-class decision-commit kernel): the honest COMMIT of a delivered fact's decision is
# class-specific, read from the registry receipt predicate. There are THREE commit modes,
# keyed by the canonical registered fact class (resolved via ``registration_for(evidence_type)
# .fact_class`` so a finer evidence_type / alias resolves correctly, e.g. ``coherence_collapse``
# -> ``recovery``, ``trace_frame`` -> ``localization``):
#
#   * MUTATION commit — an EDIT-shaping fact is committed by the edit it shapes: the first later
#     assistant message emitting a ``_action_kind=='mutation'`` command that NAMES a delivered
#     payload entity. ``obligations`` ALSO accepts a plan-prose branch (its decision is the
#     initial PLAN, committed when the agent states a plan step naming an obligation subject —
#     the plan need not mutate the tree first). NOTE ``submit_refusal`` stays mutation-only: its
#     "is completion allowed" decision is committed by the next REPAIR (a mutation: go fix more),
#     NEVER any keystroke — widening it to any-action mis-reads a non-repair turn as the commit
#     and flips a genuinely on-time refusal to LATE (measured: gitingest-94 ON_TIME under
#     mutation-commit -> spurious LATE under any-action).
#   * OPEN commit — a LOCALIZATION/def-inspection fact's decision ("which file to open" /
#     "which def to inspect") is committed the moment the agent OPENS/READS the ranked or
#     partitioned file: the first later assistant command that is a PASSIVE open/read/view
#     (``_action_kind(cmd) is None``) naming a delivered entity. This reuses the SAME passive
#     test as :func:`_native_acquisition_index` (do NOT reinvent it).
#   * PIVOT commit — ``recovery`` (receipt ``pivoted_after_steer``) is a CHANGE OF COURSE
#     (re-search / re-read / different edit) which need not mutate the tree, so the commit is
#     the first later assistant message emitting a command of ANY kind.
_MUTATION_COMMIT_CLASSES: frozenset[str] = frozenset({
    "caller_contract",
    "syntax_result",
    "signature_delta",
    "covering_red",
    "submit_refusal",
    "newfile_precedent",
    "obligations",
})
_OPEN_COMMIT_CLASSES: frozenset[str] = frozenset({"localization", "def_partition"})
_PIVOT_COMMIT_CLASSES: frozenset[str] = frozenset({"recovery"})


def _commit_class(evidence_type: str | None) -> str | None:
    """The row's canonical registered fact class (the key the commit-mode sets are keyed by),
    or ``None`` when it resolves to no registered class (fail-closed)."""
    reg = registration_for(evidence_type) if evidence_type else None
    return reg.fact_class if reg is not None else None


def _is_pivot_commit(evidence_type: str | None) -> bool:
    """True iff the row's canonical fact class commits its decision by the next agent ACT (a
    pivot/course change) rather than a repository mutation — read from the registry."""
    return _commit_class(evidence_type) in _PIVOT_COMMIT_CLASSES


def _decision_commit_index(
    messages: list[dict],
    decision_open_index: int | None,
    evidence_type: str | None = None,
    pats: list | None = None,
) -> int | None:
    """The message index where the agent COMMITTED the row's decision after the boundary.

    A (per-class kernel): the commit test is chosen by the row's canonical fact class
    (:data:`_MUTATION_COMMIT_CLASSES` / :data:`_OPEN_COMMIT_CLASSES` /
    :data:`_PIVOT_COMMIT_CLASSES`). ``pats`` are the delivered-entity matchers
    (:func:`_delivery_entity_patterns`); a mutation/open commit must NAME a delivered entity
    (when ``pats`` is empty — no entity could be extracted — the entity gate is skipped so the
    older behaviour is preserved). ``None`` (fail-closed) when the decision was never committed.
    """
    if decision_open_index is None:
        return None
    fclass = _commit_class(evidence_type)
    pats = pats or []

    def _names(text: str) -> bool:
        # Entity gate: require a delivered entity when we have patterns; when none could be
        # extracted, do not block the commit (fail-open on the entity gate only).
        return (not pats) or _named_in(text, pats)

    for j in range(decision_open_index + 1, len(messages)):
        msg = messages[j]
        if msg.get("role") != "assistant":
            continue
        cmds = _emitted_commands(msg)
        if fclass in _PIVOT_COMMIT_CLASSES:
            if cmds:  # any agent action commits a pivot/course-change decision
                return j
            continue
        if fclass in _OPEN_COMMIT_CLASSES:
            # first PASSIVE open/read/view naming a delivered (ranked/partitioned) entity —
            # the SAME passive predicate _native_acquisition_index uses.
            for cmd in cmds:
                if _action_kind(cmd) is None and _named_in(cmd, pats):
                    return j
            continue
        # MUTATION commit (default + _MUTATION_COMMIT_CLASSES): the first mutation naming a
        # delivered entity. obligations additionally accepts a plan-prose branch: the first
        # assistant prose naming an obligation subject from the delivered block.
        if fclass == "obligations" and pats and _named_in(_assistant_prose(msg), pats):
            return j
        for cmd in cmds:
            if _action_kind(cmd) == "mutation" and _names(cmd):
                return j
    return None


def _native_acquisition_index(
    messages: list[dict], delivery_index: int | None, payload: str, file_path: str
) -> int | None:
    """The earliest message where the agent ITSELF acquired the delivered fact BEFORE GT
    delivered it — a passive grep/read/view command (not a mutation/verification) naming a
    delivered entity, strictly before ``delivery_index``. Entities are extracted with
    consumption_ledger._block_entities (payload bytes preferred; else the row's file_path).
    ``None`` when the agent did not self-acquire the fact first."""
    if delivery_index is None:
        return None
    pats = _delivery_entity_patterns(payload, file_path)
    if not pats:
        return None
    for j in range(0, delivery_index):
        if messages[j].get("role") != "assistant":
            continue
        for cmd in _emitted_commands(messages[j]):
            # passive acquisition only: a mutation/verification is not a self-acquisition.
            if _action_kind(cmd) is None and _named_in(cmd, pats):
                return j
    return None


def _acknowledgment_index(
    seal: str,
    ledger_rows: list[dict],
    tool_ordinal_index: dict[int, int],
) -> int | None:
    """The message index of the ledger's own consumption ACK for this delivery — a row with
    ``event_type=='ack'`` and ``reason=='ss_ack'`` sealed to the SAME delivery bytes, mapped
    to a message index by its iteration (tool_ordinal). ``None`` when no ack row exists.
    (Feeds only the causal fair-probe branch; it never alters the timing verdict.)"""
    if not _valid_seal(seal):
        return None
    for row in ledger_rows:
        if (
            row.get("event_type") == "ack"
            and row.get("reason") == "ss_ack"
            and row.get("content_sha256_16") == seal
        ):
            it = row.get("iteration")
            if isinstance(it, int) and not isinstance(it, bool):
                return tool_ordinal_index.get(it)
    return None


def _action_index(
    messages: list[dict], delivery_index: int | None, payload: str, file_path: str
) -> int | None:
    """The message index of the agent's first relevant post-delivery action on the delivered
    entity — a mutation/verification command naming a delivered entity, strictly after
    ``delivery_index`` (the consumption-ledger 'acted' receipt). ``None`` otherwise. (Feeds
    only the causal fair-probe branch; it never alters the timing verdict.)"""
    if delivery_index is None:
        return None
    pats = _delivery_entity_patterns(payload, file_path)
    if not pats:
        return None
    for j in range(delivery_index + 1, len(messages)):
        if messages[j].get("role") != "assistant":
            continue
        for cmd in _emitted_commands(messages[j]):
            if _action_kind(cmd) in ("mutation", "verification") and _named_in(cmd, pats):
                return j
    return None


def _unmeasured_reason(
    *,
    seal: str,
    evidence_type: str,
    delivery_index: int | None,
    decision_open_index: int | None,
    decision_commit_index: int | None,
    actual_event: str,
    verdict: str,
    physical_join_reason: str | None = None,
) -> str | None:
    """The FIRST missing join that made a row UNMEASURED (observability only)."""
    if verdict != UNMEASURED:
        return None
    if physical_join_reason:
        return physical_join_reason
    if not _valid_seal(seal):
        return "no_delivery_seal"
    if registration_for(evidence_type) is None or required_event(evidence_type) is None:
        return "unregistered_evidence_type"
    if delivery_index is None:
        return "delivery_unjoined"
    if decision_open_index is None:
        return "decision_open_unresolved"
    if decision_commit_index is None:
        return "decision_commit_unresolved"
    if actual_event not in EVENTS:
        return "event_not_in_vocabulary"
    return "adjudicator_unmeasured"


# --------------------------------------------------------------------------- #
# E (logical-clock + binding assertions)
# --------------------------------------------------------------------------- #
def _binding_delivery_consistent(
    row: dict,
    delivery_index: int | None,
    tool_ordinal_index: dict[int, int],
) -> tuple[bool, str | None]:
    """E (binding defense-in-depth): a delivered row's ``observation_binding`` names the
    observation it was rendered into (``batch_start_iteration``). Assert the physical delivery
    landed AT-OR-AFTER that observation's message — a delivery that precedes its own binding
    observation is an impossible ordering (the seal joined bytes to a message BEFORE the
    observation that produced them). Returns ``(False, reason)`` ONLY on that proven
    inconsistency; otherwise ``(True, None)``. Additive + fail-safe: absent binding / absent
    iteration mapping / unjoined delivery -> ``(True, None)`` (never weakens the seal join)."""
    if delivery_index is None:
        return True, None
    binding = row.get("observation_binding")
    if not isinstance(binding, dict):
        return True, None
    it = binding.get("batch_start_iteration")
    if not isinstance(it, int) or isinstance(it, bool):
        return True, None
    obs_msg = tool_ordinal_index.get(it)
    if obs_msg is None:
        return True, None
    if delivery_index < obs_msg:
        return False, "observation_binding_precedes_delivery"
    return True, None


# --------------------------------------------------------------------------- #
# per-delivery adjudication core (shared by the whole-row and per-block paths)
# --------------------------------------------------------------------------- #
def _adjudicate_delivery(
    *,
    row_index: int,
    evidence_type: str,
    fact_class: str | None,
    actual_event: str,
    seal_str: str,
    delivery_index: int | None,
    payload: str,
    file_path: str,
    physical_state: str | None,
    physical_id: str | None,
    physical_reason: str | None,
    binding_reason: str | None,
    block_id: str | None,
    block_candidate_id: str | None,
    block_char_span: tuple[int, int] | None,
    block_chars_delivered: int | None,
    parent_physical_id: str | None,
    block_lineage_validated: bool,
    messages: list[dict],
    boundaries: dict[str, list[int]],
    ledger_rows: list[dict],
    tool_ordinal_index: dict[int, int],
    native_acq_payload: str | None = None,
) -> ExtractedChronology:
    """Build one delivery's six-index chronology and its adjudicated timing verdict.

    ``binding_reason`` (E) forces UNMEASURED when the observation-binding assertion failed.
    ``block_id`` (D) is set for a per-block adjudication of a compound brief.

    ``native_acq_payload`` (2026-07-20, co-fact): when a delivery RIDES bytes the agent
    already triggered (a co-fact riding the agent's own search), self-acquisition must be
    judged on the NOVEL delivered content, not on the trigger the agent already possessed.
    When given, the STEP_BEHIND self-acquisition scan uses these bytes' entities INSTEAD of
    the full payload's (commit/action still use the full payload). Default ``None`` =
    byte-identical to every existing caller (the whole payload judges self-acquisition)."""
    required = required_event(evidence_type)
    # A: the ONE entity model — decision-commit, native-acquisition, and action all match the
    # SAME delivered-entity patterns.
    pats = _delivery_entity_patterns(payload, file_path)
    decision_open = _decision_open_index(required, delivery_index, boundaries)
    decision_commit = _decision_commit_index(messages, decision_open, evidence_type, pats)
    if native_acq_payload is not None:
        # judge self-acquisition on the NOVEL bytes only (empty file_path so the def-file
        # fallback can't re-introduce the trigger the agent searched).
        native_acq = _native_acquisition_index(
            messages, delivery_index, native_acq_payload, "")
    else:
        native_acq = _native_acquisition_index(messages, delivery_index, payload, file_path)
    ack_index = _acknowledgment_index(seal_str, ledger_rows, tool_ordinal_index)
    act_index = _action_index(messages, delivery_index, payload, file_path)

    chronology = Chronology(
        decision_open_index=decision_open,
        delivery_index=delivery_index,
        decision_commit_index=decision_commit,
        native_acquisition_index=native_acq,
        acknowledgment_index=ack_index,
        action_index=act_index,
    )

    if binding_reason is not None:
        verdict = UNMEASURED  # E: proven observation-binding inconsistency -> fail closed
    elif _valid_seal(seal_str):
        verdict = adjudicate(
            evidence_type=evidence_type,
            actual_event=actual_event,
            delivery_seal=seal_str,
            chronology=chronology,
        ).timing_verdict
    else:
        verdict = UNMEASURED  # no seal to grade -> fail-closed

    reason = _unmeasured_reason(
        seal=seal_str,
        evidence_type=evidence_type,
        delivery_index=delivery_index,
        decision_open_index=decision_open,
        decision_commit_index=decision_commit,
        actual_event=actual_event,
        verdict=verdict,
        physical_join_reason=binding_reason or physical_reason,
    )
    return ExtractedChronology(
        ledger_row_index=row_index,
        evidence_type=evidence_type,
        fact_class=fact_class,
        delivery_seal=seal_str,
        actual_event=actual_event,
        chronology=chronology,
        timing_verdict=verdict,
        unmeasured_reason=reason,
        physical_id=physical_id,
        physical_join_state=physical_state,
        block_id=block_id,
        block_candidate_id=block_candidate_id,
        block_char_span=block_char_span,
        block_chars_delivered=block_chars_delivered,
        parent_physical_id=parent_physical_id,
        block_lineage_validated=block_lineage_validated,
    )


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
def extract_chronologies(
    trajectory: Any, ledger_rows: list[dict]
) -> dict[int, ExtractedChronology]:
    """Build the six exact-index chronology for EVERY delivered ledger row.

    Keyed by the row's index in ``ledger_rows`` (stable, points at the exact row). A row is
    'delivered' when ``outcome=='delivered'`` and ``chars_delivered>0``. Every index is an
    exact message index or ``None``; a missing join is ``None`` so ``adjudicate`` returns
    ``UNMEASURED`` (fail-closed by construction)."""
    messages = _messages(trajectory)
    tool_ordinal_index = _tool_ordinal_to_index(messages)
    consumption = build_consumption_ledger(
        trajectory, runtime_ledger_rows=ledger_rows,
    )
    physical_authority = physical_delivery_authority(consumption)
    physical_deliveries = physical_authority.get("deliveries", {})
    # B-BND (b1/b3): reconcile the boundary set with the SEAM's own recorded submit/edit events.
    boundaries = _boundary_indices(messages, ledger_rows, tool_ordinal_index)

    out: dict[int, ExtractedChronology] = {}
    for row_index, row in enumerate(ledger_rows):
        if not isinstance(row, dict):
            continue
        if row.get("outcome") != "delivered" or int(row.get("chars_delivered") or 0) <= 0:
            continue

        evidence_type = _row_evidence_type(row)
        fact_class = _row_fact_class(evidence_type, row)
        actual_event = _row_actual_event(row)
        seal = row.get("content_sha256_16")
        seal_str = str(seal) if isinstance(seal, str) else ""
        file_path = str(row.get("file_path") or "")

        physical = physical_deliveries.get(str(row_index), {})
        physical_state = (
            physical.get("state") if isinstance(physical, dict) else None
        )
        if physical_state == PHYSICAL_DELIVERY_BOUND:
            delivery_index = physical.get("msg_index")
            payload = str(physical.get("rendered_text") or "")
            physical_reason = None
        else:
            delivery_index = None
            payload = ""
            physical_reason = (
                str(physical.get("reason") or "physical_delivery_unjoined")
                if isinstance(physical, dict)
                else "physical_delivery_unjoined"
            )
        _binding_ok, binding_reason = _binding_delivery_consistent(
            row, delivery_index, tool_ordinal_index
        )
        out[row_index] = _adjudicate_delivery(
            row_index=row_index,
            evidence_type=evidence_type,
            fact_class=fact_class,
            actual_event=actual_event,
            seal_str=seal_str,
            delivery_index=delivery_index,
            payload=payload,
            file_path=file_path,
            physical_state=str(physical_state) if physical_state is not None else None,
            physical_id=(
                str(physical.get("physical_id"))
                if isinstance(physical, dict) and physical.get("physical_id")
                else None
            ),
            physical_reason=physical_reason,
            binding_reason=binding_reason,
            block_id=None,
            block_candidate_id=None,
            block_char_span=None,
            block_chars_delivered=None,
            parent_physical_id=None,
            block_lineage_validated=False,
            messages=messages,
            boundaries=boundaries,
            ledger_rows=ledger_rows,
            tool_ordinal_index=tool_ordinal_index,
        )
    return out


def _cofact_callers_text(payload: str) -> str:
    """The ``callers:`` line(s) of a def-facts block — the NOVEL caller-contract bytes the
    co-fact delivers. Self-acquisition for the caller_contract co-fact is judged on THESE
    caller entities, never the def symbol the agent grepped to trigger the delivery."""
    return "\n".join(
        ln for ln in (payload or "").splitlines()
        if ln.lstrip().lower().startswith("callers:")
    )


def extract_cofact_chronologies(
    trajectory: Any, ledger_rows: list[dict]
) -> list[ExtractedChronology]:
    """Co-fact chronologies for the caller_contract bytes that RIDE a pre-edit delivery.

    A ``post_search.localize`` def-facts block delivered at ``search_result`` carries an
    authorized ``callers:`` line; the seam stamps a ``co_fact`` sidecar (row['co_fact'],
    from ``gt_mini_patch._post_search_cofact_extra``) crediting the caller_contract FACT
    class on that SAME physical delivery. This re-adjudicates the caller_contract timing
    at its ``caller_contract_search`` boundary override (search_result) REUSING the host
    row's exact physical join — same delivery_index, seal, and physical_id — so the class
    verdict can grade ON_TIME instead of the WRONG_EVENT its file_view window forces on a
    separate post-view/edit delivery. Purely ADDITIVE: ``extract_chronologies`` is
    untouched; these fold into ``adjudicate_deliveries`` alongside the block chronologies.
    Dose-safe by construction — the co-fact shares the host physical_id, never a new one,
    so a downstream physical-id dose count is unaffected. Authorized identity ONLY: the
    sidecar must self-declare fact_class=caller_contract, evidence_type=
    caller_contract_search, and a registered producer match (no inference from payload,
    flags, or layer text)."""
    messages = _messages(trajectory)
    tool_ordinal_index = _tool_ordinal_to_index(messages)
    consumption = build_consumption_ledger(
        trajectory, runtime_ledger_rows=ledger_rows,
    )
    physical_authority = physical_delivery_authority(consumption)
    physical_deliveries = physical_authority.get("deliveries", {})
    boundaries = _boundary_indices(messages, ledger_rows, tool_ordinal_index)

    out: list[ExtractedChronology] = []
    for row_index, row in enumerate(ledger_rows):
        if not isinstance(row, dict):
            continue
        if row.get("outcome") != "delivered" or int(row.get("chars_delivered") or 0) <= 0:
            continue
        co = row.get("co_fact")
        if not isinstance(co, dict):
            continue
        # AUTHORIZED co-fact identity only — the sidecar declares its own registered
        # producer/evidence/class; a payload token or flag can never manufacture it.
        if (
            co.get("fact_class") != "caller_contract"
            or co.get("evidence_type") != "caller_contract_search"
            or co.get("producer_registration_match") is not True
        ):
            continue

        actual_event = "search_result"  # the caller_contract_search boundary override
        seal = row.get("content_sha256_16")
        seal_str = str(seal) if isinstance(seal, str) else ""
        file_path = str(row.get("file_path") or "")

        physical = physical_deliveries.get(str(row_index), {})
        physical_state = (
            physical.get("state") if isinstance(physical, dict) else None
        )
        if physical_state == PHYSICAL_DELIVERY_BOUND:
            delivery_index = physical.get("msg_index")
            payload = str(physical.get("rendered_text") or "")
            physical_reason = None
        else:
            delivery_index = None
            payload = ""
            physical_reason = (
                str(physical.get("reason") or "physical_delivery_unjoined")
                if isinstance(physical, dict)
                else "physical_delivery_unjoined"
            )
        _binding_ok, binding_reason = _binding_delivery_consistent(
            row, delivery_index, tool_ordinal_index
        )
        out.append(
            _adjudicate_delivery(
                row_index=row_index,
                evidence_type="caller_contract_search",
                fact_class="caller_contract",
                actual_event=actual_event,
                seal_str=seal_str,
                delivery_index=delivery_index,
                payload=payload,
                file_path=file_path,
                physical_state=(
                    str(physical_state) if physical_state is not None else None
                ),
                physical_id=(
                    str(physical.get("physical_id"))
                    if isinstance(physical, dict) and physical.get("physical_id")
                    else None
                ),
                physical_reason=physical_reason,
                binding_reason=binding_reason,
                block_id=None,
                block_candidate_id=None,
                block_char_span=None,
                block_chars_delivered=None,
                parent_physical_id=None,
                block_lineage_validated=False,
                messages=messages,
                boundaries=boundaries,
                ledger_rows=ledger_rows,
                tool_ordinal_index=tool_ordinal_index,
                # judge STEP_BEHIND on the NOVEL callers, not the def symbol the agent
                # searched to trigger this delivery (commit/action keep the full payload).
                native_acq_payload=_cofact_callers_text(payload),
            )
        )
    return out


_COMPOUND_LINEAGE_SCHEMA = "gt.compound_feature_lineage.v1"


def validate_block_lineage(
    block: dict[str, Any],
    *,
    parent_actual_event: str,
) -> tuple[str, str] | None:
    """Return the registered evidence type and canonical class for exact typed lineage.

    LEGACY-COMPOUND READER — DORMANT ON THE CANONICAL PATH (C14, 2026-07-28). No
    production writer emits ``block_lineage``; its prepend-and-seal producer was deleted
    along with the prepend (4f525f424) when delivery moved to the canonical task-start
    capsule. Reached only from the compound branch, which is guarded by
    ``if not isinstance(block_lineage, list) or not block_lineage``, so zero writers means
    zero calls and no false negative. Kept because SS-10 replay reads RECORDED artifacts
    that do contain compound rows.
    """
    if block.get("lineage_status") != "REGISTERED":
        return None
    declared = block.get("declared_fact_class")
    lineage = block.get("lineage")
    if not isinstance(declared, str) or not declared or not isinstance(lineage, dict):
        return None
    runtime_producer = lineage.get("runtime_producer_id")
    evidence_type = lineage.get("evidence_type")
    actual_event = lineage.get("actual_event")
    features = lineage.get("features")
    if (
        not isinstance(runtime_producer, str)
        or not runtime_producer
        or not isinstance(evidence_type, str)
        or not evidence_type
        or not isinstance(actual_event, str)
        or actual_event != parent_actual_event
        or not isinstance(features, list)
    ):
        return None
    cap_feature_ids: list[str] = []
    for ref in features:
        if not isinstance(ref, dict):
            return None
        category = ref.get("category")
        feature_id = ref.get("feature_id")
        if category == "CAP":
            if not isinstance(feature_id, str) or not feature_id:
                return None
            cap_feature_ids.append(feature_id)
        elif category != "FACT":
            return None
    try:
        rebuilt = build_lineage(
            runtime_producer_id=runtime_producer,
            evidence_type=evidence_type,
            actual_event=actual_event,
            cap_feature_ids=cap_feature_ids,
        )
    except (TypeError, ValueError):
        return None
    if (
        rebuilt is None
        or rebuilt.producer_registration_match is not True
        or rebuilt.fact_class != declared
        or lineage_to_dict(rebuilt) != lineage
    ):
        return None
    return evidence_type, declared


def _validated_compound_blocks(
    row: dict[str, Any],
    parent_payload: str,
) -> list[tuple[dict[str, Any], tuple[int, int], str, str]] | None:
    """Validate the whole compound table before exposing any registered fact block."""
    if (
        row.get("compound_delivery") is not True
        or row.get("compound_lineage_schema") != _COMPOUND_LINEAGE_SCHEMA
    ):
        return None
    blocks = row.get("block_lineage")
    if not isinstance(blocks, list) or not blocks:
        return None
    parent_event = str(row.get("event_type") or "")
    seen_ids: set[str] = set()
    last_end = 0
    validated: list[tuple[dict[str, Any], tuple[int, int], str, str]] = []
    for block in blocks:
        if not isinstance(block, dict):
            return None
        block_id = block.get("block_id")
        candidate_id = block.get("candidate_id")
        span = block.get("char_span")
        block_chars = block.get("chars_delivered")
        block_seal = block.get("content_sha256_16")
        declared = block.get("declared_fact_class")
        if (
            not isinstance(block_id, str)
            or not block_id
            or block_id in seen_ids
            or not isinstance(candidate_id, str)
            or not candidate_id
            or not isinstance(span, list)
            or len(span) != 2
            or not all(type(offset) is int for offset in span)
            or span[0] < 0
            or span[0] >= span[1]
            or span[1] > len(parent_payload)
            or span[0] < last_end
            or type(block_chars) is not int
            or block_chars <= 0
            or block_chars != span[1] - span[0]
            or not _valid_seal(block_seal)
            or not isinstance(declared, str)
            or not declared
        ):
            return None
        declared_span = (span[0], span[1])
        matching_spans = _locate_seal_spans(
            parent_payload, block_chars, str(block_seal)
        )
        if declared_span not in matching_spans:
            return None
        seen_ids.add(block_id)
        last_end = span[1]
        typed = validate_block_lineage(block, parent_actual_event=parent_event)
        if typed is None:
            if block.get("lineage_status") == "REGISTERED":
                return None
            continue
        evidence_type, fact_class = typed
        validated.append((block, declared_span, evidence_type, fact_class))
    return validated


def extract_block_chronologies(
    trajectory: Any, ledger_rows: list[dict]
) -> list[ExtractedChronology]:
    """Adjudicate each registered block in a physically bound compound brief.

    Block identity is conjunctive: unique ``block_id``, exact declared ``char_span`` and
    ``chars_delivered``, the block seal at that span, the parent physical observation, and an
    exact current-registry reconstruction of nested typed lineage. The validated lineage's
    evidence type controls timing; ``declared_fact_class`` remains the canonical FACT class.
    Any malformed/overlapping compound table fails closed as a whole.
    """
    messages = _messages(trajectory)
    tool_ordinal_index = _tool_ordinal_to_index(messages)
    consumption = build_consumption_ledger(trajectory, runtime_ledger_rows=ledger_rows)
    physical_deliveries = physical_delivery_authority(consumption).get("deliveries", {})
    boundaries = _boundary_indices(messages, ledger_rows, tool_ordinal_index)

    out: list[ExtractedChronology] = []
    for row_index, row in enumerate(ledger_rows):
        if not isinstance(row, dict):
            continue
        if row.get("outcome") != "delivered" or int(row.get("chars_delivered") or 0) <= 0:
            continue
        block_lineage = row.get("block_lineage")
        if not isinstance(block_lineage, list) or not block_lineage:
            continue

        physical = physical_deliveries.get(str(row_index), {})
        if not isinstance(physical, dict) or physical.get("state") != PHYSICAL_DELIVERY_BOUND:
            continue
        brief_msg_index = physical.get("msg_index")
        if not isinstance(brief_msg_index, int):
            continue
        parent_physical_id = physical.get("physical_id")
        # ``rendered_text`` is the exact parent-row span already claimed by the physical
        # authority. Block ``char_span`` offsets are relative to this payload, not necessarily
        # to the larger trajectory message that contains it.
        brief_content = physical.get("rendered_text")
        if (
            not isinstance(parent_physical_id, str)
            or not parent_physical_id
            or not isinstance(brief_content, str)
            or physical.get("content_sha256_16") != row.get("content_sha256_16")
            or physical.get("chars") != row.get("chars_delivered")
            or len(brief_content) != row.get("chars_delivered")
        ):
            continue
        validated_blocks = _validated_compound_blocks(row, brief_content)
        if validated_blocks is None:
            continue
        file_path = str(row.get("file_path") or "")

        for block, span, evidence_type, fact_class in validated_blocks:
            block_seal = str(block["content_sha256_16"])
            block_id = str(block["block_id"])
            block_chars = int(block["chars_delivered"])
            block_payload = brief_content[span[0]:span[1]]
            out.append(
                _adjudicate_delivery(
                    row_index=row_index,
                    evidence_type=evidence_type,
                    fact_class=fact_class,
                    actual_event=_normalize_actual_event(str(row.get("event_type") or "")),
                    seal_str=str(block_seal),
                    delivery_index=brief_msg_index,
                    payload=block_payload,
                    file_path=file_path,
                    physical_state=str(PHYSICAL_DELIVERY_BOUND),
                    physical_id=_physical_block_id(parent_physical_id, span, block_id),
                    physical_reason=None,
                    binding_reason=None,
                    block_id=block_id,
                    block_candidate_id=str(block["candidate_id"]),
                    block_char_span=span,
                    block_chars_delivered=block_chars,
                    parent_physical_id=parent_physical_id,
                    block_lineage_validated=True,
                    messages=messages,
                    boundaries=boundaries,
                    ledger_rows=ledger_rows,
                    tool_ordinal_index=tool_ordinal_index,
                )
            )
    return out


def _physical_block_id(
    parent_physical_id: str, span: tuple[int, int], block_id: str
) -> str:
    """Stable per-block physical identity for a compound-brief block adjudication."""
    return f"{parent_physical_id}:block:{span[0]}:{span[1]}:{block_id}"


def _class_verdict(verdicts: list[str]) -> tuple[str, bool | None]:
    """Roll delivered-row verdicts up to one class verdict + its ``correct_time`` bit:
    ON_TIME only when every row is ON_TIME; LATE/WRONG_EVENT/STEP_BEHIND when any row is;
    else UNMEASURED. ``correct_time``: ON_TIME->True, LATE/WRONG_EVENT/STEP_BEHIND->False
    (a delivered row that is measured-and-wrong FAILS the gate), UNMEASURED->None (fail-closed).
    C-cluster: WRONG_EVENT (a known-but-wrong delivery boundary) is a measured FALSE, never
    an unmeasured gap."""
    if verdicts and all(v == ON_TIME for v in verdicts):
        return ON_TIME, True
    # THE LATE FAMILY, most-severe first. `LATE` was split (2026-07-28) into EXPIRED
    # (nothing left to change) and LATE_CORRECTIVE (the agent still re-decided after the
    # bytes arrived). Testing only `v == LATE` here would have let an EXPIRED row fall
    # through to UNMEASURED — silently regrading a measured failure as an unmeasured gap,
    # which is precisely the direction this rollup's own docstring forbids. `LATE` is kept
    # in the tuple so historical artifacts carrying the flat string still roll up.
    #
    # ORDER IS THE CLAIM: EXPIRED outranks LATE_CORRECTIVE because a class containing one
    # useless-late row is not redeemed by another row that happened to still be actionable.
    for _verdict in (EXPIRED, LATE, LATE_CORRECTIVE):
        if any(v == _verdict for v in verdicts):
            return _verdict, False
    if any(v == EARLY for v in verdicts):
        return EARLY, False
    if any(v == WRONG_EVENT for v in verdicts):
        return WRONG_EVENT, False
    if any(v == STEP_BEHIND for v in verdicts):
        return STEP_BEHIND, False
    return UNMEASURED, None


def adjudicate_deliveries(trajectory: Any, ledger_rows: list[dict]) -> dict[str, Any]:
    """Adjudicate every delivered row and roll up per canonical fact class.

    Returns a JSON-safe diagnostic (``gt.chronological_timing_join.v1``): ``per_fact_class``
    carries each class's timing verdict, its ``correct_time`` bit for the
    ``correct_rl_adhered_time`` gate, the row counts, and any UNMEASURED reasons; ``deliveries``
    is the per-row audit trail. Rows that resolve to no registered class are recorded in
    ``deliveries`` but never override a real class (fail-closed)."""
    chronologies = extract_chronologies(trajectory, ledger_rows)
    # D: the per-BLOCK adjudications of every COMPOUND brief row, each carrying its own
    # declared_fact_class. Folded in ALONGSIDE the whole-row chronologies so obligations /
    # localization brief blocks are graded as their own class — never collapsed into one
    # verdict on the (unregistered) compound brief row.
    block_chronologies = extract_block_chronologies(trajectory, ledger_rows)
    # Co-fact chronologies (2026-07-20): credit caller_contract on the pre-edit
    # def_partition physical delivery that carries a ``callers:`` line. Folded in
    # ALONGSIDE the block chronologies so a class verdict sees the earlier, on-time
    # caller-contract delivery. Reuses the host row's physical_id (dose-safe).
    cofact_chronologies = extract_cofact_chronologies(trajectory, ledger_rows)

    by_class: dict[str, list[ExtractedChronology]] = {}
    deliveries: list[dict[str, Any]] = []

    def _record(ec: ExtractedChronology) -> None:
        deliveries.append(
            {
                "ledger_row_index": ec.ledger_row_index,
                "evidence_type": ec.evidence_type,
                "fact_class": ec.fact_class,
                "delivery_seal": ec.delivery_seal,
                "actual_event": ec.actual_event,
                "timing_verdict": ec.timing_verdict,
                "unmeasured_reason": ec.unmeasured_reason,
                "physical_id": ec.physical_id,
                "physical_join_state": ec.physical_join_state,
                "block_id": ec.block_id,
                "block_char_span": (
                    list(ec.block_char_span) if ec.block_char_span is not None else None
                ),
                "block_chars_delivered": ec.block_chars_delivered,
                "parent_physical_id": ec.parent_physical_id,
                "block_lineage_validated": ec.block_lineage_validated,
                "chronology": asdict(ec.chronology),
            }
        )
        if ec.fact_class is not None:
            by_class.setdefault(ec.fact_class, []).append(ec)

    for row_index in sorted(chronologies):
        _record(chronologies[row_index])
    for ec in block_chronologies:
        _record(ec)
    for ec in cofact_chronologies:
        _record(ec)

    per_fact_class: dict[str, Any] = {}
    for fact_class, rows in sorted(by_class.items()):
        verdicts = [r.timing_verdict for r in rows]
        verdict, correct_time = _class_verdict(verdicts)
        reasons = sorted(
            {r.unmeasured_reason for r in rows if r.unmeasured_reason is not None}
        )
        per_fact_class[fact_class] = {
            "verdict": verdict,
            "correct_time": correct_time,
            "rows_total": len(rows),
            "rows_on_time": sum(1 for v in verdicts if v == ON_TIME),
            "rows_late": sum(1 for v in verdicts if v == LATE),
            "rows_wrong_event": sum(1 for v in verdicts if v == WRONG_EVENT),
            "rows_step_behind": sum(1 for v in verdicts if v == STEP_BEHIND),
            "rows_unmeasured": sum(1 for v in verdicts if v == UNMEASURED),
            "unmeasured_reasons": reasons,
        }

    return {
        "schema": TIMING_JOIN_SCHEMA,
        "delivered_rows_graded": (
            len(chronologies) + len(block_chronologies) + len(cofact_chronologies)
        ),
        "per_fact_class": per_fact_class,
        "deliveries": deliveries,
    }


def timing_by_fact_class(join: dict[str, Any]) -> dict[str, bool | None]:
    """The ``fact_class -> correct_time`` map the grader threads into ``ss_gate_readiness``'s
    ``chronological_time`` parameter. Only classes with a measured verdict carry True/False;
    everything else is absent (the caller's ``.get`` yields ``None`` -> gate stays UNMEASURED)."""
    out: dict[str, bool | None] = {}
    for fact_class, info in (join.get("per_fact_class") or {}).items():
        if isinstance(info, dict):
            out[fact_class] = info.get("correct_time")
    return out


__all__ = [
    "TIMING_JOIN_SCHEMA",
    "ExtractedChronology",
    "adjudicate_deliveries",
    "extract_block_chronologies",
    "extract_chronologies",
    "extract_cofact_chronologies",
    "timing_by_fact_class",
]
