"""FIX 4 (2026-06-11 measurement-gate closeout) — HIGH-pin hub-witness demotion.

The inverted-confidence pattern (gt_gt §16.5 issue D, recurring VERBATIM across
runs 27307362054 and 27321848581): a HIGH pin whose witness support converges on
a HUB function (abs-stepped `evaluator/functions.go::New`, csstree fixture.js).
A hub's huge fan-in MANUFACTURES the >=2-distinct-witness convergence the HIGH
gate requires — every caller of the hub is a "distinct structural witness" — so
centrality, not evidence, stamps HIGH on a non-gold file. The existing HUB GATE
is FILE-level (in-degree p80) and passes when other files are similarly busy;
the hole is SYMBOL-level.

Fix: HIGH additionally requires the NAMED func to NOT be a symbol-level hub
(per-task fan-in distribution p80, max-composed with the audited >20-callers
floor). A hub-witness-only pin demotes to the MEDIUM candidate list — cursor
mentality: wrong-confident is worse than uncertain (The Distracting Effect,
arXiv:2505.06914 — plausible-but-wrong context is the maximally harmful class).

Red->green: hub-anchored pin (func with 25 callers) -> MEDIUM, not HIGH;
non-hub pin with identical witness shape -> HIGH preserved (no over-kill).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from groundtruth.pretask.graph_localizer import Candidate, LocalizerResult, Witness
from groundtruth.pretask.v1r_brief import _gl_normalize, _localization_header

_PIN_FILE = "evaluator/functions.go"


def _w(anchor: str, dst: str, conf: float = 1.0) -> Witness:
    return Witness(
        file_path=_PIN_FILE,
        anchor=anchor,
        edge_type="CALLS",
        direction="calls_anchor",
        verified=True,
        confidence=conf,
        hop=1,
        src_symbol=anchor,
        dst_symbol=dst,
    )


def _loc(func: str) -> LocalizerResult:
    """A HIGH-eligible localizer result pinned on ``func``:
    >=2 distinct issue anchors, >=2 distinct witnesses converging on ``func``
    (max-strength), agreement >= 2."""
    witnesses = [
        _w(func, "Eval", 1.0),  # support 1 for func (max strength)
        _w(func, "Parse", 1.0),  # support 2 for func (distinct tuple)
        _w("Evaluator", "Run", 0.8),  # 2nd distinct issue anchor (weaker)
    ]
    cand = Candidate(
        file_path=_PIN_FILE,
        score=0.9,
        witnesses=witnesses,
        lex_hits=2,
        degree=1,
        confidence=1.0,
    )
    other = Candidate(
        file_path="evaluator/lexer.go",
        score=0.4,
        witnesses=[],
        lex_hits=1,
        degree=1,
        confidence=0.2,
    )
    return LocalizerResult(
        candidates=[cand, other],
        anchor_symbols=[func, "Evaluator"],
        confidence=1.0,
        confident=True,
        gate_reason="test",
        agreement_by_file={_gl_normalize(_PIN_FILE): 3},
    )


def _build_graph(db_path: Path, pinned_func: str, pinned_fanin: int) -> str:
    """graph.db where ``pinned_func`` (in _PIN_FILE) has ``pinned_fanin``
    callers, while 5 OTHER files each carry ~26 incoming edges spread over
    26 DISTINCT single-caller symbols — so the FILE-level hub gate passes
    (functions.go in-degree <= p80 of file in-degrees) exactly like the live
    abs-stepped shape, and only the SYMBOL-level fan-in distinguishes the hub."""
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """CREATE TABLE nodes (id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT,
              name TEXT, file_path TEXT, start_line INTEGER DEFAULT 1,
              is_test INTEGER DEFAULT 0);
           CREATE TABLE edges (id INTEGER PRIMARY KEY AUTOINCREMENT,
              source_id INTEGER, target_id INTEGER, type TEXT,
              source_line INTEGER DEFAULT 1, resolution_method TEXT,
              confidence REAL DEFAULT 1.0);
           CREATE TABLE properties (id INTEGER PRIMARY KEY AUTOINCREMENT,
              node_id INTEGER, kind TEXT, value TEXT, line INTEGER);"""
    )

    def node(name: str, fp: str) -> int:
        cur = conn.execute(
            "INSERT INTO nodes (label, name, file_path) VALUES ('Function',?,?)", (name, fp)
        )
        return cur.lastrowid

    def call(src: int, tgt: int) -> None:
        conn.execute(
            "INSERT INTO edges (source_id, target_id, type, resolution_method) "
            "VALUES (?,?, 'CALLS', 'import')",
            (src, tgt),
        )

    pin = node(pinned_func, _PIN_FILE)
    for i in range(pinned_fanin):
        call(node(f"caller_{i}", f"callers/c{i % 4}.go"), pin)
    # 5 busy-but-non-hub files: 26 distinct symbols x 1 caller each.
    for f in range(5):
        fp = f"pkg/busy{f}.go"
        for s in range(26):
            tgt = node(f"sym_{f}_{s}", fp)
            call(node(f"user_{f}_{s}", f"users/u{f}.go"), tgt)
    conn.commit()
    conn.close()
    return str(db_path)


def test_hub_witness_only_pin_demotes_to_medium(tmp_path: Path):
    """func `New` with 25 callers (a symbol hub): centrality manufactured the
    witness convergence -> the imperative HIGH steer must NOT render; the
    candidate list (MEDIUM path) renders instead."""
    db = _build_graph(tmp_path / "g.db", "New", 25)
    header, primary = _localization_header(_loc("New"), db, "fix New evaluation")
    assert 'confidence="high"' not in header, (
        "single-hub-witness HIGH pin survived (inverted confidence):\n" + header
    )
    # not silenced — demoted: the candidate list still shows the file.
    assert _PIN_FILE in header, "demotion must keep the candidate visible:\n" + header
    assert primary == _PIN_FILE


def test_non_hub_pin_keeps_high(tmp_path: Path):
    """Identical witness shape on a NON-hub func (fan-in 3) -> HIGH preserved
    (no over-suppression of legitimate convergence)."""
    db = _build_graph(tmp_path / "g.db", "ParseStep", 3)
    header, primary = _localization_header(_loc("ParseStep"), db, "fix ParseStep evaluation")
    assert 'confidence="high"' in header, "legitimate HIGH over-suppressed:\n" + header
    assert f"Edit target: {_PIN_FILE} :: ParseStep" in header
    assert primary == _PIN_FILE


def test_unreadable_graph_stays_permissive(tmp_path: Path):
    """No graph.db -> the symbol-hub gate cannot judge -> prior behavior
    (HIGH renders; the gate's own failure is never a demotion fact)."""
    header, _ = _localization_header(
        _loc("New"), str(tmp_path / "missing.db"), "fix New evaluation"
    )
    assert 'confidence="high"' in header
