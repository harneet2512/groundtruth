"""TDD red-before-green for C1 (truncated guard/source-text values) + C2
(symbol-homonym contract) in the L3/L3b hooks and their root truncators.

C1 DEFECT: every render site that byte-slices an arbitrary SOURCE-TEXT VALUE
(a guard condition, a ``raise``/``except`` statement, a caller/peer/pattern code
line, a structural twin, a test-assertion expression, a parser expected/actual
value) with a fixed budget (``x[:N]``) can land mid-string-literal or
mid-expression. The agent then receives an UNTERMINATED literal
(``except ImportAbortErro``) or a line ending on a dangling binary operator
(``... documents and not``) — malformed content that violates correct-or-quiet.
Every such slice must route through ``sanitizer.clip_balanced`` so a truncation
can never reach the agent unbalanced.

C2 DEFECT: when a [GT]/[CONTRACT] block is built by resolving a bare function
NAME to a file (the beets ``set_fields() in zero.py`` bug, where the agent is
actually editing ``importer.py::set_fields``), the homonym in a different file is
shown. Correct-or-quiet: require uniqueness — if the name resolves to >1 file or
to a file other than the viewed/edited file, stay silent.

The balance ORACLE below is INDEPENDENT of the implementation (it reasons about
quotes/brackets/trailing-operator only), so it validates the real rendered
output, not the implementation under test.
"""
from __future__ import annotations

import os
import re
import sqlite3
import tempfile

import pytest

from groundtruth.runtime.sanitizer import is_well_formed_clause


# ---------------------------------------------------------------------------
# Independent balance oracle (NOT the implementation under test).
# ---------------------------------------------------------------------------
def _balanced(s: str) -> bool:
    in_str = ""
    esc = False
    depth = 0
    for ch in s:
        if esc:
            esc = False
            continue
        if in_str:
            if ch == "\\":
                esc = True
            elif ch == in_str:
                in_str = ""
            continue
        if ch in "\"'":
            in_str = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth < 0:
                return False
    if in_str or depth != 0:
        return False
    if re.search(r"(\b(and|or|not|in|is)\b|[-+*/%<>=&|^~]|->)\s*$", s.strip()):
        return False
    return True


# Malformed source-text values that the fixed-budget slices used to leak. Each
# is unbalanced (open quote / open bracket / dangling operator) so a naive
# ``x[:N]`` passes them through verbatim and the agent sees garbage.
_UNTERMINATED_RAISE = 'raise TypeError("DocumentSplitter expects a List of Document'
# An ``except`` whose handler body was cut mid-call (open paren) — the post_view
# 744 ``[CATCHES] {val}`` site. NOTE: a truncated *bare identifier* (e.g.
# ``except ImportAbortErro``) is, by quote/bracket rules, well-formed — that is
# OUT of clip_balanced's defect class and must stay untouched; the defect class
# is open-quote / open-bracket / dangling-operator only.
_UNTERMINATED_EXCEPT = "except ValueError: handler.recover(ctx, retry=(a, b"
_DANGLING_COND = "documents and not isinstance(documents[0], Document) and"
_OPEN_PAREN = "self._validate(value, strict=True, ctx=(a, b"
_OPEN_STR_ASSERT = 'assert response.json() == {"status": "ok'

_MALFORMED = [
    _UNTERMINATED_RAISE,
    _UNTERMINATED_EXCEPT,
    _DANGLING_COND,
    _OPEN_PAREN,
    _OPEN_STR_ASSERT,
]

# Negative-control values: already balanced AND under any reasonable budget, so
# clip_balanced must pass them through UNCHANGED (no over-suppression).
_BALANCED = [
    "if x is None:",
    'raise ValueError("bad input")',
    "return self.compute(a, b)",
    "assert foo(1) == 2",
    "ImportError",
]


def test_oracle_detects_the_bug_class():
    """Negative control on the oracle itself: it flags malformed, accepts good."""
    for bad in _MALFORMED:
        assert not _balanced(bad), f"oracle should reject malformed: {bad!r}"
    for good in _BALANCED:
        assert _balanced(good), f"oracle should accept balanced: {good!r}"


# ---------------------------------------------------------------------------
# semantic_check.extract_guards — the ROOT (cond[:60] -> clip_balanced).
# ---------------------------------------------------------------------------
def test_semantic_check_guards_are_balanced():
    from groundtruth.hooks.semantic_check import extract_guards

    # A guard whose condition contains an unbalanced paren within the regex
    # capture window. Old code: cond = m.group(1).strip()[:60] -> unbalanced.
    src = (
        "def f(x):\n"
        '    if isinstance(x, (list, tuple) and x and not done("a:\n'
        "        return None\n"
    )
    guards = extract_guards(src)
    for g in guards:
        assert _balanced(g), f"GUARD condition not balanced: {g!r}"


