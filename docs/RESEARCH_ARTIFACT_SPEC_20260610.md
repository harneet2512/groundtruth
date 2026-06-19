# RESEARCH ARTIFACT SPEC — canonical run-bundle schema (2026-06-10)

Status: PREP (not yet enforced by CI). Branch: `gt-trial`.
Analyzer: `scripts/research/build_run_report.py` (stdlib-only). Tests: `tests/test_run_report_builder.py`.

## 0. Purpose and audience bar

Every GroundTruth run (smoke, canary, sweep, paired experiment) must leave behind ONE artifact
bundle from which an external reviewer at a frontier lab can independently verify the claim:
"a deterministic, verified codebase-intelligence substrate measurably changes coding-agent
behavior." That means three properties, in this order:

1. **Honesty** — every number traces to a file on disk (task id + source file + line where
   applicable). A field that was not collected is reported as `NOT COLLECTED`, never imputed,
   never defaulted to 0, never dropped silently.
2. **Rigor** — paired statistics only where per-task pairing exists; bootstrap CIs on per-task
   aggregates; cells with n<5 report raw counts (`k/n`), never rates or percentages.
3. **Reproducibility** — an experiment card pins everything needed to re-run: commit SHAs,
   image digests, model + exact sampling params, dataset manifest SHA, task-set SHA, platform.

No qualitative language ("strong", "impressive", "dramatically") appears in any generated
artifact. Verdicts are restricted to the enumerated vocabularies defined below.

---

## 1. Input: the three run-dir layouts the analyzer accepts

The analyzer sniffs the layout; precedence is (A) → (B) → (C). Detection rules are exact and
tested.

### (A) `vm-sweep` — the VM runner OUT_DIR (`scripts/vm/gt_agent_run.sh`)

Detected by: any `<run>/<instance_id>/row.json` (depth 2), or `<run>/row.json` (single-task dir).

```
OUT_DIR/
  tasks.tsv                          # id \t language \t image
  AGENT_SWEEP_REPORT.md
  <instance_id>/
    row.json                         # per-task row — REQUIRED (schema §1.1)
    trial_output.log                 # wrapper log: [GT_META] witness lines live here
    outcome.json / outcome.txt       # deepswe_outcome.py classification
    gt_deep_metrics_<id>.json        # 8-dp deep record (gt_deep_metrics.v2)
    delivered_instruction.txt        # the brief text the agent received (host copy)
    gt/                              # substrate artifacts (the 8-artifact contract):
      graph.db                       #   the ONE graph
      run_manifest.json              #   graph_hash + provenance
      graph_certificate.json         #   nodes/edges/det_pct/resolution_method_dist
      lsp_certificate.json           #   resolved/residual/verified/corrected/deleted
      embedder_certificate.json
      foundational_gate_report.json
      runtime_context.json
      brief.txt                      #   the delivered brief
      issue.txt / gt_issue_anchors.json / gt_scope_files.txt / gt_lsp_metrics.txt
    pier/jobs/<ts>/<trial__hash>/    # the pier trial (layout B nested)
```

#### 1.1 `row.json` (written by `gt_agent_run.sh` step (g))

```json
{
  "instance_id": str, "language": str, "image": str, "model": str, "pier_config": str,
  "failure_class": str,          // runner-level: "" | TASK_DIR_MISSING | TASK_IMAGE_PULL_FAIL |
                                 // DISK_LOW | SRC_EXTRACT_FAIL | GT_ISSUE_MISSING |
                                 // GT_RUN_PROOF_FAIL | GT_ARTIFACT_MISSING | PIER_TIMEOUT |
                                 // DEEPSWE_ADAPTER_FAIL | PIER_RUN_FAIL |
                                 // GT_ARTIFACT_NOT_CONSUMED | GRAPH_FAIL_HASH_MISMATCH
  "pier_rc": int, "proof_reused": bool,
  "outcome_class": str|null,     // deepswe_outcome.py: INFRA/GT/AGENT/RESOLVED/UNKNOWN
  "in_resolved_denominator": bool|null, "reward": num|null, "n_agent_steps": int|null,
  "exit_status": str|null, "gt_prebuilt_active": bool|null, "hook_hash_match": bool|null,
  "timings_s": {"task_pull": int, "proof": int, "agent": int, "substrate_pull": int},
  "task_repo_commit": str, "deepswe_bench_sha": str, "gt_git_commit": str,
  "substrate_digest": str, "run_id": str, "ts_utc": str
}
```

