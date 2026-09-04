"""FIX #1 (2026-06-10) — wire the GREENFIELD anchor tier into the grep seed.

The ``unresolved_code_symbols`` tier (anchors.py, committed green) extracts
reporter-marked code tokens with ZERO graph nodes — the API to BE BUILT in a
feature/greenfield issue (go ``require``, env vars, rust ``::``-pair tails).
The gap (gt_gt §4 grep-fallback): those tokens never reached the existing
grep-to-seed recall spine — ``_grep_to_seeds`` picks its tokens from issue
TERMS sorted purely by length, so a short greenfield token (``require``, 7
chars) is crowded out of the top-10 by longer prose words, and the token's
defining file (where the literal string lives) never enters the candidate
set. A 0-node symbol has NO name-match / FTS5 node to seed — the literal
grep is its ONLY entry.

The surgical fix: ``_grep_to_seeds`` gains ``priority_tokens`` (no-op when
empty); ``localize()`` passes ``issue_anchors.unresolved_code_symbols``.
Priority tokens go FIRST in the grep token list (capped at 5, longest first)
so prose length can never crowd them out.

Synthetic fixtures, three languages (go / rust / ts). No task IDs, no gold
labels, no benchmark names, no network.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from groundtruth.pretask.graph_localizer import _grep_to_seeds, localize

_SCHEMA = """
    CREATE TABLE nodes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        label TEXT NOT NULL,
        name TEXT NOT NULL,
        qualified_name TEXT,
        file_path TEXT NOT NULL,
        start_line INTEGER,
        end_line INTEGER,
        signature TEXT,
        return_type TEXT,
        is_exported BOOLEAN DEFAULT 0,
        is_test BOOLEAN DEFAULT 0,
        language TEXT NOT NULL,
        parent_id INTEGER REFERENCES nodes(id)
    );
    CREATE TABLE edges (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id INTEGER NOT NULL,
        target_id INTEGER NOT NULL,
        type TEXT NOT NULL,
        source_line INTEGER,
        source_file TEXT,
        resolution_method TEXT,
        confidence REAL DEFAULT 0.0,
        metadata TEXT
    );
"""

# Ten distinct prose words, every one LONGER than the greenfield tokens under
# test (7-8 chars) — pre-fix these fill the grep top-10 and crowd the
# greenfield token out (the measured abs-module-cache-flags shape).
_LONG_PROSE = (
    "loading should stay deterministic and discoverable; registration, "
    "configuration and environment handling need initialization, "
    "documentation, compatibility and infrastructure treated consistently."
)


def _mk_repo_and_graph(
    tmp_path: Path,
    *,
    lang: str,
    defining_rel: str,
    defining_src: str,
    defining_node: str,
    resolved_rel: str,
    resolved_src: str,
    resolved_node: str,
    decoy_rel: str,
    decoy_src: str,
    decoy_node: str,
) -> tuple[str, str]:
    """A 3-file repo + matching graph.db. The greenfield symbol exists ONLY as
    a literal in ``defining_rel``'s source — never as a graph node."""
    for rel, src in (
        (defining_rel, defining_src),
        (resolved_rel, resolved_src),
        (decoy_rel, decoy_src),
    ):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src, encoding="utf-8")
    db = tmp_path / "graph.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(_SCHEMA)
    conn.executemany(
        "INSERT INTO nodes (label, name, file_path, start_line, end_line, "
        "is_test, language) VALUES ('Function',?,?,1,5,0,?)",
        [
            (defining_node, defining_rel, lang),
            (resolved_node, resolved_rel, lang),
            (decoy_node, decoy_rel, lang),
        ],
    )
    conn.commit()
    conn.close()
    return str(tmp_path), str(db)


