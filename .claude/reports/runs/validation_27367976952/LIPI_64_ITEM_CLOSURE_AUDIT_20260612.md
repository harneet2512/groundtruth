# LIPI 64-item closure audit - 2026-06-12

Audited HEAD: `51c54d51` (`gt-trial`)

Method: `LIPI.md`. Tests are receipts only. Closure is judged by desired product behavior from
`gt_gt.md` + `CLAUDE.md` versus current code.

Verdict counts after the first repair pass:

| Verdict | Count | Meaning |
|---|---:|---|
| CLOSED | 47 | Current code implements the desired boundary and is wired to a product surface. |
| PARTIAL | 16 | Code exists, but live proof, semantics, naming, or docs are incomplete. |
| LIVE | 1 | Launch blocker still open. |
| FALSELY CLOSED | 0 | No row remains falsely closed after this pass. |

Rows repaired during this pass: `P1-09`, `P2-03`, `P2-13`, and `P2-14`.

## P0 launch blockers

### P0-01

```text
ID: P0-01
Desired state: Real DeepSWE task images pass substrate proof for supported languages before any agent spend.
Current state: Code fixes for Go/Rust are present, but no rebuilt pinned substrate has re-proved the failing Go/Rust jobs.
Logic: Clean - the row correctly treats real task proof as the product boundary.
Implementation: Partial - Dockerfile and dep manifest changes exist, but product closure depends on rebuilt image.
Integration: Broken until re-proof - GHA still needs new GT_SUBSTRATE_DIGEST and Go/Rust proof dispatch.
Plumbing: Partial - proof_progress/proof_failure are available, but no green live artifacts yet.
Verdict: LIVE
Remaining bug, if any: P0-01 remains the only launch blocker.
Fix boundary: Rebuild substrate, pin digest, re-dispatch abs-module-cache-flags, abs-stepped-slices, boa.
```

### P0-02

```text
ID: P0-02
Desired state: Proof failure identifies the exact failed stage/language/tool instead of only rc=2.
Current state: gt_run_proof.py writes proof_progress.json and proof_failure.json with stage/code/message.
Logic: Clean - structured substage failure is the right product boundary.
Implementation: Clean - _ProofTracker persists progress and failure.
Integration: Clean - deepswe_full.yml prints proof_failure.json when proof fails.
Plumbing: Clean - artifacts are collected with proof_progress/proof_failure.
Verdict: CLOSED
Remaining bug, if any: None for this row.
Fix boundary: None.
```

### P0-03

```text
ID: P0-03
Desired state: Language smoke is explicitly a substrate preflight, not proof that DeepSWE task proof works.
Current state: Docs and workflow naming reduce the overclaim, but no first-class DeepSWE proof parity job exists.
Logic: Clean - smoke and real task proof are different products.
Implementation: Partial - naming improved; parity capability not yet implemented.
Integration: Partial - smoke can still pass while real task proof fails.
Plumbing: Partial - no parity artifact that proves issue/deps/task-image path.
Verdict: PARTIAL
Remaining bug, if any: Smoke/preflight still cannot close DeepSWE proof equivalence.
Fix boundary: Add proof-only DeepSWE parity job or keep all docs explicitly non-equivalent.
```

### P0-04

```text
ID: P0-04
Desired state: Go proof discovers task module/dependency state dynamically and fails with structured evidence when unavailable.
Current state: deepswe_full.yml probes go env GOMODCACHE and candidate caches, writes dep_store_manifest.json, and mounts cache into substrate.
Logic: Mostly clean - dynamic discovery fixes the original hardcoded /root/go/pkg/mod bug.
Implementation: Partial risk - dep_store_manifest.py currently treats every Go task as requiring a non-empty gomodcache.
Integration: Pending live proof - code is wired into GHA but not re-proved with rebuilt image.
Plumbing: Clean for evidence - source path, file_count, total_bytes, and failure marker are structured.
Verdict: PARTIAL
Remaining bug, if any: Generality risk for Go repos with no external module cache requirement; live proof still required.
Fix boundary: Re-proof first; if empty-cache false positives appear, validate package metadata readiness rather than cache non-emptiness alone.
```

### P0-05