def test_semantic_check_guards_negative_control():
    """A balanced, short guard condition is preserved verbatim (no trimming)."""
    from groundtruth.hooks.semantic_check import extract_guards

    src = "def f(x):\n    if x is None:\n        return 0\n"
    guards = extract_guards(src)
    assert "x is None" in guards, f"balanced guard was altered: {guards!r}"


# ---------------------------------------------------------------------------
# trajectory/parsers.py render_compact — expected/actual/exc_msg/text slices.
# ---------------------------------------------------------------------------
def test_render_compact_lines_balanced():
    from groundtruth.trajectory.parsers import FailureRecord

    rec = FailureRecord(
        failing_unit="tests/test_x.py::test_split",
        assertion_or_error="assert result == expected",
        expected='{"status": "ok", "items": [1, 2, 3',  # truncated mid-dict/string
        actual="(documents and not isinstance(documents[0], Document) and",
    )
    out = rec.render_compact(max_chars=300)
    for ln in out.splitlines():
        # Strip the static label prefix; the VALUE portion must be balanced.
        val = ln.split(":", 1)[1].strip() if ":" in ln else ln.strip()
        assert _balanced(val), f"render_compact value not balanced: {ln!r}"


def test_render_compact_exception_message_balanced():
    from groundtruth.trajectory.parsers import FailureRecord

    rec = FailureRecord(
        failing_unit="run",
        exception_type="ValueError",
        exception_message='cannot parse "DocumentSplitter expects a List of Doc',
    )
    out = rec.render_compact(max_chars=300)
    for ln in out.splitlines():
        val = ln.split(":", 1)[1].strip() if ":" in ln else ln.strip()
        assert _balanced(val), f"exc message not balanced: {ln!r}"


def test_render_compact_negative_control():
    """Balanced, short expected/actual pass through unchanged."""
    from groundtruth.trajectory.parsers import FailureRecord

    rec = FailureRecord(
        failing_unit="tests/test_x.py::test_ok",
        expected='{"ok": true}',
        actual='{"ok": false}',
    )
    out = rec.render_compact(max_chars=300)
    assert '{"ok": true}' in out
    assert '{"ok": false}' in out


# ---------------------------------------------------------------------------
# post_edit caller/usage render — _extract_usage_contract code[:147].
# ---------------------------------------------------------------------------
def test_post_edit_usage_contract_balanced():
    from groundtruth.hooks.post_edit import _extract_usage_contract

    long_open = "x = obj.process(" + "arg, " * 40 + '"tail string literal'
    callers = [{"code": long_open, "file": "a/b.py", "line": "10"}]
    out = _extract_usage_contract(callers)
    # Extract the backtick-quoted code span and assert it is balanced.
    for m in re.finditer(r"`([^`]*)`", out):
        assert _balanced(m.group(1)), f"caller usage not balanced: {m.group(1)!r}"


def test_post_edit_usage_contract_negative_control():
    from groundtruth.hooks.post_edit import _extract_usage_contract

    code = "result = compute(a, b)"
    out = _extract_usage_contract([{"code": code, "file": "a/b.py", "line": "3"}])
    assert code in out, f"short balanced caller code altered: {out!r}"


def test_post_edit_format_caller_line_balanced():
    from groundtruth.hooks.post_edit import _format_caller_line

    long_open = "y = handler.dispatch(" + "k=v, " * 40 + '"open literal'
    line = _format_caller_line({"code": long_open, "file": "a/b.py", "line": 7})
    for m in re.finditer(r"`([^`]*)`", line):
        assert _balanced(m.group(1)), f"caller line not balanced: {m.group(1)!r}"


