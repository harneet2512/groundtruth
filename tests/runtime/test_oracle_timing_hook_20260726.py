"""The ORACLE HOOK — GT's timing authority must be attached, not just its byte hook.

WHY THIS FILE EXISTS.  GT has two independent hooks and they answer different questions:

  _PATCHED_CLASSES        env `execute`  -> "can GT SEE actions and APPEND bytes at all?"
  _PATCHED_AGENT_CLASSES  agent `run`    -> "does GT have a TIMING AUTHORITY for those bytes?"

The second is the product. gt_oracle exists to reconstruct the agent's reasoning state, track
the ONE open decision, hold evidence while it is not yet relevant, and RELEASE IT BEFORE THAT
DECISION IS COMMITTED. Delivery without it is a producer firing on its own trigger and a
static phase table guessing admissibility -- the design that produced 137/138 `wrong_phase`
suppressions and made "you have not verified this edit" admissible only AFTER verifying.

THE FAILURE MODE THIS GUARDS.  With the env hook attached and the agent hook missing, GT
still delivers, the ledger still shows `delivered` rows with chars>0, and every dashboard
looks healthy -- there is simply no decision-timing behind any of it. That was the real state
of every pier-driven run: measured on run 30225435976, 95 delivered payloads and ZERO
canonical/capsule ledger rows.

ROOT CAUSE it encodes: `install_canonical_runtime` documents its own precondition --
"Construction occurs only after the runner has concrete model and agent instances and before
``agent.run``" -- because the oracle releases through the PROVIDER boundary, which wraps the
MODEL. The env hook never yields a model handle. Only `gt_headless_runner` satisfied the
precondition, so on harnesses that construct the agent themselves (pier
`--agent-import-path artifact_deepswe.gt_agent:GTMiniSweAgent`, i.e. deepswe_full) the
attachment stayed None forever and `_augment_output` took its legacy branch every turn.

If a future change removes the agent loop from `_install`, these tests fail. Do not "fix"
them by asserting on the env hook -- that is the exact substitution that hid this for months.
"""

from __future__ import annotations

import inspect

import pytest

from artifact_deepswe import gt_mini_patch as seam


def test_agent_classes_are_declared():
    """The oracle needs an AGENT hook; the env hook alone cannot reach the model."""
    assert seam._AGENT_CLASSES, "no agent classes declared -- the oracle can never attach"
    mods = {m for m, _ in seam._AGENT_CLASSES}
    assert "minisweagent.agents.default" in mods, (
        "DefaultAgent is the class pier constructs; without it the benchmark path has no "
        "timing authority"
    )


def test_install_wraps_agent_run_not_only_env_execute():
    """`_install` must run BOTH loops. Source-level because the env hook alone looks healthy."""
    src = inspect.getsource(seam._install)
    assert "_ENV_CLASSES" in src, "env hook loop vanished"
    assert "_AGENT_CLASSES" in src, (
        "the ORACLE hook loop is gone -- GT will deliver with no timing authority and every "
        "delivery metric will still look green"
    )
    assert "_wrap_agent_run" in src


def test_wrapper_installs_the_canonical_runtime_before_the_step_loop():
    """It must install BEFORE delegating, and pass the model -- the provider boundary needs it."""
    src = inspect.getsource(seam._wrap_agent_run)
    assert "install_canonical_runtime" in src
    assert 'getattr(self, "model", None)' in src, (
        "the model handle is the whole point: the oracle releases through the provider "
        "boundary, which wraps the model"
    )
    # install must precede the delegation to the original run
    assert src.index("install_canonical_runtime") < src.index("return orig("), (
        "the runtime must be installed BEFORE the agent's step loop starts"
    )


def test_wrapper_is_correct_or_quiet_and_never_breaks_the_agent():
    """A failed install must leave the native path intact, never raise into the agent."""
    src = inspect.getsource(seam._wrap_agent_run)
    assert "except Exception" in src
    assert "return orig(" in src, "the native agent call must always happen"


def test_wrapper_respects_the_baseline_arm():
    """GT_BASELINE=1 is the control arm: no attachment, byte-identical."""
    assert "_GT_BASELINE" in inspect.getsource(seam._wrap_agent_run)


def test_wrapper_is_idempotent_per_process():
    """One attempt, one runtime. Re-entering run must not rebuild the attachment."""
    src = inspect.getsource(seam._wrap_agent_run)
    assert "_CANONICAL_RUNTIME_ATTACHMENT is None" in src


def test_patched_agent_classes_is_tracked_separately_from_env_classes():
    """Two hooks, two questions. Collapsing them re-hides the defect."""
    assert isinstance(seam._PATCHED_AGENT_CLASSES, list)
    assert seam._PATCHED_AGENT_CLASSES is not seam._PATCHED_CLASSES


def test_the_oracle_hook_actually_attaches_to_the_real_agent_class():
    """Behavioural proof, not source inspection -- the env hook was 'attached' for months
    while the oracle was absent, so attachment must be observed on the real class."""
    default = pytest.importorskip("minisweagent.agents.default")
    assert getattr(default.DefaultAgent, "_gt_oracle_patched", False), (
        "gt_mini_patch imported but DefaultAgent.run carries no oracle hook"
    )
    assert default.DefaultAgent.run.__qualname__.startswith("_wrap_agent_run"), (
        f"DefaultAgent.run is {default.DefaultAgent.run.__qualname__}, not the oracle wrapper"
    )
    assert "minisweagent.agents.default.DefaultAgent" in seam._PATCHED_AGENT_CLASSES
