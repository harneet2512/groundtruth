from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
METRICS_PATH = ROOT / "scripts" / "swebench" / "gt_deep_metrics.py"


def _load_metrics_module():
    spec = importlib.util.spec_from_file_location(
        "gt_deep_metrics_trajectory_fallback", METRICS_PATH
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_deepswe_trajectory_gt_observations_backfill_token_accounting(tmp_path):
    mod = _load_metrics_module()
    task = "trajectory-fallback-task"
    agent_dir = tmp_path / "jobs" / "123" / f"{task}__attempt" / "agent"
    verifier_dir = agent_dir.parent / "verifier"
    agent_dir.mkdir(parents=True)
    verifier_dir.mkdir()
    (verifier_dir / "reward.txt").write_text("0", encoding="utf-8")

    content = (
        "<gt-task-brief>\nfiles: src/a.py\n</gt-task-brief>\n"
        "<gt-graph-map>\nsource graph\n</gt-graph-map>\n"
        "<gt-evidence>\ncontract evidence\n</gt-evidence>\n"
        '<gt-nudge reason="test_evidence_gap">verify this edit</gt-nudge>\n'
    )
    trajectory = {
        "info": {
            "exit_status": "submitted",
            "submission": "",
            "config": {"model": {"model_name": "test-model"}},
            "model_stats": {"api_calls": 2},
        },
        "messages": [
            {"role": "assistant", "content": "I will inspect.", "tool_calls": []},
            {"role": "tool", "content": content},
            {"role": "assistant", "content": "I will edit.", "tool_calls": []},
        ],
    }
    (agent_dir / "mini-swe-agent.trajectory.json").write_text(
        json.dumps(trajectory), encoding="utf-8"
    )

    deep = mod.build(task, str(tmp_path))

    assert deep["inputs_present"]["gt_run_summary"] is False
    assert deep["gt_delivery"]["gt_observation_chars_total"] > 0
    assert deep["gt_injected_tokens_source"] == "trajectory_proxy"
    assert deep["gt_injected_tokens_total"] > 0
    assert deep["efficiency"]["gt_injected_tokens_source"] == "trajectory_proxy"
    assert deep["layers_active"]
    assert deep["per_layer"]
    assert all(layer.get("source") == "trajectory_proxy" for layer in deep["per_layer"].values())


def test_embedder_certificate_overrides_local_probe_unavailability(tmp_path):
    mod = _load_metrics_module()
    task = "embedder-cert-task"
    artifacts = tmp_path / "gt_artifacts"
    artifacts.mkdir()
    cert = {
        "schema": "gt.embedder_certificate.v1",
        "model_path": "/models/e5-small-v2",
        "embedder_class": "EmbeddingModel",
        "embedder_dim": 384,
        "semantic_candidate_count": 12,
        "rendered_semantic_nonzero_count": 5,
        "upstream_semantic_nonzero_count": 5,
        "effective_w_sem": 0.2,
    }
    (artifacts / "embedder_certificate.json").write_text(json.dumps(cert), encoding="utf-8")

    deep = mod.build(task, str(tmp_path))

    assert deep["embedder_status_source"] == "embedder_certificate"
    assert deep["embedder_certificate_path"].endswith("embedder_certificate.json")
    assert deep["embedder_class"] == "EmbeddingModel"
    assert deep["embedder_vector_dim"] == 384.0
    assert deep["embedder_nonzero"] is True
    assert deep["semantic_enabled"] is True
    assert deep["semantic_candidate_count"] == 12.0
