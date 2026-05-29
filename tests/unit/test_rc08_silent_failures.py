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


def test_verify_report_load_jsonl_os_error_returns_empty(tmp_path, monkeypatch):
    """A present .jsonl that errors on read (OS/decode) returns [] silently —
    HEAD behavior preserved — and is NOT counted as content corruption."""
    vr = _import_verify_report()
    p = tmp_path / "killed_tasks.jsonl"
    p.write_text(json.dumps({"a": 1}) + "\n")
    vr._PARSE_FAILURES.clear()

    real_read_text = Path.read_text

    def _boom(self, *a, **kw):
        if self.name == "killed_tasks.jsonl":
            raise OSError("simulated read failure")
        return real_read_text(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", _boom)
    out = vr._load(tmp_path, "killed_tasks.jsonl")
    assert out == []
    # OS/read error is NOT a per-line content-corruption event.
    assert "killed_tasks.jsonl" not in vr._PARSE_FAILURES


def test_verify_report_load_jsonl_unicode_decode_error_returns_empty(tmp_path, monkeypatch):
    """A present .jsonl that raises UnicodeDecodeError on read returns []
    (HEAD behavior) and is NOT counted as content corruption.

    (Whether undecodable raw bytes raise is locale/codepage-dependent, so we
    force the decode fault deterministically rather than relying on the
    platform default encoding.)"""
    vr = _import_verify_report()
    p = tmp_path / "killed_tasks.jsonl"
    p.write_text(json.dumps({"a": 1}) + "\n")
    vr._PARSE_FAILURES.clear()

    real_read_text = Path.read_text

    def _decode_boom(self, *a, **kw):
        if self.name == "killed_tasks.jsonl":
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "simulated")
        return real_read_text(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", _decode_boom)
    out = vr._load(tmp_path, "killed_tasks.jsonl")
    assert out == []
    assert "killed_tasks.jsonl" not in vr._PARSE_FAILURES


# --- RC-08: compute()/_cmd_append corrupt-summary contract -------------------

def _write_min_summary(d: Path):
    (d / "gt_arm_summary.json").write_text(json.dumps({
        "arm": "test", "task_count": 1, "ack_armed_total": 1,
        "steer_delivered_total": 1, "ack_engagement_total": 1,
        "material_edit_total": 1, "delivery_rate": 1.0, "engagement_rate": 1.0,
        "must_ok_rate": 1.0, "has_patch_rate": 1.0,
    }))


def test_compute_clears_parse_failures_per_call(tmp_path):
    """_PARSE_FAILURES is reset at the top of compute() — no stale carryover."""
    vr = _import_verify_report()
    _write_min_summary(tmp_path)
    # Seed a stale count from a prior (hypothetical) compute() call.
    vr._PARSE_FAILURES["leftover.jsonl"] = 99
    vr.compute(tmp_path)
    assert "leftover.jsonl" not in vr._PARSE_FAILURES


def test_compute_surfaces_jsonl_parse_failures(tmp_path):
    """A corrupt killed_tasks.jsonl line is counted AND surfaced in the
    compute() result dict (RC-08 intent honored at the report layer)."""
    vr = _import_verify_report()
    _write_min_summary(tmp_path)
    (tmp_path / "killed_tasks.jsonl").write_text(
        json.dumps({"instance_id": "x", "reason": "y"}) + "\n" + "{bad json\n"
    )
    result = vr.compute(tmp_path)
    assert result["verify_jsonl_parse_failures"].get("killed_tasks.jsonl") == 1
    # And it renders into the operator-facing section.
    section = vr.render_section(result)
    assert "RC-08 jsonl parse failures" in section
    assert "killed_tasks.jsonl" in section


def test_cmd_append_corrupt_json_exits_clean_not_crash(tmp_path, capsys):
    """A present-but-corrupt gt_arm_summary.json yields a clean exit-2 from
    _cmd_append (mirrors the missing-summary contract), NOT an uncaught
    RuntimeError traceback."""
    vr = _import_verify_report()
    (tmp_path / "gt_arm_summary.json").write_text("{not valid json")

    args = type("Args", (), {"run_dir": str(tmp_path), "doc": None,
                             "no_append": True})()
    rc = vr._cmd_append(args)
    assert rc == 2
    err = capsys.readouterr().err
    assert "present-but-corrupt" in err


def test_cmd_append_corrupt_classification_exits_clean(tmp_path, capsys):
    """A valid summary but corrupt run_classification.json also exits clean
    (exit-2), not crash — the os-existence guard doesn't check parseability."""
    vr = _import_verify_report()
    _write_min_summary(tmp_path)
    (tmp_path / "run_classification.json").write_text("{broken")

    args = type("Args", (), {"run_dir": str(tmp_path), "doc": None,
                             "no_append": True})()
    rc = vr._cmd_append(args)
    assert rc == 2
    assert "present-but-corrupt" in capsys.readouterr().err


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
