"""DeepSWE/mini-swe per-turn <gt-evidence> must be CROSS-LANGUAGE (graph.db),
not Python-`ast`-only (gt_hook.py).

The DeepSWE live path is `pier` + `artifact_deepswe/gt_agent.py` (GTMiniSweAgent)
+ `artifact_deepswe/gt_mini_patch.py` (monkeypatches Environment.execute in the
task container). The per-view/per-edit `<gt-evidence>` body used to be built by
shelling out to `gt_hook.py understand/verify`.

gt_hook.py's POST-EDIT route (`verify`) is `ast.parse`-centric and HARD-gated to
`.py`: its diff-range parser only records ranges for files ending in `.py`
(gt_hook.py:4110 `current_file.endswith(".py")`, and ChangeAnalyzer uses
`_parse_safe = ast.parse`). On a Go/Rust/TS/JS diff it records NO changed
functions -> the CHANGE/CONTRACT evidence families produce NOTHING -> EMPTY
`<gt-evidence>` body. That is the residue this fix removes.

(NOTE — honest scope: gt_hook.py's POST-VIEW route, `understand`, has a separate
regex `_LANG_EXTS` multi-language fallback index (gt_hook.py:1741), so it is NOT
strictly empty on Go — but it is UNGATED: it renders an ego-graph whose caller
name is `?` with no resolution_method / confidence / categorical-fact gate, i.e.
the name_match laundering CLAUDE.md forbids. The fix replaces it with the
provenance-gated graph.db pillars. The clean empty->non-empty RED->GREEN proof is
on the post_edit route; the post_view improvement is QUALITY/provenance.)

The UNIFY fix builds the body from graph.db (tree-sitter, ALL languages) via the
same deterministic categorical-fact-gated pillars the host brief uses
(`_resolved_witnesses_for_file`, `_caller_contract_for_file`, `_sibling_context`,
`_edit_target_callee_contracts`) — ported INLINE into gt_mini_patch.py because the
groundtruth package is not importable in the task container.

RED  : the OLD post_edit path (gt_hook verify on a .go diff) -> empty body.
GREEN: the NEW path (graph.db pillars) -> non-empty contract/caller body on the
       SAME .go file. A .py file still works (no regression). The categorical
       fact gate holds (no name_match edge is laundered as a fact).

All deterministic: a graph.db fixture built directly with sqlite3 (no Go toolchain).
"""
from __future__ import annotations

import importlib.util
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_PATCH_PATH = _ROOT / "artifact_deepswe" / "gt_mini_patch.py"
_GT_HOOK = _ROOT / "benchmarks" / "swebench" / "gt_hook.py"


