"""#31 blocker 1 — a CONSUMER for the counterfactual pairs, scoped to what it can honestly say.

The producer emits `gt.counterfactual_pair.v1` rows and nothing reads them, so the arm produces
raw material rather than a measurement. This is the reader.

WHAT IT MAY REPORT: a CHANGE RATE — P(next action differs | dosed). The state is identical and
the only difference is the capsule, so that is a proximal causal statement about CHANGE.

WHAT IT MAY NOT REPORT: EFFECTIVENESS. Deciding whether the GT-arm action was BETTER needs an
anchor this layer does not have. `gt_substitution_grader` hard-gates level-5 CAUSAL behind
`assert_paired_for_causal`, and whether an L2 same-turn pair counts as `ARMS_PAIRED` is a
DOCTRINAL question that is deliberately NOT settled here. So the summary emits no CAUSAL
verdict, carries an explicit `effectiveness: NOT_ESTABLISHED`, and labels its own arm shape.

THE SLIDE THIS GUARDS AGAINST is "GT changed the action 40% of the time" quietly becoming "GT is
effective". They are different claims and only one of them is supported.

AND A ZERO IS NOT A FINDING: with no pairs the summary is NOT_EVALUABLE, never "0% change". A
run where the probe never fired must not read as a run where GT changed nothing.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT, ROOT / "src", ROOT / "scripts" / "swebench"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import l2_counterfactual_summary as summary  # noqa: E402


def _pair(differ: bool, *, tokens: int = 10, call: str = "c1") -> dict:
    return {
        "schema": "gt.counterfactual_pair.v1",
        "layer": "measurement.counterfactual_pair",
        "outcome": "measurement_only",
        "chars_delivered": 0,
        "model_call_id": call,
        "treatment_action": "grep -rn foo src/",
        "control_action": "ls" if differ else "grep -rn foo src/",
        "actions_differ": differ,
        "signed": False,
        "measurement_overhead_tokens": {
            "prompt_tokens": tokens,
            "completion_tokens": tokens,
            "total_tokens": tokens * 2,
        },
    }


def _write(tmp_path: Path, rows: list[dict]) -> Path:
    sink = tmp_path / "receipts.jsonl"
    sink.write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows),
        encoding="utf-8",
    )
    return sink


def test_no_pairs_is_not_evaluable_never_zero_percent(tmp_path) -> None:
    """A run where the probe never fired must NOT read as 'GT changed nothing'."""
    out = summary.summarize([_write(tmp_path, [])])
    assert out["status"] == "NOT_EVALUABLE"
    assert out["pairs"] == 0
    assert "action_change_rate" not in out


def test_rows_of_other_schemas_are_ignored(tmp_path) -> None:
    sink = _write(
        tmp_path,
        [
            {"schema": "gt.canonical_delivery.v1", "outcome": "delivered"},
            {"schema": "gt.shadow_assignment.v1", "arm": "DELIVER"},
        ],
    )
    assert summary.summarize([sink])["status"] == "NOT_EVALUABLE"


def test_change_rate_counts_only_dosed_pairs(tmp_path) -> None:
    sink = _write(
        tmp_path,
        [
            _pair(True, call="c1"),
            _pair(False, call="c2"),
            _pair(True, call="c3"),
            _pair(False, call="c4"),
        ],
    )
    out = summary.summarize([sink])
    assert out["status"] == "MEASURED"
    assert out["pairs"] == 4
    assert out["actions_differ"] == 2
    assert out["action_change_rate"] == 0.5


def test_summary_refuses_to_claim_effectiveness(tmp_path) -> None:
    """THE GUARD. A change rate is not an effect size and must never be dressed as one."""
    out = summary.summarize([_write(tmp_path, [_pair(True)])])
    assert out["effectiveness"] == "NOT_ESTABLISHED"
    # No CAUSAL verdict, no sign, no implied direction anywhere in the payload.
    assert "causal" not in out
    assert "effect_size" not in out
    assert "improved" not in out
    assert out["signed"] is False
    # And it labels its own arm shape rather than borrowing the paired-run vocabulary.
    assert out["arms"] == "same_turn_fixed_history"
    assert out["arms"] != "paired"


def test_measurement_overhead_is_totalled_and_kept_separate(tmp_path) -> None:
    """The probe's own spend is reported, and never folded into agent cost."""
    # DISTINCT model_call_ids on purpose: these are two dosed turns, not one duplicated row.
    # My first draft left both at the default call id, dedup collapsed them, and the
    # expectation was wrong rather than the code — the denominator guard doing its job.
    sink = _write(
        tmp_path,
        [_pair(True, tokens=5, call="c1"), _pair(False, tokens=7, call="c2")],
    )
    out = summary.summarize([sink])
    assert out["measurement_overhead_tokens"]["total_tokens"] == 24
    assert "total_cost_usd" not in out
    assert "cost" not in out


def test_duplicate_model_calls_are_counted_once(tmp_path) -> None:
    """One dosed turn is one opportunity; a duplicated row must not inflate the denominator."""
    sink = _write(
        tmp_path, [_pair(True, call="c1"), _pair(True, call="c1"), _pair(False, call="c2")]
    )
    out = summary.summarize([sink])
    assert out["pairs"] == 2
    assert out["actions_differ"] == 1
    assert out["action_change_rate"] == 0.5


def test_malformed_rows_are_reported_not_silently_dropped(tmp_path) -> None:
    """Silent drops are how a denominator quietly shrinks and a rate quietly lies."""
    sink = tmp_path / "receipts.jsonl"
    sink.write_text(
        json.dumps(_pair(True)) + "\n" + "{not json\n", encoding="utf-8"
    )
    out = summary.summarize([sink])
    assert out["malformed_rows"] == 1
    assert out["pairs"] == 1