### (B) `pier-jobs` — a bare pier `jobs/` tree (DeepSWE / GTMiniSweAgent)

Detected by: `**/jobs/<ts>/<trial>/result.json` (searched at bounded depth: `jobs/*/*/`,
`*/jobs/*/*/`, `artifacts/*/jobs/*/*/`) when no `row.json` exists above it.

```
jobs/<YYYY-MM-DD__HH-MM-SS>/<trial__hash>/
  result.json                        # task_name, trial_name, task_checksum, config
                                     #   (model_name, agent import_path, env type), exception info
  config.json  trial.log  exception.txt
  agent/mini-swe-agent.txt           # the agent trajectory (canonical for this layout)
  verifier/reward.txt                # "0" | "1"
  verifier/test-stdout.txt
  artifacts/model.patch              # empty file == no patch
```

### (C) `gha-openhands` — GHA artifact layout (OpenHands / SWE-bench-Live)

Detected by: `output.jsonl` at root, or a `gt_debug/` dir at root / `artifacts/*/`.

```
<run>/
  output.jsonl                       # OH trajectory (agent-observation source of truth)
  eval_result.json                   # official harness report (resolved_ids / unresolved_ids)
  scorecard.json                     # tier1_outcome / tier2_causality / tier6_legitimacy
  gt_debug/
    gt_deep_metrics_<task>.json      # gt_deep_metrics.v2 — primary per-task record
    gt_run_summary_<task>.json
    gt_layer_events_<task>.jsonl     # per-event: layer, emitted/suppressed, rendered_text
    gt_agent_events_<task>.jsonl
    cost.jsonl  payload.jsonl  l5_telemetry.jsonl
```

Optionally for ALL layouts: `task_ledgers/` (in the run dir or supplied via `--ledgers`) —
the per-task §4 audits (gt_trial.md §4 format). Ledger files are matched to a task when the
ledger filename stem is a substring of the instance id or vice versa.

---

## 2. Output: `RUN_REPORT/` — one bundle per run

```
RUN_REPORT/
  experiment_card.json        # Tier 3
  layer_effectiveness.md/.csv # Tier 2
  failure_taxonomy.md/.csv    # Tier 2
  token_economics.md          # Tier 2
  language_depth.md           # Tier 2
  behavioral_deltas.md        # Tier 2
  integrity_chain.md          # Tier 2
  TECH_REPORT_DRAFT.md        # Tier 1 (assembled from the above + TODO markers)
  tasks_normalized.json       # the normalized per-task records with per-field provenance
```

`tasks_normalized.json` is the machine-readable join surface: one record per task with every
extracted field AND a `provenance` map (`field -> source file path [:line]`). Anything not on
disk is the JSON string `"NOT COLLECTED"` (markdown) / `null` + an entry in `fields_missing`
(experiment card).

---

## 3. Tier 1 — narrative skeletons

### 3.1 Technical report (`TECH_REPORT_DRAFT.md`) — sections map 1:1 to analyzer outputs

| # | Section | Filled from | Human TODO? |
|---|---|---|---|
| 1 | Problem statement | static skeleton text (claim + non-claims) | TODO: tighten to this run's question |
| 2 | Architecture | reference to `gt_gt.md` §11–§13 (state of record) — never restated | no |
| 3 | Methodology | experiment_card.json (model, params, task set, harness, pairing design) | TODO: pairing rationale |
| 4 | Results | behavioral_deltas.md + token_economics.md + layer_effectiveness.md | TODO: interpretation |
| 5 | Failure taxonomy | failure_taxonomy.md | TODO: per-class exemplar narrative |
| 6 | Substrate depth | language_depth.md | no |
| 7 | Integrity & legitimacy | integrity_chain.md + scorecard tier6 fields | no |
| 8 | Limitations | auto-generated from `fields_missing` + n-per-cell warnings | TODO: design limitations |
| 9 | Roadmap | static skeleton | TODO |

