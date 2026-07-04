"""Item C — in-band consensus FACT-LEDGER render (``GT_CONSENSUS_LEDGER``).

A FORM-only re-grammar of ``v1r_brief._localization_header``: SAME gates, SAME
candidate set, SAME order, SAME returned ``primary_path`` — only the presentation
string changes (invariant-3 safe; no ranking/scoring/selection touched).

  * Flag OFF  => byte-identical to today (the imperative "Edit target:" grammar).
  * Flag ON   => the consensus reads as a verifiable ledger the model reasons over:
                 "N/3 signals agree (grep, structural, semantic)" + the per-leg
                 receipts from ``loc.signals_by_file``, with the imperative
                 "Edit target:" verdict phrasing DROPPED.

red->green + mutation companion: the exact ON/OFF head/output pins pin BOTH sides of
the flag, so reverting the render (always-imperative) reddens the ON pins, and forcing
it always-on reddens the OFF byte-identity pin. Leak invariant: zero test names /
FAIL_TO_PASS / PASS_TO_PASS / gold paths / task-ids / assertions in any emitted byte.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from groundtruth.pretask.graph_localizer import Candidate, LocalizerResult, Witness
from groundtruth.pretask.v1r_brief import _gl_normalize, _localization_header

_LEDGER_ENV = "GT_CONSENSUS_LEDGER"
_ALL_LEGS = ["grep", "structural", "semantic"]


def _render(monkeypatch, loc, db, issue, *, ledger: bool):
    """Call the header with the ledger flag toggled per-call (the flag is read
    inside the function body, so a per-call env toggle is honoured)."""
    if ledger:
        monkeypatch.setenv(_LEDGER_ENV, "1")
    else:
        monkeypatch.delenv(_LEDGER_ENV, raising=False)
    return _localization_header(loc, db, issue)


# ============================ HIGH-tier fixture ==============================
_HIGH_FILE = "evaluator/functions.go"
_HIGH_FUNC = "ParseStep"
_HIGH_ISSUE = "fix ParseStep evaluation"


def _hw(anchor: str, dst: str, conf: float = 1.0) -> Witness:
    return Witness(
        file_path=_HIGH_FILE, anchor=anchor, edge_type="CALLS",
        direction="calls_anchor", verified=True, confidence=conf,
        hop=1, src_symbol=anchor, dst_symbol=dst,
    )


def _high_loc() -> LocalizerResult:
    """HIGH-eligible: >=2 distinct issue anchors, >=2 witnesses converging on the
    named func, agreement 3/3 with all three legs named (lockstep with the count)."""
    witnesses = [
        _hw(_HIGH_FUNC, "Eval", 1.0),
        _hw(_HIGH_FUNC, "Parse", 1.0),
        _hw("Evaluator", "Run", 0.8),
    ]
    cand = Candidate(
        file_path=_HIGH_FILE, score=0.9, witnesses=witnesses,
        lex_hits=2, degree=1, confidence=1.0,
    )
    other = Candidate(
        file_path="evaluator/lexer.go", score=0.4, witnesses=[],
        lex_hits=1, degree=1, confidence=0.2,
    )
    key = _gl_normalize(_HIGH_FILE)
    return LocalizerResult(
        candidates=[cand, other],
        anchor_symbols=[_HIGH_FUNC, "Evaluator"],
        confidence=1.0, confident=True, gate_reason="test",
        agreement_by_file={key: 3},
        signals_by_file={key: list(_ALL_LEGS)},
    )


def _high_graph(db_path: Path, fanin: int = 3) -> str:
    """Graph where the pinned func is a NON-hub (low fan-in) but 5 other files are
    busy — the file-level hub gate passes, the symbol-level gate keeps HIGH. Mirrors
    the live abs-stepped shape used by test_high_pin_hub_witness_demotion. Also seeds
    a TEST node + assertion-shaped property that must NEVER surface (leak guard)."""
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

    def node(name: str, fp: str, is_test: int = 0) -> int:
        return conn.execute(
            "INSERT INTO nodes (label,name,file_path,is_test) VALUES ('Function',?,?,?)",
            (name, fp, is_test)).lastrowid

    def call(s: int, t: int) -> None:
        conn.execute("INSERT INTO edges (source_id,target_id,type,resolution_method) "
                     "VALUES (?,?, 'CALLS','import')", (s, t))

    pin = node(_HIGH_FUNC, _HIGH_FILE)
    for i in range(fanin):
        call(node(f"caller_{i}", f"callers/c{i % 4}.go"), pin)
    for f in range(5):
        fp = f"pkg/busy{f}.go"
        for s in range(26):
            tgt = node(f"sym_{f}_{s}", fp)
            call(node(f"user_{f}_{s}", f"users/u{f}.go"), tgt)
    # Leak bait: a test symbol + a FAIL_TO_PASS/assertion-shaped property.
    tn = node("test_ledger_must_not_leak", "tests/test_functions.go", is_test=1)
    conn.execute("INSERT INTO properties (node_id,kind,value) VALUES (?, 'assertion', ?)",
                 (tn, "assert compute() == FAIL_TO_PASS_sentinel"))
    conn.commit()
    conn.close()
    return str(db_path)


# ============================ MEDIUM-tier fixture ============================
_MED_A = "pkg/handler.py"
_MED_B = "svc/router.py"
_MED_ISSUE = "handle request routing regression"

# EXACT expected renders (today's grammar vs the ledger form). These are the
# byte-identity + mutation pins: a broken/absent render reddens them.
_MED_OFF = (
    '<gt-localization confidence="medium">\n'
    "Candidate edit targets (reason over these):\n"
    "  1. pkg/handler.py\n"
    "  2. svc/router.py\n"
    "</gt-localization>"
)
_MED_ON = (
    '<gt-localization confidence="medium">\n'
    "Candidate edit targets (reason over these):\n"
    "  1. pkg/handler.py — 1/3 signals agree (grep)\n"
    "  2. svc/router.py — 1/3 signals agree (grep)\n"
    "</gt-localization>"
)


def _medium_loc() -> LocalizerResult:
    """Two candidates, agreement 1 (grep only) each: the HIGH gate (needs >=2 +
    witnesses) never fires, and ``_low_tier`` is False (>=1) => the flat MEDIUM
    list renders. No witnesses / no semrank => empty func tails (deterministic)."""
    ca = Candidate(file_path=_MED_A, score=0.6, witnesses=[], lex_hits=1, degree=1, confidence=0.0)
    cb = Candidate(file_path=_MED_B, score=0.5, witnesses=[], lex_hits=1, degree=1, confidence=0.0)
    ka, kb = _gl_normalize(_MED_A), _gl_normalize(_MED_B)
    return LocalizerResult(
        candidates=[ca, cb],
        anchor_symbols=["handle"],
        confidence=0.0, confident=False, gate_reason="test",
        agreement_by_file={ka: 1, kb: 1},
        signals_by_file={ka: ["grep"], kb: ["grep"]},
    )


def _medium_graph(db_path: Path) -> str:
    """Minimal graph: the two candidate files' symbols, NO CALLS edges (so the
    resolved-witness tail is '') and NO nodes_fts (so the semantic-leaf tail is [])."""
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """CREATE TABLE nodes (id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT,
              name TEXT, file_path TEXT, start_line INTEGER DEFAULT 1,
              is_test INTEGER DEFAULT 0);
           CREATE TABLE edges (id INTEGER PRIMARY KEY AUTOINCREMENT,
              source_id INTEGER, target_id INTEGER, type TEXT,
              source_line INTEGER DEFAULT 1, resolution_method TEXT,
              confidence REAL DEFAULT 1.0);"""
    )
    conn.execute("INSERT INTO nodes (label,name,file_path) VALUES ('Function','handle',?)", (_MED_A,))
    conn.execute("INSERT INTO nodes (label,name,file_path) VALUES ('Function','route',?)", (_MED_B,))
    conn.commit()
    conn.close()
    return str(db_path)


# ================================ TESTS =====================================

# ---- flag OFF => byte-identical to today's grammar -------------------------

def test_flag_off_high_byte_identical(tmp_path, monkeypatch):
    """HIGH, flag OFF (unset AND ='0') => today's imperative grammar, identical."""
    db = _high_graph(tmp_path / "g.db")
    loc = _high_loc()
    unset, p1 = _render(monkeypatch, loc, db, _HIGH_ISSUE, ledger=False)
    monkeypatch.setenv(_LEDGER_ENV, "0")  # explicit off is also off
    zero, p2 = _localization_header(loc, db, _HIGH_ISSUE)
    assert unset == zero, "GT_CONSENSUS_LEDGER unset vs ='0' diverged"
    assert p1 == p2 == _HIGH_FILE
    # today's grammar: the imperative head is present, the ledger phrasing is not.
    assert f"Edit target: {_HIGH_FILE} :: {_HIGH_FUNC}" in unset
    assert "signals agree" not in unset