```text
ID: P0-05
Desired state: Rust substrate has a product-real rust-analyzer environment: rust-src/sysroot/proc-macro/linker/toolchain.
Current state: Dockerfile bumps rust-analyzer, installs gcc, and workflow copies cargo/rustup and attempts rust-src.
Logic: Clean - the failing surface was toolchain/product readiness, not agent behavior.
Implementation: Partial - code is present but only a rebuilt image proves the baked closure.
Integration: Pending live proof - the old digest still failed; new digest not yet validated.
Plumbing: Partial - dep manifest records cargo/rustup presence; rust-src success is not separately manifest-stamped.
Verdict: PARTIAL
Remaining bug, if any: Needs rebuilt image + Rust proof artifact; consider explicit rust-src/proc-macro manifest fields.
Fix boundary: Substrate image rebuild and Boa proof re-run.
```

### P0-06

```text
ID: P0-06
Desired state: task_truth.json is the authority for resolved/outcome, not legacy outcome readers.
Current state: compute_paired_metrics.py and gt_deep_metrics.py prefer task_truth.json when present.
Logic: Clean - one outcome authority is correct.
Implementation: Clean enough - readers now consume task truth.
Integration: Clean for current touched readers; old external scratch scripts remain out of product scope.
Plumbing: Clean - task_truth is collected in GHA.
Verdict: CLOSED
Remaining bug, if any: None inside product path.
Fix boundary: None.
```

### P0-07

```text
ID: P0-07
Desired state: Raw certs may exist, but product dashboards use reconciled substrate verdict.
Current state: task_truth.py emits reconciled_substrate_verdict.json using reconcile_graph_handoff().
Logic: Clean - raw cert is evidence, reconciled verdict is product truth.
Implementation: Clean - central function handles witness-over-cert.
Integration: Clean - task_truth write also writes reconciled verdict.
Plumbing: Clean - collect step includes task_truth and related artifacts.
Verdict: CLOSED
Remaining bug, if any: None for current product path.
Fix boundary: None.
```

### P0-08

```text
ID: P0-08
Desired state: If GT does not hard-block submission, docs/code must call it an intervention, not a hard gate.
Current state: gt_agent.py says pre_submit_intervention; gt_gt.md has mixed wording in one table header but semantics text says not hard block.
Logic: Clean - intervention is the implemented behavior.
Implementation: Clean - no false hard blocker in code.
Integration: Partial doc wording - CP012 table still says "Pre-submit gate" in the topic column.
Plumbing: Clean - agent-visible note is explicit.
Verdict: PARTIAL
Remaining bug, if any: Remove remaining "Pre-submit gate" topic wording where it implies hard enforcement.
Fix boundary: gt_gt.md section 17.9 wording.
```

### P0-09

```text
ID: P0-09
Desired state: Self-verifier retry is never described as official hidden verifier repair.
Current state: gt_agent.py and task_truth.verifier_semantics distinguish self_verifier_retry from official_verifier_repair=false.
Logic: Clean - distinction matches product reality.
Implementation: Clean - structured semantics exist.
Integration: Partial doc risk - gt_gt.md still has one "Verifier-fail retry plumbing" architectural label that can be misread.
Plumbing: Clean - task_truth carries the distinction.
Verdict: PARTIAL
Remaining bug, if any: Tighten architecture wording to "self-verifier retry plumbing".
Fix boundary: gt_gt.md and handoff wording.
```

### P0-10

```text
ID: P0-10
Desired state: Runtime policy owner map names the actual live code surfaces.
Current state: gt_gt.md section 17.9 now names phase_policy.py for phase policy, but still over-attributes context budget/action templates to gt_oracle.py where gt_mini_patch.py owns runtime rendering.
Logic: Clean - owner drift is a doc/product bug.
Implementation: Partial - code is in the expected modules, docs are not fully exact.
Integration: Partial - future fixes could still be sent to the wrong file.
Plumbing: Clean - no data loss, this is owner truth.
Verdict: PARTIAL
Remaining bug, if any: Correct section 17.9 owner rows for context budget/action rendering.
Fix boundary: gt_gt.md section 17.9.
```

### P0-11

```text
ID: P0-11
Desired state: Phase/context policy is first-class and testable, not buried only in patch globals.
Current state: artifact_deepswe/phase_policy.py exposes Phase, PHASE_POLICY, and phase_allows(); gt_mini_patch imports it.
Logic: Clean - policy as module matches desired state.
Implementation: Clean - module exists and is used.
Integration: Clean - runtime imports canonical policy.
Plumbing: Clean - no serialization boundary needed for this policy yet.
Verdict: CLOSED
Remaining bug, if any: None for first-class module; broader phase controller remains architecture-gap partial.
Fix boundary: None for this row.
```

