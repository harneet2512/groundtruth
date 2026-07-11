"""B-6 — per-brief-block delivery receipts (stable per-fact identity).

FINDING (verifyandobserve.md B-6): the step-0 brief is ONE opaque prepend; its
fact-bearing blocks (localization / obligations / contract / scope / companion) have
no per-fact delivery identity, so the consumption grader cannot attribute which BLOCK
was consumed.

FIX: ``_brief_block_receipts(brief_text)`` emits a sidecar map — for each fact-bearing
block, ``{block_id, fact_class, label, char_span:[start,end], content_hash}``. It is
HOST-SIDE METADATA (never rendered into the brief), so the delivered brief bytes are
byte-identical whether or not receipts are populated. Wired into ``V1RBriefResult.
block_receipts`` behind ``GT_BLOCK_RECEIPTS`` (default OFF, additive).

RED→GREEN + MUTATION
--------------------
RED (pre-fix): ``V1RBriefResult`` had no ``block_receipts`` field and no receipt
builder — these tests fail to import / find distinct ids. GREEN: distinct stable ids +
content hashes + byte-identical brief. BITING MUTATIONS (see the report):
  * drop the ``#k`` disambiguation -> the two companion blocks collide -> the
    distinct-id assertion fails;
  * hash a constant instead of the block bytes -> the span/hash-consistency
    assertion fails.
Deterministic, LLM-free, no I/O for the pure tests.
"""
from __future__ import annotations

import hashlib
import sqlite3

import pytest

import groundtruth.pretask.graph_localizer as GL
import groundtruth.pretask.v7_4_brief as V74
from groundtruth.pretask.v1r_brief import (
    _block_receipts_on,
    _brief_block_receipts,
    generate_v1r_brief,
)


# A representative rendered brief exercising every fact-bearing segmentation branch,
# including TWO companion blocks ("Other candidates" + "Scope chain") so distinctness
# of same-label blocks is under test.
_SYNTH_BRIEF = "\n".join([
    "<gt-localization>",
    "Primary edit target: pkg/config.py (HIGH)",
    "</gt-localization>",
    "<gt-task-brief>",
    "<gt-obligations>",
    "- apply_defaults must not mutate the caller mapping",
    "</gt-obligations>",
    "Expected behavior: apply_defaults returns a copy",
    "EDIT-TARGET CONTRACTS (config.py):",
    "  apply_defaults -> calls copy(cfg)  [pkg/config.py:2]",
    "1. pkg/config.py",
    "   Callers: load_config (pkg/loader.py)",
    "2. pkg/loader.py",
    "   Context: module entrypoint",
    "Other candidates: pkg/util.py",
    "Scope chain: config.py -> loader.py",
    "</gt-task-brief>",
])


# --------------------------------------------------------------------------- #
# pure function
# --------------------------------------------------------------------------- #
def test_receipts_cover_fact_blocks_with_distinct_ids():
    receipts = _brief_block_receipts(_SYNTH_BRIEF)
    # 8 fact-bearing blocks: localization-header, obligations, expected-behavior,
    # edit-target-contracts, file-entry-1, file-entry-2, and TWO companion blocks.
    assert len(receipts) == 8, [r["block_id"] for r in receipts]

    ids = [r["block_id"] for r in receipts]
    assert len(set(ids)) == len(ids), f"block ids must be DISTINCT: {ids}"

    # The two same-label companion blocks are disambiguated (the biting mutation).
    assert "companion#0" in ids and "companion#1" in ids, ids

    # fact classes span the finding's named surfaces.
    classes = {r["fact_class"] for r in receipts}
    assert classes == {"localization", "obligations", "contract", "scope"}, classes

    # scaffold (<gt-task-brief> tags) + blank/misc filler are NOT fact-bearing.
    labels = {r["label"] for r in receipts}
    assert "scaffold" not in labels and "misc" not in labels


def test_span_and_hash_are_faithful_to_brief_text():
    receipts = _brief_block_receipts(_SYNTH_BRIEF)
    for r in receipts:
        start, end = r["char_span"]
        seg = _SYNTH_BRIEF[start:end]
        # span points EXACTLY at the block bytes ...
        assert seg, r
        # ... and the content_hash is sha256 of exactly those bytes.
        assert r["content_hash"] == hashlib.sha256(seg.encode("utf-8")).hexdigest(), r
        assert len(r["content_hash"]) == 64

    # A couple of anchor blocks reconstruct their exact text from the span.
    by_id = {r["block_id"]: r for r in receipts}
    ob = by_id["obligations"]
    assert _SYNTH_BRIEF[ob["char_span"][0]:ob["char_span"][1]] == (
        "<gt-obligations>\n"
        "- apply_defaults must not mutate the caller mapping\n"
        "</gt-obligations>"
    )
    etc = by_id["edit-target-contracts"]
    assert _SYNTH_BRIEF[etc["char_span"][0]:etc["char_span"][1]] == (
        "EDIT-TARGET CONTRACTS (config.py):\n"
        "  apply_defaults -> calls copy(cfg)  [pkg/config.py:2]"
    )


