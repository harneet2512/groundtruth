"""LSP contract — did the precision pass do real work, fail, or validly no-op?

Parses resolve.py's machine-readable contract line
  LSP_METRICS resolved=<int> residual=<int> scoped_source_files=<int>
(resolve.py ~1048) plus the verified/corrected/deleted/failed/skipped summary if
present, and applies the user's rule: a tiny/empty in-scope demand on an
already-deterministic graph is LSP_NO_OP_VALID, NOT an LSP failure. Read-only.
"""
from __future__ import annotations

import os
import re

# A demand of <= this many in-scope name_match method-call edges is "trivially small"
# (too small to justify a conversion requirement), per the audit rule.
NOOP_RESIDUAL_ABS = 8
# At/above this deterministic fraction the graph is "already deterministic enough"
# that a small residual is a tail, not a degradation.
NOOP_DET_FLOOR = 0.60

_LSP_LINE = re.compile(
    r"LSP_METRICS\s+resolved=(\d+)\s+residual=(\d+)\s+scoped_source_files=(\d+)")
_STAT = lambda name: re.compile(rf'"?{name}"?\s*[=:]\s*(\d+)')  # noqa: E731


def build_lsp_contract(lsp_metrics_path: str, det_frac: float | None = None) -> dict:
    """det_frac: deterministic CALLS fraction (0..1) from the graph contract, used
    only to decide LSP_NO_OP_VALID. None -> not provided."""
    c: dict = {"contract": "lsp", "lsp_metrics_path": lsp_metrics_path}
    text = ""
    if lsp_metrics_path and os.path.exists(lsp_metrics_path):
        try:
            text = open(lsp_metrics_path, encoding="utf-8", errors="ignore").read()
        except Exception:
            text = ""
    c["contract_line_present"] = False
    c["resolved"] = c["residual"] = c["scoped_source_files"] = None

    m = _LSP_LINE.search(text)
    if m:
        c["contract_line_present"] = True
        c["resolved"] = int(m.group(1))
        c["residual"] = int(m.group(2))
        c["scoped_source_files"] = int(m.group(3))

    # best-effort stats (resolve.py prints a summary dict / lines)
    for k in ("verified", "corrected", "deleted", "failed", "skipped"):
        mm = _STAT(k).search(text)
        c[k] = int(mm.group(1)) if mm else None

    resolved = c["resolved"]
    residual = c["residual"]
    failed = c["failed"] or 0

    # --- the no-op-valid decision (the heart of GATE_FALSE_FAIL avoidance) ---
    noop_valid = False
    noop_reason = ""
    if residual is not None:
        if residual == 0:
            noop_valid, noop_reason = True, "residual=0 (no in-scope name_match demand)"
        elif residual <= NOOP_RESIDUAL_ABS and (det_frac is not None and det_frac >= NOOP_DET_FLOOR):
            noop_valid = True
            noop_reason = (f"residual={residual}<= {NOOP_RESIDUAL_ABS} on an already-"
                           f"deterministic graph (det={det_frac:.2%})")
        elif residual <= NOOP_RESIDUAL_ABS:
            noop_valid = True
            noop_reason = f"residual={residual}<= {NOOP_RESIDUAL_ABS} (trivially small demand)"
    c["lsp_no_op_valid"] = noop_valid
    c["lsp_no_op_reason"] = noop_reason

    # did the LSP do real work?
    c["lsp_did_work"] = bool(resolved and resolved > 0)
    c["resolve_frac"] = (round(resolved / residual, 8)
                         if (resolved is not None and residual) else None)

    # --- verdict ---
    hf: list[str] = []
    if not c["contract_line_present"]:
        hf.append("lsp_metrics_line_absent")  # resolve emitted no contract -> opaque
    if failed and resolved == 0 and not noop_valid:
        hf.append("lsp_real_failure")  # had a real demand, resolved nothing, server errored
    c["hard_fail"] = hf
    # classify.py reads (lsp_did_work, lsp_no_op_valid, hard_fail) to pick
    # LSP_FAIL / LSP_NO_OP_VALID / ok.
    return c


if __name__ == "__main__":
    import json
    import sys

    p = sys.argv[1] if len(sys.argv) > 1 else "/tmp/gt/lsp_metrics.txt"
    df = float(sys.argv[2]) if len(sys.argv) > 2 else None
    print(json.dumps(build_lsp_contract(p, df), indent=2, default=str))