def test_flag_off_medium_byte_identical(tmp_path, monkeypatch):
    """MEDIUM, flag OFF => EXACTLY today's flat candidate list (full-output pin)."""
    db = _medium_graph(tmp_path / "g.db")
    loc = _medium_loc()
    header, primary = _render(monkeypatch, loc, db, _MED_ISSUE, ledger=False)
    assert header == _MED_OFF, f"flag-off drifted from today's grammar:\n{header!r}"
    assert primary == _MED_A
    assert "signals agree" not in header


# ---- flag ON => the fact-ledger form ---------------------------------------

def test_ledger_high_shows_n_of_3_and_receipts(tmp_path, monkeypatch):
    """HIGH, flag ON: the head becomes 'file :: func — N/3 signals agree (legs)'."""
    db = _high_graph(tmp_path / "g.db")
    header, primary = _render(monkeypatch, _high_loc(), db, _HIGH_ISSUE, ledger=True)
    head = header.splitlines()[1]  # index 0 = <gt-localization ...>, 1 = head
    assert head == (
        f"{_HIGH_FILE} :: {_HIGH_FUNC} — 3/3 signals agree (grep, structural, semantic)"
    ), f"ledger head wrong:\n{head!r}"
    assert primary == _HIGH_FILE  # SAME target — no ranking change
    assert 'confidence="high"' in header  # SAME gate/tier


