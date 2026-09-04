"""Greenfield / non-Python anchor-extraction fixes — red->green tests
(DeepSWE non-Python audit, run 27290157847).

Two confirmed extraction gaps, root-caused EMPIRICALLY on the run's own
artifacts (NOT the regex gap the trajectory audit hypothesized — every
"missed" token IS raw-extracted; they die later):

  GAP 1 — `Type::method` qualified pairs (Rust/C++ path syntax) never reach
     `_resolve_qualified_dotted`: `_IDENT_RE` only spans `.`-qualified forms,
     so boa's issue-named `Script::evaluate` decomposed into two bare tokens
     that died independently (`Script` survived only by luck; `Context` was
     dropped as a homonym by the generic-hub gate, 10 defs / 3 files). The
     2026-06-10 dotted-pair fix (`Class.method`) never engaged for `::`.
     Fix: harvest `A::b` pairs and confirm them through the SAME graph probes
     (parent-child / qualified_name / same-file containment); a confirmed pair
     is kept and exempt from the generic-hub + prose gates exactly like a
     confirmed dotted pair. Correct-or-quiet: unconfirmed pairs stay out.

  GAP 2 — GREENFIELD anchors are structurally deleted: the graph cross-check
     (correct for bug-fix issues) drops every issue-named symbol that does not
     exist YET. Measured on abs-module-cache-flags: `require`,
     `ABS_MODULE_PATH`, `require_cache_info`, `require_cache_keys`,
     `reset_require_cache` are ALL raw-extracted and ALL have 0 graph nodes
     (they are the feature to BE BUILT / env vars), so the final anchor set
     collapsed to `["BeginRepl", "clear"]` and lexical rank latched onto
     "module"/"install" -> wrong brief #1. Fix: a new
     ``unresolved_code_symbols`` tier — reporter-marked code tokens (backtick/
     fence provenance) that did NOT resolve against the graph, minus language
     keywords/closed-class words. These are NEVER graph seeds (no node to
     seed); they are the honest GREP/BM25 tier for feature work (`require`
     literally appears in evaluator/functions.go as the builtin registration
     string — exactly the grep the agent used to self-localize at step 10).
     Consumer wiring (grep spine / brief note) is designed separately; this
     locks the extraction primitive deterministically.

Pure sqlite fixtures; no task IDs, no gold labels, no network.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from groundtruth.pretask.anchors import extract_issue_anchors


def _mk_graph(db_path: Path, nodes: list[tuple]) -> str:
    """nodes: (name, label, file_path, parent_name_or_None)."""
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT, "
        "name TEXT, qualified_name TEXT, file_path TEXT, start_line INTEGER, "
        "end_line INTEGER, signature TEXT, is_test BOOLEAN DEFAULT 0, "
        "language TEXT DEFAULT 'rust', parent_id INTEGER)"
    )
    ids: dict[str, int] = {}
    for name, label, fp, parent in nodes:
        pid = ids.get(parent) if parent else None
        conn.execute(
            "INSERT INTO nodes (label, name, file_path, start_line, end_line, "
            "parent_id) VALUES (?,?,?,?,?,?)",
            (label, name, fp, 1, 2, pid),
        )
        ids[name] = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    conn.close()
    return str(db_path)


@pytest.fixture
def rust_graph(tmp_path: Path) -> str:
    """boa shape: Script class with an `evaluate` method (parent-child), plus
    enough same-named noise that bare `evaluate` is ambiguous."""
    return _mk_graph(
        tmp_path / "graph.db",
        [
            ("Script", "Class", "core/engine/src/script.rs", None),
            ("evaluate", "Method", "core/engine/src/script.rs", "Script"),
            ("Module", "Class", "core/engine/src/module/mod.rs", None),
            # homonym noise: evaluate defined elsewhere too
            ("evaluate", "Function", "core/engine/src/other.rs", None),
            ("job_loop", "Function", "core/engine/src/job.rs", None),
        ],
    )


@pytest.fixture
def go_graph(tmp_path: Path) -> str:
    """abs shape: the graph has BeginRepl but NONE of the feature-to-be-built
    symbols (`require`, cache helpers) nor the env var."""
    return _mk_graph(
        tmp_path / "graph2.db",
        [
            ("BeginRepl", "Function", "repl/repl.go", None),
            ("requireFn", "Function", "evaluator/functions.go", None),
            ("Install", "Function", "install/install.go", None),
        ],
    )


# ===========================================================================
# GAP 1 — `Type::method` qualified pairs
# ===========================================================================
def test_colon_qualified_pair_confirmed_against_graph(rust_graph):
    issue = "Add cancellation support to `Script::evaluate` so long-running scripts can be stopped."
    anchors = extract_issue_anchors(issue, rust_graph)
    assert "Script.evaluate" in anchors.symbols, (
        f"::-qualified pair not confirmed: {sorted(anchors.symbols)}"
    )


def test_colon_qualified_pair_unconfirmed_stays_out(rust_graph):
    """Greenfield `Context::new_evaluation_handle` — neither side parented in
    the graph -> the PAIR stays out (correct-or-quiet), no minted symbol."""
    issue = "Add `Context::new_evaluation_handle` returning a handle."
    anchors = extract_issue_anchors(issue, rust_graph)
    assert "Context.new_evaluation_handle" not in anchors.symbols


def test_colon_pair_in_prose_without_db_is_not_minted():
    anchors = extract_issue_anchors("Call `Script::evaluate` here.", None)
    # no DB -> no confirmation possible -> pair never minted from shape alone
    assert "Script.evaluate" not in anchors.symbols


# ===========================================================================
# GAP 2 — unresolved_code_symbols (the greenfield tier)
# ===========================================================================
_GO_ISSUE = (
    "Improve ABS module loading so `require()` remains deterministic, "
    "supports discovery through `ABS_MODULE_PATH`, and reports cache state.\n"
    "- Expose cache stats via `require_cache_info()` with numeric fields.\n"
    "- Expose `reset_require_cache()` to clear module cache.\n"
    "- Preserve the public REPL entrypoint signature: "
    "`BeginRepl(args []string, version string)`.\n"
)


def test_greenfield_code_tokens_survive_as_unresolved_tier(go_graph):
    anchors = extract_issue_anchors(_GO_ISSUE, go_graph)
    # the feature-to-be-built symbols + env var are reporter-marked code with
    # 0 graph nodes -> they land in the unresolved tier, not in symbols
    for tok in ("require", "ABS_MODULE_PATH", "require_cache_info", "reset_require_cache"):
        assert tok in anchors.unresolved_code_symbols, (
            f"greenfield anchor {tok!r} lost: {sorted(anchors.unresolved_code_symbols)}"
        )
        assert tok not in anchors.symbols  # never minted as a graph symbol


def test_resolved_symbols_not_duplicated_into_unresolved_tier(go_graph):
    anchors = extract_issue_anchors(_GO_ISSUE, go_graph)
    assert "BeginRepl" in anchors.symbols
    assert "BeginRepl" not in anchors.unresolved_code_symbols


def test_language_keywords_excluded_from_unresolved_tier(go_graph):
    anchors = extract_issue_anchors(_GO_ISSUE, go_graph)
    # `string`/`args`/`version` come from the backticked BeginRepl signature —
    # type/parameter keywords are not localization anchors
    assert "string" not in anchors.unresolved_code_symbols
    assert "args" not in anchors.unresolved_code_symbols


def test_prose_only_words_never_enter_unresolved_tier(go_graph):
    """Words that were never backtick/fence-marked are NOT code provenance."""
    anchors = extract_issue_anchors(
        "The loader should normalize and deduplicate equivalent directories "
        "while preserving order.",
        go_graph,
    )
    assert not anchors.unresolved_code_symbols, (
        f"prose leaked into the code tier: {sorted(anchors.unresolved_code_symbols)}"
    )


def test_no_db_path_keeps_unresolved_tier_empty():
    """Without a graph we cannot KNOW a token is unresolved — abstain."""
    anchors = extract_issue_anchors(_GO_ISSUE, None)
    assert anchors.unresolved_code_symbols == set()
