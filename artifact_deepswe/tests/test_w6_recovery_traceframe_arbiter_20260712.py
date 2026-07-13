"""W6 (2026-07-12) — two global-arbiter delivery defects, verified on run 29217805592.

DEFECT 1 — the RECOVERY channel is structurally DEAD under the global arbiter.
    ``gt_mini_patch._recovery_candidate`` enters the pool with the LITERAL kind "recovery"
    (the Lane-B gate stamps ``_last_gate_winner_kind="recovery"``). ``global_arbiter.arbitrate``
    calls ``rank_of("recovery")`` -> ``class_of_kind`` found NO "recovery" KEY in
    ``_KIND_TO_CLASS`` (only the PRODUCER kinds that map TO the "recovery" class) -> "" -> rank
    None -> dropped ``REASON_INTERNAL``. Measured: 57/57 pool-recovery suppressions, 0 recovery
    deliveries. FIX 1a: add the ``"recovery": "recovery"`` self-map. FIX 1b (REQUIRED with 1a):
    the upstream classifier OVER-FIRES (a benign, number-scrubbed "… 0 failed …" summary recurs
    with an identical fingerprint on every PASSING run, so ``same_failure_unchanged_patch`` reads
    a progressing agent as a repeating failure) — so a deterministic STALL GATE
    (``_recovery_stall_active``) makes the candidate eligible ONLY on a genuine repetition/stall.

DEFECT 2 — ``trace_frame`` late-DEMOTED against its own registry declaration.
    ``fact_registry._REACTIVE_EVIDENCE_TYPES`` declares ``trace_frame`` REACTIVE (on-time BY
    CONSTRUCTION). But it aliases to the ``localization`` LADDER class, which is preventive, so the
    SM-10 repair_support demotion killed it as ``repair_support_late``. FIX 2: gate
    ``_ga_is_preventive`` on the registry's reactive declaration — a reactive evidence type is
    NEVER demoted for lateness (its ladder class is unchanged).

Hermetic (no graph / no ONNX). Windows: run with PYTHONIOENCODING=utf-8.
"""
from __future__ import annotations

import pytest

import gt_mini_patch as g
from groundtruth.runtime import fact_registry as fr
from groundtruth.runtime import global_arbiter as ga
from groundtruth.runtime import hypothesis_ledger as hl
from groundtruth.runtime.native_render import (  # noqa: E402
    contains_test_identity, render_recovery_native)


@pytest.fixture(autouse=True)
def _clean_state():
    g._reset_oracle_state()
    g._gt_hypothesis_recovery = None
    g._recovery_repeat_fp = False
    g._detect_loop_fired = False
    g._last_test_outcome_failed = False
    yield
    g._reset_oracle_state()
    g._gt_hypothesis_recovery = None


def _pool():
    """(delivered_list, mk) — mk(cand, tag) yields (cand, thunk) recording its tag on delivery."""
    delivered: list[str] = []

    def mk(cand, tag):
        def _t():
            delivered.append(tag)
        return (cand, _t)

    return delivered, mk


def _cand(kind, *, boundary, current, dedup_key, plane=ga.PLANE_GATEWAY):
    return ga.Candidate(plane=plane, kind=kind, dedup_key=dedup_key,
                        boundary_ordinal=boundary, current_ordinal=current, seq=0)


# =========================================================================== #
# FIX 1a — the "recovery" self-map puts the recovery CLASS NAME on the ladder.
# =========================================================================== #
def test_recovery_kind_resolves_to_the_recovery_class():
    # pre-fix RED: class_of_kind("recovery") == "" and rank_of("recovery") is None.
    assert ga.class_of_kind("recovery") == "recovery"
    assert ga.rank_of("recovery") == ga.RANK_LADDER["recovery"] == 10
    # the class VALUE already existed (producer kinds map to it) — this only makes the
    # class NAME itself resolve, so the seam's literal-"recovery" candidate reaches the ladder.
    assert "recovery" in {v for v in ga._KIND_TO_CLASS.values()}


# =========================================================================== #
# RED 1 — a pool holding ONLY a recovery candidate delivers under the arbiter.
# (pre-FIX-1a: winner=None / REASON_INTERNAL -> 0 bytes.)
# =========================================================================== #
def test_red1_lone_recovery_candidate_delivers_under_arbiter():
    delivered, mk = _pool()
    # recovery boundary=5 (verify/submit); on-time (current=5) -> not repair. It is REACTIVE
    # (class "recovery"), so it delivers even if late; here it is the lone winner.
    pool = [mk(_cand("recovery", boundary=5, current=5, dedup_key="k_rec"), "REC")]
    g._global_pool_flush(pool, kkind="post_edit", kf="a/x.py", krel="a/x.py")
    assert delivered == ["REC"]                        # FIX 1a: recovery reaches + wins the ladder
    assert "k_rec" in g._EPISODE.delivered_dedup       # dedup-stamped (delivered, not deferred)


