# TRAJECTORY AUDIT — rust · `boa-hierarchical-evaluation-cancellation` (run 27290157847, GT-on, deepseek-v4-flash, digest d30f34b4) — 2026-06-10

**Source of truth:** `jobs/2026-06-10__16-46-32/boa-hierarchical-evaluation-canc__hKkabqV/agent/mini-swe-agent.trajectory.json` — **351 messages read chronologically in full** (not grepped), reconciled against `gt_artifacts/*.json` certs and `verifier/test-stdout.txt`. Full §4 per-component tables: `task_ledgers/boa-hierarchical-evaluation-cancellation.md`. §5 scorecard: `boa-hierarchical-evaluation-cancellation/scorecard.json`.

---

## THE HEADLINE (Tier 3b first, per the brief)

**The deepest graph of the four languages reached the agent and was almost entirely ignored. gt_caused = FALSE · consumed ≈ 0 · fair_probe = FALSE · resolved = NO (reward 0.00000000).**

The rust substrate really is the deepest of the run — 32,520 call edges at 66.93% deterministic, `impl_method=9542`, `type_flow=6046`, FTS5 probe-ok, embedder GREEN (gte-modernbert 768d, `effective_w_sem=0.25`, 281/283 nonzero). That depth WAS delivered: the L1 brief + ~25 `<gt-scope>`/`<gt-evidence>`/`<gt-contract>` injections all rendered resolved-caller facts mined from it. But **graph depth ≠ graph used.** This is a greenfield feature task whose issue text names every required public entry point (`Context::{new_evaluation_handle, …}`, `Script::evaluate_with_evaluation`, `Module::{…}`, `EvaluationHandle::{…}`). The agent's very first targeted action is a grep of those issue-given names:

> [4] `find . -type f -name "*.rs" | xargs grep -l "evaluation_handle\|EvaluationHandle\|eval_with_evaluation\|cancel_with_reason\|cancellation_reason"` → empty
> [6] `grep -l "is_cancelled\|cancellation\|CancellationToken"` → job.rs, abort/mod.rs, …
> [8] `cat ./core/engine/src/job.rs`

job.rs is GT's #1 candidate — but it is also hit #1 of the agent's own grep. From [62] onward every decision in the 100-message implementation loop is driven by the agent's own greps and the **rustc compiler** ([113]→[231]: 16 errors → 0, each fix quoting a cargo error, never a GT line). Not one turn cites a GT payload. The single plausible GT consumption in 174 steps is the L5 nudge:

> [51] `<gt-nudge reason="scaffold_trap"> GT: 25+ actions and no source-file edit yet …` → [52] "I now have a good understanding of the codebase. Let me start implementing the EvaluationHandle system"

— premise factually true, behavior change one turn later, coincidence not excludable.

**So the rust trajectory "makes sense" only in the deflationary sense:** GT delivered correct-ish, honest, harmless context on a true map, at the right moments — and the agent neither needed it for navigation (the issue pre-localized) nor used it for implementation (the compiler out-competed it). Right trajectory for GT-as-cause: NO.

---

## Why it did NOT resolve (reward 0.0 vs the deepseek board 0.790) — read from the verifier, not guessed

`verifier/test-stdout.txt`:
1. **Step 3 baseline: exit 101** — `rust-lld: error: undefined hidden symbol` storm inside `libboa_engine.rlib` (boa_parser/bitflags/temporal_rs internals). The incremental build state in `/app/target` was corrupted — consistent with the agent's **six `timeout 60/120 cargo test …` SIGKILL-mid-build cycles** ([243]–[333]), which kill rustc/lld mid-write. Even the unmodified baseline could no longer link.
2. **Step 4 new tests: exit 101** — same link storm **plus a real API defect**: `error[E0596]: cannot borrow 'context' as mutable` at the hidden test's `let child = context.new_child_evaluation_handle(&parent);`. The agent declared the handle constructors `&mut self` ([80]: `pub fn new_evaluation_handle(&mut self) -> …`); the hidden test calls them on a non-`mut Context`. The grader test never compiled against the patch.
3. The agent **submitted having never observed one executed test.** [332]: *"The tests seem to pass but it's not printing results"* — after every `cargo test` invocation either timed out at 120s or printed only compile warnings. It shipped a 398-line greenfield feature on `cargo check` alone.

