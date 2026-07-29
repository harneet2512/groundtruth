"""Run 30390877219 credibility defect: GT contradicting itself in one observation.

The witness (task ``amoffat__sh-744``, msg63) printed ``[not exercised]`` directly
beneath a green ``4 passed`` pytest result.  The self-contradiction CLASS this file
pins is the one that lives in ``obligation_truth_statuses``: a clause whose subject
set is a strict SUBSET of the subject set carried by the observed execution evidence
could never be credited, because the coverage predicate was exact SET EQUALITY.

Both halves are pinned here, because the loosening is only safe with its counterweight:

  1. broader evidence + an edit on the clause's own symbol -> PROVEN, nothing rendered;
  2. broader evidence + NO edit on the clause's symbols     -> EXERCISED_UNPROVEN,
     never PROVEN (the over-credit guard).

Direction matters and is NOT symmetric: evidence NARROWER than the clause still
credits nothing (pinned by ``test_partial_high_specificity_subject_set_does_not_cover_broader_clause``
in test_obligation_truth_v2.py).  Only evidence that names EVERY subject of the clause
may credit it.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO / "src") not in sys.path:
    sys.path.insert(0, str(_REPO / "src"))

from types import SimpleNamespace  # noqa: E402

from groundtruth.runtime.obligations import (  # noqa: E402
    BehavioralProof,
    ExerciseEvidence,
    ObligationTruthState,
    obligation_truth_statuses,
    render_obligation_truth_block,
)


def _clause(idx: int = 0, sym: str = "parse_url"):
    return SimpleNamespace(
        idx=idx,
        region="normative",
        verbatim=f"`{sym}` must preserve the configured behavior",
        subject_symbols=frozenset({sym}),
        sym_parts=frozenset({sym}),
    )


# the broad green run: its subject key names the clause's symbol AND more
_BROAD = frozenset({"parse_url", "normalize", "resolve"})


def test_broad_green_proof_naming_the_clause_symbol_proves_the_clause() -> None:
    view = _clause()
    exercise = ExerciseEvidence(_BROAD, 6, "related_execution", 0)
    proof = BehavioralProof(_BROAD, 6, "subject_bound_test_runner")

    rows = obligation_truth_statuses(
        [view],
        edited_tokens={"parse_url"},
        exercise_evidence=[exercise],
        behavioral_proofs=[proof],
    )

    assert rows[0].state is ObligationTruthState.PROVEN, (
        "a green execution that names the clause's own subject must not be "
        "reported as '[not exercised]' in the same observation")
    assert render_obligation_truth_block(rows) == ""


def test_broader_evidence_without_an_edit_stays_exercised_unproven() -> None:
    """Over-credit guard: widened credit requires the clause's own symbol to be edited."""
    view = _clause()
    exercise = ExerciseEvidence(_BROAD, 6, "related_execution", 0)
    proof = BehavioralProof(_BROAD, 6, "subject_bound_test_runner")

    rows = obligation_truth_statuses(
        [view],
        edited_tokens=set(),
        exercise_evidence=[exercise],
        behavioral_proofs=[proof],
    )

    assert rows[0].state is ObligationTruthState.EXERCISED_UNPROVEN
    assert "[exercised, result unproven]" in render_obligation_truth_block(rows)


def test_exact_subject_proof_still_proves_without_an_edit() -> None:
    """The pre-existing exact-match path is unchanged: no new edit precondition."""
    view = _clause()
    rows = obligation_truth_statuses(
        [view],
        edited_tokens=set(),
        exercise_evidence=[],
        behavioral_proofs=[
            BehavioralProof(frozenset({"parse_url"}), 6, "direct_checked_calls")
        ],
    )

    assert rows[0].state is ObligationTruthState.PROVEN


def test_narrower_evidence_still_credits_nothing() -> None:
    """Loosening is directional: evidence missing a clause subject never credits."""
    view = SimpleNamespace(
        idx=0,
        region="normative",
        verbatim="`parse_url` must preserve `EmptyValue` instances",
        subject_symbols=frozenset({"parse_url", "EmptyValue"}),
        sym_parts=frozenset({"parse_url", "EmptyValue"}),
    )
    rows = obligation_truth_statuses(
        [view],
        edited_tokens={"parse_url", "emptyvalue"},
        exercise_evidence=[
            ExerciseEvidence(frozenset({"parse_url"}), 6, "related_execution", 0)
        ],
        behavioral_proofs=[
            BehavioralProof(frozenset({"parse_url"}), 6, "direct_checked_calls")
        ],
    )

    assert rows[0].state is ObligationTruthState.UNEXERCISED


