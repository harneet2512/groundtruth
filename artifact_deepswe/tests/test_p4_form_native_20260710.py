"""P4 FORM — D-8 (gateway native), RL-2 (trace-frame native), RL-3 (Lane-B steer native).

FORM changes render the SAME evidence in the native environment voice (tag-free, no
``GT:`` marker) instead of the ``<gt-*>`` tagged block — the tags/voice are OOD for an
RL-trained model, the content is identical. Every native arm is a NEW behavioral flag,
so it is default-OFF byte-identical and ``--ae`` forwarded (governed by
test_r1_ae_parity_invariant_failclosed).

RL-3 (this file's focus): ``_steer_native`` renders a Lane-B steer tag-free. The D-5
dedup hash is the GATE text's hash, fixed at gate time, so a native render dedups
identically to a tagged render of the same winner.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
for _p in (str(_REPO / "artifact_deepswe"), str(_REPO / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import gt_mini_patch as g  # noqa: E402


# A verbatim Lane-B steer as the producers emit it (see _no_test_evidence @ :4899):
# leading "\n", a <gt-nudge reason="..."> open tag, one "GT: ..." imperative body
# line, a </gt-nudge> close tag.
_REAL_STEER = (
    '\n<gt-nudge reason="no_test_evidence">\nGT: your test commands have produced '
    "no visible test results (likely killed/timed out before any test ran). Run a "
    "narrower target or raise the timeout until you see real pass/fail output.\n"
    "</gt-nudge>"
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("GT_BASELINE", "GT_STEER_NATIVE", "GT_GATEWAY_NATIVE"):
        monkeypatch.delenv(k, raising=False)
    g._GT_BASELINE = False
    yield


# ---------------------------------------------------------------------------- #
# RL-3 gating: default-off byte-identical, flag-on, baseline-arm never native
# ---------------------------------------------------------------------------- #
def test_rl3_default_off_gate_false(monkeypatch):
    """No GT_STEER_NATIVE -> the native arm is OFF (delivered bytes untouched)."""
    monkeypatch.delenv("GT_STEER_NATIVE", raising=False)
    assert g._steer_native_on() is False


def test_rl3_flag_on_gate_true(monkeypatch):
    monkeypatch.setenv("GT_STEER_NATIVE", "1")
    assert g._steer_native_on() is True


def test_rl3_baseline_arm_never_native(monkeypatch):
    """The GT_BASELINE arm is byte-identical to no-GT — the native arm must be OFF
    there even if the flag is set (baseline is the frozen reference)."""
    monkeypatch.setenv("GT_STEER_NATIVE", "1")
    g._GT_BASELINE = True
    try:
        assert g._steer_native_on() is False
    finally:
        g._GT_BASELINE = False


@pytest.mark.parametrize("val", ["0", "false", "no", "off", ""])
def test_rl3_falsey_values_off(monkeypatch, val):
    monkeypatch.setenv("GT_STEER_NATIVE", val)
    assert g._steer_native_on() is False


# ---------------------------------------------------------------------------- #
# RL-3 transform: strips wrapper tags + "GT:" marker, keeps the body verbatim
# ---------------------------------------------------------------------------- #
def test_rl3_strips_wrapper_tags():
    out = g._steer_native(_REAL_STEER)
    assert "<gt-nudge" not in out
    assert "</gt-nudge>" not in out
    assert "<gt-" not in out


def test_rl3_strips_leading_gt_marker():
    out = g._steer_native(_REAL_STEER)
    assert "GT:" not in out


def test_rl3_body_preserved_verbatim():
    """Only tags + the 'GT:' marker are removed — every content word survives."""
    out = g._steer_native(_REAL_STEER)
    for phrase in (
        "your test commands have produced",
        "no visible test results",
        "raise the timeout until you see real pass/fail output.",
    ):
        assert phrase in out


def test_rl3_render_is_single_paragraph():
    out = g._steer_native(_REAL_STEER).strip()
    # tags gone, marker gone -> exactly the one body line remains.
    assert out.startswith("your test commands have produced")
    assert out.count("\n") == 0


# ---------------------------------------------------------------------------- #
# RL-3 safety: an ALREADY-native block (covering RED / edit_check syntax
# transcript — no <gt-* tag) passes through UNCHANGED (line structure intact)
# ---------------------------------------------------------------------------- #
def test_rl3_already_native_passthrough_unchanged():
    native = "$ pytest tests/test_x.py::test_y\n<3 lines>\nE   assert 1 == 2\n[exit 1]"
    assert g._steer_native(native) == native


def test_rl3_empty_input_untouched():
    assert g._steer_native("") == ""


def test_rl3_all_tag_block_collapses_to_empty():
    """A block that is ONLY tags renders to '' -> caller delivers nothing (never
    stamps/appends a no-op steer)."""
    assert g._steer_native('\n<gt-nudge reason="x">\n</gt-nudge>') == ""


# ---------------------------------------------------------------------------- #
# RL-3 flag governance: GT_STEER_NATIVE + GT_GATEWAY_NATIVE are --ae forwarded
# ---------------------------------------------------------------------------- #
# ---------------------------------------------------------------------------- #
# N-1: fold / non-target note splice is arm-aware — tagged inserts after the
# open tag; native appends a trailing tag-free comment (rows left pristine)
# ---------------------------------------------------------------------------- #
_TAGGED = '<gt-search-facts symbol="foo">\ndef: a/x.py:1\n</gt-search-facts>'
_NATIVE = "a/x.py:1:foo\ntest refs: 2"
_NOTE = '("foo" not found; indexed as "bar" - verify: grep -rn "bar" .)'


def test_n1_tagged_arm_inserts_after_open_tag(monkeypatch):
    monkeypatch.delenv("GT_POST_SEARCH_NATIVE", raising=False)
    out = g._splice_search_note(_TAGGED, _NOTE)
    lines = out.split("\n")
    assert lines[0] == '<gt-search-facts symbol="foo">'
    assert lines[1] == _NOTE  # note directly after the open tag (historical shape)
    assert lines[2] == "def: a/x.py:1"


def test_n1_native_arm_appends_trailing_comment_rows_pristine(monkeypatch):
    monkeypatch.setenv("GT_POST_SEARCH_NATIVE", "1")
    out = g._splice_search_note(_NATIVE, _NOTE)
    lines = out.split("\n")
    # the ripgrep rows are UNTOUCHED at the top (no GT-voice spliced mid-render)
    assert lines[0] == "a/x.py:1:foo"
    assert lines[1] == "test refs: 2"
    # the annotation trails as a tag-free `# ` comment, no wrapping parens
    assert lines[-1].startswith("# ")
    assert "<gt-" not in out
    assert lines[-1] == '# "foo" not found; indexed as "bar" - verify: grep -rn "bar" .'


def test_n1_native_note_never_splits_def_rows(monkeypatch):
    """The exact N-1 corruption: index-1 insertion between two def rows. Native arm
    must NOT do that."""
    monkeypatch.setenv("GT_POST_SEARCH_NATIVE", "1")
    two_rows = "a/x.py:1:foo\nb/y.py:9:foo"
    out = g._splice_search_note(two_rows, _NOTE)
    lines = out.split("\n")
    assert lines[0] == "a/x.py:1:foo"
    assert lines[1] == "b/y.py:9:foo"  # rows stay adjacent, note did NOT land between


def test_rl3_and_d8_flags_are_ae_forwarded():
    wf = (_REPO / ".github/workflows/deepswe_full.yml").read_text(encoding="utf-8")
    ae = (_REPO / "artifact_deepswe/gt_integration/gt_ae_block.sh").read_text(
        encoding="utf-8")
    for flag in ("GT_STEER_NATIVE", "GT_GATEWAY_NATIVE"):
        forwarded = (f"--ae {flag}=" in wf) or (f'--ae "{flag}=' in ae)
        assert forwarded, f"{flag} must be --ae forwarded (un-enableable in prod else)"


# ---------------------------------------------------------------------------- #
# L-1a: lane-output separator normalization — a no-leading-newline block (the
# edit.syntax native diagnostic) never jams onto the previous observation line
# ---------------------------------------------------------------------------- #
def test_l1a_no_leading_newline_block_gets_separator():
    prev = "observation text with no trailing newline"
    block = 'File "a/x.py", line 1\n  def (\nSyntaxError: invalid syntax'
    out = g._join_lane_output(prev, block)
    assert out == prev + "\n" + block          # exactly one \n inserted
    assert "newlineFile" not in out            # the live jam is gone


def test_l1a_steer_opening_with_newline_is_byte_identical():
    """Every default nudge/verify opens with `\\n` — those must be untouched."""
    prev = "obs"
    block = '\n<gt-nudge reason="loop">\nGT: stop.\n</gt-nudge>'
    assert g._join_lane_output(prev, block) == prev + block   # no extra \n


def test_l1a_prev_ending_newline_is_byte_identical():
    prev = "obs\n"
    block = 'File "a/x.py", line 1'
    assert g._join_lane_output(prev, block) == prev + block   # boundary already present


def test_l1a_empty_prev_untouched():
    block = 'File "a/x.py", line 1'
    assert g._join_lane_output("", block) == block


# ---------------------------------------------------------------------------- #
# L-1b: edit_check names the file REPO-RELATIVE in the diagnostic (not basename)
# ---------------------------------------------------------------------------- #
def test_l1b_diagnostic_uses_repo_relative_path(tmp_path):
    from groundtruth.runtime import edit_check
    pkg = tmp_path / "a"
    pkg.mkdir()
    bad = pkg / "x.py"
    bad.write_text("def f(:\n    pass\n", encoding="utf-8")
    res = edit_check.check_edit_syntax("a/x.py", str(tmp_path))
    assert res["verdict"] == "syntax_error"
    # repo-relative path present, bare basename form absent
    assert 'File "a/x.py"' in res["diagnostic"]
    assert 'File "x.py"' not in res["diagnostic"]


def test_l1b_absolute_path_derives_repo_relative(tmp_path):
    from groundtruth.runtime import edit_check
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    bad = pkg / "y.py"
    bad.write_text("x = (\n", encoding="utf-8")
    res = edit_check.check_edit_syntax(str(bad), str(tmp_path))
    assert res["verdict"] == "syntax_error"
    assert 'File "pkg/y.py"' in res["diagnostic"]


# ---------------------------------------------------------------------------- #
# V-1: verify advisory verb agrees with subject number (no "tests covers them")
# ---------------------------------------------------------------------------- #
def test_v1_plural_subject_plural_verb():
    from groundtruth.runtime import verification_horizon as vh
    body = vh.render_verify_emission(
        "advisory", action_count=10, step_limit=100,
        edited_rels={"a/x.py"}, covering_tests=None)
    assert "the relevant tests cover them" in body
    assert "tests covers them" not in body       # the live garble is gone


def test_v1_singular_subject_singular_verb():
    from groundtruth.runtime import verification_horizon as vh
    body = vh.render_verify_emission(
        "advisory", action_count=10, step_limit=100,
        edited_rels={"a/x.py"}, covering_tests=["a/test_x.py"])
    assert "a graph-linked covering test covers them" in body


# ---------------------------------------------------------------------------- #
# D-8: the Gateway's own render mode is keyed to GT_GATEWAY_NATIVE, DECOUPLED
# from GT_POST_SEARCH_NATIVE (the post_search FORMAT arm) — a FORM A/B on one
# surface no longer contaminates the other (the flag-overload confound)
# ---------------------------------------------------------------------------- #
def test_d8_gateway_dispatch_reads_its_own_flag():
    src = (_REPO / "artifact_deepswe/gt_mini_patch.py").read_text(encoding="utf-8")
    # the gateway render-mode variable keys off GT_GATEWAY_NATIVE ...
    assert 'native = os.environ.get("GT_GATEWAY_NATIVE") == "1"' in src
    # ... and the render-mode variable is NEVER assigned from the post_search FORMAT
    # flag (that re-coupling was the D-8 confound). The post_search flag may still be
    # NAMED in comments / the shared def-facts renderer — only the `native =` dispatch
    # assignment must be decoupled.
    assert 'native = os.environ.get("GT_POST_SEARCH_NATIVE")' not in src
