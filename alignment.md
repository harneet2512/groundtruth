# GT v7 Alignment Audit

Date: 2026-04-30

This audit checks GT v7 against two things:

- our own stated operating rules: compact agent-facing guidance, full evidence in telemetry, deterministic behavior, measurable participation;
- frontier lab/product patterns and recent research on coding agents, context engineering, hooks, and benchmark hygiene.

## Executive Finding

v7 is directionally correct but not fully aligned yet.

The pre-task brief has been fixed from the earlier over-expanded behavior: it now shows a compact ranked `agent_focus_files` list capped at 3 files, and keeps the broader `cluster_files` in telemetry. That directly addresses the observed brief-to-edit overlap failure.

However, the system still violates the spirit of the same rule in two important places:

- `gt_plan` still returns full plan JSON to the agent by default.
- `gt_report` still emphasizes `cluster_touch_rate` instead of the more important `agent_focus_files` adherence metrics.

That means the agent-facing brief is compact, but the tool/report layer can still encourage or reward broad-cluster behavior. This is an alignment gap.

## Evidence From The n=10 v7 Smoke

Inputs reviewed:

- `C:\Users\Lenovo\Downloads\gt_v7_smoke_analysis\GT_V7_DEEP_ANALYSIS.md`
- `C:\Users\Lenovo\Downloads\gt_v7_smoke_analysis\paired_table.csv`
- `C:\Users\Lenovo\Downloads\gt_v7_smoke_analysis\LOCALIZATION_COMPARISON.md`
- `C:\Users\Lenovo\Downloads\gt_v7_smoke_analysis\brief_overlap.csv`

Smoke setup:

- n=10 SWE-bench Live Lite paired tasks
- model: `openrouter/xiaomi/mimo-v2-flash`
- harness: OpenHands CodeActAgent
- baseline resolved: 1/10
- GT v7 resolved: 1/10
- baseline patches: 7/10
- GT patches: 6/10
- v7 briefs injected: 10/10

Key measured failure:

- average brief files listed: 11.1
- min/max brief files listed: 9/12
- average brief chars: 2248.9
- average first-message chars: 13167.5
- brief visible in first message: 10/10

Localization rubric:

- baseline first-gold: 90
- GT v7 first-gold: 108
- baseline precision: 0.13
- GT v7 precision: 0.18
- baseline coverage: 0.48
- GT v7 coverage: 0.38
- tasks where the agent ever edited a gold file: baseline 5/10, GT v7 5/10

Reading:

- Precision is slightly higher for GT v7, but at the edge of the stated noise band.
- Coverage is lower by 10pp, which is a negative signal.
- First-gold is 18 steps slower, which is a negative signal.
- The run does not prove v7 helps localization; it shows at best no reliable signal and at worst speed/coverage regression.

Interpretation:

The run does not show a byte-transport failure. It shows a product-delivery failure. The agent received GT content, but the delivered content was not a usable plan: it listed too many files and did not force a ranked edit path. In product terms, delivery failed because the delivered payload was the wrong shape.

The important result is not pass@1, because n=10 with a 1/10 baseline has too little power. The important result is adherence: the brief was visible but weakly acted upon.

This directly supports the P0 decision:

- cap model-facing v7 to ranked `agent_focus_files`;
- measure focus adherence rather than broad cluster touch;
- compact `gt_plan` by default so tools do not reintroduce the same token/context problem.

## Research Anchors

