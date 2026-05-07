"""RC-08 — silent-swallow → counted+logged failures.

These tests cover the new ``groundtruth.observability.silent_failures``
helper plus the ``verify_report._load`` contract change (file missing vs
file present-but-corrupt) and the new silent_failures gate.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

# Make src/ importable for direct pytest invocation.
_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO / "scripts" / "swebench"))


def test_silent_failures_record_appends_jsonl(tmp_path, monkeypatch):
    from groundtruth.observability.silent_failures import record, count_from_file

    sf = tmp_path / "silent_failures.jsonl"
    monkeypatch.setenv("GT_SILENT_FAILURES_FILE", str(sf))

    try:
        raise ValueError("boom")
    except ValueError as exc:
        record("test.site_a", exc)

    record("test.site_b", RuntimeError("bad"))

    assert sf.is_file()
    lines = [json.loads(ln) for ln in sf.read_text().splitlines() if ln.strip()]
    assert len(lines) == 2
    assert lines[0]["site"] == "test.site_a"
    assert lines[0]["exc_type"] == "ValueError"
    assert lines[1]["site"] == "test.site_b"

    records, parse_failures = count_from_file(str(sf))
    assert records == 2
    assert parse_failures == 0


def test_silent_failures_count_separates_corrupt_lines(tmp_path):
    from groundtruth.observability.silent_failures import count_from_file

    sf = tmp_path / "silent_failures.jsonl"
    sf.write_text(
        json.dumps({"site": "ok", "ts": 1.0}) + "\n"
        "{not valid json\n"
        + json.dumps({"site": "ok2", "ts": 2.0}) + "\n"
    )
    records, bad = count_from_file(str(sf))
    assert records == 2
    assert bad == 1


def test_silent_failures_no_env_is_logger_only(tmp_path, monkeypatch):
    from groundtruth.observability.silent_failures import record

    monkeypatch.delenv("GT_SILENT_FAILURES_FILE", raising=False)
    record("test.no_env", RuntimeError("x"))  # must not raise


def test_silent_failures_count_missing_file_returns_zero():
    from groundtruth.observability.silent_failures import count_from_file
    assert count_from_file("/no/such/path") == (0, 0)


# --- verify_report._load contract --------------------------------------------

def _import_verify_report():
    import importlib
    sys.path.insert(0, str(_REPO / "scripts" / "swebench"))
    return importlib.import_module("verify_report")


def test_verify_report_load_missing_file_is_silent(tmp_path):
    vr = _import_verify_report()
    assert vr._load(tmp_path, "no_such.json") == {}
    assert vr._load(tmp_path, "no_such.jsonl") == []


def test_verify_report_load_corrupt_json_raises(tmp_path):
    vr = _import_verify_report()
    p = tmp_path / "broken.json"
    p.write_text("{not valid json")
    with pytest.raises(RuntimeError, match="present-but-corrupt"):
        vr._load(tmp_path, "broken.json")


def test_verify_report_load_jsonl_partial_corruption_counts(tmp_path):
    vr = _import_verify_report()
    p = tmp_path / "partial.jsonl"
    p.write_text(json.dumps({"a": 1}) + "\n" + "garbage\n")
    vr._PARSE_FAILURES.clear()
    out = vr._load(tmp_path, "partial.jsonl")
    assert out == [{"a": 1}]
    assert vr._PARSE_FAILURES.get("partial.jsonl") == 1


# --- v22_brief silent-failure wiring -----------------------------------------

def test_v22_brief_records_rank_files_failure(tmp_path, monkeypatch):
    """rank_files raises → record() is called via the new wrapper, NOT pass."""
    sf = tmp_path / "sf.jsonl"
    monkeypatch.setenv("GT_SILENT_FAILURES_FILE", str(sf))

    fake_pre = type(sys)("groundtruth.pretask.query_preprocessor")
    fake_pre.preprocess = lambda x: x  # type: ignore[attr-defined]
    fake_rank = type(sys)("groundtruth.pretask.v2_ranker")

    def _bad_rank_files(*a, **kw):
        raise RuntimeError("rank_files exploded")

    fake_rank.rank_files = _bad_rank_files  # type: ignore[attr-defined]
    fake_rank.rank_functions = lambda *a, **kw: []  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules,
                        "groundtruth.pretask.query_preprocessor", fake_pre)
    monkeypatch.setitem(sys.modules,
                        "groundtruth.pretask.v2_ranker", fake_rank)

    from groundtruth.pretask import v22_brief
    # generate_brief returns "" if graph_db_path doesn't exist, so create it.
    (tmp_path / "g.db").write_text("")
    out = v22_brief.generate_brief(
        issue_text="bug somewhere",
        repo_path=str(tmp_path),
        graph_db_path=str(tmp_path / "g.db"),
    )
    assert out == ""  # ranked_files empty → empty brief, BUT the failure
                       # must have been recorded.
    assert sf.is_file(), "rank_files exception must be recorded"
    rec = [json.loads(ln) for ln in sf.read_text().splitlines() if ln.strip()]
    assert any("v22_brief.rank_files" == r["site"] for r in rec)
