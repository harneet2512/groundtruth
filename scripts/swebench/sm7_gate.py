#!/usr/bin/env python3
r"""SM-7 — the ALL-CYLINDERS Stage-1 GATE (the Super-Mode build's DEFINITION OF DONE).

Run:  ``python scripts/swebench/sm7_gate.py``

This is a DETERMINISTIC, ``$0``, OFFLINE proof harness — NOT a paid run and NOT a new product
surface. It executes fixture-driven checks over the FROZEN product code (it imports + drives the
existing seam / renderers / resolver, and invokes the landed test suites as its check
implementation) and prints a per-member verdict table. It NEVER edits a product file: when a check
finds a defect it REPORTS it (with the exact function to fix) and FAILS — a FAIL in the report is a
valid deliverable, never something to weaken away.

Members are enumerated DYNAMICALLY from ``rl_profile.PROFILE_MEMBERS["2"]`` (which already contains
every ``PROFILE_MEMBERS["1"]`` member, including the W10 native-FORM flags). Nothing is hardcoded.

THE SEVEN CHECKS (all deterministic, offline):
  1  PROFILE / INVERSION   fresh subprocess: unset GT_RL_PROFILE + non-baseline -> import
                           gt_mini_patch -> every Profile-2 member env=="1" (kill-switches
                           honored); GT_RL_PROFILE=off -> zero members; a profile receipt with
                           member_source is written. (Reuses the proven test_w8 subprocess pattern.)
  2  PREFLIGHT-GAP         rl_profile.preflight, routed through resolve_default_token on an UNSET
                           env, MUST resolve "2" and run the fail-closed capability checks (not
                           return [] as if OFF). This is the fail-closed guarantee for the exact
                           production path W8 made default. If preflight cannot do this today, the
                           gate marks it FAIL and names the function to fix. (NOT patched here.)
  3  PER-MEMBER FIRING     each fact-producing member -> its landed covering test node-ids (read
                           from the suites); a member with NO covering test = DARK-UNPROVEN (a
                           finding, not a skip). The check implementation invokes those node-ids.
  4  NATIVE-GRAMMAR INVAR. every native renderer, fed a fixture that embeds a <gt-*> tag + a test
                           identity, emits ZERO <gt-*> bytes and ZERO test identity (the firewall
                           strips them); the legacy-tag emitters fire ONLY when their native flag
                           is explicitly killed (proven by the byte-identical-off node-ids).
  5  DETERMINISM           the W10 + W13 byte-producing suites run TWICE and diffed (identical
                           per-node outcomes, both green) + an in-process byte-diff of two renderers.
  6  <=1 DOSE + LATCH      the arbiter dose/latch suites (W5/W6/W7 + sm7 invariants) as checks.
  7  REBAKE-READINESS      list what an SM-8 rebake changes (brief native+minimal, capability
                           spec/receipt, profile-receipt producer) and confirm each has a landed test.

OUTPUT: a markdown verdict table (Member | Check | Evidence | Verdict) to stdout AND to
``.claude/reports/SM7_GATE_<ts>.md`` (a gitignored, local-only dir). EXIT 0 iff ZERO FAIL and ZERO
DARK-UNPROVEN among ENABLED members; else non-zero.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# ── repo layout / import path (light in-process imports only; gt_mini_patch stays in subprocess) ──
_REPO = Path(__file__).resolve().parents[2]
_SRC = _REPO / "src"
_ART = _REPO / "artifact_deepswe"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# The report carries an em-dash / native diagnostics carry U+2192; force utf-8 stdio so the Windows
# console (cp1252 by default) never mojibakes the printed report (the file is written utf-8 anyway).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001 — older/replaced streams: best-effort
        pass

from groundtruth.runtime import native_render as nr  # noqa: E402
from groundtruth.runtime import rl_profile as rp  # noqa: E402

# ── verdict vocabulary ────────────────────────────────────────────────────────
PASS = "PASS"
FAIL = "FAIL"
ABSTAIN = "CORRECT-ABSTAIN"
DARK = "DARK-UNPROVEN"
_GREEN = {"PASSED", "XPASS", "XPASSED"}


# ══════════════════════════════════════════════════════════════════════════════
# MEMBER -> COVERING TEST NODE-IDS  (read from the landed suites; full node-ids, tree-prefixed:
# "artifact_deepswe/tests/..." = the seam-side suites; "tests/..." = the runtime/pretask suites)
# A member with NO entry here surfaces as DARK-UNPROVEN (a finding, never a silent skip).
# ══════════════════════════════════════════════════════════════════════════════
A = "artifact_deepswe/tests/"
R = "tests/"
MEMBER_EVIDENCE: dict[str, list[str]] = {
    # ---- Profile-1 (the coherent RL-adherence stack) ----
    "GT_GATEWAY": [
        A + "test_gateway_seam_wiring.py::test_flag_off_no_op",
        A + "test_gateway_seam_wiring.py::test_flag_on_appends_byte_preserving",
    ],
    "GT_GATEWAY_NATIVE": [
        A + "test_gateway_seam_wiring.py::test_native_flag_is_tagfree_reuses_fmt_def_facts_native",
        A + "test_p4_form_native_20260710.py::test_d8_gateway_dispatch_reads_its_own_flag",
    ],
    "GT_STEER_NATIVE": [
        A + "test_p4_form_native_20260710.py::test_rl3_flag_on_gate_true",
        A + "test_p4_form_native_20260710.py::test_rl3_strips_wrapper_tags",
        A + "test_p4_form_native_20260710.py::test_rl3_body_preserved_verbatim",
    ],
    "GT_LANE_ENVELOPE": [
        A + "test_p3_envelope_unification_20260710.py::test_rl1_on_seals_lane_delivery",
        A + "test_p3_envelope_unification_20260710.py::test_rl1_on_delivered_bytes_identical_to_off",
    ],
    "GT_EDIT_CHECK": [
        R + "runtime/test_e_edit_syntax_seam.py::test_producer_fires_on_broken_source_edit",
        R + "runtime/test_e_edit_syntax_seam.py::test_producer_byte_identical_off",
        R + "runtime/test_e_edit_syntax_seam.py::test_producer_leaklaw_skips_test_file",
    ],
    "GT_VERIFY_EXECUTE": [
        R + "runtime/test_b1_mini_seam.py::test_on_path_delivers_format_d_for_attributed_crash",
        R + "runtime/test_b1_mini_seam.py::test_on_path_quiet_on_unattributed_assertion",
        A + "test_b1_seam_fixes_20260709.py::test_f3_executor_routes_through_live_env",
    ],
    "GT_D7_RELATEDNESS": [
        R + "runtime/test_p5_remainder_20260710.py::test_s1_flag_on_related_edit_credits",
        R + "runtime/test_p5_remainder_20260710.py::test_s1_flag_on_unrelated_edit_does_not_credit",
        R + "runtime/test_p5_remainder_20260710.py::test_s1_default_off_is_relatedness_blind",
    ],
    "GT_OBLIGATION_FRESHNESS": [
        R + "runtime/test_p5_remainder_20260710.py::test_o2_stale_edit_after_test_demotes",
        R + "runtime/test_p5_remainder_20260710.py::test_o2_edit_not_newer_than_test_does_not_demote",
        R + "runtime/test_p5_remainder_20260710.py::test_o2_freshness_flag_ae_forwarded",
    ],
    "GT_CONTRACT_MODE": [
        A + "test_seam_bimode_wiring_20260710.py::test_b19_signature_change_emits_delta_not_preserve",
        A + "test_seam_bimode_wiring_20260710.py::test_b19_modify_in_place_preserves",
        A + "test_seam_bimode_wiring_20260710.py::test_b19_add_new_file_suppresses_preserve",
    ],
    "GT_CONTRACT_BILATERAL": [
        A + "test_seam_bimode_wiring_20260710.py::test_b20_on_composes_consumption_contract",
        A + "test_seam_bimode_wiring_20260710.py::test_b20_off_no_consumed_line",
    ],
    "GT_GATEWAY_EDIT_BRIDGES": [
        A + "test_seam_bimode_wiring_20260710.py::test_b3_edit_bridges_reconstructs_before",
        A + "test_seam_bimode_wiring_20260710.py::test_b3_edit_bridges_off_byte_identical",
        A + "test_seam_bimode_wiring_20260710.py::test_b3_seam_threads_bridges_into_normalize_event",
    ],
    "GT_POST_SEARCH_NATIVE": [
        R + "runtime/test_post_search_format_ab.py::test_native_arm_drops_tag_same_facts",
        R + "runtime/test_post_search_format_ab.py::test_native_arm_leak_clean",
        R + "runtime/test_post_search_format_ab.py::test_default_arm_is_tagged",
    ],
    "GT_SCOPE_NATIVE": [
        A + "test_scope_native_form_20260712.py::test_scope_native_form_when_flag_on",
        A + "test_scope_native_form_20260712.py::test_scope_tag_byte_identical_when_flag_off",
        A + "test_scope_native_form_20260712.py::test_scope_native_firewalls_test_path_neighbour",
    ],
    "GT_CONTRACT_NATIVE": [
        A + "test_w10_native_sweep_20260713.py::test_item1_native_sigchange_is_one_compiler_diagnostic",
        A + "test_w10_native_sweep_20260713.py::test_item1_native_preserved_is_bare_rg_caller_rows",
        A + "test_w10_native_sweep_20260713.py::test_item1_native_firewalls_test_caller_rows",
    ],
    "GT_EVIDENCE_NATIVE": [
        A + "test_w10_native_sweep_20260713.py::test_item2_witness_and_caller_units_become_rows",
        A + "test_w10_native_sweep_20260713.py::test_item2_quiet_when_no_locatable_unit",
    ],
    "GT_NUDGE_NATIVE": [
        A + "test_w10_native_sweep_20260713.py::test_item3_on_is_body_only_frame_dropped",
        A + "test_w10_native_sweep_20260713.py::test_item3_off_byte_identical",
        A + "test_w10_native_sweep_20260713.py::test_item3_producer_scaffold_arm_native",
    ],
    "GT_BRIEF_NATIVE": [
        R + "pretask/test_brief_native_obligations_20260713.py::test_section_on_is_plain_checklist",
        R + "pretask/test_brief_native_obligations_20260713.py::test_section_off_is_byte_identical_tag_block",
        R + "pretask/test_brief_native_obligations_20260713.py::test_end_to_end_form_swap",
    ],
    "GT_INSEAM_METRICS": [
        A + "test_w10_native_sweep_20260713.py::test_item6_on_writes_eligible_and_authority_rows",
        A + "test_w10_native_sweep_20260713.py::test_item6_default_off_writes_no_rows",
        A + "test_w10_native_sweep_20260713.py::test_item6_baseline_never_writes",
    ],
    # ---- Super-Mode (Profile-2 only) ----
    "GT_REGISTRY_ENFORCE": [
        R + "runtime/test_registry_enforce_20260711.py::test_route_registry_want_defers_wrong_event_under_enforcement",
        R + "runtime/test_registry_enforce_20260711.py::test_route_registry_want_expires_late_event_under_enforcement",
        R + "runtime/test_registry_enforce_20260711.py::test_renderer_gate_drops_rendererless_class_under_enforcement",
        R + "runtime/test_registry_enforce_20260711.py::test_enforcement_is_byte_identical_for_valid_producers",
    ],
    "GT_COMPLETION_CERT": [
        A + "test_sm3_completion_cert_20260711.py::test_block_decision_unchanged_and_cert_only_when_flag_on",
        A + "test_sm3_completion_cert_20260711.py::test_allow_decision_unchanged_and_cert_decision_allow",
        A + "test_sm3_completion_cert_20260711.py::test_completion_cert_record_never_raises_and_no_leak",
    ],
    "GT_HYPOTHESIS": [
        A + "test_sm3_hypothesis_ledger_20260711.py::test_env_failure_selects_repair_not_source",
        A + "test_sm3_hypothesis_ledger_20260711.py::test_repeated_failure_no_edit_selects_new_hypothesis",
        A + "test_sm3_hypothesis_ledger_20260711.py::test_flag_off_is_a_noop",
    ],
    "GT_VERIFICATION_PLAN": [
        A + "test_sm3_verification_plan_20260711.py::test_flag_on_uses_verification_plan",
        A + "test_sm3_verification_plan_20260711.py::test_attributed_unit_red_delivers_and_feeds_submit",
        A + "test_sm3_verification_plan_20260711.py::test_flag_off_uses_single_lever",
    ],
    "GT_EDIT_OVERLAY": [
        A + "test_sm4_episode_overlay_20260711.py::test_route_delivery_drops_stale_edited_file",
        A + "test_sm4_episode_overlay_20260711.py::test_augment_drops_stale_fact_about_edited_file",
        A + "test_sm4_episode_overlay_20260711.py::test_flag_off_is_byte_identical",
        A + "test_sm3_edit_overlay_20260711.py::test_transaction_reports_new_error_and_preserves_edit",
    ],
    "GT_GLOBAL_ARBITER": [
        A + "test_sm5_global_arbiter_20260711.py::test_flush_delivers_at_most_one_highest_class",
        A + "test_sm5_global_arbiter_20260711.py::test_off_is_byte_identical_and_on_single_plane_matches",
        A + "test_sm5_global_arbiter_20260711.py::test_delivered_winner_is_leak_clean",
    ],
    "GT_CHANGE_SURFACE": [
        R + "pretask/test_change_surface.py::test_python_new_provider_all_roles_missing",
        R + "pretask/test_change_surface.py::test_flag_off_returns_empty_immediately",
        R + "pretask/test_change_surface.py::test_leak_law_test_role_emits_paths_not_bodies",
    ],
    "GT_PATCH_DELTA": [
        R + "runtime/test_patch_delta.py::test_added_required_param_fires_mismatch_with_right_line",
        R + "runtime/test_patch_delta.py::test_flag_off_returns_empty",
        R + "runtime/test_patch_delta.py::test_is_test_caller_never_surfaced",
    ],
    "GT_BRIEF_MINIMAL": [
        A + "test_sm7_stage1_gate_20260712.py::test_51b_generate_brief_minimal_drops_retired_keeps_obligations",
        A + "test_sm7_stage1_gate_20260712.py::test_51a_generate_brief_full_carries_retired_surfaces",
        R + "pretask/test_brief_native_obligations_20260713.py::test_minimal_reducer_keeps_native_obligations",
    ],
    "GT_XSESSION_MEMORY": [
        A + "test_xsession_seam_wiring_20260712.py::test_write_then_load_round_trips_into_episode",
        A + "test_xsession_seam_wiring_20260712.py::test_flush_writes_deterministic_store",
        A + "test_xsession_seam_wiring_20260712.py::test_flag_off_is_a_no_op",
    ],
    "GT_XSESSION_RANKUP": [
        R + "runtime/test_xsession_rankup_20260712.py::test_consumed_caller_break_wins_over_higher_severity_with_flag",
        R + "runtime/test_xsession_rankup_20260712.py::test_rankup_flag_off_returns_same_list_object",
        R + "runtime/test_xsession_rankup_20260712.py::test_arbitrate_still_returns_exactly_one_dose",
    ],
    "GT_CONTENT_LEG": [  # SUBSTRATE-property member (receipt-gated); the CODE path is proven here
        R + "pretask/test_content_leg.py::test_content_fts_candidates_retrieves_by_body",
        R + "pretask/test_content_leg.py::test_content_fts_candidates_excludes_test_symbols",
        R + "pretask/test_content_leg.py::test_localize_reaches_content_leg_on_repoless_no_anchor",
    ],
    "GT_SEM_BODY": [  # SUBSTRATE-property member (receipt-gated); the CODE path is proven here
        R + "pretask/test_sm_body_legs_profile2_20260712.py::test_body_legs_are_profile2_members",
        R + "pretask/test_sm_body_legs_profile2_20260712.py::test_profile2_lights_content_leg_for_stratum_b_gold",
        R + "pretask/test_sm_body_legs_profile2_20260712.py::test_profile_off_leaves_body_legs_dark",
    ],
    "GT_LOC_RESLOT": [
        A + "test_loc_reslot_live_seam_20260712.py::test_broad_grep_abstain_delivers_ranked_rows",
        A + "test_loc_reslot_live_seam_20260712.py::test_bare_symbol_grep_byte_unchanged_no_reslot",
        A + "test_loc_reslot_live_seam_20260712.py::test_test_candidate_dropped_leak_zero",
    ],
    "GT_CERT_DELIVERY": [
        A + "test_cert_delivery_d7_20260712.py::test_covering_block_delivers_native_cert_block_leak0",
        A + "test_cert_delivery_d7_20260712.py::test_hygiene_block_delivers_native_cert_block",
        A + "test_cert_delivery_d7_20260712.py::test_cert_delivery_off_is_byte_identical",
    ],
    "GT_L6_FRESH": [  # SUBSTRATE-property member (staged gt-index binary); the reindex path is proven here
        A + "test_l6_fresh_reindex_telemetry.py::test_l6_fresh_reindex_adds_new_file_and_emits_proof",
        A + "test_l6_fresh_reindex_telemetry.py::test_l6_staging_fallback_refuses_to_reindex_ro_mount",
    ],
}

# CHECK 6 (<=1 dose + latch) node-ids — the arbiter single-dose law + Lane-A latch defer/commit.
DOSE_NODEIDS: list[str] = [
    A + "test_sm5_global_arbiter_20260711.py::test_flush_delivers_at_most_one_highest_class",
    A + "test_sm5_global_arbiter_20260711.py::test_flush_stamps_winner_unified_key",
    A + "test_sm5_global_arbiter_20260711.py::test_flush_unified_dedup_suppresses_already_delivered",
    A + "test_sm7_stage1_gate_20260712.py::test_invariant_3a_arbitrate_yields_one_winner",
    A + "test_sm7_stage1_gate_20260712.py::test_invariant_3b_flush_delivers_at_most_one",
    A + "test_sm7_stage1_gate_20260712.py::test_invariant_8a_lane_a_loser_reproduces_next_turn",
    A + "test_sm7_stage1_gate_20260712.py::test_invariant_8b_lane_a_winner_does_not_re_fire",
    A + "test_w6_recovery_traceframe_arbiter_20260712.py::test_red1_lone_recovery_candidate_delivers_under_arbiter",
    A + "test_w6_recovery_traceframe_arbiter_20260712.py::test_stall_gate_suppresses_progressing_agent",
    A + "test_w7_scope_map_timing_20260713.py::test_le_one_dose_two_on_time_candidates_deliver_one",
    A + "test_w7_scope_map_timing_20260713.py::test_le_one_dose_retired_scope_adds_no_second_lane_a_entry",
]

# CHECK 7 (rebake-readiness) — the SM-8 rebake changes, each mapped to a LANDED covering test.
REBAKE_ITEMS: list[tuple[str, str, list[str]]] = [
    ("brief native FORM (obligations -> plain checklist)",
     "v1r_brief obligations section renders as a native `- [ ]` checklist under GT_BRIEF_NATIVE",
     [R + "pretask/test_brief_native_obligations_20260713.py::test_end_to_end_form_swap",
      R + "pretask/test_brief_native_obligations_20260713.py::test_certificate_obligations_present_under_native_header"]),
    ("brief MINIMAL reduction (retire heavy step-0 surfaces)",
     "generate_v1r_brief drops <gt-localization>/<gt-graph-map>/contract narration, keeps obligations+orientation under GT_BRIEF_MINIMAL",
     [A + "test_sm7_stage1_gate_20260712.py::test_51b_generate_brief_minimal_drops_retired_keeps_obligations",
      A + "test_sm7_stage1_gate_20260712.py::test_51c_certificate_is_reusable_and_pure"]),
    ("capability SPEC / typed manifest (substrate-property members fail closed without a baked-surface receipt)",
     "the typed capability manifest (rl_profile._MEMBER_CAPABILITY_RECEIPT + capability_receipt.py) — NB: no literal .pyi stub in-repo; the SPEC is the typed manifest",
     [R + "runtime/test_capability_manifest_20260712.py::test_substrate_property_members_fail_closed_without_receipt",
      R + "runtime/test_capability_manifest_20260712.py::test_preflight_aborts_on_missing_substrate_receipt"]),
    ("capability RECEIPT producer (reads the REAL baked substrate)",
     "capability_receipt.py emits the real symbol_content_fts / sem_body / gt_index_bin / brief_minimal receipt the preflight consumes",
     [R + "runtime/test_capability_receipt_producer_20260712.py::test_content_and_body_row_counts_are_real",
      R + "runtime/test_capability_receipt_producer_20260712.py::test_brief_minimal_true_only_for_minimal_brief",
      R + "runtime/test_capability_receipt_producer_20260712.py::test_emit_json_cli_roundtrips"]),
    ("profile RECEIPT producer (per-member provenance seal at delivery)",
     "the seam writes gt.profile_receipt.v1 next to the ledger with member_source (inherited vs resolved)",
     [A + "test_delivery_seals_profile_receipt_20260712.py::test_profile_receipt_written_with_members_filtering",
      A + "test_w8_default_inversion_20260713.py::test_receipt_member_source_distinguishes_inherited_vs_resolved"]),
]


# ══════════════════════════════════════════════════════════════════════════════
# pytest runner — routes node-ids to the correct cwd/tree, parses -v, returns {full_nid: status}
# ══════════════════════════════════════════════════════════════════════════════
# pytest -v right-pads short node names with MULTIPLE spaces before the `[ NN%]` column, so the
# gap between the status word and the bracket is variable (\s+, not a single space).
_RES_RE = re.compile(
    r"^(?P<nid>.+?::.+?)\s+(?P<st>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS|XFAILED|XPASSED)"
    r"(?:\s+\[\s*\d+%\])?\s*$"
)


def _hermetic_env() -> dict[str, str]:
    """A child env with every GT_* stripped (no parent-flag contamination) + utf-8 stdio (the
    native diagnostics carry U+2192 '->' / em-dashes; cp1252 would crash them on Windows)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GT_")}
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("PYTHONPATH", None)
    return env


