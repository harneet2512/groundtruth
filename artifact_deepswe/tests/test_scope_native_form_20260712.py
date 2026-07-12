r"""Task #63 (2026-07-12) — the SCOPE surface FORM A/B: <gt-scope> tag -> native compiler note.

Context. #59 wired ``render_scope_constraint_native`` into the adapter's per-class native
dispatch (``_NATIVE_CLASS_RENDERERS["scope"]``) but NO producer emits a ``scope``-class
envelope, so that renderer was PRODUCTION-DORMANT. The only LIVE scope surface is the mini
seam's Lane-A ``gt_mini_patch._consensus_scope_block`` <gt-scope> TAG (delivered on view/edit
of an in-scope file). This wave makes the BUILT renderer LIVE by routing the SAME Lane-A scope
facts through it under a per-surface FORM flag ``GT_SCOPE_NATIVE`` — mirroring the post_search
tag->native FORM arm (``GT_POST_SEARCH_NATIVE`` / ``_fmt_def_facts_native``). No new registry
class, no gateway producer, no envelope: the change is a single flag-gated FORM swap inside the
seam's own block builder (the smaller of the two routes — route (a) would need a brand-new
registered ``scope`` fact class in the shared fact_registry spine + a gateway producer).

Discipline held:
  * SAME firing occasions — the flag is read AFTER every guard + the ``_seen`` latch burn + the
    loser-rearm arm, so ONLY the delivered FORM changes; the latch, the guards, and the rearm
    are byte-identical to the tag arm. Proven by the latch + guard-parity tests below.
  * DEFAULT OFF = the EXACT legacy <gt-scope> tag bytes (byte-identical-off pin).
  * LEAK: the native form routes each neighbour through the renderer's own
    ``_is_test_path``/``_final_scrub`` belts, so a test-path scope entry DROPS (the native form
    is strictly leak-SAFER than the tag, which had no per-line test firewall).

RED-first (this file goes RED on the pre-fix tree — proven by the documented mutations, each of
which reverts a load-bearing hunk):
  M0  remove the ``if os.environ.get("GT_SCOPE_NATIVE") == "1": return _consensus_scope_native``
      branch (pre-#63 state) -> the flag-on call returns the <gt-scope> TAG ->
      ``test_scope_native_form_when_flag_on`` REDDENS (a tag is present, no note grammar).
  M1  blank the native renderer (``render_scope_constraint_native`` -> "") -> the native arm
      produces "" -> ``test_scope_native_form_when_flag_on`` REDDENS (empty, no note).
  M2  neuter the identity firewall (``native_render._is_test_path`` -> False) -> a test-path
      neighbour leaks into a native note -> ``test_scope_native_firewalls_test_path_neighbour``
      REDDENS (leak pin fails).

Hermetic: ``_query_scope`` monkeypatched (no real graph.db), no ONNX, no repo checkout.
Windows: run with ``PYTHONIOENCODING=utf-8`` (the legacy tag carries an em-dash, U+2014).
"""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(_HERE, ".."))

import gt_mini_patch as g  # noqa: E402
import groundtruth.runtime.native_render as nr  # noqa: E402

# The em-dash (U+2014) the legacy tag renders. This source is UTF-8; the module docstring notes
# PYTHONIOENCODING=utf-8 for the Windows console when the tag bytes are printed on a failure.
_EM = "—"


# --------------------------------------------------------------------------- #
# fixtures / helpers
# --------------------------------------------------------------------------- #
@pytest.fixture
def scope_on(monkeypatch):
    """GT-on, clean oracle/latch state, a 1-neighbour scope. Flag NOT set (each test sets it)."""
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_query_scope", lambda rel: ["src/neighbor.py"])
    monkeypatch.delenv("GT_SCOPE_NATIVE", raising=False)
    g._reset_oracle_state()
    yield monkeypatch
    g._reset_oracle_state()


def _fire(rel: str) -> str:
    """Seed scope membership + fire the real block builder (SAME path production uses)."""
    g._consensus_scope.add(g._norm_rel(rel))
    return g._consensus_scope_block(rel)


def _expected_tag(rel_short: str, *neigh_short: str) -> str:
    """The EXACT legacy <gt-scope> tag bytes for a rel + its (already-short) neighbours."""
    lines = ["1. %s %s in scope (you are working here)" % (rel_short, _EM)]
    for i, fp in enumerate(neigh_short, 2):
        lines.append("%d. %s %s graph-connected" % (i, fp, _EM))
    return (
        '\n<gt-scope files="%d">\n' % len(lines)
        + "\n".join(lines)
        + "\nThese files are graph-connected in scope; GT has not confirmed a single "
          "primary target %s confirm the edit target with grep.\n</gt-scope>" % _EM
    )


