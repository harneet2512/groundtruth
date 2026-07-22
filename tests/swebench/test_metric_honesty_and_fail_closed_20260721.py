"""Metric-honesty + fail-closed fixes (2026-07-21).

Four confirmed defects, each proven RED on the pre-fix tree:

D1 ``edit_revert_rate`` was ``revert_count / total_edits`` — an UNBOUNDED value named a "rate"
   (observed 5.0, 2.0 when git-checkout style reverts outnumber edits). Reverts are per-command
   (``git checkout .`` etc.) and are NOT linkable to a specific prior edit, so a true bounded
   "fraction of edits reverted" is not honestly computable. The value is renamed to the honest
   ``revert_commands_per_edit`` (an unbounded per-edit COUNT) and its mandatory-registry type moves
   from ``rate`` (which enforces [0,1] and would fail-close a legitimate 5.0) to ``nonnegative_number``.

D2 ``wasted_token_rate`` is a STEP ratio (``non_gold_steps / non_idle_steps``), never a token ratio
   (no per-step token attribution exists in the trajectory). Renamed to ``non_gold_step_rate``; it
   is genuinely bounded [0,1] so the registry type stays ``rate``.

D3 ``gt_injected_tokens_total`` held CHARACTERS under the ``runtime_ledger_sealed_chars`` /
   ``trajectory_proxy`` sources (raw chars, or chars that were only /4'd into a token ESTIMATE) —
   the same key carried two different units and mislabeled chars as tokens. It is split into
   ``gt_injected_chars_exact`` (exact chars when the source provides them) and
   ``gt_injected_tokens_estimated`` (the disclosed chars/4 token estimate). The emitted schema is
   bumped ``gt_deep_metrics.v2`` -> ``gt_deep_metrics.v3``; every reader accepts BOTH.

D4 The deep-metrics emitter caught every exception from ``build()``, wrote a thin partial record,
   and still ``return 0`` — fail-OPEN. It now still writes the diagnostic failure artifact but
   exits NONZERO so a genuine collection crash is unpublishable. (A bare task dir where the agent
   never started is classified by a SUCCESSFUL ``build()`` and still exits 0 — only a real
   ``build()`` crash is fail-closed.)
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
for _p in (ROOT / "src", ROOT / "scripts" / "swebench", ROOT / "scripts" / "research"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import gt_deep_metrics as dm  # noqa: E402
import gt_feature_metrics as fm  # noqa: E402
import gt_performance_metrics as pm  # noqa: E402
import gt_run_metrics as rm  # noqa: E402


# ── D1: edit_revert_rate -> revert_commands_per_edit (unbounded COUNT, not a [0,1] rate) ───────
def test_revert_commands_per_edit_is_unbounded_count_not_a_rate() -> None:
    timeline = [
        {"role": "assistant", "step": 0, "is_edit": True, "edited_file": "src/x.py"},
        {"role": "assistant", "step": 1, "is_revert": True},
        {"role": "assistant", "step": 2, "is_revert": True},
        {"role": "assistant", "step": 3, "is_revert": True},
        {"role": "assistant", "step": 4, "is_revert": True},
        {"role": "assistant", "step": 5, "is_revert": True},
    ]
    eq = pm._compute_edit_quality(timeline, gold_files=["src/x.py"], submission="")
    # honest name present, unbounded value preserved verbatim (5 reverts / 1 edit)
    assert eq["revert_commands_per_edit"] == 5.0
    # the misleading "rate" name is GONE (it implied [0,1] but held 5.0)
    assert "edit_revert_rate" not in eq


def test_revert_metric_registered_as_nonnegative_number_not_rate() -> None:
    edit_quality = dict(rm._MANDATORY_METRICS["edit_quality"])
    assert edit_quality.get("revert_commands_per_edit") == "nonnegative_number"
    assert "edit_revert_rate" not in edit_quality
    # a 5.0 value must now PASS its own contract (rate would have failed it closed)
    assert rm._contract_number(5.0, "nonnegative_number") == 5.0
    # the mandatory count/contract is preserved by the rename
    assert rm._MANDATORY_METRIC_COUNT == 58


# ── D2: wasted_token_rate -> non_gold_step_rate (a STEP ratio, honestly named) ────────────────
def test_non_gold_step_rate_replaces_wasted_token_name() -> None:
    tok = pm._compute_token_efficiency(trajectory={}, timeline=[], gold_files=[])
    assert "wasted_token_rate" not in tok
    assert "non_gold_step_rate" in tok


def test_non_gold_step_rate_registered_and_provenance_names_step_proxy() -> None:
    token_eff = dict(rm._MANDATORY_METRICS["token_efficiency"])
    assert token_eff.get("non_gold_step_rate") == "rate"  # genuinely bounded [0,1]
    assert "wasted_token_rate" not in token_eff
    formula, denom = fm._perf_provenance("token_efficiency", "non_gold_step_rate")
    assert "STEP-PROXY" in formula and "NOT the" in formula
    assert "_non_idle_step_count" in denom


# ── D3: gt_injected_tokens_total split into exact chars + disclosed token estimate; schema v3 ──
def _trajectory_proxy_deep(tmp_path: Path) -> dict:
    task = "honesty-d3-task"
    agent_dir = tmp_path / "jobs" / "123" / f"{task}__attempt" / "agent"
    verifier_dir = agent_dir.parent / "verifier"
    agent_dir.mkdir(parents=True)
    verifier_dir.mkdir()
    (verifier_dir / "reward.txt").write_text("0", encoding="utf-8")
    content = (
        "<gt-task-brief>\nfiles: src/a.py\n</gt-task-brief>\n"
        "<gt-evidence>\ncontract evidence\n</gt-evidence>\n"
    )
    trajectory = {
        "info": {
            "exit_status": "submitted", "submission": "",
            "config": {"model": {"model_name": "test-model"}},
            "model_stats": {"api_calls": 2},
        },
        "messages": [
            {"role": "assistant", "content": "inspect", "tool_calls": []},
            {"role": "tool", "content": content},
            {"role": "assistant", "content": "edit", "tool_calls": []},
        ],
    }
    (agent_dir / "mini-swe-agent.trajectory.json").write_text(
        json.dumps(trajectory), encoding="utf-8"
    )
    return dm.build(task, str(tmp_path))


def test_deep_metrics_split_chars_exact_and_token_estimate(tmp_path: Path) -> None:
    deep = _trajectory_proxy_deep(tmp_path)
    chars = deep["gt_delivery"]["gt_observation_chars_total"]
    assert chars > 0
    # exact chars are labelled as CHARS, not tokens
    assert deep["gt_injected_chars_exact"] == chars
    assert deep["efficiency"]["gt_injected_chars_exact"] == chars
    # the token figure is an explicit chars/4 ESTIMATE, distinct from the char count
    assert deep["gt_injected_tokens_estimated"] == pytest.approx(chars / 4.0)
    assert deep["efficiency"]["gt_injected_tokens_estimated"] == pytest.approx(chars / 4.0)
    # the mislabeled key is gone from BOTH the top level and efficiency
    assert "gt_injected_tokens_total" not in deep
    assert "gt_injected_tokens_total" not in deep["efficiency"]
    # schema is bumped because the emitted token/char semantics changed
    assert deep["schema"] == "gt_deep_metrics.v3"


def test_run_metrics_accepts_both_v2_and_v3_deep_schema() -> None:
    base = {"precision_decimals": 8, "task_id": "t"}
    v3_issues = rm._deep_metric_record_issues({**base, "schema": "gt_deep_metrics.v3"})
    v2_issues = rm._deep_metric_record_issues({**base, "schema": "gt_deep_metrics.v2"})
    bad_issues = rm._deep_metric_record_issues({**base, "schema": "gt_deep_metrics.v0"})
    assert "record:deep_schema" not in v3_issues  # new schema accepted
    assert "record:deep_schema" not in v2_issues  # old artifacts still valid (compat)
    assert "record:deep_schema" in bad_issues  # a genuinely wrong schema still rejected


def test_paired_and_report_readers_prefer_v3_token_estimate(tmp_path: Path) -> None:
    # gt_deep_metrics.pair(): token_delta reads the honest estimate on a v3 record …
    v3 = {"task_id": "t", "gt_injected_tokens_estimated": 40.0, "agent": {}}
    assert dm.pair(v3, {"agent": {}})["token_delta"] == 40.0
    # … and still reads a legacy v2 record via the old key (compat reader).
    v2 = {"task_id": "t", "gt_injected_tokens_total": 33.0, "agent": {}}
    assert dm.pair(v2, {"agent": {}})["token_delta"] == 33.0

    import build_run_report as brr  # noqa: E402  (scripts/research)

    def _reader_gt_tokens(record: dict) -> float | None:
        path = tmp_path / "gt_deep_metrics_t.json"
        path.write_text(json.dumps(record), encoding="utf-8")
        task = brr.new_task("t")
        brr.parse_deep_metrics(task, path)
        return task["gt_injected_tokens"]

    # v3 artifact → the honest token estimate populates the display column …
    assert _reader_gt_tokens({"gt_injected_tokens_estimated": 40.0}) == 40.0
    # … and a legacy v2 artifact still resolves via the old key (compat reader).
    assert _reader_gt_tokens({"gt_injected_tokens_total": 33.0}) == 33.0


# ── D4: emitter is fail-CLOSED — a build() crash writes the artifact but exits NONZERO ─────────
def test_emitter_exits_nonzero_on_collection_failure_but_still_writes_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = "collection-crash-task"
    out_path = tmp_path / f"gt_deep_metrics_{task}.json"

    def _boom(*_a, **_k):
        raise RuntimeError("forced collection failure")

    monkeypatch.setattr(dm, "build", _boom)
    monkeypatch.setattr(sys, "argv", [
        "gt_deep_metrics.py", task, str(tmp_path), "--out", str(out_path),
    ])
    rc = dm.main()

    assert rc != 0, "a build() collection crash must be fail-CLOSED (nonzero exit)"
    assert out_path.exists(), "the diagnostic failure artifact must still be written"
    rec = json.loads(out_path.read_text(encoding="utf-8"))
    assert rec["task_id"] == task
    assert rec["failure_stage"] == "infra"
    assert "forced collection failure" in rec["failure_reason"]