def test_mutation_a_remove_recovery_mapping_reddens_red1(monkeypatch):
    """MUTATION (a): remove the ``"recovery"`` self-map (== the pre-FIX-1a tree). RED-1 reddens:
    the lone recovery candidate is dropped ``REASON_INTERNAL`` again (0 bytes, not stamped)."""
    delivered, mk = _pool()
    patched = dict(ga._KIND_TO_CLASS)
    patched.pop("recovery")                            # the mutation
    monkeypatch.setattr(ga, "_KIND_TO_CLASS", patched)
    assert ga.rank_of("recovery") is None              # confirm the mutation bit
    pool = [mk(_cand("recovery", boundary=5, current=5, dedup_key="k_rec"), "REC")]
    g._global_pool_flush(pool, kkind="post_edit", kf="a/x.py", krel="a/x.py")
    assert delivered == []                             # dropped internal -> 0 bytes (bites)
    assert "k_rec" not in g._EPISODE.delivered_dedup


# =========================================================================== #
# FIX 1b — the STALL GATE: suppress a progressing agent, admit a genuine stall.
# =========================================================================== #
def _arm_recovery(monkeypatch, *, disposition=None):
    disposition = disposition or hl.D_REQUEST_NEW_HYPOTHESIS
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setenv("GT_HYPOTHESIS", "1")
    monkeypatch.setenv("GT_GLOBAL_ARBITER", "1")
    imp = g._hypothesis_imperative_map()[disposition]
    g._gt_hypothesis_recovery = (disposition, imp)
    return imp


def test_stall_gate_suppresses_progressing_agent(monkeypatch):
    imp = _arm_recovery(monkeypatch)
    # PROGRESSING: last test PASSED (zero-count-safe sensor -> False), no repeat fp, no hard loop.
    g._last_test_outcome_failed = False
    g._recovery_repeat_fp = False
    g._detect_loop_fired = False
    rows: list[dict] = []
    monkeypatch.setattr(g, "_runtime_ledger_record", lambda **kw: rows.append(kw))
    assert g._recovery_candidate() is None, (
        "the recovery candidate must be SUPPRESSED at the stall gate on a progressing agent "
        "(FIX 1a alone would ship a nudge here — the over-fire the forensics found)")
    # the drop is LOGGED with a reason that NAMES the gate (never a silent drop).
    assert any(r.get("reason") == "recovery_stall_gate" for r in rows), rows
    assert imp  # sanity: a real imperative was armed (the candidate WOULD have rendered)


def test_stall_gate_admits_genuine_stall_and_delivers(monkeypatch):
    imp = _arm_recovery(monkeypatch)
    # GENUINE STALL: this turn's failure fingerprint RECURRED while the last test genuinely FAILED.
    g._last_test_outcome_failed = True
    g._recovery_repeat_fp = True
    g._detect_loop_fired = False
    rc = g._recovery_candidate()
    assert rc is not None
    sev, kind, text, edit_bound = rc
    assert kind == "recovery" and edit_bound is True
    assert text == render_recovery_native(hl.D_REQUEST_NEW_HYPOTHESIS, imp)
    assert not contains_test_identity(text)            # leak law: no test identity
    # with FIX 1a the admitted candidate WINS arbitration when nothing higher fires + DELIVERS.
    delivered, mk = _pool()
    key = g._ga_unified_dedup_key("recovery", "recovery", "", "", [text])
    cand = g._ga_make_candidate(ga.PLANE_STEER, "recovery", dedup_key=key,
                                kkind="post_edit", seq=0)
    assert cand is not None
    g._global_pool_flush([(cand, (lambda: delivered.append("REC")))],
                         kkind="post_edit", kf="a/x.py", krel="a/x.py")
    assert delivered == ["REC"]


def test_hard_loop_admits_recovery_even_when_tests_pass(monkeypatch):
    """A HARD degenerate loop (``_detect_loop_fired``: TIDE >=3x identical command+output) is a
    genuine stall by itself and admits recovery regardless of the test outcome."""
    _arm_recovery(monkeypatch)
    g._last_test_outcome_failed = False                # tests not failing
    g._recovery_repeat_fp = False                      # no fingerprint recurrence
    g._detect_loop_fired = True                        # but a hard loop this attempt
    assert g._recovery_candidate() is not None


def test_mutation_b_invert_stall_gate_reddens_progressing(monkeypatch):
    """MUTATION (b): invert the stall gate to ALWAYS-eligible. The progressing-agent case reddens
    — a progressing agent is now nudged (noise)."""
    _arm_recovery(monkeypatch)
    g._last_test_outcome_failed = False
    g._recovery_repeat_fp = False
    g._detect_loop_fired = False
    monkeypatch.setattr(g, "_recovery_stall_active", lambda: True)   # the mutation
    assert g._recovery_candidate() is not None         # progressing agent nudged (bites)