### 3.2 Executive brief (2 pages max) — skeleton embedded at the top of TECH_REPORT_DRAFT.md

1. The claim under test (one sentence; from experiment card `claim` field if present, else TODO).
2. Design (n tasks, languages, model, paired-vs-unpaired — from experiment card).
3. Headline numbers — ONLY numbers that exist in Tier 2 artifacts; each carries its source
   pointer. No number may appear here that does not appear in a Tier 2 table.
4. What failed (top 2 failure classes by count).
5. What this does NOT show (auto: unpaired comparisons present, n<5 cells, missing fields).

---

## 4. Tier 2 — evidence artifacts (all generated, all traceable)

### 4.1 Layer-effectiveness matrix (`layer_effectiveness.md` / `.csv`)

Rows: layer × language. Layers: L1, L3_router_v2, L3b, L4, L5, L5b, L6, consensus (only those
observed in data; never a fixed list padded with zeros).

Columns and their ONLY admissible sources:

| Column | Source | Notes |
|---|---|---|
| tasks | count of tasks with that language | |
| eligible / emitted / suppressed | `gt_deep_metrics.per_layer.<L>` or aggregated `gt_layer_events` | event counts, not task counts |
| rendered_tokens_total | same | |
| utilization_score | `gt_deep_metrics.per_layer.<L>.utilization_score` | distribution (min/med/max) when n≥2 |
| delivered_tasks | tasks where emitted>0 for that layer | count `k/n`, rate only when n≥5 |
| correct / consumed / gt_caused | task-level `scorecard.tier2_causality` ONLY (or a parsed ledger verdict) | per-layer correctness does NOT exist in telemetry — when only task-level causality exists it is reported in a separate task-level row, never attributed to a layer |
| exemplar | pointer: `gt_layer_events_<task>.jsonl:<line>` + first 120 chars of `rendered_text`, or `task_ledgers/<task>.md` | verbatim, with file:line |

Hard rule (AGENT-OBSERVATION rule, `.claude/CLAUDE.md`): emitted/eligible counts are labeled
`telemetry` in the table header; `consumed`/`correct`/`gt_caused` are labeled
`agent-observation (scorecard/ledger)`. The two are never summed or mixed in one column.

### 4.2 Failure taxonomy (`failure_taxonomy.md` / `.csv`)

Every non-resolved task gets exactly one class. Classification rules, applied in this order
(first match wins), each rule names the signals it consumes:

| Class | Rule (signals consumed) |
|---|---|
| `infra` | runner `failure_class` in {TASK_DIR_MISSING, TASK_IMAGE_PULL_FAIL, DISK_LOW, SRC_EXTRACT_FAIL, GT_ISSUE_MISSING, GT_RUN_PROOF_FAIL, GT_ARTIFACT_MISSING, PIER_TIMEOUT, PIER_RUN_FAIL, DEEPSWE_ADAPTER_FAIL} OR `outcome_class`=INFRA OR pier `exception.txt` non-empty OR no trajectory file exists |
| `step-exhausted` | `exit_status` indicates step/iteration limit, OR `n_agent_steps` ≥ declared max (when max is on disk), OR no patch AND trajectory truncated at max_iter |
| `localization-miss` | unresolved AND L1 emitted AND causality says L1 `correct=0` (scorecard tier2 or ledger L1 verdict `CORRECT=NO`) |
| `delivered-not-consumed` | unresolved AND `tier2_causality.delivered`≥1 AND `consumed`=0 |
| `consumed-wrong-fix` | unresolved AND `consumed`≥1 AND patch present |
| `UNCLASSIFIED(missing-signals)` | none of the above decidable from on-disk signals — the analyzer states WHICH signal was missing |

