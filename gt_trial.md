# gt_trial.md — The Live-Run Protocol (follow EVERY live run, no exceptions)

> Every live GT run (OpenHands / mini-swe-agent / DeepSWE-pier) MUST follow this, top to bottom.
> It exists because a ~12-hour, 12-run session (arviz-devs__arviz-2413, run3..run12) measured
> **nothing**: the environment wasn't guaranteed full-stack, the eval harness wasn't wired (so no
> resolved verdict was ever obtainable), and the failure reason was host-invisible by construction
> — so every run returned the same null bit with no information gradient. **Never again.** If a
> step here cannot be satisfied, DO NOT RUN — fix the precondition first.
>
> Companion docs: `gt_gt.md` (architecture + the verification protocol this run is graded by),
> `CLAUDE.md` (two-stage methodology, deep-metrics rule, AGENT-OBSERVATION rule). Branch
> `gt-consensus-curation`.

---

## 0. The rule above the rules

A live run is a **TEST, not a debugger.** If the thing you want to learn is not **observable in
the run's host-visible output BEFORE you launch**, do not launch — make it observable first
(§2). Prove the logic **OFFLINE and DETERMINISTICALLY** (controlled inputs, real binary, exact
assertions — `scripts/drift/stage1_metrics.py` is the template) before any live run. **Stage 1
(deterministic correctness) before Stage 2 (flips).** A given task MAY NEVER FLIP — that is
irrelevant to whether GT is correct.

---

## 1. ENVIRONMENT — identical and FULL-STACK every time (arm the gates; abort on degrade)

Same correct environment on every run — **LSP enabled, embedder ON, FTS5 on, full stack** — or
the run **ABORTS** (no silent fallback → no confounded results). Arm ALL of gt_gt §7:

- `GT_REQUIRE_FTS5=1` — `nodes_fts` Go-built (`-tags sqlite_fts5`) + populated + a real MATCH returns rows; else `gt-index` aborts.
- `GT_REQUIRE_EMBEDDER=1` — a real embedder loads and yields **finite non-zero** vectors (semantic NOT silently zeroed); else raise.
- `GT_FORCE_ONNX_EMBEDDER=1` — both semantic halves on the **identical container ONNX surface** (e5-small-v2, no torch).
- `GT_REQUIRE_LSP=1` — the LSP server **launches** AND a real probe resolves (`method=='lsp_references'`, `latency>0`); else the wrapper raises (no 0ms confidence-filter fallback).
- `GT_REQUIRE_FULL_STACK=1` — per-task graph-base gate: `graph_exists, schema, fts5, edge_quality, data_flow enriched, assertions, lsp_enrichment, lsp_edges`; raises on any degraded dimension.
- `GT_FORBID_PREBUILT_GRAPH=1` — fresh in-container per-task index; refuses prebuilt/cross-run `graph.db`.

**Pre-flight asserts (host-visible):** graph `nodes>0` (no `nodes=0` like run6), `W_SEM>0`
(semantic actually on), LSP edges present, `git_commit` recorded. If any fails → abort, do not run.

---

## 2. PRECONDITIONS — before launching (ALL must hold)

- [ ] **Logic proven offline & deterministic** — same-input→same-output on the real binary, FP/TP/determinism asserted (Stage-1 harness). No live run substitutes for this.
- [ ] **The signal under test is HOST-OBSERVABLE** — per-layer `deliver/suppress (+reason)` lands in a host log the wrapper provably does NOT strip. NOT in-container stderr; NOT the stripped `__GT_STRUCTURED__` accumulator (that channel inherits the very bug it probes — the run7–12 lesson). Verify the observable exists by reading it once before relying on it.
- [ ] **EVAL HARNESS WIRED** — the run produces a **RESOLVED verdict** (Microsoft SWE-bench-Live `run_evaluation` → `report.json`, `--namespace starryzhang`). A run with no verdict instrument is unfalsifiable → do not run. (CARDINAL: never write a custom eval.)
- [ ] **`/tmp/gt_debug` cleared** — no stale belief-ledger recall across runs (the run4→run5 stale-flood recall).
- [ ] **Frozen baseline on disk** for pairing — `.claude/reports/full300_baseline_ohdeepseek_20260531/FINAL_resolved_300_20260531.json` (87/300). **NEVER re-run the baseline.**
- [ ] **Deep 8-dp logging armed** (CLAUDE.md): per-layer eligible/emitted/suppressed+reason, `rendered_tokens`, utilization, `action_count`, `first_edit_action`, `gold_edited`, tokens, timing, and the **RAW delivered text** from `output.jsonl`.