OpenAI retired SWE-bench Verified for frontier reporting because benchmark contamination and flawed tests make it a poor signal for current frontier capability. OpenAI recommends moving toward harder and fresher benchmarks such as SWE-bench Pro. This means GT’s claims should emphasize controlled scaffold deltas and behavioral telemetry, not raw SWE-bench Verified-style numbers. Source: [OpenAI, “Why SWE-bench Verified no longer measures frontier coding capabilities”](https://openai.com/index/why-we-no-longer-evaluate-swe-bench-verified/).

SWE-Bench Mobile reports that agent design matters as much as model capability, with up to a 6x spread between agents using the same model, and that simple defensive prompts outperform complex ones. This supports GT’s move from large context dumps to short ranked instructions and runtime checks. Source: [SWE-Bench Mobile](https://arxiv.org/abs/2602.09540).

CodeRAG-Bench finds that retrieval helps only when the retrieved context is high quality and usable; retrievers often fetch weak context, and generators often fail to use retrieved context effectively. This maps directly to v7’s over-expanded-cluster failure: more context is not automatically better context. Source: [CodeRAG-Bench](https://aclanthology.org/2025.findings-naacl.176/).

SWE-ContextBench evaluates whether agents can reuse related repository/task context across tasks. GT’s project memory is currently opt-in and shallow, so v7 is not yet aligned with this direction except as a prototype. Source: [SWE Context Bench](https://arxiv.org/abs/2602.08316).

The Claude Code system-analysis paper argues that much of frontier agent performance comes from scaffolding around the model loop: permissions, compaction, hooks, MCP/plugins/skills, session storage, and worktree isolation. This supports treating GT hooks and telemetry as first-class benchmark scaffold components, not passive analysis. Source: [Dive into Claude Code](https://arxiv.org/abs/2604.14228).

Claude Code hooks can add context before prompts and can block or control some events. GT’s OpenHands hook path is weaker: it can warn and log, and the wrapper runs a final audit, but hard decision control is limited by harness support. Source: [Claude Code hooks documentation](https://code.claude.com/docs/en/hooks).

Codex product guidance emphasizes verifiable terminal/test evidence and repo-local instructions such as `AGENTS.md`, which agents use for testing, code style, and project conventions. GT v7 does not yet parse repo instruction files into constraints. Source: [OpenAI, “Introducing Codex”](https://openai.com/index/introducing-codex/).

SWE-Bench Pro emphasizes long-horizon tasks with multi-file changes and substantial code modifications. v7’s current 3-file focus mode is appropriate for Live Lite bug-fix smoke tests, but it needs an explicit long-horizon mode before being positioned as Pro-ready. Source: [SWE-Bench Pro](https://arxiv.org/abs/2509.16941).

SWE-Bench++ highlights multilingual benchmark generation across thousands of repositories and 11 languages. GT v7 still has Python-heavy contract extraction, side-file rules, and test selection. Source: [SWE-Bench++](https://arxiv.org/abs/2512.17419).

Benchmark mutation work argues that formal GitHub issue descriptions overestimate real chat-agent capability and that benchmarks should reflect realistic user-style queries. GT should evaluate on Live/Pro/mutated tasks and avoid claiming broad robustness from one formal issue distribution. Source: [Saving SWE-Bench](https://arxiv.org/abs/2510.08996).

## Current v7 Behavior

### Aligned

The pre-task brief now follows the compact-context rule.

Evidence:

- `src/groundtruth/pretask/v7_brief.py` defines `DEFAULT_MAX_AGENT_FILES = 3`.
- `src/groundtruth/pretask/v7_brief.py` defines `MAX_AGENT_BRIEF_CHARS = 3500`.
- `_agent_focus_files()` ranks compact edit targets.
- `_render_v7()` renders ranked edit targets and keeps the full cluster in plan JSON.
- `tests/pretask/test_v7_brief.py` asserts `len(agent_focus_files) <= 3`.
- `tests/pretask/test_v7_brief.py` asserts the rendered brief is no more than 3500 chars.
- `tests/pretask/test_v7_brief.py` asserts real source files outrank low-value `__init__.py`.

This is aligned with the frontier lesson from SWE-Bench Mobile and CodeRAG-Bench: simple, high-quality, directly usable context beats large context dumps.

### Not Aligned

`gt_plan` still dumps too much.

Evidence:

- `src/groundtruth/mcp/server.py` has `gt_plan()` returning `json.dumps(result, sort_keys=True)`.
- `src/groundtruth/cli/commands.py` has `gt_plan_cmd()` printing the full loaded plan JSON.

That means the agent can still receive the full `cluster_files`, full `contract_lines`, full `expected_side_files`, and broad evidence. This repeats the original v7 problem through a different surface.

Required change:

- Make compact plan the default:
  - `agent_focus_files`
  - top 2 contract lines
  - top constraints
  - confidence
  - explicit “full evidence in telemetry”
- Add a separate diagnostic-only full mode:
  - `gt_plan --full`
  - MCP `gt_plan(full: bool = False)`

Priority: P0 before 50-task smoke.

### Not Aligned

Reports reward broad cluster behavior instead of focus adherence.

Evidence:

- `src/groundtruth/runtime/report.py` reports `cluster_touch_rate`.
- There is no `brief_edit_overlap`, `focus_hit_at_1`, or `focus_hit_at_3`.
- `src/groundtruth/runtime/patch_auditor.py` audits against `cluster_files`, not `agent_focus_files`.

This is misaligned with the actual failure we observed: the agent saw the brief but edited only 1/4 recommended files. The primary v7 quality metric must be whether the agent edited the ranked focus files, not whether it touched anything in a broad cluster.

Required change:

- Add to patch audit/report:
  - `agent_focus_files_touched`
  - `brief_edit_overlap`
  - `focus_hit_at_1`
  - `focus_hit_at_3`
  - `edited_ranked_focus_files`
- Keep `cluster_touch_rate` as secondary diagnostics only.

Priority: P0 before 50-task smoke.

### Partially Aligned

Hooks exist, but runtime control is weaker than frontier hook systems.

Evidence:

- `benchmarks/swebench/gt_hook.py` logs `gt_patch_shape` and `gt_runtime`.
- `scripts/swebench/oh_gt_hook_wrapper.py` runs a final hook audit before extracting logs.
- The hook can warn and log, but hard blocking is harness-dependent.

Compared with Claude Code hook capabilities, GT currently lacks reliable native decision control in OpenHands. This is acceptable if disclosed as scaffold instrumentation, but it is not equivalent to a hook system that can consistently block, inject context, or force replanning.

Required change:

- Separate hook capabilities in reports:
  - `hook_logged`
  - `hook_visible_to_agent`
  - `hook_blocked`
  - `final_audit_only`
- Do not imply that every warning was seen by the agent unless telemetry proves it.

Priority: P1 for honest benchmarking.

### Partially Aligned

Test validation selects tests but does not fully validate.

Evidence:

- `src/groundtruth/runtime/test_runner.py` selects commands.
- It logs `gt_test_validation`.
- It does not execute the selected command or record exit code/failing test names.

This misses Codex-style verifiable terminal/test evidence. Selection is useful, but the planned contract said to log command, exit code, failing tests, and contract mapping.

Required change:

- Add `gt_run_tests --execute`.
- Log:
  - command
  - exit code
  - duration
  - stdout/stderr tail
  - failing test names if parseable
  - selected contract files
- Keep default as selection-only for benchmark cost control.

Priority: P1.

### Not Aligned

Project instructions are ignored.

Evidence:

- No v7 path currently parses `AGENTS.md`, `CLAUDE.md`, `CONTRIBUTING.md`, `README.md` test sections, or local developer docs into constraints.

Codex explicitly uses repo instruction files for tests and project conventions. Claude/Cursor-style tools also rely heavily on persistent project context and instructions. GT v7 currently misses this.

Required change:

- Add deterministic instruction extraction:
  - `AGENTS.md`
  - `CLAUDE.md`
  - `CONTRIBUTING.md`
  - `README.md` test commands
  - scoped package-level instruction files
- Render only the top 1-2 relevant constraints in the brief.
- Store full extraction in telemetry.

Priority: P1.

### Partially Aligned

Memory exists but is too shallow.

Evidence:

- `src/groundtruth/runtime/project_memory.py` detects repo identity, package manager, test layout, side-file conventions, generated/vendor patterns, and co-change clusters.
- It is opt-in and static.

This is a correct leakage-safe starting point, but it does not yet address SWE-ContextBench-style experience reuse across related tasks.

Required change:

- Keep cross-task memory disabled for comparative SWE-bench unless explicitly allowed.
- For non-comparative runs, add run-scoped memory:
  - prior task id
  - files touched
  - failing/passing commands
  - reusable constraints
  - repo-local conventions
- Add leakage controls:
  - no gold patch
  - no hidden tests
  - no cross-run carryover unless configured.

Priority: P2.

### Partially Aligned

Multilingual support is uneven.

Evidence:

- Scaffold patterns and test detection include multiple languages.
- Contract extraction and test command selection remain Python-leaning.
- Side-file detection handles some manifests but does not deeply model language-specific export/stub/changelog conventions.

This conflicts with SWE-Bench++’s multilingual direction.

Required change:

- Add language adapters for:
  - JavaScript/TypeScript: `package.json`, exports, `.test/.spec`, `tsconfig`, `types`.
  - Go: package tests, `go test ./...`, generated files, `go.mod`.
  - Rust: `Cargo.toml`, `cargo test`, module surfaces.
  - Java: Maven/Gradle, `*Test.java`, package paths.
- Add per-language side-file rules.

Priority: P2 unless the 50-task smoke includes non-Python repos.

### Not Aligned

No task-complexity mode.

Evidence:

- v7 now assumes a compact 3-file focus list.
- There is no explicit mode for long-horizon, multi-stage tasks.

This is good for Live Lite, but not enough for SWE-Bench Pro-style tasks that require substantial multi-file edits.

Required change:

- Add `task_complexity` to plan:
  - `lite_bugfix`
  - `multi_file_api_change`
  - `long_horizon`
- For `long_horizon`, render:
  - stage 1 focus files
  - expansion trigger
  - expected side files
  - validation path
- Keep the first message compact.

Priority: P2.

## Behavior Audit Of Our Own Recent Work

We violated our own intended review discipline once:

- We fixed the agent-facing v7 brief but did not initially check all other agent-facing surfaces.
- The hidden mismatch was `gt_plan`: it still returned full JSON.
- This is exactly the kind of tool/brief inconsistency that causes token dumping and agent non-adherence.

Corrective rule for future GT changes:

- Every new v7 surface must be classified as one of:
  - agent-facing compact
  - telemetry/full evidence
  - diagnostic-only
- Tests must cover token/size behavior for every agent-facing surface, not only the pre-brief.
- Reports must measure the exact behavior we want, not broad proxies.

## Required Changes Before 50-Task Smoke

P0:

1. Compact `gt_plan` by default.
2. Add full mode only for diagnostics.
3. Add focus-adherence metrics:
   - `brief_edit_overlap`
   - `focus_hit_at_1`
   - `focus_hit_at_3`
   - `agent_focus_files_touched`
4. Update the 50-task smoke handoff to treat focus adherence as the primary v7 metric.

P1:

5. Add `gt_run_tests --execute` with exit code and failing test logging.
6. Split hook telemetry into logged/visible/blocked/final-only states.
7. Parse project instruction files into constraints.

P2:

8. Add run-scoped memory with leakage controls.
9. Add language-specific adapters.
10. Add task-complexity modes for Pro/long-horizon workloads.

## Implementation Ledger

Vocabulary update:

- `transport_delivery_rate`: GT bytes or tool output reached the runtime/agent channel.
- `usable_delivery_rate`: delivered GT context was compact, ranked, and not a broad default dump.
- `adherence_rate`: the agent edited ranked `agent_focus_files`, reported primarily as focus hit@3.
- `outcome_rate`: task resolution/pass rate, kept separate from delivery and adherence.

Change ID: P0-compact-plan-surface

- Why: prevent `gt_plan` from reintroducing broad `cluster_files` into the agent context after the brief was compacted.
- Citation: CodeRAG-Bench finding that retrieval helps only when context is high-quality and usable; SWE-Bench Mobile result that scaffold/prompt surfaces can dominate outcomes.
- Code files: `src/groundtruth/runtime/plan_surface.py`, `src/groundtruth/cli/commands.py`, `src/groundtruth/main.py`, `src/groundtruth/mcp/server.py`.
- Telemetry block: `gt_plan_served` with `full`, `char_count`, `served_keys`, `agent_facing`, and `broad_full_plan_json`.
- Verification command: `python -m pytest tests\pretask tests\unit\test_runtime_patch_auditor.py tests\unit\test_v7_plan_surface.py -q`
- Result: `64 passed in 3.59s`.
- Remaining risk: MCP telemetry uses the default telemetry directory unless the runtime sets `GT_LOG_DIR`; OpenHands wrapper paths should be checked in smoke.

Change ID: P0-focus-adherence-metrics

- Why: measure whether the agent edited the ranked files v7 asked it to edit, not only whether it touched a broad cluster member.
- Citation: OpenAI SWE-bench Verified retirement note emphasizing behavioral telemetry over raw pass@1 claims; Claude Code hooks as precedent for runtime-level control/telemetry.
- Code files: `src/groundtruth/runtime/patch_auditor.py`, `src/groundtruth/runtime/report.py`.
- Telemetry block: `gt_patch_shape` now includes `agent_focus_files_touched`, `edited_ranked_focus_files`, `brief_edit_overlap`, `focus_hit_at_1`, `focus_hit_at_3`, and `focus_edit_precision`.
- Verification command: `python -m pytest tests\pretask tests\unit\test_runtime_patch_auditor.py tests\unit\test_v7_plan_surface.py -q`
- Result: `64 passed in 3.59s`.
- Remaining risk: adherence is based on edited paths, not semantic correctness; outcome analysis must stay separate.

Change ID: P0-usable-delivery-gate

- Why: distinguish byte transport from product-quality delivery.
- Citation: CodeRAG-Bench and SWE-Bench Mobile both support compact, task-shaped context over broad retrieval dumps.
- Code files: `src/groundtruth/runtime/plan_surface.py`, `src/groundtruth/pretask/v7_brief.py`, `src/groundtruth/runtime/report.py`.
- Telemetry block: `gt_usable_delivery` with `transport_delivered`, `brief_chars`, `first_message_chars`, `agent_focus_count`, `brief_file_mentions_count`, `usable_delivery_ok`, and `failure_reasons`.
- Verification command: `python -m pytest tests\pretask tests\unit\test_runtime_patch_auditor.py tests\unit\test_v7_plan_surface.py -q`
- Result: `64 passed in 3.59s`.
- Remaining risk: `first_message_chars` is nullable until harness-side capture feeds it into telemetry.

Change ID: P1-test-validation-execution

- Why: let GT validate selected visible repo-native tests without changing benchmark defaults.
- Citation: Codex product guidance emphasizes iterative test evidence and repo instructions.
- Code files: `src/groundtruth/runtime/test_runner.py`, `src/groundtruth/cli/commands.py`, `src/groundtruth/main.py`, `src/groundtruth/mcp/server.py`.
- Telemetry block: `gt_test_validation` records command, mode, selected contract files, exit code, duration, parsed failing test names, output tails, and timeout state.
- Verification command: `python -m pytest tests\pretask tests\unit\test_runtime_patch_auditor.py tests\unit\test_v7_plan_surface.py -q`
- Result: `64 passed in 3.59s`.
- Remaining risk: failing-test-name parsing is best effort for common runners and should be expanded when non-Python smoke coverage grows.

Change ID: P1-project-instruction-extraction

- Why: full-form GT must follow repo-local rules and validation hints, not only localized files.
- Citation: Codex-style repository instructions and agent-context research both point to scoped project guidance as a high-leverage control surface, but broad instructions must stay compact to avoid diluting focus.
- Code files: `src/groundtruth/pretask/project_instructions.py`, `src/groundtruth/pretask/v7_brief.py`, `tests/pretask/test_project_instructions.py`, `tests/pretask/test_v7_brief.py`.
- Telemetry block: `gt_project_instructions` with full evidence, selected sources, rendered constraints, extraction mode, and abstain reason.
- Verification command: `python -m pytest tests\pretask tests\unit\test_runtime_patch_auditor.py tests\unit\test_v7_plan_surface.py tests\unit\test_mcp_server.py tests\unit\test_mcp_tools.py -q`
- Result: `103 passed in 4.55s`.
- Remaining risk: relevance ranking is deterministic but still heuristic; tune after seeing which instructions correlate with adherence/outcome.

Change ID: P1-hook-truthfulness

- Why: reports must not imply the model saw or obeyed GT if GT only logged after the fact.
- Citation: hook-based coding-agent products distinguish visible/blocking runtime control from passive telemetry; benchmark claims need that distinction.
- Code files: `src/groundtruth/runtime/hook_truth.py`, `src/groundtruth/runtime/report.py`, `benchmarks/swebench/gt_hook.py`, `tests/unit/test_runtime_patch_auditor.py`.
- Telemetry block: hook logs now normalize/report `hook_logged`, `hook_visible_to_agent`, `hook_blocked`, and `final_audit_only`.
- Verification command: `python -m ruff check ...` and `python -m pytest tests\pretask tests\unit\test_runtime_patch_auditor.py tests\unit\test_v7_plan_surface.py tests\unit\test_mcp_server.py tests\unit\test_mcp_tools.py -q`
- Result: Ruff passed; `103 passed in 4.55s`.
- Remaining risk: harness-specific visibility still needs confirmation from actual OpenHands/SWE-agent observations.

## Alignment Verdict

Current v7 is acceptable for an instrumented Live Lite smoke now that the P0 compact-plan, focus-adherence, and usable-delivery fixes are implemented.

Without those P0 fixes, the smoke can still tell us whether GT participates and whether patches touch source files, but it cannot honestly answer the central question:

Did the compact v7 brief cause the agent to edit the ranked recommended files?

That question is now the core alignment metric for v7.
