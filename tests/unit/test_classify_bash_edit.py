"""Layer 2.4/L4b — bash-edit detection + L4a retirement.

Closes the coverage gap where agents editing via bash (sed -i, heredoc,
tee, redirection) got no L6 reindex / L3 post-edit. Also confirms L4a
auto-query is retired (L3b subsumes it).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "swebench"))
import oh_gt_full_wrapper as w  # noqa: E402


class CmdRunAction:
    """Minimal stand-in for an OH CmdRunAction (name matters: _action_class
    reads type(action).__name__)."""
    def __init__(self, command: str):
        self.command = command


def _cmd(command: str):
    return CmdRunAction(command)


# ---- _parse_bash_edit_command ----

def test_sed_inplace_detected():
    assert w._parse_bash_edit_command("sed -i 's/foo/bar/' src/app.py") == "src/app.py"


def test_tee_detected():
    assert w._parse_bash_edit_command("echo x | tee src/config.py") == "src/config.py"


def test_tee_append_detected():
    assert w._parse_bash_edit_command("echo x | tee -a src/config.py") == "src/config.py"


def test_redirect_truncate_detected():
    assert w._parse_bash_edit_command("echo 'code' > src/new.py") == "src/new.py"


def test_redirect_append_detected():
    assert w._parse_bash_edit_command("echo 'code' >> src/new.py") == "src/new.py"


def test_read_only_sed_not_detected():
    # sed -n is read-only — must NOT be flagged as an edit
    assert w._parse_bash_edit_command("sed -n '1,10p' src/app.py") == ""


def test_plain_cat_not_detected():
    assert w._parse_bash_edit_command("cat src/app.py") == ""


# ---- classify_tool_event ordering ----

def test_sed_inplace_classifies_as_post_edit():
    ev = w.classify_tool_event(_cmd("sed -i 's/a/b/' src/app.py"))
    assert ev.kind == "post_edit"
    assert ev.path.endswith("src/app.py")


def test_read_sed_classifies_as_post_view():
    ev = w.classify_tool_event(_cmd("sed -n '1,5p' src/app.py"))
    assert ev.kind == "post_view"


def test_redirect_to_nonsource_skipped():
    # grep output to a .txt — not a source edit, source-ext gate filters it
    ev = w.classify_tool_event(_cmd("grep foo src/app.py > out.txt"))
    assert ev.kind == "skip"


def test_bash_edit_to_test_file_skipped():
    ev = w.classify_tool_event(_cmd("sed -i 's/a/b/' tests/test_app.py"))
    assert ev.kind == "skip"


# ---- L4a retirement ----

def test_l4a_auto_query_disabled():
    assert w._L4A_AUTO_QUERY_ENABLED is False
