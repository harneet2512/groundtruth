"""Process-level determinism pin for the SCOPE-detection weight lever (D7).

Real defect (run 29544917048, task aiogram__aiogram-1594): the Stage-1 brief
determinism gate reported a single diff at

    /metrics/control_participation/0/candidate_sha256_16

i.e. the GT_BRIEF_MINIMAL ``minimal_reduction`` record whose candidate bytes are the
FULL pre-reduction brief. The two acquisitions run in SEPARATE python processes (the
gate's primary is the gate3b-cached result, the witness is a fresh in-process
generation), so ``PYTHONHASHSEED`` differs and set iteration order changes.

Root cause — ``v7_4_brief._adapt_weights_for_issue`` Dimension-2 (scope detection):

    for sym in list(issue_anchors.symbols)[:10]:   # symbols is a SET

``IssueAnchors.symbols`` is a ``set``, so ``list(set)[:10]`` selects WHICH 10 symbols
in hash-seeded iteration order. When the issue names >10 anchor symbols, two processes
sample DIFFERENT 10 -> a different ``_anchor_files`` COUNT -> a different scope branch
(single-file boosts W_REACH/W_PROX, multi-file boosts W_LEX/W_PATH, 2 == ambiguous /
no change) -> different composite weights -> a different ranking -> a different full
brief -> a different canonical control-participation identity. Data-dependent: an issue
with <=10 anchor symbols samples the whole set (order-free) and is byte-identical, so
most tasks pass the same gate.

The fix samples in CANONICAL order — ``sorted(issue_anchors.symbols)[:10]`` — making the
sampled subset a pure function of the set, PYTHONHASHSEED-independent. The determinism
comparison gate is untouched (full-precision bytes, no pin, no quantization).

These tests exercise the REAL producer (``_adapt_weights_for_issue``) in fresh processes
across differing hash seeds against a synthetic graph.db engineered so the sampled-10
straddles the scope threshold. Reliably RED on the pre-fix ``list(...)[:10]`` (>=2
distinct weight vectors across seeds), GREEN on the ``sorted(...)[:10]`` fix. Generic
symbols/files only — the invariant must hold for ANY input.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"

# 12 anchor symbols, engineered as a TIGHT straddle of the scope threshold:
#   * 10 symbols map ONLY to one file (pkg/a.py) -> single-file evidence;
#   * 2 symbols (zsym0, zsym1) each map to a DISTINCT file (pkg/b.py, pkg/c.py).
# The [:10] cut therefore samples a subset whose distinct-file COUNT is:
#   * 1  (single-file branch: boost W_REACH/W_PROX) when it excludes both z-symbols,
#   * 2  (ambiguous: base weights, no change)        when it includes exactly one,
#   * 3  (multi-file branch: boost W_LEX/W_PATH)      when it includes both.
# Which z-symbols land in the first 10 = hash-seeded set-iteration order -> the branch
# swings across seeds on the buggy code. 12 (>10) symbols is what makes [:10] discriminate.
_SINGLE = [f"asym{i:02d}" for i in range(10)]          # all -> pkg/a.py
_MULTI = ["zsym0", "zsym1"]                             # -> pkg/b.py, pkg/c.py
_SYMBOLS = _SINGLE + _MULTI

# Seeds empirically shown to straddle the branch on the pre-fix ``list(set)[:10]`` for
# this input (>=2 distinct weight vectors) — so the invariant is a real RED->GREEN pin.
_SEEDS = (0, 1, 2, 3, 7, 11, 42, 1000, 65535)
_ISSUE = "add a get_value accessor and wire it through the handler"

_BASE = {
    "W_SEM": 0.25, "W_LEX": 0.50, "W_PATH": 0.45, "W_REACH": 0.05,
    "W_PROX": 0.05, "W_HUB": 0.0, "W_COMMIT": 0.0, "W_FRAME": 0.0, "W_CODE_DEF": 0.0,
}

# Child script: build IssueAnchors from the symbol set, run the REAL weight adapter
# against the synthetic graph.db, and emit the canonical weight vector as JSON bytes.
_CHILD = r"""
import json, os, sys
sys.path.insert(0, os.environ["GT_TEST_SRC"])
from groundtruth.pretask.v7_4_brief import _adapt_weights_for_issue
from groundtruth.pretask.anchors import IssueAnchors

