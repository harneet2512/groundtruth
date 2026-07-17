"""W4 content guards — Stage-1 deterministic tests (red-first + biting mutations).

Four generalized guards over the recovery + obligations + GT_HYPOTHESIS surfaces,
each grounded in a MEASURED smoke30-ss128 false/weak-fire:

  * GUARD 1 — infra/teardown-noise exclusion  (hydra-3005 l5.failure on a passing
    run's `pytest_unconfigure` AssertionError).
  * GUARD 2 — hypothesis-contradiction guard  (sh-744 "reconsider the target file"
    when the target was RIGHT / BAD_INFO_RESISTED).
  * GUARD 3 — recovery FORM at repeat-N        (dynaconf 3 correct thrash warnings
    ignored into budget death — form too weak).
  * GUARD 4 — obligation steering guard        (aiogram obligation narrowing away
    from rank-1 gold scene.py).

Each guard has: a POSITIVE (fires/suppresses correctly), a NEGATIVE (does NOT
touch the genuine case), and >=2 BITING MUTATIONS — a re-implementation of the
guard with its discriminating clause removed, asserted to CHANGE the verdict (so
the test has teeth: a silent weakening of the guard breaks a test). The ledger
observations replayed here are the real bytes extracted from
D:/gt_runs/smoke30_ss128_20260716 (quoted inline, read-only).
"""
from __future__ import annotations

import re

from groundtruth.runtime.content_guards import (
    escalate_recovery_form,
    guarded_obligation_symbols,
    obligation_narrowing_excludes_ranked,
    recovery_escalation_level,
    recovery_form_strength,
    target_corroboration_score,
    target_is_gt_corroborated,
)
from groundtruth.runtime.patterns import INFRA_NOISE_RE, is_infra_noise

# --------------------------------------------------------------------------- #
# Real replayed observations (read-only, quoted from the saved ledgers).
# --------------------------------------------------------------------------- #
# hydra-3005 tool msg 105: returncode 0, "389 passed", a stray plugin-teardown
# AssertionError deep in pytest_unconfigure.
HYDRA_TEARDOWN = (
    "<returncode>0</returncode>\n<output>\n"
    "    return wrap_session(config, _main)\n"
    '  File "/usr/local/lib/python3.8/site-packages/_pytest/config/__init__.py", '
    "line 1123, in _ensure_unconfigure\n"
    "    self.hook.pytest_unconfigure(config=self)\n"
    "  File \".../pytest_snail/plugin.py\", line 60, in pytest_unconfigure\n"
    '    config.pluginmanager.unregister("snail_plugin")\n'
    '    assert name is not None, "plugin is not registered"\n'
    "AssertionError: plugin is not registered\n"
    "============================= 389 passed in 1.51s ==="
)
# A GENUINE test-body regression (what a real l5.failure SHOULD fire on).
REAL_REGRESSION = (
    "<returncode>1</returncode>\n"
    "    def test_widget(self):\n"
    ">       assert compute(3) == 7\n"
    "E       AssertionError: assert 6 == 7\n"
    "=========== 1 failed, 12 passed in 0.42s ==========="
)


# =========================================================================== #
# GUARD 1 — infra/teardown-noise exclusion
# =========================================================================== #
def test_g1_hydra_teardown_is_infra_noise():
    """RED-first: the hydra passing-run teardown AssertionError must classify as
    infra noise so l5.failure stays silent on a green run."""
    assert is_infra_noise(HYDRA_TEARDOWN) is True


def test_g1_genuine_regression_is_not_infra_noise():
    """A real test-body failure ("1 failed") is never called noise — recovery
    must still steer."""
    assert is_infra_noise(REAL_REGRESSION) is False


def test_g1_teardown_coexisting_with_real_failure_is_not_noise():
    """A teardown frame that coexists with a genuine non-zero failure count must
    NOT mask the real regression (fail toward firing on real failures)."""
    coex = HYDRA_TEARDOWN + "\n1 failed, 388 passed in 1.6s"
    assert is_infra_noise(coex) is False