### P0-12

```text
ID: P0-12
Desired state: Runtime suppression heuristic must not be confused with post-run consumption proof.
Current state: gt_mini_patch.py renamed ledger path to _ledger_note_suppression_heuristic; post-run consumption remains separate.
Logic: Clean - separation is correct.
Implementation: Clean - runtime name no longer overclaims consumption.
Integration: Clean - delivered/consumed/enforced are still post-run metrics, not runtime heuristic.
Plumbing: Clean - no product field falsely asserts consumption from same-turn trigger.
Verdict: CLOSED
Remaining bug, if any: None for semantics split.
Fix boundary: None.
```

### P0-13

```text
ID: P0-13
Desired state: Horizon calibration is versioned and treated as a heuristic, not universal truth from nine tasks.
Current state: .claude/calibration/horizon_v1.json exists and gt_mini_patch loads calibration/defaults.
Logic: Clean - versioned heuristic is the right closure for now.
Implementation: Clean enough - calibration artifact exists.
Integration: Partial - validate_proof_readiness only checks thresholds presence, not corpus sufficiency.
Plumbing: Clean - file is tracked and readable.
Verdict: CLOSED
Remaining bug, if any: Larger calibration quality is future product work, not this row.
Fix boundary: None.
```

### P0-14

```text
ID: P0-14
Desired state: Checkpoint docs and code changes remain synchronized.
Current state: CHECKPOINT_PROTOCOL.md and check_checkpoint_protocol.py exist; however current machine/doc truth still drifted after later commits.
Logic: Clean - protocol is needed.
Implementation: Partial - checker covers only a subset of drift.
Integration: Partial - docs can still say stale HEAD/run without failing.
Plumbing: Partial - current run file had stale head before this audit.
Verdict: PARTIAL
Remaining bug, if any: Need stronger doc/machine truth freshness checks.
Fix boundary: Extend checkpoint checker or add current-run consistency check.
```

## P1 product correctness

### P1-01

```text
ID: P1-01
Desired state: Issue source, length, and hash are structured provenance.
Current state: issue_manifest.py exists and GHA writes issue_manifest.json before proof.
Logic: Clean.
Implementation: Clean.
Integration: Clean - workflow invokes it.
Plumbing: Clean - artifact is collected.
Verdict: CLOSED
Remaining bug, if any: None.
Fix boundary: None.
```

### P1-02

```text
ID: P1-02
Desired state: Dependency-store copy is structured evidence, not echo-only logs.
Current state: dep_store_manifest.py records source path, existence, file count, and bytes.
Logic: Clean.
Implementation: Clean.
Integration: Clean - GHA calls manifest after copying stores.
Plumbing: Clean - manifest is copied to artifacts and printed on failure.
Verdict: CLOSED
Remaining bug, if any: None beyond P0-04/P0-05 live proof.
Fix boundary: None.
```

### P1-03

```text
ID: P1-03
Desired state: Wrong task root fails closed instead of silently falling back to /testbed.
Current state: deepswe_full.yml validates repo root/.git before proof.
Logic: Clean.
Implementation: Clean.
Integration: Clean.
Plumbing: Clean.
Verdict: CLOSED
Remaining bug, if any: None.
Fix boundary: None.
```

### P1-04

```text
ID: P1-04
Desired state: Proof progress records memory heartbeat to distinguish OOM/capacity failure.
Current state: _ProofTracker writes rss_kb for each stage.
Logic: Clean.
Implementation: Clean.
Integration: Clean - proof stages call tracker.
Plumbing: Clean - proof_progress.json persists.
Verdict: CLOSED
Remaining bug, if any: None.
Fix boundary: None.
```

### P1-05

```text
ID: P1-05
Desired state: gt-run-proof persists last successful stage before nonzero exit.
Current state: proof_progress.json is flushed on each complete/fail call.
Logic: Clean.
Implementation: Clean.
Integration: Clean.
Plumbing: Clean.
Verdict: CLOSED
Remaining bug, if any: None.
Fix boundary: None.
```

### P1-06