def test_ledger_high_drops_imperative_edit_target(tmp_path, monkeypatch):
    """The imperative 'Edit target:' verdict phrasing is DROPPED under the flag."""
    db = _high_graph(tmp_path / "g.db")
    header, _ = _render(monkeypatch, _high_loc(), db, _HIGH_ISSUE, ledger=True)
    assert "Edit target:" not in header, f"imperative phrasing survived:\n{header}"


def test_ledger_medium_receipts_exact(tmp_path, monkeypatch):
    """MEDIUM, flag ON: each candidate line carries its N/3 receipt (full pin)."""
    db = _medium_graph(tmp_path / "g.db")
    header, primary = _render(monkeypatch, _medium_loc(), db, _MED_ISSUE, ledger=True)
    assert header == _MED_ON, f"ledger medium render wrong:\n{header!r}"
    assert primary == _MED_A  # SAME order/primary — no ranking change


# ---- mutation companion: the flag governs BOTH sides -----------------------

def test_mutation_flag_governs_both_sides(tmp_path, monkeypatch):
    """The ON and OFF heads are DISTINCT and each is the expected exact string.
    Flipping the render always-imperative reddens the ON pin; forcing it always-on
    reddens the OFF pin — the mutation companion for the FORM change."""
    db = _high_graph(tmp_path / "g.db")
    loc = _high_loc()
    off, _ = _render(monkeypatch, loc, db, _HIGH_ISSUE, ledger=False)
    on, _ = _render(monkeypatch, loc, db, _HIGH_ISSUE, ledger=True)
    off_head = off.splitlines()[1]
    on_head = on.splitlines()[1]
    assert off_head == f"Edit target: {_HIGH_FILE} :: {_HIGH_FUNC}"
    assert on_head == (
        f"{_HIGH_FILE} :: {_HIGH_FUNC} — 3/3 signals agree (grep, structural, semantic)"
    )
    assert off_head != on_head
    # MEDIUM twin pin (the receipt must actually change the bytes).
    mdb = _medium_graph(tmp_path / "m.db")
    mloc = _medium_loc()
    m_off, _ = _render(monkeypatch, mloc, mdb, _MED_ISSUE, ledger=False)
    m_on, _ = _render(monkeypatch, mloc, mdb, _MED_ISSUE, ledger=True)
    assert m_off == _MED_OFF and m_on == _MED_ON and m_off != m_on


# ---- leak invariant: zero forbidden tokens in any emitted byte -------------

_FORBIDDEN = (
    "test_ledger_must_not_leak",   # a test symbol name
    "tests/test_functions.go",     # a test file path
    "FAIL_TO_PASS",                # gold-label token
    "PASS_TO_PASS",                # gold-label token
    "FAIL_TO_PASS_sentinel",       # the seeded assertion payload
    "assert ",                     # assertion text
)


def test_leak_zero_high(tmp_path, monkeypatch):
    """The rendered ledger surfaces NO test name / gold label / assertion, even
    though the graph seeds all of them (the header never reads them)."""
    db = _high_graph(tmp_path / "g.db")
    header, _ = _render(monkeypatch, _high_loc(), db, _HIGH_ISSUE, ledger=True)
    lowered = header.lower()
    for tok in _FORBIDDEN:
        assert tok.lower() not in lowered, f"LEAK: {tok!r} appeared in:\n{header}"


def test_leak_zero_medium(tmp_path, monkeypatch):
    """MEDIUM ledger: only file paths + leg names — no forbidden token."""
    db = _medium_graph(tmp_path / "g.db")
    header, _ = _render(monkeypatch, _medium_loc(), db, _MED_ISSUE, ledger=True)
    lowered = header.lower()
    for tok in _FORBIDDEN:
        assert tok.lower() not in lowered, f"LEAK: {tok!r} appeared in:\n{header}"


# ---- determinism: byte-identical on repeat with the flag ON ----------------

def test_deterministic_byte_identical_twice(tmp_path, monkeypatch):
    """Same input, flag ON, twice => byte-identical (deterministic, no LLM)."""
    hdb = _high_graph(tmp_path / "g.db")
    hloc = _high_loc()
    a, pa = _render(monkeypatch, hloc, hdb, _HIGH_ISSUE, ledger=True)
    b, pb = _render(monkeypatch, hloc, hdb, _HIGH_ISSUE, ledger=True)
    assert a == b and pa == pb
    mdb = _medium_graph(tmp_path / "m.db")
    mloc = _medium_loc()
    c, pc = _render(monkeypatch, mloc, mdb, _MED_ISSUE, ledger=True)
    d, pd = _render(monkeypatch, mloc, mdb, _MED_ISSUE, ledger=True)
    assert c == d and pc == pd