@pytest.fixture
def go_fixture(tmp_path: Path) -> tuple[str, str]:
    """abs shape: `require` is a builtin-registration STRING in the evaluator
    file (the exact grep the agent used to self-localize) — 0 graph nodes."""
    return _mk_repo_and_graph(
        tmp_path,
        lang="go",
        defining_rel="evaluator/builtins.go",
        defining_src=(
            'package evaluator\n\nfunc registerBuiltins() {\n\tbuiltins["require"] = requireFn\n}\n'
        ),
        defining_node="registerBuiltins",
        resolved_rel="repl/repl.go",
        resolved_src="package repl\n\nfunc BeginRepl() {\n}\n",
        resolved_node="BeginRepl",
        decoy_rel="setup/setup.go",
        decoy_src=(
            "package setup\n// registration configuration environment "
            "initialization documentation\nfunc Setup() {\n}\n"
        ),
        decoy_node="Setup",
    )


# ===========================================================================
# Unit: _grep_to_seeds priority plumbing (red pre-fix at this exact site)
# ===========================================================================
def test_priority_token_survives_length_crowding(go_fixture) -> None:
    """RED (pre-fix): `require` is shorter than the 10 prose words -> cut from
    the grep token list -> the defining file never seeds. GREEN: passed as a
    priority token it goes first and the file seeds."""
    repo, db = go_fixture
    long_words = {
        "deterministic",
        "discoverable",
        "registration",
        "configuration",
        "environment",
        "initialization",
        "documentation",
        "compatibility",
        "consistently",
        "infrastructure",
    }
    conn = sqlite3.connect(db)
    try:
        seeds = _grep_to_seeds(
            long_words | {"require"},
            repo,
            conn,
            priority_tokens={"require"},
        )
    finally:
        conn.close()
    assert any(fp == "evaluator/builtins.go" for _, _, fp in seeds), (
        f"greenfield token did not seed its defining file: {seeds}"
    )


def test_no_priority_tokens_is_a_noop(go_fixture) -> None:
    """No-regression property: priority_tokens empty -> byte-identical token
    selection -> the crowded-out shape stays crowded out (documents the
    pre-fix red, and that the fix changes NOTHING for bug-fix issues)."""
    repo, db = go_fixture
    long_words = {
        "deterministic",
        "discoverable",
        "registration",
        "configuration",
        "environment",
        "initialization",
        "documentation",
        "compatibility",
        "consistently",
        "infrastructure",
    }
    conn = sqlite3.connect(db)
    try:
        seeds = _grep_to_seeds(long_words | {"require"}, repo, conn)
    finally:
        conn.close()
    assert not any(fp == "evaluator/builtins.go" for _, _, fp in seeds)


# ===========================================================================
# localize() wiring: anchors tier -> grep priority -> candidate (3 languages)
# ===========================================================================
_GO_ISSUE = (
    "Module cache control for `require()`\n\n"
    "Add cache management so `require()` " + _LONG_PROSE + "\n"
    "The public REPL entrypoint `BeginRepl` must keep its signature.\n"
)


def test_go_greenfield_token_reaches_candidates(go_fixture) -> None:
    """RED (pre-fix): `require` (0 nodes, crowded out of the grep top-10) ->
    evaluator/builtins.go absent from candidates. GREEN: priority-seeded."""
    repo, db = go_fixture
    res = localize(_GO_ISSUE, db, top_k=8, repo_root=repo)
    paths = [c.file_path for c in res.candidates]
    assert "evaluator/builtins.go" in paths, (
        f"greenfield defining file missing from candidates: {paths}"
    )


