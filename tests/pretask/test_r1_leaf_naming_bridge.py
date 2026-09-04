"""TTD (red->green) for the R1 leaf-naming CONTENT bridge.

SEAM: graph_localizer._semantic_score_by_file computes per-SYMBOL MaxSim cosines
for the FILE score, then DISCARDED them. The brief's symbol-naming stage
(v1r_brief._localization_header -> _defines_funcs) only named functions from
defines_anchor witnesses; on a behavior-described issue (gold leaf shares no token
with a named anchor) that set is EMPTY, and the tail fell back to nothing or, in
the contract path, the raw in-degree hub. R1 threads the per-symbol semantic signal
(+ per-symbol FTS5/BM25) into leaf naming via RRF with symbol-level hub demotion,
firing ONLY when defines_anchor is empty.

This test asserts the two load-bearing properties from the seam spec:
  1. With an FTS-relevant / semantically-relevant NON-HUB leaf, the bridge names
     that leaf ABOVE the in-degree hub.
  2. With NO content signal (no semantic map AND no FTS match), the bridge returns
     [] — byte-identical to the prior empty-tail behavior (correct-or-quiet no-op).

Pure sqlite + regex. No AI, no task symbols, no per-case weight tuning.
"""

from __future__ import annotations

import sqlite3

from groundtruth.pretask.graph_localizer import LocalizerResult, _normalize as _gl_norm
from groundtruth.pretask.v1r_brief import _semantic_leaf_names, _fts5_symbol_rank


_ISSUE = (
    "When the importer coerces a field, the value is stored verbatim instead of "
    "being normalized. The normalization step that should run on each field value "
    "is skipped during import."
)

_GOLD_FILE = "pkg/importer.py"


