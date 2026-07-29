"""GT_OBLIGATIONS_V2 — T3 integration (plan §8 t13/14/16/18/19).

Drives the REAL artifact->loader->statuses->render->dose pipeline inside
gt_mini_patch with clauses extracted from the REAL true-myth v1.0.0 issue:
the filterMap clause must be flagged unexercised when test evidence names
everything BUT filterMap (the 189/192 loss, replayed at the unit seam)."""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

_FIX = Path(__file__).parent / "pretask" / "fixtures" / "obligations_v2"
TRUE_MYTH = (_FIX / "true-myth-iterable-collection-combinators.md").read_text(
    encoding="utf-8"
)


@pytest.fixture()
def gmp(monkeypatch, tmp_path):
    """Import gt_mini_patch fresh-ish with GT_CERT_DIR at tmp and clean state."""
    monkeypatch.setenv("GT_CERT_DIR", str(tmp_path))
    monkeypatch.setenv("GT_OBLIGATIONS_V2", "1")
    repo = str(Path(__file__).resolve().parents[1])
    for p in (repo, str(Path(repo) / "artifact_deepswe")):
        if p not in sys.path:
            sys.path.insert(0, p)
    import gt_mini_patch as m  # noqa: PLC0415
    importlib.reload(m) if getattr(m, "_obligations_v2_cache", None) else None
    # `_GT_BASELINE` is latched at IMPORT time, and tests/test_oracle_stage3_obligations.py /
    # test_oracle_live_obligation_wiring.py load the oracle modules under GT_BASELINE=1 —
    # which is the first import of gt_mini_patch in suite order, so the whole module stays
    # baseline-pinned for the rest of the process. `_unexercised_clause_candidate` returns
    # None immediately when it is set (gt_mini_patch.py:9303), which silently turned 7 of
    # these GT-ON assertions into "no candidate" instead of a real verdict. These tests are
    # about GT-ON behaviour by construction, so the fixture pins the flag rather than
    # inheriting whatever a neighbour's import happened to latch.
    monkeypatch.setattr(m, "_GT_BASELINE", False, raising=False)
    # the plan-load marker is a one-shot latch; a per-test fixture must un-latch it or only
    # the first test in the process ever sees the row.
    monkeypatch.setattr(m, "_oblig_plan_marker_emitted", False, raising=False)
    m._obligations_v2_cache = None
    m._unexercised_emitted.clear()
    m._unexercised_late_suppressed.clear()
    m._oracle_tested_tokens.clear()
    m._oracle_edited_tokens.clear()
    m._ss_behavioral_probe_events.clear()
    m._ss_edit_proof_events.clear()
    m._action_count = 0
    return m, tmp_path