def test_rust_colon_pair_tail_reaches_candidates(tmp_path: Path) -> None:
    """`Context::reanchor` — `Context` is a language-keyword token, the tail
    `reanchor` is the greenfield anchor (0 nodes). Its defining file (the
    literal appears in a trait impl) must become a candidate. The file name
    shares NO token with the issue, so neither path-to-seed nor FTS5 can
    shortcut — only the greenfield grep wiring reaches it."""
    repo, db = _mk_repo_and_graph(
        tmp_path,
        lang="rust",
        defining_rel="core/src/lifecycle.rs",
        defining_src=(
            "pub struct Hooks;\n\nimpl Hooks {\n"
            "    pub fn on_swap(&self) { /* reanchor goes here */ }\n}\n"
        ),
        defining_node="on_swap",
        resolved_rel="core/src/script.rs",
        resolved_src="pub fn evaluate_script() {}\n",
        resolved_node="evaluate_script",
        decoy_rel="core/src/prep.rs",
        decoy_src=(
            "// registration configuration environment initialization\npub fn prep_engine() {}\n"
        ),
        decoy_node="prep_engine",
    )
    issue = (
        "Add `Context::reanchor` for moving execution\n\n"
        "Moving execution " + _LONG_PROSE + "\n"
        "`evaluate_script` keeps its current behavior.\n"
    )
    res = localize(issue, db, top_k=8, repo_root=repo)
    paths = [c.file_path for c in res.candidates]
    assert "core/src/lifecycle.rs" in paths, (
        f"rust ::-tail greenfield file missing from candidates: {paths}"
    )


def test_ts_dotted_method_part_reaches_candidates(tmp_path: Path) -> None:
    """js/ts `.`-method greenfield: `registry.flushAll()` — the dotted token
    is decomposed by the extractor; the unresolved part `flushAll` (0 nodes)
    must grep-seed the file containing the literal. The file name shares NO
    token with the issue (no path-to-seed/FTS5 shortcut)."""
    repo, db = _mk_repo_and_graph(
        tmp_path,
        lang="typescript",
        defining_rel="src/holder.ts",
        defining_src=(
            "export class Holder {\n"
            "  items: string[] = [];\n"
            "  // flushAll lands here\n"
            "  dropOne(k: string) { return k; }\n}\n"
        ),
        defining_node="dropOne",
        resolved_rel="src/render.ts",
        resolved_src="export function renderBoard() {}\n",
        resolved_node="renderBoard",
        decoy_rel="src/prep.ts",
        decoy_src=(
            "// registration configuration environment initialization\n"
            "export function prepBoard() {}\n"
        ),
        decoy_node="prepBoard",
    )
    issue = (
        "Add `registry.flushAll()` to clear every entry\n\n"
        "Clearing entries " + _LONG_PROSE + "\n"
        "`renderBoard` is unchanged.\n"
    )
    res = localize(issue, db, top_k=8, repo_root=repo)
    paths = [c.file_path for c in res.candidates]
    assert "src/holder.ts" in paths, (
        f"ts dotted-part greenfield file missing from candidates: {paths}"
    )


# ===========================================================================
# End-to-end on the LIVE surface (BRIEFING.md §0: measure generate_v1r_brief)
# ===========================================================================
class _FlatEmbedModel:
    """Deterministic embedder (same unit vector for every text) — the
    established integration-test pattern (test_rerank_localization_fixes).
    Isolates the recall assertion from the machine's persisted embed cache
    (stale vectors from a different-dimension model crash the matmul)."""

    def encode(self, texts, **kw):
        import numpy as np

        texts = list(texts)
        v = np.zeros((len(texts), 384), dtype="float32")
        v[:, 0] = 1.0
        return v


def test_generate_v1r_brief_renders_greenfield_defining_file(go_fixture, monkeypatch) -> None:
    """The defining file must survive through render: candidates -> .files."""
    from groundtruth.pretask import anchor_select as _as
    from groundtruth.pretask import v1r_brief as _v1r
    from groundtruth.pretask import v7_4_brief as _b

    _as._EMBED_CACHE.clear()
    try:
        _as._SYMVEC_CACHE.clear()
    except Exception:
        pass
    monkeypatch.setattr(_b, "_get_model", lambda: _FlatEmbedModel())
    monkeypatch.setattr(_b, "_SEMANTIC_AVAILABLE", True)

    repo, db = go_fixture
    result = _v1r.generate_v1r_brief(
        _GO_ISSUE,
        repo,
        db,
        bug_id="unit-greenfield",
        repo="unit",
    )
    norm = [str(getattr(f, "path", f)).replace("\\", "/").lstrip("./") for f in result.files]
    assert any(f.endswith("evaluator/builtins.go") for f in norm), (
        f"greenfield defining file absent from the rendered brief: {norm}"
    )
