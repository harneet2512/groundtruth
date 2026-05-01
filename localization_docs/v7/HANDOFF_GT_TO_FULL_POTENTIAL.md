# GT Full-Potential Handoff

Date: 2026-04-30
Owner: harneet
Strategic plan: `D:\Groundtruth\future_plan.md`

This handoff is written so any future agent (or human) can pick up cold and continue the work without re-deriving context. It is meant to support running multiple agents in parallel, each with a clear lane.

---

## 1. Where We Are Right Now

### Shipped + verified (2026-04-30)
- v7.3 sanitizer + confidence gating shipped locally and deployed to VM1.
- v7.3 verification gate **PASS** on paired n=10 vs v7.2 frozen smoke (SWE-bench-Live lite, MiMo-V2-Flash). Layer 1 bit-identical, Layer 2 non-regression with sanitizer fix, Layer 3 out of scope. See `future_plan.md` "v7.3 verification gate — PASS" section for full evidence.
- Phase 0 deliverables shipped: `docs/kernel/API.md`, `docs/kernel/telemetry.md`, ADRs 0001-0004, fixture SCHEMA.md.
- Phase 1 skeleton landed (per user-authorized parallelization on 2026-04-30): `src/groundtruth/control/{kernel.py, types.py, decision_log.py}`, `src/groundtruth/adapters/base.py`, 6 fixture pairs, 50 stress-test layer tests xfail RED.
- Files touched in v7.3:
  - `src/groundtruth/pretask/v7_brief.py` (`_sanitize_brief_line`, `_render_v7`, `generate_brief`)
  - `tests/pretask/test_v7_brief.py`
  - `.tmp_v7_bundle/build_v7_bundle.py`

### Not yet done
- Phase 1 implementation: fill kernel stubs, ship OH adapter, flip stress tests RED → green, delete deprecated wrappers (`oh_gt_hook_wrapper.py`, `oh_gt_startupmode_wrapper.py`).
- Phase 2 paired n>=15 gate against v7.3-only baseline.
- Layer 1 hard-miss spike on `aws-cloudformation__cfn-lint-3767` and `pydata__xarray-9586` (parallel track, independent of Phase 1).

### Architectural % toward full potential
**60 / 100** as of 2026-04-30 post-gate. Substrate solid; v7.3 verified non-regression; Phase 0 shipped; Phase 1 skeleton landed. Phase 1 implementation is the next jump.

---

## 2. Locked Decisions (Inviolable)

These came from direct user instruction. Do not relitigate without explicit re-discussion. Canonical 9-decision list also in `future_plan.md`.

1. **No LLM inside GT, ever.** Deterministic only.
2. **No writes to the repo, ever.** Read-only.
3. **No reliance on tests existing.** Validation is graph + structural + repo-instruction only.
4. **Build adapters, not new scaffolds.** GT plugs into OpenHands / SWE-agent / mini-SWE / Aider via thin adapters. Never build a competing harness.
5. **Paired Wilcoxon n>=15 gate before default merge.** Standing rule. Per CLAUDE.md.
6. **TTD discipline.** Artifact-first tests, red-before-green, mutation checks. Per CLAUDE.md and `feedback_ttd_artifact_first` memory.
7. **No marketing jargon.** Plain language only. Per `feedback_no_marketing_jargon` memory.
8. **No strawman caveats.** Per `feedback_no_strawman_caveats` memory.
9. **No human-calendar time estimates in plans.** Name the lane and its deliverable, not the duration. Per `feedback_no_time_estimates` memory.

---

## 3. The Architecture We're Building

```
GT control kernel (deterministic, scaffold-agnostic)
    |
    +-- brief generation                  (mostly done in v7.3)
    +-- pre-tool decision                 (not started)
    +-- post-edit observation             (partial; today via OH wrapper)
    +-- mid-task pull                     (MCP works standalone; not wired into agent loop)
    +-- drift detection                   (not started)
    +-- graph-validation-to-replan        (not started)
    +-- telemetry                         (mostly done)

Adapters (thin, <=200 LOC each, translation only):
    openhands_adapter    swe_agent_adapter    mini_swe_adapter    aider_adapter

Each adapter declares a capability matrix; kernel respects it.
```

See `future_plan.md` for the full capability matrix and build order.

---

## 4. v7.3 Verification Gate — DONE / PASS (2026-04-30)

Gate ran 2026-04-30, verdict PASS, Phase 0 + Phase 1 skeleton unblocked. Section retained for historical record of the gate spec; do not re-run without explicit ask.

**Phase 0 of the plan was originally blocked on this gate; user explicitly authorized parallel start on 2026-04-30 — see `project_kernel_phase0_parallel_start.md` memory.**

