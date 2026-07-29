"""RED-first proof for the PRODUCER half of continuous endpoints: the live-opportunity export
must MEASURE continuous consequence fields (searches / edits / verifications / steps) over the
window from the opportunity boundary to the next decision commit, and attach them to each
exported observation as a ``consequences`` map.

Pristine HEAD has no ``_window_consequences`` and emits only the boolean receipt, so both tests
here fail on pristine (import error / missing key). The existing boolean export test is
untouched (the receipt fields are unchanged).
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

# scripts/swebench is not a package and is not installed; `fair_probe_result` resolves only
# when that directory is on sys.path. A bare `pytest <this file>` sets no PYTHONPATH, so the
# file used to be a collection ERROR whenever it ran alone. Same bootstrap as the working
# neighbours (tests/swebench/test_gt_feature_metrics_128.py).
for _p in (Path(__file__).resolve().parents[2] / "scripts" / "swebench",):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import fair_probe_result as fair_probe_module  # noqa: E402
from fair_probe_result import (  # noqa: E402
    ControlResult,
    _window_consequences,
    extract_live_opportunity_observations,
)


def _assistant(command: str) -> dict:
    return {"role": "assistant", "extra": {"actions": [{"command": command}]}}


def test_window_consequences_classifies_searches_edits_verifications() -> None:
    messages = [
        {"role": "user", "content": "issue"},          # 0: boundary
        _assistant("grep -rn foo src/"),                # 1: search/view
        _assistant("cat src/a.py"),                     # 2: search/view
        _assistant("apply_patch <<'EOF'"),              # 3: edit
        _assistant("pytest tests/test_a.py"),           # 4: verification
        _assistant("str_replace src/a.py old new"),     # 5: edit
    ]
    # window is the whole assistant span (message 0 boundary excluded).
    cons = _window_consequences(messages, 1, 6)
    assert cons["steps_to_next_decision"] == 5.0
    assert cons["native_search_views"] == 2.0
    assert cons["edit_cycles"] == 2.0
    assert cons["verification_runs"] == 1.0
    # all continuous fields are real-valued (feedable to the continuous estimator).
    assert all(isinstance(v, float) for v in cons.values())


def test_window_consequences_is_bounded_and_empty_safe() -> None:
    messages = [_assistant("grep foo"), _assistant("apply_patch x")]
    # out-of-range / inverted windows never raise and never over-count.
    assert _window_consequences(messages, 5, 99)["steps_to_next_decision"] == 0.0
    assert _window_consequences([], 0, 10)["edit_cycles"] == 0.0


def _assignment_row(opportunity_id: str, candidate_id: str, seal: str, arm: str) -> dict:
    return {
        "schema": "gt.shadow_assignment.v1",
        "layer": "shadow.assignment",
        "outcome": "assigned",
        "fact_class": "caller_contract",
        "candidate_id": candidate_id,
        "candidate_sha256_16": seal,
        "assignment": arm,
        "assignment_probability": 0.5,
        "observation_binding": {"opportunity_id": opportunity_id},
    }


def test_export_attaches_consequences_to_every_observation(monkeypatch) -> None:
    deliver_opportunity, holdout_opportunity = "1" * 64, "2" * 64
    deliver_candidate, holdout_candidate = "3" * 64, "4" * 64
    deliver_seal, holdout_seal = "a" * 16, "b" * 16

    rows = [
        _assignment_row(deliver_opportunity, deliver_candidate, deliver_seal, "DELIVER"),
        {
            "outcome": "delivered",
            "fact_class": "caller_contract",
            "candidate_id": deliver_candidate,
            "content_sha256_16": deliver_seal,
            "observation_binding": {"opportunity_id": deliver_opportunity},
        },
        _assignment_row(holdout_opportunity, holdout_candidate, holdout_seal, "HOLDOUT"),
        {
            "outcome": "shadow_holdout",
            "fact_class": "caller_contract",
            "candidate_id": holdout_candidate,
            "content_sha256_16": holdout_seal,
            "file_path": "src/caller.py",
            "observation_binding": {"opportunity_id": holdout_opportunity},
        },
    ]
    chronology = SimpleNamespace(
        fact_class="caller_contract",
        evidence_type="caller_contract",
        ledger_row_index=1,
        chronology=SimpleNamespace(
            delivery_index=1,
            decision_open_index=0,
            decision_commit_index=2,
            native_acquisition_index=None,
        ),
    )
    monkeypatch.setattr(
        fair_probe_module,
        "_control_outcome",
        lambda *args, **kwargs: ControlResult(
            holdout_row_index=3, fact_class="caller_contract", control_seal=holdout_seal,
            withhold_index=1, window_end=2, entity_named=False, receipt=False,
            predecision_state_id="state", opportunity_id=holdout_opportunity,
            outcome="not_acted",
        ),
    )
    monkeypatch.setattr(
        fair_probe_module, "_native_acquisition_index", lambda *args, **kwargs: None
    )

    exported = extract_live_opportunity_observations(
        {"messages": [{"role": "user"}, _assistant("grep foo"), {"role": "user"}]},
        rows,
        {1: chronology},
        task_label="task",
        live_witness=True,
    )
    assert len(exported) == 2
    for record in exported:
        cons = record["consequences"]
        assert isinstance(cons, dict)
        for key in (
            "steps_to_next_decision",
            "native_search_views",
            "edit_cycles",
            "verification_runs",
        ):
            assert isinstance(cons[key], float)
    # the boolean receipt fields are UNCHANGED by the continuous producer.
    assert [row["endpoint"] for row in exported] == [True, False]
    assert [row["endpoint_name"] for row in exported] == [
        "registered_receipt",
        "registered_receipt",
    ]
