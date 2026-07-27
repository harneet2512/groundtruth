"""Pin: the live-lite Collect step must HARVEST the canonical RuntimeJournal SQLite.

THE GAP (verified 2026-07-27): the canonical runtime's PRIMARY record is the RuntimeJournal
SQLite (delivery_journal / compilation_journal / oracle_journal / evidence_journal /
canonical_events / verified_snapshots), written in-container to
dirname(GT_RUNTIME_LEDGER)/gt_reasoning_runtime.sqlite3 (gt_mini_patch.py seam, RuntimeJournal
construction). With GT_RUNTIME_LEDGER=/gt_out/gt_runtime_ledger.jsonl in
swebench_live_lite_full.yml it lands at /gt_out = host /tmp/gt_out — inside the already-harvested
bind mount — yet the string `gt_reasoning_runtime` appeared NOWHERE in that workflow. The
oracle's whole decision history (READY..RESPONSE_COMMITTED, coalition inputs, compilation
failure codes) died with the container, and "0 canonical rows in the JSONL" was misread as
"the oracle released nothing" when the oracle's success path writes nothing to that JSONL.

THE READER CONTRACT (runtime_attestation.py `runtime_attestation_diagnostic`): it rglobs the
EXACT filename `gt_reasoning_runtime.sqlite3` under the graded task_dir, returns None when
absent, and FAILS CLOSED on more than one match. The graded task dirs are:
  1. $TASK_ARTIFACT_ROOT = /tmp/gt/<task>  (in-workflow gt_feature_metrics run_dir), and
  2. the uploaded trial_results/ tree      (offline /tmp/all/ll-full-<task> grading).
So the harvest must land exactly ONE copy per graded root, FILENAME UNCHANGED — the
deepswe_full.yml-style rename to gt_reasoning_runtime_<task>.sqlite3 is invisible to the
reader and therefore worthless here.

Style follows tests/test_mandatory_metrics_gate_pin_20260713.py (workflow-wiring pin by YAML
parse). Every assertion here is calibrated: the helpers first prove they can find KNOWN
harvest lines before claiming anything about this one.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[1]
_WF = _ROOT / ".github" / "workflows" / "swebench_live_lite_full.yml"
_READER = _ROOT / "src" / "groundtruth" / "runtime" / "runtime_attestation.py"

_JOURNAL = "gt_reasoning_runtime.sqlite3"
_SOURCE = f"/tmp/gt_out/{_JOURNAL}"


def _doc() -> dict:
    return yaml.safe_load(_WF.read_text(encoding="utf-8"))


def _all_steps() -> list[dict]:
    out: list[dict] = []
    for job in _doc().get("jobs", {}).values():
        for step in job.get("steps", []) or []:
            if isinstance(step, dict):
                out.append(step)
    return out


def _step_run_containing(token: str) -> str:
    for step in _all_steps():
        run = step.get("run")
        if run and token in run:
            return run
    raise AssertionError(f"no workflow step run contains {token!r}")


def _collect_run() -> str:
    # The Collect step, anchored on its distinctive mkdir (same anchor as the
    # mandatory-metrics pin; the eval step also creates trial_results).
    return _step_run_containing("mkdir -p trial_results trial_results/gt_artifacts")


def _code_lines(run: str) -> list[str]:
    # Exclude bash comments so a token appearing only in a comment cannot satisfy
    # a code-presence assertion.
    return [ln for ln in run.splitlines() if not ln.strip().startswith("#")]


# ── 0. instrument calibration: the parser DOES find known harvest lines ───────────────────────


def test_instrument_finds_known_harvest_lines() -> None:
    """Never report an absence with an instrument that cannot produce a presence."""
    code = "\n".join(_code_lines(_collect_run()))
    # Positive controls: three harvest copies known to exist in Collect today.
    assert 'cp /tmp/gt_out/gt_runtime_ledger.jsonl "$_GT_LEDGER_DEST"' in code
    assert (
        "cp /tmp/gt_out/gt_profile_receipt.json "
        "trial_results/gt_artifacts/gt_profile_receipt.json" in code
    )
    assert "gt_oracle_events.jsonl" in code


# ── 1. the reader contract this harvest must satisfy ──────────────────────────────────────────


def test_reader_rglobs_the_exact_unrenamed_filename() -> None:
    """Pin the reader side of the join: runtime_attestation_diagnostic looks for the EXACT
    basename. If this ever changes, the workflow destinations below must change with it."""
    src = _READER.read_text(encoding="utf-8")
    assert f'rglob("{_JOURNAL}")' in src, (
        "runtime_attestation_diagnostic no longer rglobs gt_reasoning_runtime.sqlite3 — "
        "re-verify the harvest destinations in swebench_live_lite_full.yml against the "
        "new reader contract"
    )


# ── 2. THE HARVEST: journal copied into BOTH graded task roots, filename unchanged ────────────


def _harvest_lines() -> list[str]:
    return [ln for ln in _code_lines(_collect_run()) if _SOURCE in ln and "cp " in ln]


def test_collect_harvests_canonical_journal_into_task_artifact_root() -> None:
    hits = [
        ln for ln in _harvest_lines()
        if f'"$TASK_ARTIFACT_ROOT/{_JOURNAL}"' in ln
    ]
    assert hits, (
        f"the Collect step must `cp {_SOURCE}` to $TASK_ARTIFACT_ROOT/{_JOURNAL} "
        "(the in-workflow gt_feature_metrics task_dir) with the FILENAME UNCHANGED — "
        "runtime_attestation_diagnostic rglobs that exact basename and a "
        "deepswe-style _<task> rename is invisible to it"
    )


def test_collect_harvests_canonical_journal_into_trial_results() -> None:
    hits = [
        ln for ln in _harvest_lines()
        if re.search(r"trial_results(?:/gt_artifacts)?/" + re.escape(_JOURNAL), ln)
    ]
    assert hits, (
        f"the Collect step must `cp {_SOURCE}` under trial_results/ (uploaded as "
        f"ll-full-<task> for offline grading) with the FILENAME UNCHANGED"
    )


def test_harvest_is_fail_soft_and_single_copy_per_root() -> None:
    lines = _harvest_lines()
    assert lines, "no canonical-journal harvest lines found at all"
    for ln in lines:
        assert "|| true" in ln or "2>/dev/null" in ln, (
            f"canonical-journal harvest must be fail-soft like its neighbours — a missing "
            f"journal on the baseline arm must never fail the run: {ln!r}"
        )
    # The reader FAILS CLOSED on >1 journal under one graded root: exactly one copy
    # may target $TASK_ARTIFACT_ROOT and exactly one may target trial_results.
    task_root = [ln for ln in lines if "$TASK_ARTIFACT_ROOT/" in ln]
    trial = [ln for ln in lines if "trial_results/" in ln]
    assert len(task_root) == 1, (
        f"exactly ONE copy may land under $TASK_ARTIFACT_ROOT (reader fails closed on "
        f"MULTIPLE_CANONICAL_JOURNALS); found {len(task_root)}"
    )
    assert len(trial) == 1, (
        f"exactly ONE copy may land under trial_results/ (reader fails closed on "
        f"MULTIPLE_CANONICAL_JOURNALS); found {len(trial)}"
    )


def test_harvest_runs_before_feature_metrics_grading() -> None:
    code = "\n".join(_code_lines(_collect_run()))
    assert _SOURCE in code, "canonical-journal harvest missing entirely"
    assert "gt_feature_metrics.py" in code
    assert code.index(_SOURCE) < code.index("gt_feature_metrics.py"), (
        "the journal must be in place under $TASK_ARTIFACT_ROOT BEFORE the in-workflow "
        "gt_feature_metrics grading pass reads that task_dir"
    )
