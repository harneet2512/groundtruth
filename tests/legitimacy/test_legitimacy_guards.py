"""Tests for scripts/verify/legitimacy.py -- leaderboard-legitimacy guards.

Proves the guardrail:
  - detects stale prebuilt task-specific artifacts (graph.db / brief / localization)
    under GT_FORBID_PREBUILT_GRAPH=1,
  - strips gold/FAIL_TO_PASS and raises loudly if GT reads a forbidden key,
  - builds a manifest with all required keys and writes valid JSON,
  - does NOT flag a graph.db created AFTER job start.
"""

from __future__ import annotations

import importlib.util
import json
import os
import time
from pathlib import Path

import pytest

# --------------------------------------------------------------------------- #
# Load the module by path (it lives under scripts/verify, not an importable pkg).
# --------------------------------------------------------------------------- #

_MODULE_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "verify" / "legitimacy.py"
)
_spec = importlib.util.spec_from_file_location("gt_legitimacy", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
legitimacy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(legitimacy)


@pytest.fixture
def forbid_prebuilt(monkeypatch):
    monkeypatch.setenv("GT_FORBID_PREBUILT_GRAPH", "1")
    yield


def _plant(path: Path, content: str, mtime: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    os.utime(path, (mtime, mtime))


# --------------------------------------------------------------------------- #
# scan / assert_legitimate
# --------------------------------------------------------------------------- #


def test_stale_graph_db_flagged(tmp_path, forbid_prebuilt):
    job_start = time.time()
    stale = job_start - 3600.0  # one hour before the job started
    _plant(tmp_path / "work" / "graph.db", "x", stale)

    found = legitimacy.scan_prebuilt_artifacts(
        "owner__repo-123", str(tmp_path / "work"), job_start
    )
    assert any("prebuilt graph.db older than job start" in m for m in found)

    with pytest.raises(RuntimeError, match="illegitimate_prebuilt_artifact_detected"):
        legitimacy.assert_legitimate(
            "owner__repo-123", str(tmp_path / "work"), job_start
        )


def test_stale_brief_and_localization_flagged(tmp_path, forbid_prebuilt):
    job_start = time.time()
    stale = job_start - 60.0
    _plant(tmp_path / "gt_brief.json", "{}", stale)
    _plant(tmp_path / "gt_localization.json", "{}", stale)

    found = legitimacy.scan_prebuilt_artifacts(
        "owner__repo-123", str(tmp_path), job_start
    )
    assert any("gt_brief.json" in m for m in found)
    assert any("gt_localization.json" in m for m in found)

    with pytest.raises(RuntimeError, match="illegitimate_prebuilt_artifact_detected"):
        legitimacy.assert_legitimate("owner__repo-123", str(tmp_path), job_start)


def test_stale_deep_metrics_flagged(tmp_path, forbid_prebuilt):
    job_start = time.time()
    stale = job_start - 120.0
    task = "owner__repo-9"
    _plant(tmp_path / f"gt_deep_metrics_{task}.json", "{}", stale)

    found = legitimacy.scan_prebuilt_artifacts(task, str(tmp_path), job_start)
    assert any("gt_deep_metrics from earlier run" in m for m in found)


def test_fresh_graph_db_not_flagged(tmp_path, forbid_prebuilt):
    job_start = time.time() - 10.0  # job started 10s ago
    fresh = time.time()  # graph created now, AFTER job start
    _plant(tmp_path / "graph.db", "x", fresh)

    found = legitimacy.scan_prebuilt_artifacts(
        "owner__repo-123", str(tmp_path), job_start
    )
    assert found == []
    # Must not raise.
    legitimacy.assert_legitimate("owner__repo-123", str(tmp_path), job_start)


def test_flag_ignored_when_env_not_set(tmp_path, monkeypatch):
    monkeypatch.delenv("GT_FORBID_PREBUILT_GRAPH", raising=False)
    job_start = time.time()
    _plant(tmp_path / "graph.db", "x", job_start - 3600.0)
    # scan still finds it, but assert is a no-op without the env flag.
    found = legitimacy.scan_prebuilt_artifacts(
        "owner__repo-123", str(tmp_path), job_start
    )
    assert found  # detection works
    legitimacy.assert_legitimate("owner__repo-123", str(tmp_path), job_start)  # no raise


def test_task_specific_cache_dir_flagged(tmp_path, forbid_prebuilt):
    job_start = time.time()
    stale = job_start - 500.0
    task = "owner__repo-77"
    cache = tmp_path / f"cache_{task}"
    _plant(cache / "graph.db", "x", stale)

    found = legitimacy.scan_prebuilt_artifacts(task, str(tmp_path), job_start)
    assert any("task-specific cache dir with prebuilt" in m for m in found)


# --------------------------------------------------------------------------- #
# sanitize_instance_for_gt / leakage guard
# --------------------------------------------------------------------------- #


def test_sanitize_strips_forbidden_fields():
    instance = {
        "instance_id": "owner__repo-1",
        "problem_statement": "fix the bug",
        "patch": "GOLD DIFF",
        "test_patch": "TEST DIFF",
        "FAIL_TO_PASS": ["test_a"],
        "PASS_TO_PASS": ["test_b"],
        "hidden_tests": ["secret"],
        "repo": "owner/repo",
    }
    guarded = legitimacy.sanitize_instance_for_gt(instance)

    # Allowed keys preserved.
    assert guarded["problem_statement"] == "fix the bug"
    assert guarded["instance_id"] == "owner__repo-1"
    assert guarded["repo"] == "owner/repo"

    # Forbidden keys absent from iteration.
    for k in legitimacy.FORBIDDEN_FIELDS:
        assert k not in guarded.keys()


def test_reading_forbidden_key_raises_via_getitem():
    instance = {"problem_statement": "x", "patch": "GOLD", "FAIL_TO_PASS": ["t"]}
    guarded = legitimacy.sanitize_instance_for_gt(instance)
    with pytest.raises(legitimacy.LegitimacyError):
        _ = guarded["patch"]
    with pytest.raises(legitimacy.LegitimacyError):
        _ = guarded["FAIL_TO_PASS"]


def test_reading_forbidden_key_raises_via_get():
    instance = {"problem_statement": "x", "test_patch": "GOLD"}
    guarded = legitimacy.sanitize_instance_for_gt(instance)
    with pytest.raises(legitimacy.LegitimacyError):
        guarded.get("test_patch")
    with pytest.raises(legitimacy.LegitimacyError):
        guarded.get("hidden_tests", default=None)
    # Allowed key with .get default still works.
    assert guarded.get("nonexistent_allowed", "fallback") == "fallback"


# --------------------------------------------------------------------------- #
# build_manifest / write_manifest
# --------------------------------------------------------------------------- #

_REQUIRED_MANIFEST_KEYS = {
    "workflow_run_id",
    "job_id",
    "task_id",
    "gt_git_commit",
    "task_repo_commit",
    "created_epoch",
    "created_iso",
    "container_id",
    "fresh_index_built",
    "prebuilt_graph_used",
    "gold_accessed",
    "fail_to_pass_accessed",
    "hidden_tests_accessed",
    "prebuilt_graph_found",
    "prebuilt_artifacts",
    "graph_deleted_before_index",
    "graph_db_path",
    "graph_db_sha256",
    "index_start_ts",
    "index_end_ts",
    "index_duration_s",
    "closure_rebuild_ts",
    "lsp_enrichment_ts",
    "brief_gen_ts",
    "dataset_source",
    "dataset_local_offline_proof",
    "no_gold_fields_read",
    "no_fail_to_pass_or_hidden_read",
    "require_env",
    "legitimacy_status",
}


def _build(tmp_path, prebuilt_found, dataset_source, monkeypatch=None):
    db = tmp_path / "graph.db"
    db.write_text("graph-bytes", encoding="utf-8")
    t0 = time.time()
    return legitimacy.build_manifest(
        "owner__repo-42",
        str(db),
        str(tmp_path),
        dataset_source=dataset_source,
        prebuilt_found=prebuilt_found,
        graph_deleted_before_index=True,
        index_start=t0,
        index_end=t0 + 1.23456789,
        closure_rebuild_ts=t0 + 0.5,
        lsp_enrichment_ts=t0 + 0.75,
        brief_gen_ts=t0 + 0.9,
    )


def test_build_manifest_has_all_keys(tmp_path):
    manifest = _build(tmp_path, [], "/local/dataset")
    assert set(manifest.keys()) >= _REQUIRED_MANIFEST_KEYS


def test_build_manifest_pass_when_clean(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_DATASETS_OFFLINE", "1")
    manifest = _build(tmp_path, [], "/local/datasets/swebench")
    assert manifest["legitimacy_status"] == "pass"
    assert manifest["prebuilt_graph_used"] is False
    assert manifest["dataset_local_offline_proof"] is True
    assert manifest["graph_db_sha256"]  # non-empty hash
    # 8-decimal rounding on durations (FP subtraction tolerance).
    assert manifest["index_duration_s"] == pytest.approx(1.23456789, abs=1e-6)
    # Stored value is rounded to <= 8 decimal places.
    assert manifest["index_duration_s"] == round(manifest["index_duration_s"], 8)


def test_build_manifest_fail_when_prebuilt_found(tmp_path):
    manifest = _build(tmp_path, ["prebuilt graph.db older than job start: /x"], "/d")
    assert manifest["legitimacy_status"] == "fail"
    assert manifest["prebuilt_graph_used"] is True
    assert manifest["prebuilt_graph_found"] is True


def test_offline_proof_false_without_env(tmp_path, monkeypatch):
    monkeypatch.delenv("HF_DATASETS_OFFLINE", raising=False)
    manifest = _build(tmp_path, [], "/local/dataset")
    assert manifest["dataset_local_offline_proof"] is False


def test_write_manifest_writes_valid_json(tmp_path):
    manifest = _build(tmp_path, [], "/local/dataset")
    out_dir = tmp_path / "out"
    path = legitimacy.write_manifest(manifest, str(out_dir))
    assert os.path.exists(path)
    assert "gt_legitimacy_manifest_owner__repo-42.json" in path
    with open(path, encoding="utf-8") as fh:
        loaded = json.load(fh)
    assert loaded["task_id"] == "owner__repo-42"
    assert set(loaded.keys()) >= _REQUIRED_MANIFEST_KEYS


def test_write_manifest_always_writes_on_fail(tmp_path):
    manifest = _build(tmp_path, ["something"], "/d")
    assert manifest["legitimacy_status"] == "fail"
    out_dir = tmp_path / "out2"
    path = legitimacy.write_manifest(manifest, str(out_dir))
    assert os.path.exists(path)
