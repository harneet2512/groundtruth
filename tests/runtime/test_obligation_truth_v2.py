from __future__ import annotations

from dataclasses import dataclass

from groundtruth.pretask.spec import classify_spec_v2_regions, extract_spec_v2
from groundtruth.runtime import obligations as ob


@dataclass(frozen=True)
class _View:
    idx: int
    verbatim: str
    subject_symbols: frozenset[str]
    sym_parts: frozenset[str]
    region: str = "normative"


def _view(idx: int, subject: str, *, region: str = "normative") -> _View:
    return _View(
        idx=idx,
        verbatim=f"`{subject}` must preserve the configured behavior",
        subject_symbols=frozenset({subject}),
        sym_parts=frozenset({subject}),
        region=region,
    )


def test_spec_v2_classifies_structural_regions_without_promoting_context() -> None:
    issue = """### Describe the bug
`parse_item` currently drops empty values.

### To reproduce
Run `parse_item` against an empty fixture.

### Expected behavior
`parse_item` must preserve empty values.
"""

    regions = classify_spec_v2_regions(issue)

    assert [(region.kind, region.heading) for region in regions] == [
        ("evidence", "Describe the bug"),
        ("process", "To reproduce"),
        ("normative", "Expected behavior"),
    ]
    rows = extract_spec_v2(issue).to_serializable(version=2)
    assert [row["verbatim_text"] for row in rows] == [
        "`parse_item` must preserve empty values"
    ]
    assert rows[0]["region"] == "normative"


def test_extractor_stamps_the_actual_structural_source_region() -> None:
    issue = """### Describe the bug
`parse_item` must currently be called twice to preserve empty values.

### Expected behavior
`parse_item` must preserve empty values in one call.
"""

    rows = extract_spec_v2(issue).to_serializable(version=2)

    assert [(row["region"], row["verbatim_text"]) for row in rows] == [
        ("evidence", "`parse_item` must currently be called twice to preserve empty values"),
        ("normative", "`parse_item` must preserve empty values in one call"),
    ]


def test_failed_related_execution_is_exercise_evidence_not_behavioral_proof() -> None:
    command = "python -m pytest tests/test_parser.py -k parse_item"
    output = "test_parse_item FAILED\n1 failed in 0.04s"

    evidence = ob.classify_exercise_evidence(
        command, output, 1, {"parse_item"}, turn=7
    )
    proof = ob.classify_checked_behavioral_proof(
        command, output, 1, {"parse_item"}, turn=7
    )

    assert evidence == ob.ExerciseEvidence(
        frozenset({"parse_item"}), 7, "related_execution", 1
    )
    assert proof is None


def test_shared_truth_projection_drives_tri_state_render_and_metrics() -> None:
    views = [_view(0, "alpha_fn"), _view(1, "beta_fn"), _view(2, "gamma_fn")]
    exercise = ob.ExerciseEvidence(
        frozenset({"beta_fn"}), 5, "related_execution", 1
    )
    proven_exercise = ob.ExerciseEvidence(
        frozenset({"gamma_fn"}), 6, "related_execution", 0
    )
    proof = ob.BehavioralProof(frozenset({"gamma_fn"}), 6, "checked_result_matrix")

    truth = ob.obligation_truth_statuses(
        views,
        edited_tokens={"alpha_fn", "beta_fn", "gamma_fn"},
        exercise_evidence=[exercise, proven_exercise],
        behavioral_proofs=[proof],
    )

    assert [row.state for row in truth] == [
        ob.ObligationTruthState.UNEXERCISED,
        ob.ObligationTruthState.EXERCISED_UNPROVEN,
        ob.ObligationTruthState.PROVEN,
    ]
    block = ob.render_obligation_truth_block(truth)
    assert "[not exercised]" in block
    assert "[exercised, result unproven]" in block
    assert "gamma_fn" not in block
    summary = ob.obligation_truth_summary(truth)
    assert summary["n_unexercised"] == 1
    assert summary["n_exercised_unproven"] == 1
    assert summary["n_proven"] == 1
    assert summary["proof_coverage"] == 1 / 3


def test_nonnormative_regions_never_enter_delivery_or_metric_denominator() -> None:
    truth = ob.obligation_truth_statuses(
        [
            _view(0, "required_fn"),
            _view(1, "repro_fn", region="process"),
            _view(2, "observed_fn", region="evidence"),
        ],
        edited_tokens=set(),
        exercise_evidence=[],
        behavioral_proofs=[],
    )

    assert [row.view.idx for row in truth] == [0]
    assert ob.obligation_truth_summary(truth)["n_normative"] == 1


def test_high_specificity_subject_subset_covers_broader_clause() -> None:
    view = _View(
        idx=0,
        verbatim="`parse_item` must preserve `EmptyValue` instances",
        subject_symbols=frozenset({"parse_item", "EmptyValue"}),
        sym_parts=frozenset({"parse_item", "EmptyValue"}),
    )
    exercise = ob.ExerciseEvidence(
        frozenset({"parse_item"}), 4, "related_execution", 1
    )
    proof = ob.BehavioralProof(
        frozenset({"parse_item"}), 5, "direct_checked_calls"
    )

    attempted = ob.obligation_truth_statuses(
        [view], set(), [exercise], []
    )
    proven = ob.obligation_truth_statuses(
        [view], set(), [exercise], [proof]
    )

    assert attempted[0].state is ob.ObligationTruthState.EXERCISED_UNPROVEN
    assert proven[0].state is ob.ObligationTruthState.PROVEN


def test_plain_word_subset_does_not_cover_a_broader_clause() -> None:
    view = _View(
        idx=0,
        verbatim="The parser must preserve behavior",
        subject_symbols=frozenset({"parser", "behavior"}),
        sym_parts=frozenset({"parser", "behavior"}),
    )
    evidence = ob.ExerciseEvidence(
        frozenset({"parser"}), 4, "related_execution", 0
    )

    truth = ob.obligation_truth_statuses([view], set(), [evidence], [])

    assert truth[0].state is ob.ObligationTruthState.UNEXERCISED


def test_symbol_free_normative_clause_is_unverifiable_not_unexercised() -> None:
    view = _View(
        idx=0,
        verbatim="Registered objects must remain paired one to one",
        subject_symbols=frozenset(),
        sym_parts=frozenset(),
    )

    truth = ob.obligation_truth_statuses([view], set(), [], [])

    assert truth[0].state is ob.ObligationTruthState.UNVERIFIABLE
    block = ob.render_obligation_truth_block(truth)
    assert "could not be verified automatically" in block
    assert "[not exercised]" not in block
    summary = ob.obligation_truth_summary(truth)
    assert summary["n_unverifiable"] == 1
    assert summary["n_verifiable"] == 0
    assert summary["proof_coverage"] == 1.0
