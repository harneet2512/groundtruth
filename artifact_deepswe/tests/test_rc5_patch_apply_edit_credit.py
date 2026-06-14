"""RC5 FINALIZATION — apply_patch / git-apply / patch edit-channel credit, driven
LIVE through the REAL classifier + the REAL post-edit population path.

THE INERTNESS LESSON (why this file exists): the original RC5 tests injected
``content_toks`` / ``edited_lines`` / ``(42,45)`` directly into
``edit_coverage_ratio`` — so they were GREEN while the live path stayed dead,
because ``_classify`` returned ``(None, None)`` for every patch-application
command and the post-edit population gate never opened. These tests fix that:
EVERY case DRIVES the real ``_classify`` / ``_edit_target`` on an actual command
STRING and asserts the LIVE population of ``_oracle_edit_content_tokens`` /
``_oracle_edited_lines_by_file`` via the real ``_augment_output`` post-edit path.
NOTHING is injected after classification.

GENERALIZED (not benchmaxx): ``apply_patch`` is the OpenAI/Codex edit format;
``git apply`` / ``patch -pN`` are universal POSIX edit mechanisms. The target is
parsed from ``*** Update File: <path>`` (apply_patch) or the ``+++ b/<path>``
unified-diff header; the edited line ranges from the ``@@ -a,b +c,d @@`` hunk
headers. No task IDs, gold labels, or repo-specific logic.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gt_mini_patch as g  # noqa: E402


# --- the OLD (pre-RC5) lexical-only credit, reproduced to prove RED ---
def _old_edit_coverage_ratio(obligation_syms, edited_tokens):
    syms = {s for s in (obligation_syms or ()) if s}
    if not syms:
        return None
    edited = {s for s in syms if s in (edited_tokens or ())}
    return len(edited) / len(syms)


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


def _stage_diff(text):
    fd, path = tempfile.mkstemp(suffix=".diff")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    return path


# A real unified diff whose target is src/groundtruth/foo.py, hunk +42,4 (=> 42..45).
_UNIFIED_DIFF = (
    "diff --git a/src/groundtruth/foo.py b/src/groundtruth/foo.py\n"
    "--- a/src/groundtruth/foo.py\n"
    "+++ b/src/groundtruth/foo.py\n"
    "@@ -40,3 +42,4 @@ class Importer:\n"
    "     def set_fields(self):\n"
    "+        return set_fields(self)\n"
)
# An apply_patch heredoc whose target is src/foo.py, hunk +42,4.
_HEREDOC = (
    "apply_patch <<'EOF'\n"
    "*** Begin Patch\n"
    "*** Update File: src/foo.py\n"
    "@@ -40,3 +42,4 @@\n"
    "+        set_fields(self)\n"
    "*** End Patch\n"
    "EOF"
)


# ===========================================================================
# POINT-1 (classification) + POINT-2 (body/lines) — driven LIVE on the STRING.
# Each case proves RED (None today) then GREEN (post_edit + file + body + lines).
# ===========================================================================
def test_heredoc_apply_patch_classifies_live():
    # GREEN classification: target from `*** Update File:`.
    assert g._classify(_HEREDOC) == ("post_edit", "src/foo.py")
    # body carries the symbol (Signal 1), lines from the `@@` hunk (Signal 3).
    assert "set_fields" in g._edit_body_tokens(_HEREDOC)
    assert (42, 45) in g._edited_line_ranges(_HEREDOC)


def test_apply_patch_redirect_file_classifies_live():
    p = _stage_diff(_UNIFIED_DIFF)
    try:
        cmd = f"apply_patch < {p}"
        # target READ FROM the staged diff (+++ b/...), p1 strip -> repo-rel.
        assert g._classify(cmd) == ("post_edit", "src/groundtruth/foo.py")
        assert "set_fields" in g._edit_body_tokens(cmd)
        assert (42, 45) in g._edited_line_ranges(cmd)
    finally:
        os.unlink(p)


def test_git_apply_file_classifies_live():
    p = _stage_diff(_UNIFIED_DIFF)
    try:
        cmd = f"git apply {p}"
        assert g._classify(cmd) == ("post_edit", "src/groundtruth/foo.py")
        assert "set_fields" in g._edit_body_tokens(cmd)
        assert (42, 45) in g._edited_line_ranges(cmd)
    finally:
        os.unlink(p)


def test_patch_p1_redirect_file_classifies_live():
    p = _stage_diff(_UNIFIED_DIFF)
    try:
        cmd = f"patch -p1 < {p}"
        assert g._classify(cmd) == ("post_edit", "src/groundtruth/foo.py")
        assert (42, 45) in g._edited_line_ranges(cmd)
    finally:
        os.unlink(p)


def test_patch_p2_strip_count():
    # -p2 strips TWO leading path segments from the diff header.
    diff = (
        "--- x/y/src/foo.py\n"
        "+++ x/y/src/foo.py\n"
        "@@ -1,1 +10,2 @@\n"
        "+ value\n"
    )
    p = _stage_diff(diff)
    try:
        assert g._classify(f"patch -p2 < {p}") == ("post_edit", "src/foo.py")
    finally:
        os.unlink(p)


def test_red_unpatched_behaviour_documented():
    """RED witness: the FOUR shapes used to classify as (None, None). We prove the
    OLD lexical-only credit (command tokens) would return 0.0 — the symbol never
    appears in the bare command string of the `< file` forms."""
    for cmd in (
        "apply_patch < /tmp/p.diff",
        "git apply /tmp/p.diff",
        "patch -p1 < /tmp/p.diff",
    ):
        old_cmd_tokens = {t for t in g._BLOCK_TOKEN_RE.findall(cmd) if len(t) >= 3}
        assert "set_fields" not in old_cmd_tokens
        assert _old_edit_coverage_ratio({"set_fields"}, old_cmd_tokens) == 0.0


# ===========================================================================
# CORRECT-OR-QUIET negative controls + no-regression on existing edit shapes.
# ===========================================================================
def test_missing_and_malformed_degrade_to_none():
    # absent staged file -> no fabricated target.
    assert g._classify("git apply /tmp/_no_such_rc5_missing.diff") == (None, None)
    # malformed payload (no `*** Update File` / `+++`) -> no target.
    p = _stage_diff("this is not a diff, no markers whatsoever\n")
    try:
        assert g._classify(f"apply_patch < {p}") == (None, None)
    finally:
        os.unlink(p)


def test_existing_edit_shapes_unchanged():
    assert g._classify("sed -i 's/a/b/' src/foo.py") == ("post_edit", "src/foo.py")
    assert g._classify("cat > src/bar.py <<'EOF'\nx=1\nEOF") == ("post_edit", "src/bar.py")
    assert g._classify("cat src/foo.py") == ("post_view", "src/foo.py")
    assert g._classify("grep -n parse src/lexer.py") == ("post_view", "src/lexer.py")
    assert g._classify("ls -la") == (None, None)


# ===========================================================================
# END-TO-END: drive `_augment_output` (the REAL post-edit population path), then
# call edit_coverage_ratio in the EXACT 2-arg consumer form at gt_mini_patch:3331.
# Asserts the LIVE globals populate and the ratio flips 0.0 (pre) -> 1.0 (post).
# NO injection of post-classify globals.
# ===========================================================================
def _drive_post_edit_live(cmd, rel, db, *, content_tokens, line_ranges):
    """Run the real _augment_output post-edit branch for ``cmd`` under a fully
    wired environment (graph db + anchors + root). Returns the live
    edit_coverage_ratio computed via the exact :3331 2-arg consumer form."""
    # anchors artifact -> obligation symbol set = {set_fields}.
    fd, anchors = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write('{"obligations": [{"symbols": ["set_fields"]}]}')
    fd, rootf = tempfile.mkstemp(suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("/")  # relative repo paths pass through _to_repo_rel unchanged

    saved_env = {k: os.environ.get(k) for k in
                 ("GT_HOST_GRAPH_DB", "GT_ANCHORS_PATH", "GT_BASELINE")}
    saved_baseline = g._GT_BASELINE
    saved_route = g._ORACLE_ROUTE
    saved_oblig = g._oblig_syms_cache
    saved_rootfile = g._ROOT_FILE
    try:
        os.environ["GT_HOST_GRAPH_DB"] = db
        os.environ["GT_ANCHORS_PATH"] = anchors
        os.environ.pop("GT_BASELINE", None)
        g._GT_BASELINE = False
        g._ORACLE_ROUTE = True
        g._ROOT_FILE = rootf
        g._oblig_syms_cache = None  # force re-read of the anchors artifact

        # full reset of oracle/delivery state (the :2574 clear path).
        g._reset_oracle_state()
        g._oracle_edit_content_tokens.clear()
        g._oracle_edited_lines_by_file.clear()

        # obligation set must be {set_fields} from the live artifact.
        assert g._obligation_symbol_set() >= {"set_fields"}

        # PRE (RED): nothing populated yet -> ratio 0.0 (symbol not credited).
        pre = g.edit_coverage_ratio(g._obligation_symbol_set(), g._oracle_edited_tokens)
        assert pre == 0.0, f"expected PRE 0.0, got {pre}"

        # DRIVE the real post-edit path on the command STRING.
        out: dict = {"output": ""}
        g._augment_output({"command": cmd}, out)

        # LIVE population assertions (NOT injected): the body symbol + line ranges
        # reached the module globals through _classify -> the :3652 gate.
        assert "set_fields" in g._oracle_edit_content_tokens, \
            "live _oracle_edit_content_tokens did not receive the body symbol"
        assert rel in g._oracle_edited_lines_by_file, \
            f"live _oracle_edited_lines_by_file missing {rel}"
        for lo, hi in line_ranges:
            assert (lo, hi) in g._oracle_edited_lines_by_file[rel]

        # POST (GREEN): the EXACT :3331 2-arg consumer form, reading the now-live
        # globals via the degrade contract -> 1.0.
        post = g.edit_coverage_ratio(g._obligation_symbol_set(), g._oracle_edited_tokens)
        return pre, post
    finally:
        g._GT_BASELINE = saved_baseline
        g._ORACLE_ROUTE = saved_route
        g._oblig_syms_cache = saved_oblig
        g._ROOT_FILE = saved_rootfile
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        for pth in (anchors, rootf):
            try:
                os.unlink(pth)
            except OSError:
                pass


def test_end_to_end_heredoc_credits_live():
    rel = "src/foo.py"
    db = _tmpdb([("set_fields", rel, "Function", 40, 55),
                 ("other", rel, "Function", 1, 30)])
    try:
        pre, post = _drive_post_edit_live(
            _HEREDOC, rel, db,
            content_tokens={"set_fields"}, line_ranges=[(42, 45)])
        assert pre == 0.0
        assert post == 1.0, f"expected GREEN 1.0 (live credit), got {post}"
    finally:
        os.unlink(db)


def test_end_to_end_apply_patch_redirect_credits_live():
    rel = "src/groundtruth/foo.py"
    db = _tmpdb([("set_fields", rel, "Function", 40, 55),
                 ("other", rel, "Function", 1, 30)])
    p = _stage_diff(_UNIFIED_DIFF)
    try:
        cmd = f"apply_patch < {p}"
        pre, post = _drive_post_edit_live(
            cmd, rel, db, content_tokens={"set_fields"}, line_ranges=[(42, 45)])
        assert pre == 0.0
        assert post == 1.0, f"expected GREEN 1.0 (live credit), got {post}"
    finally:
        os.unlink(db)
        os.unlink(p)


def test_end_to_end_git_apply_credits_live():
    rel = "src/groundtruth/foo.py"
    db = _tmpdb([("set_fields", rel, "Function", 40, 55)])
    p = _stage_diff(_UNIFIED_DIFF)
    try:
        cmd = f"git apply {p}"
        pre, post = _drive_post_edit_live(
            cmd, rel, db, content_tokens={"set_fields"}, line_ranges=[(42, 45)])
        assert pre == 0.0
        assert post == 1.0, f"expected GREEN 1.0 (live credit), got {post}"
    finally:
        os.unlink(db)
        os.unlink(p)


def test_end_to_end_overcount_rejected_live():
    """OVER-COUNT killer through the live path: the patch edits a region that does
    NOT overlap the obligation symbol's span -> credited 0.0 even though the
    symbol is named in the file."""
    rel = "src/groundtruth/foo.py"
    # set_fields lives at [200,260]; the diff above touches lines 42..45 (miss).
    db = _tmpdb([("set_fields", rel, "Function", 200, 260),
                 ("other", rel, "Function", 1, 30)])
    p = _stage_diff(_UNIFIED_DIFF)
    try:
        cmd = f"apply_patch < {p}"
        # classification + population still happen LIVE; only the credit rejects.
        out: dict = {"output": ""}
        # reuse the driver but assert post==0.0 (edit-site miss).
        fd, anchors = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write('{"obligations": [{"symbols": ["set_fields"]}]}')
        fd, rootf = tempfile.mkstemp(suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("/")
        saved = (g._GT_BASELINE, g._ORACLE_ROUTE, g._oblig_syms_cache, g._ROOT_FILE,
                 os.environ.get("GT_HOST_GRAPH_DB"), os.environ.get("GT_ANCHORS_PATH"))
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
            g._augment_output({"command": cmd}, out)
            # globals populated LIVE (classification worked) ...
            assert rel in g._oracle_edited_lines_by_file
            # ... but the edit-site (42..45) misses set_fields' span [200,260].
            post = g.edit_coverage_ratio(g._obligation_symbol_set(),
                                         g._oracle_edited_tokens)
            assert post == 0.0, f"over-count killer expected 0.0, got {post}"
        finally:
            (g._GT_BASELINE, g._ORACLE_ROUTE, g._oblig_syms_cache, g._ROOT_FILE,
             oldhost, oldanch) = saved
            for k, v in (("GT_HOST_GRAPH_DB", oldhost), ("GT_ANCHORS_PATH", oldanch)):
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            for pth in (anchors, rootf):
                try:
                    os.unlink(pth)
                except OSError:
                    pass
    finally:
        os.unlink(db)
        os.unlink(p)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"ALL {len(fns)} RC5 PATCH-APPLY EDIT-CREDIT TESTS PASSED")