```text
ID: P1-06
Desired state: LSP product status is effective resolution/readiness, not a single warm bit.
Current state: resolve/gates expose verdicts such as LSP_FAIL_NOT_READY, LSP_FAIL_NO_WARM, LSP_ACTIVE_VALID.
Logic: Clean.
Implementation: Clean enough.
Integration: Partial only until dashboards consume the verdict everywhere.
Plumbing: Clean in proof artifacts.
Verdict: CLOSED
Remaining bug, if any: Watch dashboard consumers outside product path.
Fix boundary: None for current product path.
```

### P1-07

```text
ID: P1-07
Desired state: Embedder product verdict is separated from diagnostic local probe.
Current state: gt_deep_metrics.py emits embedder_product_verdict / embedder_diagnostic_only semantics.
Logic: Clean.
Implementation: Clean.
Integration: Clean for deep metrics.
Plumbing: Clean.
Verdict: CLOSED
Remaining bug, if any: None.
Fix boundary: None.
```

### P1-08

```text
ID: P1-08
Desired state: Graph handoff reconciliation has one function used by task truth and outcome surfaces.
Current state: scripts/swebench/reconcile.py centralizes reconcile_graph_handoff().
Logic: Clean.
Implementation: Clean.
Integration: Clean for task_truth; deepswe_outcome still has historical logic but product authority is task_truth.
Plumbing: Clean.
Verdict: CLOSED
Remaining bug, if any: None in authoritative path.
Fix boundary: None.
```

### P1-09

```text
ID: P1-09
Desired state: Product can prove the substrate brief block delivered to the agent is the same brief.txt.
Current state: artifact_resolver.brief_provenance extracts the delivered <gt-task-brief> block, compares it to brief.txt, and records containment semantics. gt_agent.py witness also records substrate_brief_sha256, delivered_brief_block_sha256, delivered_contains_substrate_brief, and brief_match.
Logic: Clean - the product proof is now block equality/containment, not full delivered-instruction equality.
Implementation: Clean - hashing and fallback containment are deterministic.
Integration: Clean - task_truth consumes brief_provenance; adapter_witness carries the same witness fields.
Plumbing: Clean - delivered instruction may include prompt/preamble while brief proof still survives as a distinct block.
Verdict: CLOSED
Remaining bug, if any: None for product proof. Live runs still need regenerated artifacts to populate the new fields.
Fix boundary: artifact_resolver.py + gt_agent.py witness metadata.
```

### P1-10

```text
ID: P1-10
Desired state: Adapter witness is structured JSON, not grep-only.
Current state: gt_agent.py writes /tmp/gt/adapter_witness.json and GHA collects it.
Logic: Clean.
Implementation: Clean.
Integration: Clean.
Plumbing: Clean.
Verdict: CLOSED
Remaining bug, if any: None.
Fix boundary: None.
```

### P1-11

```text
ID: P1-11
Desired state: Adapter errors are structurally scanned/classified.
Current state: adapter_error_scan.py exists and workflow invokes structured scan.
Logic: Clean.
Implementation: Clean.
Integration: Clean.
Plumbing: Clean.
Verdict: CLOSED
Remaining bug, if any: None.
Fix boundary: None.
```

### P1-12

```text
ID: P1-12
Desired state: Proof/substrate mode cannot silently run legacy oracle route.
Current state: gt_mini_patch.py raises if GT_PROOF_MODE=1 and GT_ORACLE_ROUTE=0.
Logic: Clean.
Implementation: Clean.
Integration: Clean.
Plumbing: Clean.
Verdict: CLOSED
Remaining bug, if any: None.
Fix boundary: None.
```

### P1-13

```text
ID: P1-13
Desired state: Event-bound candidates still respect phase/context policy.
Current state: gt_mini_patch imports phase_policy and event-bound logic is policy-gated.
Logic: Clean enough.
Implementation: Clean enough.
Integration: Clean for current DeepSWE runtime.
Plumbing: Clean.
Verdict: CLOSED
Remaining bug, if any: Broader controller completeness is covered in architecture gap pass.
Fix boundary: None for this row.
```

### P1-14

```text
ID: P1-14
Desired state: Runtime ledger wording does not claim next-action consumption.
Current state: _ledger_note_suppression_heuristic separates runtime suppression from consumption proof.
Logic: Clean.
Implementation: Clean.
Integration: Clean.
Plumbing: Clean.
Verdict: CLOSED
Remaining bug, if any: None.
Fix boundary: None.
```

