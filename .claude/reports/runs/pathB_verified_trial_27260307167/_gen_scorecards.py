import json, os
BASE = os.path.dirname(os.path.abspath(__file__))

# Tier-2 / Tier-3 judgments from the chronological gt_trial.md S4 reads (2026-06-10 session)
J = {
 "astropy__astropy-12907": dict(resolved=True, delivered=1, correct=0, consumed=0, fair_probe=0, right_trajectory=0,
   gate_broke="correct (headline=non-gold core.py; jQuery junk callers; isinstance->TableColumns false CALLEEs) + consumed=0 + fair_probe=0 (issue names separability_matrix + repro)",
   gold_in_brief=True, first_gold_rank=2, gold_edited=True, turns_to_gold=16, edit_to_gold_action=21,
   self_localized=True, traj="Issue-driven self-localization; agent hand-traced _cstack to the =1->=right fix (MSG 34-38); GT peripheral."),
 "astropy__astropy-13033": dict(resolved=False, delivered=1, correct=0, consumed=0, fair_probe=1, right_trajectory=0,
   gate_broke="correct (all 6 L1 candidates wrong; jQuery junk) - post-view <gt-scope> later named gold core.py correctly (PARTIAL delivery win, ambiguous consumption)",
   gold_in_brief=False, first_gold_rank=None, gold_edited=True, turns_to_gold=4, edit_to_gold_action=46,
   self_localized=True, traj="Gold file+function edited; miss = error-message WORDING vs hidden F2P assertion (agent calibrated against stale visible test_sampled.py it grepped itself, MSG 70-75)."),
 "astropy__astropy-13236": dict(resolved=False, delivered=1, correct=0, consumed=1, fair_probe=0, right_trajectory=0,
   gate_broke="correct (rank-1 gold but issue quotes the exact code = redundant; jQuery minified junk delivered as WITNESS; contracts anchored on artifact symbol). consumed=1 is the L5 scaffold_trap nudge (MSG 49 -> pivot to table.py source at MSG 50), not L1",
   gold_in_brief=True, first_gold_rank=1, gold_edited=True, turns_to_gold=1, edit_to_gold_action=46,
   self_localized=True, traj="FutureWarning added at gold line with NON-gold wording -> hidden pytest.warns(match=...) fails."),
 "astropy__astropy-13398": dict(resolved=False, delivered=1, correct=0, consumed=0, fair_probe=0, right_trajectory=0,
   gate_broke="correct (gold = NEW FILE itrs_observed_transforms.py, unnamable by ranking; candidates adjacent only) + fair_probe=0 (issue embeds the full implementation). Submitted patch MALFORMED -> eval ERROR (Patch Apply Failed)",
   gold_in_brief=False, first_gold_rank=None, gold_edited=True, turns_to_gold=25, edit_to_gold_action=29,
   self_localized=True, traj="Fix content followed the issue-embedded implementation; loss = hand-built patch concatenation (patch unexpectedly ends in middle of line, line 104)."),
 "astropy__astropy-13453": dict(resolved=True, delivered=1, correct=0, consumed=0, fair_probe=1, right_trajectory=0,
   gate_broke="correct (gold ascii/html.py ABSENT from all 6 L1 candidates) + consumed=0 (agent action 1 = own find; action 2 grep class HTML -> html.py)",
   gold_in_brief=False, first_gold_rank=None, gold_edited=True, turns_to_gold=3, edit_to_gold_action=25,
   self_localized=True, traj="Self-derived fix (_set_col_formats in HTML.write); GT scope/post-view trailed the agent."),
 "astropy__astropy-13579": dict(resolved=True, delivered=1, correct=1, consumed=1, fair_probe=1, right_trajectory=1,
   gate_broke="none - the GT-caused resolve. Caveat: brief contains isinstance->TableColumns junk CALLEE lines (minority); fair-probe caveat: a frontier model might guess the path from the class name, but the chronology shows ZERO search actions and the wrappers/ segment existed only in the brief",
   gold_in_brief=True, first_gold_rank=1, gold_edited=True, turns_to_gold=1, edit_to_gold_action=20,
   self_localized=False, traj="Agent FIRST command = cat of the exact brief path; fix flowed through the brief-named helper _pixel_to_world_values_all."),
 "django__django-10097": dict(resolved=False, delivered=1, correct=1, consumed=0, fair_probe=0, right_trajectory=0,
   gate_broke="consumed=0 + fair_probe=0 (issue names URLValidator + quotes the regex). Rank-1 gold correct but redundant. Agent installed the gold-class regex [^\\s:@/] (MSG 89-94) then REVERTED to [^\\s@/] after the STALE visible valid_urls.txt contradicted it; the failure_persisted nudge (MSG 96) fired at exactly that moment and plausibly reinforced the revert",
   gold_in_brief=True, first_gold_rank=1, gold_edited=True, turns_to_gold=1, edit_to_gold_action=44,
   self_localized=True, traj="Gold file+line edited; regex character class calibrated against stale fixtures replaced by the hidden test patch."),
 "django__django-10554": dict(resolved=False, delivered=1, correct=0, consumed=0, fair_probe=1, right_trajectory=0,
   gate_broke="correct (gold compiler.py at rank 2; headline rank 1 = query.py where the wrong fix landed) + consumed=0 (agent read get_order_by twice, MSG 88-105, and walked away; failure_persisted nudge MSG 151 - the one substantively-correct nudge - unconsumed)",
   gold_in_brief=True, first_gold_rank=2, gold_edited=False, turns_to_gold=10, edit_to_gold_action=None,
   self_localized=True, traj="Wrong mechanism: defensive query.chain() clone in query.py; PostgreSQL-only symptom unreproducible on SQLite testbed."),
 "sympy__sympy-11618": dict(resolved=True, delivered=1, correct=0, consumed=0, fair_probe=0, right_trajectory=0,
   gate_broke="correct (gold point.py at rank 6/6 of a confidence=low list; headline = wrong ellipse.py) + fair_probe=0 (250-char issue contains the repro naming Point.distance)",
   gold_in_brief=True, first_gold_rank=6, gold_edited=True, turns_to_gold=7, edit_to_gold_action=27,
   self_localized=True, traj="Agent ran the issue repro as action 1, grepped def distance in point.py itself (MSG 14)."),
 "sympy__sympy-12096": dict(resolved=True, delivered=1, correct=0, consumed=0, fair_probe=0, right_trajectory=0,
   gate_broke="correct (HIGH-confidence single target = WRONG file lambdify.py; gold function.py only via the issue) + fair_probe=0 (issue: the code is in Function._eval_evalf)",
   gold_in_brief=False, first_gold_rank=None, gold_edited=True, turns_to_gold=2, edit_to_gold_action=24,
   self_localized=True, traj="Agent first probe greps BOTH lambdify.py (brief target - one wasted probe) and function.py (issue target); followed the issue."),
}
TURNS = {
 "astropy__astropy-12907": (16, 56.02960467), "astropy__astropy-13033": (4, 5.25348043),
 "astropy__astropy-13236": (1, 0.0), "astropy__astropy-13398": (25, 100.93471241),
 "astropy__astropy-13453": (3, 1.47359729), "astropy__astropy-13579": (1, 0.0),
 "django__django-10097": (1, 0.0), "django__django-10554": (10, 10.68186069),
 "sympy__sympy-11618": (7, 14.30868769), "sympy__sympy-12096": (2, 0.0),
}
NUDGE = {
 "astropy__astropy-12907": (2, 0, 1), "astropy__astropy-13033": (3, 1, 0),
 "astropy__astropy-13236": (3, 1, 1), "astropy__astropy-13398": (2, 0, 0),
 "astropy__astropy-13453": (2, 0, 1), "astropy__astropy-13579": (2, 0, 1),
 "django__django-10097": (3, 0, 1), "django__django-10554": (3, 0, 1),
 "sympy__sympy-11618": (2, 0, 1), "sympy__sympy-12096": (1, 0, 0),
}
def f8(x):
    return None if x is None else float("%.8f" % float(x))

