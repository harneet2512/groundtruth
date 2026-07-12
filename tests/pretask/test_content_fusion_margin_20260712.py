"""SM-10 redirect (2026-07-12) — the content-BM25 leg as a MARGIN-GATED FUSION leg.

Per the production-grade redirect, GT_CONTENT_LEG is no longer a grep-empty-only fallback:
on the grep-NON-empty path it is an ALWAYS-CONSIDERED, provenance-SEPARATED fusion leg in the
composite RRF, MARGIN-GATED so it contributes only when its top body-BM25 hit is clearly
separated (correct-or-quiet otherwise). Double-count with grep is prevented by the DISTINCT
"content" leg, not by the grep-empty exclusion. The margin THRESHOLD is a configurable input
(GT_CONTENT_MARGIN) whose VALUE is measurement-derived (scripts/measure_brief.py) — OWED — so
an UNSET/uncalibrated threshold keeps the fusion leg silent (byte-identical).

Proven here (hermetic, deterministic — BM25 is deterministic; no embedder needed):
  UNIT  _content_margin_threshold: env parsing (None when unset/bad/out-of-range).
  UNIT  _content_leg_margin_ok: fires on a clear margin, correct-or-quiet on a weak/tied one.
  E2E   (b) a behaviour-described issue (gold's BODY, not its name, carries the vocab) ranks
        the gold HIGHER with the fusion leg on (margin cleared) than off.
  E2E   (a) the SAME data with a margin the gate does NOT clear -> ABSTAIN -> ranking
        byte-identical to leg-off (correct-or-quiet), and (off/uncalibrated) byte-identical too.

RED-first / MUTATION: removing the ``_content_rank`` RRF term (disabling the fusion
contribution) reddens the (b) ranking assertion with a TRUE value assertion.

OWED (NOT claimed here, per CLAUDE.md): the measured GT_CONTENT_MARGIN value + the substrate
REBAKE (baked symbol_content_fts + body embeddings) + the measured stratum-B efficacy
(steps-to-first-correct-edit vs off) + the same margin-gate for the GT_SEM_BODY semantic leg
(embedder-gated) + the T2 (post_search) native routing of the behaviour-localization answer.
No stratum-B win / "promoted" is claimed without those.
"""
from __future__ import annotations

import sqlite3

import pytest

from groundtruth.pretask.graph_localizer import (
    _content_leg_margin_ok,
    _content_margin_threshold,
    _normalize,
    localize,
)


def _fts5_available() -> bool:
    try:
        c = sqlite3.connect(":memory:")
        c.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        c.close()
        return True
    except sqlite3.OperationalError:
        return False


# --------------------------------------------------------------------------- #
# UNIT: the margin threshold parser (measurement-derived input, uncalibrated -> None).
# --------------------------------------------------------------------------- #
def test_margin_threshold_env_parsing(monkeypatch):
    monkeypatch.delenv("GT_CONTENT_MARGIN", raising=False)
    assert _content_margin_threshold() is None          # unset -> uncalibrated -> abstain
    monkeypatch.setenv("GT_CONTENT_MARGIN", "")
    assert _content_margin_threshold() is None
    monkeypatch.setenv("GT_CONTENT_MARGIN", "notafloat")
    assert _content_margin_threshold() is None
    monkeypatch.setenv("GT_CONTENT_MARGIN", "1.0")       # out of [0,1) -> None
    assert _content_margin_threshold() is None
    monkeypatch.setenv("GT_CONTENT_MARGIN", "-0.2")
    assert _content_margin_threshold() is None
    monkeypatch.setenv("GT_CONTENT_MARGIN", "0.25")
    assert _content_margin_threshold() == 0.25


# --------------------------------------------------------------------------- #
# UNIT: the margin gate — fires on a clear top hit, correct-or-quiet on a weak field.
# --------------------------------------------------------------------------- #
def test_margin_gate_correct_or_quiet():
    # rows are (id, name, file, -bm25) with higher = better.
    clear = [(1, "a", "a.py", 10.0), (2, "b", "b.py", 2.0)]     # (10-2)/10 = 0.8 margin
    weak = [(1, "a", "a.py", 5.0), (2, "b", "b.py", 4.9)]        # ~0.02 margin
    single = [(1, "a", "a.py", 3.0)]
    assert _content_leg_margin_ok(clear, 0.2) is True
    assert _content_leg_margin_ok(weak, 0.2) is False           # correct-or-quiet
    assert _content_leg_margin_ok(single, 0.2) is True          # one hit = max separation
    assert _content_leg_margin_ok([], 0.2) is False
    # a tie contributes ~0 (never body-BM25-first noise on a flat field).
    tie = [(1, "a", "a.py", 4.0), (2, "b", "b.py", 4.0)]
    assert _content_leg_margin_ok(tie, 0.01) is False
    # the SAME clear data fails a HIGHER threshold (the gate is a real, tunable knob).
    assert _content_leg_margin_ok(clear, 0.9) is False