# =========================================================================== #
# 1. FORM ON — native compiler-note grammar, ZERO <gt-*> tag
# =========================================================================== #
def test_scope_native_form_when_flag_on(scope_on):
    """Flag on + scope fires -> the native ``<file>: note: your change must also update this
    file`` grammar, no <gt-scope>/<gt-*> anywhere.

    RED (M0): pre-#63 the flag was never read -> this returns the <gt-scope> tag (has a tag,
    no note). RED (M1): a blanked renderer returns "" (no note)."""
    scope_on.setenv("GT_SCOPE_NATIVE", "1")
    out = _fire("src/work.py")
    assert out == "src/neighbor.py: note: your change must also update this file"
    assert "<gt-" not in out.lower()          # native rides the compiler channel, tag-free
    assert "gt-scope" not in out.lower()
    assert nr.contains_test_identity(out) is False


def test_scope_native_form_multi_neighbour_one_note_per_file(scope_on):
    """Every graph-connected neighbour (same [:4] cap + order as the tag) becomes its OWN
    native ``note:`` continuation line — the SAME facts, native form. The SELF file is NOT a
    ``note:`` (the agent is already editing it), so it is absent from the native form."""
    scope_on.setattr(g, "_query_scope", lambda rel: ["src/a.py", "src/b.py", "src/c.py"])
    scope_on.setenv("GT_SCOPE_NATIVE", "1")
    out = _fire("src/work.py")
    assert out == (
        "src/a.py: note: your change must also update this file\n"
        "src/b.py: note: your change must also update this file\n"
        "src/c.py: note: your change must also update this file"
    )
    assert "work.py" not in out               # self is the edit anchor, not a must-ALSO-touch
    assert "<gt-" not in out.lower()


def test_scope_native_form_caps_at_four_like_the_tag(scope_on):
    """The native form keeps the tag's ``neigh[:4]`` cap — a 6-neighbour scope renders exactly
    4 notes (byte-count of DELIVERED facts unchanged; only the form differs)."""
    scope_on.setattr(g, "_query_scope",
                     lambda rel: ["src/n%d.py" % i for i in range(6)])
    scope_on.setenv("GT_SCOPE_NATIVE", "1")
    out = _fire("src/work.py")
    assert out.count(": note: your change must also update this file") == 4
    assert "src/n4.py" not in out and "src/n5.py" not in out   # dropped by the [:4] cap


# =========================================================================== #
# 2. FORM OFF — the EXACT legacy <gt-scope> tag bytes (byte-identical-off pin)
# =========================================================================== #
def test_scope_tag_byte_identical_when_flag_off(scope_on):
    """Default OFF (flag unset) -> the EXACT pre-#63 <gt-scope> tag bytes, character for
    character. This is the byte-identical-off contract: nothing but the FORM flag may change
    the tag arm."""
    out = _fire("src/work.py")
    assert out == _expected_tag("src/work.py", "src/neighbor.py")


def test_scope_tag_byte_identical_when_flag_zero(scope_on):
    """An explicit ``GT_SCOPE_NATIVE=0`` is also the tag arm (only ``==\"1\"`` flips the form)."""
    scope_on.setenv("GT_SCOPE_NATIVE", "0")
    out = _fire("src/work.py")
    assert out == _expected_tag("src/work.py", "src/neighbor.py")
    assert "<gt-scope" in out


# =========================================================================== #
# 3. LEAK — a test-path neighbour DROPS from the native form (firewalled)
# =========================================================================== #
def test_scope_native_firewalls_test_path_neighbour(scope_on):
    """Flag on: a source neighbour renders a note; a TEST-path neighbour is firewalled out by
    the renderer's ``_is_test_path`` belt. contains_test_identity == False.

    RED (M2): neuter ``native_render._is_test_path`` -> the test target leaks into a note."""
    scope_on.setattr(g, "_query_scope",
                     lambda rel: ["src/neighbor.py", "tests/test_helpers.py"])
    scope_on.setenv("GT_SCOPE_NATIVE", "1")
    out = _fire("src/work.py")
    assert out == "src/neighbor.py: note: your change must also update this file"
    assert "test_helpers" not in out and "tests/" not in out
    assert nr.contains_test_identity(out) is False


def test_scope_native_quiet_when_only_test_neighbour(scope_on):
    """Flag on: when the ONLY neighbour is a test path, the native form is "" (correct-or-quiet;
    Lane A drops the empty block). CONTRAST: the tag arm (flag off) would still LIST that test
    file — so the native form is strictly leak-SAFER than the legacy tag."""
    scope_on.setattr(g, "_query_scope", lambda rel: ["tests/test_helpers.py"])
    scope_on.setenv("GT_SCOPE_NATIVE", "1")
    assert _fire("src/work.py") == ""                         # native: firewalled -> quiet

    g._reset_oracle_state()
    scope_on.delenv("GT_SCOPE_NATIVE", raising=False)
    tag = _fire("src/work.py")                                # tag arm: has NO test firewall
    assert "test_helpers" in tag and "<gt-scope" in tag       # (documents the native's win)


