"""Blocker B — consensus overconfidence demotion (run-28886910434 audit).

`3/3 signals agree` was rendered on a NON-gold file (cfn-3749 update_schemas_manually.py)
whose three agreeing legs were all vocabulary-family (grep/structural/semantic on a
name+signature passage) — the RRF independence assumption is violated, so a lexical match
read as strong as an edge-corroborated file. The fix flags MULTI-signal (>=2) agreement as
"— lexical (no verified issue link)" when NO verified issue-anchored witness backs the file,
while leaving edge-corroborated files (and lone 1/3) unqualified.

Pins: lexical-only >=2 -> qualified; corroborated -> clean; 1/3 -> unqualified; flag OFF ->
byte-identical (no "signals agree", no "lexical" token). Leak invariant: fixed literal only.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from groundtruth.pretask.graph_localizer import Candidate, LocalizerResult, Witness
from groundtruth.pretask.v1r_brief import _gl_normalize, _localization_header

_ALL_LEGS = ["grep", "structural", "semantic"]
_ANCHORS = ["ParseStep", "Evaluator"]


def _graph(db_path: Path) -> str:
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """CREATE TABLE nodes (id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT,
              name TEXT, file_path TEXT, start_line INTEGER DEFAULT 1, is_test INTEGER DEFAULT 0);
           CREATE TABLE edges (id INTEGER PRIMARY KEY AUTOINCREMENT, src INTEGER, dst INTEGER,
              type TEXT, resolution_method TEXT, confidence REAL DEFAULT 1.0);"""
    )
    conn.commit()
    conn.close()
    return str(db_path)


def _corr_witness() -> Witness:
    # verified + anchor IN anchor_symbols -> implementation-role corroborated. ONE distinct
    # anchor only, so it does NOT reach HIGH (needs >=2 distinct) -> renders in the MEDIUM list.
    return Witness(
        file_path="svc/corr.py", anchor="ParseStep", edge_type="CALLS",
        direction="calls_anchor", verified=True, confidence=1.0,
        hop=1, src_symbol="ParseStep", dst_symbol="Eval",
    )


def _loc() -> LocalizerResult:
    corr = Candidate(file_path="svc/corr.py", score=0.90, witnesses=[_corr_witness()],
                     lex_hits=2, degree=1, confidence=0.9)
    lex = Candidate(file_path="svc/lex.py", score=0.80, witnesses=[],
                    lex_hits=2, degree=1, confidence=0.5)
    weak = Candidate(file_path="svc/weak.py", score=0.70, witnesses=[],
                     lex_hits=1, degree=1, confidence=0.3)
    return LocalizerResult(
        candidates=[corr, lex, weak],
        anchor_symbols=list(_ANCHORS),
        confidence=0.6, confident=True, gate_reason="test",
        agreement_by_file={
            _gl_normalize("svc/corr.py"): 3,   # corroborated, 3/3
            _gl_normalize("svc/lex.py"): 3,    # lexical-only, 3/3  -> QUALIFIED
            _gl_normalize("svc/weak.py"): 1,   # lone grep, 1/3     -> unqualified
        },
        signals_by_file={
            _gl_normalize("svc/corr.py"): list(_ALL_LEGS),
            _gl_normalize("svc/lex.py"): list(_ALL_LEGS),
            _gl_normalize("svc/weak.py"): ["grep"],
        },
    )


_QUAL = "— lexical (no verified issue link)"


def test_lexical_only_multisignal_is_demoted(tmp_path, monkeypatch):
    monkeypatch.setenv("GT_CONSENSUS_LEDGER", "1")
    db = _graph(tmp_path / "g.db")
    head, _ = _localization_header(_loc(), db, "fix ParseStep evaluation")
    lines = head.splitlines()
    lex_line = next(l for l in lines if "svc/lex.py" in l)
    corr_line = next(l for l in lines if "svc/corr.py" in l)
    weak_line = next(l for l in lines if "svc/weak.py" in l)
    # lexical-only 3/3 -> qualified
    assert "3/3 signals agree" in lex_line and _QUAL in lex_line, lex_line
    # corroborated 3/3 -> clean (no qualifier)
    assert "3/3 signals agree" in corr_line and _QUAL not in corr_line, corr_line
    # lone 1/3 -> not qualified (already visibly weak)
    assert _QUAL not in weak_line, weak_line


def test_flag_off_is_byte_identical_no_qualifier(tmp_path, monkeypatch):
    monkeypatch.delenv("GT_CONSENSUS_LEDGER", raising=False)
    db = _graph(tmp_path / "g.db")
    head, _ = _localization_header(_loc(), db, "fix ParseStep evaluation")
    # OFF -> imperative grammar; no ledger receipts and, critically, no lexical qualifier.
    assert "signals agree" not in head
    assert "lexical (no verified issue link)" not in head


def test_qualifier_is_leak_safe_fixed_literal(tmp_path, monkeypatch):
    monkeypatch.setenv("GT_CONSENSUS_LEDGER", "1")
    db = _graph(tmp_path / "g.db")
    head, _ = _localization_header(_loc(), db, "fix ParseStep evaluation")
    for banned in ("FAIL_TO_PASS", "PASS_TO_PASS", "::test_", "def test_", "assert "):
        assert banned not in head