def _load_patch_module():
    """Import gt_mini_patch.py by file path (its module-bottom _install() is
    exception-guarded and no-ops when minisweagent is absent, so this is safe)."""
    spec = importlib.util.spec_from_file_location("gt_mini_patch_under_test", _PATCH_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# graph.db schema mirrors tests/invariants/conftest_l1.create_graph_db (the Go
# indexer's output schema): nodes + edges with resolution_method + confidence.
def _create_graph_db(db_path: Path, nodes: list[dict], edges: list[tuple]) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT NOT NULL, name TEXT NOT NULL,
            qualified_name TEXT, file_path TEXT NOT NULL, start_line INTEGER, end_line INTEGER,
            signature TEXT, return_type TEXT, is_exported BOOLEAN DEFAULT 0,
            is_test BOOLEAN DEFAULT 0, language TEXT NOT NULL DEFAULT 'go', parent_id INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER NOT NULL,
            target_id INTEGER NOT NULL, type TEXT NOT NULL, source_line INTEGER,
            source_file TEXT, resolution_method TEXT, confidence REAL DEFAULT 1.0, metadata TEXT
        )
        """
    )
    name_to_id: dict[str, int] = {}
    for n in nodes:
        conn.execute(
            "INSERT INTO nodes (label, name, file_path, signature, start_line, end_line, "
            "is_test, language) VALUES (?,?,?,?,?,?,?,?)",
            (n["label"], n["name"], n["file_path"], n.get("signature", ""),
             n.get("start_line", 1), n.get("end_line", 1), int(n.get("is_test", 0)),
             n.get("language", "go")),
        )
        name_to_id[n["name"]] = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    for src, tgt, etype, source_line, method, conf in edges:
        conn.execute(
            "INSERT INTO edges (source_id, target_id, type, source_line, resolution_method, "
            "confidence) VALUES (?,?,?,?,?,?)",
            (name_to_id[src], name_to_id[tgt], etype, source_line, method, conf),
        )
    conn.commit()
    conn.close()


@pytest.fixture
def go_repo(tmp_path: Path):
    """A tiny Go repo + graph.db where handler.go::Handle CALLS auth.go::Verify
    over an `import` (DETERMINISTIC) edge, and store.go::Save calls Handle (a
    cross-file caller). Plus a name_match edge that must NOT launder as a fact."""
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    handler = root / "pkg" / "handler.go"
    auth = root / "pkg" / "auth.go"
    store = root / "pkg" / "store.go"
    handler.write_text(
        "package pkg\n\n"
        "func Handle(req *Request) error {\n"
        "    return Verify(req.Token)\n"  # call site to auth.Verify (line 4)
        "}\n",
        encoding="utf-8",
    )
    auth.write_text(
        "package pkg\n\n"
        "func Verify(token string) error {\n"
        "    return nil\n"
        "}\n",
        encoding="utf-8",
    )
    store.write_text(
        "package pkg\n\n"
        "func Save(req *Request) error {\n"
        "    return Handle(req)\n"  # cross-file caller of Handle (line 4)
        "}\n",
        encoding="utf-8",
    )
    db = tmp_path / "graph.db"
    nodes = [
        {"label": "Function", "name": "Handle", "file_path": "pkg/handler.go",
         "signature": "func Handle(req *Request) error", "start_line": 3, "end_line": 5},
        {"label": "Function", "name": "Verify", "file_path": "pkg/auth.go",
         "signature": "func Verify(token string) error", "start_line": 3, "end_line": 5},
        {"label": "Function", "name": "Save", "file_path": "pkg/store.go",
         "signature": "func Save(req *Request) error", "start_line": 3, "end_line": 5},
        # A same-named project function colliding with stdlib-ish guess (name_match).
        {"label": "Function", "name": "Sibling", "file_path": "pkg/handler.go",
         "signature": "func Sibling() {}", "start_line": 7, "end_line": 8},
    ]
    edges = [
        # Handle (handler.go) CALLS Verify (auth.go) — DETERMINISTIC (import).
        ("Handle", "Verify", "CALLS", 4, "import", 1.0),
        # Save (store.go) CALLS Handle (handler.go) — DETERMINISTIC caller (import).
        ("Save", "Handle", "CALLS", 4, "import", 1.0),
        # A name_match CALLS edge into Handle — must NEVER render as a fact.
        ("Sibling", "Verify", "CALLS", 7, "name_match", 0.9),
    ]
    _create_graph_db(db, nodes, edges)
    return root, db, handler


@pytest.fixture
def py_repo(tmp_path: Path):
    """A tiny Python repo + graph.db (no-regression check)."""
    root = tmp_path / "pyrepo"
    (root / "app").mkdir(parents=True)
    users = root / "app" / "users.py"
    svc = root / "app" / "svc.py"
    users.write_text(
        "def get_user(uid):\n"
        "    return load(uid)\n",
        encoding="utf-8",
    )
    svc.write_text(
        "from app.users import get_user\n\n"
        "def handler(uid):\n"
        "    return get_user(uid)\n",  # cross-file caller of get_user (line 4)
        encoding="utf-8",
    )
    db = tmp_path / "pygraph.db"
    nodes = [
        {"label": "Function", "name": "get_user", "file_path": "app/users.py",
         "signature": "def get_user(uid)", "start_line": 1, "end_line": 2, "language": "python"},
        {"label": "Function", "name": "handler", "file_path": "app/svc.py",
         "signature": "def handler(uid)", "start_line": 3, "end_line": 4, "language": "python"},
    ]
    edges = [
        ("handler", "get_user", "CALLS", 4, "import", 1.0),
    ]
    _create_graph_db(db, nodes, edges)
    return root, db, users


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update({
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    })
    return subprocess.run(["git", *args], cwd=str(root), capture_output=True,
                          text=True, env=env, timeout=30)


def _run_old_verify(root: Path) -> str:
    """The OLD post_edit path: gt_hook.py verify --quiet. Returns stdout."""
    if not _GT_HOOK.is_file():
        pytest.skip("gt_hook.py not present")
    r = subprocess.run(
        [sys.executable, "-S", str(_GT_HOOK), "verify", f"--root={root}", "--quiet",
         "--max-items=3"],
        capture_output=True, text=True, timeout=60,
    )
    return (r.stdout or "").strip()


# ---------------------------------------------------------------------------
# RED: the OLD gt_hook.py post-edit (verify) path emits EMPTY evidence on a
# Go diff — _get_modified_files + the diff-range parser are both .py-gated
# (gt_hook.py:3797 / :4110), so a Go edit records zero changed functions.
# ---------------------------------------------------------------------------
def test_old_gt_hook_verify_empty_on_go_diff(go_repo):
    root, _db, handler = go_repo
    if _git(root, "rev-parse", "--git-dir").returncode != 0:
        _git(root, "init", "-q")
        _git(root, "add", "-A")
        _git(root, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "init")
    # Make a real Go edit -> an unstaged .go diff.
    handler.write_text(handler.read_text(encoding="utf-8").replace(
        "return Verify(req.Token)", "return Verify(req.Token) // edited"),
        encoding="utf-8")
    diff = _git(root, "diff", "--name-only").stdout
    assert "handler.go" in diff, f"fixture sanity: expected a Go diff, got: {diff!r}"
    out = _run_old_verify(root)
    assert out == "", (
        "EXPECTED the ast-only gt_hook.py verify to emit NOTHING on a .go diff "
        f"(.py-gated at gt_hook.py:3797/:4110). Got:\n{out!r}"
    )


# ---------------------------------------------------------------------------
# GREEN: the NEW graph.db path emits a NON-EMPTY contract/caller body on the
# SAME non-Python file.
# ---------------------------------------------------------------------------
def test_new_graph_path_nonempty_on_go(go_repo, monkeypatch):
    root, db, _handler = go_repo
    mod = _load_patch_module()
    monkeypatch.setenv("GT_GRAPH_DB", str(db))
    # post_view on handler.go
    body_view = mod._evidence_body("post_view", "pkg/handler.go", str(root))
    assert body_view, "NEW post_view path produced an EMPTY body on a .go file"
    # The caller contract surfaces the DETERMINISTIC cross-file caller Save (store.go).
    assert "store.go" in body_view, f"missing cross-file caller witness; got:\n{body_view}"
    # The witness surfaces the DETERMINISTIC callee Verify (auth.go).
    assert "auth.go" in body_view or "Verify" in body_view, (
        f"missing resolved callee witness; got:\n{body_view}"
    )

    # post_edit on handler.go -> the callee CONTRACT (signature of Verify, called by Handle).
    body_edit = mod._evidence_body("post_edit", "pkg/handler.go", str(root))
    assert body_edit, "NEW post_edit path produced an EMPTY body on a .go file"
    assert "Verify(token string)" in body_edit, (
        f"post_edit missing the callee signature contract; got:\n{body_edit}"
    )


# ---------------------------------------------------------------------------
# Full <gt-evidence> wrapper end-to-end through _evidence(cmd).
# ---------------------------------------------------------------------------
def test_evidence_wrapper_cross_language_go(go_repo, monkeypatch):
    root, db, _handler = go_repo
    mod = _load_patch_module()
    monkeypatch.setenv("GT_GRAPH_DB", str(db))
    monkeypatch.setenv("GT_ROOT_FILE", "/nonexistent")  # _root() falls back to "/"
    monkeypatch.setattr(mod, "_root", lambda: str(root))
    mod._seen.clear()
    # A view command on the .go file.
    ev = mod._evidence("cat pkg/handler.go")
    assert ev, "no <gt-evidence> emitted for a .go view"
    assert "<gt-evidence" in ev and "</gt-evidence>" in ev
    assert 'kind="post_view"' in ev


# ---------------------------------------------------------------------------
# Categorical FACT gate: a name_match edge is NEVER laundered as a fact.
# ---------------------------------------------------------------------------
def test_name_match_not_laundered_as_fact(go_repo, monkeypatch):
    root, db, _handler = go_repo
    mod = _load_patch_module()
    monkeypatch.setenv("GT_GRAPH_DB", str(db))
    # auth.go::Verify has TWO incoming CALLS: Handle (import=FACT) and Sibling
    # (name_match=GUESS). The fact (Handle/handler.go) must appear as a witness;
    # the name_match caller (Sibling) must NOT be rendered as a confident fact.
    body = mod._evidence_body("post_view", "pkg/auth.go", str(root))
    assert body, "expected witness body on auth.go (it has a verified caller)"
    # The verified caller Handle is a fact.
    assert "Handle" in body or "handler.go" in body, f"verified caller missing; got:\n{body}"
    # The name_match caller Sibling must not be laundered into a [WITNESS]/[CALLERS] FACT line.
    fact_lines = [ln for ln in body.splitlines()
                  if ln.startswith("[WITNESS]") or ln.startswith("[CALLERS]")]
    assert not any("Sibling" in ln for ln in fact_lines), (
        f"name_match caller 'Sibling' was laundered as a fact:\n{body}"
    )


# ---------------------------------------------------------------------------
# No regression: the NEW path still works on Python.
# ---------------------------------------------------------------------------
def test_new_graph_path_nonempty_on_python(py_repo, monkeypatch):
    root, db, _users = py_repo
    mod = _load_patch_module()
    monkeypatch.setenv("GT_GRAPH_DB", str(db))
    body = mod._evidence_body("post_view", "app/users.py", str(root))
    assert body, "NEW path produced an EMPTY body on a .py file (regression)"
    # get_user has a verified cross-file caller handler (svc.py).
    assert "svc.py" in body or "handler" in body, f"py caller witness missing; got:\n{body}"


# ---------------------------------------------------------------------------
# Correct-or-quiet: no graph.db -> empty body (never an exception).
# ---------------------------------------------------------------------------
def test_no_db_quiet(monkeypatch, tmp_path):
    mod = _load_patch_module()
    monkeypatch.setenv("GT_GRAPH_DB", str(tmp_path / "missing.db"))
    assert mod._evidence_body("post_view", "pkg/handler.go", str(tmp_path)) == ""
