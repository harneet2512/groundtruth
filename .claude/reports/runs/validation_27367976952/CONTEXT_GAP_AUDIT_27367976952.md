# Context Gap Audit — validation_27367976952

Date: 2026-06-11  
Run: `validation_27367976952` (0/9 resolved)  
Method: Chronological trajectory read from `mini-swe-agent.trajectory.json` + `mini-swe-agent.txt`; compare GT delivery to agent next actions and gold-fix needs. Builds on `GT_TRAJECTORY_GAP_AUDIT_INITIAL.md`.

**CP004 note:** The validation run **already delivered** `post_view` `[WITNESS]` blocks on these tasks (24–9 blocks each). CP004 fixes the **oracle relevance gate** path that suppressed evidence when `_oracle_focus()` is empty (reproduced in `tests/test_verified_adapter.py` fake-env). CP004 does **not** retroactively change this run's trajectories; it closes a regression/harness gap for view-without-anchors.

---

## adaptix-name-mapping-aliases

| Field | Value |
|---|---|
| Language | Python |
| Resolved | No (reward 0) |
| Agent steps | 177 |
| Trajectory | `deepswe-full-adaptix-name-mapping-aliases/jobs/2026-06-11__18-24-52/adaptix-name-mapping-aliases__VmAoCGb/agent/` |

### Delivery timeline

1. **Turn 1:** `<gt-task-brief>` — morphing/name-layout files surfaced (`name_mapping.py`, retort facade, tests).
2. **Turn 13 (first `post_view`):** After viewing morphing code, GT appended `[WITNESS]` caller/callee edges (`name_mapping`, `append_with`, `field_to_loc`).
3. **Next 3 agent actions:** Turn 14 — assistant reads `name_mapping` module; turn 15 — `cat` name_mapping source; turn 16 — reads base types (`Key`, `KeyPath`).

### Trajectory finding

**Right localization, weak obligation/spec trajectory.** GT named the correct morphing subtree and the agent edited those files. Consumption after first witness is aligned (agent deep-dives `name_mapping`). Failure is post-localization: 179-step burn, multi-file edits, reward 0 — likely spec compliance / over-broad implementation, not missing file hit.

### Context gap

| GT sent | Needed for correct fix |
|---|---|
| File-level pins + caller witnesses on morphing symbols | **Alias mapping contract**: which request types accept aliases, how `NameMappingRequest` maps to layout keys, test obligations for alias round-trips |
| Test-tree symbol names in blocks (noise) | Issue-shaped **obligation vector** (clause → edited symbol → test status) |

### CP004 hypothesis

**Low impact on this task.** Evidence already delivered at turn 13. CP004 would not change the agent's turn-14 decision (already following GT file pins). Remaining gap is Stage-2 obligation/feature-shape context (§14), not witness suppression.

---

## fd-deterministic-multi-key-sorting

| Field | Value |
|---|---|
| Language | Rust |
| Resolved | No (reward 0) |
| Agent steps | 155 |
| Trajectory | `deepswe-full-fd-deterministic-multi-key-sorting/jobs/2026-06-11__18-21-42/fd-deterministic-multi-key-sorti__i3JNJuh/agent/` |

### Delivery timeline

1. **Turn 1:** Brief + issue context (CLI/walk/sort surface).
2. **Turn 5 (first `post_view`):** After `src/main.rs` view — `[WITNESS]` on `exit`, `gen_completions` (stdlib-adjacent noise mixed with real edges).
3. **Next 3 actions:** Turn 6 — empty assistant; turn 7 — tool output truncated warning; turn 8 — assistant pivots to `cli.rs` / `main.rs` `run`.

### Trajectory finding

**Partially right file set, self-driven feature build.** GT named `main.rs`, `walk.rs`, `cli.rs`; agent edited `cli.rs`, `config.rs`, `main.rs`, `sort.rs`, `walk.rs`. Local tests passed; verifier reward 0. Trajectory is feature implementation, not clearly GT-caused — agent explored CLI/sort/walk independently after brief.

### Context gap

| GT sent | Needed for correct fix |
|---|---|
| Graph witnesses on `main.rs` (some garbage `write`/`drop` name_match targets) | **Multi-key sort semantics**: deterministic ordering across keys, flag surface on CLI, interaction with `walk` pipeline |
| LSP **0 effective conversions** (`project_ready=false`, 234/234 failed) | Type-aware callee resolution for method calls in `walk.rs` / `sort.rs` |

### CP004 / CP005 hypothesis

- **CP004:** Witnesses already present at turn 5; waiver fix irrelevant to this run's delivery.
- **CP005:** **High impact** — Rust LSP product readiness + dep mounts should improve edge quality on re-run; may sharpen `walk`/`sort` witnesses.

