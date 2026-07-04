"""Tests for scripts/research/build_run_report.py.

Covers (per docs/RESEARCH_ARTIFACT_SPEC_20260610.md):
  - synthetic mini run-dirs for all three layouts -> all sections emitted
  - integrity SHA256s are correct (recomputed independently)
  - missing-data handling: absent field -> NOT COLLECTED, never a number
  - no fabrication: a field deleted from input never appears as a number in output
  - n<5 cells report counts, not rates
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

_MOD_PATH = Path(__file__).resolve().parents[1] / "scripts" / "research" / "build_run_report.py"
_spec = importlib.util.spec_from_file_location("build_run_report", _MOD_PATH)
brr = importlib.util.module_from_spec(_spec)
sys.modules["build_run_report"] = brr
_spec.loader.exec_module(brr)

EXPECTED_FILES = [
    "experiment_card.json", "tasks_normalized.json",
    "layer_effectiveness.md", "layer_effectiveness.csv",
    "failure_taxonomy.md", "failure_taxonomy.csv",
    "token_economics.md", "language_depth.md",
    "behavioral_deltas.md", "integrity_chain.md", "TECH_REPORT_DRAFT.md",
]

TASK_A = "acme__widgets-101"
TASK_B = "acme__gadgets-202"


# ---------------------------------------------------------------- builders


def make_gha_run(root: Path, *, with_cost: bool = True, with_scorecard: bool = True) -> Path:
    run = root / "gha_run"
    dbg = run / "gt_debug"
    dbg.mkdir(parents=True)
    dm = {
        "task_id": TASK_A,
        "schema": "gt_deep_metrics.v2",
        "git_commit": "abc123def456",
        "outcome": "unresolved_with_patch",
        "resolved": None,
        "has_patch": True,
        "graph_nodes": 100.0, "graph_edges": 250.0,
        "verified_edge_count": 200.0, "verified_edge_ratio": 0.8,
        "fts5_row_count": 100.0,
        "lsp_server_name": "pyright", "lsp_enriched_edge_count": 40.0,
        "embedder_vector_dim": 384.0, "embedder_nonzero": True, "semantic_enabled": True,
        "gt_injected_tokens_total": 1300.0,
        "per_layer": {
            "L1": {"eligible": 1.0, "emitted": 1.0, "suppressed": 0.0,
                   "rendered_tokens_total": 500.0, "utilization_score": 0.75},
        },
        "agent": {"action_count": 52.0, "first_edit_action": 32.0},
        "efficiency": {},
    }
    if with_cost:
        dm["efficiency"] = {
            "llm_calls": 49.0, "llm_tokens_in": 1000.0, "llm_tokens_out": 100.0,
            "llm_tokens_cached": 0.0, "llm_cache_hit_tokens": 0.0,
            "llm_cache_miss_tokens": 0.0, "llm_cost_usd": 0.1673,
            "gt_injection_overhead_pct": 0.11,
        }
    (dbg / f"gt_deep_metrics_{TASK_A}.json").write_text(json.dumps(dm), encoding="utf-8")
    events = [
        {"layer": "L1", "eligible": True, "emitted": True, "suppressed": False,
         "iter": 0, "max_iter": 100, "rendered_text": "<gt-localization>candidates</gt-localization>"},
        {"layer": "L4", "eligible": True, "emitted": False, "suppressed": True,
         "iter": 0, "max_iter": 100, "suppression_reason": "no_prefetch_results"},
    ]
    (dbg / f"gt_layer_events_{TASK_A}.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events), encoding="utf-8")
    (run / "output.jsonl").write_text('{"id": 1, "action": "read"}\n', encoding="utf-8")
    (run / "eval_result.json").write_text(json.dumps({
        "resolved_ids": [], "unresolved_ids": [TASK_A]}), encoding="utf-8")
    if with_scorecard:
        (run / "scorecard.json").write_text(json.dumps({
            "task": TASK_A, "run_id": "999",
            "tier1_outcome": {"resolved": 0},
            "tier2_causality": {"delivered": 1, "correct": 0, "consumed": 0, "gt_caused": 0},
            "tier6_legitimacy": {"no_gold_labels": True},
        }), encoding="utf-8")
    return run


def make_vm_run(root: Path) -> Path:
    run = root / "vm_run"
    tdir = run / TASK_B
    gt = tdir / "gt"
    gt.mkdir(parents=True)
    (tdir / "row.json").write_text(json.dumps({
        "instance_id": TASK_B, "language": "go", "image": "img:tag",
        "model": "vertex_ai/some-model", "pier_config": "cfg.yaml",
        "failure_class": "", "pier_rc": 0, "proof_reused": False,
        "outcome_class": "RESOLVED", "in_resolved_denominator": True,
        "reward": 1, "n_agent_steps": 17, "exit_status": "submitted",
        "gt_prebuilt_active": True, "hook_hash_match": True,
        "timings_s": {"task_pull": 30, "proof": 120, "agent": 600, "substrate_pull": 10},
        "task_repo_commit": "deadbeef00", "deepswe_bench_sha": "bench123",
        "gt_git_commit": "abc123def456", "substrate_digest": "ghcr.io/x@sha256:fff",
        "run_id": "vm_sweep_1", "ts_utc": "2026-06-10T00:00:00Z",
    }), encoding="utf-8")
    (gt / "graph.db").write_bytes(b"FAKE GRAPH DB BYTES")
    (gt / "graph_certificate.json").write_text(json.dumps({
        "nodes_count": 89, "edges_count": 95, "calls_count": 89, "det_pct": 100.0,
        "deterministic_count": 89, "name_match_count": 0, "fts5_row_count": 89,
        "fts5_match_probe_ok": True, "assertions_count": 68,
        "resolution_method_dist": {"import": 52, "same_file": 35, "verified_unique": 2},
        "schema_version": "v15.2-trust-tier",
    }), encoding="utf-8")
    (gt / "lsp_certificate.json").write_text(json.dumps({
        "resolved": 0, "residual": 0, "lsp_no_op_valid": True,
        "lsp_no_op_reason": "residual=0 (no in-scope name_match demand)",
    }), encoding="utf-8")
    (gt / "run_manifest.json").write_text(json.dumps({
        "graph_hash": "ghash111"}), encoding="utf-8")
    trial = tdir / "pier" / "jobs" / "2026-06-10__00-00-00" / "trial__abc"
    (trial / "agent").mkdir(parents=True)
    (trial / "verifier").mkdir()
    (trial / "artifacts").mkdir()
    (trial / "result.json").write_text(json.dumps({
        "task_name": f"datacurve/{TASK_B}", "trial_name": "trial__abc",
        "task_checksum": "chk", "config": {"agent": {"model_name": "vertex_ai/some-model"}},
    }), encoding="utf-8")
    (trial / "agent" / "mini-swe-agent.txt").write_text("step 1\nstep 2\n", encoding="utf-8")
    (trial / "verifier" / "reward.txt").write_text("1", encoding="utf-8")
    (trial / "artifacts" / "model.patch").write_text("diff --git a b\n", encoding="utf-8")
    return run


def make_pier_run(root: Path) -> Path:
    run = root / "pier_run"
    trial = run / "jobs" / "2026-06-10__01-00-00" / "task__xyz"
    (trial / "agent").mkdir(parents=True)
    (trial / "verifier").mkdir()
    (trial / "artifacts").mkdir()
    (trial / "result.json").write_text(json.dumps({
        "task_name": "datacurve/pier-task-1", "trial_name": "task__xyz",
        "task_checksum": "c1", "config": {"agent": {"model_name": "deepseek/deepseek-v4-flash"}},
    }), encoding="utf-8")
    (trial / "agent" / "mini-swe-agent.txt").write_text("trajectory line\n", encoding="utf-8")
    (trial / "verifier" / "reward.txt").write_text("0", encoding="utf-8")
    (trial / "artifacts" / "model.patch").write_text("", encoding="utf-8")  # empty == no patch
    return run


def run_analyzer(run_dir: Path, *extra: str) -> Path:
    out = run_dir / "RUN_REPORT"
    rc = brr.main([str(run_dir), *extra])
    assert rc == 0
    return out


# ---------------------------------------------------------------- layout sniffing


def test_sniff_layouts(tmp_path):
    assert brr.sniff_layout(make_gha_run(tmp_path)) == "gha-openhands"
    assert brr.sniff_layout(make_vm_run(tmp_path)) == "vm-sweep"
    assert brr.sniff_layout(make_pier_run(tmp_path)) == "pier-jobs"


def test_unknown_layout_errors(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert brr.main([str(empty)]) == 3


# ---------------------------------------------------------------- sections emitted


@pytest.mark.parametrize("maker", [make_gha_run, make_vm_run, make_pier_run])
def test_all_sections_emitted(tmp_path, maker):
    out = run_analyzer(maker(tmp_path))
    for fname in EXPECTED_FILES:
        p = out / fname
        assert p.exists(), f"missing artifact: {fname}"
        assert p.stat().st_size > 0, f"empty artifact: {fname}"


# ---------------------------------------------------------------- integrity SHAs


def test_integrity_shas_correct(tmp_path):
    run = make_vm_run(tmp_path)
    out = run_analyzer(run)
    tasks = json.loads((out / "tasks_normalized.json").read_text(encoding="utf-8"))
    t = tasks[0]
    graph_path = run / TASK_B / "gt" / "graph.db"
    expected_graph = hashlib.sha256(graph_path.read_bytes()).hexdigest()
    assert t["integrity"]["graph_db_sha256"] == expected_graph
    traj_path = Path(t["integrity"]["trajectory_path"])
    expected_traj = hashlib.sha256(traj_path.read_bytes()).hexdigest()
    assert t["integrity"]["trajectory_sha256"] == expected_traj
    chain = (out / "integrity_chain.md").read_text(encoding="utf-8")
    assert expected_graph in chain and expected_traj in chain
    assert t["integrity"]["graph_hash_post_lsp"] == "ghash111"


def test_gha_trajectory_sha(tmp_path):
    run = make_gha_run(tmp_path)
    out = run_analyzer(run)
    tasks = json.loads((out / "tasks_normalized.json").read_text(encoding="utf-8"))
    expected = hashlib.sha256((run / "output.jsonl").read_bytes()).hexdigest()
    assert tasks[0]["integrity"]["trajectory_sha256"] == expected


# ---------------------------------------------------------------- no fabrication


def test_missing_cost_is_not_collected_never_zero(tmp_path):
    run = make_gha_run(tmp_path, with_cost=False)
    out = run_analyzer(run)
    tasks = json.loads((out / "tasks_normalized.json").read_text(encoding="utf-8"))
    assert tasks[0]["llm_cost_usd"] is None  # null, not 0
    econ = (out / "token_economics.md").read_text(encoding="utf-8")
    row = next(line for line in econ.splitlines() if TASK_A in line)
    cost_cell = row.split("|")[8].strip()
    assert cost_cell == "NOT COLLECTED"
    card = json.loads((out / "experiment_card.json").read_text(encoding="utf-8"))
    assert card["cost_usd_8dp"] is None
    assert "llm_cost_usd (all tasks)" in card["fields_missing"]


def test_missing_causality_labeled(tmp_path):
    run = make_gha_run(tmp_path, with_scorecard=False)
    out = run_analyzer(run)
    eff = (out / "layer_effectiveness.md").read_text(encoding="utf-8")
    assert "No agent-observation causality data" in eff
    tax = (out / "failure_taxonomy.md").read_text(encoding="utf-8")
    # without causality the unresolved task cannot be classified into a GT class
    assert "UNCLASSIFIED(missing-signals)" in tax
    card = json.loads((out / "experiment_card.json").read_text(encoding="utf-8"))
    assert "tier2_causality (agent-observation)" in card["fields_missing"]


def test_no_fabricated_numbers_for_deleted_fields(tmp_path):
    """A field absent in input never appears as a number in output (spec §7.1)."""
    run = make_gha_run(tmp_path, with_cost=False, with_scorecard=False)
    # delete first_edit_action and agent counts too
    dm_path = run / "gt_debug" / f"gt_deep_metrics_{TASK_A}.json"
    dm = json.loads(dm_path.read_text(encoding="utf-8"))
    del dm["agent"]
    del dm["gt_injected_tokens_total"]
    dm_path.write_text(json.dumps(dm), encoding="utf-8")
    out = run_analyzer(run)
    tasks = json.loads((out / "tasks_normalized.json").read_text(encoding="utf-8"))
    t = tasks[0]
    for field in ("n_agent_steps", "first_edit_action", "gt_injected_tokens",
                  "llm_cost_usd", "edit_to_gold_action"):
        assert t[field] is None, f"{field} fabricated: {t[field]}"
    behav = (out / "behavioral_deltas.md").read_text(encoding="utf-8")
    assert "NOT COLLECTED" in behav
    # behavioral table must show n=0 rows, not invented stats
    for line in behav.splitlines():
        if line.startswith("| action_count"):
            assert "| 0 |" in line


# ---------------------------------------------------------------- n<5 counts not rates


def test_small_n_reports_counts_not_rates(tmp_path):
    run = make_gha_run(tmp_path)
    out = run_analyzer(run)
    eff = (out / "layer_effectiveness.md").read_text(encoding="utf-8")
    l1_row = next(line for line in eff.splitlines()
                  if line.startswith("| L1 |"))
    assert "1/1" in l1_row
    assert "(" not in l1_row.split("|")[9]  # delivered_tasks cell: no rate in parens
    tax = (out / "failure_taxonomy.md").read_text(encoding="utf-8")
    assert "%" not in tax
    assert brr.rate_or_count(2, 3) == "2/3"
    assert "(" in brr.rate_or_count(4, 8)


# ---------------------------------------------------------------- semantics


def test_vm_resolved_and_outcome(tmp_path):
    out = run_analyzer(make_vm_run(tmp_path))
    tasks = json.loads((out / "tasks_normalized.json").read_text(encoding="utf-8"))
    t = tasks[0]
    assert t["resolved"] is True
    assert t["language"] == "go"
    tax = (out / "failure_taxonomy.md").read_text(encoding="utf-8")
    # resolved without causality evidence is NEVER a GT win
    assert "resolved (causation NOT COLLECTED)" in tax
    card = json.loads((out / "experiment_card.json").read_text(encoding="utf-8"))
    assert card["layout"] == "vm-sweep"
    assert card["replay"]["command"] is not None
    assert TASK_B in card["replay"]["command"]


def test_pier_no_patch_and_unresolved(tmp_path):
    out = run_analyzer(make_pier_run(tmp_path))
    tasks = json.loads((out / "tasks_normalized.json").read_text(encoding="utf-8"))
    t = tasks[0]
    assert t["instance_id"] == "pier-task-1"
    assert t["resolved"] is False
    assert t["has_patch"] is False
    assert t["model"] == "deepseek/deepseek-v4-flash"


def test_gha_failure_classification_with_scorecard(tmp_path):
    out = run_analyzer(make_gha_run(tmp_path))
    tax = (out / "failure_taxonomy.md").read_text(encoding="utf-8")
    # delivered=1, correct=0, L1 emitted -> localization-miss per spec §4.2
    assert "localization-miss" in tax


def test_provenance_recorded(tmp_path):
    out = run_analyzer(make_gha_run(tmp_path))
    tasks = json.loads((out / "tasks_normalized.json").read_text(encoding="utf-8"))
    prov = tasks[0]["provenance"]
    assert any("gt_deep_metrics" in v for v in prov.values())
    assert "resolved" in prov  # traceable to eval_result.json
    assert "eval_result.json" in prov["resolved"]


def test_telemetry_vs_observation_separation(tmp_path):
    out = run_analyzer(make_gha_run(tmp_path))
    eff = (out / "layer_effectiveness.md").read_text(encoding="utf-8")
    assert "telemetry" in eff
    assert "agent-observation" in eff
    # causality table exists and carries the scorecard values at TASK level
    assert "Task-level causality" in eff


def test_baseline_pairing(tmp_path):
    gt_on = make_gha_run(tmp_path / "on")
    gt_off = make_gha_run(tmp_path / "off")
    out = gt_on / "RUN_REPORT"
    rc = brr.main([str(gt_on), "--baseline", str(gt_off)])
    assert rc == 0
    behav = (out / "behavioral_deltas.md").read_text(encoding="utf-8")
    assert "Paired deltas (pairing key = instance_id; 1 pairs)" in behav
    assert "+0.00000000" in behav  # identical synthetic arms -> delta 0, listed raw
    assert "no Wilcoxon" in behav  # n_pairs < 5