def _pytest_one(cwd: Path, files: list[str], prefix: str, timeout: int) -> dict[str, str]:
    """Run the given test FILES (not node-ids) and return {prefix+parsed_nid: status} for EVERY
    test in them. Running files (never bare node-ids) is deliberate: a single stale/typo node-id
    passed on the command line is a pytest USAGE error (exit 4) that aborts the whole session with
    zero output; running the files instead lets every real test report, and a stale mapped node-id
    simply shows up as MISSING in the lookup (a real finding, not a hidden whole-tree abort)."""
    cmd = [sys.executable, "-m", "pytest", "-v", "-p", "no:cacheprovider",
           "--no-header", "--continue-on-collection-errors", *files]
    proc = subprocess.run(cmd, cwd=str(cwd), env=_hermetic_env(),
                          capture_output=True, text=True, timeout=timeout)
    out: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        m = _RES_RE.match(line.strip())
        if m:
            # Windows pytest echoes node-id paths with os.sep (backslash); the mapping keys use
            # forward slashes -> normalize so the lookup matches on every platform + both trees.
            nid = m.group("nid").replace("\\", "/")
            out[prefix + nid] = m.group("st")
    return out


def run_pytest(targets: list[str], timeout: int = 900) -> dict[str, str]:
    """Run every landed suite FILE referenced by ``targets`` (full node-ids or file paths) and
    return {full_nid: status} for all their tests. Node-ids are looked up by the caller."""
    art_files, repo_files = set(), set()
    for t in targets:
        if t.startswith("artifact_deepswe/"):
            art_files.add(t[len("artifact_deepswe/"):].split("::")[0])
        else:
            repo_files.add(t.split("::")[0])
    results: dict[str, str] = {}
    if art_files:
        results.update(_pytest_one(_ART, sorted(art_files), "artifact_deepswe/", timeout))
    if repo_files:
        results.update(_pytest_one(_REPO, sorted(repo_files), "", timeout))
    return results


