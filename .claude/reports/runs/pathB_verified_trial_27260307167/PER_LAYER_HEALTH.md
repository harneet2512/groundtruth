# PER_LAYER_HEALTH — PATH B trial (run 27260307167) — per-layer aggregate vs gt_gt.md §12 roles
## 2026-06-10 · 10 tasks (astropy-12907/13033/13236/13398/13453/13579, sympy-11618/12096, django-10097/10554)

**Question answered:** besides L1/localization (known broken — 4/4 wrong-localizations = RERANK_LOGIC per
TIER3B), is every OTHER GT layer behaving correctly per its gt_gt.md §12 role?

**Method (read + synthesis only):** aggregated from the 10 per-task §4 ledgers
(`task_ledgers/<task>.md`, heading `## 2026-06-10 PATH B trial`), the 10 `<task>/scorecard.json`,
`GT_TRIAL_AUDIT_SUMMARY.md`, `SECTION4_SUMMARY.md`, `TIER3B_ARCHITECTURAL_CONFORMANCE.md`, and
gt_gt.md §12 (lines 666–697) for each layer's role + criterion. Where the two audit passes in a ledger
disagreed (first-iteration vs the §4+§5 second pass), the §4+§5 second pass (second-verifier
cross-checked) was used. One claim was re-verified against the raw artifacts directly:
`graph_certificate.json` + `foundational_gate_report.json` on all 10 tasks (see gates/certs row).

---

## The table

| layer | §12 role | eligible-on | fired-on | correct-on | consumed-on | VERDICT | defect-if-any |
|---|---|---|---|---|---|---|---|
| **L3b** (post-view contract pillar) | contract pillar: DELIVERED + CORRECT + relevant to bug locus + CONSUMED; low consumed = relevance, NOT "agent ignored it" | 10/10 | 10/10 | 7 correct (13033, 13398, 13579, 10097, 10554, 11618, 12096) · 1 partial (13453) · **2 wrong (12907, 13236)** | 1 weak (13579) | **WORKING-WITH-DEFECTS** | Two firings delivered garbage as [WITNESS] facts: 12907 (`where calls -> astropy/table/index.py:511` — irrelevant edge for separable.py) and 13236 (raw minified jQuery: `gb called by -> astropy/extern/jquery/data/js/jquery.dataTables.min.js:9`). Recurring builtin-laundered `isinstance -> table.py:308` lines ride inside otherwise-correct blocks (13033, 13453, 13579). Bug-locus relevance was genuinely high once (10554: SIBLINGS literally named gold `get_order_by`). |
| **consensus / scope** (`<gt-scope>` + curation tracker) | deliver a correct working-set scope at view time | 10/10 | 10/10 (multi-fire: re-anchors follow the agent) | correct on 8 (incl. the run's one leading delivery win: 13033 MSG 7 named gold `timeseries/core.py` 1 turn pre-open) · noise/wrong firings on 4 tasks (setup.py build-noise scope on 12907 MSG 15/25, 13033 MSG 31/47, 13236 MSG 131; wrong initial anchor on 12096, inherited from L1's wrong HIGH steer) | 1 partial/ambiguous (13033) + 2 weak (13398, 13579) | **WORKING-WITH-DEFECTS** | Mostly TRAILING (re-anchors confirm where the agent already is — decision value ≈ 0); fires on build-noise views (`setup.py — in scope (you are viewing this); GT could not expand scope`); 10097 scope missed the genuinely needed companion (`tests/validators/*.txt`). Not the literal once-per-run Layer-A on this path — it re-fires on re-anchor, so no "single shot wasted" failure mode; the waste mode here is setup.py firings. |
| **L3 post-edit contract** (`<gt-contract>` + post_edit) | post-edit interface/raises contract of the edited region | 10/10 | 10/10 | 6 correct · 3 partial (12907, 13236, 13453 — builtin-laundered CALLEE/CALLERS facts) · 1 mechanically-correct-but-contextually-noise (11618: fired on py3.11 env-shim edits to basic.py/plot.py) | 0 proven (13453 fill_values regression check rated PLAUSIBLE-Y in pass 1, C=N in the authoritative pass 2) | **WORKING-WITH-DEFECTS** | Worst single output of the run: 13236 MSG 93 `[SIGNATURE] def isinstance( self: Self@TableColumns, cls: Any ) -> list [CALLERS] isinstance: 1048 verified caller(s) in 226 file(s) — preserve this interface` — a builtin laundered into a preserve-this-interface instruction. Also no edit-classifier: contracts fire on env-shim edits (11618). |
| **GT_VERIFY** (agent-invoked verify surface) | L3 verify hook with correct content | **0/10 — not wired on PATH B** | 0 | — | — | **N-A-BY-DESIGN (on this path)** | Identical ledger row on all 10: "the agent-invoked GT_VERIFY surface is not wired on PATH B (mini-swe Verified pipeline); the post-edit `<gt-contract>` injections carry the L3 role… no `gt understand`/`gt verify` invocation occurs in any of the agent's commands." Not a dead layer — but note this run provides ZERO evidence about GT_VERIFY's own correctness. |
| **L4** (EVENT hook) | fires on a specific agent event; absence = the event didn't occur, NOT dead | event occurred **0/10** | 0 | — | — | **N-A-BY-DESIGN (on this path)** | Identical ledger row on all 10: "on PATH B the wrapper's view/edit/failure/loop events ARE the hook surface… No separate L4 event exists on this path." Correct silence; zero misfires observed. Cannot be certified WORKING from this run — it was never exercised (no evidence either way). |
| **L5 / L5b** (trajectory governor: failure_persisted + scaffold_trap + loop) | nudge DELIVERED at a live hook AND strong enough to change behavior, without harming a correct course | 10/10 | ~23 nudge firings (per-task 2,3,3,2,2,2,3,3,2,1) | **failure_persisted: 1/7 substantively correct.** scaffold_trap: 4/5 true-positive. loop: 0/1 | scaffold_trap: 1 full (13236 MSG 49→50 immediate pivot) + 2 partial (13033, 13398). failure_persisted: 0 (the one correct firing, 10554 MSG 151, was ignored) | **BROKEN (failure_persisted + loop arms); scaffold_trap arm WORKING** | failure_persisted fired 7×: **5 false positives on ENV/scratch errors** (12907 `_compiler` C-ext, 13033 scratch-script bug, 13236 erfa import, 13579 test-harness frame error, 11618 py3.11 `collections.Mapping`) telling agents with CORRECT fixes "your current hypothesis is likely wrong"; **1 fired against a gold-equivalent edit (10097 MSG 96** — confirmed from the ledger: "agent abandons `[^\s:@/]` (the GOLD character class)… the nudge's 'your hypothesis is likely wrong' pointed AWAY from the gold fix… C=N (false positive WITH plausible HARM)… revert followed it immediately"); **1 substantively correct (10554) — unconsumed**. Loop nudge (13453 MSG 47) also false-positive ("no progress" was wrong — each rerun produced new state). Cursor-mentality (non-harm) violation class. |
| **L6** (post-edit reindexer) | reindex + preserve LSP enrichment; on the substrate path it is **gated OFF by design** (authoritative read-only graph.db, hash parity — gt_gt §12 update + §6 note) | 0/10 by design | 0 | — | — | **N-A-BY-DESIGN** | None. Identical ledger row on all 10: "single-file reindex is deliberately OFF… 'L6 fired' is the wrong expectation here." Nothing in any trajectory or scorecard expected it to fire; no stale-graph symptom attributable to its absence was logged. |
| **gates / certs** (legitimacy layer) | proof artifacts; reconcile any FAIL against the runtime witness before reporting | 10/10 | 10/10 | 10/10 | n/a | **WORKING** | Verified directly from the artifacts (not from the summaries): `graph_certificate.json` verdict = `GRAPH_FAIL_MISSING_HANDOFF` on **all 10**, with `hook_graph_hash=null` + `lsp_warm_from_same_graph=true` — exactly the §12 documented FALSE FAIL, reconciled on every task by the runtime witness (`foundational_gate_report.json`: `all_on=true`, gate_resolution/gate_lsp/gate_embedder `pass=true`, `stamp_mismatch=""` on all 10). Foundational gates GREEN 10/10; no task VOID; test_names_leaked=0/10, F2P leaked=false/10. **Two nits:** (a) the embedder consumption gate reports `pass=true` while `pred_2_coverage=False` on 5/10 tasks — a flat-dense fusion can never void a run (gate-logic review item, flagged in TIER3B); (b) documentation inconsistency: the ledger boilerplate + GT_TRIAL_AUDIT_SUMMARY say "no GRAPH_FAIL_MISSING_HANDOFF false-FAIL present on this run" while the certs show it IS present on all 10 (TIER3B/SECTION4 are correct) — an audit-text error, not a product bug. |

