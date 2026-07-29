"""Every canonical STAGING site must leave a durable compilation row — P1-3.

THE DEFECT (codex audit 2026-07-29, confirmed by reading all three call sites):
``_stage_canonical_compilation`` has three seam callers —

  * the ordinary observation path: writes the ``canonical_runtime.compilation``
    SUCCESS mirror row ("Until 2026-07-27 only the FAILED branch wrote a row…"),
  * the step-0 path: writes its own ``canonical_runtime.step0`` row,
  * the PRECOMMIT path (direct-commitment batches — the seam gives the coalition
    one chance to stage already-computed evidence before the native action):
    writes NOTHING. A capsule staged at the submit-adjacent boundary is
    indistinguishable downstream from a compilation that never happened — the
    exact "silent outcome" defect the success-mirror row was written to kill,
    reappearing at the third site.

This pins the invariant at the SOURCE level (same technique as
``test_emittable_rung_has_a_writer_20260728``): every ``_stage_canonical_compilation(``
call site must be followed, within its own basic block, by a
``_runtime_ledger_record(`` whose kind starts with ``canonical_runtime.`` — the
durable explanation of what was staged there.
"""
from __future__ import annotations

import re
from pathlib import Path

_SEAM = Path(__file__).resolve().parents[2] / "artifact_deepswe" / "gt_mini_patch.py"

#: how far after the staging call a site's own explanation row must appear.
#: The ordinary site's row starts ~20 lines after the call; step-0's ~10.
_WINDOW_LINES = 90


def _staging_call_sites(source_lines: list[str]) -> list[int]:
    """0-based indices of every CALL of _stage_canonical_compilation (not the def)."""
    return [
        i
        for i, line in enumerate(source_lines)
        if "_stage_canonical_compilation(" in line
        and not line.lstrip().startswith("def ")
    ]


def test_every_staging_site_writes_a_canonical_row() -> None:
    source_lines = _SEAM.read_text(encoding="utf-8").splitlines()
    sites = _staging_call_sites(source_lines)
    assert len(sites) >= 3, f"expected >=3 staging call sites, found {len(sites)}"

    silent: list[int] = []
    for site in sites:
        window = "\n".join(source_lines[site: site + _WINDOW_LINES])
        if not re.search(r"_runtime_ledger_record\(", window):
            silent.append(site + 1)  # 1-based for the report
        elif not re.search(r"kind=\"canonical_runtime\.", window):
            silent.append(site + 1)
    assert not silent, (
        "staging site(s) at line(s) "
        f"{silent} stage a capsule and write NO canonical_runtime.* ledger row — "
        "a staged capsule there is indistinguishable from a compilation that "
        "never happened"
    )


def test_precommit_row_carries_the_site_discriminator() -> None:
    """The precommit site's row must be distinguishable from the ordinary
    observation-path row: one reader query over kind=canonical_runtime.compilation
    sees both, and 'staging_site' tells them apart."""
    source = _SEAM.read_text(encoding="utf-8")
    # the precommit site is the one staged under the direct-commitment branch,
    # identified by the ':precommit' observation id it builds just above.
    precommit_pos = source.find('observation_id=f"{proposing_model_call_id}:precommit"')
    assert precommit_pos != -1, "precommit staging block not found — source drifted"
    window = source[precommit_pos: precommit_pos + 6000]
    assert '"staging_site": "precommit"' in window, (
        "the precommit staging row must stamp staging_site=precommit so it is "
        "differencable from the ordinary observation-path success row"
    )


def test_commitment_plan_observer_writes_a_durable_row() -> None:
    """#54 part 1: the commitment boundary's plan observer is the ONLY place the
    withhold decision is visible host-side; run 30478454517's 2,421 withholds left
    zero telemetry. The observer must write a commitment_boundary.plan row with the
    decision + reason_code + deferred count."""
    source = _SEAM.read_text(encoding="utf-8")
    anchor = source.find("def _observe_commitment_plan(")
    assert anchor != -1
    window = source[anchor: anchor + 4000]
    assert 'kind="commitment_boundary.plan"' in window, (
        "the plan observer writes no ledger row — the withhold loop is invisible"
    )
    for field in ('"decision"', '"reason_code"', '"deferred_actions"',
                  '"qualifying_evidence_ids"'):
        assert field in window, f"plan row missing {field}"