**Mandatory context-gap analysis (never "model failure"):** the deltas between what GT sent and what the correct fix needed —
- **No surface for "you are submitting with zero test evidence."** L5's `failure_persisted` is (correctly, post-2026-06-10) gated on an explicit test-FAILURE marker; a timeout/no-output is neither failure nor success, so GT stayed silent while the agent walked into the grader blind. This is a real product gap exposed on rust: an *absence-of-verification* governor does not exist.
- **Requirement 5 (cancel DURING execution) needs the VM run-loop / `RuntimeLimits` interrupt path** — GT never surfaced `vm/mod.rs` or `RuntimeLimits` (it was even visible in the agent's own read of context/mod.rs imports). The agent's check-only-at-entry design cannot satisfy mid-execution cancellation; the deep graph knew the call structure but the brief never connected "cancellation checkpoint" → VM loop.
- **GT's smartest pointer was wasted:** brief candidate #6 `core/runtime/src/abort/mod.rs — cancel, is_cancelled, reason` is Boa's AbortController — the exact AbortError/reason-lineage pattern the spec demands. Delivered once at rank 6 in turn 1, never re-surfaced, never visited.

---

## Did the 2026-06-10 fixes behave on rust? (asked explicitly)

| fix under test | verdict on this trajectory |
|---|---|
| `failure_persisted` classification gate | **HELD — 0 false fires.** Six failed/timeout `cargo test` observations and a 16-error compile loop produced zero "your hypothesis is likely wrong" steers. Correct-or-quiet honored. |
| `loop` nudge (command, normalized-observation) | **HELD — 0 fires, correctly.** The repeated `cargo test | tail` invocations varied in timeout/filter, so no proven no-new-state repetition. (Honest note: a looser detector might have caught the 6× timeout loop that corrupted the build dir — but firing there would have violated the new signature spec.) |
| `scaffold_trap` (unchanged, 4/5 TP in audit) | **1 fire, premise-true, plausibly consumed, harmless.** Borderline-FP in spirit (methodical greenfield exploration ≠ stuck), but it nudged toward editing exactly when the agent was ready to edit. |
| confident-wrong steer | **NONE observed** in 351 messages. |
| vendored/builtin pollution | **PRESENT — 1 clear instance:** [57] post_view evidence for `module/source.rs` cites `chainTest() in benches/scripts/v8-benches/deltablue.js:1112 'plan.execute();'` — a vendored **JS benchmark** file cited as a caller of Rust engine code (cross-language name_match on `execute`). Also brief contract line `call -> calls fn as_mut(&mut self) [core/ast/src/expression/literal/array.rs:182]` = name_match garbage; caller snippets truncated mid-expression (`let realm = then`, `is_cancelled -> calls pub(crate)`). |
| leakage | **0.** No FAIL_TO_PASS / grader-test name surfaced (hidden test `core/engine/src/tests/evaluation.rs` appears nowhere in GT output); no GT_META in agent-visible text; no empty dedup tags. Existing repo test/bench caller names are ordinary evidence. |

**New defect class surfaced by this trajectory (rust-visible, language-general): stale post-edit witnesses.** With L6 reindex OFF by design on the DeepSWE substrate (read-only authoritative graph), line-keyed snippets drift after the agent edits: [109] `[WITNESS] root_shape calls -> core/engine/src/context/mod.rs:545 '/// Returns an error if the handle is already cancelled.'` — GT quoting the agent's **own just-inserted doc comment** and attributing it to `root_shape`; [257] `enter_realm calls -> …:527 '}'`. Harmless here (ignored), but it is GT asserting falsehoods post-edit — the exact "wrong info is worse than no info" class.

