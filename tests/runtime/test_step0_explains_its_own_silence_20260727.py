r"""C3 — step-0 delivery must never be silently absent.

THE DEFECT. `_stage_initial_canonical_evidence` had TWO exits that produced no delivery and no
trace whatsoever:

    if not records:                       # <- silent: brief unreadable / stale bake / schema miss
        return
    ...
    if plan.delivery_attempt_id:          # <- no `else`: coalition failed, nothing said
        attachment.provider_boundary.stage(...)

Neither is wrong as BEHAVIOUR — both are correct-or-quiet. Both were fatal as OBSERVABILITY: a
step-0 that stayed quiet is indistinguishable downstream from a step-0 that delivered, so a run
could pay for 300 tasks with the brief disarmed and report exactly what a healthy run reports.

That is not hypothetical for the first branch. `_canonical_brief_records` returns `()` for a whole
family of conditions -- `brief.txt`/`brief_result.json` unreadable at `dirname(GT_BRIEF_FILE)`, a
schema that is not `gt.brief_result.v1`, `brief_text` not byte-equal to the sealed copy, no receipt
whose sha256 re-derives -- and the step-0 brief is BAKED into the substrate, so a stale bake
disarms step-0 while every other signal stays green.

WHAT THIS FILE ASSERTS: that all THREE outcomes (no records / no delivery / staged) leave a
self-describing `canonical_runtime.step0` row, and that the row is telemetry only. It does NOT
assert that step-0 delivers -- that is a product question, not an observability one, and forcing
it here would be the "weaken the bar to make the test pass" move the sibling compilation test
warns against.
"""

from __future__ import annotations

import inspect
import json

from artifact_deepswe import gt_mini_patch as seam


def _source() -> str:
    return inspect.getsource(seam._stage_initial_canonical_evidence)


# --------------------------------------------------------------------------- #
# BEHAVIOURAL. Everything below the divider is a source scrape, which can only
# prove the text is present -- not that the write is REACHED. This drives the
# branch for real and reads the durable sink.
# --------------------------------------------------------------------------- #
def test_the_no_records_branch_actually_WRITES_a_row(tmp_path, monkeypatch):
    """PLUMBING, end to end: call the real function, read the real JSONL sink.

    `attachment` is passed as None deliberately -- this branch must return before touching
    it, so None both keeps the fixture honest and proves the early exit really is early.
    """
    ledger = tmp_path / "runtime_ledger.jsonl"
    monkeypatch.setenv("GT_RUNTIME_LEDGER", str(ledger))

    seam._stage_initial_canonical_evidence(None, (), "the task text")

    assert ledger.exists(), "no durable ledger row was written at all"
    rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()]
    step0 = [r for r in rows if str(r.get("layer", "")) == "canonical_runtime.step0"]
    assert step0, f"no canonical_runtime.step0 row in the sink; got layers {[r.get('layer') for r in rows]}"
    row = step0[0]
    assert row.get("reason") == "no_brief_records"
    assert int(row.get("chars_delivered", -1)) == 0, "step-0 telemetry must ship zero bytes"
    # NO env-derived field is asserted here, on purpose. The first draft put GT_BRIEF_FILE on
    # this row to name the unreadable file; `test_r1_ae_parity_invariant_failclosed` caught it
    # because that variable is not --ae forwarded, so in-container it would resolve to "" and
    # the breadcrumb would look specific while pointing at nothing. The reason code carries the
    # signal; a phantom knob added for observability is still a phantom knob.
    assert "brief_file" not in row, (
        "an un-forwarded env read is back on this row; --ae parity will fail in artifact_deepswe"
    )


def test_the_no_records_branch_is_silent_when_the_sink_is_unwritable(tmp_path, monkeypatch):
    """NEGATIVE CONTROL for the guard: a broken telemetry sink must not raise into the agent
    loop. This branch runs BEFORE the function's own try/except, so an unguarded write here
    would turn an observability improvement into a task-start crash."""
    monkeypatch.setenv("GT_RUNTIME_LEDGER", str(tmp_path / "no" / "such" / "dir" / "x.jsonl"))
    seam._stage_initial_canonical_evidence(None, (), "the task text")  # must not raise