for task, j in J.items():
    g = json.load(open(os.path.join(BASE, task, "gt_artifacts", "foundational_gate_report.json")))
    d = json.load(open(os.path.join(BASE, task, "gt_deep_metrics_%s.json" % task)))
    gr, gl, ge = g['gate_resolution'], g['gate_lsp'], g['gate_embedder']
    eff, ag, gd = d['efficiency'], d['agent'], d['gt_delivery']
    gt_caused = int(all([j['delivered'], j['correct'], j['consumed'], j['fair_probe'], j['right_trajectory']]))
    nd, nc, nfp = NUDGE[task]
    sc = {
      "task": task, "run": "pathB_verified_trial_27260307167",
      "audit": "gt_trial.md S4+S5, 2026-06-10, chronological read of traj.json (never grep)",
      "tier1_outcome": {
        "resolved": j['resolved'],
        "baseline_pass": "N/A - no frozen SWE-bench Verified baseline exists; the frozen 87/300 file is OH+SWE-bench-Live (different benchmark+harness). Do not pair against it.",
        "flip": "N/A", "regression": "N/A", "per_task_delta": "N/A",
      },
      "tier2_causality": {
        "delivered": f8(j['delivered']), "correct": f8(j['correct']), "consumed": f8(j['consumed']),
        "fair_probe": f8(j['fair_probe']), "right_trajectory": f8(j['right_trajectory']),
        "gt_caused": f8(gt_caused), "gate_broke": j['gate_broke'],
        "verdict_note": ("gt_caused & flip=N/A: per S5 logic this is a GT win on trajectory grounds (correct context delivered+consumed)" if gt_caused
                          else ("resolved & NOT gt_caused = self-solve/luck - NOT a GT win, do not count it" if j['resolved'] else "not resolved & not gt_caused")),
      },
      "tier3_localization": {
        "gold_file_reached_by_brief": j['gold_in_brief'],
        "first_gold_rank": (f8(j['first_gold_rank']) if j['first_gold_rank'] is not None else "absent (no abstain taken)"),
        "gold_edited": j['gold_edited'],
        "first_edit_action": f8(ag['first_edit_action']),
        "edit_to_gold_action": f8(j['edit_to_gold_action']),
        "turns_to_gold_view": f8(TURNS[task][0]),
      },
      "tier4_nonharm_efficiency": {
        "action_count": f8(ag['action_count']), "action_count_delta": "N/A (no paired baseline arm)",
        "first_edit_latency_actions": f8(ag['first_edit_action']),
        "unique_files_edited": len(ag['edited_files']),
        "looped_stuck": False,
        "gt_injected_tokens": f8(d['gt_injected_tokens_total']),
        "time_first_action_to_gold_view_s": f8(TURNS[task][1]),
        "self_localized": j['self_localized'],
      },
      "tier5_per_layer": {
        "L1_brief":   {"eligible":1,"emitted":int(gd['brief_delivered']>0),"suppressed":0,"rendered_chars":f8(d['substrate']['brief_chars']),"consumed":int(task=="astropy__astropy-13579")},
        "consensus_scope": {"eligible":1,"emitted":int(gd['scope_delivered']),"suppressed":0,"consumed":int(task=="astropy__astropy-13033")},
        "L3b_post_view": {"eligible":1,"emitted":int(gd['evidence_delivered']),"suppressed":0,"consumed":0},
        "L3_post_edit_contract": {"eligible":1,"emitted":int(gd['contract_delivered']),"suppressed":0,"consumed":0},
        "L4_event_hook": {"note":"no separate L4 surface on PATH B; view/edit/failure events are the wrapper hooks above - absence != dead layer (gt_gt S12)"},
        "L5_L5b_governor_nudges": {"emitted":nd,"consumed":nc,"false_positive_firings":nfp},
        "L6_reindexer": {"note":"N/A-by-design on the substrate path - authoritative read-only mounted graph, hash parity (gt_gt S12 update)"},
        "cochange_multifile": {"emitted":int(gd['cochange_delivered']),"consumed":0},
      },
      "tier6_legitimacy": {
        "foundational_gates_green": bool(g['verdict']['all_on']),
        "gate_detail": {
          "P1_det_pct": f8(gr['det_pct']), "P1_name_match": int(gr['name_match_edges']),
          "P2_calls_edges": f8(gr['calls_edges']), "P3_embedder_class": ge['present']['class'],
          "P3_cos_related": f8(ge['present']['cos_related']), "P3_cos_unrelated": f8(ge['present']['cos_unrelated']),
          "P3_is_zero": ge['present']['is_zero'], "P3_effective_w_sem": f8(ge['consumption']['effective_w_sem']),
        },
        "test_names_leaked": 0, "fail_to_pass_leaked": False, "no_gold_labels": True,
        "telemetry_stdout_leak": "1 ([gt-patch:loaded] verbatim in MSG 3 tool output - loader banner on stdout; product bug, NOT test/F2P leakage; does not void per S6 but must move to stderr)",
        "void": False,
      },
      "tier7_cost": {
        "llm_in": f8(eff['llm_tokens_in']), "llm_out": f8(eff['llm_tokens_out']),
        "llm_cost_usd": f8(eff['llm_cost_usd']), "cost_source": eff['cost_source'],
        "gt_injected_tokens": f8(eff['gt_injected_tokens_total']),
        "wall_clock_s": f8(d['wall_clock_s']),
        "time_to_first_edit_actions": f8(ag['first_edit_action']),
        "time_to_gold_view_s": f8(TURNS[task][1]),
      },
      "manager_line": {"gt_caused_flip": "N/A (flip undefined - no Verified baseline)", "gt_caused": bool(gt_caused)},
      "trajectory_finding": j['traj'],
    }
    out = os.path.join(BASE, task, "scorecard.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(sc, fh, indent=1)
    print("wrote", out, "| resolved=%s gt_caused=%s" % (j['resolved'], gt_caused))