### Gate spec
- Run paired n=10 v7.3 vs v7.2 on the **same Live-Lite task set used for v7.2 smoke**.
- Same OH wrapper, same MiMo-V2-Flash model, same prompts, same eval harness.
- Compute deltas:
  - pass@1 (primary)
  - localization metrics: first_gold, precision, coverage
  - brief_chars distribution
  - sanitizer-affected line count (how many lines had non-focus paths)
  - confidence distribution (how many tasks landed in 0.5-0.6, where the gating threshold sits)

### Decision tree
| Outcome | Action |
|---|---|
| No regression on any metric | v7.3 lands as default. Move to Phase 0 (kernel API spec). |
| Cosmetic only (constraints line ugly, no pass@1 impact) | Patch sanitizer to skip pattern-list lines, re-smoke, then proceed. |
| Confidence gating regresses | Tune `HIGH_CONFIDENCE_MIN` (try 0.5 or 0.7) or rework suggestive framing. Re-smoke. |
| Sanitizer regresses (loses CONTRACT info) | Add fallback: if stripping leaves <8 chars of content, keep original line. Re-smoke. |
| Both regress | Revert to v7.0 render, ship sanitizer-only fix as v7.0.1. Confidence gating goes back to drawing board. |

### Files needed to run the gate
- Bundle: `D:\Groundtruth\.tmp_v7_bundle\gt_pretask_brief_v7_full.py`
- VM1 details: see `reference_gcp_swebench_vms.md` memory (project-26227097-98fa-4016-a54, us-central1-a)
- Same task set as v7.2 smoke, captured in `C:\Users\Lenovo\Downloads\gt_v7_2_smoke_analysis\`

---

## 5. Parallel Agent Briefs

When ready to spawn parallel agents, use these self-contained briefs. Each agent does not see this conversation; the brief must stand alone.

### Agent A: Coder (kernel + adapters)

**Charter:** Build the GT control kernel and the per-scaffold adapters per `future_plan.md`. Ship one phase at a time, each behind its TTD test layers.

**Inputs:** `future_plan.md`, this handoff doc, `src/groundtruth/pretask/v7_brief.py` (current brief logic to factor into the kernel), `src/groundtruth/mcp/` (existing MCP tools to expose mid-task), `scripts/swebench/oh_gt_hook_wrapper.py` and `oh_gt_startupmode_wrapper.py` (existing OH integration to refactor into adapter).

**Phase ownership:** Phases 0, 1, 3, 4, 5, 6, 7, 8.

**Constraints:**
- Adapters <=200 LOC each, translation only.
- Kernel never imports adapter code.
- Every kernel feature must ship with: unit test (red-then-green), adapter contract test, replay test against frozen scaffold trajectory, paired n>=15 smoke gate.
- Locked decisions 1-8 above.

**Hand-off artifact per phase:** PR with passing tests, updated capability matrix, replay fixtures committed.

### Agent B: Benchmarker

**Charter:** Run paired evaluations, manage VM1 / VM2 capacity, produce per-phase smoke results. Never modify GT code; surface regressions to Agent A.

**Inputs:** `future_plan.md`, this handoff doc, the v7.3 verification gate spec above, `scripts/swebench/verify_report.py` (gates), `reference_gcp_swebench_vms.md` memory.

**First task:** the v7.3 verification gate (section 4 above). Do not start Phase 0 work until this gate passes.

**Per-phase work:** for each kernel/adapter phase that ships from Agent A, run paired n>=15 vs control. Apply the standing verify_report gate. Render the results table inline in the handoff log.

**Constraints:**
- Reuse `verify_report.py`, do not invent new gates without explicit ask.
- Archive `output.jsonl` before any relaunch (per `feedback_oh_resume_skips_errors` memory).
- Workers <= vCPUs (per CLAUDE.md note).

### Agent C: Verifier

**Charter:** Independent code review and architectural conformance check. Never writes code, never runs benchmarks. Reads PRs and reports drift from the locked plan.

**Inputs:** `future_plan.md`, this handoff doc, the locked decisions list, the architecture diagram and rules.

**Per-PR checks:**
- Does the change respect locked decisions 1-9?
- Adapter <=200 LOC code (cloc), helpers <=120 LOC each, total <=320 LOC? (per `future_plan.md` "Adapter LOC budget definition")
- Kernel does not import adapter code? (`grep` verified)
- TTD layers present (happy / boundary / adversarial / mutation per pure function; replay tests for kernel; contract tests for adapter; paired smoke for the run)?
- Capability matrix updated when an adapter changes?
- No marketing words in code, comments, or docs?
- **Runtime-behavior verification block present in PR body** (per `feedback_verify_code_does_what_intended` memory). PR body MUST include a "Verified by running" section that:
  - Names the specific input(s) the code was run against (fixture path, smoke task ID, replay artifact, or one-off probe command — not "manual testing").
  - Quotes the actual observable output (Decision JSON, Replan JSON, log line, brief snippet, telemetry record — copy-pasted, not paraphrased).
  - States in one sentence what behavior was being verified, and confirms the output matches that statement.
  - For glue / adapter / wrapper / I/O code: end-to-end run on a representative input is REQUIRED. Unit tests alone do not satisfy this check (oh_gt_hook_wrapper 0-byte stdout bug, sanitizer empty-brief, verify_report rate=0 all passed unit tests).
  - For docs / spec / ADR PRs: verification = "re-read the file post-edit, confirms it conveys [intended thing]" with the specific section quoted.
  - If the verification step was skipped for any change, the PR body MUST say so explicitly under "Unverified surfaces" so reviewers and the user know what's not proven.

**Reject the PR if the verification block is missing, generic ("ran tests, looks good"), or asserts only that tests pass without naming the runtime-behavior probe.**

**Hand-off artifact:** PR review comment summarizing pass/fail per check, with file:line citations for any violation. The runtime-verification check is a hard gate — failing it blocks merge regardless of how clean the code looks.

### Coordination rules between agents
- Agent A blocks on Agent B's gate result before merging to default.
- Agent B blocks on Agent A's PR landing before running the next phase smoke.
- Agent C reviews on every PR, not asynchronously.
- All three agents read `future_plan.md` and this handoff at the start of every spawn.

---

## 6. File Map

| Path | Purpose |
|---|---|
| `D:\Groundtruth\future_plan.md` | Strategic plan, locked decisions, build order |
| `D:\Groundtruth\localization_docs\v7\HANDOFF_GT_TO_FULL_POTENTIAL.md` | This file |
| `D:\Groundtruth\src\groundtruth\pretask\v7_brief.py` | Current brief logic (becomes kernel input) |
| `D:\Groundtruth\src\groundtruth\mcp\` | Existing MCP tools (mid-task pull substrate) |
| `D:\Groundtruth\src\groundtruth\runtime\` | Runtime telemetry, plan surface, patch auditor |
| `D:\Groundtruth\.tmp_v7_bundle\gt_pretask_brief_v7_full.py` | Deployed bundle for OH integration |
| `D:\Groundtruth\.tmp_v7_bundle\build_v7_bundle.py` | Bundle builder (auto-globs pretask + runtime) |
| `D:\Groundtruth\scripts\swebench\oh_gt_hook_wrapper.py` | Current OH wrapper, will become adapter |
| `D:\Groundtruth\scripts\swebench\oh_gt_startupmode_wrapper.py` | Alternate OH wrapper, candidate for adapter consolidation |
| `D:\Groundtruth\scripts\swebench\verify_report.py` | Paired-eval gate logic |
| `D:\Groundtruth\tests\pretask\` | Unit tests for the brief layer |
| `C:\Users\Lenovo\Downloads\gt_v7_2_smoke_analysis\RESEARCH_DIAGNOSIS.md` | Research-backed diagnosis behind v7.3 |
| `C:\Users\Lenovo\.claude-personal\projects\D--Groundtruth\memory\` | Standing user feedback (read every spawn) |

---

## 7. Open Questions — Resolved during Phase 0

All four resolved 2026-04-30:

1. **OH adapter pre-tool block** — RESOLVED: YES, OH supports it. `ConfirmationPolicy` per arXiv 2511.03690 §4.9. Capability matrix in `future_plan.md` upgraded to all-yes. `degraded_capability_rate == 0` is a Phase 2 gate.
2. **Telemetry parity** — RESOLVED: canonical fields = full `gt_kernel_decision` 7-element Decision Trace record. Scaffold-specific fields land under `gt_kernel_decision.context_evaluated.provenance`. Spec in `docs/kernel/telemetry.md`.
3. **Kernel location** — RESOLVED: `src/groundtruth/control/`. Adapters at `src/groundtruth/adapters/<scaffold>.py`. ADR 0001.
4. **Bundle deployment** — RESOLVED: bundle stays for VM deployment but becomes a thin kernel-install pointer, not an 84KB monolith. ADR 0004.

Residual gaps audited 2026-04-30 and folded into `future_plan.md` "Operational specs" section: OH SDK version pinning, Phase 2 paired-gate threshold, `gt_keep_rate` formula, LSP roadmap status, adapter LOC counting rules, `runtime/repo_adapters.py` rename to "language profile" in docs.

---

## 8. What Not To Do

- Do not build a new scaffold. The user said no twice and was clear.
- Do not add an LLM to any GT step.
- Do not add features the plan does not call for. No half-finished implementations.
- Do not write documentation files outside what the plan explicitly requires.
- Do not skip the paired Wilcoxon gate "for prototyping." It is the gate.