# =========================================================================== #
# 4. SAME FIRING OCCASIONS — the latch + guards are byte-identical across the flag
# =========================================================================== #
def test_scope_native_latch_burns_identically(scope_on):
    """The ``_seen`` latch is burned by the SAME guard path in both arms: under the flag, the
    FIRST view/edit fires the native form, the SECOND (same file) returns "" — the once-per-file
    occasion is unchanged, only the form of the first delivery differs."""
    scope_on.setenv("GT_SCOPE_NATIVE", "1")
    first = _fire("src/work.py")
    assert first == "src/neighbor.py: note: your change must also update this file"
    assert ("consensus_scope", g._norm_rel("src/work.py")) in g._seen
    assert g._consensus_scope_block("src/work.py") == ""      # latched -> no re-fire


def test_scope_guards_identical_across_flag(scope_on):
    """The pre-render guards (baseline / off-scope / isolated) return "" REGARDLESS of the flag
    — the flag only ever changes the FORM of a fact that WOULD have delivered."""
    for flag in (None, "1"):
        # (a) baseline: no delivery either way
        g._reset_oracle_state()
        if flag:
            scope_on.setenv("GT_SCOPE_NATIVE", flag)
        else:
            scope_on.delenv("GT_SCOPE_NATIVE", raising=False)
        scope_on.setattr(g, "_GT_BASELINE", True)
        g._consensus_scope.add(g._norm_rel("src/work.py"))
        assert g._consensus_scope_block("src/work.py") == ""

        # (b) off-scope file: never in _consensus_scope -> "" (both arms)
        g._reset_oracle_state()
        scope_on.setattr(g, "_GT_BASELINE", False)
        assert g._consensus_scope_block("src/never.py") == ""

        # (c) isolated file: in scope but ZERO neighbours -> "" (both arms)
        g._reset_oracle_state()
        scope_on.setattr(g, "_query_scope", lambda rel: [])
        g._consensus_scope.add(g._norm_rel("src/lonely.py"))
        assert g._consensus_scope_block("src/lonely.py") == ""


# =========================================================================== #
# 5. BITING MUTATIONS — each reddens a load-bearing hunk
# =========================================================================== #
def test_mutation_blank_renderer_reddens_native_form(scope_on):
    """M1: with the native renderer blanked, the native arm produces "" — proving the renderer
    is what carries the native form (not incidental)."""
    scope_on.setenv("GT_SCOPE_NATIVE", "1")
    assert _fire("src/work.py")                               # REAL: non-empty native note

    g._reset_oracle_state()
    scope_on.setattr(nr, "render_scope_constraint_native", lambda *a, **k: "")
    assert _fire("src/work.py") == ""                         # MUTANT: no renderer -> quiet


def test_mutation_neuter_firewall_leaks_test_path(scope_on):
    """M2: neuter the renderer's ``_is_test_path`` WHOLE-FILE belt -> the test-path neighbour is
    no longer dropped; it appears as a SECOND native note line. Proves ``_is_test_path`` is the
    load-bearing whole-line-drop belt for a test scope entry (the SECOND belt, ``_final_scrub``,
    still scrubs the basename token ``test_helpers`` -> ``<test>``, so the two belts are layered
    — this mutation isolates the first)."""
    scope_on.setattr(g, "_query_scope",
                     lambda rel: ["src/neighbor.py", "tests/test_helpers.py"])
    scope_on.setenv("GT_SCOPE_NATIVE", "1")
    real = _fire("src/work.py")
    assert "tests/" not in real and real.count(": note:") == 1  # REAL: test path dropped whole

    g._reset_oracle_state()
    scope_on.setattr(nr, "_is_test_path", lambda *a, **k: False)
    leaked = _fire("src/work.py")
    assert "tests/" in leaked and leaked.count(": note:") == 2  # MUTANT: the belt was load-bearing


# =========================================================================== #
# 6. the native helper itself (unit — direct, no seam state)
# =========================================================================== #
def test_consensus_scope_native_helper_is_note_join():
    assert g._consensus_scope_native(["src/a.py", "src/b.py"]) == (
        "src/a.py: note: your change must also update this file\n"
        "src/b.py: note: your change must also update this file"
    )
    assert g._consensus_scope_native([]) == ""               # no neighbour -> quiet
    assert g._consensus_scope_native(["tests/test_x.py"]) == ""  # all-test -> firewalled quiet


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
