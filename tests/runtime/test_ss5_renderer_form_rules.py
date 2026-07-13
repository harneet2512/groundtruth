"""SS-5 — lock the 8 FORM RULES on EVERY native_render renderer.

The renderers are already native (SM-1 / W10 era) — this suite PINS that so a regression to
GT-voice narration, self-identification, an authority/confidence marker, or a leaked ``<gt-*>``
tag / test identity reddens. Rules enforced per the SS research memo's FORM-RULE deliverable:
  R2  native-channel mimicry: 0 ``<gt-*>`` tags survive any renderer.
  R5  no self-identification: no renderer names GroundTruth / GT / "ground truth (tool)".
  R6  zero leakage: ``contains_test_identity(out) is False`` for every renderer.
  (C1) no confidence/authority markers ([VERIFIED]/[WARNING]/[INFO]/verified caller/certified).
  entity-present: an ACTIONABLE fact carries a ``path:line`` / ``symbol`` referent so the SS-0
  ack watcher can detect an acknowledgment.
Each rule ships with MUTATION EVIDENCE: the guard predicate is proven to BITE on a synthetic
violating line, so a renderer that regressed to emit one would be caught.
"""
from __future__ import annotations

import re

from groundtruth.runtime import native_render as nr

# ── the guard predicates ─────────────────────────────────────────────────────
_SELF_ID_RE = re.compile(r"groundtruth|ground[ -]truth|\bgt\b|gt ran|gt shows", re.IGNORECASE)
_CONF_MARKER_RE = re.compile(
    r"\[VERIFIED\]|\[WARNING\]|\[INFO\]|high[- ]confidence|certified|verified caller",
    re.IGNORECASE)
_ENTITY_RE = re.compile(r"[\w./\\+\-]+:\d+|\b\w+\(\)|`[^`]+`|\.[a-z]{1,4}\b")

_TEST_FILES = ["tests/test_pool.py", "tests/pool_spec.rs"]

# One representative NON-EMPTY output per renderer I own. (edited_symbol / entity present so the
# actionable-renderer entity rule is exercised.)
def _samples() -> dict[str, str]:
    return {
        "covering_failure": nr.render_covering_failure_native(
            {"stdout_tail": '  File "src/pool.py", line 88, in acquire\nE   AssertionError: got 3 want 4',
             "stderr_tail": ""},
            edited_symbol="acquire", test_files=_TEST_FILES),
        "syntax_error": nr.render_syntax_error_native(
            {"verdict": "syntax_error",
             "diagnostic": '  File "src/x.py", line 5\n    def f(\n         ^\nSyntaxError: unexpected EOF'}),
        "submit_rejection": nr.render_submit_rejection("unresolved failures", "type-check failed"),
        "trace_frame": nr.render_trace_frame_native("src/pool.py", 88, "acquire"),
        "signature_delta": nr.render_signature_delta_native(
            "src/caller.py", 12, "connect", expected_min=2, expected_max=2, given=3),
        "caller_contract": nr.render_caller_contract_native("connect", 5, 3, def_file="src/db.py"),
        "registration": nr.render_registration_native(
            "src/reg.py", 10, "new_handler", ["a_handler", "b_handler"]),
        "def_rows": nr.render_def_rows_native([("src/a.py", 10, "foo"), ("src/b.py", 20, "bar")]),
        "ranked_list": nr.render_ranked_list_native([("src/a.py", 10, "foo")]),
        "body_concept": nr.render_body_concept_native([("src/a.py", 10, "foo")]),
        "recovery": nr.render_recovery_native("no_test_evidence", "Run the covering test before submitting"),
        "scope_constraint": nr.render_scope_constraint_native("src/config.py"),
        "completion_cert": nr.render_completion_cert_native(
            ["selected_test_status", "type_status"],
            hygiene_blocked=True, hygiene_detail="vendored file changed"),
    }


# Renderers whose non-empty output MUST carry a path:line / symbol entity (actionable facts).
_ACTIONABLE = {
    "covering_failure", "syntax_error", "trace_frame", "signature_delta", "caller_contract",
    "registration", "def_rows", "ranked_list", "body_concept", "scope_constraint",
}


def test_no_renderer_emits_a_gt_tag():
    for name, out in _samples().items():
        assert not nr.contains_gt_tag(out), f"{name} leaked a <gt-*> tag: {out!r}"


def test_no_renderer_self_identifies():
    for name, out in _samples().items():
        assert not _SELF_ID_RE.search(out), f"{name} self-identifies (R5): {out!r}"


def test_no_renderer_adds_confidence_markers():
    for name, out in _samples().items():
        assert not _CONF_MARKER_RE.search(out), f"{name} carries a confidence marker (C1): {out!r}"


def test_no_renderer_leaks_test_identity():
    for name, out in _samples().items():
        assert not nr.contains_test_identity(out, _TEST_FILES), f"{name} leaked test identity: {out!r}"


def test_actionable_renderers_carry_an_entity():
    samples = _samples()
    for name in _ACTIONABLE:
        out = samples[name]
        assert out, f"{name} produced empty output for a valid input"
        assert _ENTITY_RE.search(out), f"{name} carries no path:line/symbol entity: {out!r}"


def test_recovery_is_imperative_single_line():
    # R7: the proven-consumed recovery form is SHORT + ACTIVE (verb-first) + one line.
    out = nr.render_recovery_native("no_test_evidence", "Run the covering test before submitting")
    assert "\n" not in out
    assert out.split()[0][0].isupper() or out.split()[0].islower()  # a leading verb token
    assert out.startswith("Run ")


def test_cochange_is_internal_only():
    # cochange has NO model-facing native form — it must ALWAYS be quiet (never GT narration).
    assert nr.render_cochange_native() == ""
    assert nr.render_cochange_native("co-changed with the edit in 3 commits", n=3) == ""


# ── MUTATION EVIDENCE: the guard predicates BITE ─────────────────────────────
def test_guards_bite_on_synthetic_violations():
    # If a renderer regressed to prepend a GT-voice / self-ID / confidence marker, THESE fire.
    assert _SELF_ID_RE.search("GroundTruth: src/a.py:10:foo")
    assert _SELF_ID_RE.search("GT ran the covering test")
    assert _CONF_MARKER_RE.search("[VERIFIED] src/db.py: connect() signature changed")
    assert _CONF_MARKER_RE.search("3 verified caller(s) — preserve this interface")
    assert nr.contains_gt_tag("<gt-fact>src/a.py:10</gt-fact>")
    assert nr.contains_test_identity("tests/test_pool.py::test_acquire", _TEST_FILES)
    # and the entity detector actually requires an entity (an empty/opaque string has none)
    assert not _ENTITY_RE.search("scope is broad, be careful")


def test_correct_or_quiet_on_empty_input():
    # every renderer returns "" (never narration) when its input carries no signal.
    assert nr.render_covering_failure_native({}) == ""
    assert nr.render_syntax_error_native({"verdict": "ok"}) == ""
    assert nr.render_trace_frame_native("", 1) == ""
    assert nr.render_signature_delta_native("", 1, "", expected_min=1, expected_max=1, given=1) == ""
    assert nr.render_caller_contract_native("sym", 0, 0) == ""
    assert nr.render_registration_native("f.py", 1, "sym", []) == ""
    assert nr.render_def_rows_native([]) == ""
    assert nr.render_ranked_list_native([]) == ""
    assert nr.render_recovery_native("r", "") == ""
    assert nr.render_scope_constraint_native(None) == ""
    assert nr.render_completion_cert_native([]) == ""