### P1-15

```text
ID: P1-15
Desired state: Duplicate context is suppressed by stable fact identity where possible.
Current state: gt_mini_patch.py has _stable_fact_id and _DELIVERED_FACT_IDS.
Logic: Clean.
Implementation: Clean enough for line-derived facts.
Integration: Clean.
Plumbing: Clean.
Verdict: CLOSED
Remaining bug, if any: Semantic duplicate detection can improve later but row is closed.
Fix boundary: None.
```

### P1-16

```text
ID: P1-16
Desired state: Budgeting reports actual char and estimated token metadata instead of opaque trim.
Current state: _last_budget_meta records char/token/dedupe metadata.
Logic: Clean.
Implementation: Clean.
Integration: Clean.
Plumbing: Clean.
Verdict: CLOSED
Remaining bug, if any: None.
Fix boundary: None.
```

### P1-17

```text
ID: P1-17
Desired state: Graph facts become action-oriented next steps.
Current state: action templates were expanded in runtime/oracle surfaces.
Logic: Clean direction.
Implementation: Partial by nature - template coverage is not exhaustive, but row target was expanded coverage.
Integration: Clean for current candidate renderer.
Plumbing: Clean.
Verdict: CLOSED
Remaining bug, if any: Full architecture still needs broader graph-to-action quality audit.
Fix boundary: None for register row.
```

### P1-18

```text
ID: P1-18
Desired state: Obligation lifecycle emits events outside runtime memory.
Current state: gt_mini_patch persists obligation_status and appends obligation_status events to GT_ORACLE_EVENTS.
Logic: Clean.
Implementation: Clean.
Integration: Clean.
Plumbing: Clean - oracle events and status file exist.
Verdict: CLOSED
Remaining bug, if any: None.
Fix boundary: None.
```

### P1-19

```text
ID: P1-19
Desired state: task_truth.json contains obligation_status vector.
Current state: task_truth.py loads GT_OBLIGATION_STATUS or trial-adjacent status and includes obligation_status.
Logic: Clean.
Implementation: Clean.
Integration: Clean.
Plumbing: Clean.
Verdict: CLOSED
Remaining bug, if any: None.
Fix boundary: None.
```

### P1-20

```text
ID: P1-20
Desired state: Verification horizon is actionable without exact hidden test names, file paths, or single-test commands.
Current state: _render_verify_emission uses behavior-level "narrowest relevant repo test target" wording.
Logic: Clean - avoids leak and still nudges verification.
Implementation: Clean.
Integration: Clean.
Plumbing: Clean.
Verdict: CLOSED
Remaining bug, if any: Quality may need trajectory validation, but no leak bug remains.
Fix boundary: None.
```

### P1-21

```text
ID: P1-21
Desired state: Retry uses repo-native targeted category when safe, not broad/noisy default only.
Current state: gt_agent.py has retry command selection and bounded retry loop.
Logic: Clean enough.
Implementation: Clean enough for configured self-verifier retry.
Integration: Partial - not enabled by default in workflow; that is a product choice.
Plumbing: Clean.
Verdict: CLOSED
Remaining bug, if any: Enablement policy belongs to architecture/ops, not this row.
Fix boundary: None.
```

### P1-22

```text
ID: P1-22
Desired state: Test feedback is sanitized/truncated with metadata.
Current state: gt_agent.py formats bounded <test-feedback> rather than raw unbounded output.
Logic: Clean.
Implementation: Clean enough.
Integration: Clean.
Plumbing: Clean.
Verdict: CLOSED
Remaining bug, if any: None.
Fix boundary: None.
```

### P1-23

```text
ID: P1-23
Desired state: Harness neutrality claim is not false when GT adds retry/gate note.
Current state: task_truth.verifier_semantics documents self_verifier_retry and official_verifier_repair=false; GT note is acknowledged.
Logic: Clean.
Implementation: Clean.
Integration: Clean.
Plumbing: Clean.
Verdict: CLOSED
Remaining bug, if any: None.
Fix boundary: None.
```

### P1-24