def _judge(nodeids: list[str], results: dict[str, str]) -> tuple[str, str]:
    """PASS iff every mapped node-id ran GREEN; FAIL naming the first non-green (or MISSING)."""
    bad = [(n, results.get(n, "MISSING")) for n in nodeids if results.get(n) not in _GREEN]
    if bad:
        detail = ", ".join(f"{n.split('::')[-1]}={s}" for n, s in bad[:3])
        return FAIL, detail
    return PASS, f"{len(nodeids)} node-id(s) green"


# ══════════════════════════════════════════════════════════════════════════════
# CHECK 1 — PROFILE / INVERSION (fresh subprocess; test_w8 pattern)
# ══════════════════════════════════════════════════════════════════════════════
_CHILD = r"""
import json, os, sys
sys.path.insert(0, r"{art}")
sys.path.insert(0, r"{src}")
import gt_mini_patch as g
from groundtruth.runtime import rl_profile as r
members = sorted(r.PROFILE_MEMBERS["1"] | r.PROFILE_MEMBERS["2"])
snap = {{m: os.environ.get(m) for m in members}}
receipt = None
led = os.environ.get("GT_RUNTIME_LEDGER")
if led:
    rpath = os.path.join(os.path.dirname(led) or ".", "gt_profile_receipt.json")
    if os.path.isfile(rpath):
        receipt = json.load(open(rpath, encoding="utf-8"))
payload = {{
    "env": snap,
    "gt_rl_profile": os.environ.get("GT_RL_PROFILE"),
    "member_source": dict(getattr(g, "_GT_PROFILE_MEMBER_SOURCE", {{}})),
    "pytest_in": "pytest" in sys.modules,
    "receipt": receipt,
}}
print("SM7_JSON_BEGIN"); print(json.dumps(payload)); print("SM7_JSON_END")
"""


