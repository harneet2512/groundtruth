"""Build a tiny multi-language graph.db for layer tests.

Schema mirrors gt-index (Go indexer) so any code that calls
``groundtruth.index.graph_store.is_graph_db`` accepts it.

Layout (5 files x 3-5 functions each):
  src/server.py                (3 functions, .py)
  src/url_utils.py             (3 functions, .py)
  src/validators.py            (2 functions, .py)
  pkg/parser.go                (3 functions, .go)
  crates/util/src/normalize.rs (2 functions, .rs)

Edges encode confidence variation:
  - import      -> confidence 1.0 (verified)
  - same_file   -> confidence 1.0
  - name_match  -> confidence 0.4 / 0.9 (ambiguous fallback)

This fixture is shared by L1 (gt_index) and L3 (gt_hook) suites.
L3 in particular does not actually consume the DB (its `analyze`
subcommand builds an in-process AST/regex index off the source tree)
but the file is materialised so any caller that asserts on its
existence keeps working.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

FIXTURE_DIR = Path(__file__).resolve().parent
DB_PATH = FIXTURE_DIR / "synthetic_graph.db"


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT NOT NULL,
    name TEXT NOT NULL,
    qualified_name TEXT,
    file_path TEXT NOT NULL,
    start_line INTEGER,
    end_line INTEGER,
    signature TEXT,
    return_type TEXT,
    is_exported INTEGER DEFAULT 0,
    is_test INTEGER DEFAULT 0,
    language TEXT NOT NULL,
    parent_id INTEGER REFERENCES nodes(id)
);

CREATE TABLE IF NOT EXISTS edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES nodes(id),
    target_id INTEGER NOT NULL REFERENCES nodes(id),
    type TEXT NOT NULL,
    source_line INTEGER,
    source_file TEXT,
    resolution_method TEXT,
    confidence REAL DEFAULT 0.0,
    metadata TEXT
);

CREATE INDEX IF NOT EXISTS idx_nodes_name ON nodes(name);
CREATE INDEX IF NOT EXISTS idx_nodes_file ON nodes(file_path);
CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_id);
CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_id);
"""


# (label, name, qualified_name, file_path, start_line, end_line, language)
NODES: list[tuple[str, str, str, str, int, int, str]] = [
    # repo_python — server.py
    ("Function", "handle_request", "src.server.handle_request",
     "src/server.py", 8, 18, "python"),
    ("Function", "serve_forever", "src.server.serve_forever",
     "src/server.py", 21, 23, "python"),
    ("Function", "_main", "src.server._main",
     "src/server.py", 26, 30, "python"),
    # repo_python — url_utils.py
    ("Function", "parse_url", "src.url_utils.parse_url",
     "src/url_utils.py", 11, 25, "python"),
    ("Function", "normalize_url", "src.url_utils.normalize_url",
     "src/url_utils.py", 28, 33, "python"),
    ("Function", "is_https", "src.url_utils.is_https",
     "src/url_utils.py", 36, 39, "python"),
    # repo_python — validators.py
    ("Function", "validate_request_url", "src.validators.validate_request_url",
     "src/validators.py", 10, 15, "python"),
    ("Function", "validate_callback", "src.validators.validate_callback",
     "src/validators.py", 18, 21, "python"),
    # repo_go — parser.go
    ("Function", "ParseURL", "pkg.parser.ParseURL",
     "pkg/parser.go", 12, 24, "go"),
    ("Function", "Normalize", "pkg.parser.Normalize",
     "pkg/parser.go", 27, 33, "go"),
    ("Function", "IsHTTPS", "pkg.parser.IsHTTPS",
     "pkg/parser.go", 36, 41, "go"),
    # repo_rust — normalize.rs
    ("Function", "normalize", "crates.util.normalize.normalize",
     "crates/util/src/normalize.rs", 4, 12, "rust"),
    ("Function", "to_https", "crates.util.normalize.to_https",
     "crates/util/src/normalize.rs", 15, 21, "rust"),
]


def _id(name: str, file_path: str, ids: dict[tuple[str, str], int]) -> int:
    return ids[(name, file_path)]


