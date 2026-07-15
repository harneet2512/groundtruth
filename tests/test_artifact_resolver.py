"""Tests for scripts/swebench/artifact_resolver.py."""
from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import sys
import tempfile


def _load():
    path = os.path.join(
        os.path.dirname(__file__), "..", "scripts", "swebench", "artifact_resolver.py"
    )
    spec = importlib.util.spec_from_file_location("artifact_resolver", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["artifact_resolver"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_brief_provenance_match():
    mod = _load()
    with tempfile.TemporaryDirectory() as td:
        brief = os.path.join(td, "brief.txt")
        delivered = os.path.join(td, "delivered_instruction.txt")
        open(brief, "w", encoding="utf-8").write("hello")
        open(delivered, "w", encoding="utf-8").write("hello")
        arts = mod.TrialArtifacts(
            trial_dir=td,
            result_json=None,
            mini_trajectory=None,
            canonical_trajectory=None,
            deep_metrics=None,
            task_truth=None,
            outcome_json=None,
            oracle_events=None,
            runtime_ledger=None,
            delivered_instruction=delivered,
            adapter_witness=None,
            brief_txt=brief,
        )
        prov = mod.brief_provenance(arts)
        assert prov["brief_match"] is True
        assert prov["delivered_contains_substrate_brief"] is True
        assert prov["delivered_brief_block_sha256"] is None


def test_brief_provenance_matches_wrapped_instruction():
    mod = _load()
    with tempfile.TemporaryDirectory() as td:
        brief = os.path.join(td, "brief.txt")
        delivered = os.path.join(td, "delivered_instruction.txt")
        block = "<gt-task-brief>\nGT brief body\n</gt-task-brief>"
        open(brief, "w", encoding="utf-8").write(block)
        open(delivered, "w", encoding="utf-8").write(
            "issue prompt\n" + block + "\nGT runtime preamble"
        )
        arts = mod.TrialArtifacts(
            trial_dir=td,
            result_json=None,
            mini_trajectory=None,
            canonical_trajectory=None,
            deep_metrics=None,
            task_truth=None,
            outcome_json=None,
            oracle_events=None,
            runtime_ledger=None,
            delivered_instruction=delivered,
            adapter_witness=None,
            brief_txt=brief,
        )
        prov = mod.brief_provenance(arts)
        assert prov["brief_match"] is True
        assert prov["delivered_contains_substrate_brief"] is True
        assert prov["substrate_brief_sha256"] == prov["delivered_brief_block_sha256"]
        assert prov["substrate_brief_sha256"] != prov["delivered_instruction_sha256"]


def test_resolver_and_provenance_use_task_root_exact_seal(tmp_path):
    mod = _load()
    block = "<gt-task-brief>\nranked file\n</gt-task-brief>"
    (tmp_path / "mini-swe-agent.trajectory.json").write_text(
        json.dumps({"messages": [
            {"role": "system", "content": "system"},
            {"role": "user", "content": f"issue\n{block}\nrest"},
        ]}),
        encoding="utf-8",
    )
    (tmp_path / "gt_runtime_ledger_task.jsonl").write_text(
        json.dumps({
            "layer": "brief.task",
            "event_type": "task_start",
            "outcome": "delivered",
            "chars_delivered": len(block),
            "content_sha256_16": hashlib.sha256(block.encode()).hexdigest()[:16],
        }) + "\n",
        encoding="utf-8",
    )
    brief_dir = tmp_path / "gt_artifacts"
    brief_dir.mkdir()
    (brief_dir / "brief.txt").write_text(block, encoding="utf-8")

    arts = mod.resolve_trial_artifacts(str(tmp_path), instance_id="task")
    assert arts.mini_trajectory == str(tmp_path / "mini-swe-agent.trajectory.json")
    assert arts.runtime_ledger == str(tmp_path / "gt_runtime_ledger_task.jsonl")
    provenance = mod.brief_provenance(arts)
    assert provenance["brief_match"] is True
    assert provenance["delivered_contains_substrate_brief"] is True
    assert provenance["delivery_join_method"] == "seal"
    assert provenance["delivery_message_index"] == 1


def test_brief_provenance_rejects_ambiguous_exact_seals(tmp_path):
    mod = _load()
    block = "<gt-task-brief>\nranked file\n</gt-task-brief>"
    (tmp_path / "mini-swe-agent.trajectory.json").write_text(
        json.dumps({"messages": [{"role": "user", "content": block}]}),
        encoding="utf-8",
    )
    row = {
        "layer": "brief.task", "event_type": "task_start", "outcome": "delivered",
        "chars_delivered": len(block),
        "content_sha256_16": hashlib.sha256(block.encode()).hexdigest()[:16],
    }
    (tmp_path / "gt_runtime_ledger_task.jsonl").write_text(
        json.dumps(row) + "\n" + json.dumps(row) + "\n", encoding="utf-8",
    )
    brief_dir = tmp_path / "gt_artifacts"
    brief_dir.mkdir()
    (brief_dir / "brief.txt").write_text(block, encoding="utf-8")

    provenance = mod.brief_provenance(
        mod.resolve_trial_artifacts(str(tmp_path), instance_id="task")
    )
    assert provenance["brief_match"] is None
    assert provenance["delivery_join_method"] is None
