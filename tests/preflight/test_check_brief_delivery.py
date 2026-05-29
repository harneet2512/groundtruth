"""Fixture tests proving scripts/verify/check_brief_delivery.py catches each
delivery failure: valid PASS, empty graph-map FAIL, double-wrap FAIL, leak FAIL.
Also proves it reads the agent-facing `instruction`, NOT `gt_brief_full`.

New opt-in gates covered here:
  - --require-balanced-contracts : malformed Contract:/Preserve: guard FAILS,
    balanced guard PASSES (C1 regression guard).
  - leak set includes [GT_RANK_DIAG] / [GT_BRIEF_DIAG].
  - --require-layer-markers : L3/L3b/L6 markers asserted in observation content,
    only when their trigger (edit / edit->review) is present.
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


def _write_records(tmp_path, records: list[dict]) -> str:
    p = tmp_path / "output.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
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


# ---------------------------------------------------------------------------
# --require-balanced-contracts (C1 regression guard)
# ---------------------------------------------------------------------------

# A Contract: guard whose value was shred mid-token by an unbalanced clip:
# unterminated string literal + unbalanced '(' — the exact C1 failure shape.
_MALFORMED_CONTRACT = (
    "<gt-task-brief>\n1. haystack/document_splitter.py (def run(self, documents):)\n"
    '   Contract: raise TypeError("DocumentSplitter expects a List of Document\n'
    "</gt-task-brief>\n<gt-graph-map>\nhaystack/document_splitter.py :: run\n</gt-graph-map>\nissue"
)
# Dangling trailing binary operator — second canonical malformed shape.
_MALFORMED_PRESERVE = (
    "<gt-task-brief>\n1. pkg/mod.py (def run(self):)\n"
    "   Preserve: guard_clause: (documents and not\n"
    "</gt-task-brief>\n<gt-graph-map>\npkg/mod.py :: run\n</gt-graph-map>\nissue"
)
# Same field, but the guard is balanced and complete — must PASS.
_BALANCED_CONTRACT = (
    "<gt-task-brief>\n1. haystack/document_splitter.py (def run(self, documents):)\n"
    '   Contract: raise TypeError("DocumentSplitter expects a List of Document.")\n'
    "</gt-task-brief>\n<gt-graph-map>\nhaystack/document_splitter.py :: run\n</gt-graph-map>\nissue"
)


def test_malformed_contract_fails_require_balanced(tmp_path):
    r = cbd.check_brief_delivery(
        _write(tmp_path, _MALFORMED_CONTRACT), require_balanced_contracts=True
    )
    assert r["passed"] is False, r["reasons"]
    assert r["malformed_contract_found"] is True
    assert any("contract" in reason.lower() for reason in r["reasons"])


def test_dangling_operator_preserve_fails_require_balanced(tmp_path):
    r = cbd.check_brief_delivery(
        _write(tmp_path, _MALFORMED_PRESERVE), require_balanced_contracts=True
    )
    assert r["passed"] is False, r["reasons"]
    assert r["malformed_contract_found"] is True


def test_balanced_contract_passes_require_balanced(tmp_path):
    r = cbd.check_brief_delivery(
        _write(tmp_path, _BALANCED_CONTRACT),
        require_graph_map=True,
        require_balanced_contracts=True,
    )
    assert r["passed"] is True, r["reasons"]
    assert r["malformed_contract_found"] is False


def test_malformed_contract_computed_but_not_gated_without_flag(tmp_path):
    """Negative control: the malformed guard is detected (computed always)
    but does NOT fail when the opt-in flag is off — existing callers unaffected."""
    r = cbd.check_brief_delivery(_write(tmp_path, _MALFORMED_CONTRACT))
    assert r["malformed_contract_found"] is True
    assert r["passed"] is True, r["reasons"]


# ---------------------------------------------------------------------------
# leak set includes [GT_RANK_DIAG] / [GT_BRIEF_DIAG]
# ---------------------------------------------------------------------------

_RANK_DIAG_LEAK = _VALID.replace(
    "</gt-task-brief>",
    "</gt-task-brief>\n[GT_RANK_DIAG] #1 score=0.71 app/core.py :: run",
)
_BRIEF_DIAG_LEAK = _VALID.replace(
    "</gt-task-brief>",
    "</gt-task-brief>\n[GT_BRIEF_DIAG] candidates=5 elapsed=12ms",
)


def test_rank_diag_leak_fails(tmp_path):
    r = cbd.check_brief_delivery(_write(tmp_path, _RANK_DIAG_LEAK))
    assert r["passed"] is False
    assert r["leak_found"] is True
    assert "[GT_RANK_DIAG]" in r["leaked_markers"]


def test_brief_diag_leak_fails(tmp_path):
    r = cbd.check_brief_delivery(_write(tmp_path, _BRIEF_DIAG_LEAK))
    assert r["passed"] is False
    assert "[GT_BRIEF_DIAG]" in r["leaked_markers"]


# ---------------------------------------------------------------------------
# --require-layer-markers : L3 / L3b / L6 in observation content, trigger-gated
# ---------------------------------------------------------------------------

def _brief_rec() -> dict:
    return {"instruction": _VALID}


def test_layer_markers_present_pass(tmp_path):
    """Edit happened AND edit->review transition exists; all markers present -> PASS."""
    records = [
        _brief_rec(),
        {"history": [
            {"action": "edit", "args": {"path": "app/core.py"}},
            {"observation": "run", "content": "<gt-evidence>\n[CALLER] helper calls run\n</gt-evidence>"},
            {"observation": "run", "content": "[CONTRACT] run(self) -> value | raises ValueError"},
            {"action": "finish", "args": {}},
            {"observation": "run", "content": "[GT_VERIFY] Tests covering app/core.py: test_run"},
        ]},
    ]
    r = cbd.check_brief_delivery(_write_records(tmp_path, records), require_layer_markers=True)
    assert r["passed"] is True, r["reasons"]
    assert r["edit_seen"] is True
    assert r["l3_evidence_seen"] is True
    assert r["l3b_contract_seen"] is True
    assert r["edit_review_transition"] is True
    assert r["l6_verify_seen"] is True


def test_layer_markers_missing_l3_fails(tmp_path):
    """Edit happened (trigger present) but L3 <gt-evidence> absent -> FAIL."""
    records = [
        _brief_rec(),
        {"history": [
            {"action": "edit", "args": {"path": "app/core.py"}},
            {"observation": "run", "content": "[CONTRACT] run(self) -> value"},
        ]},
    ]
    r = cbd.check_brief_delivery(_write_records(tmp_path, records), require_layer_markers=True)
    assert r["passed"] is False
    assert r["edit_seen"] is True
    assert r["l3_evidence_seen"] is False
    assert any("L3" in reason for reason in r["reasons"])


def test_layer_markers_no_edit_no_failure(tmp_path):
    """No edit anywhere -> no L3/L3b/L6 trigger -> PASS even with the flag on.
    A layer with NO trigger is NOT a failure."""
    records = [
        _brief_rec(),
        {"history": [
            {"action": "read", "args": {"path": "app/core.py"}},
            {"observation": "read", "content": "file contents, no GT markers"},
        ]},
    ]
    r = cbd.check_brief_delivery(_write_records(tmp_path, records), require_layer_markers=True)
    assert r["passed"] is True, r["reasons"]
    assert r["edit_seen"] is False
    assert r["edit_review_transition"] is False


def test_layer_markers_l6_only_required_on_edit_review_transition(tmp_path):
    """Edit happened, L3/L3b present, but NO review transition -> L6 not required -> PASS."""
    records = [
        _brief_rec(),
        {"history": [
            {"action": "edit", "args": {"path": "app/core.py"}},
            {"observation": "run", "content": "<gt-evidence>\n[CALLER] x\n</gt-evidence>"},
            {"observation": "run", "content": "[CONTRACT] run(self) -> value"},
        ]},
    ]
    r = cbd.check_brief_delivery(_write_records(tmp_path, records), require_layer_markers=True)
    assert r["passed"] is True, r["reasons"]
    assert r["edit_review_transition"] is False
    assert r["l6_verify_seen"] is False


def test_layer_markers_l6_missing_on_transition_fails(tmp_path):
    """Edit->review transition present but L6 [GT_VERIFY] Tests covering absent -> FAIL."""
    records = [
        _brief_rec(),
        {"history": [
            {"action": "edit", "args": {"path": "app/core.py"}},
            {"observation": "run", "content": "<gt-evidence>\n[CALLER] x\n</gt-evidence>"},
            {"observation": "run", "content": "[CONTRACT] run(self) -> value"},
            {"action": "finish", "args": {}},
            {"observation": "run", "content": "submitted, no verify marker"},
        ]},
    ]
    r = cbd.check_brief_delivery(_write_records(tmp_path, records), require_layer_markers=True)
    assert r["passed"] is False
    assert r["edit_review_transition"] is True
    assert r["l6_verify_seen"] is False
    assert any("L6" in reason for reason in r["reasons"])


def test_layer_markers_ignores_telemetry_and_instruction(tmp_path):
    """Markers in the instruction or in a telemetry field must NOT satisfy the
    observation-content requirement: only history `content` counts."""
    records = [
        # brief instruction itself contains <gt-evidence>/[CONTRACT] — must be ignored
        {"instruction": _VALID + "\n<gt-evidence>x</gt-evidence>\n[CONTRACT] noise"},
        {"history": [
            {"action": "edit", "args": {"path": "app/core.py"}},
            # telemetry field, NOT observation content
            {"gt_layer_event": "<gt-evidence>telemetry</gt-evidence>", "observation": "run", "content": "plain output"},
        ]},
    ]
    r = cbd.check_brief_delivery(_write_records(tmp_path, records), require_layer_markers=True)
    assert r["passed"] is False
    assert r["l3_evidence_seen"] is False
    assert r["l3b_contract_seen"] is False