Cross-layer (not a layer, but real): `[gt-patch:loaded]` loader banner leaked into agent-visible
stdout at MSG 3 on **10/10** tasks (telemetry leak, carries no test/gold info, must move to stderr).

---

## Synthesis

The delivery plumbing is sound: every layer that is supposed to fire on PATH B fired on all 10 tasks,
the legitimacy layer is fully green (gates 10/10, the all-10 `GRAPH_FAIL_MISSING_HANDOFF` is the
documented pre-agent FALSE FAIL and reconciles against the witness on every task), and the two layers
that did not fire (L6, GT_VERIFY/L4) are silent for the documented path-design reason, not because
they are dead. But "every other layer working correctly" is NOT the honest answer. Three defects exist
beyond L1 and the L5 governor: (1) a **graph/render content-quality defect that surfaces through
L3b, L3, and L1 alike** — vendored/minified-JS edges (`astropy/extern/jquery/*.min.js`) and
builtin-shadow laundering (`isinstance → TableColumns.isinstance`, peaking at "1048 verified
caller(s) … preserve this interface") delivered as deterministic [WITNESS]/[CALLEE]/[CALLERS] facts on
4+ tasks — this is one substrate bug polluting three delivery surfaces, the stdlib-shadow class at the
consumer; (2) **scope's setup.py/build-noise firings** and its near-universal trailing character (its
one leading win, 13033, was consumption-ambiguous); (3) the **`[gt-patch:loaded]` stdout leak 10/10**.
The L5 verdict must be split to be fair: the scaffold_trap arm is the run's only clean nudge
conversion (13236) and is working; the failure_persisted arm is broken at the classification level (it
cannot tell an env failure from a hypothesis failure — 6 of 7 firings wrong, one plausibly reinforcing
the revert of a gold-equivalent edit on 10097), and the loop arm false-fired on its single outing.
Net: **L3b / L3 / scope / gates = working mechanisms carrying a shared content-pollution defect;
L5.failure_persisted (+loop) = broken; L4 / L6 / GT_VERIFY = correctly silent on this path but
unexercised (no positive evidence of health).**

*Sources: task_ledgers/<task>.md §"2026-06-10 PATH B trial - gt_trial.md §4+§5 audit"; <task>/scorecard.json;
GT_TRIAL_AUDIT_SUMMARY.md; SECTION4_SUMMARY.md; TIER3B_ARCHITECTURAL_CONFORMANCE.md; gt_gt.md §12 (L666–697);
graph_certificate.json + foundational_gate_report.json re-verified directly on all 10 tasks.*
