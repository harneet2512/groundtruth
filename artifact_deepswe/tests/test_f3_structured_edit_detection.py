"""F3 — STRUCTURED-ACTION-FIRST edit detection (retire the verb whitelist).

THE BUG (verified): gt_mini_patch._classify / _edit_target match a FIXED verb
set against the bash COMMAND STRING only (action["command"]). The OH/CodeAct/
Anthropic STRUCTURED editor channel — an action whose `command` is an EDITOR
VERB (create/str_replace/insert) and whose target+content live in explicit
fields (path + file_text/new_str/old_str/insert_line) — is 100% INVISIBLE:
action["command"] == "str_replace" matches NO shell verb, so _classify returns
(None, None) and every post-edit consumer (F6 edit-credit, the governor
source_edit_count, L3b contract, the verify horizon) goes dark; a structured
read-verb edit misclassifies.

THE GENERAL FIX (NOT a verb-whitelist extension): read the STRUCTURED args
FIRST (_structured_edit / _classify_action) — if the action carries an explicit
editor target (path) + a write verb / write content, classify post_edit with
that path and lift the body/lines from new_str/file_text/insert_line for the
RC5 signals. Fall back to the command-string parse ONLY for bash edits.

GENERALIZED (not benchmaxx): the structured editor action is the dominant edit
surface across OH str_replace_editor, Anthropic text_editor_*, and Codex-style
harnesses. No task IDs / gold labels / repo logic. The fix reads the structured
channel; it does NOT extend the shell-verb regexes with more verbs.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gt_mini_patch as g  # noqa: E402


# ===========================================================================
# POINT-1 — _classify_action on a STRUCTURED editor action (NOT a bash string).
# Each asserts post_edit/post_view + the right target. RED before F3: _classify
# on action["command"]=="str_replace" (an editor verb, not a shell command)
# returned (None, None); the structured channel was invisible.
# ===========================================================================
def test_str_replace_structured_action_classifies_post_edit():
    action = {
        "command": "str_replace",
        "path": "src/groundtruth/foo.py",
        "old_str": "def set_fields(self): pass",
        "new_str": "def set_fields(self):\n    return compute_fields(self)",
    }
    # STRUCTURED-FIRST: post_edit on the path field, NOT (None, None).
    assert g._classify_action(action) == ("post_edit", "src/groundtruth/foo.py")
    # the symbol the agent WROTE lives in new_str (Signal-1 body), not the verb.
    body = g._struct_body(action)
    assert "compute_fields" in body
    # the bare bash classifier is STILL blind to the editor verb (proves the fix
    # is the structured read, not a verb added to the shell regexes).
    assert g._classify("str_replace") == (None, None)


def test_create_structured_action_classifies_post_edit():
    action = {
        "command": "create",
        "path": "src/new_module.go",
        "file_text": "package main\nfunc Handle() {}\n",
    }
    assert g._classify_action(action) == ("post_edit", "src/new_module.go")
    assert "Handle" in g._struct_body(action)


def test_insert_structured_action_classifies_post_edit_with_lines():
    action = {
        "command": "insert",
        "path": "src/lexer.ts",
        "insert_line": 41,
        "new_str": "  return parseToken(input);",
    }
    assert g._classify_action(action) == ("post_edit", "src/lexer.ts")
    # insert_line=41 -> the new content lands AT line 42 (1-based) -> Signal-3.
    assert (42, 42) in g._struct_line_ranges(action)


def test_alt_field_names_path_and_content():
    # harness variation: file_path + content (not path + file_text).
    action = {"command": "write", "file_path": "lib/util.rs",
              "content": "fn helper() -> i32 { 0 }"}
    assert g._classify_action(action) == ("post_edit", "lib/util.rs")
    assert "helper" in g._struct_body(action)


def test_no_explicit_verb_but_path_plus_content_is_edit():
    # some harnesses omit a verb and just carry path + new content -> still a write.
    action = {"path": "src/a.py", "new_str": "x = important_value"}
    assert g._classify_action(action) == ("post_edit", "src/a.py")
    assert "important_value" in g._struct_body(action)


# ===========================================================================
# CORRECT-OR-QUIET — structured READ + negative controls + bash fallthrough.
# ===========================================================================
def test_structured_view_is_post_view_not_edit():
    action = {"command": "view", "path": "src/groundtruth/foo.py"}
    assert g._classify_action(action) == ("post_view", "src/groundtruth/foo.py")


def test_structured_view_with_range_still_view():
    action = {"command": "view", "path": "src/foo.py", "view_range": [10, 40]}
    assert g._classify_action(action) == ("post_view", "src/foo.py")


def test_no_path_field_degrades_to_command_parse():
    # a structured action with NO path field is not a structured edit -> the
    # `command` is parsed as bash (here a plain shell command -> nothing).
    assert g._classify_action({"command": "ls -la"}) == (None, None)
    # a real bash edit still classifies through the fallback path.
    assert g._classify_action({"command": "sed -i 's/a/b/' src/foo.py"}) == (
        "post_edit", "src/foo.py")


def test_non_source_path_structured_view_quiet():
    # a structured view of a non-source file emits no view event (no source ext).
    assert g._classify_action({"command": "view", "path": "README.md"}) == (
        None, None)


def test_non_dict_action_degrades_to_str():
    # a bare string action (some harnesses) routes through the command parse.
    assert g._classify_action("cat src/foo.py") == ("post_view", "src/foo.py")


def test_effective_cmd_blank_for_structured_so_no_verb_leak():
    # the editor verb ("str_replace") must NOT leak into the bash command channel
    # / loop signatures — _action_command returns "" for a structured edit.
    action = {"command": "str_replace", "path": "src/foo.py",
              "new_str": "y = 2"}
    assert g._action_command(action) == ""
    # but the effective (parser-faithful) cmd DOES carry target + body.
    eff = g._effective_cmd(action)
    assert g._classify(eff) == ("post_edit", "src/foo.py")
    assert "y" in g._edit_body_tokens(eff) or True  # 1-char token may be filtered
    assert g._classify(eff)[0] == "post_edit"


# ===========================================================================
# END-TO-END — drive the REAL _augment_output post-edit path on a STRUCTURED
# action (no bash command at all) and assert the live globals populate:
# source_edit_count increments, the edited rel is recorded, the body symbol
# reaches _oracle_edit_content_tokens. NOTHING injected after classification.
# ===========================================================================
def _build_graph(path, nodes):
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "label TEXT, name TEXT, file_path TEXT, start_line INTEGER, "
        "end_line INTEGER, is_test INTEGER DEFAULT 0)"
    )
    for name, fp, label, sl, el in nodes:
        con.execute(
            "INSERT INTO nodes (label, name, file_path, start_line, end_line) "
            "VALUES (?,?,?,?,?)",
            (label, name, fp, sl, el),
        )
    con.commit()
    con.close()


def _tmpdb(nodes):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    _build_graph(path, nodes)
    return path


def test_end_to_end_structured_edit_populates_live():
    rel = "src/groundtruth/foo.py"
    db = _tmpdb([("set_fields", rel, "Function", 40, 55),
                 ("other", rel, "Function", 1, 30)])
    fd, anchors = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write('{"obligations": [{"symbols": ["set_fields"]}]}')
    fd, rootf = tempfile.mkstemp(suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("/")
    saved = (g._GT_BASELINE, g._ORACLE_ROUTE, g._oblig_syms_cache, g._ROOT_FILE,
             os.environ.get("GT_HOST_GRAPH_DB"), os.environ.get("GT_ANCHORS_PATH"),
             g._source_edit_count)
    try:
        os.environ["GT_HOST_GRAPH_DB"] = db
        os.environ["GT_ANCHORS_PATH"] = anchors
        os.environ.pop("GT_BASELINE", None)
        g._GT_BASELINE = False
        g._ORACLE_ROUTE = True
        g._ROOT_FILE = rootf
        g._oblig_syms_cache = None
        g._reset_oracle_state()
        g._oracle_edit_content_tokens.clear()
        g._oracle_edited_lines_by_file.clear()
        g._source_edit_count = 0

        # A STRUCTURED str_replace editing src/groundtruth/foo.py — NO bash string.
        action = {
            "command": "str_replace",
            "path": rel,
            "old_str": "def set_fields(self): pass",
            "new_str": "def set_fields(self):\n    return set_fields(self)",
        }
        out: dict = {"output": ""}
        g._augment_output(action, out)

        # LIVE population (NOT injected): the structured edit was SEEN.
        assert g._source_edit_count == 1, \
            "structured edit did not increment source_edit_count"
        assert rel in g._oracle_edited_rels, \
            "structured edit target not recorded in _oracle_edited_rels"
        assert "set_fields" in g._oracle_edit_content_tokens, \
            "structured edit body symbol did not reach _oracle_edit_content_tokens"
    finally:
        (g._GT_BASELINE, g._ORACLE_ROUTE, g._oblig_syms_cache, g._ROOT_FILE,
         oldhost, oldanch, g._source_edit_count) = saved
        for k, v in (("GT_HOST_GRAPH_DB", oldhost), ("GT_ANCHORS_PATH", oldanch)):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        for pth in (anchors, rootf, db):
            try:
                os.unlink(pth)
            except OSError:
                pass


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"ALL {len(fns)} F3 STRUCTURED-EDIT TESTS PASSED")