---

## Cert reconciliation (gt_gt §12 — judge by role, reconcile against the witness)

- **`GRAPH_FAIL_MISSING_HANDOFF` = FALSE FAIL, as documented.** Cert is pre-agent (`hook_graph_hash:null`, `prebuilt_active:null`); the runtime witness proves the handoff: `lsp_warm_from_same_graph=true`, `graph_hash_after_lsp==graph_hash`, `closure_rebuilt_after_lsp=true`, and outcome.json `gt_prebuilt_active:true, hook_hash_match:true`. **Defect worth fixing:** outcome.json still stamps `failure_class:"GT", cert_fail:true` off this known false-fail — the run tally counts this task as a GT-class failure for the wrong reason.
- **LSP rust: "warm" is technically true and materially empty.** `server_launched=true, warm_probe_ok=true` BUT `project_ready=false` after 20s/8 attempts and `verified=0, corrected=0, deleted=0, failed=6620 (all "empty")`, graph hash unchanged. The precision pass resolved **zero** edges; the celebrated 67%-deterministic depth is entirely the tree-sitter indexer's (impl_method/type_flow/CHA), not rust-analyzer's. On a big Rust workspace, 20s of project-ready wait is not enough — demand-edges should wait for `project_ready` or be re-queued, else the rust LSP lever is decorative.
- **Telemetry contradictions (flagged, certs authoritative):** deep_metrics says "semantic (embedder) False dim=0" (cert: 768d, w_sem 0.25, separating) and "LSP-enriched edges 4393" (cert: 0 edges written, hash unchanged). Two readers disagree with the stamped certs — measurement bug, not substrate bug.

---

## Per-layer verdicts (from the §4 tables; D/C/C on fair probes only)

| layer | role-correct behavior? | delivered | correct | consumed |
|---|---|---|---|---|
| L1 brief | yes (medium-confidence honest list; 2/6 edited files named incl. #1; missed Context/Script — the issue named them anyway) | YES | PARTIAL (1 garbage edge line, truncated renders) | not attributable (self-localization) |
| L3b post_view | fired on every view | YES ×12 | MOSTLY (1 bench pollution, 2 stale witnesses) | NO |
| L3 post_edit contracts | fired on every edit; interfaces it protected were untouched (no harm) — structurally un-consumable for greenfield additions | YES ×5 | YES/irrelevant | NO |
| consensus `<gt-scope>` | honest abstention + re-anchor; weak neighbors, never followed, never misdirected | YES ×8 | WEAK | NO |
| L4 | event did not occur — N/A, not a no-op | — | — | — |
| L5 | 1 scaffold_trap (true premise); 0 false `failure_persisted`/`loop` fires | YES ×1 | YES | PLAUSIBLE |
| L5b | no trigger event | — | — | — |
| L6 | OFF by design (substrate); visible cost = stale post-edit witnesses | — | — | — |

---

## Bottom line (the one-liner asked for)

**The rust trajectory makes sense as a self-localized, compiler-driven greenfield implementation that GT watched honestly but did not steer: GT delivered a true-enough map (with one vendored-bench pollution line and two stale post-edit witnesses), leaked nothing, false-fired nothing — and was consumed exactly once (a nudge). gt_caused=FALSE, consumed≈0, right-trajectory(GT)=inert, resolved=NO** — the agent shipped untested `&mut self` constructors the hidden test couldn't compile against, from a build dir its own timeout-killed test runs had corrupted, and GT has no layer whose event is "submitting with zero test evidence." That absent governor, the wasted `abort/mod.rs` pointer, the never-surfaced VM-interrupt mechanism, the de-facto-no-op rust LSP pass (project_ready=false → 0/6620), and the false-fail cert polluting `failure_class` are the actionable findings; the graph's depth was real and irrelevant to the outcome.
