r"""SM-10 Item C (2026-07-12) — DECOUPLE the executed covering-RED from the once-per-task
risk-advisory latch, and broaden ``render_covering_failure_native`` so a real RED in a
non-standard runner grammar is not fail-closed to "".

THE BUG (Fable-confirmed audit finding). The flagship EXECUTED world-fact — the repo's OWN
covering test run in the agent's env, delivered as the Format-D native RED (ladder class
``executed_world_fact``, rank 70) — was reachable ONLY inside
``_verification_horizon_candidate``'s risk block, gated by ``_risk_trigger`` (a high-fan-in
>=0.5 edited-but-untested obligation symbol) AND the unspent once-per-task
``_horizon_advisory_fired`` latch. So a covering test that REDs on a LOW-fan-in edit, or on a
SECOND distinct-symbol edit after the latch was spent, NEVER ran — the execution-fact missed
its own decision point (T4/T5). Separately, ``render_covering_failure_native``'s default-STRIP
tail swallowed a RED whose runner grammar matched none of the specific recognizers -> "".

THE FIX, proven here on the REAL seam (hermetic — every graph/runner dependency injected):
  * ``_executed_covering_candidate`` delivers the executed RED for a LOW-fan-in edit EVEN when
    the once-per-task advisory latch is SPENT (was unreachable pre-fix);
  * it re-arms PER DISTINCT edited symbol (a 2nd distinct-symbol edit's RED still fires),
    bounded by the per-symbol ``_covering_exec_fired_syms`` latch (one run per symbol until
    re-edited);
  * byte-identical when GT_VERIFY_EXECUTE is off (None, zero state touched);
  * ``render_covering_failure_native`` rescues a non-standard-grammar RED (leak-closed);
  * MUTATIONS (each applied, observed to bite, restored): restore the once-per-task coupling
    (gate the producer on the spent latch) -> the latch-spent delivery reddens; remove the
    broadened recognizer -> the non-standard RED is swallowed again.

Windows: run with PYTHONIOENCODING=utf-8.
"""
from __future__ import annotations

import os
import re

import pytest

import gt_mini_patch as g
from groundtruth.runtime import native_render as nr
from groundtruth.runtime.native_render import (
    contains_gt_tag,
    contains_test_identity,
    render_covering_failure_native,
)


@pytest.fixture(autouse=True)
def _clean_state():
    g._reset_oracle_state()
    yield
    g._reset_oracle_state()


# =========================================================================== #
# Part 3 — the native-grammar broadening (a non-standard RED is not swallowed)
# =========================================================================== #
def test_nonstandard_grammar_red_is_rescued():
    """A RED whose runner grammar matches NONE of the specific recognizers (no pytest ``E``
    line, no ``path:line`` frame, no got/want value delta) previously default-STRIPped every
    line -> "". The permissive second pass now rescues it (leak-closed)."""
    res = {"stdout_tail": "Check FAILED: routine produced 5, the contract requires 7"}
    out = render_covering_failure_native(res, edited_symbol="routine")
    assert out, "a real RED in a non-standard grammar must not fail-closed to ''"
    assert "routine produced 5" in out                 # the signal survives
    assert out.startswith("Your change to `routine()` fails a covering test:")
    # (b) leak law — the rescued line still passes the identity firewall.
    assert not contains_gt_tag(out)
    assert contains_test_identity(out) is False, out


def test_nonstandard_grammar_scrubs_embedded_testpath():
    """A rescued generic-fail line carrying an EMBEDDED test path keeps its signal but the
    test path is scrubbed to ``<test>`` by the _final_scrub belt (leak=0 preserved)."""
    res = {"stdout_tail": "failure occurred in tests/thing_spec.rb somewhere"}
    out = render_covering_failure_native(res, test_files=["tests/thing_spec.rb"])
    assert contains_test_identity(out, ["tests/thing_spec.rb"]) is False, out
    assert "thing_spec" not in out and "<test>" in out


