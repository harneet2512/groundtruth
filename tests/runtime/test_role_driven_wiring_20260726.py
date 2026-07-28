"""Wiring guard: the lever must reach BOTH stages, resolved once at the seam.

Three separate things can silently break and each looks identical to working:

1. the seam never reads the environment, so the runtime is always default;
2. the runtime holds the flag but forwards it to only ONE of the two stages -- evidence is
   then HELD by the temporal gate and the composer's role logic never runs;
3. the default flips, changing production behaviour without anyone asking for it.

The pipeline tests in ``test_role_driven_temporal_gate_20260726.py`` pass ``role_driven=``
by hand, so they prove the STAGES agree. They cannot prove the SEAM supplies the value --
exactly the gap that made ``viewed_files`` dead in production while its component suite was
green. These tests assert the wiring itself.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import yaml

import pytest

from artifact_deepswe import gt_mini_patch as seam
from groundtruth.runtime import reasoning_runtime as rr


def test_runtime_defaults_to_partitioned_eligibility():
    """Byte-identical default: nobody opts in by accident."""
    signature = inspect.signature(rr.AttemptReasoningRuntime.__init__)
    assert signature.parameters["role_driven_coalition"].default is False


@pytest.mark.parametrize(
    "fn",
    [
        rr.evaluate_feature_contract,
        rr.select_evidence_coalition,
        rr.compile_observation_capsule,
    ],
)
def test_every_stage_defaults_to_partitioned_eligibility(fn):
    """Each stage must default OFF independently.

    Added because a mutation flipping ``compile_observation_capsule``'s signature default to
    True SURVIVED: every test passes ``role_driven=`` explicitly, so no default was ever
    exercised. A stage whose default silently flipped would opt production in without anyone
    setting the environment variable.
    """
    assert inspect.signature(fn).parameters["role_driven"].default is False


def test_env_reader_defaults_off_and_only_accepts_one():
    assert rr.role_driven_coalition_enabled({}) is False
    assert rr.role_driven_coalition_enabled({"GT_ROLE_DRIVEN_COALITION": "0"}) is False
    assert rr.role_driven_coalition_enabled({"GT_ROLE_DRIVEN_COALITION": "true"}) is False
    assert rr.role_driven_coalition_enabled({"GT_ROLE_DRIVEN_COALITION": "1"}) is True
    assert rr.role_driven_coalition_enabled({"GT_ROLE_DRIVEN_COALITION": " 1 "}) is True


def test_env_reader_reads_the_real_process_environment(monkeypatch):
    monkeypatch.delenv("GT_ROLE_DRIVEN_COALITION", raising=False)
    assert rr.role_driven_coalition_enabled() is False
    monkeypatch.setenv("GT_ROLE_DRIVEN_COALITION", "1")
    assert rr.role_driven_coalition_enabled() is True


def _call_sites(source: str, symbol: str) -> list[str]:
    """Every call to ``symbol`` in ``source``, sliced to its closing paren.

    Returns ALL sites, not the first: slicing to the first match is how the compiler guard
    below was once vacuous (it landed on the ``enabled=False`` disabled-plan call).
    """
    sites, start = [], 0
    while True:
        found = source.find(symbol, start)
        if found == -1:
            return sites
        depth, i = 0, found + len(symbol) - 1
        while i < len(source):
            if source[i] == "(":
                depth += 1
            elif source[i] == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        sites.append(source[found : i + 1])
        start = i + 1


def _runtime_source() -> str:
    return inspect.getsource(rr.AttemptReasoningRuntime)


def test_runtime_forwards_the_flag_to_the_temporal_gate():
    """Stage 1. If this is dropped, evidence is HELD before the composer ever sees it."""
    for call in _call_sites(_runtime_source(), "evaluate_feature_contract("):
        assert "role_driven=self.role_driven_coalition" in call, call


def test_runtime_forwards_the_flag_to_the_coalition_composer():
    """Stage 2."""
    for call in _call_sites(_runtime_source(), "select_evidence_coalition("):
        assert "role_driven=self.role_driven_coalition" in call, call


def test_runtime_forwards_the_flag_to_the_capsule_compiler():
    """Stage 3, found the hard way.

    ``compile_observation_capsule`` held a THIRD copy of the provenance-context comparison.
    With only the gate and composer updated, an admitted out-of-context record reached the
    compiler and the whole capsule failed MIXED_DECISION_CONTEXT -- so the change looked
    wired at two stages and still delivered nothing. Only driving the installed path end to
    end surfaced it.

    Checked over EVERY call site rather than the first one. An earlier version of this test
    sliced from the first ``compile_observation_capsule(`` in the class -- which is the
    ``enabled=False`` disabled-plan call, where the lever is inert -- so the named guard was
    vacuous while appearing to pass. An adversarial review caught that.
    """
    for call in _call_sites(_runtime_source(), "compile_observation_capsule("):
        assert "role_driven=self.role_driven_coalition" in call, (
            f"a compile call site does not forward the lever:\n{call}"
        )


def test_every_stage_receives_the_same_value():
    """All stages must agree -- a mismatch anywhere is the silent-nothing failure.

    Counted rather than named so a NEW stage added without the flag fails here instead of
    shipping a capsule that half the pipeline disagrees about. If this count changes,
    confirm the new call site genuinely forwards it before updating the number.
    """
    source = _runtime_source()
    forwards = source.count("role_driven=self.role_driven_coalition")
    stages = (
        source.count("evaluate_feature_contract(")
        + source.count("select_evidence_coalition(")
        + source.count("compile_observation_capsule(")
        # `_evaluate_current_decision_contract` ADDED to the census 2026-07-28. The C11
        # per-record commitment-window change moved the runtime's temporal-gate call BEHIND
        # this module-level helper (it evaluates twice, NOT_OPEN then OPEN). `_runtime_source`
        # is `inspect.getsource(AttemptReasoningRuntime)`, so the helper's own
        # `evaluate_feature_contract(` calls live OUTSIDE the counted class -- leaving a
        # forward at the call site with no matching in-class stage token, i.e. 4 forwards
        # vs 3 stages.
        #
        # The invariant is unchanged and still bites: it is an eligibility-sensitive call
        # site, it must forward the lever, and a NEW stage added without the flag still
        # fails here. Only the token list had to learn about the indirection. Counting it
        # is not weakening -- omitting it would UNDER-count stages and could let a future
        # unforwarded call site hide inside the discrepancy.
        + source.count("_evaluate_current_decision_contract(")
    )
    assert forwards == stages, (
        f"{stages} eligibility-sensitive call sites but only {forwards} forward the lever"
    )


def test_seam_resolves_the_flag_from_the_environment():
    """The seam is the ONE place the environment is read, keeping the runtime pure."""
    source = inspect.getsource(seam.install_canonical_runtime)
    assert "role_driven_coalition=role_driven_coalition_enabled(env)" in source


def test_runtime_stores_the_flag_as_a_bool():
    runtime_init = inspect.getsource(rr.AttemptReasoningRuntime.__init__)
    assert "self.role_driven_coalition = bool(role_driven_coalition)" in runtime_init


def test_neither_stage_reads_the_environment_itself():
    """Purity (architecture item 8): replay must not depend on ambient process state."""
    for fn in (rr.evaluate_feature_contract, rr.select_evidence_coalition):
        body = inspect.getsource(fn)
        assert "os.environ" not in body, f"{fn.__name__} reads the environment"
        assert "GT_ROLE_DRIVEN_COALITION" not in body


def test_seam_commitment_evidence_honours_role_driven_eligibility():
    """Stage 4 -- the SEAM's own provenance filter, found by adversarial review.

    ``CanonicalRuntimeAttachment._commitment_context`` builds ``CommitmentEvidence`` and
    filtered it on ``record.decision_context is active.context``. That feeds
    ``commitment_control._qualifying_evidence``, which decides whether GT DEFERS the agent's
    commitment so it sees evidence before acting. Under role-driven eligibility, evidence GT
    had compiled, released and DELIVERED for this decision was invisible to that gate -- so
    GT could never hold a commitment on account of what it had just shown the model.

    Fifth copy of the same provenance comparison in the pipeline. Failure direction is
    under-intervention rather than leakage, which is the conservative side, but it silently
    negated the lever for the entire verify-before-submit surface.
    """
    source = inspect.getsource(seam.CanonicalRuntimeAttachment._commitment_context)
    assert "record.decision_context is active.context" in source, (
        "the provenance comparison moved; re-verify this guard still covers it"
    )
    assert "self.attempt_runtime.role_driven_coalition" in source, (
        "the seam's CommitmentEvidence filter does not honour role-driven eligibility"
    )


# ---------------------------------------------------------------------------------------
# Stage 5 -- the DISPATCH path. Everything above proves the lever is honoured once it is
# inside the process. These prove it can get there at all: a lever that the workflow never
# sets, or that gt_ae_block.sh never forwards into the container, is off in production no
# matter how correct the runtime is. That is the same gap that made `viewed_files` dead.
# ---------------------------------------------------------------------------------------
_REPO = Path(__file__).resolve().parents[2]


def test_workflow_enables_the_lever():
    parsed = yaml.safe_load(
        (_REPO / ".github" / "workflows" / "deepswe_full.yml").read_text(encoding="utf-8")
    )
    assert str(parsed["env"]["GT_ROLE_DRIVEN_COALITION"]) == "1", (
        "the lever is not enabled in the job environment"
    )


def test_workflow_stays_within_githubs_dispatch_input_cap():
    """GitHub rejects a workflow with more than 25 workflow_dispatch inputs.

    Learned the hard way: adding the lever as a 26th input made the ENTIRE workflow
    undispatchable with HTTP 422 "failed to parse workflow" -- not just the new input, every
    run. The lever is therefore a literal env value, not an input. If you need it switchable
    at dispatch time, you must REMOVE another input first.
    """
    parsed = yaml.safe_load(
        (_REPO / ".github" / "workflows" / "deepswe_full.yml").read_text(encoding="utf-8")
    )
    dispatch_key = True if True in parsed else "on"
    inputs = parsed[dispatch_key]["workflow_dispatch"]["inputs"]
    assert len(inputs) <= 25, (
        f"{len(inputs)} workflow_dispatch inputs; GitHub's hard cap is 25 and exceeding it "
        "makes the whole workflow undispatchable"
    )


def test_lever_is_not_bundled_with_the_other_delivery_levers():
    """It must stay its OWN input.

    Bundling it with ``new_delivery_levers`` would make a null result ambiguous between
    "the oracle delivered nothing" and "one of eleven bundled levers interfered" -- the same
    reasoning the workflow already applies to GT_MULTIDOSE and GT_BOUNDARY_EXPIRE.
    """
    parsed = yaml.safe_load(
        (_REPO / ".github" / "workflows" / "deepswe_full.yml").read_text(encoding="utf-8")
    )
    assert "new_delivery_levers" not in str(parsed["env"]["GT_ROLE_DRIVEN_COALITION"])


def test_ae_block_forwards_the_lever_into_the_container():
    """Without this line the container never sees it, whatever the workflow sets."""
    ae_block = (
        _REPO / "artifact_deepswe" / "gt_integration" / "gt_ae_block.sh"
    ).read_text(encoding="utf-8")
    assert '--ae "GT_ROLE_DRIVEN_COALITION=${GT_ROLE_DRIVEN_COALITION:-0}"' in ae_block, (
        "gt_ae_block.sh does not forward the lever into the container"
    )


# ---------------------------------------------------------------------------------------
# LIVE-LITE is the path that can actually run the oracle.
#
# It drives the agent via `gt_headless_runner.py`, which calls install_canonical_runtime
# (:571) and FAIL-CLOSES before spend if the attachment is unproven. deepswe_full drives the
# agent via pier and never installs the runtime at all.
#
# But the runtime attaching is not enough: without GT_ROLE_DRIVEN_COALITION it observes,
# reduces, produces evidence and ships NOTHING. Both a job-env value AND a `docker run -e`
# forward are required — the env alone never crosses the container boundary, and the -e alone
# resolves to its `:-0` default. Either one missing = a silent zero-delivery oracle run.
# ---------------------------------------------------------------------------------------
_LIVE_LITE = _REPO / ".github" / "workflows" / "swebench_live_lite_full.yml"


def test_live_lite_sets_the_lever_in_the_trial_job_env():
    parsed = yaml.safe_load(_LIVE_LITE.read_text(encoding="utf-8"))
    envs = {
        name: job["env"]
        for name, job in parsed["jobs"].items()
        if isinstance(job, dict) and isinstance(job.get("env"), dict)
    }
    setters = {n: e["GT_ROLE_DRIVEN_COALITION"] for n, e in envs.items()
               if "GT_ROLE_DRIVEN_COALITION" in e}
    assert setters, "no job sets GT_ROLE_DRIVEN_COALITION — the oracle will ship nothing"
    assert all(str(v) == "1" for v in setters.values()), (
        f"the lever is declared but not enabled: {setters}"
    )


def test_live_lite_forwards_the_lever_into_the_container():
    """The job env is host-side; the agent runs in `docker run`. Both are required."""
    text = _LIVE_LITE.read_text(encoding="utf-8")
    assert '-e GT_ROLE_DRIVEN_COALITION="${GT_ROLE_DRIVEN_COALITION:-0}"' in text, (
        "the docker run env list does not forward the lever; the container would fall back "
        "to the :-0 default and the oracle would release nothing"
    )


def test_live_lite_uses_the_headless_runner_not_pier():
    """The canonical runtime is installed by gt_headless_runner, nowhere else.

    If this path ever switches to `pier --agent-import-path`, install_canonical_runtime stops
    being called and the oracle silently disappears — exactly what happened on deepswe_full.
    """
    text = _LIVE_LITE.read_text(encoding="utf-8")
    assert "gt_headless_runner.py" in text
    runner = (_REPO / "artifact_deepswe" / "gt_headless_runner.py").read_text(encoding="utf-8")
    assert "install_canonical_runtime(" in runner, (
        "the headless runner no longer installs the canonical runtime"
    )