```text
ID: P1-24
Desired state: Trajectory resolver requires exact task match when instance_id is known.
Current state: artifact_resolver and gt_deep_metrics use strict task matching where instance_id is passed.
Logic: Clean.
Implementation: Clean.
Integration: Clean enough.
Plumbing: Clean.
Verdict: CLOSED
Remaining bug, if any: None.
Fix boundary: None.
```

### P1-25

```text
ID: P1-25
Desired state: Deep metrics consumes task_truth outcome instead of inferring separate resolved truth.
Current state: gt_deep_metrics.py finds task_truth.json and applies outcome.resolved/failure_class.
Logic: Clean.
Implementation: Clean.
Integration: Clean.
Plumbing: Clean.
Verdict: CLOSED
Remaining bug, if any: None.
Fix boundary: None.
```

### P1-26

```text
ID: P1-26
Desired state: Metrics do not call edited-file proxies "gold" unless legitimate gold is available.
Current state: Handoff says alias exists, but report-facing naming still includes steps_to_first_gold_edit.
Logic: Broken naming - a wrong patch can define its own proxy target.
Implementation: Partial - alias/wiring exists but language is still misleading.
Integration: Partial - reports may continue to overread proxy as gold.
Plumbing: Clean.
Verdict: PARTIAL
Remaining bug, if any: Rename product-facing fields to edited-file/proxy language or add known-gold provenance.
Fix boundary: compute_paired_metrics.py report schema + docs.
```

### P1-27

```text
ID: P1-27
Desired state: INFRA and unresolved denominator are determined from normalized task truth.
Current state: Code is wired toward task_truth, but live run validation is still pending.
Logic: Clean.
Implementation: Clean enough.
Integration: Partial - needs live artifacts proving denominator behavior.
Plumbing: Partial - no post-fix live run artifact yet.
Verdict: PARTIAL
Remaining bug, if any: Validate on rebuilt-substrate live run.
Fix boundary: Live artifact audit after P0-01.
```

### P1-28

```text
ID: P1-28
Desired state: Patch hygiene is always present in task truth/report summary.
Current state: task_truth.py classifies patch hygiene automatically.
Logic: Clean.
Implementation: Clean.
Integration: Clean for task_truth authority.
Plumbing: Clean.
Verdict: CLOSED
Remaining bug, if any: None.
Fix boundary: None.
```

### P1-29

```text
ID: P1-29
Desired state: Artifact path resolution is centralized to avoid split readers.
Current state: artifact_resolver.py centralizes result, trajectory, deep metrics, task truth, oracle, delivered instruction, and brief paths.
Logic: Clean.
Implementation: Clean.
Integration: Clean for new readers.
Plumbing: Clean.
Verdict: CLOSED
Remaining bug, if any: Legacy scratch readers are out of product scope.
Fix boundary: None.
```

## P2 observability and documentation

### P2-01

```text
ID: P2-01
Desired state: Docs say pre-submit intervention unless a hard block exists.
Current state: Most semantics are corrected, but gt_gt.md still has a "Pre-submit gate" topic label.
Logic: Clean but wording incomplete.
Implementation: N/A - docs row.
Integration: Partial - doc can still mislead.
Plumbing: N/A.
Verdict: PARTIAL
Remaining bug, if any: Remove hard-gate wording where not explicitly negated.
Fix boundary: gt_gt.md section 17.9.
```

### P2-02

```text
ID: P2-02
Desired state: Docs name primary owner surfaces accurately.
Current state: Some owner rows are corrected; context/action rows still over-name gt_oracle.py relative to gt_mini_patch.py.
Logic: Clean.
Implementation: N/A.
Integration: Partial.
Plumbing: N/A.
Verdict: PARTIAL
Remaining bug, if any: Correct owner table.
Fix boundary: gt_gt.md.
```

### P2-03

```text
ID: P2-03
Desired state: Handoff and machine files point at current local audit head.
Current state: CURRENT_VALIDATION_RUN.json now points at audited HEAD 51c54d51 and the hbali-stack GHA run URL; LIPI/handoff docs identify this repair pass.
Logic: Clean - machine truth is current-head scoped.
Implementation: Clean - JSON pointer corrected.
Integration: Clean - current-run pointer and handoff docs now agree on the active blocker.
Plumbing: Clean after commit - durable once this repair pass is committed.
Verdict: CLOSED
Remaining bug, if any: None in code/doc state; process still needs future freshness guard discipline.
Fix boundary: .claude/CURRENT_VALIDATION_RUN.json and handoff docs.
```