syms = set(json.loads(os.environ["GT_TEST_SYMS"]))
ia = IssueAnchors(symbols=syms)
base = json.loads(os.environ["GT_TEST_BASE"])
w = _adapt_weights_for_issue(
    {}, {}, dict(base),
    graph_db=os.environ["GT_TEST_DB"],
    issue_anchors=ia,
    issue_text=os.environ["GT_TEST_ISSUE"],
)
sys.stdout.write(json.dumps(w, sort_keys=True))
"""


def _make_graph_db(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY, name TEXT, file_path TEXT, "
        "is_test INTEGER DEFAULT 0, label TEXT DEFAULT 'Function', "
        "language TEXT DEFAULT 'python')"
    )
    conn.execute(
        "CREATE TABLE edges (id INTEGER PRIMARY KEY, source_id INT, target_id INT, "
        "type TEXT, resolution_method TEXT, confidence REAL)"
    )
    nid = 1
    for s in _SINGLE:
        conn.execute("INSERT INTO nodes(id,name,file_path) VALUES(?,?,?)",
                     (nid, s, "pkg/a.py")); nid += 1
    for i, s in enumerate(_MULTI):
        conn.execute("INSERT INTO nodes(id,name,file_path) VALUES(?,?,?)",
                     (nid, s, f"pkg/b{i:02d}.py")); nid += 1
    conn.commit()
    conn.close()


def _weights_bytes(hash_seed: int, db_path: str, symbols: list[str]) -> bytes:
    env = os.environ.copy()
    env.update({
        "PYTHONHASHSEED": str(hash_seed),
        "GT_TEST_SRC": str(SRC),
        "GT_TEST_DB": db_path,
        "GT_TEST_SYMS": json.dumps(symbols),
        "GT_TEST_BASE": json.dumps(_BASE),
        "GT_TEST_ISSUE": _ISSUE,
    })
    return subprocess.run(
        [sys.executable, "-c", _CHILD],
        cwd=ROOT, env=env, check=True, capture_output=True, timeout=30,
    ).stdout


def test_scope_weights_identical_across_hash_seeds() -> None:
    """The defect, exactly: independent processes, differing hash seeds, one anchor set.

    Pre-fix (``list(set)[:10]``) the sampled 10 differ across seeds -> a different scope
    branch -> different weight bytes (>=2 distinct across ``_SEEDS``) -> RED. Post-fix
    (``sorted(set)[:10]``) EVERY seed yields the byte-identical weight vector -> GREEN.
    (Any two fixed seeds can coincide by luck, so the pin spans the empirically-
    straddling seed set rather than a single 0-vs-1 pair.)
    """
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "g.db")
        _make_graph_db(db)
        emitted = {seed: _weights_bytes(seed, db, _SYMBOLS) for seed in _SEEDS}
    distinct = set(emitted.values())
    assert len(distinct) == 1, (
        "scope-detection weights diverged across processes (set-iteration-order "
        f"leak): {len(distinct)} distinct vectors across seeds "
        f"{ {s: v.decode() for s, v in emitted.items()} }"
    )


def test_small_symbol_set_is_value_preserving() -> None:
    """<=10 anchor symbols: ``sorted(set)[:10]`` samples the WHOLE set, and the
    ``_anchor_files`` COUNT is order-free, so the weights are identical to the
    (already-deterministic) pre-fix behavior. Guards that the fix is value-preserving
    on the common path — it only disambiguates the >10-symbol tail."""
    small = _SINGLE[:3] + _MULTI[:4]  # 7 symbols -> 5 distinct files -> multi-file
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "g.db")
        _make_graph_db(db)
        baseline = _weights_bytes(0, db, small)
        for seed in (1, 7, 65535):
            assert _weights_bytes(seed, db, small) == baseline
    got = json.loads(baseline)
    # 5 distinct anchor files (>=3) -> multi-file branch boosts W_LEX/W_PATH.
    assert got["W_LEX"] == 0.55 and got["W_PATH"] == 0.50