def test_stall_active_predicate_truth_table():
    """The stall predicate is exactly ``loop OR (last_test_failed AND repeat_fp)`` —
    conservative: a passing agent and a single fresh failure are each ineligible."""
    def probe(loop, failed, repeat):
        g._detect_loop_fired = loop
        g._last_test_outcome_failed = failed
        g._recovery_repeat_fp = repeat
        return g._recovery_stall_active()
    assert probe(False, False, False) is False         # idle / progressing
    assert probe(False, True, False) is False          # a single FRESH failure -> not yet a stall
    assert probe(False, False, True) is False          # a recurring benign summary while passing
    assert probe(False, True, True) is True            # genuine: repeated fp + failing state
    assert probe(True, False, False) is True           # hard degenerate loop admits alone


# =========================================================================== #
# RED 2 — a reactive ``trace_frame`` on a TEST turn is NOT demoted for lateness.
# (pre-FIX-2: _ga_is_preventive True -> repair_support_late -> 0 bytes.)
# =========================================================================== #
def test_red2_trace_frame_on_test_turn_delivers():
    delivered, mk = _pool()
    # trace_frame -> localization (boundary=1); delivered on a TEST turn (current=4) -> late.
    # REACTIVE by registry declaration -> FIX 2 must NOT demote it -> it delivers (lone winner).
    pool = [mk(_cand("trace_frame", boundary=1, current=4, dedup_key="k_tf"), "TRACE")]
    g._global_pool_flush(pool, kkind="post_edit", kf="a/x.py", krel="a/x.py")
    assert delivered == ["TRACE"]
    assert "k_tf" in g._EPISODE.delivered_dedup


def test_registry_reactive_types_are_never_preventive():
    """Registry-alignment PIN: every evidence_type the registry declares REACTIVE is judged
    NOT-preventive by the seam (so it can never be demoted for lateness)."""
    assert fr._REACTIVE_EVIDENCE_TYPES                 # non-empty (guards a vacuous pass)
    for et in fr._REACTIVE_EVIDENCE_TYPES:
        assert g._ga_is_preventive(et) is False, et
    # trace_frame's LADDER class is unchanged (localization) — only the lateness demotion is gated.
    assert ga.class_of_kind("trace_frame") == "localization"


def test_mutation_c_drop_reactive_check_reddens_registry_pin(monkeypatch):
    """MUTATION (c): drop the reactive check (force ``is_reactive`` -> False). ``trace_frame``'s
    preventive ladder class then flips it back to preventive, reddening the registry-alignment pin
    (and re-enabling the RED-2 demotion)."""
    monkeypatch.setattr(fr, "is_reactive", lambda _et: False)   # the mutation
    assert g._ga_is_preventive("trace_frame") is True           # reactive gate gone -> preventive
    # RED-2 reddens under the mutation: the late trace_frame is demoted -> 0 bytes.
    delivered, mk = _pool()
    pool = [mk(_cand("trace_frame", boundary=1, current=4, dedup_key="k_tf"), "TRACE")]
    g._global_pool_flush(pool, kkind="post_edit", kf="a/x.py", krel="a/x.py")
    assert delivered == []


# =========================================================================== #
# BYTE-IDENTITY — GT_GLOBAL_ARBITER off: the stall gate does NOT apply, so the
# arbiter-off recovery path (incl. the legacy inline steer delivery) is unchanged.
# =========================================================================== #
def test_arbiter_off_recovery_gate_not_applied(monkeypatch):
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setenv("GT_HYPOTHESIS", "1")
    monkeypatch.delenv("GT_GLOBAL_ARBITER", raising=False)   # arbiter OFF
    imp = g._hypothesis_imperative_map()[hl.D_REQUEST_NEW_HYPOTHESIS]
    g._gt_hypothesis_recovery = (hl.D_REQUEST_NEW_HYPOTHESIS, imp)
    # PROGRESSING globals — the gate WOULD suppress under the arbiter, but it must not apply off.
    g._last_test_outcome_failed = False
    g._recovery_repeat_fp = False
    g._detect_loop_fired = False
    rc = g._recovery_candidate()
    assert rc is not None and rc[1] == "recovery", (
        "with GT_GLOBAL_ARBITER OFF the stall gate must NOT apply — byte-identical to the "
        "pre-fix unconditional candidate (the gate is arbiter-scoped by design)")


def test_arbiter_off_flag_reader_is_false(monkeypatch):
    """Sanity pin for the byte-identity scope: the arbiter flag reader is False when unset/0."""
    monkeypatch.delenv("GT_GLOBAL_ARBITER", raising=False)
    assert g._global_arbiter_on() is False
    monkeypatch.setenv("GT_GLOBAL_ARBITER", "0")
    assert g._global_arbiter_on() is False
    monkeypatch.setenv("GT_GLOBAL_ARBITER", "1")
    assert g._global_arbiter_on() is True


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