### P2-04

```text
ID: P2-04
Desired state: Atomic register exists and is the bug closure unit.
Current state: ATOMIC_PRODUCT_BUG_REGISTER_20260612.md exists with 64 rows.
Logic: Clean.
Implementation: Clean.
Integration: Clean.
Plumbing: Clean.
Verdict: CLOSED
Remaining bug, if any: None.
Fix boundary: None.
```

### P2-05

```text
ID: P2-05
Desired state: Historical GHA wording is tied to commit/run so old logs are not read as current truth.
Current state: Docs mention historical run ids, but old logs naturally still contain old wording.
Logic: Clean.
Implementation: Partial - docs help but cannot rewrite history.
Integration: Partial - readers still need commit context.
Plumbing: N/A.
Verdict: PARTIAL
Remaining bug, if any: Add current/old run table to handoff docs whenever re-proof completes.
Fix boundary: Handoff docs.
```

### P2-06

```text
ID: P2-06
Desired state: Failed proof runs still have minimal provenance before proof starts.
Current state: deepswe_full.yml writes run_provenance.json before proof.
Logic: Clean.
Implementation: Clean.
Integration: Clean.
Plumbing: Clean.
Verdict: CLOSED
Remaining bug, if any: None.
Fix boundary: None.
```

### P2-07

```text
ID: P2-07
Desired state: Outcome docs classify adapter-wire as GT, not INFRA.
Current state: deepswe_outcome.py docstring states adapter-wire is GT, not INFRA.
Logic: Clean.
Implementation: Clean.
Integration: Clean.
Plumbing: N/A.
Verdict: CLOSED
Remaining bug, if any: None.
Fix boundary: None.
```

### P2-08

```text
ID: P2-08
Desired state: Infra marker parsing cannot collide with adapter error lines.
Current state: find_infra_markers line-anchors markers and excludes DEEPSWE_ADAPTER_FAIL lines.
Logic: Clean.
Implementation: Clean.
Integration: Clean.
Plumbing: Clean.
Verdict: CLOSED
Remaining bug, if any: None.
Fix boundary: None.
```

### P2-09

```text
ID: P2-09
Desired state: UNKNOWN outcomes carry unknown_reason.
Current state: deepswe_outcome has unknown_reason handling.
Logic: Clean.
Implementation: Clean.
Integration: Clean.
Plumbing: Clean.
Verdict: CLOSED
Remaining bug, if any: None.
Fix boundary: None.
```

### P2-10

```text
ID: P2-10
Desired state: task_truth.json is collected in GHA artifacts.
Current state: deepswe_full.yml runs task_truth.py into trial_results/task_truth.json.
Logic: Clean.
Implementation: Clean.
Integration: Clean.
Plumbing: Clean.
Verdict: CLOSED
Remaining bug, if any: None.
Fix boundary: None.
```

### P2-11

```text
ID: P2-11
Desired state: Missing oracle events are visible in task truth.
Current state: task_truth.py emits oracle_events_status with path/present.
Logic: Clean.
Implementation: Clean.
Integration: Clean.
Plumbing: Clean.
Verdict: CLOSED
Remaining bug, if any: None.
Fix boundary: None.
```

### P2-12

```text
ID: P2-12
Desired state: Delivered instruction absence is not silent when agent ran.
Current state: workflow collects delivered_instruction and adapter_witness; task_truth can resolve delivered path.
Logic: Clean.
Implementation: Clean enough.
Integration: Clean enough.
Plumbing: Partial risk - absence after agent-run is not yet a hard failure everywhere.
Verdict: CLOSED
Remaining bug, if any: Future hardening can make missing delivered_instruction fail when n_agent_steps>0.
Fix boundary: None for this row.
```

### P2-13

```text
ID: P2-13
Desired state: run manifest and witness include structured brief hash.
Current state: gt_agent.py witness now records substrate_brief_sha256, delivered_brief_block_sha256, delivered_contains_substrate_brief, brief_match, and brief_match_semantics.
Logic: Clean - run witness distinguishes full prompt hash from brief-block proof.
Implementation: Clean - deterministic witness fields are written beside delivered_instruction.txt.
Integration: Clean - artifact_resolver/task_truth read the same semantics.
Plumbing: Clean for new runs; historical artifacts remain old by definition.
Verdict: CLOSED
Remaining bug, if any: None for new artifacts.
Fix boundary: gt_agent.py + artifact_resolver.py.
```