# (source_name/file, target_name/file, type, line, resolution_method, confidence)
EDGES: list[tuple[tuple[str, str], tuple[str, str], str, int, str, float]] = [
    # high-confidence: server.handle_request -> url_utils.parse_url (import)
    (("handle_request", "src/server.py"),
     ("parse_url", "src/url_utils.py"),
     "CALLS", 10, "import", 1.0),
    # high-confidence: server.handle_request -> url_utils.normalize_url (import)
    (("handle_request", "src/server.py"),
     ("normalize_url", "src/url_utils.py"),
     "CALLS", 11, "import", 1.0),
    # high-confidence: server.handle_request -> validators.validate_request_url (import)
    (("handle_request", "src/server.py"),
     ("validate_request_url", "src/validators.py"),
     "CALLS", 12, "import", 1.0),
    # high-confidence: validators.validate_request_url -> url_utils.parse_url (import)
    (("validate_request_url", "src/validators.py"),
     ("parse_url", "src/url_utils.py"),
     "CALLS", 12, "import", 1.0),
    # high-confidence: validators.validate_request_url -> url_utils.is_https (import)
    (("validate_request_url", "src/validators.py"),
     ("is_https", "src/url_utils.py"),
     "CALLS", 13, "import", 1.0),
    # high-confidence: validators.validate_callback -> url_utils.parse_url (import)
    (("validate_callback", "src/validators.py"),
     ("parse_url", "src/url_utils.py"),
     "CALLS", 20, "import", 1.0),
    # same-file: url_utils.normalize_url -> url_utils.parse_url
    (("normalize_url", "src/url_utils.py"),
     ("parse_url", "src/url_utils.py"),
     "CALLS", 30, "same_file", 1.0),
    # same-file: url_utils.is_https -> url_utils.parse_url
    (("is_https", "src/url_utils.py"),
     ("parse_url", "src/url_utils.py"),
     "CALLS", 38, "same_file", 1.0),
    # name_match (ambiguous, low confidence): pkg.parser.ParseURL -> parse_url (cross-language collision)
    (("ParseURL", "pkg/parser.go"),
     ("parse_url", "src/url_utils.py"),
     "CALLS", 13, "name_match", 0.4),
    # name_match (single candidate, higher conf): pkg.parser.Normalize -> normalize
    (("Normalize", "pkg/parser.go"),
     ("normalize", "crates/util/src/normalize.rs"),
     "CALLS", 28, "name_match", 0.9),
    # same-file Go: ParseURL -> Normalize
    (("ParseURL", "pkg/parser.go"),
     ("Normalize", "pkg/parser.go"),
     "CALLS", 14, "same_file", 1.0),
    # same-file Rust: to_https -> normalize
    (("to_https", "crates/util/src/normalize.rs"),
     ("normalize", "crates/util/src/normalize.rs"),
     "CALLS", 16, "same_file", 1.0),
    # name_match low conf: serve_forever -> ParseURL
    (("serve_forever", "src/server.py"),
     ("ParseURL", "pkg/parser.go"),
     "CALLS", 22, "name_match", 0.4),
]


def build(db_path: Path = DB_PATH) -> Path:
    """Build (or rebuild) the synthetic graph DB. Returns the path."""
    if db_path.exists():
        db_path.unlink()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(SCHEMA_SQL)

        ids: dict[tuple[str, str], int] = {}
        for label, name, qname, fp, sl, el, lang in NODES:
            cur = conn.execute(
                "INSERT INTO nodes "
                "(label, name, qualified_name, file_path, start_line, end_line, "
                "signature, return_type, is_exported, is_test, language, parent_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (label, name, qname, fp, sl, el,
                 f"{name}(...)", None, 1, 0, lang, None),
            )
            ids[(name, fp)] = cur.lastrowid

        for src, tgt, etype, line, method, conf in EDGES:
            sid = ids[src]
            tid = ids[tgt]
            conn.execute(
                "INSERT INTO edges "
                "(source_id, target_id, type, source_line, source_file, "
                "resolution_method, confidence, metadata) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (sid, tid, etype, line, src[1], method, conf, None),
            )

        conn.commit()
    finally:
        conn.close()
    return db_path


if __name__ == "__main__":
    p = build()
    print(f"built {p}")