def test_recognized_grammar_is_byte_identical():
    """A pytest ``E``-line RED already produced a block; the second pass runs ONLY when the
    first kept nothing, so recognized grammars are byte-unchanged."""
    out = render_covering_failure_native({"stdout_tail": "E   assert 1 == 2"},
                                         edited_symbol="transform")
    assert out == "Your change to `transform()` fails a covering test:\nassert 1 == 2"


def test_mutation_remove_broadened_recognizer_reddens(monkeypatch):
    """MUTATION — the broadened recognizer matches nothing (== removing the second pass's
    KEEP): the non-standard RED is swallowed to "" again, reddening the rescue pin."""
    monkeypatch.setattr(nr, "_RE_GENERIC_FAIL", re.compile(r"(?!)"))  # never matches
    res = {"stdout_tail": "Check FAILED: routine produced 5, the contract requires 7"}
    assert nr.render_covering_failure_native(res, edited_symbol="routine") == ""


# =========================================================================== #
# the seam decouple — inject every graph/runner dependency (hermetic)
# =========================================================================== #
def _prep_seam(monkeypatch, *, edited_syms, covering, block,
               action_count=5, last_test_step=None):
    monkeypatch.setenv("GT_VERIFY_EXECUTE", "1")
    monkeypatch.setattr(g, "_GT_BASELINE", False, raising=False)
    monkeypatch.setattr(g, "_action_count", action_count, raising=False)
    monkeypatch.setattr(g, "_last_test_step", last_test_step, raising=False)
    g._oracle_edited_rels.clear()
    g._oracle_edited_rels.add("mod.py")
    monkeypatch.setattr(g, "_edited_symbols_for_selection", lambda: set(edited_syms))
    monkeypatch.setattr(g, "_covering_tests_for_symbols",
                        lambda syms: list(covering) if syms else [])
    monkeypatch.setattr(g, "_executed_covering_emission",
                        lambda cov, rels, syms: (block if cov else None))


def test_low_fan_in_red_delivered_when_advisory_latch_spent(monkeypatch):
    """THE load-bearing decouple pin. The once-per-task advisory latch is ALREADY SPENT and
    the risk trigger does NOT fire (low fan-in) -> pre-fix the covering never ran. The
    independent producer delivers the executed RED regardless of the spent latch."""
    g._horizon_advisory_fired = True   # the once-per-task latch is SPENT
    _prep_seam(monkeypatch, edited_syms={"foo"},
               covering=[{"file": "test_mod.py", "confidence": 0.9}],
               block="A covering test fails:\n  at mod.py:2")
    cand = g._executed_covering_candidate()
    assert cand is not None, "a low-fan-in RED must deliver even with the advisory latch spent"
    sev, kind, block, edit_bound = cand
    assert kind == "verify.horizon.executed"    # ladder class executed_world_fact (rank 70)
    assert block.startswith("A covering test fails:")
    assert edit_bound is True
    assert "foo" in g._covering_exec_fired_syms  # per-symbol latch now holds foo
    # bounded: a SECOND call for the same (already-fired) symbol does not re-run.
    assert g._executed_covering_candidate() is None


def test_second_distinct_symbol_red_fires(monkeypatch):
    """A DISTINCT symbol's RED fires even after another symbol was already covering-executed —
    the per-symbol re-arm, NOT the once-per-task latch (pre-fix: latch spent -> never ran)."""
    g._covering_exec_fired_syms.add("foo")   # foo already covering-executed
    g._horizon_advisory_fired = True         # once-per-task latch spent
    _prep_seam(monkeypatch, edited_syms={"foo", "bar"},
               covering=[{"file": "test_bar.py", "confidence": 0.9}],
               block="A covering test fails:\n  at mod.py:9")
    cand = g._executed_covering_candidate()
    assert cand is not None and cand[1] == "verify.horizon.executed"
    assert "bar" in g._covering_exec_fired_syms   # the 2nd distinct symbol is now latched