---

## 3. THE RUN

- One task (or a batch) **GT-on**, armed environment, paired against the frozen baseline.
- **ALWAYS STREAMED, NEVER BACKGROUND.** Default surface is **GitHub Codespaces**; only if
  explicitly told use **gcp / other**, and then that surface — but ALWAYS streamed live
  (`tail -f` the host log), never launched-and-stopped/backgrounded. The point of streaming is
  to catch a mistake (degraded env, wrong path, leakage, loop) **as it happens**, mid-run — not
  in a post-mortem. If you cannot watch it live, do not launch it.
- Persist all deep artifacts (`gt_run_summary`, `gt_layer_events`, `gt_deep_metrics`, `output.jsonl`, `report.json`) before the run counts as done.

---

## 4. EVALUATION — mandatory after EVERY run (a run is not "done" without this)

1. **Resolved verdict** from the official eval, **paired** vs the frozen baseline (Wilcoxon /
   sign-test on per-task delta — never avg-subtraction). flip = GT-on resolves a baseline=NO id;
   regression = GT-on fails a baseline=PASS id.

2. **Spawn a VERIFIER AGENT to evaluate the trajectory against `gt_gt.md`.** The agent **READS
   `output.jsonl` FULLY and CHRONOLOGICALLY (the AGENT-OBSERVATION rule) — it does NOT grep.** It
   scores every gt_gt gate, with raw quotes:
   - **DELIVERED** — payload appears in the agent's raw observation text (not telemetry/event counts).
   - **CORRECT** — claims match ground truth, AND **LEAKAGE CHECK**: GT surfaced **NO test names /
     FAIL_TO_PASS / assertions** (the run12 finding: `test_plot_hdi() [test]` + `Verify: pytest
     …::test_plot_hdi` was surfaced 6× and the agent grepped it — a flip obtained that way is
     benchmaxxing, not a GT win). Any leaked test name = CORRECT fails.
   - **CONSUMED** — the agent acted on it (and NOT on a leak). Zero reaction = inert.
   - **FAIR PROBE** — GT caused it, vs the agent self-localizing from the issue traceback.
   - **RIGHT TRAJECTORY** — correct context → consumed → reasoned through → correct fix FOR THAT REASON.
   **VERDICT GATE:** say a layer "works" only when DELIVERED + CORRECT + CONSUMED hold on a fair
   probe. Otherwise state exactly which of {delivered, correct, consumed} passed. "Delivered" alone
   is reported as **"delivered; correctness unverified"** — NEVER "works".

3. **Lead the report with the TRAJECTORY finding, not pass/fail.** "Resolved" is a footnote to
   "the trajectory was right."

---

## 5. METRICS — the per-run scorecard (COMPUTE + SHOW + STORE for every run)

Manager's rule: **no run is reported without this scorecard**, filled from `output.jsonl` (raw
agent observation) + the official eval + the paired frozen baseline — NEVER from telemetry/event
counts alone. Every numeric value at **8 decimal places**. Store to
`.claude/reports/runs/<ts>__<task>/scorecard.json` AND print the table. The scorecard exists to
keep **"GT caused it"** separate from **"it resolved"** — conflating them is how luck gets counted
as a win.