def _run_child(overrides: dict[str, str | None], ledger_dir: Path | None) -> dict:
    env = _hermetic_env()
    if ledger_dir is not None:
        env["GT_RUNTIME_LEDGER"] = str(ledger_dir / "led.jsonl")
    for k, v in overrides.items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v
    code = _CHILD.format(art=str(_ART), src=str(_SRC))
    proc = subprocess.run([sys.executable, "-c", code], env=env,
                          capture_output=True, text=True, timeout=180)
    b, e = proc.stdout.find("SM7_JSON_BEGIN"), proc.stdout.find("SM7_JSON_END")
    if b == -1 or e == -1:
        raise RuntimeError(f"child produced no payload (rc={proc.returncode})\n"
                           f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
    return json.loads(proc.stdout[b + len("SM7_JSON_BEGIN"):e].strip())


def check1_inversion(p2: list[str]) -> tuple[list[dict], dict[str, bool]]:
    """Returns (rows, per_member_inverted). rows = the sub-checks; per_member = env=="1" on unset."""
    rows: list[dict] = []
    per_member: dict[str, bool] = {m: False for m in p2}
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        # (a) UNSET + non-baseline -> every Profile-2 member "1", gt_rl_profile "2", all "resolved".
        try:
            unset = _run_child({"GT_RL_PROFILE": None, "GT_BASELINE": None}, tdp)
            for m in p2:
                per_member[m] = unset["env"].get(m) == "1"
            missing = [m for m in p2 if unset["env"].get(m) != "1"]
            ok_a = (unset["pytest_in"] is False and unset["gt_rl_profile"] == "2"
                    and not missing
                    and all(unset["member_source"].get(m) == "resolved" for m in p2))
            det = ("every Profile-2 member=='1'; GT_RL_PROFILE->2; member_source all 'resolved'"
                   if ok_a else f"missing/'!=1'={missing[:5]} gt_rl_profile={unset['gt_rl_profile']}")
            rows.append({"name": "unset -> Profile-2 (all members ON, resolved)",
                         "verdict": PASS if ok_a else FAIL, "detail": det})
            # (c) receipt written with member_source
            rec = unset.get("receipt")
            ok_r = (isinstance(rec, dict) and rec.get("schema") == "gt.profile_receipt.v1"
                    and "member_source" in rec and rec.get("gt_rl_profile") == "2")
            rows.append({"name": "profile receipt written (gt.profile_receipt.v1 + member_source)",
                         "verdict": PASS if ok_r else FAIL,
                         "detail": (f"schema={rec.get('schema')} keys={sorted(rec)[:6]}"
                                    if isinstance(rec, dict) else f"receipt={rec!r}")})
        except Exception as exc:  # noqa: BLE001
            rows.append({"name": "unset -> Profile-2", "verdict": FAIL, "detail": f"child fault: {exc}"})
            rows.append({"name": "profile receipt written", "verdict": FAIL, "detail": "unrun"})
        # (b) explicit off -> zero members set, empty member_source, posture untouched.
        try:
            off = _run_child({"GT_RL_PROFILE": "off", "GT_BASELINE": None}, tdp)
            on = [m for m in p2 if off["env"].get(m) is not None]
            ok_b = (not on and off["member_source"] == {} and off["gt_rl_profile"] == "off")
            rows.append({"name": "explicit off -> zero members (legacy/control posture)",
                         "verdict": PASS if ok_b else FAIL,
                         "detail": ("no member set; member_source empty; posture untouched"
                                    if ok_b else f"leaked_members={on[:5]}")})
        except Exception as exc:  # noqa: BLE001
            rows.append({"name": "explicit off -> zero members", "verdict": FAIL, "detail": f"child fault: {exc}"})
        # kill-switch honored: GT_GATEWAY=0 survives while the rest default ON.
        try:
            ks = _run_child({"GT_RL_PROFILE": None, "GT_BASELINE": None, "GT_GATEWAY": "0"}, tdp)
            other = next(m for m in p2 if m != "GT_GATEWAY")
            ok_k = (ks["env"].get("GT_GATEWAY") == "0" and ks["env"].get(other) == "1"
                    and ks["member_source"].get("GT_GATEWAY") == "inherited")
            rows.append({"name": "kill-switch honored (GT_GATEWAY=0 survives; rest ON)",
                         "verdict": PASS if ok_k else FAIL,
                         "detail": (f"GT_GATEWAY={ks['env'].get('GT_GATEWAY')} (inherited); {other}=1"
                                    if ok_k else f"GT_GATEWAY={ks['env'].get('GT_GATEWAY')}")})
        except Exception as exc:  # noqa: BLE001
            rows.append({"name": "kill-switch honored", "verdict": FAIL, "detail": f"child fault: {exc}"})
    return rows, per_member


# ══════════════════════════════════════════════════════════════════════════════
# CHECK 2 — PREFLIGHT-GAP (in-process; a KNOWN defect the gate is designed to catch)
# ══════════════════════════════════════════════════════════════════════════════
def check2_preflight_gap() -> dict:
    """On an UNSET env the W8 seam inverts to Profile-2 (resolve_default_token->'2'), but the
    fail-closed CLI/library guard rp.preflight treats unset as OFF and runs NO capability check.
    So a stale substrate on the default production path would NOT abort before model spend. The
    desired behaviour: preflight, routed through resolve_default_token, resolves '2' on unset and
    runs the capability checks. We PROVE the gap: with a capability deliberately absent (available
    set empty) preflight({}, []) MUST be non-empty (abort). Today it is []."""
    token = rp.resolve_default_token({})                 # '2' — the seam's production default
    gap = rp.preflight({}, [])                            # [] today — the blind path (the defect)
    explicit = rp.preflight({"GT_RL_PROFILE": "2"}, [])   # non-empty — proves the check EXISTS when set
    runs_capability_checks = bool(gap)                    # the property we require of the unset path
    ok = runs_capability_checks and token == "2"
    if ok:
        return {"verdict": PASS,
                "detail": "preflight resolves the production default on unset and runs capability checks"}
    return {
        "verdict": FAIL,
        "detail": (
            f"resolve_default_token({{}})='{token}' but preflight({{}}, [])={gap!r} (blind: runs NO "
            f"capability check on the UNSET production path), while preflight(GT_RL_PROFILE=2, [])="
            f"{len(explicit)} aborts. FIX: groundtruth.runtime.rl_profile.preflight "
            f"(src/groundtruth/runtime/rl_profile.py:451) early-returns via _profile_token/"
            f"_profile_is_off (:460-462) treating unset as OFF; route the effective token through "
            f"resolve_default_token so an unset env resolves '2' and runs _available_from_env + the "
            f"member-capability/manifest checks (fail-closed on the default production path)."
        ),
    }


# ══════════════════════════════════════════════════════════════════════════════
# CHECK 4 — NATIVE-GRAMMAR INVARIANT (in-process renderer sweep + the byte-identical-off node-ids)
# ══════════════════════════════════════════════════════════════════════════════
# Each fixture DELIBERATELY smuggles a <gt-*> tag AND a test identity; the firewall must strip both.
_LEAKY = "\n<gt-evidence>x</gt-evidence>\ntests/test_secret.py::test_a\ndef test_a():"


def _native_sweep() -> tuple[list[dict], list[str]]:
    """Drive every native renderer with a tag+identity-laced fixture; return (rows, leaks)."""
    cov = {"stdout_tail": "=== FAILURES ===\ntests/test_x.py::test_a FAILED\n"
                          "src/mod.py:12: boom\nE   assert 4 == 3\ngot 4 want 3\n" + _LEAKY}
    cases = [
        ("render_covering_failure_native", nr.render_covering_failure_native(cov)),
        ("render_syntax_error_native",
         nr.render_syntax_error_native({"verdict": "syntax_error",
                                        "diagnostic": 'File "src/a.py", line 3\n<gt-x>SyntaxError: bad'})),
        ("render_caller_contract_native",
         nr.render_caller_contract_native("get_user", 3, 2, def_file="svc/users.py", sig_delta="a" + _LEAKY + "->a, b")),
        ("render_signature_delta_native",
         nr.render_signature_delta_native("app/main.py", 7, "get_user", expected_min=2, expected_max=2, given=1)),
        ("render_trace_frame_native", nr.render_trace_frame_native("src/pool.py", 88, "connect")),
        ("render_registration_native",
         nr.render_registration_native("providers/__init__.py", 4, "AzureProvider" + _LEAKY,
                                       ["AwsProvider", "GcpProvider"])),
        ("render_def_rows_native",
         nr.render_def_rows_native([("src/a.py", 1, "foo"), ("tests/test_a.py", 2, "foo")])),
        ("render_ranked_list_native", nr.render_ranked_list_native([("src/a.py", 1, "foo")])),
        ("render_recovery_native",
         nr.render_recovery_native("repair_not_source", "repair the env" + _LEAKY + "; do not edit source")),
        ("render_scope_constraint_native", nr.render_scope_constraint_native("src/neighbor.py")),
        ("render_completion_cert_native",
         nr.render_completion_cert_native(["selected_test_status", "syntax_status"],
                                          hygiene_blocked=True, hygiene_detail="vendored file changed" + _LEAKY)),
        ("render_cochange_native", nr.render_cochange_native("anything")),   # internal-only: always ""
    ]
    rows: list[dict] = []
    leaks: list[str] = []
    for name, out in cases:
        tag = nr.contains_gt_tag(out)
        ident = nr.contains_test_identity(out)
        if tag or ident:
            leaks.append(name)
        if out.strip():
            v = FAIL if (tag or ident) else PASS
            det = f"{len(out)}B; tag={tag} identity={ident}"
        else:
            v = ABSTAIN                       # correct-or-quiet (e.g. cochange = internal-only)
            det = "empty (correct-or-quiet)"
        rows.append({"name": name, "verdict": v, "detail": det})
    return rows, leaks


# ══════════════════════════════════════════════════════════════════════════════
# CHECK 5 — DETERMINISM (byte-producing suites run twice + in-process renderer byte-diff)
# ══════════════════════════════════════════════════════════════════════════════
_DET_FILES = [
    A + "test_w10_native_sweep_20260713.py",
    A + "test_w13_cd_prefix_search_20260713.py",
]


def check5_determinism() -> dict:
    run1 = run_pytest(_DET_FILES)
    run2 = run_pytest(_DET_FILES)
    identical = run1 == run2
    both_green = run1 and all(s in _GREEN for s in run1.values()) and all(s in _GREEN for s in run2.values())
    # in-process byte-diff: the same input must yield byte-identical native output twice.
    cov = {"stdout_tail": "src/mod.py:12: boom\nE   assert 4 == 3\ngot 4 want 3\n"}
    byte_ok = (nr.render_covering_failure_native(cov) == nr.render_covering_failure_native(cov)
               and nr.render_completion_cert_native(["selected_test_status"])
               == nr.render_completion_cert_native(["selected_test_status"]))
    ok = identical and both_green and byte_ok
    detail = (f"W10+W13 twice: {len(run1)} nodes, identical={identical}, both_green={both_green}; "
              f"in-process renderer byte-identity={byte_ok}")
    if not both_green:
        reds = sorted(n for n, s in run1.items() if s not in _GREEN)[:4]
        detail += f"; reds={reds}"
    return {"verdict": PASS if ok else FAIL, "detail": detail}


# ══════════════════════════════════════════════════════════════════════════════
# report + main
# ══════════════════════════════════════════════════════════════════════════════
def _tbl(headers: list[str], rows: list[list[str]]) -> str:
    def esc(x: str) -> str:
        return str(x).replace("|", "\\|").replace("\n", " ")
    out = ["| " + " | ".join(headers) + " |",
           "| " + " | ".join("---" for _ in headers) + " |"]
    for r in rows:
        out.append("| " + " | ".join(esc(c) for c in r) + " |")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="SM-7 all-cylinders Stage-1 gate")
    ap.add_argument("--out-dir", default=str(_REPO / ".claude" / "reports"))
    args = ap.parse_args()

    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    # ── enumerate members DYNAMICALLY ────────────────────────────────────────
    p1 = sorted(rp.PROFILE_MEMBERS["1"])
    p2 = sorted(rp.PROFILE_MEMBERS["2"])
    super_only = sorted(set(p2) - set(p1))
    w10_native = sorted({"GT_CONTRACT_NATIVE", "GT_EVIDENCE_NATIVE", "GT_NUDGE_NATIVE",
                         "GT_BRIEF_NATIVE", "GT_INSEAM_METRICS"} & set(p1))
    enabled = p2  # clean env: the W8 default fans every Profile-2 member ON (none kill-switched)

    # ── batch every pytest node-id needed by checks 3/6/7 into ONE run ───────
    firing_ids = sorted({nid for ids in MEMBER_EVIDENCE.values() for nid in ids})
    rebake_ids = sorted({nid for _n, _d, ids in REBAKE_ITEMS for nid in ids})
    all_ids = sorted(set(firing_ids) | set(DOSE_NODEIDS) | set(rebake_ids))
    print(f"[sm7] running {len(all_ids)} covering node-id(s) across the landed suites ...",
          file=sys.stderr)
    results = run_pytest(all_ids)

    # ── run the checks ───────────────────────────────────────────────────────
    inv_rows, per_member_inv = check1_inversion(p2)
    gap = check2_preflight_gap()
    native_rows, native_leaks = _native_sweep()
    determinism = check5_determinism()

    # per-member firing verdicts
    member_rows: list[list[str]] = []
    firing_verdicts: dict[str, str] = {}
    for m in p2:
        ids = MEMBER_EVIDENCE.get(m)
        if not ids:
            verdict, det = DARK, "no covering test mapped"
        else:
            verdict, det = _judge(ids, results)
        firing_verdicts[m] = verdict
        inv = "1" if per_member_inv.get(m) else "-"
        ev = (ids[0].split("::")[-1] + (f" (+{len(ids)-1})" if len(ids) > 1 else "")) if ids else "-"
        member_rows.append([m, "PER-MEMBER FIRING", f"inv={inv}; {ev}; {det}", verdict])

    # dose + rebake verdicts
    dose_v, dose_d = _judge(DOSE_NODEIDS, results)
    rebake_rows: list[list[str]] = []
    rebake_all_ok = True
    for name, desc, ids in REBAKE_ITEMS:
        v, d = _judge(ids, results)
        rebake_all_ok = rebake_all_ok and v == PASS
        rebake_rows.append([name, v, ", ".join(i.split("::")[-1] for i in ids), d])

    native_v = FAIL if native_leaks else PASS

    # ── tallies + exit ───────────────────────────────────────────────────────
    fails = 0
    darks = 0
    for m in enabled:
        if firing_verdicts[m] == FAIL:
            fails += 1
        elif firing_verdicts[m] == DARK:
            darks += 1
    global_checks = [
        ("PROFILE/INVERSION", FAIL if any(r["verdict"] == FAIL for r in inv_rows) else PASS),
        ("PREFLIGHT-GAP", gap["verdict"]),
        ("NATIVE-GRAMMAR INVARIANT", native_v),
        ("DETERMINISM", determinism["verdict"]),
        ("<=1 DOSE + LATCH", dose_v),
        ("REBAKE-READINESS", PASS if rebake_all_ok else FAIL),
    ]
    for _n, v in global_checks:
        if v == FAIL:
            fails += 1
    exit_code = 0 if (fails == 0 and darks == 0) else 1

    # ── build the markdown report ────────────────────────────────────────────
    L: list[str] = []
    L.append(f"# SM-7 ALL-CYLINDERS Stage-1 Gate — {ts}")
    L.append("")
    L.append(f"- **Members enumerated dynamically:** Profile-1 = {len(p1)}, Profile-2 = {len(p2)} "
             f"(Super-Mode-only = {len(super_only)}, W10 native FORM flags in Profile-1 = {len(w10_native)}).")
    L.append(f"- **Enabled (clean env, W8 default-ON):** {len(enabled)} members.")
    L.append(f"- **Exit:** `{exit_code}` — FAILs among enabled = {fails}, DARK-UNPROVEN = {darks}. "
             f"(Exit 0 iff both are 0.)")
    L.append("")
    L.append("## Cross-cutting checks")
    L.append("")
    L.append(_tbl(["Scope", "Check", "Evidence", "Verdict"], [
        ["ALL (subprocess)", "PROFILE/INVERSION",
         "; ".join(f"{r['name']} -> {r['verdict']}" for r in inv_rows), global_checks[0][1]],
        ["rl_profile.preflight", "PREFLIGHT-GAP", gap["detail"], gap["verdict"]],
        ["native_render.*", "NATIVE-GRAMMAR INVARIANT",
         (f"12 renderers swept with tag+identity-laced fixtures; leaks={native_leaks or 'none'}; "
          "byte-identical-off proven by GT_POST_SEARCH_NATIVE/GT_SCOPE_NATIVE off node-ids"), native_v],
        ["W10+W13", "DETERMINISM", determinism["detail"], determinism["verdict"]],
        ["global_arbiter/Lane-A", "<=1 DOSE + LATCH", dose_d, dose_v],
        ["SM-8 rebake", "REBAKE-READINESS",
         f"{len(REBAKE_ITEMS)} rebake changes, each mapped to a landed test", global_checks[5][1]],
    ]))
    L.append("")
    L.append("### PROFILE/INVERSION sub-checks (fresh subprocess)")
    L.append("")
    L.append(_tbl(["Sub-check", "Detail", "Verdict"],
                  [[r["name"], r["detail"], r["verdict"]] for r in inv_rows]))
    L.append("")
    L.append("### NATIVE-GRAMMAR renderer sweep (in-process; each fixture smuggles a `<gt-*>` tag + a test identity)")
    L.append("")
    L.append(_tbl(["Renderer", "Detail", "Verdict"],
                  [[r["name"], r["detail"], r["verdict"]] for r in native_rows]))
    L.append("")
    L.append("### REBAKE-READINESS (what an SM-8 rebake changes -> its landed test)")
    L.append("")
    L.append(_tbl(["Rebake change", "Landed?", "Evidence node-ids", "Detail"], rebake_rows))
    L.append("")
    L.append("## PER-MEMBER FIRING (34 members)")
    L.append("")
    L.append(_tbl(["Member", "Check", "Evidence (inv = inverted-ON on unset; node-id)", "Verdict"],
                  member_rows))
    L.append("")
    L.append("## Summary")
    L.append("")
    n_pass = sum(1 for m in enabled if firing_verdicts[m] == PASS)
    L.append(f"- Firing: PASS={n_pass}, FAIL={sum(1 for m in enabled if firing_verdicts[m]==FAIL)}, "
             f"DARK-UNPROVEN={darks} / {len(enabled)} enabled members.")
    L.append(f"- Cross-cutting: " + ", ".join(f"{n}={v}" for n, v in global_checks) + ".")
    L.append(f"- **Gate verdict: {'GREEN (exit 0)' if exit_code == 0 else 'RED (exit ' + str(exit_code) + ')'}.**")
    if gap["verdict"] == FAIL:
        L.append("")
        L.append("> **Open FAIL — PREFLIGHT-GAP (report-only; NOT patched by the gate):** "
                 + gap["detail"])
    report = "\n".join(L) + "\n"

    # ── write + print ────────────────────────────────────────────────────────
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"SM7_GATE_{ts}.md"
    out_path.write_text(report, encoding="utf-8")

    sys.stdout.write(report)
    sys.stdout.write(f"\n[sm7] report written to {out_path}\n")
    sys.stdout.write(f"[sm7] EXIT {exit_code}\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