def test_off_path_is_byte_identical(monkeypatch):
    """GT_VERIFY_EXECUTE off -> None and ZERO state touched (byte-identical)."""
    monkeypatch.delenv("GT_VERIFY_EXECUTE", raising=False)
    g._oracle_edited_rels.add("mod.py")
    assert g._executed_covering_candidate() is None
    assert g._covering_exec_fired_syms == set()   # latch never touched on the off path


def test_no_covering_file_latches_without_delivery(monkeypatch):
    """A symbol with NO covering file latches (so it is not re-queried every turn) but
    delivers nothing — correct-or-quiet; a re-edit re-arms it (see the difference_update in
    the post_edit handler)."""
    _prep_seam(monkeypatch, edited_syms={"foo"}, covering=[],
               block="A covering test fails:\n  at mod.py:2")
    assert g._executed_covering_candidate() is None
    assert "foo" in g._covering_exec_fired_syms   # latched (bounded), no delivery


def test_mutation_restore_once_per_task_coupling_reddens(monkeypatch):
    """MUTATION — re-couple the producer to the once-per-task latch (return None when the
    advisory latch is spent, the PRE-FIX reachability). The latch-spent delivery reddens: the
    fix delivers, the re-coupled variant does not."""
    g._horizon_advisory_fired = True
    _prep_seam(monkeypatch, edited_syms={"foo"},
               covering=[{"file": "test_mod.py", "confidence": 0.9}],
               block="A covering test fails:\n  at mod.py:2")
    # the fix delivers even with the latch spent (the green being defended):
    assert g._executed_covering_candidate() is not None
    # the re-coupled variant gates on the spent latch -> None (reddens the green):
    g._covering_exec_fired_syms.clear()  # reset the per-symbol latch for the mutated call
    def _recoupled():
        if g._horizon_advisory_fired:    # the restored once-per-task coupling (the mutation)
            return None
        return g._executed_covering_candidate()
    assert _recoupled() is None


# =========================================================================== #
# C-1 (Fable LIPI bounce, 2026-07-12) — the gate-loss re-arm must restore the
# RIGHT latch. `verify.horizon.executed` is emitted by TWO producers with DIFFERENT
# latch consumption; `_rearm_latches` keyed on the kind alone re-armed the once-per-task
# advisory dose for the INDEPENDENT producer (which never consumed it) — re-opening a spent
# dose + a redundant re-execution — AND left the real per-symbol latch burned (destroying the
# RED). The per-turn `_covering_exec_pending` record disambiguates.
# =========================================================================== #
def test_c1_independent_producer_records_symbols_not_advisory(monkeypatch):
    """The INDEPENDENT producer records its burned symbols and marks advisory=False (it does
    NOT consume the once-per-task advisory dose)."""
    g._horizon_advisory_fired = True             # once-per-task dose legitimately spent earlier
    g._covering_exec_pending["syms"].clear()
    g._covering_exec_pending["advisory"] = False
    _prep_seam(monkeypatch, edited_syms={"foo"},
               covering=[{"file": "test_mod.py", "confidence": 0.9}],
               block="A covering test fails:\n  at mod.py:2")
    cand = g._executed_covering_candidate()
    assert cand is not None and cand[1] == "verify.horizon.executed"
    assert g._covering_exec_pending["syms"] == {"foo"}     # symbols recorded for the re-arm
    assert g._covering_exec_pending["advisory"] is False   # NOT the advisory source


def test_c1_independent_loss_keeps_advisory_cap_and_rearms_symbol(monkeypatch):
    """THE C-1 load-bearing pin. The independent producer's executed RED LOSES the gate (an
    urgent band outranks it). `_rearm_latches` must (a) leave `_horizon_advisory_fired` TRUE —
    the spent once-per-task dose is NOT re-opened (no redundant advisory dose, no redundant
    covering re-execution) — and (b) re-arm the lost symbol OUT of `_covering_exec_fired_syms`
    (deferred-not-destroyed)."""
    g._horizon_advisory_fired = True
    g._covering_exec_pending["syms"].clear()
    g._covering_exec_pending["advisory"] = False
    _prep_seam(monkeypatch, edited_syms={"foo"},
               covering=[{"file": "test_mod.py", "confidence": 0.9}],
               block="A covering test fails:\n  at mod.py:2")
    assert g._executed_covering_candidate() is not None    # produced -> record populated
    assert "foo" in g._covering_exec_fired_syms
    # the executed RED LOSES the gate (outranked by urgent) -> the seam re-arms the losers.
    g._rearm_latches({"verify.horizon.executed", "verify.horizon.urgent"},
                     kkind="post_edit", kf="mod.py", krel="mod.py")
    assert g._horizon_advisory_fired is True, "advisory dose-cap must stay intact (C-1 defect a)"
    assert "foo" not in g._covering_exec_fired_syms, "the RED must be deferred, not destroyed (C-1 defect b)"


