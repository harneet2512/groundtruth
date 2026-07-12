"""Pin: the substrate closure gate must be STRICT and TRUTHFUL (readiness spec §1).

Regression guard for the 2026-07-12 fix of the two confirmed closure defects:
  * `gt_substrate_image.yml`'s post-pull closure ran `bash -lc` (no `set -e`); its
    `test -s .../e5-small-v2/model.onnx` failed SILENTLY (e5 removed 2026-06-20) and it
    still printed `verified portable (... gte+e5)` — a masked-failure false claim.
  * `Dockerfile.gt-substrate`'s self-test echo claimed `LSP: py/ts/go/rust/java; gte+e5`
    while testing neither java nor e5.

These pins bite on the exact strings that changed: they FAIL on the pre-fix text.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[1]
_WF = _ROOT / ".github" / "workflows" / "gt_substrate_image.yml"
_DOCKERFILE = _ROOT / "docker" / "Dockerfile.gt-substrate"


def _closure_run_block() -> str:
    doc = yaml.safe_load(_WF.read_text(encoding="utf-8"))
    for job in doc.get("jobs", {}).values():
        for step in job.get("steps", []) or []:
            if isinstance(step, dict) and "Confirm the published image" in (step.get("name") or ""):
                return step["run"]
    raise AssertionError("closure step 'Confirm the published image' not found")


def test_closure_step_is_strict() -> None:
    run = _closure_run_block()
    # the OUTER GHA shell AND the INNER container shell must both be strict
    assert "set -euo pipefail" in run, "closure inner shell must engage strict mode (fail on first missing dep)"


def test_closure_step_does_not_probe_removed_deps() -> None:
    run = _closure_run_block()
    # ban the PROBE COMMANDS/PATHS (a doc comment may still name the removed deps as context).
    assert "/opt/gt/models/e5-small-v2/model.onnx" not in run, \
        "e5 was removed 2026-06-20 — a silent-failing probe re-introduces the masked-failure class"
    assert "/opt/gt/bin/jdtls" not in run and "/jre/bin/java" not in run, \
        "java/jdtls removed — do not restore a probe for an out-of-contract dep"


def test_closure_receipt_is_truthful() -> None:
    run = _closure_run_block()
    # the truthful capability contract must name java/e5 as out-of-contract, never 'available'
    assert "not_in_benchmark_contract" in run, "java must be declared not_in_benchmark_contract"
    assert "not_in_runtime_contract" in run, "e5 must be declared not_in_runtime_contract"
    # the false 'verified portable (... java; ... gte+e5)' label must be gone
    assert not re.search(r"py/ts/go/rust/java", run), "closure label must not claim java LSP"
    assert "gte+e5" not in run, "closure label must not claim the e5 embedder"


def test_dockerfile_selftest_label_is_truthful() -> None:
    text = _DOCKERFILE.read_text(encoding="utf-8")
    # the specific stale self-test echo line must no longer claim java/e5
    assert "LSP: py/ts/go/rust/java" not in text, "Dockerfile self-test label must not claim java LSP"
    assert "gte+e5" not in text, "Dockerfile self-test label must not claim the e5 embedder"
