"""Tests for scripts/swebench/artifact_resolver.py."""

from __future__ import annotations

import importlib.util
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
            brief_txt=brief,
        )
        prov = mod.brief_provenance(arts)
        assert prov["brief_match"] is True
        assert prov["delivered_contains_substrate_brief"] is True
        assert prov["substrate_brief_sha256"] == prov["delivered_brief_block_sha256"]
        assert prov["substrate_brief_sha256"] != prov["delivered_instruction_sha256"]