# ---------------------------------------------------------------------------
# C1 end-to-end: property-value chokepoint in generate_improved_evidence.
# A malformed guard_clause / exception_flow stored by an older indexer must be
# rebalanced before it is rendered into [BEHAVIORAL CONTRACT].
# ---------------------------------------------------------------------------
def _build_repo_and_db(tmp: str, *, prop_kind: str, prop_value: str) -> tuple[str, str]:
    repo = os.path.join(tmp, "repo")
    os.makedirs(os.path.join(repo, "pkg"), exist_ok=True)
    rel = "pkg/mod.py"
    # A function body > 20 chars so the properties path (not the short-body
    # path) is taken.
    body = (
        "def target(value):\n"
        "    if value is None:\n"
        "        raise ValueError('no value provided here at all')\n"
        "    out = value + 1\n"
        "    return out\n"
    )
    with open(os.path.join(repo, rel), "w", encoding="utf-8") as f:
        f.write(body)

    db = os.path.join(tmp, "graph.db")
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL, name TEXT NOT NULL, qualified_name TEXT,
            file_path TEXT NOT NULL, start_line INTEGER, end_line INTEGER,
            signature TEXT, return_type TEXT, is_exported BOOLEAN DEFAULT 0,
            is_test BOOLEAN DEFAULT 0, language TEXT, parent_id INTEGER
        );
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER, target_id INTEGER, type TEXT,
            source_line INTEGER, source_file TEXT, resolution_method TEXT,
            confidence REAL DEFAULT 0.0, metadata TEXT
        );
        CREATE TABLE properties (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            node_id INTEGER, kind TEXT, value TEXT, line INTEGER
        );
        """
    )
    conn.execute(
        "INSERT INTO nodes (id,label,name,qualified_name,file_path,start_line,"
        "end_line,signature,return_type,is_exported,is_test,language) "
        "VALUES (1,'Function','target','pkg.mod.target',?,1,5,"
        "'target(value)','int',1,0,'python')",
        (rel,),
    )
    conn.execute(
        "INSERT INTO properties (node_id,kind,value,line) VALUES (1,?,?,3)",
        (prop_kind, prop_value),
    )
    conn.commit()
    conn.close()
    return repo, db


@pytest.mark.parametrize(
    "kind,value",
    [
        ("guard_clause", "isinstance(x, (list, tuple) and x and not done(a:"),
        ("exception_flow", 'WHEN bad: raise TypeError("expects a List of Doc'),
        ("exception_handler", "except ValueError: recover(ctx, retry=(a, b"),
        ("conditional_return", "return self.compute(a, b"),
    ],
)
def test_behavioral_contract_property_values_balanced(kind, value):
    from groundtruth.hooks.post_edit import generate_improved_evidence

    with tempfile.TemporaryDirectory() as tmp:
        repo, db = _build_repo_and_db(tmp, prop_kind=kind, prop_value=value)
        out = generate_improved_evidence(
            "pkg/mod.py", ["target"], db, repo, mode="post_edit"
        )
        # Every CONTRACT detail line that carries the property value must be
        # balanced. The XML wrapper (<gt-evidence ...>) and pure tag lines
        # (``[BEHAVIORAL CONTRACT]``) are structural, not source-text values, so
        # they are skipped — we assert balance on the indented detail lines that
        # carry the stored property value.
        detail_lines = [
            ln for ln in out.splitlines()
            if ln.startswith("  ") and ln.strip() and "<" not in ln
        ]
        # Defect must be observable: the property value reached a detail line.
        assert detail_lines, f"[{kind}] no contract detail rendered:\n{out}"
        for ln in detail_lines:
            stripped = ln.strip()
            # Drop the static tag prefix ([RAISES], L3:, PRESERVE:, [CATCHES]) so
            # only the source-text VALUE portion is checked.
            val = stripped
            for sep in ("] ", ": "):
                if sep in val:
                    val = val.split(sep, 1)[1]
                    break
            assert _balanced(val), (
                f"[{kind}] rendered contract value not balanced: {stripped!r}\n"
                f"full output:\n{out}"
            )


def test_behavioral_contract_property_value_negative_control():
    """A balanced-under-budget property value is rendered verbatim."""
    from groundtruth.hooks.post_edit import generate_improved_evidence

    with tempfile.TemporaryDirectory() as tmp:
        repo, db = _build_repo_and_db(
            tmp, prop_kind="guard_clause", prop_value="value is None"
        )
        out = generate_improved_evidence(
            "pkg/mod.py", ["target"], db, repo, mode="post_edit"
        )
        assert "value is None" in out, (
            f"balanced guard_clause value was altered:\n{out}"
        )


# ---------------------------------------------------------------------------
# C2: symbol-homonym guard in post_view ego-graph block.
# When a bare function NAME resolves to a file OTHER than the viewed file (a
# homonym in a different file), the [GT] ego block must stay silent
# (correct-or-quiet). This guards against ego_graph's LIMIT 1 silently picking
# the wrong file when the LIKE/most-called resolution lands on a homonym (the
# beets ``set_fields() in zero.py`` bug). Generalized: no hardcoded names —
# the guard is a uniqueness/identity check on center.file_path vs the viewed
# file, exercised here by simulating a homonym-resolving ego_graph.
# ---------------------------------------------------------------------------
def _ego_db_with_viewed_callers(tmp: str, viewed: str) -> str:
    """A graph where the viewed file's set_fields HAS in-file callers, so the
    ego block's ``len(callers) > 0`` gate would pass — only the file-identity
    guard can stop a homonym center from being rendered."""
    db = os.path.join(tmp, "graph.db")
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL, name TEXT NOT NULL, qualified_name TEXT,
            file_path TEXT NOT NULL, start_line INTEGER, end_line INTEGER,
            signature TEXT, return_type TEXT, is_exported BOOLEAN DEFAULT 0,
            is_test BOOLEAN DEFAULT 0, language TEXT, parent_id INTEGER
        );
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER, target_id INTEGER, type TEXT,
            source_line INTEGER, source_file TEXT, resolution_method TEXT,
            confidence REAL DEFAULT 1.0, metadata TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO nodes (id,label,name,file_path,start_line,end_line,is_test,language) "
        "VALUES (1,'Function','set_fields',?,10,20,0,'python')",
        (viewed,),
    )
    conn.commit()
    conn.close()
    return db


class _FakeEgoNode:
    def __init__(self, name: str, file_path: str, start_line: int = 10):
        self.name = name
        self.file_path = file_path
        self.start_line = start_line


class _FakeEgo:
    """Minimal stand-in for an EgoGraphResult whose center resolved to an
    arbitrary file, with non-empty callers and a render() that names the file."""

    def __init__(self, center_file: str):
        self.center = _FakeEgoNode("set_fields", center_file)
        self.callers = [_FakeEgoNode("run_command", "beets/ui/commands.py")]
        self.guards: list = []
        self.test_assertions: list = []

    def render(self, max_tokens: int = 200) -> str:
        import os as _os
        return (
            f"set_fields() in {_os.path.basename(self.center.file_path)}:"
            f"{self.center.start_line}\n  run_command() commands.py:1"
        )


def test_c2_homonym_ego_block_stays_silent(monkeypatch):
    """ego_graph resolves the bare name to a DIFFERENT file (zero.py) than the
    viewed file (importer.py). The ego block must suppress (correct-or-quiet)."""
    from groundtruth.hooks import post_view
    import groundtruth.graph.ego as ego_mod

    viewed = "beets/importer.py"
    with tempfile.TemporaryDirectory() as tmp:
        db = _ego_db_with_viewed_callers(tmp, viewed)
        monkeypatch.setenv("GT_REPO_ROOT", tmp)
        monkeypatch.setattr(post_view, "_load_issue_terms", lambda *a, **k: {"set_fields"})
        # Homonym: center lands in beetsplug/zero.py, NOT the viewed file.
        monkeypatch.setattr(
            ego_mod, "ego_graph",
            lambda *a, **k: _FakeEgo("beetsplug/zero.py"),
        )
        out, _ = post_view.graph_navigation(viewed, db, iteration_ratio=0.0)
        joined = "\n".join(out)
        assert "zero.py" not in joined, (
            f"homonym file leaked into viewed-file evidence:\n{joined}"
        )


def test_c2_negative_control_same_file_ego_renders(monkeypatch):
    """ego_graph resolves the name to the SAME viewed file: the block is allowed
    to render (no over-suppression — the guard is a no-op on correct content)."""
    from groundtruth.hooks import post_view
    import groundtruth.graph.ego as ego_mod

    viewed = "beets/importer.py"
    with tempfile.TemporaryDirectory() as tmp:
        db = _ego_db_with_viewed_callers(tmp, viewed)
        monkeypatch.setenv("GT_REPO_ROOT", tmp)
        monkeypatch.setattr(post_view, "_load_issue_terms", lambda *a, **k: {"set_fields"})
        # Center is the viewed file itself -> guard must NOT suppress.
        monkeypatch.setattr(
            ego_mod, "ego_graph",
            lambda *a, **k: _FakeEgo(viewed),
        )
        out, _ = post_view.graph_navigation(viewed, db, iteration_ratio=0.0)
        joined = "\n".join(out)
        # The correct, same-file ego text is delivered (no over-suppression).
        assert "set_fields() in importer.py" in joined, (
            f"valid same-file ego block was over-suppressed:\n{joined}"
        )
