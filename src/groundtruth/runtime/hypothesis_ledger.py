"""HypothesisLedger — recovery reasoning over TYPED runtime state (W4).

GT's recovery layer historically read TEXT: it grepped observations and matched
prose heuristics to decide "the agent is stuck / the edit failed / try elsewhere".
This module replaces that with reasoning over the ONE canonical typed state
(:class:`~groundtruth.runtime.episode_state.EpisodeState`) — the probe ledger's
per-stem OUTCOMES, the failure-fingerprint set, the ordered edit record, and the
graph revision. Two things live here:

  1. THE HYPOTHESIS ENVELOPE (:class:`Hypothesis`) + pure functions that store it in
     ``EpisodeState.hypothesis_slots`` (the W4 placeholder the state reserves for us).
  2. NAMED STATE TRANSITIONS — pure classifiers over ``(EpisodeState, LedgerEvent)``.
     Each returns a typed :class:`Advisory` CANDIDATE or ``None`` (correct-or-quiet).

PURE · DETERMINISTIC · LLM-FREE · stdlib-only (+ ``groundtruth.trajectory.classifier``
for the env-failure signature — IMPORTED, never re-detected — and
``groundtruth.runtime.evidence_envelope`` for the ONE tier / advisory vocabulary, so
this layer never mints a parallel controlled vocabulary). No time, no randomness, no
I/O.

LAWS enforced here:
  * UNMEASURED => ADVISORY. Every transition emits an :class:`Advisory` whose
    ``blocking_eligibility`` is ALWAYS ``ADVISORY`` and whose ``tier`` is WARNING/INFO,
    never VERIFIED — a recovery suggestion is a CANDIDATE / a fact-shaped statement,
    NEVER a directive ("edit X", "run Y"). :func:`advisory_violations` gives that teeth
    (rejects a non-advisory eligibility, a VERIFIED tier, or an imperative statement).
  * CORRECT-OR-QUIET. A classifier fires ONLY on its typed evidence; absent evidence =>
    ``None``. Wrong evidence is worse than none.
  * LEAK-SAFE. Statements + evidence ids reference the agent's OWN probe operands
    (stems), failure-fingerprint HASHES, action INDICES, and graph REVISIONS — never a
    file path, test identity, FAIL_TO_PASS, or assertion.

NOVELTY (the observation-novelty distinction — REUSED, not re-invented): "same command,
NEW output" is NOT a loop. The signal is the probe-stem OUTCOME sequence in
``EpisodeState.probe_ledger`` (``outcomes`` == ``["zero"|"hit", ...]``, the
gt_mini_patch Listen-Lattice ledger). All-identical outcomes over >=2 probes ==>
no discriminating evidence (stuck on one surface); an outcome DELTA ==> new information
==> explicitly not a loop. The two classifiers are exact complements on that axis.

INGRESS (the W1a container-normalization lesson): ``hypothesis_slots`` stores plain
ENVELOPE DICTS (``Hypothesis.to_dict()``), never frozen dataclass objects — so
``EpisodeState.to_dict`` (which snapshots per-slot ``dict`` copies, mirroring
``edit_events`` — F4 2026-07-10) stays JSON-serializable and its round-trip law
``from_dict(to_dict(e)) == e`` keeps holding (a dict of str/list-of-str fields
round-trips through JSON exactly; a frozen object would not). Tuple fields normalize
to lists at :meth:`Hypothesis.to_dict` and back to tuples at
:meth:`Hypothesis.from_dict`. ``reset_attempt`` clears the slots (owned by
EpisodeState); this module never touches the state's reset semantics.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, ClassVar

from groundtruth.runtime.episode_state import EpisodeState
from groundtruth.runtime.evidence_envelope import ADVISORY, INFO, VERIFIED, WARNING
from groundtruth.runtime.patterns import is_infra_noise
from groundtruth.trajectory.classifier import FailureKind, is_env_failure

__all__ = [
    "Hypothesis",
    "LedgerEvent",
    "Advisory",
    # hypothesis-slot pure functions (the documented ingress)
    "add_hypothesis",
    "get_hypothesis",
    "hypotheses",
    "record_experiment",
    "mark_supported",
    "mark_falsified",
    "mark_superseded",
    # named state transitions (pure classifiers)
    "classify_no_discriminating_evidence",
    "classify_edit_contradicted_contract",
    "classify_same_failure_unchanged_patch",
    "classify_same_command_new_output",
    "classify_env_failure",
    "classify_stale_graph",
    "classify_all",
    "TRANSITIONS",
    # validation
    "advisory_violations",
    # status vocabulary
    "STATUS_OPEN",
    "STATUS_SUPPORTED",
    "STATUS_FALSIFIED",
    "STATUS_SUPERSEDED",
    # transition-name vocabulary
    "T_NO_DISCRIMINATING_EVIDENCE",
    "T_EDIT_CONTRADICTED_CONTRACT",
    "T_SAME_FAILURE_UNCHANGED_PATCH",
    "T_SAME_COMMAND_NEW_OUTPUT",
    "T_ENV_FAILURE",
    "T_STALE_GRAPH",
    # disposition (recovery-candidate CLASS) vocabulary
    "D_ALTERNATE_SURFACE_CANDIDATE",
    "D_HYPOTHESIS_FALSIFIED",
    "D_REQUEST_NEW_HYPOTHESIS",
    "D_NOT_A_LOOP",
    "D_REPAIR_NOT_SOURCE",
    "D_REFRESH_BEFORE_ADVICE",
]

# --------------------------------------------------------------------------- #
# controlled vocabularies
# --------------------------------------------------------------------------- #
STATUS_OPEN = "open"
STATUS_SUPPORTED = "supported"
STATUS_FALSIFIED = "falsified"
STATUS_SUPERSEDED = "superseded"
_STATUSES: frozenset[str] = frozenset(
    {STATUS_OPEN, STATUS_SUPPORTED, STATUS_FALSIFIED, STATUS_SUPERSEDED}
)

# named transitions (the recovery table)
T_NO_DISCRIMINATING_EVIDENCE = "no_discriminating_evidence"
T_EDIT_CONTRADICTED_CONTRACT = "edit_contradicted_contract"
T_SAME_FAILURE_UNCHANGED_PATCH = "same_failure_unchanged_patch"
T_SAME_COMMAND_NEW_OUTPUT = "same_command_new_output"
T_ENV_FAILURE = "env_failure"
T_STALE_GRAPH = "stale_graph"
_TRANSITION_NAMES: frozenset[str] = frozenset(
    {
        T_NO_DISCRIMINATING_EVIDENCE,
        T_EDIT_CONTRADICTED_CONTRACT,
        T_SAME_FAILURE_UNCHANGED_PATCH,
        T_SAME_COMMAND_NEW_OUTPUT,
        T_ENV_FAILURE,
        T_STALE_GRAPH,
    }
)

# dispositions == the CLASS of recovery candidate (never a directive string).
D_ALTERNATE_SURFACE_CANDIDATE = "alternate_surface_candidate"
D_HYPOTHESIS_FALSIFIED = "hypothesis_falsified"
D_REQUEST_NEW_HYPOTHESIS = "request_new_hypothesis"
D_NOT_A_LOOP = "not_a_loop"
D_REPAIR_NOT_SOURCE = "repair_not_source"
D_REFRESH_BEFORE_ADVICE = "refresh_before_advice"
_DISPOSITIONS: frozenset[str] = frozenset(
    {
        D_ALTERNATE_SURFACE_CANDIDATE,
        D_HYPOTHESIS_FALSIFIED,
        D_REQUEST_NEW_HYPOTHESIS,
        D_NOT_A_LOOP,
        D_REPAIR_NOT_SOURCE,
        D_REFRESH_BEFORE_ADVICE,
    }
)

# the only tiers a recovery candidate may carry (VERIFIED is a measured-fact promise —
# a recovery suggestion is UNMEASURED, so it is WARNING or INFO, never VERIFIED).
_ADVISORY_TIERS: frozenset[str] = frozenset({WARNING, INFO})

# statement-must-be-declarative guard: an advisory STATES a fact; it never issues a
# directive. _is_directive_statement is a BEST-EFFORT detector of the common directive
# shapes (leading imperative verb, politeness-prefixed imperative, second-person modal
# command) — not a grammar; a short "Label:" head (e.g. "Edit target: ...") is a
# noun-phrase FACT form and never a directive (F3, Fable bounce 2026-07-10).
_IMPERATIVE_VERBS: frozenset[str] = frozenset(
    {
        "edit", "run", "change", "add", "fix", "delete", "remove", "replace",
        "modify", "rewrite", "use", "try", "call", "open", "read", "write",
        "create", "move", "refactor", "revise", "update", "check", "verify",
        "inspect", "focus", "start",
        # F3 misses (bounce): the detector's verb list was too narrow
        "rerun", "re-run", "retry", "install", "apply", "grep", "ensure",
        "make", "implement", "execute", "launch", "revert", "restore",
        "investigate", "debug", "confirm", "avoid", "stop", "consider",
        "search", "look", "find", "examine", "rebuild", "double-check",
    }
)

# leading politeness/sequencing tokens that wrap an imperative ("Please edit ...").
_POLITENESS_PREFIXES: frozenset[str] = frozenset(
    {"please", "kindly", "just", "first", "next", "then", "also", "now"}
)

# modals after a leading "you" that make a second-person directive ("You should edit").
_MODAL_AFTER_YOU: frozenset[str] = frozenset(
    {"should", "must", "need", "ought", "shall", "can", "could", "may",
     "might", "will", "would", "have"}
)


def _is_directive_statement(stmt: str) -> str | None:
    """Return the offending token(s) when ``stmt`` reads as a DIRECTIVE, else None.
    Detected shapes: a leading imperative verb ("Edit the loader."), a politeness/
    sequencing prefix wrapping one ("Please edit ..."), and a second-person modal
    command ("You should edit ..."). A colon within the first two tokens is a LABEL
    head — the noun-phrase fact form ("Edit target: the loader module.") — and is
    never a directive (F3 false-reject fix). Best-effort shape detection, not a
    grammar: declarative statements that merely CONTAIN a verb are not flagged."""
    tokens = stmt.strip().split()
    if not tokens:
        return None
    for t in tokens[:2]:
        if t.endswith(":"):
            return None  # "X:" / "X y:" label head => noun-phrase fact, not a command
    i = 0
    while i < len(tokens) and tokens[i].rstrip(":,.!").lower() in _POLITENESS_PREFIXES:
        i += 1
    if i >= len(tokens):
        return None
    first = tokens[i].rstrip(":,.!").lower()
    if first in _IMPERATIVE_VERBS:
        return first
    if first == "you" and i + 1 < len(tokens):
        nxt = tokens[i + 1].rstrip(":,.!").lower()
        if nxt in _MODAL_AFTER_YOU:
            return f"you {nxt}"
    return None


# --------------------------------------------------------------------------- #
# THE HYPOTHESIS ENVELOPE
# --------------------------------------------------------------------------- #
def _hypothesis_id(episode_id: str, claim: str) -> str:
    """Deterministic hash of ``claim`` scoped to ``episode_id`` — the hypothesis
    identity. No wallclock, no randomness: the SAME claim in the SAME episode always
    hashes the same, so re-forming a claim is idempotent (one slot). The pair is
    framed as a canonical JSON LIST (the W0/derive_dedup_key lesson, re-proven by the
    reviewer's executed attack): a separator-joined framing is forgeable when a field
    CONTAINS the separator — ``('a', 'b\\x00c')`` collided with ``('a\\x00b', 'c')``
    under the old NUL join. JSON string framing is injective on the pair and TOTAL
    (``ensure_ascii`` escapes every code point, including NUL and lone surrogates)."""
    raw = json.dumps(
        [episode_id or "", claim or ""], ensure_ascii=True, separators=(",", ":")
    ).encode("ascii")
    return hashlib.sha256(raw).hexdigest()[:16]


@dataclass(frozen=True)
class Hypothesis:
    """One immutable recovery hypothesis about the task, envelope-style.

    ``id`` is the deterministic hash of ``claim`` + episode. ``supporting_fact_ids`` /
    ``contradicting_fact_ids`` are opaque evidence-id tuples (probe/edit/fingerprint/
    hypothesis references — never file paths). ``graph_revision`` records the revision
    the supporting facts were gathered under, so :func:`classify_stale_graph` can tell
    when the advice's grounding went stale.
    """

    id: str
    claim: str
    supporting_fact_ids: tuple[str, ...] = ()
    contradicting_fact_ids: tuple[str, ...] = ()
    predicted_observation: str = ""
    experiment_command: str = ""
    result: str = ""
    status: str = STATUS_OPEN
    graph_revision: str = ""

    FIELD_ORDER: ClassVar[tuple[str, ...]] = (
        "id",
        "claim",
        "supporting_fact_ids",
        "contradicting_fact_ids",
        "predicted_observation",
        "experiment_command",
        "result",
        "status",
        "graph_revision",
    )

    @classmethod
    def build(
        cls,
        *,
        claim: str,
        episode_id: str = "",
        supporting_fact_ids: "tuple[str, ...] | list[str]" = (),
        contradicting_fact_ids: "tuple[str, ...] | list[str]" = (),
        predicted_observation: str = "",
        experiment_command: str = "",
        result: str = "",
        status: str = STATUS_OPEN,
        graph_revision: str = "",
    ) -> Hypothesis:
        """Construct a hypothesis with its ``id`` DERIVED from ``claim`` + ``episode_id``
        (never hand-set). Evidence-id containers normalize to string tuples at ingress
        (the W1a lesson — a tuple/list of mixed types would break round-trip equality).
        The claim is COERCED to str BEFORE the id derivation (reviewer attack: with
        ``claim=None`` the stored claim ``'None'`` did not hash to its own id, and
        None silently shared a slot with ``''``)."""
        claim_s = str(claim)
        return cls(
            id=_hypothesis_id(str(episode_id), claim_s),
            claim=claim_s,
            supporting_fact_ids=tuple(str(x) for x in (supporting_fact_ids or ())),
            contradicting_fact_ids=tuple(str(x) for x in (contradicting_fact_ids or ())),
            predicted_observation=str(predicted_observation),
            experiment_command=str(experiment_command),
            result=str(result),
            status=str(status),
            graph_revision=str(graph_revision),
        )

    def to_dict(self) -> dict[str, Any]:
        """A JSON-serializable envelope dict (keys in :attr:`FIELD_ORDER`). Tuples become
        lists (JSON has no tuple); :meth:`from_dict` restores them exactly. This is the
        SHAPE stored in ``EpisodeState.hypothesis_slots`` — a dict, so the state's own
        ``to_dict``/``from_dict`` round-trip law keeps holding."""
        return {
            "id": self.id,
            "claim": self.claim,
            "supporting_fact_ids": list(self.supporting_fact_ids),
            "contradicting_fact_ids": list(self.contradicting_fact_ids),
            "predicted_observation": self.predicted_observation,
            "experiment_command": self.experiment_command,
            "result": self.result,
            "status": self.status,
            "graph_revision": self.graph_revision,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Hypothesis:
        """Inverse of :meth:`to_dict`; ``from_dict(to_dict(h)) == h`` exactly (evidence-id
        lists rebuilt as string tuples). Input is ``Any``-typed because a slot dict came
        through JSON — coercion is the boundary's job."""
        return cls(
            id=str(d.get("id", "")),
            claim=str(d.get("claim", "")),
            supporting_fact_ids=tuple(str(x) for x in (d.get("supporting_fact_ids") or ())),
            contradicting_fact_ids=tuple(
                str(x) for x in (d.get("contradicting_fact_ids") or ())
            ),
            predicted_observation=str(d.get("predicted_observation", "")),
            experiment_command=str(d.get("experiment_command", "")),
            result=str(d.get("result", "")),
            status=str(d.get("status", STATUS_OPEN)),
            graph_revision=str(d.get("graph_revision", "")),
        )


# --------------------------------------------------------------------------- #
# THE EVENT + THE ADVISORY (transition output)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class LedgerEvent:
    """The latest runtime event a classifier reasons over, alongside the EpisodeState.

    Scalar + typed. ``probe_stem`` is the PRE-NORMALIZED stem key (the seam folds it via
    ``gt_mini_patch._norm_stem`` — this module never re-implements that folding, so the
    key matches ``EpisodeState.probe_ledger`` exactly). ``failure_fingerprint`` is the
    hash the seam already computes into ``EpisodeState.failure_fingerprints``.
    ``graph_revision`` is the CURRENT graph revision at this event.
    """

    action_index: int = -1
    command: str = ""
    observation: str = ""
    probe_stem: str = ""
    failure_fingerprint: str = ""
    graph_revision: str = ""


@dataclass(frozen=True)
class Advisory:
    """A recovery CANDIDATE record emitted by a transition — never blocking, never a
    directive. ``statement`` is a fact-shaped declarative sentence; ``disposition`` is
    the CLASS of recovery candidate (e.g. ``request_new_hypothesis``), not an action to
    take; ``evidence_ids`` reference the typed facts that justify it. Serializable +
    deterministic.
    """

    transition: str
    disposition: str
    tier: str = INFO
    blocking_eligibility: str = ADVISORY  # ALWAYS advisory — a candidate, never enforced
    statement: str = ""
    evidence_ids: tuple[str, ...] = field(default_factory=tuple)

    FIELD_ORDER: ClassVar[tuple[str, ...]] = (
        "transition",
        "disposition",
        "tier",
        "blocking_eligibility",
        "statement",
        "evidence_ids",
    )

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable envelope dict (keys in :attr:`FIELD_ORDER`); the evidence-id
        tuple becomes a list. Order-preserving (evidence ids carry probe ORDER), no set
        iteration, no time — byte-identical across processes/seeds."""
        return {
            "transition": self.transition,
            "disposition": self.disposition,
            "tier": self.tier,
            "blocking_eligibility": self.blocking_eligibility,
            "statement": self.statement,
            "evidence_ids": list(self.evidence_ids),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Advisory:
        """Inverse of :meth:`to_dict`; ``from_dict(to_dict(a)) == a`` exactly."""
        return cls(
            transition=str(d.get("transition", "")),
            disposition=str(d.get("disposition", "")),
            tier=str(d.get("tier", INFO)),
            blocking_eligibility=str(d.get("blocking_eligibility", ADVISORY)),
            statement=str(d.get("statement", "")),
            evidence_ids=tuple(str(x) for x in (d.get("evidence_ids") or ())),
        )


def _advisory(
    transition: str,
    disposition: str,
    tier: str,
    statement: str,
    evidence_ids: "tuple[str, ...] | list[str]",
) -> Advisory:
    """Factory that FORCES the advisory invariant: blocking_eligibility == ADVISORY,
    evidence ids normalized to a string tuple (deterministic order preserved)."""
    return Advisory(
        transition=transition,
        disposition=disposition,
        tier=tier,
        blocking_eligibility=ADVISORY,
        statement=statement,
        evidence_ids=tuple(str(x) for x in evidence_ids),
    )


def advisory_violations(adv: Advisory) -> list[str]:
    """Return every law violation of ``adv`` (empty list == law-abiding). Gives the
    UNMEASURED=>ADVISORY + non-directive laws teeth: an advisory MUST be non-blocking,
    MUST carry a WARNING/INFO tier (never VERIFIED — unmeasured), MUST name a known
    transition + disposition, and its statement MUST be declarative (not an imperative
    directive). Deterministic ordering."""
    v: list[str] = []
    if adv.transition not in _TRANSITION_NAMES:
        v.append(f"transition: unknown transition {adv.transition!r}")
    if adv.disposition not in _DISPOSITIONS:
        v.append(f"disposition: unknown disposition {adv.disposition!r}")
    if adv.blocking_eligibility != ADVISORY:
        v.append(
            f"authority: blocking_eligibility={adv.blocking_eligibility!r} — a recovery "
            f"candidate is ALWAYS advisory (never blocking/one_bounce)"
        )
    if adv.tier == VERIFIED:
        v.append("tier: VERIFIED forbidden — a recovery candidate is unmeasured (WARNING/INFO)")
    elif adv.tier not in _ADVISORY_TIERS:
        v.append(f"tier: {adv.tier!r} not in {{WARNING, INFO}}")
    if not adv.statement.strip():
        v.append("statement: empty (an advisory must state its fact)")
    else:
        tok = _is_directive_statement(adv.statement)
        if tok is not None:
            v.append(
                f"statement: directive shape ({tok!r}) — must be declarative, never an "
                f"imperative or second-person command (a fact-shaped statement)"
            )
    return v


# --------------------------------------------------------------------------- #
# HYPOTHESIS-SLOT PURE FUNCTIONS — the documented ingress into hypothesis_slots.
# Each mutates state.hypothesis_slots IN PLACE (the ONLY sanctioned write path, mirror
# of EpisodeState.note_edited_lines) and returns the affected envelope dict, so a caller
# can chain/inspect. Slots hold DICTS (Hypothesis.to_dict()), never frozen objects, so
# EpisodeState.to_dict/from_dict stays JSON-safe + round-trip stable.
# --------------------------------------------------------------------------- #
def _find_slot(state: EpisodeState, hyp_id: str) -> dict[str, Any] | None:
    """The stored envelope dict for ``hyp_id``, or None (correct-or-quiet)."""
    for slot in state.hypothesis_slots:
        if isinstance(slot, dict) and slot.get("id") == hyp_id:
            return slot
    return None


def add_hypothesis(state: EpisodeState, hyp: Hypothesis) -> dict[str, Any]:
    """Store ``hyp`` in ``state.hypothesis_slots`` as its envelope dict; return the stored
    dict. IDEMPOTENT by id — re-adding a claim already present in this episode returns the
    EXISTING slot unchanged (the deterministic-id identity: one claim+episode => one
    slot), so the ledger never duplicates a hypothesis."""
    existing = _find_slot(state, hyp.id)
    if existing is not None:
        return existing
    slot = hyp.to_dict()
    state.hypothesis_slots.append(slot)
    return slot


def get_hypothesis(state: EpisodeState, hyp_id: str) -> Hypothesis | None:
    """The typed :class:`Hypothesis` for ``hyp_id``, or None."""
    slot = _find_slot(state, hyp_id)
    return Hypothesis.from_dict(slot) if slot is not None else None


def hypotheses(state: EpisodeState) -> list[Hypothesis]:
    """All stored hypotheses as typed objects, in insertion order (deterministic).
    Malformed / non-dict slots are skipped (correct-or-quiet)."""
    out: list[Hypothesis] = []
    for slot in state.hypothesis_slots:
        if isinstance(slot, dict) and slot.get("id"):
            out.append(Hypothesis.from_dict(slot))
    return out


def record_experiment(state: EpisodeState, hyp_id: str, result: str) -> dict[str, Any] | None:
    """Record the observed ``result`` of a hypothesis's experiment. Mutates the stored
    slot in place; returns it, or None if the id is unknown (correct-or-quiet)."""
    slot = _find_slot(state, hyp_id)
    if slot is None:
        return None
    slot["result"] = str(result)
    return slot


def _mark(state: EpisodeState, hyp_id: str, status: str) -> dict[str, Any] | None:
    slot = _find_slot(state, hyp_id)
    if slot is None:
        return None
    slot["status"] = status
    return slot


def mark_supported(state: EpisodeState, hyp_id: str) -> dict[str, Any] | None:
    """Mark the hypothesis SUPPORTED (its prediction held). None if unknown id."""
    return _mark(state, hyp_id, STATUS_SUPPORTED)


def mark_falsified(state: EpisodeState, hyp_id: str) -> dict[str, Any] | None:
    """Mark the hypothesis FALSIFIED (its prediction was contradicted). None if unknown."""
    return _mark(state, hyp_id, STATUS_FALSIFIED)


def mark_superseded(state: EpisodeState, hyp_id: str) -> dict[str, Any] | None:
    """Mark the hypothesis SUPERSEDED (a newer hypothesis replaced it). None if unknown."""
    return _mark(state, hyp_id, STATUS_SUPERSEDED)


# --------------------------------------------------------------------------- #
# helpers shared by the failure-repeat classifiers
# --------------------------------------------------------------------------- #
def _prior_failure_index(state: EpisodeState, fingerprint: str) -> int:
    """The action index of the LAST recorded occurrence of ``fingerprint``, or -1 when
    UNKNOWN. Read from ``last_failure_record`` (tolerant keys, matching the
    FailureSnapshot ``iter_observed`` shape and the gt_mini_patch record shape). Only
    trusted when the recorded failure IS this fingerprint (or no fingerprint was
    recorded); a DIFFERENT recorded failure gives -1 (we will not borrow its index).

    HONEST DEGRADATION (F1): -1 is a "no ordering information" SENTINEL, never an
    index. Seams that write no index into ``last_failure_record`` (the mini seam's
    default) land here ALWAYS; on that path edit-vs-failure ordering is UNDECIDABLE
    (:func:`_edit_between_verdict` returns ``"unknown"`` when edits exist), so
    falsification NEVER fires and the sentinel never reaches a statement."""
    rec = state.last_failure_record or {}
    rec_fp = str(
        rec.get("failure_fingerprint") or rec.get("fingerprint") or rec.get("hash") or ""
    )
    if rec_fp and rec_fp != fingerprint:
        return -1
    for k in ("action_index", "index", "iter", "iter_observed"):
        if k in rec:
            try:
                return int(rec[k])
            except (TypeError, ValueError):
                continue
    return -1


def _edit_indices(state: EpisodeState) -> list[int]:
    """Sorted, de-duplicated action indices at which a source edit is recorded — from the
    ordered ``edit_events`` (``{index, blob}``) plus the ``edited_files`` last-touch map
    (best available)."""
    idxs: set[int] = set()
    for ev in state.edit_events:
        raw = ev.get("index") if isinstance(ev, dict) else None
        if raw is None:
            continue
        try:
            idxs.add(int(raw))
        except (TypeError, ValueError):
            continue
    for v in state.edited_files.values():
        try:
            idxs.add(int(v))
        except (TypeError, ValueError):
            continue
    return sorted(idxs)


# tri-state verdict values for "did an edit intervene between the two occurrences?"
_EDIT_YES = "yes"
_EDIT_NO = "no"
_EDIT_UNKNOWN = "unknown"


def _edit_between_verdict(state: EpisodeState, prior_idx: int, current_idx: int) -> str:
    """TRI-STATE: did a source edit occur BETWEEN the prior occurrence of the failure
    and the current one? (F1, Fable bounce 2026-07-10 — the old boolean degraded
    "prior index unknown" to "any edit ever before now", which FALSELY falsified when
    the only edit PREDATED the fingerprint's first occurrence.)

      * ``"no"``      — provably not: ZERO edits are recorded, or (with a known
                        ``prior_idx``) every edit falls outside the window.
      * ``"yes"``     — provably yes: ``prior_idx`` is KNOWN and an edit lands
                        strictly after it (and strictly before ``current_idx`` when
                        that is known; every edit already in the state was recorded
                        before "now" by construction, so a known prior alone
                        suffices to order against the recurrence).
      * ``"unknown"`` — ``prior_idx`` is UNKNOWN (-1) and edits exist: the edits
                        cannot be ordered against the fingerprint's earlier
                        occurrence (an edit may PREDATE it), so NEITHER complement
                        may fire (correct-or-quiet). -1 is a no-ordering-information
                        sentinel, NEVER an index — it must never reach a statement.
    """
    edits = _edit_indices(state)
    if current_idx >= 0:
        edits = [ei for ei in edits if ei < current_idx]
    if not edits:
        return _EDIT_NO
    if prior_idx < 0:
        return _EDIT_UNKNOWN
    return _EDIT_YES if any(ei > prior_idx for ei in edits) else _EDIT_NO


def _probe_evidence_ids(stem: str, indices: "list[Any]") -> list[str]:
    """Stable, leak-safe evidence ids for a stem's probes: ``probe:<stem>:<index>``."""
    return [f"probe:{stem}:{i}" for i in indices]


def _probe_outcomes_and_indices(entry: Any) -> tuple[list[str], list[Any]] | None:
    """Validate + coerce a probe-ledger entry to ``(outcomes, indices)`` at ingress, or
    ``None`` when the entry is NON-CONFORMING — the classifier then ABSTAINS instead of
    crashing (F2, Fable bounce 2026-07-10: one malformed entry TypeError'd out of
    ``classify_all`` and killed the whole recovery layer).

      * ``outcomes`` missing/None -> ``[]``; a BARE STR is ONE outcome (``"zero"`` must
        never char-split into ``['z','e','r','o']`` — the false not_a_loop, P5); a
        list/tuple must be all-str (any non-str member -> non-conforming -> None).
      * ``probe_indices`` missing/None -> ``[]``; non-list/tuple -> non-conforming.
      * a non-dict entry -> non-conforming."""
    if not isinstance(entry, dict):
        return None
    raw = entry.get("outcomes")
    outcomes: list[str]
    if raw is None:
        outcomes = []
    elif isinstance(raw, str):
        outcomes = [raw]
    elif isinstance(raw, (list, tuple)):
        outcomes = []
        for o in raw:
            if not isinstance(o, str):
                return None
            outcomes.append(o)
    else:
        return None
    raw_idx = entry.get("probe_indices")
    if raw_idx is None:
        indices: list[Any] = []
    elif isinstance(raw_idx, (list, tuple)):
        indices = list(raw_idx)
    else:
        return None
    return outcomes, indices


# --------------------------------------------------------------------------- #
# NAMED STATE TRANSITIONS — pure classifiers over (EpisodeState, LedgerEvent).
# Each returns an Advisory candidate or None (correct-or-quiet).
# --------------------------------------------------------------------------- #
def classify_no_discriminating_evidence(
    state: EpisodeState, event: LedgerEvent
) -> Advisory | None:
    """Repeated probes of ONE stem that all returned the SAME outcome (all "zero", or all
    "hit") gained no discriminating evidence — the agent is stuck on one surface. Suggests
    an ALTERNATE-SURFACE candidate. Fires only on >=2 probes with a single distinct
    outcome; an outcome DELTA is NOT this transition (that is same_command_new_output)."""
    stem = event.probe_stem
    if not stem:
        return None
    norm = _probe_outcomes_and_indices(state.probe_ledger.get(stem))
    if norm is None:
        return None  # missing or non-conforming entry -> abstain (F2)
    outcomes, indices = norm
    if len(outcomes) < 2:
        return None  # a single probe is not "stuck"
    if len(set(outcomes)) != 1:
        return None  # an outcome delta => new information => NOT no-discriminating
    only = outcomes[0]
    statement = (
        f'the probe stem "{stem}" was queried {len(outcomes)} times with the same '
        f'outcome ("{only}") each time; no discriminating evidence was gained — an '
        f"alternate surface may distinguish the candidates."
    )
    return _advisory(
        T_NO_DISCRIMINATING_EVIDENCE,
        D_ALTERNATE_SURFACE_CANDIDATE,
        INFO,
        statement,
        _probe_evidence_ids(stem, indices),
    )


def classify_same_command_new_output(
    state: EpisodeState, event: LedgerEvent
) -> Advisory | None:
    """The SAME probe stem produced CHANGING outcomes across its probes (e.g. "zero" then
    "hit") — the same command yielded NEW output. This is progress, EXPLICITLY NOT a loop.
    The novelty signal is the probe-ledger outcome DELTA; without a delta this stays
    quiet (that case is no_discriminating_evidence)."""
    stem = event.probe_stem
    if not stem:
        return None
    norm = _probe_outcomes_and_indices(state.probe_ledger.get(stem))
    if norm is None:
        return None  # missing or non-conforming entry -> abstain (F2)
    outcomes, indices = norm
    if len(outcomes) < 2:
        return None
    if len(set(outcomes)) < 2:
        return None  # all-identical => no novelty => not this transition
    statement = (
        f'the probe stem "{stem}" produced changing outcomes across {len(outcomes)} '
        f"probes ({outcomes}); the same command yielded new output — this is progress, "
        f"not a loop."
    )
    return _advisory(
        T_SAME_COMMAND_NEW_OUTPUT,
        D_NOT_A_LOOP,
        INFO,
        statement,
        _probe_evidence_ids(stem, indices),
    )


def classify_edit_contradicted_contract(
    state: EpisodeState, event: LedgerEvent
) -> Advisory | None:
    """A source edit was PROVABLY followed by a REPEAT of a known failure fingerprint —
    the edit did not change the failing result, so the hypothesis the edit embodied is
    FALSIFIED. Fires ONLY on: (a) a repeat fingerprint (already in
    ``failure_fingerprints``), AND (b) a *provable* edit-between verdict — the prior
    occurrence index must be KNOWN and an edit must land strictly inside the window
    (F1: on unknown ordering this NEVER fires; falsification demands proof)."""
    fp = event.failure_fingerprint
    if not fp:
        return None
    # W4 guard 1: a fingerprint sitting on a HARNESS infra/teardown observation is not
    # the agent's source regression — never falsify a hypothesis from teardown noise.
    if event.observation and is_infra_noise(event.observation):
        return None
    if fp not in state.failure_fingerprints:
        return None  # a FRESH failure is not yet a contradiction
    prior_idx = _prior_failure_index(state, fp)
    if _edit_between_verdict(state, prior_idx, event.action_index) != _EDIT_YES:
        return None  # "no" => same_failure_unchanged_patch; "unknown" => quiet (F1)
    # verdict YES guarantees prior_idx >= 0 and a non-empty in-window edit set, so
    # edit_idx is always a REAL index — the -1 sentinel can never reach the statement.
    between = [
        ei for ei in _edit_indices(state)
        if ei > prior_idx and (event.action_index < 0 or ei < event.action_index)
    ]
    edit_idx = max(between)
    statement = (
        f'the failure fingerprint "{fp}" recurred after a source edit (at action '
        f"{edit_idx}); the edit did not change the failing result — the hypothesis it "
        f"embodied is falsified."
    )
    evidence = [f"fingerprint:{fp}", f"edit:{edit_idx}"]
    return _advisory(
        T_EDIT_CONTRADICTED_CONTRACT,
        D_HYPOTHESIS_FALSIFIED,
        WARNING,
        statement,
        evidence,
    )


def classify_same_failure_unchanged_patch(
    state: EpisodeState, event: LedgerEvent
) -> Advisory | None:
    """A known failure fingerprint REPEATED with PROVABLY no intervening edit —
    re-running the unchanged patch cannot change the result, so a NEW hypothesis is
    needed. Complement of edit_contradicted_contract on the DECIDABLE region of the
    "edit between" axis; when the ordering is UNKNOWN (prior index unrecorded + edits
    exist) BOTH stay quiet (F1: the no-edit half must be provable — it is, trivially,
    when zero edits are recorded)."""
    fp = event.failure_fingerprint
    if not fp:
        return None
    # W4 guard 1: harness infra/teardown noise is never the agent's regression, so it
    # cannot demand a new hypothesis (correct-or-quiet — wrong steering beats none).
    if event.observation and is_infra_noise(event.observation):
        return None
    if fp not in state.failure_fingerprints:
        return None
    prior_idx = _prior_failure_index(state, fp)
    if _edit_between_verdict(state, prior_idx, event.action_index) != _EDIT_NO:
        return None  # "yes" => edit_contradicted_contract; "unknown" => quiet (F1)
    statement = (
        f'the failure fingerprint "{fp}" recurred with no intervening source edit; '
        f"re-running the unchanged patch cannot change the result — a new hypothesis is "
        f"needed."
    )
    return _advisory(
        T_SAME_FAILURE_UNCHANGED_PATCH,
        D_REQUEST_NEW_HYPOTHESIS,
        WARNING,
        statement,
        [f"fingerprint:{fp}"],
    )


def classify_env_failure(state: EpisodeState, event: LedgerEvent) -> Advisory | None:
    """The observation matches an ENVIRONMENT-failure signature (module-not-found +
    pip-install, command-not-found, connection/permission/host errors) per the IMPORTED
    ``classifier.is_env_failure`` — GT re-detects nothing. Points at environment REPAIR,
    not a defect in the source under edit."""
    obs = event.observation
    if not obs or not is_env_failure(obs):
        return None
    statement = (
        f"the latest observation matches an environment-failure signature "
        f"({FailureKind.ENV_ERROR}); it indicates environment repair, not a defect in "
        f"the source under edit."
    )
    evidence = [f"failure_kind:{FailureKind.ENV_ERROR}"]
    if event.action_index >= 0:
        evidence.append(f"observation:{event.action_index}")
    return _advisory(
        T_ENV_FAILURE,
        D_REPAIR_NOT_SOURCE,
        INFO,
        statement,
        evidence,
    )


def _latest_hypothesis_revision(state: EpisodeState) -> tuple[str, str]:
    """The (graph_revision, hypothesis_id) of the most-recently-added hypothesis carrying
    a non-empty ``graph_revision`` — the revision the ledger's current advice is grounded
    on. ("", "") when none exists."""
    for slot in reversed(state.hypothesis_slots):
        if isinstance(slot, dict) and str(slot.get("graph_revision") or ""):
            return str(slot["graph_revision"]), str(slot.get("id", ""))
    return "", ""


def classify_stale_graph(state: EpisodeState, event: LedgerEvent) -> Advisory | None:
    """The event's CURRENT graph revision differs from the revision the active hypothesis's
    facts were gathered under — the advice's grounding is stale, so refresh the graph facts
    BEFORE advising. Correct-or-quiet: quiet when the event carries no revision, when no
    hypothesis records a revision, or when the revisions match."""
    cur = event.graph_revision
    if not cur:
        return None
    ref_rev, hyp_id = _latest_hypothesis_revision(state)
    if not ref_rev:
        return None  # no grounding revision to compare against
    if ref_rev == cur:
        return None
    statement = (
        f'the current graph revision ("{cur}") differs from the revision ("{ref_rev}") '
        f"the active hypothesis's facts were gathered under; the grounding is stale — "
        f"refresh the graph facts before advising."
    )
    evidence = [f"graph_revision:{ref_rev}", f"graph_revision:{cur}"]
    if hyp_id:
        evidence.append(f"hypothesis:{hyp_id}")
    return _advisory(
        T_STALE_GRAPH,
        D_REFRESH_BEFORE_ADVICE,
        WARNING,
        statement,
        evidence,
    )


# The transition table, in a FIXED deterministic evaluation order (name -> classifier).
TRANSITIONS: tuple[tuple[str, Any], ...] = (
    (T_ENV_FAILURE, classify_env_failure),
    (T_STALE_GRAPH, classify_stale_graph),
    (T_EDIT_CONTRADICTED_CONTRACT, classify_edit_contradicted_contract),
    (T_SAME_FAILURE_UNCHANGED_PATCH, classify_same_failure_unchanged_patch),
    (T_NO_DISCRIMINATING_EVIDENCE, classify_no_discriminating_evidence),
    (T_SAME_COMMAND_NEW_OUTPUT, classify_same_command_new_output),
)


def classify_all(state: EpisodeState, event: LedgerEvent) -> list[Advisory]:
    """Run every transition classifier over ``(state, event)`` in the fixed
    :data:`TRANSITIONS` order; return the non-None advisories (the typed recovery layer a
    consumer — e.g. the governor — can later act on). Deterministic; correct-or-quiet
    per classifier (an event with no matching evidence yields an empty list).

    ISOLATION (F2 belt — the normalizing ingress is the braces): a classifier that
    raises on adversarial state is SKIPPED, never propagated — one malformed ledger
    entry must not kill the whole recovery layer. Deterministic on deterministic
    input (the same state raises the same way every time)."""
    out: list[Advisory] = []
    for _name, fn in TRANSITIONS:
        try:
            adv = fn(state, event)
        except Exception:  # noqa: BLE001 — correct-or-quiet beats crash-not-quiet
            continue
        if adv is not None:
            out.append(adv)
    return out