# --------------------------------------------------------------------------- #
# E2E: the fusion leg promotes a stratum-B gold on the grep-NON-empty path.
# --------------------------------------------------------------------------- #
def _build_fusion_repo(tmp_path):
    """A hermetic repo + graph.db where GREP recalls three lexical DISTRACTORS (each matches
    one issue token by name), but the behaviour GOLD's BODY (symbol_content_fts) carries the
    dense behaviour vocab while its SOURCE matches NO issue token. Off, the gold is buried
    BELOW every grep file; a cleared content margin lifts it. Returns (repo_root, db_path)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    # Three WEAK grep distractors (one issue token each in their source).
    (repo / "handlers.py").write_text("def h():\n    return upgrade\n", encoding="utf-8")
    (repo / "conn.py").write_text("def c():\n    return handshake\n", encoding="utf-8")
    (repo / "svc.py").write_text("def s():\n    return opcode\n", encoding="utf-8")
    # GOLD: generic source (grep finds NO issue token) — its behaviour lives in the indexed BODY.
    (repo / "net.py").write_text("def handle_stream(x):\n    return x\n", encoding="utf-8")
    # A SECONDARY (weak) content hit so the leg has >=2 hits -> a real, tunable margin.
    (repo / "aux.py").write_text("def a(y):\n    return y\n", encoding="utf-8")

    db = tmp_path / "graph.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT NOT NULL, name TEXT NOT NULL,
            qualified_name TEXT, file_path TEXT NOT NULL, start_line INTEGER, end_line INTEGER,
            signature TEXT, return_type TEXT, is_exported BOOLEAN DEFAULT 0,
            is_test BOOLEAN DEFAULT 0, language TEXT NOT NULL, parent_id INTEGER
        );
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER, target_id INTEGER,
            type TEXT NOT NULL, source_line INTEGER, source_file TEXT,
            resolution_method TEXT, confidence REAL DEFAULT 0.0, metadata TEXT
        );
        CREATE VIRTUAL TABLE symbol_content_fts USING fts5(content);
        """
    )
    con.executemany(
        "INSERT INTO nodes (id,label,name,file_path,start_line,end_line,is_exported,is_test,language)"
        " VALUES (?,?,?,?,1,2,1,0,'python')",
        [(1, "Function", "h", "handlers.py"), (2, "Function", "c", "conn.py"),
         (3, "Function", "s", "svc.py"), (4, "Function", "handle_stream", "net.py"),
         (5, "Function", "a", "aux.py")],
    )
    # handlers.py (a grep seed) CALLS the gold + the secondary -> both are hop-1 graph
    # candidates EVEN with the content leg OFF, so this measures a RANKING move (not appear).
    for _t in (4, 5):
        con.execute(
            "INSERT INTO edges (source_id,target_id,type,source_line,source_file,resolution_method,confidence)"
            " VALUES (1,?,'CALLS',2,'handlers.py','import',1.0)", (_t,))
    # GOLD body: dense behaviour vocab (high BM25). Secondary body: a single weak term ->
    # a CLEAR content margin (the gold dominates the field).
    con.execute("INSERT INTO symbol_content_fts(rowid,content) VALUES (4,"
                "'websocket upgrade handshake frame opcode payload masking connection stream "
                "websocket handshake frame masking')")
    con.execute("INSERT INTO symbol_content_fts(rowid,content) VALUES (5,'upgrade')")
    con.commit()
    con.close()
    return str(repo), str(db)


def _rank_of(res, path):
    for i, c in enumerate(res.candidates):
        if _normalize(c.file_path) == _normalize(path):
            return i
    return None


@pytest.mark.skipif(not _fts5_available(), reason="Python sqlite3 built without FTS5")
def test_fusion_leg_promotes_stratum_b_gold_and_is_correct_or_quiet(tmp_path, monkeypatch):
    repo_root, db = _build_fusion_repo(tmp_path)
    issue = "websocket upgrade handshake fails: the opcode frame is rejected during masking"
    GOLD = "net.py"

    def _run(env):
        for f in ("GT_CONTENT_LEG", "GT_CONTENT_MARGIN"):
            monkeypatch.delenv(f, raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        return localize(issue, db, top_k=8, repo_root=repo_root)

    # BASELINE (leg off): grep recalls the distractor; the gold is a low structural candidate.
    off = _run({})
    off_gold = _rank_of(off, GOLD)
    assert off_gold is not None, "gold must be a candidate even off (structural reach)"

    # FUSION ON + margin cleared: the content leg promotes the stratum-B gold ABOVE its
    # leg-off rank.
    on = _run({"GT_CONTENT_LEG": "1", "GT_CONTENT_MARGIN": "0.1"})
    on_gold = _rank_of(on, GOLD)
    assert on_gold is not None
    assert on_gold < off_gold, (
        f"the margin-gated content leg must RANK the stratum-B gold higher "
        f"(on={on_gold} vs off={off_gold})"
    )

    # CORRECT-OR-QUIET: leg on but the margin gate is NOT cleared (threshold too high) ->
    # abstain -> ranking byte-identical to leg-off.
    quiet = _run({"GT_CONTENT_LEG": "1", "GT_CONTENT_MARGIN": "0.999999"})
    assert _rank_of(quiet, GOLD) == off_gold, "a weak/uncleared margin must be a no-op"

    # UNCALIBRATED (GT_CONTENT_LEG on, GT_CONTENT_MARGIN unset) -> fusion abstains ->
    # byte-identical to off.
    uncal = _run({"GT_CONTENT_LEG": "1"})
    assert _rank_of(uncal, GOLD) == off_gold, "an unset (uncalibrated) threshold must abstain"