def test_receipts_deterministic():
    assert _brief_block_receipts(_SYNTH_BRIEF) == _brief_block_receipts(_SYNTH_BRIEF)


def test_empty_brief_yields_no_receipts():
    assert _brief_block_receipts("") == []


def test_flag_default_off(monkeypatch):
    monkeypatch.delenv("GT_BLOCK_RECEIPTS", raising=False)
    assert _block_receipts_on() is False
    monkeypatch.setenv("GT_BLOCK_RECEIPTS", "1")
    assert _block_receipts_on() is True


# --------------------------------------------------------------------------- #
# end-to-end: brief TEXT byte-identical with the instrumentation ON
# --------------------------------------------------------------------------- #
def _build_graph(db: str) -> None:
    con = sqlite3.connect(db)
    con.executescript(
        """CREATE TABLE nodes(id INTEGER PRIMARY KEY, label TEXT, name TEXT,
             qualified_name TEXT, file_path TEXT, start_line INTEGER, end_line INTEGER,
             signature TEXT, return_type TEXT, is_exported INTEGER, is_test INTEGER,
             language TEXT, parent_id INTEGER);
           CREATE TABLE edges(id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,
             type TEXT, source_line INTEGER, source_file TEXT, resolution_method TEXT,
             confidence REAL, metadata TEXT);"""
    )
    con.execute(
        "INSERT INTO nodes(id,label,name,file_path,start_line,end_line,signature,is_test,language)"
        " VALUES(1,'Function','apply_defaults','pkg/config.py',1,3,'apply_defaults(cfg)',0,'python')"
    )
    con.execute(
        "INSERT INTO nodes(id,label,name,file_path,start_line,end_line,is_test,language)"
        " VALUES(2,'Function','load_config','pkg/loader.py',1,3,0,'python')"
    )
    con.execute(
        "INSERT INTO edges(id,source_id,target_id,type,source_line,resolution_method,confidence)"
        " VALUES(1,2,1,'CALLS',2,'import',1.0)"
    )
    con.commit()
    con.close()


@pytest.fixture()
def _semantic_off(monkeypatch):
    monkeypatch.setattr(GL, "_get_embedder", lambda: None)

    def _zero_model():
        class _Z:
            dim = 8

            def encode(self, texts, **_):
                return [[0.0] * self.dim for _ in texts]

        return _Z()

    monkeypatch.setattr(V74, "_get_model", _zero_model)


def test_instrumentation_is_brief_text_byte_identical(tmp_path, monkeypatch, _semantic_off):
    """B-6 TTD: with the receipt instrumentation ON, the delivered brief TEXT is
    byte-identical to the OFF run, and block_receipts is populated with distinct ids
    + verifiable content hashes."""
    db = str(tmp_path / "g.db")
    _build_graph(db)
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "config.py").write_text(
        "def apply_defaults(cfg):\n    return cfg\n", encoding="utf-8"
    )
    (root / "pkg" / "loader.py").write_text(
        "def load_config():\n    return apply_defaults({})\n", encoding="utf-8"
    )
    issue = "apply_defaults mutates the caller mapping; correct apply_defaults in config"

    monkeypatch.delenv("GT_BLOCK_RECEIPTS", raising=False)
    off = generate_v1r_brief(issue, str(root), db)
    assert off.block_receipts == [], "receipts must be empty by default"

    monkeypatch.setenv("GT_BLOCK_RECEIPTS", "1")
    on = generate_v1r_brief(issue, str(root), db)

    # THE INVARIANT: instrumentation never changes the delivered brief bytes.
    assert on.brief_text == off.brief_text, "B-6 instrumentation changed the brief TEXT"

    # ... and it DOES populate distinct, verifiable receipts.
    assert on.block_receipts, "receipts should be populated when the flag is on"
    ids = [r["block_id"] for r in on.block_receipts]
    assert len(set(ids)) == len(ids), ids
    for r in on.block_receipts:
        s, e = r["char_span"]
        assert r["content_hash"] == hashlib.sha256(
            on.brief_text[s:e].encode("utf-8")
        ).hexdigest(), r