def _write_artifact(tmp_path: Path) -> None:
    from groundtruth.pretask.spec import extract_spec_v2
    spec = extract_spec_v2(TRUE_MYTH)
    payload = {
        "obligations_version": 2,
        "render_path_tokens": [],
        "clauses": spec.to_serializable(version=2),
    }
    (tmp_path / "gt_obligations_v2.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


def _write_single_clause_artifact(
    tmp_path: Path, verbatim: str, *, clause_id: str = "clause-1",
    subject_symbols: tuple[str, ...] = (),
) -> None:
    payload = {
        "obligations_version": 2,
        "issue_sha256": "a" * 64,
        "render_path_tokens": [],
        "clauses": [{
            "clause_id": clause_id,
            "verbatim_text": verbatim,
            "subject_symbols": list(subject_symbols),
            "symbols": [],
        }],
    }
    (tmp_path / "gt_obligations_v2.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


# The one-shot `obligation.plan` load marker (gt_mini_patch._note_obligation_plan, added
# 2026-07-29 to make obligations eligibility three-valued) writes an INTERNAL-ONLY row at
# artifact-LOAD time — zero model bytes, `suppressed_internal_only`, and deliberately in NO
# layer->fact-class map so it can never be counted as production. Every assertion below is
# about SUPPRESSION ATTRIBUTION, so it must read the obligation rows, not the raw file: the
# old `not ledger.exists()` / whole-file equality checks used "the ledger is empty" as a
# proxy for "nothing was attributed", and that proxy stopped holding the moment a legitimate
# non-attributing row started being written. `test_plan_marker_is_internal_only_and_byteless`
# pins the properties that make skipping it correct rather than convenient.
_PLAN_MARKER_LAYER = "obligation.plan"


def _obligation_rows(ledger: Path) -> list[dict]:
    """Ledger rows EXCLUDING the internal-only plan-load marker. Missing file == no rows."""
    if not ledger.exists():
        return []
    return [
        row
        for row in (
            json.loads(line)
            for line in ledger.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
        if row.get("layer") != _PLAN_MARKER_LAYER
    ]


def _plan_marker_rows(ledger: Path) -> list[dict]:
    if not ledger.exists():
        return []
    return [
        row
        for row in (
            json.loads(line)
            for line in ledger.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
        if row.get("layer") == _PLAN_MARKER_LAYER
    ]


_CONAN_CLAUSE = "Support inverse matching with .conanignore"
_CONAN_COMMAND = """python3 - <<'PY'
# Exercise inverse matching for conanignore rules.
print(f'keep: {matches("keep")}')  # False
print(f'drop: {matches("drop")}')  # True
PY"""
_CONAN_OUTPUT = "keep: False\ndrop: True"

_BABEL_CLAUSE = "I would expect this to be W53 of 2023 or possible W1 of 2024"
_BABEL_COMMAND = '''python3 -c "
foo = babel.dates.format_date(left)
bar = babel.dates.format_date(right)
print('en_US:', repr(foo))  # W1 2024
print('en_AU:', repr(bar))  # W53 2023
"'''
_BABEL_OUTPUT = "en_US: 'W1 2024'\nen_AU: 'W53 2023'"


# ── t13: the filterMap replay at the unit seam ───────────────────────────────
def test_t3_filtermap_flagged_unexercised(gmp):
    m, tmp = gmp
    _write_artifact(tmp)
    # the real run's shape: test evidence names other combinators, never filterMap
    m._oracle_tested_tokens.update(
        {"traverse", "partition", "sequence", "zipWith", "retryN", "vitest"}
    )
    got = m._unexercised_clause_candidate()
    assert got is not None
    block = got[1]
    assert "normative requirement truth" in block
    assert "[not exercised]" in block
    assert "<gt-" not in block
    assert not m._V2_LEAK_TEST_RE.search(block.replace("test/run output", ""))


# ── t14: exercising the clause silences it ───────────────────────────────────
def test_t3_token_mention_alone_does_not_retire_clause(gmp):
    m, tmp = gmp
    _write_artifact(tmp)
    m._oracle_tested_tokens.add("test_filtermap")  # substring credit (compound)
    got = m._unexercised_clause_candidate()
    assert got is not None
    assert "filterMap" in got[1]


def test_t3_fresh_behavioral_proof_filters_only_proven_clause(
    gmp, monkeypatch
):
    m, tmp = gmp
    monkeypatch.setenv("GT_SS_LATE_DROP", "1")
    monkeypatch.setenv("GT_RUNTIME_LEDGER", str(tmp / "ledger.jsonl"))
    payload = {
        "obligations_version": 2,
        "issue_sha256": "a" * 64,
        "render_path_tokens": [],
        "clauses": [
            {
                "verbatim_text": "TypeError must reject categorical values",
                "subject_symbols": ["TypeError"],
                "symbols": ["TypeError"],
            },
            {
                "verbatim_text": "render_predictions must support grouped values",
                "subject_symbols": ["render_predictions"],
                "symbols": ["render_predictions"],
            },
        ],
    }
    (tmp / "gt_obligations_v2.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    m._action_count = 8
    m._ss_record_behavioral_proof(
        command="python -m pytest -k TypeError",
        output="1 passed in 0.02s",
        returncode=0,
    )

    got = m._unexercised_clause_candidate()

    assert got is not None
    assert "TypeError" not in got[1]
    assert "render_predictions" in got[1]
    rows = _obligation_rows(tmp / "ledger.jsonl")
    assert [(row["layer"], row["reason"]) for row in rows] == [
        ("obligation.unexercised", "ss_late")
    ]
    assert rows[0]["artifact_issue_sha256"] == "a" * 64
    assert rows[0]["proof_turn"] == 8


def test_plan_marker_is_internal_only_and_byteless(gmp, monkeypatch):
    """The property that makes `_obligation_rows` skipping the plan marker CORRECT rather
    than convenient. The marker exists to make obligations eligibility three-valued, so it
    must fire at LOAD time — but it must own zero model bytes, report itself suppressed,
    and stay out of `_LAYER_TO_FACT_CLASS` so `classify_ledger` can never count it as
    production. If any of that ever changes, the skip is hiding a real delivery."""
    m, tmp = gmp
    ledger = tmp / "ledger.jsonl"
    monkeypatch.setenv("GT_RUNTIME_LEDGER", str(ledger))
    _write_single_clause_artifact(tmp, _CONAN_CLAUSE)

    m._unexercised_clause_candidate()

    markers = _plan_marker_rows(ledger)
    assert len(markers) == 1, "the load marker is one-shot per task (latched)"
    assert markers[0]["reason"] == "obligation_plan_loaded"
    assert markers[0]["outcome"] == "suppressed_internal_only"
    assert markers[0]["chars_delivered"] == 0
    assert _PLAN_MARKER_LAYER not in m._LAYER_TO_FACT_CLASS


def test_t3_failed_exercise_is_unproven_and_never_manufactures_suppression(
    gmp, monkeypatch
):
    m, tmp = gmp
    monkeypatch.setenv("GT_SS_LATE_DROP", "1")
    ledger = tmp / "ledger.jsonl"
    monkeypatch.setenv("GT_RUNTIME_LEDGER", str(ledger))
    payload = {
        "obligations_version": 2,
        "issue_sha256": "a" * 64,
        "render_path_tokens": [],
        "clauses": [{
            "verbatim_text": "filterMap must preserve mapped values",
            "subject_symbols": ["filterMap"],
            "symbols": ["filterMap"],
        }],
    }
    (tmp / "gt_obligations_v2.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    m._action_count = 8
    m._ss_record_behavioral_proof(
        command="python -m pytest -k filterMap",
        output="1 failed in 0.02s",
        returncode=1,
    )

    candidate = m._unexercised_clause_candidate()
    assert candidate is not None
    assert "[exercised, result unproven]" in candidate[1]
    assert _obligation_rows(ledger) == []


def test_t3_new_proof_generation_records_new_suppression(gmp, monkeypatch):
    m, tmp = gmp
    monkeypatch.setenv("GT_SS_LATE_DROP", "1")
    ledger = tmp / "ledger.jsonl"
    monkeypatch.setenv("GT_RUNTIME_LEDGER", str(ledger))
    payload = {
        "obligations_version": 2,
        "issue_sha256": "a" * 64,
        "render_path_tokens": [],
        "clauses": [{
            "verbatim_text": "TypeError must reject categorical values",
            "subject_symbols": ["TypeError"],
            "symbols": ["TypeError"],
        }],
    }
    (tmp / "gt_obligations_v2.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    def record_proof(turn):
        m._action_count = turn
        m._ss_record_behavioral_proof(
            command="python -m pytest -k TypeError",
            output="1 passed in 0.02s",
            returncode=0,
        )

    record_proof(8)
    assert m._unexercised_clause_candidate() is None
    assert m._unexercised_clause_candidate() is None
    record_proof(10)
    assert m._unexercised_clause_candidate() is None

    rows = _obligation_rows(ledger)
    assert [row["proof_turn"] for row in rows] == [8, 10]


def test_t3_checked_receiver_matrix_retires_rhetorical_behavior_clause(gmp):
    m, _tmp = gmp
    view = m._V2ClauseView(0, {
        "verbatim_text": (
            "The rules would be more useful if they supported inverse matching, "
            "just like another ignore format does"
        ),
        "subject_symbols": [],
        "symbols": [],
    })
    m._action_count = 8
    m._ss_record_behavioral_proof(
        command="""python3 - <<'PY'
class PolicyMatcher:
    def matches(self, value):
        return value != 'keep'
matcher = PolicyMatcher()
# Inverse matching behavior
print(f'keep: {matcher.matches("keep")}')  # False
print(f'drop: {matcher.matches("drop")}')  # True
PY""",
        output="keep: False\ndrop: True",
        returncode=0,
    )

    proof = m._v2_clause_fresh_behavioral_proof(view)

    assert proof is not None
    assert proof["proof_turn"] == 8


def test_t3_assigned_call_outputs_cover_literal_alternatives(gmp):
    m, _tmp = gmp
    view = m._V2ClauseView(0, {
        "verbatim_text": "I expect result A 2023 or possibly result B 2024",
        "subject_symbols": [],
        "symbols": [],
    })
    command = '''python3 -c "
first = render_value('left')
second = render_value('right')
print('left:', repr(first))  # expected result A 2023
print('right:', repr(second))  # possible result B 2024
"'''
    m._action_count = 9
    m._ss_record_behavioral_proof(
        command=command,
        output="left: 'result B 2024'\nright: 'result A 2023'",
        returncode=0,
    )

    assert m._v2_clause_fresh_behavioral_proof(view) is not None

    m._ss_behavioral_probe_events.clear()
    m._ss_record_behavioral_proof(
        command=command,
        output="left: 'unrelated'\nright: 'result A 2023'",
        returncode=0,
    )
    assert m._v2_clause_fresh_behavioral_proof(view) is None


def test_t3_disjunctive_alternatives_accept_repeated_allowed_result(gmp):
    """Every checked case may resolve to the same member of an allowed A-or-B set."""
    m, _tmp = gmp
    view = m._V2ClauseView(0, {
        "verbatim_text": "I expect result A 2023 or possibly result B 2024",
        "subject_symbols": [],
        "symbols": [],
    })
    command = '''python3 -c "
first = render_value('left')
second = render_value('right')
print('left:', repr(first))
print('right:', repr(second))
"'''
    m._action_count = 9
    m._ss_record_behavioral_proof(
        command=command,
        output="left: 'result A 2023'\nright: 'result A 2023'",
        returncode=0,
    )

    assert m._v2_clause_fresh_behavioral_proof(view) is not None


def test_t3_clause_relevant_custom_result_is_a_semantic_verify_boundary(gmp):
    m, tmp = gmp
    _write_single_clause_artifact(
        tmp, "`render_value` must preserve grouped values",
        subject_symbols=("render_value",),
    )
    m._action_count = 12
    m._ss_record_behavioral_proof(
        command="python3 -c \"print(render_value('grouped'))\"",
        output="grouped",
        returncode=0,
    )

    assert m._v2_obligation_result_ready() is True
    candidate = m._unexercised_clause_candidate()
    assert candidate is not None
    assert "exercised, result unproven" in candidate[1]


def test_t3_unrelated_success_is_not_a_semantic_verify_boundary(gmp):
    m, tmp = gmp
    _write_single_clause_artifact(
        tmp, "`render_value` must preserve grouped values",
        subject_symbols=("render_value",),
    )
    m._action_count = 12
    m._ss_record_behavioral_proof(
        command="python3 -c \"print(6 * 7)\"",
        output="42",
        returncode=0,
    )

    assert m._v2_obligation_result_ready() is False


@pytest.mark.parametrize(
    ("clause", "command", "output"),
    [
        (_CONAN_CLAUSE, _CONAN_COMMAND, _CONAN_OUTPUT),
        (_BABEL_CLAUSE, _BABEL_COMMAND, _BABEL_OUTPUT),
    ],
)
def test_t3_dynamic_probe_cannot_promote_subjectless_clause(
    gmp, monkeypatch, clause, command, output
):
    m, tmp = gmp
    monkeypatch.setenv("GT_SS_LATE_DROP", "1")
    ledger = tmp / "ledger.jsonl"
    monkeypatch.setenv("GT_RUNTIME_LEDGER", str(ledger))
    _write_single_clause_artifact(tmp, clause)
    m._action_count = 8
    m._ss_record_behavioral_proof(
        command=command, output=output, returncode=0
    )

    truth = m._v2_obligation_truth()
    assert truth is not None
    from groundtruth.runtime.obligations import ObligationTruthState
    assert truth[0].state is ObligationTruthState.UNVERIFIABLE
    candidate = m._unexercised_clause_candidate()
    assert candidate is not None
    assert "could not be verified automatically" in candidate[1]
    assert _obligation_rows(ledger) == []


def test_unexercised_delivery_row_carries_typed_obligation_lineage(gmp):
    m, _tmp = gmp
    extra = m._lane_delivery_extra(
        "obligation.unexercised", "verify the reported requirement", "",
        m.Event.TEST_RESULT,
    )
    assert extra["producer_registration_match"] is True
    assert extra["fact_class"] == "obligations"
    assert extra["actual_event"] == "test_result"
    assert extra["required_event"] == "test_result"
    assert extra["feature_ids"] == [
        {"category": "FACT", "feature_id": "obligations", "role": "fact"}
    ]


@pytest.mark.parametrize(
    ("command", "output", "returncode"),
    [
        (_CONAN_COMMAND, _CONAN_OUTPUT, 1),
        ("python3 -c \"print('inverse matching conanignore')\"", _CONAN_OUTPUT, 0),
        (_CONAN_COMMAND, "keep: True\ndrop: False", 0),
        (_BABEL_COMMAND.replace("print('en_AU:', repr(bar))  # W53 2023", ""),
         _BABEL_OUTPUT, 0),
        (_BABEL_COMMAND, "en_US: 'W1 2024'\nen_AU: 'W2 2024'", 0),
    ],
)
def test_t3_unverifiable_clause_requires_strict_checked_dynamic_proof(
    gmp, monkeypatch, command, output, returncode
):
    m, tmp = gmp
    monkeypatch.setenv("GT_SS_LATE_DROP", "1")
    ledger = tmp / "ledger.jsonl"
    monkeypatch.setenv("GT_RUNTIME_LEDGER", str(ledger))
    clause = _BABEL_CLAUSE if "babel.dates" in command else _CONAN_CLAUSE
    _write_single_clause_artifact(tmp, clause)
    m._action_count = 8
    m._ss_record_behavioral_proof(
        command=command, output=output, returncode=returncode
    )

    assert m._unexercised_clause_candidate() is not None
    assert _obligation_rows(ledger) == []


def test_t3_true_false_literals_are_not_semantic_relation_terms(gmp):
    m, _tmp = gmp
    view = m._V2ClauseView(0, {
        "verbatim_text": "Values can be true or false",
        "subject_symbols": [],
        "symbols": [],
    })
    unrelated = """python3 - <<'PY'
# true false
print(f'left: {check_left()}')  # True
print(f'right: {check_right()}')  # False
PY"""

    assert "true" not in m._v2_proof_terms(view.verbatim)
    assert "false" not in m._v2_proof_terms(view.verbatim)
    assert not m._v2_checked_probe_covers_clause(
        view, unrelated, "left: True\nright: False", 0
    )
    m._action_count = 8
    m._ss_record_behavioral_proof(
        command=unrelated, output="left: True\nright: False", returncode=0
    )
    assert m._v2_clause_fresh_behavioral_proof(view) is None


def test_t3_clause_comment_cannot_bind_unrelated_boolean_matrix(gmp):
    m, _tmp = gmp
    view = m._V2ClauseView(0, {
        "verbatim_text": "Support inverse matching with .conanignore",
        "subject_symbols": [],
        "symbols": [],
    })
    unrelated = """python3 - <<'PY'
# Support inverse matching with .conanignore
print(f'left: {len("keep")}')  # True
print(f'right: {len("drop")}')  # False
PY"""

    assert not m._v2_checked_probe_covers_clause(
        view, unrelated, "left: True\nright: False", 0
    )


def test_t3_hard_coded_alternative_labels_cannot_bind_unrelated_results(gmp):
    m, _tmp = gmp
    view = m._V2ClauseView(0, {
        "verbatim_text": "I would expect this to be W53 of 2023 or possible W1 of 2024",
        "subject_symbols": [],
        "symbols": [],
    })
    unrelated = '''python3 -c "
first = len('left')
second = len('right')
print('W1 2024:', first)
print('W53 2023:', second)
"'''

    assert not m._v2_checked_probe_covers_clause(
        view, unrelated, "W1 2024: 4\nW53 2023: 5", 0
    )
    m._action_count = 8
    m._ss_record_behavioral_proof(
        command=unrelated, output="W1 2024: 4\nW53 2023: 5", returncode=0
    )
    assert m._v2_clause_fresh_behavioral_proof(view) is None


def test_t3_dynamic_proof_before_relevant_edit_is_stale(gmp, monkeypatch):
    m, tmp = gmp
    monkeypatch.setenv("GT_SS_LATE_DROP", "1")
    ledger = tmp / "ledger.jsonl"
    monkeypatch.setenv("GT_RUNTIME_LEDGER", str(ledger))
    _write_single_clause_artifact(tmp, _CONAN_CLAUSE)
    m._action_count = 8
    m._ss_record_behavioral_proof(
        command=_CONAN_COMMAND, output=_CONAN_OUTPUT, returncode=0
    )
    m._ss_edit_proof_events.append((
        "pkg/policy.py",
        9,
        "cat > pkg/policy.py <<'PY'\n# inverse matching conanignore\n"
        "def inverse_matching_conanignore():\n    pass\nPY",
        True,
    ))

    assert m._unexercised_clause_candidate() is not None
    assert _obligation_rows(ledger) == []


def test_t3_subjectless_probe_does_not_attribute_resurface_silence(
    gmp, monkeypatch
):
    m, tmp = gmp
    monkeypatch.setenv("GT_SS_LATE_DROP", "1")
    ledger = tmp / "ledger.jsonl"
    monkeypatch.setenv("GT_RUNTIME_LEDGER", str(ledger))
    _write_single_clause_artifact(tmp, _BABEL_CLAUSE)
    m._action_count = 8
    m._ss_record_behavioral_proof(
        command=_BABEL_COMMAND, output=_BABEL_OUTPUT, returncode=0
    )

    m._v2_attribute_resurface_silence()

    assert _obligation_rows(ledger) == []


def test_t3_unverifiable_dynamic_proof_is_inert_when_late_drop_off(
    gmp, monkeypatch
):
    m, tmp = gmp
    ledger = tmp / "ledger.jsonl"
    monkeypatch.setenv("GT_RUNTIME_LEDGER", str(ledger))
    _write_single_clause_artifact(tmp, _CONAN_CLAUSE)
    m._action_count = 8
    m._ss_record_behavioral_proof(
        command=_CONAN_COMMAND, output=_CONAN_OUTPUT, returncode=0
    )

    got = m._unexercised_clause_candidate()

    assert got is not None
    assert "could not be verified automatically" in got[1]
    assert _obligation_rows(ledger) == []


def test_t3_resurface_boundary_attributes_earned_v2_silence(gmp, monkeypatch):
    m, tmp = gmp
    monkeypatch.setenv("GT_SS_LATE_DROP", "1")
    ledger = tmp / "ledger.jsonl"
    monkeypatch.setenv("GT_RUNTIME_LEDGER", str(ledger))
    payload = {
        "obligations_version": 2,
        "issue_sha256": "a" * 64,
        "render_path_tokens": [],
        "clauses": [{
            "clause_id": "clause-1",
            "verbatim_text": "render_value must preserve grouped values",
            "subject_symbols": ["render_value"],
            "symbols": ["render_value"],
        }],
    }
    (tmp / "gt_obligations_v2.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    m._action_count = 8
    m._ss_record_behavioral_proof(
        command="python -m pytest -k render_value",
        output="1 passed in 0.02s",
        returncode=0,
    )
    m._oracle_edited_tokens.add("render_value")
    m._action_count = 12

    m._v2_attribute_resurface_silence()

    rows = _obligation_rows(ledger)
    assert len(rows) == 1
    assert rows[0]["layer"] == "obligation.resurface"
    assert rows[0]["reason"] == "ss_late"
    assert rows[0]["chars_delivered"] == 0
    assert rows[0]["clause_id"] == "clause-1"
    assert rows[0]["artifact_issue_sha256"] == "a" * 64
    assert rows[0]["boundary"] == "post_edit"


def test_t3_resurface_silence_is_byte_and_ledger_inert_when_late_drop_off(
    gmp, monkeypatch
):
    m, tmp = gmp
    ledger = tmp / "ledger.jsonl"
    monkeypatch.setenv("GT_RUNTIME_LEDGER", str(ledger))
    monkeypatch.setattr(
        m,
        "_v2_obligation_truth",
        lambda: (_ for _ in ()).throw(AssertionError("flag-off must not inspect V2")),
    )
    observation = {"output": "native bytes"}

    m._v2_attribute_resurface_silence()

    assert observation == {"output": "native bytes"}
    assert _obligation_rows(ledger) == []


# ── t16: inactive without the artifact — v1 site behavior preserved ──────────
def test_t3_inactive_without_artifact(gmp):
    m, _tmp = gmp
    assert m._load_obligations_v2() is None
    assert m._unexercised_clause_candidate() is None


# ── t18: dose — at most 2 emissions, deduped by status vector ────────────────
def test_t3_dose_max_two_and_dedup(gmp):
    m, tmp = gmp
    payload = {
        "obligations_version": 2,
        "render_path_tokens": [],
        "clauses": [{
            "clause_id": "filter-map",
            "verbatim_text": "filterMap must preserve mapped values",
            "subject_symbols": ["filterMap"],
            "symbols": ["filterMap"],
        }],
    }
    (tmp / "gt_obligations_v2.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    first = m._unexercised_clause_candidate()
    assert first is not None
    assert m._unexercised_clause_candidate() is None  # same vector -> dedup
    m._action_count = 4
    m._ss_record_behavioral_proof(
        command="python -m pytest -k filterMap",
        output="1 failed in 0.02s",
        returncode=1,
    )
    second = m._unexercised_clause_candidate()
    assert second is not None
    m._action_count = 5
    m._ss_record_behavioral_proof(
        command="python -m pytest -k filterMap",
        output="1 passed in 0.02s",
        returncode=0,
    )
    assert m._unexercised_clause_candidate() is None  # hard cap 2


# ── reset law: latch + cache clear on _reset_oracle_state ────────────────────
def test_t3_reset_law_clears_latch_and_cache(gmp):
    m, tmp = gmp
    _write_artifact(tmp)
    assert m._unexercised_clause_candidate() is not None
    assert m._unexercised_emitted
    m._reset_oracle_state()
    assert not m._unexercised_emitted
    assert not m._unexercised_late_suppressed
    assert m._obligations_v2_cache is None


# ── leak screen: render_path_tokens drop rows whole ──────────────────────────
def test_t3_path_token_leak_drops_row(gmp):
    m, tmp = gmp
    payload = {
        "obligations_version": 2,
        "render_path_tokens": ["goldstem"],
        "clauses": [{
            "verbatim_text": "the goldstem module must always frobnicate",
            "kind": "behavior", "symbols": ["frobnicate_fn"],
            "subject_symbols": ["frobnicate_fn"], "keywords": [],
            "checkable_forms": [], "clause_id": "z", "modality": "mandatory",
            "modality_strength": 3, "parent_id": "", "part_index": 0,
        }],
    }
    (tmp / "gt_obligations_v2.json").write_text(json.dumps(payload), encoding="utf-8")
    got = m._unexercised_clause_candidate()
    assert got is None  # only row leaks -> whole block silent (fail closed)


# ── t19: coverage_v2 lands in the persisted status artifact ──────────────────
def test_t3_coverage_v2_in_status_artifact(gmp, monkeypatch):
    m, tmp = gmp
    _write_artifact(tmp)
    status_path = tmp / "obligation_status.json"
    monkeypatch.setenv("GT_OBLIGATION_STATUS", str(status_path))
    monkeypatch.setenv("GT_ORACLE_EVENTS", str(tmp / "ev.jsonl"))

    class _Tr:
        def coverage_ratio(self):
            return 0.5
        def snapshot(self):
            return []

    m._persist_obligation_status(_Tr(), turn=1)
    snap = json.loads(status_path.read_text(encoding="utf-8"))
    assert snap["coverage_v2"]["coverage_version"] == 3
    assert "proof_coverage" in snap["coverage_v2"]
    assert "n_exercised_unproven" in snap["coverage_v2"]