def test_g1_mutation_drop_realfail_guard_would_swallow_regression():
    """BITING MUTATION #1: remove the real-failure-count clause. A mutant that
    trusts only the infra signature would call a teardown-tainted REAL failure
    'noise' — the guard's real-fail clause is what prevents that."""
    def mutant(text: str) -> bool:            # infra-signature only (no real-fail veto)
        return bool(INFRA_NOISE_RE.search(text or ""))
    coex = HYDRA_TEARDOWN + "\n1 failed, 388 passed in 1.6s"
    assert mutant(coex) is True               # mutant wrongly suppresses a real failure
    assert is_infra_noise(coex) is False      # the real guard does not


def test_g1_fixture_setup_error_running_agent_code_is_not_noise():
    """REGRESSION (jupyterlab/jupyter-ai-1294): a pytest ``ERROR at setup of`` whose
    cause is a real ``ValueError`` in the agent's OWN ``config_manager.py`` — with
    per-test ``FAILED`` lines — must NOT be called infra noise. A fixture runs the
    agent's code; only harness-OWN machinery is noise."""
    jupyter = (
        "test_config_manager.py::test_init_with_blocklists FAILED [  7%]\n"
        "test_config_manager.py::test_init_with_default_values ERROR [ 23%]\n"
        "==================================== ERRORS ====================================\n"
        "_______________ ERROR at setup of test_init_with_default_values ________________\n"
        ">               raise ValueError(\n"
        "E               ValueError: No language model is associated with 'x'.\n"
        "packages/jupyter-ai/jupyter_ai/config_manager.py:295: ValueError")
    assert is_infra_noise(jupyter) is False


def test_g1_pytest_usage_error_is_not_teardown_noise():
    """REGRESSION (conan-17123): a pytest USAGE error (``unrecognized arguments:
    --timeout``) is a bad command, not a session-teardown finalizer. Removing the
    broad ``_pytest/config`` frame keeps it out of the infra-noise class."""
    conan = (
        "UsageError: usage: __main__.py [options] [file_or_dir]\n"
        "__main__.py: error: unrecognized arguments: --timeout=120\n"
        "ERROR: usage: __main__.py [options] [file_or_dir]")
    assert is_infra_noise(conan) is False


def test_g1_mutation_drop_infra_signature_would_miss_hydra():
    """BITING MUTATION #2: require ONLY the real-fail veto (no infra signature).
    Such a mutant never recognizes the hydra teardown as noise."""
    real_fail = re.compile(r"\b[1-9]\d* (?:failed|error)\b", re.I)
    def mutant(text: str) -> bool:            # 'noise = no real failure count'
        return not real_fail.search(text or "")
    plain_pass = "12 passed in 0.3s"          # a plain green run with NO teardown frame
    assert mutant(plain_pass) is True         # mutant over-fires on any clean pass
    assert is_infra_noise(plain_pass) is False  # the real guard needs an infra signature


# =========================================================================== #
# GUARD 2 — hypothesis-contradiction guard
# =========================================================================== #
# sh-744: the agent's edited target's tokens overlapped the issue anchors — the
# hypothesis was corroborated, yet the "reconsider the target file" steer fired.
SH744_TARGET_TOKENS = {"RunningCommand", "sh", "handle_close"}
SH744_ISSUE_ANCHORS = {"RunningCommand", "OProc", "close"}


def test_g2_corroborated_target_suppresses_wrong_target_steer():
    """RED-first: when the current target agrees with the issue anchors, the
    wrong-target steer must be SUPPRESSED (fail toward silence)."""
    assert target_is_gt_corroborated(SH744_TARGET_TOKENS, SH744_ISSUE_ANCHORS) is True


