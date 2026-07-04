"""TASK #47-remainder — relevance gating for [RECALL] and [FORMAT] noise signals.

Artifact-first TTD. The observed defect (smoke trajectory):
  - [RECALL] replayed a stale post_view method-dump keyed to ``progress_write``
    while the agent had edited ``set_fields`` — unrelated content rendered as
    evidence.
  - [FORMAT] emitted irrelevant fixture keys (literal strings ``path`` and
    ``SKIP_SLOW_TESTS``) unrelated to the edited function's contract.

These are non-edge signals (no CALLS/IMPORTS edge backs them), so the
categorical edge filter cannot gate them. They need a relevance gate keyed to
the edited function's identifier tokens OR the issue terms, else they inject
noise. Correct-or-quiet: when neither anchor is available, drop.

Both halves of the gate live in ``groundtruth.config.evidence_markers`` (the
leaf config module) so every emitter (the wrapper's [RECALL] path and
``format_contract``'s [FORMAT] path) can reuse them without importing hooks.
"""
from __future__ import annotations

import sqlite3

import pytest

from groundtruth.config.evidence_markers import (
    identifier_tokens,
    passes_relevance_gate,
)
from groundtruth.evidence.format_contract import mine_return_shape


# ---------------------------------------------------------------------------
# Part A — the reusable gate helper (config/evidence_markers.py)
# ---------------------------------------------------------------------------
class TestIdentifierTokens:
    def test_snake_case_split(self):
        assert identifier_tokens("set_fields") == {"set", "fields", "set_fields"}

    def test_camel_case_split(self):
        toks = identifier_tokens("embedAlbum")
        assert "embed" in toks
        assert "album" in toks
        assert "embedalbum" in toks

    def test_drops_short_subtokens_but_keeps_full(self):
        # "is" is < 3 chars -> dropped as a sub-token, full name kept.
        toks = identifier_tokens("is_open")
        assert "open" in toks
        assert "is_open" in toks
        assert "is" not in toks

    def test_empty(self):
        assert identifier_tokens("") == set()


class TestPassesRelevanceGate:
    def test_overlap_with_fn_tokens_passes(self):
        assert passes_relevance_gate(
            'Callers access keys: "fields"', set(), {"set", "fields", "set_fields"}
        )

    def test_overlap_with_issue_terms_passes(self):
        assert passes_relevance_gate(
            "progress_write dump", {"progress", "write"}, set()
        )

    def test_no_overlap_drops(self):
        # The literal-fixture-key noise: "path"/"SKIP_SLOW_TESTS" vs an
        # unrelated edited fn -> no overlap -> dropped.
        assert not passes_relevance_gate(
            'Callers access keys: "path", "SKIP_SLOW_TESTS"',
            {"timezone", "offset"},
            {"normalize", "tz"},
        )

    def test_no_anchor_drops(self):
        # Correct-or-quiet: no issue terms AND no fn tokens -> cannot judge ->
        # drop rather than launder.
        assert not passes_relevance_gate("anything at all", set(), set())

    def test_empty_text_drops(self):
        assert not passes_relevance_gate("", {"set"}, {"fields"})


# ---------------------------------------------------------------------------
# Part B — [FORMAT] relevance gating end-to-end (evidence/format_contract.py)
# ---------------------------------------------------------------------------
def _build_db(tmp_path, *, caller_keys: list[str]) -> tuple[str, str]:
    """Build a minimal graph.db + caller file so mine_return_shape emits
    [FORMAT] lines whose keys are the supplied (noise) fixture keys.

    Returns (db_path, repo_root).
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    # edited function lives here
    (repo / "target.py").write_text(
        "def normalize_tz(dt):\n    return dt\n", encoding="utf-8"
    )
    # caller subscripts the return value with the noise keys
    subs = "".join(f'    _ = result["{k}"]\n' for k in caller_keys)
    (repo / "caller.py").write_text(
        "from target import normalize_tz\n"
        "def use():\n"
        "    result = normalize_tz(0)\n" + subs,
        encoding="utf-8",
    )

    db_path = str(tmp_path / "graph.db")
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT, name TEXT, qualified_name TEXT, file_path TEXT,
            start_line INTEGER, end_line INTEGER, signature TEXT,
            return_type TEXT, is_exported INTEGER, is_test INTEGER,
            language TEXT, parent_id INTEGER
        );
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER, target_id INTEGER, type TEXT,
            source_line INTEGER, source_file TEXT, resolution_method TEXT,
            confidence REAL, metadata TEXT
        );
        """
    )
    # target node id=1, caller node id=2
    conn.execute(
        "INSERT INTO nodes (id,label,name,file_path,start_line,end_line,is_test,language) "
        "VALUES (1,'Function','normalize_tz','target.py',1,2,0,'python')"
    )
    conn.execute(
        "INSERT INTO nodes (id,label,name,file_path,start_line,end_line,is_test,language) "
        "VALUES (2,'Function','use','caller.py',2,4,0,'python')"
    )
    # caller -> target CALLS edge at the call line (line 3 of caller.py)
    conn.execute(
        "INSERT INTO edges (source_id,target_id,type,source_line,confidence) "
        "VALUES (2,1,'CALLS',3,1.0)"
    )
    conn.commit()
    conn.close()
    return db_path, str(repo)


class TestFormatRelevanceGate:
    def test_irrelevant_format_keys_suppressed(self, tmp_path):
        """RED-before-fix: noise fixture keys unrelated to the edited fn must
        not render as [FORMAT] evidence."""
        db_path, repo = _build_db(tmp_path, caller_keys=["path", "SKIP_SLOW_TESTS"])
        out = mine_return_shape(
            db_path, "target.py", "normalize_tz", repo,
            issue_terms={"timezone", "offset"},
        )
        joined = "\n".join(out)
        assert "path" not in joined
        assert "SKIP_SLOW_TESTS" not in joined
        assert out == [], f"expected suppression, got: {out!r}"

    def test_relevant_format_keys_preserved(self, tmp_path):
        """Positive control: a key overlapping the edited fn's tokens survives."""
        db_path, repo = _build_db(tmp_path, caller_keys=["normalize", "other"])
        out = mine_return_shape(
            db_path, "target.py", "normalize_tz", repo,
            issue_terms=set(),
        )
        joined = "\n".join(out)
        assert any("[FORMAT]" in line for line in out), f"expected [FORMAT], got {out!r}"
        assert "normalize" in joined

    def test_relevant_via_issue_terms_preserved(self, tmp_path):
        """Positive control: overlap via issue terms (not fn tokens) survives."""
        db_path, repo = _build_db(tmp_path, caller_keys=["offset", "junk"])
        out = mine_return_shape(
            db_path, "target.py", "normalize_tz", repo,
            issue_terms={"offset"},
        )
        joined = "\n".join(out)
        assert any("[FORMAT]" in line for line in out), f"expected [FORMAT], got {out!r}"
        assert "offset" in joined

    def test_backward_compatible_no_kwargs(self, tmp_path):
        """The legacy 4-positional-arg call site must still work. With no
        issue terms supplied, the fn-token anchor still gates: a relevant key
        survives."""
        db_path, repo = _build_db(tmp_path, caller_keys=["normalize"])
        out = mine_return_shape(db_path, "target.py", "normalize_tz", repo)
        assert any("[FORMAT]" in line for line in out), f"got {out!r}"
