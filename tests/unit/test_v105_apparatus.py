"""Unit tests for the v1.0.5 apparatus modules.

Covers:
  - groundtruth.mcp.composite              — budget counter + format helpers
  - groundtruth.pretask.v22_brief          — tier mapping + format + degradation
  - groundtruth.runtime.v105_telemetry     — sink writers + payload sidecar
  - groundtruth.runtime.pre_finish_gate    — coverage detection + soft-escape

Tests do NOT depend on a real graph.db, OH SDK, or VM. They're hermetic.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# composite.py
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_counter(tmp_path, monkeypatch):
    """Redirect the per-task counter file into a tmp_path subdir."""
    from groundtruth.mcp import composite

    monkeypatch.setattr(composite, "_DEFAULT_COUNTER_DIR", str(tmp_path))
    yield composite


def test_composite_caps_are_locked(isolated_counter):
    assert isolated_counter._CAPS == {"gt_lookup": 2, "gt_impact": 2, "gt_check": 3}


def test_composite_redirects_complete(isolated_counter):
    for endpoint in ("gt_lookup", "gt_impact", "gt_check"):
        assert endpoint in isolated_counter._REDIRECTS


def test_composite_counter_first_call_allowed(isolated_counter):
    allowed, msg = isolated_counter.check_and_increment("t1", "gt_lookup")
    assert allowed is True
    assert msg == ""


def test_composite_counter_caps_at_threshold(isolated_counter):
    isolated_counter.check_and_increment("t2", "gt_lookup")
    isolated_counter.check_and_increment("t2", "gt_lookup")
    allowed, msg = isolated_counter.check_and_increment("t2", "gt_lookup")
    assert allowed is False
    assert msg.startswith("BUDGET_EXHAUSTED:")
    assert "gt_lookup" in msg
    assert "2" in msg  # cap value cited


def test_composite_counter_independent_per_endpoint(isolated_counter):
    isolated_counter.check_and_increment("t3", "gt_lookup")
    isolated_counter.check_and_increment("t3", "gt_lookup")
    # gt_check counter should NOT be affected.
    allowed, _ = isolated_counter.check_and_increment("t3", "gt_check")
    assert allowed is True


def test_composite_counter_independent_per_task(isolated_counter):
    isolated_counter.check_and_increment("task_a", "gt_lookup")
    isolated_counter.check_and_increment("task_a", "gt_lookup")
    # task_b is a separate counter.
    allowed, _ = isolated_counter.check_and_increment("task_b", "gt_lookup")
    assert allowed is True


def test_composite_tier_mapping(isolated_counter):
    assert isolated_counter._tier_for_score(2) == "[VERIFIED]"
    assert isolated_counter._tier_for_score(3) == "[VERIFIED]"
    assert isolated_counter._tier_for_score(1) == "[WARNING]"
    assert isolated_counter._tier_for_score(0) == "[INFO]"


def test_composite_unknown_endpoint_passes_through(isolated_counter):
    allowed, msg = isolated_counter.check_and_increment("t", "gt_unknown")
    assert allowed is True
    assert msg == ""


# ---------------------------------------------------------------------------
# v22_brief.py
# ---------------------------------------------------------------------------


def test_v22_brief_no_rank_based_tier_helpers():
    # The rank-as-confidence tier helpers (_file_tier/_func_tier) were removed:
    # RRF rank is dimensionless, so neither the delivered brief NOR telemetry may
    # assert a rank-position [VERIFIED]/[WARNING]/[INFO] tiering. Only real,
    # dimensionful signals (rank + score) are kept.
    import groundtruth.pretask.v22_brief as v22

    assert not hasattr(v22, "_file_tier")
    assert not hasattr(v22, "_func_tier")


def test_v22_brief_empty_issue_returns_empty_string(tmp_path):
    from groundtruth.pretask.v22_brief import generate_brief

    assert generate_brief("", "/no", "/no") == ""
    # Whitespace-only also degrades.
    assert generate_brief("   \n\t  ", "/no", "/no") == ""


def test_v22_brief_missing_db_returns_empty(tmp_path):
    from groundtruth.pretask.v22_brief import generate_brief

    out = generate_brief("real issue text", str(tmp_path), str(tmp_path / "absent.db"))
    assert out == ""


def test_v22_brief_format_with_files_and_funcs():
    from groundtruth.pretask.v22_brief import _format_brief
    from groundtruth.pretask.v2_types import RankedFile, RankedFunction

    files = [
        RankedFile(file="src/foo.py", score=0.9),
        RankedFile(file="src/bar.py", score=0.7),
    ]
    funcs = [
        (RankedFunction(file="src/foo.py", function="handle", score=0.8), 42),
        (RankedFunction(file="src/foo.py", function="_validate", score=0.6), 0),  # unknown line
    ]
    text = _format_brief(files, funcs)
    assert "<gt-task-brief>" in text
    assert "</gt-task-brief>" in text
    assert "<gt-focus-functions>" in text
    assert "</gt-focus-functions>" in text
    assert "src/foo.py" in text
    # No rank-position fake tier labels in the rendered brief (correct-or-quiet:
    # tier is a filter, not a display — a rank-1 entry is not "verified" because
    # it ranked first). Honest per-edge provenance lives in <gt-graph-map>, not here.
    assert "[VERIFIED]" not in text
    assert "[WARNING]" not in text
    assert "[INFO]" not in text
    assert "tier=" not in text
    assert "rank=" in text  # rank included
    assert "score=" in text  # score included
    # Unknown line renders as ":?" — never blanks.
    assert ":?" in text
    # The known line renders with line number.
    assert ":42" in text


# ---------------------------------------------------------------------------
# v105_telemetry.py
# ---------------------------------------------------------------------------


@pytest.fixture
def telemetry_root(tmp_path, monkeypatch):
    monkeypatch.setenv("GT_TELEMETRY_ROOT", str(tmp_path))
    return tmp_path


def test_telemetry_layers_exposed(telemetry_root):
    from groundtruth.runtime.v105_telemetry import LAYERS

    assert "layer1_localization" in LAYERS
    assert "layer2_brief" in LAYERS
    assert "layer3_hook" in LAYERS
    assert "layer4_endpoints" in LAYERS
    assert "layer5_gate" in LAYERS
    assert "trajectory_full" in LAYERS


def test_telemetry_dir_creates(telemetry_root):
    from groundtruth.runtime.v105_telemetry import telemetry_dir

    path = Path(telemetry_dir("probe_x"))
    assert path.exists()
    assert path.name == "gt_telemetry_probe_x"


def test_telemetry_layer_path_format(telemetry_root):
    from groundtruth.runtime.v105_telemetry import layer_path

    p = Path(layer_path("layer1_localization", "probe_y"))
    assert p.name == "layer1_localization.jsonl"
    assert p.parent.name == "gt_telemetry_probe_y"


def test_telemetry_log_localization_writes_jsonl(telemetry_root):
    from groundtruth.runtime.v105_telemetry import log_localization, layer_path

    log_localization(
        instance_id="t_loc",
        files=[{"file": "f.py", "rank": 1}],
        functions=[{"file": "f.py", "function": "x", "line": 1}],
    )
    p = Path(layer_path("layer1_localization", "t_loc"))
    assert p.exists()
    rec = json.loads(p.read_text(encoding="utf-8").strip())
    assert rec["files"][0]["file"] == "f.py"
    assert rec["functions"][0]["function"] == "x"
    assert "ts" in rec


def test_telemetry_log_endpoint_writes_payload_sidecar(telemetry_root):
    from groundtruth.runtime.v105_telemetry import log_endpoint, telemetry_dir

    call_id = log_endpoint(
        instance_id="t_ep",
        endpoint="gt_lookup",
        args={"symbol": "foo"},
        output="<gt-evidence>...</gt-evidence>" * 100,  # > 2000 chars
        tier_distribution={"verified": 1},
        budget_remaining=1,
        latency_ms=12.0,
    )
    assert call_id.startswith("gt_lookup-")

    full_dir = Path(telemetry_dir("t_ep")) / "layer4_endpoints_full"
    assert full_dir.exists()
    payload_files = list(full_dir.glob("*.json"))
    assert len(payload_files) == 1
    payload = json.loads(payload_files[0].read_text(encoding="utf-8"))
    assert "output" in payload
    assert payload["args"]["symbol"] == "foo"

    # JSONL preview is truncated.
    jsonl_path = Path(telemetry_dir("t_ep")) / "layer4_endpoints.jsonl"
    rec = json.loads(jsonl_path.read_text(encoding="utf-8").strip())
    assert len(rec["output_preview"]) <= 2000
    assert rec["output_chars"] > 2000


def test_telemetry_log_brief_estimates_tokens(telemetry_root):
    from groundtruth.runtime.v105_telemetry import log_brief, layer_path

    log_brief(instance_id="t_b", text="x" * 400)
    p = Path(layer_path("layer2_brief", "t_b"))
    rec = json.loads(p.read_text(encoding="utf-8").strip())
    # ~4 chars per token → 100.
    assert rec["token_estimate"] == 100


def test_telemetry_log_gate_records_decision(telemetry_root):
    from groundtruth.runtime.v105_telemetry import log_gate, layer_path

    log_gate(
        instance_id="t_g",
        edited_files=["a.py"],
        checked_files=[],
        uncovered=["a.py"],
        attempt=2,
        decision="block",
        intervention="<gt-intervention>...</gt-intervention>",
    )
    p = Path(layer_path("layer5_gate", "t_g"))
    rec = json.loads(p.read_text(encoding="utf-8").strip())
    assert rec["decision"] == "block"
    assert rec["attempt"] == 2
    assert rec["uncovered"] == ["a.py"]


# ---------------------------------------------------------------------------
# pre_finish_gate.py
# ---------------------------------------------------------------------------


@pytest.fixture
def gate_env(tmp_path, monkeypatch, capsys):
    """Stub _edited_files; redirect attempts file to tmp_path."""
    monkeypatch.setenv("GT_INSTANCE_ID", "gate_unit")
    monkeypatch.setenv("GT_TELEMETRY_ROOT", str(tmp_path))
    # Force _attempts_path / _check_log_path under tmp_path so tests don't
    # contaminate /tmp.
    from groundtruth.runtime import pre_finish_gate as pfg

    monkeypatch.setattr(
        pfg, "_attempts_path", lambda: str(tmp_path / "gt_finish_attempts_gate_unit.json")
    )
    monkeypatch.setattr(pfg, "_check_log_path", lambda: str(tmp_path / "gt_check_log.jsonl"))
    return pfg, tmp_path, capsys


def test_gate_no_edits_allows_finish(gate_env):
    pfg, tmp_path, capsys = gate_env
    pfg._edited_files = lambda ws: []
    rc = pfg.main()
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out == ""  # silent → finish allowed


def test_gate_full_coverage_allows_finish(gate_env):
    pfg, tmp_path, capsys = gate_env
    pfg._edited_files = lambda ws: ["src/foo.py"]
    (tmp_path / "gt_check_log.jsonl").write_text(
        json.dumps({"instance_id": "gate_unit", "file": "src/foo.py"}) + "\n",
        encoding="utf-8",
    )
    rc = pfg.main()
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out == ""


def test_gate_uncovered_blocks_with_attempt_counter(gate_env):
    pfg, tmp_path, capsys = gate_env
    pfg._edited_files = lambda ws: ["src/foo.py", "src/bar.py"]
    rc1 = pfg.main()
    out1 = capsys.readouterr().out
    rc2 = pfg.main()
    out2 = capsys.readouterr().out
    rc3 = pfg.main()
    out3 = capsys.readouterr().out
    assert all(r == 0 for r in (rc1, rc2, rc3))
    assert "attempt 1/3" in out1
    assert "attempt 2/3" in out2
    assert "attempt 3/3" in out3
    assert 'expected="gt_check src/foo.py"' in out1


def test_gate_soft_escapes_after_three_attempts(gate_env):
    pfg, tmp_path, capsys = gate_env
    pfg._edited_files = lambda ws: ["src/foo.py"]
    for _ in range(3):
        pfg.main()
        capsys.readouterr()
    # Fourth attempt soft-escapes.
    pfg.main()
    out = capsys.readouterr().out
    assert "soft-escape" in out
    assert 'attempts="4"' in out


def test_gate_normalizes_paths(gate_env):
    pfg, tmp_path, capsys = gate_env
    # Backslash + leading ./ should both normalize.
    assert pfg._normalize("./src/foo.py") == "src/foo.py"
    assert pfg._normalize("src\\bar.py") == "src/bar.py"
    assert pfg._normalize("  src/baz.py  ") == "src/baz.py"


def test_gate_filters_test_files_from_edited(gate_env):
    pfg, _, _ = gate_env
    assert pfg._is_test_file("tests/test_foo.py") is True
    assert pfg._is_test_file("src/test_foo.py") is True  # basename starts with test_
    assert pfg._is_test_file("src/foo.py") is False
    assert pfg._is_test_file("tests/regression/test_x.py") is True