`resolved` tasks are listed separately, split by `gt_caused` (scorecard) when present —
per the trajectory-over-resolved rule, a resolve without causality evidence is reported as
`resolved (causation NOT COLLECTED)`, never as a GT win.

### 4.3 Token economics (`token_economics.md`)

Per task: `gt_injected_tokens_total`, `llm_tokens_in/out/cached`, `llm_cache_hit_tokens` /
`llm_cache_miss_tokens`, `llm_cost_usd` (8 dp), `gt_injection_overhead_pct`, `action_count`,
outcome. Aggregates: median + bootstrap 95% CI (n≥5) of cost-per-task and
injection-overhead-pct; cache-hit economics = cached/(cached+miss) when both collected.
NO per-token pricing math is invented: cost is read from `efficiency.llm_cost_usd` only.

### 4.4 Per-language depth profile (`language_depth.md`)

Per language, from substrate artifacts already persisted (graph_certificate / gt_gates_deep /
gt_deep_metrics):

- fact-ratio: `det_pct` (deterministic edges / CALLS edges) and `verified_edge_ratio`
- resolution-method distribution (import / same_file / verified_unique / lsp / type_flow /
  impl_method / inherited / name_match)
- LSP promotion: `lsp_certificate.resolved` / (`resolved`+`residual`); `lsp_no_op_valid` and
  its reason are carried verbatim (a valid no-op is NOT a failure)
- evidence hit-rate: FTS5 probe ok, assertions linked / total, enriched-bases coverage
- embedder: dim, nonzero, cos_related vs cos_unrelated when present

### 4.5 Behavioral deltas (`behavioral_deltas.md`)

Distributions (min / p25 / median / p75 / max — never bare means) of: `action_count`
(n_agent_steps), `first_edit_action`, `edit_to_gold_action`, wall-clock per task.
Paired deltas appear ONLY when `gt_metrics_delta_<task>.json` files (or an explicit
`--baseline` run dir with matching instance ids) exist; the pairing key is instance_id.
Comparisons against the frozen baseline file
(`.claude/reports/full300_baseline_ohdeepseek_20260531/FINAL_resolved_300_20260531.json`)
are PAIRED on resolution (id ∈ resolved_ids) but UNPAIRED on behavior, and are labeled so.

### 4.6 Per-task integrity chain (`integrity_chain.md`)

One row per task — the custody chain from substrate to trajectory:

| link | source |
|---|---|
| substrate_digest | row.json / run_contract.json |
| gt_git_commit | row.json / gt_deep_metrics.git_commit |
| task_repo_commit | row.json / run_manifest |
| graph.db sha256 | computed by the analyzer over the on-disk file |
| graph_hash (post-LSP) | run_manifest.json / `[GT_META] graph_witness` line in trial log |
| witness | `gt_prebuilt_active` + `hook_hash_match` (row.json / trial log grep is NOT used — only structured fields) |
| trajectory file + sha256 | computed by the analyzer over output.jsonl / mini-swe-agent.txt |
| ledger | task_ledgers/<task>.md present? |

Any broken/missing link prints `NOT COLLECTED` — the chain is reported per-link, never as a
single boolean.

---

## 5. Tier 3 — reproducibility

### 5.1 `experiment_card.json` schema

