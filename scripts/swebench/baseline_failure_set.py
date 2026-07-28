#!/usr/bin/env python3
"""The pre-dispatch baseline failure SET — by NAME, per suite, diffed both ways.

WHY THIS EXISTS. The pre-dispatch audit requires "seam failure-SET vs baseline". Until now that
leg had NO executable and NO stored set: the baselines were carried in a human's head and in
session prose, so the check could only ever be done by hand and would be silently skipped the
first time someone was in a hurry. A leg that exists only in prose is a leg that does not exist.

BY NAME, NEVER BY COUNT. A count matches while the SET has changed underneath it: one new failure
plus one accidental fix reads as "still 2F" and both are invisible. Every comparison here is over
node-id sets.

BOTH DIRECTIONS ARE REPORTED. A test that unexpectedly PASSES is not good news to swallow -- it
means the baseline is stale, or a real defect was fixed silently, or the test stopped exercising
what it claims. Either way the set must be updated deliberately, not drifted into.

THIS IS NOT AN ALLOW-LIST. Nothing here suppresses, xfails, or skips anything. It records what is
KNOWN-RED at a named commit so that a NEW red is detectable in one command. Adding a name here is
an admission, and it should be made with a reason attached.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Iterable

# Recorded on the ss-diagnosis-128 branch, verified BY NAME on every suite run through the
# 2026-07-28 session. Each entry is a pytest node id exactly as pytest prints it after FAILED.
BASELINE: dict[str, frozenset[str]] = {
    "tests/runtime": frozenset({
        "tests/runtime/test_edit_overlay.py::test_capability_matrix_static_honesty",
        "tests/runtime/test_edit_overlay.py::test_differential_unprovable_baseline_never_attributed",
    }),
    "tests/swebench": frozenset({
        "tests/swebench/test_cluster2b_def_partition_obligations_attestation_20260717.py"
        "::test_collect_task_def_partition_correct_info_true",
        "tests/swebench/test_verifier_truth_plumbing_20260715.py"
        "::test_deep_and_standalone_performance_share_canonical_verifier_truth",
    }),
    "tests/pretask": frozenset({
        "tests/pretask/test_c1_passage_window_status.py::test_off_is_safe_default_128_32",
        "tests/pretask/test_c1_passage_window_status.py::test_on_witnesses_wide_256_16",
        "tests/pretask/test_c1_passage_window_status.py::test_scope_note_names_c2_b2",
        "tests/pretask/test_c1_passage_window_status.py::test_deterministic",
        "tests/pretask/test_c1_passage_window_status.py::test_e5_preserved_when_model_given",
        "tests/pretask/test_localization_vnext_shadow.py"
        "::test_live_brief_text_and_localization_proof_are_byte_identical",
        "tests/pretask/test_traces.py::test_traces_python",
        "tests/pretask/test_traces.py::test_traces_in_repo_filter",
        "tests/pretask/test_traces.py::test_traces_javascript",
        "tests/pretask/test_traces.py::test_traces_javascript_order_and_vendor_filter",
        "tests/pretask/test_v1r_brief.py::test_top_functions_returns_by_ref_count",
        "tests/pretask/test_v1r_crosslang_disqualifier.py"
        "::test_host_legacy_schema_without_language_is_permissive",
        "tests/pretask/test_v7_4_brief.py::test_item19_path_component_max_normalized_to_one",
    }),
    "artifact_deepswe": frozenset({
        "artifact_deepswe/tests/test_listen_import_invariant.py"
        "::test_source_references_no_benchmark_labels",
        "artifact_deepswe/tests/test_parent_opportunity_binding_20260715.py"
        "::test_each_delivered_fire_carries_its_exact_observation_and_opportunity",
        "artifact_deepswe/tests/test_parent_opportunity_binding_20260715.py"
        "::test_eligibility_ruling_binds_same_typed_candidate_before_delivery",
        "artifact_deepswe/tests/test_parent_opportunity_binding_20260715.py"
        "::test_inseam_metrics_flag_changes_no_model_visible_byte",
        "artifact_deepswe/tests/test_parent_opportunity_binding_20260715.py"
        "::test_opportunity_instrumentation_fault_cannot_change_visible_bytes",
        "artifact_deepswe/tests/test_parent_opportunity_binding_20260715.py"
        "::test_parallel_candidates_bind_exact_parent_without_persisting_prose",
        "artifact_deepswe/tests/test_producer_dispatch_reachability_20260724.py"
        "::test_each_agent_action_reaches_its_own_producer",
        "artifact_deepswe/tests/test_sm10_recovery_timing_20260712.py"
        "::test_recovery_candidate_producer",
        "artifact_deepswe/tests/test_sm6_gateway_byte_owner_20260711.py"
        "::test_cochange_is_internal_no_native_form",
        "artifact_deepswe/tests/test_sm6_gateway_byte_owner_20260711.py"
        "::test_only_the_three_declared_classes_may_retire",
        "artifact_deepswe/tests/test_ss0_provenance_late_ack_20260713.py"
        "::test_gateway_provenance_collapses_same_call_clean_key_before_pool",
        "artifact_deepswe/tests/test_ss2_seam_exec_truth_20260713.py"
        "::test_flag_on_all_phantom_is_empty_kills_false_claim",
    }),
}

_FAILED_RE = re.compile(r"^(?:FAILED|ERROR)\s+(\S+?)(?:\s+-\s+.*)?$")


def parse_failures(text: str) -> frozenset[str]:
    """Node ids from pytest's short summary. Duplicates collapse -- pytest prints a name once
    per failing phase (setup/call/teardown) and three phases of one test are one failure."""
    out: set[str] = set()
    for line in (text or "").splitlines():
        match = _FAILED_RE.match(line.strip())
        if match:
            out.add(match.group(1))
    return frozenset(out)


def diff(suite: str, observed: Iterable[str]) -> dict[str, list[str]]:
    """Both directions. `new_failures` is a regression; `unexpected_passes` is a STALE BASELINE,
    which is a finding in its own right and never something to quietly absorb."""
    expected = BASELINE.get(suite, frozenset())
    seen = frozenset(observed)
    return {
        "suite": suite,
        "new_failures": sorted(seen - expected),
        "unexpected_passes": sorted(expected - seen),
        "matched": sorted(seen & expected),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Diff a pytest run's failure SET against the recorded baseline, by name."
    )
    parser.add_argument("suite", choices=sorted(BASELINE))
    parser.add_argument(
        "output", nargs="?", help="file with pytest output; omit to read stdin"
    )
    args = parser.parse_args(argv)
    text = Path(args.output).read_text(encoding="utf-8", errors="replace") if args.output \
        else sys.stdin.read()
    result = diff(args.suite, parse_failures(text))
    json.dump(result, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    # Exit 1 on EITHER direction. A stale baseline is not a pass.
    return 1 if (result["new_failures"] or result["unexpected_passes"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