def test_c1_risk_path_loss_rearms_both_latches():
    """The RISK-path executed RED (which DID consume the advisory dose + mark the per-symbol
    latch — the risk block sets ``advisory=True``) re-arms BOTH on loss, so the risk block
    re-competes on a later turn (deferred, not destroyed)."""
    g._horizon_advisory_fired = True
    g._covering_exec_fired_syms.clear()
    g._covering_exec_fired_syms.add("foo")
    g._covering_exec_pending["syms"] = {"foo"}
    g._covering_exec_pending["advisory"] = True   # the risk block is the source
    g._rearm_latches({"verify.horizon.executed"},
                     kkind="post_edit", kf="mod.py", krel="mod.py")
    assert g._horizon_advisory_fired is False      # risk-path dose re-armed (re-competes)
    assert "foo" not in g._covering_exec_fired_syms


def test_c1_mutation_restore_unconditional_advisory_rearm_reddens():
    """MUTATION — restore the pre-fix UNCONDITIONAL ``_horizon_advisory_fired = False`` for a
    lost executed RED (simulated by marking the INDEPENDENT loss as advisory=True). The
    advisory-cap pin reddens: the spent dose is wrongly re-opened."""
    g._horizon_advisory_fired = True
    g._covering_exec_fired_syms.clear()
    g._covering_exec_fired_syms.add("foo")
    g._covering_exec_pending["syms"] = {"foo"}
    g._covering_exec_pending["advisory"] = True    # the mutation: independent loss treated as advisory-source
    g._rearm_latches({"verify.horizon.executed"},
                     kkind="post_edit", kf="mod.py", krel="mod.py")
    assert g._horizon_advisory_fired is False       # reddens the "stays True" pin


def test_c1_mutation_drop_symbol_rearm_reddens():
    """MUTATION — drop the ``difference_update`` re-arm (simulated by an empty pending symbol
    set): the lost RED's symbol stays burned in `_covering_exec_fired_syms` — destroyed, not
    deferred — reddening the deferred-not-destroyed pin."""
    g._covering_exec_fired_syms.clear()
    g._covering_exec_fired_syms.add("foo")
    g._covering_exec_pending["syms"] = set()       # the mutation: nothing re-armed
    g._covering_exec_pending["advisory"] = False
    g._rearm_latches({"verify.horizon.executed"},
                     kkind="post_edit", kf="mod.py", krel="mod.py")
    assert "foo" in g._covering_exec_fired_syms     # reddens the "re-armed" pin


def test_c1_off_path_executed_kind_never_arises(monkeypatch):
    """Byte-identical off: `verify.horizon.executed` is produced ONLY under GT_VERIFY_EXECUTE
    (both producers early-return None when off), so the new re-arm branch is unreachable and
    the advisory branch's added difference_update is a no-op on an empty record."""
    monkeypatch.delenv("GT_VERIFY_EXECUTE", raising=False)
    g._covering_exec_fired_syms.clear()
    g._covering_exec_pending["syms"].clear()
    g._covering_exec_pending["advisory"] = False
    g._horizon_advisory_fired = True
    # a budget-band advisory loss (no covering execution) -> advisory re-armed, no fired-syms churn.
    g._rearm_latches({"verify.horizon.advisory"},
                     kkind="post_edit", kf="mod.py", krel="mod.py")
    assert g._horizon_advisory_fired is False       # unconditional advisory re-arm unchanged
    assert g._covering_exec_fired_syms == set()     # difference_update(empty) -> no-op


