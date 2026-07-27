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


def test_agent_targets_are_found_by_capability_not_only_by_name():
    """Naming the class is the wrong mechanism and it failed twice.

    The benchmark harness constructs pier.agents.installed.mini_swe_agent.MiniSweAgent;
    minisweagent.agents.default.DefaultAgent is not even in its MRO. A hardcoded list
    therefore patched a class the harness never instantiates -- and the earlier version of
    THIS test asserted on that same hardcoded name, so it passed while proving nothing.

    What actually identifies an agent we can hook is a testable property: it owns the step
    loop (defines `run`) AND owns a model (`model` in __init__), because the provider
    boundary rebinds model._query / model.query / agent.add_messages on INSTANCES.
    """
    assert seam._AGENT_CLASSES, "no named fallbacks"
    discovered = seam._discover_agent_classes()
    assert isinstance(discovered, list)
    for mod, cls in discovered:
        obj = getattr(__import__(mod, fromlist=[cls]), cls)
        assert "run" in obj.__dict__, f"{mod}.{cls} discovered without owning run()"
        import inspect as _i
        assert "model" in _i.signature(obj.__init__).parameters


def test_discovery_rejects_a_launcher_that_owns_no_model():
    """pier's MiniSweAgent is a LAUNCHER: it installs mini-swe into the container and the
    real agent+model live in a separate in-container process. It takes no `model`, so it
    must NOT be selected -- wrapping it could never give the provider boundary an instance."""
    pier = pytest.importorskip("pier.agents.installed.mini_swe_agent")
    import inspect as _i

    assert "model" not in _i.signature(pier.MiniSweAgent.__init__).parameters
    assert ("pier.agents.installed.mini_swe_agent", "MiniSweAgent") not in         seam._discover_agent_classes()


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


def test_every_discovered_agent_class_actually_carries_the_hook():
    """Behavioural, and NOT anchored to a name I picked.

    Asserts the hook is on EVERY class discovery selected, whatever those are in this
    install. A local pass here still does not prove the in-container class was patched --
    that is what `patched_agent_classes` in gt_profile_receipt.json exists to record, and
    it is the only evidence that settles it for a real run.
    """
    if not seam._PATCHED_AGENT_CLASSES:
        pytest.skip("no agent class importable in this environment")
    for dotted in seam._PATCHED_AGENT_CLASSES:
        mod, _, cls = dotted.rpartition(".")
        obj = getattr(__import__(mod, fromlist=[cls]), cls)
        assert getattr(obj, "_gt_oracle_patched", False), f"{dotted} lost the hook"
        assert obj.run.__qualname__.startswith("_wrap_agent_run"), (
            f"{dotted}.run is {obj.run.__qualname__}, not the oracle wrapper"
        )


def test_profile_receipt_records_oracle_activation():
    """The receipt is the POSITIVE CONTROL for "did GT have a timing authority?".

    `patched_classes` alone answers only "can GT append bytes". A process can be non-empty
    there and EMPTY on the agent hook — the state every pier-driven run was in: 95 delivered
    payloads, zero canonical rows, every metric green, no decision-timing behind any of it.

    Recording it makes attachment a FACT in the artifact rather than an inference from absent
    canonical ledger rows, which is ambiguous between "never attached" and "attached but
    never compiled". `role_driven_coalition` matters too: the runtime can attach and still
    release nothing when the lever is off.
    """
    src = inspect.getsource(seam._write_profile_receipt)
    assert '"patched_agent_classes": _PATCHED_AGENT_CLASSES' in src
    assert '"canonical_runtime_attached"' in src
    assert '"role_driven_coalition"' in src


def test_receipt_reports_attachment_as_a_bool_not_a_truthy_object():
    """A receipt must serialise; an attachment object would break json.dump or leak state."""
    src = inspect.getsource(seam._write_profile_receipt)
    assert "bool(_CANONICAL_RUNTIME_ATTACHMENT is not None)" in src


def test_the_import_time_receipt_value_is_dead_by_construction():
    """WHY the rewrite below has to exist, stated as a fact about call ordering.

    `_write_profile_receipt()` is the last statement of `_install()`, which runs at module
    IMPORT. `_CANONICAL_RUNTIME_ATTACHMENT` is only ever assigned inside `agent.run`. So the
    receipt written at import can only ever record `canonical_runtime_attached: false` --
    on a healthy run too. Reading that field from an artifact without the rewrite would be a
    measurement error, not a finding.
    """
    install_src = inspect.getsource(seam._install)
    assert "_write_profile_receipt()" in install_src
    assert "_CANONICAL_RUNTIME_ATTACHMENT" not in install_src, (
        "if _install now sets the attachment, this test's premise is stale -- recheck "
        "whether the import-time receipt is still dead"
    )
    assert "_CANONICAL_RUNTIME_ATTACHMENT" in inspect.getsource(seam._wrap_agent_run)


def test_receipt_is_rewritten_after_the_install_attempt(monkeypatch):
    """Behavioural: running the wrapped agent must refresh the receipt.

    Driven through the real wrapper with a stub install, so it fails if the rewrite is
    deleted, moved before the install, or made conditional on success.
    """
    calls: list[str] = []
    monkeypatch.setattr(seam, "_GT_BASELINE", False, raising=False)
    monkeypatch.setattr(seam, "_CANONICAL_RUNTIME_ATTACHMENT", None, raising=False)
    monkeypatch.setattr(seam, "_write_profile_receipt", lambda *a, **k: calls.append("receipt"))
    monkeypatch.setattr(seam, "_runtime_ledger_record", lambda **k: calls.append("ledger"))
    monkeypatch.setattr(
        seam, "install_canonical_runtime", lambda **k: calls.append("install") or None
    )

    class _Agent:
        model = object()

        def run(self, task="", **kw):
            calls.append("orig")
            return {"exit_status": "Submitted"}

    _Agent.run = seam._wrap_agent_run(_Agent.run)
    _Agent().run(task="t")

    assert calls.index("install") < calls.index("receipt"), (
        "the receipt is refreshed before the install attempt -- it would record the same "
        "dead import-time value"
    )
    assert calls.index("receipt") < calls.index("orig"), (
        "the receipt must be refreshed before the step loop; a crashed run would otherwise "
        "leave only the dead import-time value on disk"
    )


def test_receipt_refresh_is_written_even_when_the_install_fails(monkeypatch):
    """The failure case is the one worth recording. A receipt refreshed only on success
    makes `canonical_runtime_attached: false` ambiguous between 'never ran' and 'failed'."""
    calls: list[str] = []
    monkeypatch.setattr(seam, "_GT_BASELINE", False, raising=False)
    monkeypatch.setattr(seam, "_CANONICAL_RUNTIME_ATTACHMENT", None, raising=False)
    monkeypatch.setattr(seam, "_write_profile_receipt", lambda *a, **k: calls.append("receipt"))
    monkeypatch.setattr(seam, "_runtime_ledger_record", lambda **k: calls.append("ledger"))

    def _boom(**k):
        raise RuntimeError("attach exploded")

    monkeypatch.setattr(seam, "install_canonical_runtime", _boom)

    class _Agent:
        model = object()

        def run(self, task="", **kw):
            calls.append("orig")
            return {}

    _Agent.run = seam._wrap_agent_run(_Agent.run)
    _Agent().run(task="t")

    assert "receipt" in calls, "no receipt refresh on the install-failure path"
    assert "ledger" in calls, "no install-outcome ledger row on the failure path"
    assert "orig" in calls, "a failed install must not stop the agent (correct-or-quiet)"