```json
{
  "schema": "gt_experiment_card.v1",
  "run_id": str|null,
  "layout": "vm-sweep" | "pier-jobs" | "gha-openhands",
  "claim": str|null,                       // human-supplied via --claim; null otherwise
  "commits": {"gt_git_commit": str|null, "task_repo_commits": {id: sha},
               "deepswe_bench_sha": str|null},
  "images": {"substrate_digest": str|null, "task_images": {id: ref}},
  "model": {"name": str|null, "params": {…}|null},   // params verbatim from pier config.json
                                                      // or run config; NEVER reconstructed
  "dataset": {"manifest_sha256": str|null, "task_ids": [...], "task_ids_sha256": str,
               "n_tasks": int, "languages": {lang: n}},
  "cost_usd_8dp": str|null,                // sum of per-task llm_cost_usd, 8 dp, only if all
                                           // tasks collected cost; else null + fields_missing
  "wall_clock_s": num|null,
  "platform": {"analyzer_host": str, "run_host": str|null},
  "replay": {"command": str|null, "contract": "one command, same commits+digests+params+task set"},
  "fields_missing": [str, ...],            // every spec field absent from this run's data
  "generated_utc": str, "analyzer_version": str
}
```

### 5.2 One-command replay contract

A run is replayable iff the card has non-null: gt_git_commit, substrate_digest (or image
refs), model.name + model.params, task_ids, and the runner entrypoint. The card's
`replay.command` is the literal command (e.g.
`OUT_DIR=… MODEL=… GT_SUBSTRATE_DIGEST=… scripts/vm/gt_agent_run.sh --tasks <ids>`), emitted
only when all of its variables are known; otherwise `null` and the missing variables are
listed in `fields_missing`.

---

## 6. Statistical rules (binding for every artifact)

1. **Paired tests only where paired data exists.** Per-task deltas (same instance id, two
   arms) → Wilcoxon signed-rank / sign test. No pairing → no paired test, and the table is
   labeled `UNPAIRED`.
2. **Bootstrap CIs** (percentile, ≥2000 resamples, fixed seed recorded in the artifact) on
   per-task aggregates when n≥5.
3. **n<5 per cell → counts, not rates.** Print `k/n`, never `%`. (No-n=2-claims rule.)
4. **Aggregate-vs-leaderboard comparisons are labeled `UNPAIRED, different harness`** and
   carry no significance claim.
5. **8-dp precision** for every stored numeric (constitution mandate); display may shorten,
   storage may not.
6. **Distributions, not means**, for behavioral metrics (action counts are heavy-tailed).

## 7. Honesty rules (binding)

1. A field absent from input NEVER appears as a number in output (`NOT COLLECTED` / null).
   Enforced by test: synthetic run dirs with deleted fields must not produce numbers.
2. Telemetry vs agent-observation labeling per §4.1. "Emitted" is never reported as
   "delivered to agent" without an output.jsonl/ledger-grounded source.
3. `resolved` without causality evidence is never counted as a GT win (§4.2).
4. Every generated table carries a `sources:` footer listing the exact input files consumed.
5. The analyzer never reads gold patches, FAIL_TO_PASS, or task metadata beyond ids/language.

---

## 8. Fields the NEW runners must emit (gaps observed in existing on-disk runs)

To be kept current by running the analyzer on each new run and copying its `fields_missing`.
Initial list from validation against `.claude/reports/runs/20260606_gha_run1__conan-17123`
(GHA layout) and the 2026-06-03 pier capture — see the analyzer report:

- `model.params` (temperature/top_p/max_tokens) — GHA layout has no config snapshot; pier
  result.json carries model_name but not sampling params → runner must persist the resolved
  LLM config verbatim.
- `edit_to_gold_action`, `gold_edited`, `edited_files` — mandated by the constitution's deep-log
  block; absent from gt_deep_metrics.v2 `agent` section as written today.
- per-task wall-clock + time-to-first-edit in seconds (GHA deep metrics has no `timings`;
  VM row.json has them).
- `gt_metrics_delta_<task>.json` (paired deltas) — absent everywhere; required for §4.5.
- dataset/manifest SHA in the run dir itself (run_contract.json exists only in proof runs).
- `max_iter` persisted in a structured field (only inferable from layer events today).
- suppression reasons aggregated per layer (present per-event in layer events; keep).
- resolved verdict in a structured per-task field for VM/pier layouts (reward.txt exists;
  keep both).
- trajectory file SHA256 emitted at run time by the runner itself (analyzer recomputes; the
  runner-side hash closes the copy-tamper window).