def test_positive_control_the_function_and_its_marker_exist():
    """Run FIRST. Every `in` assertion below is unreadable if the slice is empty or if the
    row marker were renamed -- a substring test that silently measures the wrong text is the
    failure mode this control exists to exclude."""
    src = _source()
    assert len(src) > 500, "source slice is implausibly small; assertions below prove nothing"
    assert src.count('kind="canonical_runtime.step0"') == 3, (
        "expected exactly three step0 rows (no-records / no-delivery / staged); found "
        f"{src.count('kind=\"canonical_runtime.step0\"')}"
    )


def test_the_no_records_exit_is_no_longer_silent():
    """The most dangerous branch: a stale or unreadable bake disarms step-0 completely."""
    src = _source()
    assert "no_brief_records" in src, (
        "the `if not records: return` exit writes no row; a disarmed step-0 is "
        "indistinguishable from a delivered one"
    )


def test_the_no_delivery_exit_is_no_longer_silent():
    """A coalition that fails to compose must say so, exactly as the per-observation path does."""
    src = _source()
    assert "else:" in src, "the delivery branch still has no else"
    assert "failure_code" in src, (
        "the no-delivery row does not carry the compilation failure code, so it explains "
        "nothing about WHY step-0 was quiet"
    )


def test_every_step0_row_names_the_unresolved_role_and_the_pool():
    """`unresolved_roles` is the discriminator between 'nothing was ingested' (a producer gap)
    and 'evidence existed and no required role was carried' (anchor starvation). Those are
    opposite diagnoses with opposite fixes, and a row without it explains nothing."""
    src = _source()
    for field in ("unresolved_roles", "evidence_store", "coalition_size", "held_evidence"):
        assert field in src, f"step0 rows do not carry {field}"


def test_the_held_contexts_are_named():
    """Only ONE decision context is staged at step 0. A brief carrying both localization
    (SOURCE_TARGET_SELECTION) and obligations (PATCH_CONSTRUCTION) therefore ships only the
    former, and obligations is held. That is a real, surprising property; naming the held
    contexts on the row is what keeps it a measurement instead of folklore."""
    assert "held_contexts" in _source()


def test_step0_rows_are_telemetry_only():
    """ANTI-REGRESSION. This must never ship bytes or change a delivery decision. Note
    `_runtime_ledger_record` downgrades a 0-byte "delivered" anyway, so claiming delivery here
    would be both wrong and silently rewritten."""
    src = _source()
    assert 'outcome="delivered"' not in src
    assert src.count('outcome="suppressed_internal_only"') == 3
    assert src.count("chars=0") == 3


def test_every_writer_is_self_guarded():
    """Telemetry never blocks the agent's turn: each row write sits in its own try/except.

    Load-bearing for the FIRST branch in particular -- it runs BEFORE the function's main
    try/except, so an unguarded write there would raise straight into the agent loop at task
    start, converting an observability improvement into a total failure.
    """
    src = _source()
    assert src.count("telemetry never blocks") == 3, (
        "a step0 telemetry write is not individually guarded"
    )


def test_the_stale_prepend_comment_is_gone_from_the_runner():
    """The runner's comment claimed `_resolve_task` prepends the brief. That prepend was
    deleted when delivery moved to the canonical runtime; the comment survived and contradicted
    the docstring 100 lines above it. A comment describing a deleted mechanism sends the next
    reader hunting for code that does not exist -- which is how step-0 got audited as dead."""
    import artifact_deepswe.gt_headless_runner as runner

    src = inspect.getsource(runner)
    assert "prepend the substrate brief onto the task" not in src, (
        "the stale STEP-0 prepend comment is back"
    )
    # And the replacement must state where delivery ACTUALLY happens, or the next reader is
    # merely un-misled rather than informed.
    assert "_stage_initial_canonical_evidence" in src
