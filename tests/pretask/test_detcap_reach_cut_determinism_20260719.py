"""DET-CAP (2026-07-19, smoke 29711373486 class C) — the graph-expansion cap cut
must be hash-order independent.

Live kill: llama-factory-7505 — equal-reach files at the [:max_graph_expand]
boundary were admitted in `set` iteration order (per-process PYTHONHASHSEED), so
the gate subprocess and the in-process witness admitted different files
(k_sem_top 73 vs 72 -> DETERMINISM_MISMATCH -> fail-closed, agent never started).

This test runs the cut expression in TWO subprocesses with different
PYTHONHASHSEED (the exact live condition) on a tie plateau straddling the cap:
  RED  — the pre-fix expression (reach-only key, raw set iteration) diverges;
  GREEN — the fixed expression ((-reach, path) key, sorted iteration) is stable;
and verifies the fixed expression is what v7_4_brief.py actually ships.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]

_SNIPPET = r"""
import json, sys
# 12 equal-reach plateau files straddling a cap of 8 + 4 higher-reach files.
files = [f"pkg/mod_{i}.py" for i in range(12)] + [f"pkg/top_{i}.py" for i in range(4)]
reach = {f: (2.0 if f.startswith("pkg/top_") else 1.0) for f in files}
graph_only = set(files)
cap = 8
mode = sys.argv[1]
if mode == "old":
    by_reach = sorted(
        ((fp, reach[fp]) for fp in graph_only if fp in reach),
        key=lambda x: x[1],
        reverse=True,
    )
else:
    by_reach = sorted(
        ((fp, reach[fp]) for fp in sorted(graph_only) if fp in reach),
        key=lambda x: (-x[1], x[0]),
    )
print(json.dumps(sorted(fp for fp, _ in by_reach[:cap])))
"""


def _run(mode: str, seed: str) -> list[str]:
    out = subprocess.run(
        [sys.executable, "-c", _SNIPPET, mode],
        env={"PYTHONHASHSEED": seed, "SYSTEMROOT": "C:\\Windows", "PATH": ""},
        capture_output=True, text=True, check=True,
    )
    return json.loads(out.stdout)


def test_old_expression_diverges_across_hash_seeds():
    results = {json.dumps(_run("old", seed)) for seed in ("0", "1", "2", "3")}
    assert len(results) > 1, (
        "the pre-fix expression must demonstrably flip across hash seeds "
        "(otherwise this test cannot bite)")


def test_fixed_expression_stable_across_hash_seeds():
    results = {json.dumps(_run("fixed", seed)) for seed in ("0", "1", "2", "3")}
    assert len(results) == 1, f"fixed cut must be seed-independent: {results}"


def test_shipped_code_uses_the_fixed_expression():
    src = (_REPO / "src" / "groundtruth" / "pretask" / "v7_4_brief.py").read_text(
        encoding="utf-8")
    block = src[src.index("DET-CAP"):]
    assert "for fp in sorted(graph_only)" in block[:900]
    assert re.search(r"key=lambda x: \(-x\[1\], x\[0\]\)", block[:900])
    # the divergent pre-fix shape must be gone from the cap cut
    cap_region = src[src.index("Cap graph-expanded set"):src.index("Stage A candidate set")]
    assert "reverse=True" not in cap_region