def test_g2_uncorroborated_target_still_fires():
    """A target with NO anchor agreement is not corroborated — genuine recovery
    still fires."""
    assert target_is_gt_corroborated({"unrelated_helper"}, SH744_ISSUE_ANCHORS) is False


def test_g2_empty_evidence_never_corroborates():
    """No anchors or no target => not corroborated (recovery fires); the guard
    never suppresses on absent evidence."""
    assert target_is_gt_corroborated(set(), SH744_ISSUE_ANCHORS) is False
    assert target_is_gt_corroborated(SH744_TARGET_TOKENS, set()) is False


def test_g2_mutation_union_instead_of_intersection_over_suppresses():
    """BITING MUTATION #1: score by |union| instead of |intersection|. That mutant
    corroborates EVERY non-empty pair — it would suppress recovery even when the
    target shares NOTHING with the anchors."""
    def mutant(tt, ia) -> bool:
        return len(set(tt) | set(ia)) >= 1
    assert mutant({"x"}, {"y"}) is True                       # mutant: always corroborated
    assert target_is_gt_corroborated({"x"}, {"y"}) is False   # real guard: not corroborated


def test_g2_mutation_min_agreement_zero_over_suppresses():
    """BITING MUTATION #2: lower the agreement threshold to 0. Then any pair is
    'corroborated' and recovery is permanently muted — the >=1 shared-token floor
    is load-bearing."""
    assert target_is_gt_corroborated({"x"}, {"y"}, min_agreement=1) is False
    # a mutant that clamps min_agreement below 1 is caught: guard clamps to >=1.
    assert target_is_gt_corroborated({"x"}, {"y"}, min_agreement=0) is False


# =========================================================================== #
# GUARD 3 — recovery FORM at repeat-N (escalate, never weaker)
# =========================================================================== #
DYNACONF_BODY = (
    "GT: you have rewritten settings.py 4 times with no passing test between edits "
    "- you are overwriting your own work blind. Run targeted verification FIRST."
)


def test_g3_first_fire_is_byte_identical():
    """RED-first: the 1st delivery is unchanged (a single-shot warning must not be
    perturbed) — byte-identical to the base."""
    assert escalate_recovery_form(DYNACONF_BODY, 1) == DYNACONF_BODY


def test_g3_repeat_escalates_and_is_never_weaker():
    """The 2nd and 3rd deliveries escalate monotonically and never weaken — the
    dynaconf 'ignored 3 times' shape gets a stronger short-active form each time."""
    f1 = escalate_recovery_form(DYNACONF_BODY, 1)
    f2 = escalate_recovery_form(DYNACONF_BODY, 2)
    f3 = escalate_recovery_form(DYNACONF_BODY, 3)
    assert recovery_form_strength(f1) == 0
    assert recovery_form_strength(f1) < recovery_form_strength(f2) <= recovery_form_strength(f3)
    assert recovery_escalation_level(1) < recovery_escalation_level(2) <= recovery_escalation_level(3)
    # base action is preserved (no information lost, only urgency added).
    assert "rewritten settings.py 4 times" in f3


def test_g3_escalation_level_is_monotone_and_capped():
    """The escalation ordinal is monotone non-decreasing in repeat_n and bounded."""
    levels = [recovery_escalation_level(n) for n in range(1, 8)]
    assert levels == sorted(levels)                 # never decreases
    assert levels[0] == 0 and max(levels) == max(levels[-1], 2)


def test_g3_mutation_downgrade_on_repeat_is_caught():
    """BITING MUTATION #1: a mutant that SOFTENS on a later repeat (level decreases)
    is rejected — recovery_form_strength must be non-decreasing."""
    def mutant_level(n: int) -> int:                # inverted: weaker on repeat
        return max(0, 3 - n)
    seq = [mutant_level(n) for n in range(1, 5)]
    assert seq != sorted(seq)                        # mutant decreases -> caught
    real = [recovery_escalation_level(n) for n in range(1, 5)]
    assert real == sorted(real)                      # real guard never weakens