# ---------------------------------------------------------------------------
# CROSS-CLAUSE POOLING -- the shape that actually occurs in production.
#
# Added 2026-07-28 after a LIPI Logic pass falsified this file's original
# motivation. The subset widening was justified as repairing a same-clause
# ``len<3`` asymmetry between ``_credit_eligible_symbols`` (filters) and the
# evidence key (does not). That asymmetry is unreachable: the sole producer of
# ``subject_symbols`` (pretask/spec.py:593-615) already enforces >=3 on every
# branch.
#
# The live channel is different and broader. ``gt_mini_patch.py:8677-8709``
# builds evidence per clause, appends it all to ONE flat list, and passes that
# list plus EVERY view to ``obligation_truth_statuses``. So each clause is
# matched against every OTHER clause's evidence. Under ``<=`` a narrow clause
# is credited by a broad sibling's evidence -- the ordinary shape of two
# clauses drawn from one issue paragraph.
#
# Every test above this line uses a single view, so none of them exercised it.
# ---------------------------------------------------------------------------


def _sibling_clauses():
    """A narrow clause and a broad sibling, as one issue paragraph yields."""
    return [
        SimpleNamespace(
            idx=0, region="normative",
            verbatim="`parse_url` must reject empty input",
            subject_symbols={"parse_url"},
            sym_parts={"parse_url", "input"},
        ),
        SimpleNamespace(
            idx=1, region="normative",
            verbatim="`parse_url` and `normalize` must agree",
            subject_symbols={"parse_url", "normalize"},
            sym_parts={"parse_url", "normalize"},
        ),
    ]


def _sibling_proof():
    """A proof earned for the BROAD clause only."""
    return BehavioralProof(
        frozenset({"parse_url", "normalize"}), 6, "subject_bound_test_runner"
    )


def test_a_narrow_clause_inherits_a_broad_siblings_exercise():
    """THE INTENDED WIDENING. The sibling's test named every symbol the narrow
    clause is about, so it genuinely exercised it."""
    exercise = ExerciseEvidence(
        frozenset({"parse_url", "normalize"}), 6, "subject_bound_test_runner", 0
    )
    rows = obligation_truth_statuses(_sibling_clauses(), set(), [exercise], [])
    assert rows[0].state is ObligationTruthState.EXERCISED_UNPROVEN, (
        "the narrow clause was not credited by its broad sibling's exercise"
    )


def test_a_narrow_clause_does_NOT_inherit_PROVEN_on_a_sentence_word_edit():
    """THE OVER-CREDIT CHANNEL, closed.

    ``touched`` comes from ``overlap(view, edited)``, which reads
    ``view.sym_parts`` -- every >=3-char word of the clause's ENGLISH sentence
    -- against edit-command tokens accumulated over the whole episode. Gating
    the widened proof on it let clause 0 reach PROVEN because the word
    ``input`` appeared in some edit command. ``input`` is not one of clause 0's
    subject symbols. Demonstrated executing before the guard was scoped.
    """
    rows = obligation_truth_statuses(
        _sibling_clauses(), {"input"}, [], [_sibling_proof()]
    )
    assert rows[0].state is not ObligationTruthState.PROVEN, (
        "a sentence WORD promoted a clause to PROVEN on a sibling's proof"
    )
    assert rows[1].state is ObligationTruthState.PROVEN, (
        "the clause the proof was actually earned for lost its credit"
    )
    assert rows[0].edited is True, (
        "ObligationTruth.edited must keep its loose v1-compatible meaning -- "
        "only the PROVEN gate is subject-scoped"
    )


def test_a_narrow_clause_DOES_inherit_PROVEN_when_its_own_subject_was_edited():
    """The counterweight is a real signal, not a permanent block: changed
    symbols AND validation evidence is exactly the PROVEN bar."""
    rows = obligation_truth_statuses(
        _sibling_clauses(), {"parse_url"}, [], [_sibling_proof()]
    )
    assert rows[0].state is ObligationTruthState.PROVEN