---

## katex-multicolumn-array-spans

| Field | Value |
|---|---|
| Language | JavaScript |
| Resolved | No (reward 0) |
| Agent steps | 123 |
| Trajectory | `deepswe-full-katex-multicolumn-array-spans/jobs/2026-06-11__18-22-43/katex-multicolumn-array-spans__YJh9eHj/agent/` |

### Delivery timeline

1. **Turn 1:** Brief pins `src/environments/array.ts`, parser/dom neighborhood.
2. **Turn 87 (first `post_view`):** Late first witness — only **3** `post_view` blocks total in run.
3. **Next 3 actions:** Turn 88 — assistant reasons about vertical separator suppression; turn 89 — `find` CSS paths (tangential); turn 90 — empty assistant.

### Trajectory finding

**Wrong trajectory on verify + leakage.** Agent edited `array.ts`, `functions/multicolumn.ts`, `parseNode.ts` but `<gt-verify>` leaked exact Jest name `it: should return one group, not a fragment` (lines ~3352, ~7677 in `mini-swe-agent.txt`). Under `gt_trial.md`, correctness gate **fails** regardless of resolve. Patch polluted `package-lock.json` / `yarn.lock`.

### Context gap

| GT sent | Needed for correct fix |
|---|---|
| File pins to array/multicolumn | **Span geometry contract** for multicolumn inside `array` environments; how `\multicolumn` interacts with column descriptions |
| Leaked exact test name in `<gt-verify>` | Category-level verify hint without hidden test string (fixed in CP002 for current code; this run predates/fix not in deployed image) |

### CP004 hypothesis

**Medium for late witnesses only.** First witness at turn 87 is very late — agent already built wrong separator model. CP004 does not address late firing; separate phase/timing controller gap. B3 no-leak fix (CP002) addresses verify leakage on **future** runs.

---

## abs-module-cache-flags

| Field | Value |
|---|---|
| Language | Go |
| Resolved | No (reward 0) |
| Agent steps | 125 |
| Trajectory | `deepswe-full-abs-module-cache-flags/jobs/2026-06-11__18-22-15/abs-module-cache-flags__AfqevMi/agent/` |

### Delivery timeline

1. **Turn 1:** Brief led with terminal/install/repl (generic ABS surface).
2. **Turn 9 (first `post_view`):** After early repl exploration — `[WITNESS]` on repl package.
3. **Next 3 actions:** Turn 10 — empty assistant; turn 11 — `cat` repl imports; turn 12 — empty assistant.

### Trajectory finding

**Self-localization dominated.** Agent self-navigated to `evaluator/functions.go` and `repl/repl.go` despite generic brief lead. GT delivered 17 events / 9 post_view blocks but graph_map count 0 in metrics — agent did not consume a structured graph map. LSP Go: `project_ready=false`, 0 conversions.

### Context gap

| GT sent | Needed for correct fix |
|---|---|
| Repl/install hub bias in brief | **Module cache flag semantics** in evaluator — which objects cache module state, flag lifetime, REPL vs evaluator boundary |
| No useful LSP edges | Import-verified callers into gold evaluator symbols |

### CP004 / CP005 hypothesis

- **CP004:** Witnesses delivered; would not fix hub-bias brief or missing obligation context.
- **CP005:** **High impact** on re-run — Go dep-store mount + `LSP_PRODUCT_READY` gate should enable effective conversions on ABS graph.

---

## Cross-task summary

| Task | First witness turn | Trajectory | Dominant gap | CP004 would change run? |
|---|---:|---|---|---|
| adaptix | 13 | Right files, wrong spec depth | Obligation / alias contract | No (already delivered) |
| fd | 5 | Feature build, local pass / reward fail | Multi-key sort semantics + LSP | No |
| katex | 87 | Verify leakage + late context | Spec + no-leak verify (CP002) | Unlikely |
| abs | 9 | Self-localized past generic brief | Module-cache obligation + Go LSP | No |

**Product conclusion:** Validation run proves **delivery often works**; failures are **consumption, obligation shape, LSP product readiness, verify leakage, and patch hygiene** — addressed by CP002 (leak), CP005–CP010 (this session), not primarily CP004 on these four trajectories.

## Evidence paths

- Initial audit: `GT_TRAJECTORY_GAP_AUDIT_INITIAL.md`
- Per-task deep metrics: `deepswe-full-*/gt_deep_metrics_*.json`
- Mini trajectories: `deepswe-full-*/jobs/*/*/agent/mini-swe-agent.trajectory.json`