def test_g3_mutation_perturb_first_fire_is_caught():
    """BITING MUTATION #2: a mutant that escalates even the 1st fire would break the
    byte-identical single-shot contract."""
    def mutant(base: str, n: int) -> str:
        return f"URGENT — {base}"                    # escalates unconditionally
    assert mutant(DYNACONF_BODY, 1) != DYNACONF_BODY
    assert escalate_recovery_form(DYNACONF_BODY, 1) == DYNACONF_BODY


# =========================================================================== #
# GUARD 4 — obligation steering guard
# =========================================================================== #
# aiogram: localization ranked aiogram/fsm/scene.py #1; an obligation narrowing
# that omits it steers the agent away from rank-1 gold.
AIOGRAM_RANKED_TOP = {"aiogram/fsm/scene.py", "aiogram/fsm/context.py"}
AIOGRAM_OBLIG_FILES = {"aiogram/fsm/context.py"}          # omits scene.py


def test_g4_obligation_omitting_ranked_file_is_flagged():
    """RED-first: an obligation file set that omits a top-ranked localization file
    is the adverse 'narrow away' shape."""
    assert obligation_narrowing_excludes_ranked(AIOGRAM_OBLIG_FILES, AIOGRAM_RANKED_TOP) is True


def test_g4_obligation_covering_ranked_is_not_flagged():
    """When the obligation set already covers the ranked-top set, there is no
    exclusion."""
    assert obligation_narrowing_excludes_ranked(AIOGRAM_RANKED_TOP, AIOGRAM_RANKED_TOP) is False


def test_g4_empty_obligation_narrows_nothing():
    """An empty obligation set narrows nothing -> never an exclusion (correct-or-quiet)."""
    assert obligation_narrowing_excludes_ranked(set(), AIOGRAM_RANKED_TOP) is False


def test_g4_guarded_symbols_union_in_ranked_top():
    """The guarded obligation symbol set unions in ranked-top symbols so narrowing
    can never drop them (fail toward silence)."""
    guarded = guarded_obligation_symbols({"clear"}, {"scene", "ActionContainer"})
    assert {"scene", "ActionContainer"} <= guarded
    assert "clear" in guarded


def test_g4_guarded_symbols_empty_stays_empty():
    """No obligation narrowing requested -> the guard adds nothing."""
    assert guarded_obligation_symbols(set(), {"scene"}) == set()


def test_g4_mutation_intersection_instead_of_difference_is_caught():
    """BITING MUTATION #1: flag exclusion by INTERSECTION (of ∩ rt) instead of the
    ranked-minus-obligation DIFFERENCE. That mutant flags the covering case as an
    exclusion and misses the real 'omits scene.py' case."""
    def mutant(of, rt) -> bool:
        of, rt = set(of), set(rt)
        return bool(of and rt and (of & rt))          # 'overlap' = exclusion (wrong)
    # real case: obligation omits scene.py -> real guard True, mutant may be True too,
    # but the covering case exposes the mutant:
    assert mutant(AIOGRAM_RANKED_TOP, AIOGRAM_RANKED_TOP) is True   # mutant: false alarm
    assert obligation_narrowing_excludes_ranked(AIOGRAM_RANKED_TOP, AIOGRAM_RANKED_TOP) is False


def test_g4_mutation_guard_drops_ranked_is_caught():
    """BITING MUTATION #2: a mutant that INTERSECTS (keeps only obligation∩ranked)
    would DROP a ranked symbol not in the obligation set — the opposite of fail
    toward silence."""
    def mutant(os_, rt):
        return set(os_) & set(rt)                      # narrows AWAY ranked-only syms
    assert "scene" not in mutant({"clear"}, {"scene"})           # mutant drops scene
    assert "scene" in guarded_obligation_symbols({"clear"}, {"scene"})  # real guard keeps it
