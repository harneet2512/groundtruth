"""D-S (the "0-consumption" root): l3b evidence shipped as bare `path:line:sym`
grep rows on a native pipe, which RL-trained models ignore (pull-tool-shaped
content). The FORM is now the compiler-`note:` grammar those models have seen from
every compiler/linter, keeping `path:line` leading (grep-compatible, entity-present)
and inheriting the EXACT identity firewall of render_def_rows_native.

Also pins the D-O production interaction: the DELIVERED form is what
_note_l3b_delivered_callers parses, so the note-form (leading path:line) must
populate the delivered-caller registry — the chain that was silently broken in
production before (the old parser matched only the non-native "() in file" prose).
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
for p in (_REPO / "src", _REPO / "artifact_deepswe"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from groundtruth.runtime.native_render import (  # noqa: E402
    render_note_rows_native, contains_test_identity,
)
import gt_mini_patch as g  # noqa: E402


def test_note_form_is_compiler_diagnostic_not_bare_grep():
    out = render_note_rows_native([("src/api/routes.py", "88", "get_session")])
    assert out.startswith("src/api/routes.py:88:"), "path:line must stay leading"
    assert ": note: " in out, "must be compiler-note grammar, not a bare grep row"
    assert "get_session" in out


def test_note_form_inherits_leak_firewall():
    out = render_note_rows_native(
        [("tests/test_x.py", "5", "foo"), ("src/x.py", "9", "foo")])
    assert "tests/" not in out and "src/x.py" in out, "test-path row must be dropped"
    assert contains_test_identity(out) is False, "no test identity may survive the firewall"


def test_empty_rows_stay_quiet():
    assert render_note_rows_native([]) == ""
    assert render_note_rows_native([("only/two.py", "5")]) == ""   # malformed tuple -> skipped


def test_do_parser_extracts_callers_from_delivered_note_form():
    # the D-O production interaction: a DELIVERED pure-caller note-form payload must
    # populate the registry (the bare/old-prose parser missed the production form).
    payload = render_note_rows_native(
        [("fsm/scene.py", "88", "update"), ("fsm/router.py", "12", "handle")])
    g._l3b_pure_caller_hashes.clear()
    g._l3b_delivered_caller_rels.clear()
    g._l3b_pure_caller_hashes.add(g._l3b_content_key(payload))
    g._note_l3b_delivered_callers("l3b.evidence", payload)
    assert g._norm_rel("fsm/scene.py") in g._l3b_delivered_caller_rels
    assert g._norm_rel("fsm/router.py") in g._l3b_delivered_caller_rels