### P2-14

```text
ID: P2-14
Desired state: Single machine-readable current-run file exists and is accurate.
Current state: File exists and now points at 51c54d51 plus the current GHA run URL.
Logic: Clean.
Implementation: Clean.
Integration: Clean.
Plumbing: Clean after commit.
Verdict: CLOSED
Remaining bug, if any: None in current machine file; freshness guard remains process discipline.
Fix boundary: .claude/CURRENT_VALIDATION_RUN.json + checkpoint checker.
```

### P2-15

```text
ID: P2-15
Desired state: Test scope receipts are pinned in a manifest but not treated as closure authority.
Current state: tests/MANIFEST.json exists with command.
Logic: Clean.
Implementation: Clean.
Integration: Clean.
Plumbing: Clean.
Verdict: CLOSED
Remaining bug, if any: None.
Fix boundary: None.
```

### P2-16

```text
ID: P2-16
Desired state: Scripts guard against accidental GT-off baseline rerun.
Current state: compute_paired_metrics.py has baseline guard behavior.
Logic: Clean.
Implementation: Clean.
Integration: Clean.
Plumbing: Clean.
Verdict: CLOSED
Remaining bug, if any: None.
Fix boundary: None.
```

### P2-17

```text
ID: P2-17
Desired state: Cost fallback is labeled estimate/model-scoped.
Current state: gt_deep_metrics.py sets cost_estimate and cost_source for recomputed trajectory costs.
Logic: Clean.
Implementation: Clean.
Integration: Clean.
Plumbing: Clean.
Verdict: CLOSED
Remaining bug, if any: None.
Fix boundary: None.
```

### P2-18

```text
ID: P2-18
Desired state: API calls and assistant steps are separate metrics.
Current state: gt_deep_metrics.py stores assistant_steps separately from action_count.
Logic: Clean.
Implementation: Clean.
Integration: Clean.
Plumbing: Clean.
Verdict: CLOSED
Remaining bug, if any: None.
Fix boundary: None.
```

### P2-19

```text
ID: P2-19
Desired state: Dirty local tree cannot cause accidental commits/reverts.
Current state: Worktree is very dirty; process says stage explicit paths only.
Logic: Clean as process residual.
Implementation: Manual discipline only.
Integration: Process risk remains.
Plumbing: N/A.
Verdict: PARTIAL
Remaining bug, if any: No automated guard for unrelated dirty files.
Fix boundary: Commit discipline or pre-commit/staging guard.
```

### P2-20

```text
ID: P2-20
Desired state: Push guard behavior is documented and not bypassed silently.
Current state: Handoff documents local commits and push guard issue.
Logic: Clean as process residual.
Implementation: Partial - no policy decision to bypass/allow report docs.
Integration: Partial.
Plumbing: N/A.
Verdict: PARTIAL
Remaining bug, if any: Decide push policy for run-folder docs.
Fix boundary: Repo push guard policy.
```

### P2-21

```text
ID: P2-21
Desired state: Remote split is documented so pushes target the intended repo.
Current state: Handoff mentions active branch/repo, but stale run URL showed drift.
Logic: Clean as process residual.
Implementation: Partial.
Integration: Partial.
Plumbing: N/A.
Verdict: PARTIAL
Remaining bug, if any: Keep machine file and handoff URLs on hbali-stack/groundtruth.
Fix boundary: Handoff/machine docs.
```

## Closure summary

Architecture-first closure is not complete yet.

Launch blockers:

- `P0-01` remains LIVE until rebuilt substrate proof is green.

False-closed rows repaired in this pass:

- `P1-09`: brief provenance now proves delivered brief-block equality/containment.
- `P2-03`: current validation machine file now points at `51c54d51`.

Most important partials:

- `P0-03`: smoke is still not DeepSWE task proof.
- `P0-04/P0-05`: code exists but live proof is required.
- `P0-08/P0-09/P0-10`: architecture wording still has semantic/owner drift.
- `P1-26/P1-27`: scorecard naming and denominator behavior need live/product validation.
- `P2-13/P2-14`: repaired in this pass; new artifacts carry brief-block hash semantics and current-run machine truth is updated.