### Tier 1 — OUTCOME (ground truth)
| metric | meaning |
|---|---|
| `resolved` | official eval verdict (FAIL_TO_PASS pass) — the ONLY success bit |
| `baseline_pass` | is this id in the frozen `resolved_ids` (87/300) |
| `flip` | `resolved AND NOT baseline_pass` — the prize |
| `regression` | `NOT resolved AND baseline_pass` — the harm |
| `per_task_delta` | +1 flip / −1 regression / 0 — feeds the paired Wilcoxon across tasks |

### Tier 2 — CAUSALITY (did GT cause it, or luck/self-solve) — gt_gt gates, 0/1, from output.jsonl
| metric | meaning |
|---|---|
| `delivered` | GT payload in the agent's RAW observation text |
| `correct` | claims match ground truth (caller accuracy) AND **zero leakage** |
| `consumed` | agent referenced/acted on GT content after delivery (and not on a leak) |
| `fair_probe` | the issue did NOT pre-localize the gold (GT caused, not self-localized) |
| `right_trajectory` | correct ctx → consumed → reasoned → correct fix FOR THAT REASON |
| `gt_caused` | `AND(delivered, correct, consumed, fair_probe, right_trajectory)` |

Verdict logic: **`gt_caused AND flip` = the only real GT win.** `gt_caused AND NOT flip` = right
trajectory / stochastic miss = still a GT win (context was correct). **`flip AND NOT gt_caused` =
luck/self-solve = NOT a GT win — do not count it.**

### Tier 3 — LOCALIZATION (did GT point at the gold)
`gold_file_reached` (GT brief named the gold file) · `first_gold_rank` (rank in GT's list, or
"abstain") · `gold_edited` · `first_edit_action` · `edit_to_gold_action`.

### Tier 4 — NON-HARM / EFFICIENCY (Cursor mentality, paired vs baseline)
`action_count` (+Δ) · `first_edit_latency` (+Δ) · `unique_files_viewed` (+Δ — did GT reduce
wandering or open a new exploration tree) · `looped_stuck` (did GT make every obs unique →
stuck-detector dead → loop) · `gt_injected_tokens`. Any of these worse than baseline without an
outcome gain = **regression until proven otherwise** (CLAUDE.md).

### Tier 5 — PER-LAYER DELIVERY (L1 brief / L3 post_edit / L3b post_view / consensus)
per layer: `eligible` / `emitted` / `suppressed (+reason)` / `rendered_tokens` / `consumed (0/1)`.

### Tier 6 — LEGITIMACY GATES (any failure VOIDS the run — do not report it as a result)
`env_full_stack` (LSP + embedder ONNX with W_SEM>0 + FTS5 + full-stack gates all GREEN) ·
`test_names_leaked` (count GT surfaced to the agent — **MUST be 0**) · `fail_to_pass_leaked`
(GT surfaced the grader test — **MUST be false**) · `no_gold_labels` (no task IDs / gold /
FAIL_TO_PASS in product logic).

### Tier 7 — COST
`llm_in` / `llm_out` / `llm_cost` / `gt_injected_tokens` / `wall_clock_s` / `time_to_first_edit` /
`time_to_gold`.

**The one-line a manager reads:** `gt_caused_flip` (bool) — and when it's false, the scorecard
shows exactly which gate broke (delivered? correct? consumed? fair? or just no-flip-but-right).

---

## 6. LESSONS BAKED IN (from run3..run12 — do not repeat)

- A 13-min live run is the WORST instrument for a silent-emission defect. Bisect with an
  observable; isolate the cheapest, most-observable check first.
- Never route a diagnostic through a channel the consumer strips (`__GT_STRUCTURED__` on the
  router_v2 path) — it inherits the exact blindness it was meant to cure.
- "Delivered" ≠ "correct" ≠ "consumed" ≠ "works". Verify correctness + leakage from the agent's
  observation, never from telemetry, event counts, or grep.
- Don't fixate on one task across N runs; "this task may never flip" is fine — Stage 1 is proven
  deterministically, not by a flip.
- Change one variable at a time — but only if that variable has a per-variable host-observable.
  One-variable-at-a-time without an observable is worthless.

---

*End — gt_trial.md. Grade against `gt_gt.md`. Methodology in `CLAUDE.md`.*
