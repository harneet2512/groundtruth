"""RC-02: cost discipline tests.

Covers:
  - _compute_expected_cost math + surface line format (G-002)
  - --total-cost-limit appends correct argv pair (G-001/G-009)
  - Vertex 403 body classifier (E-001 / RC-02 (e))
  - Preflight unsets paid-call API keys from os.environ (G-007/G-008)
  - mcp/server.py invariant: api_key=None for all 3 AI components (G-007)
  - Reconciliation tolerance helper (G-005)

These tests run without spending money — every codepath is local.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

REPO_DIR = Path(__file__).resolve().parents[2]
RUNNER = REPO_DIR / "scripts" / "swebench" / "swe_agent_smoke_runner.py"
CLASSIFIER = REPO_DIR / "scripts" / "swebench" / "vertex_403_classifier.py"


def _load_module(name: str, path: Path):
    """Load a module by file path. Adds the script dir to sys.path so the
    runner's `from image_name_resolver import ...` line resolves.
    """
    script_dir = str(path.parent)
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    # Register before exec so @dataclass-class lookups via cls.__module__
    # find the loaded module (otherwise sys.modules.get(...) returns None).
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def runner_mod():
    return _load_module("swe_agent_smoke_runner_under_test", RUNNER)


@pytest.fixture(scope="module")
def classifier_mod():
    return _load_module("vertex_403_classifier_under_test", CLASSIFIER)


# ---- G-002: expected-cost surface -----------------------------------------

def test_compute_expected_cost_basic(runner_mod):
    expected, line = runner_mod._compute_expected_cost(
        task_count=30,
        per_task_estimate_usd=0.12,
        cap_usd=50.0,
    )
    assert expected == pytest.approx(3.6)
    assert line.startswith("EXPECTED_COST:")
    assert "30 tasks" in line
    assert "$0.1200" in line
    assert "$3.6000" in line
    assert "cap $50.00" in line


def test_compute_expected_cost_no_cap(runner_mod):
    _, line = runner_mod._compute_expected_cost(
        task_count=1, per_task_estimate_usd=0.12, cap_usd=None
    )
    assert "cap unset" in line


def test_compute_expected_cost_rejects_negative(runner_mod):
    with pytest.raises(ValueError):
        runner_mod._compute_expected_cost(task_count=-1)
    with pytest.raises(ValueError):
        runner_mod._compute_expected_cost(task_count=1, per_task_estimate_usd=-0.5)


# ---- G-001/G-009: --total-cost-limit -> argv ------------------------------

def test_total_cost_limit_emits_agent_flag(runner_mod):
    cmd = runner_mod.build_sweagent_cmd(
        config_path="config/gt_track4.yaml",
        task_ids=["x__y-1"],
        output_dir="/tmp/out",
        workers=1,
        per_instance_cost_limit=1.0,
        per_instance_wallclock_cap_seconds=1800,
        total_cost_limit=50.0,
        venv_python="python3",
        launcher="sweagent",
    )
    # Find the flag pair.
    assert "--agent.model.total_cost_limit" in cmd
    idx = cmd.index("--agent.model.total_cost_limit")
    assert cmd[idx + 1] == "50.0"


def test_total_cost_limit_omitted_by_default(runner_mod):
    cmd = runner_mod.build_sweagent_cmd(
        config_path="config/gt_track4.yaml",
        task_ids=["x__y-1"],
        output_dir="/tmp/out",
        workers=1,
        per_instance_cost_limit=1.0,
        per_instance_wallclock_cap_seconds=1800,
        venv_python="python3",
        launcher="sweagent",
    )
    assert "--agent.model.total_cost_limit" not in cmd


# ---- E-001 / RC-02 (e): Vertex 403 body classifier -------------------------

def test_classify_403_throttle(classifier_mod):
    body = json.dumps(
        {"error": {"status": "RESOURCE_EXHAUSTED", "message": "Quota exceeded"}}
    )
    assert classifier_mod.classify_403(body) == "throttle"
    assert classifier_mod.is_retryable(body) is True


def test_classify_403_iam(classifier_mod):
    body = json.dumps(
        {"error": {"status": "PERMISSION_DENIED", "reason": "IAM_PERMISSION_DENIED"}}
    )
    assert classifier_mod.classify_403(body) == "iam"
    assert classifier_mod.is_retryable(body) is False


def test_classify_403_iam_plain(classifier_mod):
    # Real Vertex sometimes only emits PERMISSION_DENIED without the
    # IAM_ prefix. Should still classify as iam (fail-fast).
    body = '{"error":{"status":"PERMISSION_DENIED"}}'
    assert classifier_mod.classify_403(body) == "iam"


def test_classify_403_unknown(classifier_mod):
    assert classifier_mod.classify_403("") == "unknown"
    assert classifier_mod.classify_403("some-html-page") == "unknown"


def test_classify_403_throttle_wins_when_both_present(classifier_mod):
    # If a body somehow mentions both, RESOURCE_EXHAUSTED wins
    # (retry-eligible) so we don't fail-fast on a recoverable throttle.
    body = "RESOURCE_EXHAUSTED ... PERMISSION_DENIED"
    assert classifier_mod.classify_403(body) == "throttle"


# ---- G-007/G-008: env scrubbing -------------------------------------------

def test_preflight_unsets_paid_call_keys(runner_mod, tmp_path, monkeypatch):
    monkeypatch.setenv("GT_LLM_API_KEY", "sk-real-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-real")
    out = tmp_path / "out"
    # No config_path / api_base — exercise just the env-scrub branch.
    result = runner_mod._run_preflight(out)
    # The scrubbing happens unconditionally and is recorded in checks.
    assert any("unset_env:GT_LLM_API_KEY" in c for c in result.checks)
    assert any("unset_env:ANTHROPIC_API_KEY" in c for c in result.checks)
    # And os.environ no longer has them.
    assert "GT_LLM_API_KEY" not in os.environ
    assert "ANTHROPIC_API_KEY" not in os.environ


# ---- G-007: mcp/server.py invariant ---------------------------------------

def test_mcp_server_passes_api_key_none():
    """Static-source assertion that the three AI components are constructed
    with api_key=None. Failing this means GT's $0-AI product hypothesis
    has silently regressed (CLAUDE.md: "GT must stay LLM-free in its core
    pipeline").
    """
    server_py = REPO_DIR / "src" / "groundtruth" / "mcp" / "server.py"
    src = server_py.read_text(encoding="utf-8")
    for ctor in ("TaskParser", "BriefingEngine", "ValidationOrchestrator"):
        # Find the constructor call and verify api_key=None appears within
        # its argument list.
        idx = src.find(ctor + "(")
        assert idx != -1, f"{ctor} not found in mcp/server.py"
        # Read up to the matching ')' (cheap balanced scan, sufficient for
        # the 1-2-line invocations in server.py).
        end = src.find(")", idx)
        assert end != -1
        snippet = src[idx:end + 1]
        assert "api_key=None" in snippet, (
            f"{ctor} no longer passes api_key=None: {snippet!r}"
        )


# ---- G-005: reconciliation helper -----------------------------------------

def test_reconcile_within_tolerance(runner_mod, tmp_path):
    log = tmp_path / "litellm_calls.jsonl"
    rows = [
        {"response_cost": 0.10, "model": "qwen3-coder"},
        {"response_cost": 0.05, "model": "qwen3-coder"},
    ]
    with log.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    within, line = runner_mod._reconcile_litellm_calls(tmp_path, 0.155)
    assert within is True
    assert "rows=2" in line
    assert "$0.1500" in line  # proxy total


def test_reconcile_outside_tolerance(runner_mod, tmp_path):
    log = tmp_path / "litellm_calls.jsonl"
    log.write_text(
        json.dumps({"response_cost": 0.50}) + "\n", encoding="utf-8"
    )
    within, line = runner_mod._reconcile_litellm_calls(tmp_path, 0.20)
    assert within is False
    assert "delta=" in line


def test_reconcile_missing_log(runner_mod, tmp_path):
    within, line = runner_mod._reconcile_litellm_calls(tmp_path, 0.0)
    assert within is False
    assert "missing" in line


# ---- G-005: proxy YAML has cost callback config ---------------------------

def test_litellm_proxy_yaml_has_cost_callback():
    """The proxy YAML must declare success_callback so per-call
    (input_tokens, output_tokens, cost) is recoverable post-run.
    """
    proxy = REPO_DIR / "scripts" / "swebench" / "litellm_proxy_qwen.yaml"
    text = proxy.read_text(encoding="utf-8")
    assert "success_callback:" in text
    assert "json_logs: true" in text
    assert "fallbacks:" in text
    assert "rpm:" in text
    assert "tpm:" in text
