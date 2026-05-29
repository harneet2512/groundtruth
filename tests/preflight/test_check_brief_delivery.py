"""Fixture tests proving scripts/verify/check_brief_delivery.py catches each
delivery failure: valid PASS, empty graph-map FAIL, double-wrap FAIL, leak FAIL.
Also proves it reads the agent-facing `instruction`, NOT `gt_brief_full`.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

VERIFIER = Path(__file__).resolve().parents[2] / "scripts" / "verify" / "check_brief_delivery.py"
_spec = importlib.util.spec_from_file_location("check_brief_delivery", VERIFIER)
cbd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cbd)


def _write(tmp_path, instruction: str, gt_brief_full: str | None = None) -> str:
    rec = {"instruction": instruction}
    if gt_brief_full is not None:
        rec["gt_brief_full"] = gt_brief_full
    p = tmp_path / "output.jsonl"
    p.write_text(json.dumps(rec) + "\n", encoding="utf-8")
    return str(p)


_VALID = (
    "<gt-task-brief>\n1. app/core.py (def run(self):)\n   Contract: raises ValueError | returns value\n"
    "</gt-task-brief>\n<gt-graph-map>\napp/core.py :: run\n  calls: helper (app/util.py)\n</gt-graph-map>\n"
    "\n<uploaded_files>\nrepo\n</uploaded_files>\nissue text"
)
_EMPTY_MAP = _VALID.replace("app/core.py :: run\n  calls: helper (app/util.py)\n", "")
_DOUBLE_WRAP = "<gt-task-brief>\n" + _VALID  # nested: two open tags
_LEAK = _VALID.replace("</gt-task-brief>", "</gt-task-brief>\n[GT_RANK_DIAG] #1 score=0.7 app/core.py")


def test_valid_passes(tmp_path):
    r = cbd.check_brief_delivery(_write(tmp_path, _VALID), require_graph_map=True)
    assert r["passed"] is True, r["reasons"]
    assert r["task_brief_open"] == 1 and r["task_brief_close"] == 1
    assert r["graph_map_body_len"] > 0


def test_empty_graph_map_fails(tmp_path):
    r = cbd.check_brief_delivery(_write(tmp_path, _EMPTY_MAP), require_graph_map=True)
    assert r["passed"] is False
    assert r["graph_map_present"] is True and r["graph_map_body_len"] == 0
    assert any("EMPTY" in reason or "empty" in reason for reason in r["reasons"])


def test_double_wrap_fails(tmp_path):
    r = cbd.check_brief_delivery(_write(tmp_path, _DOUBLE_WRAP))
    assert r["passed"] is False
    assert r["task_brief_open"] == 2
    assert any("<gt-task-brief>" in reason for reason in r["reasons"])


def test_diagnostic_leak_fails(tmp_path):
    r = cbd.check_brief_delivery(_write(tmp_path, _LEAK))
    assert r["passed"] is False
    assert r["leak_found"] is True
    assert "[GT_RANK_DIAG]" in r["leaked_markers"]


def test_reads_instruction_not_gt_brief_full(tmp_path):
    """If the instruction is broken (empty map) but gt_brief_full is healthy,
    the verifier must FAIL — proving it reads the agent-facing artifact only."""
    healthy_full = _VALID  # the logging copy is fine...
    p = _write(tmp_path, _EMPTY_MAP, gt_brief_full=healthy_full)  # ...but the instruction is broken
    r = cbd.check_brief_delivery(p, require_graph_map=True)
    assert r["passed"] is False, "must judge the instruction, not gt_brief_full"
    assert r["graph_map_body_len"] == 0