def _make_hub_vs_leaf_db(tmp_path, *, with_fts: bool):
    """One file with a HUB function (high CALLS fan-in across the repo) and a
    NON-HUB 'behavior' function the issue describes.

      pkg/importer.py :: run         <- 40 callers (the in-degree hub)
      pkg/importer.py :: normalize_field_value  <- 0 callers (the bug site)

    The issue describes 'normalize'/'field'/'value' (the leaf), NOT 'run'.
    """
    db = str(tmp_path / "graph.db")
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY, label TEXT, name TEXT, qualified_name TEXT,
            file_path TEXT, start_line INTEGER, end_line INTEGER, signature TEXT,
            return_type TEXT, is_exported INTEGER, is_test INTEGER, language TEXT,
            parent_id INTEGER
        );
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER, type TEXT,
            source_line INTEGER, source_file TEXT, resolution_method TEXT,
            confidence REAL, metadata TEXT
        );
        """
    )
    conn.executemany(
        "INSERT INTO nodes (id,label,name,file_path,start_line,end_line,signature,"
        "is_test,language) VALUES (?,?,?,?,?,?,?,0,'python')",
        [
            (1, "Method", "run", _GOLD_FILE, 1, 50, "def run(self):"),
            (
                2,
                "Method",
                "normalize_field_value",
                _GOLD_FILE,
                60,
                80,
                "def normalize_field_value(self, field, value):",
            ),
            # caller nodes (sources of edges into the hub) in other files
            *[
                (100 + i, "Function", f"caller_{i}", f"pkg/mod_{i}.py", 1, 2, f"def caller_{i}():")
                for i in range(40)
            ],
        ],
    )
    # 40 CALLS edges into `run` (id=1) -> `run` is the per-task in-degree HUB.
    conn.executemany(
        "INSERT INTO edges (id,source_id,target_id,type,resolution_method,confidence) "
        "VALUES (?,?,1,'CALLS','import',1.0)",
        [(500 + i, 100 + i) for i in range(40)],
    )
    if with_fts:
        # The Go indexer normally builds nodes_fts; emulate it so the lexical half
        # of the bridge can rank symbols by issue-token BM25.
        conn.execute(
            "CREATE VIRTUAL TABLE nodes_fts USING fts5("
            "name, qualified_name, signature, file_path, content='')"
        )
        conn.execute(
            "INSERT INTO nodes_fts(rowid, name, qualified_name, signature, file_path) "
            "SELECT id, name, COALESCE(qualified_name,''), COALESCE(signature,''), "
            "file_path FROM nodes"
        )
    conn.commit()
    conn.close()
    return db


def _loc_with_sem(symrank: dict[str, list[tuple[str, float]]]) -> LocalizerResult:
    return LocalizerResult(
        candidates=[],
        anchor_symbols=[],
        confidence=0.0,
        confident=False,
        gate_reason="test",
        symbol_semrank_by_file=symrank,
    )


# --------------------------------------------------------------------------
# PROPERTY 1: non-hub leaf the content signal favors OUTRANKS the in-degree hub
# --------------------------------------------------------------------------


def test_lexical_half_ranks_behavior_leaf_over_hub(tmp_path):
    """FTS5 half alone (no embedder): the issue tokens hit normalize_field_value,
    not `run`. The per-symbol FTS rank surfaces ONLY the matched leaf."""
    db = _make_hub_vs_leaf_db(tmp_path, with_fts=True)
    terms = {"normalize", "field", "value", "importer", "coerce"}
    names = _fts5_symbol_rank(db, _GOLD_FILE, terms)
    assert "normalize_field_value" in names, names
    # the hub `run` shares no issue token -> not matched by the lexical half
    assert names[0] == "normalize_field_value", names


def test_bridge_names_nonhub_leaf_above_hub(tmp_path):
    """End-to-end on _semantic_leaf_names: a semantic map favoring the behavior
    leaf, fused with the FTS lexical hit, names the NON-HUB leaf — never `run`,
    the 40-caller hub (symbol-level hub demotion)."""
    db = _make_hub_vs_leaf_db(tmp_path, with_fts=True)
    # semantic half: the issue<->leaf cosine is high, the issue<->hub cosine is low.
    loc = _loc_with_sem(
        {
            _gl_norm(_GOLD_FILE): [("normalize_field_value", 0.71), ("run", 0.12)],
        }
    )
    names = _semantic_leaf_names(loc, db, _GOLD_FILE, _ISSUE, limit=3)
    assert names, "bridge returned nothing despite both signals present"
    assert names[0] == "normalize_field_value", names
    # hub never named first; if present at all it is demoted to the tail.
    assert "run" not in names[:1], names


def test_semantic_half_alone_orders_leaves(tmp_path):
    """Embedder ON, FTS table ABSENT: the semantic per-symbol map alone drives the
    order (the issue->code bridge for token-mismatched golds)."""
    db = _make_hub_vs_leaf_db(tmp_path, with_fts=False)
    loc = _loc_with_sem(
        {
            _gl_norm(_GOLD_FILE): [("normalize_field_value", 0.66), ("run", 0.20)],
        }
    )
    names = _semantic_leaf_names(loc, db, _GOLD_FILE, _ISSUE, limit=3)
    assert names and names[0] == "normalize_field_value", names


# --------------------------------------------------------------------------
# PROPERTY 2: NO content signal -> [] (byte-identical empty tail, no-op)
# --------------------------------------------------------------------------


def test_no_signal_is_byte_identical_empty(tmp_path):
    """Embedder OFF (empty semantic map) AND no FTS match: the bridge contributes
    NOTHING — the caller keeps its prior empty tail. This is the correct-or-quiet
    no-op that makes R1 dormant until the embedder/FTS signal lands."""
    db = _make_hub_vs_leaf_db(tmp_path, with_fts=False)
    loc = _loc_with_sem({})  # no per-symbol semantic scores at all
    # issue terms that match NO symbol name/signature in this file
    names = _semantic_leaf_names(loc, db, _GOLD_FILE, "zzqqxx wibblefrob plonk", limit=3)
    assert names == [], names


def test_no_fts_table_lexical_half_silent(tmp_path):
    """nodes_fts absent -> lexical half returns [] (never crashes, never invents)."""
    db = _make_hub_vs_leaf_db(tmp_path, with_fts=False)
    assert _fts5_symbol_rank(db, _GOLD_FILE, {"normalize", "field"}) == []