# =========================================================================== #
# C-1b (Fable re-verify bounce, 2026-07-12) — the advisory-branch per-symbol re-arm
# must be GATED on the RISK source. `_covering_exec_pending` is per-TURN, not per-producer:
# on a NON-risk turn a BAND advisory AND the INDEPENDENT executed producer can BOTH fire, so
# ``syms`` then holds the EXECUTED producer's symbols (advisory=False). When the band advisory
# LOSES while the executed RED WINS+delivers, an ungated advisory-branch difference_update would
# DESTROY the delivered winner's latch (-> redundant ~35-70s re-execution + a duplicate dose next
# turn). The MUTATION (restore the ungated difference_update, i.e. revert the ``if advisory`` gate)
# is verified at SOURCE level: with the gate removed, test_c1b_executed_winner_latch_survives_...
# FAILS (winner's latch destroyed); gated -> passes.
# =========================================================================== #
def test_c1b_executed_winner_latch_survives_band_advisory_loss():
    """THE C-1b load-bearing pin (the one the suite was missing). The independent executed RED
    WINS+delivers while a BAND advisory LOSES the SAME turn — record = independent source
    (syms={sym}, advisory=False). `_rearm_latches` on the advisory loss must LEAVE the winner's
    per-symbol latch INTACT."""
    g._covering_exec_fired_syms.clear()
    g._covering_exec_fired_syms.add("blowfan")            # burned by the WINNING executed producer
    g._covering_exec_pending["syms"] = {"blowfan"}         # per-turn record = INDEPENDENT source
    g._covering_exec_pending["advisory"] = False
    g._horizon_advisory_fired = True                       # the band advisory consumed the dose
    g._rearm_latches({"verify.horizon.advisory"},          # band advisory LOST (executed won)
                     kkind="post_edit", kf="src/mod.py", krel="src/mod.py")
    assert g._horizon_advisory_fired is False              # the band's OWN dose re-arms (correct)
    assert "blowfan" in g._covering_exec_fired_syms, (
        "the DELIVERED executed winner's latch must survive the ADVISORY's loss (C-1b)")


def test_c1b_risk_advisory_loss_still_rearms_its_own_syms():
    """The gate must not OVER-restrict: when the RISK block produced the advisory (advisory=True,
    it marked its OWN syms), an advisory LOSS STILL re-arms those syms (deferred-not-destroyed)."""
    g._covering_exec_fired_syms.clear()
    g._covering_exec_fired_syms.add("A_highfan")
    g._covering_exec_pending["syms"] = {"A_highfan"}
    g._covering_exec_pending["advisory"] = True            # RISK block is the source
    g._horizon_advisory_fired = True
    g._rearm_latches({"verify.horizon.advisory"},
                     kkind="post_edit", kf="src/mod.py", krel="src/mod.py")
    assert g._horizon_advisory_fired is False
    assert "A_highfan" not in g._covering_exec_fired_syms  # risk-block syms re-armed (deferred)


def test_c1b_both_lose_executed_branch_rearms_syms():
    """Both the band advisory AND the independent executed lose the same turn (a higher-severity
    producer won). advisory=False -> the advisory branch does NOT touch syms; the EXECUTED branch
    re-arms them unconditionally (the both-lose case is covered there)."""
    g._covering_exec_fired_syms.clear()
    g._covering_exec_fired_syms.add("blowfan")
    g._covering_exec_pending["syms"] = {"blowfan"}
    g._covering_exec_pending["advisory"] = False
    g._horizon_advisory_fired = True
    g._rearm_latches({"verify.horizon.advisory", "verify.horizon.executed"},
                     kkind="post_edit", kf="src/mod.py", krel="src/mod.py")
    assert g._horizon_advisory_fired is False              # band advisory dose re-armed
    assert "blowfan" not in g._covering_exec_fired_syms    # executed branch re-armed it (deferred)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
