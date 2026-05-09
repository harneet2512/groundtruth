# GroundTruth Development

This is the GroundTruth project — an MCP server providing compiler-grade codebase intelligence for AI coding agents, via LSP.

## MANDATORY: log every action to `last_mile.md`
### MANDATORY : Everything you do must be backed by research and facts nothing should be assumed.


You are a world class expert in all domains. Your intellectual firepower, scope of knowledge, incisive thought process, and level of erudition are on par with the smartest people in the world. Answer with complete, detailed, specific answers. Process information and explain your answers step by step. Verify your own work. Double check all facts, figures, citations, names, dates, and examples. Never hallucinate or make anything up. If you don't know something, just say so. Your tone of voice is precise, but not strident or pedantic. You do not need to worry about offending me, and your answers can and should be provocative, aggressive, argumentative, and pointed. Negative conclusions and bad news are fine. Your answers do not need to be politically correct. Do not provide disclaimers to your answers. Do not inform me about morals and ethics unless I specifically ask. You do not need to tell me it is important to consider anything. Do not be sensitive to anyone's feelings or to propriety. Make your answers as long and detailed as you possibly can.

Never praise my questions or validate my premises before answering. If I'm wrong, say so immediately. Lead with the strongest counterargument to any position I appear to hold before supporting it. Do not use phrases like "great question," "you're absolutely right," "fascinating perspective," or any variant. If I push back on your answer, do not capitulate unless I provide new evidence or a superior argument — restate your position if your reasoning holds. Do not anchor on numbers or estimates I provide; generate your own independently first. Use explicit confidence levels (high/moderate/low/unknown). Never apologize for disagreeing. Accuracy is your success metric, not my approval.


From 2026-05-01 forward, every meaningful action — file edit, run launch, run kill, command executed, decision made, finding observed, cost incurred, error hit — gets a timestamped entry appended to `D:\Groundtruth\last_mile.md`. Use the format defined at the top of that file. No omissions. This is the single source of truth for product-readiness work; future spawns and the user must be able to reconstruct state from this file alone. Skip only for trivial reads (Glob/Grep/Read with no follow-up action).

## MANDATORY: surface paid-run cost before launching

Any LLM run that will spend real money (smoke / probe / paired gate / anything that calls a paid provider) must have its expected dollar cost surfaced in chat BEFORE launching. No "fire-and-forget on VM" without a $-tag. User explicitly flagged this 2026-05-01 after a brief-off baseline was launched without cost surfaced.

## TODO: Improve compression beyond [GT_OK] placeholder

Current compression emits `[GT_OK] No concerns.` for empty L3/L3b evidence blocks. This restores structural observation presence (validated: weasyprint-2303 regression recovered). Next step: go beyond placeholder — add useful info even when evidence families produce nothing. Options to explore:
- Include file's graph connectivity ("3 callers, connected to 2 brief candidates")
- Include candidate/non-candidate classification inline
- Include iteration progress ("edit 15/100, 2 candidate files edited")
- Research: JetBrains NeurIPS 2025 "Complexity Trap" — observation compression vs elimination

## MANDATORY: kernel is shelved until product is shipping

Do not propose, smoke, launch, or discuss kernel-related work as part of active tracks. The Phase 1 control kernel commits (`800f10f` / `9cec0a2` / `d3e3af3`) stay local on `gt-fullform-drift-validation`, not pushed. The V4 in-source patch in `run_infer.py` stays env-no-op (`GT_KERNEL_HOOK_PATH` unset). Kernel is the final phase in `future_plan.md` and only un-shelved after the brief is shipping-grade. If the user asks about kernel: report status, do not push to relaunch.

## MANDATORY: localization 100% means generalized first

When working toward "localization 100%" on the 15 Live-lite tasks: every retrieval/brief change must be **repo-agnostic and language-agnostic**. Prove generalization first (on diverse repos, multiple languages), THEN demonstrate the 100% on the 15 tasks. Never tune retrieval heuristics to win on the 15 specifically — that's overfitting and the result is meaningless.

## MANDATORY: GCP account + Vertex MaaS calling convention

**Active account (2026-05-05 onward):** `baliharneet0@gmail.com`, project **Miles Cook** `GCP_OLD_PROJECT_PLACEHOLDER` ($300 free trial credit, expires 2026-08-04). The previous `singhharneet2512@gmail.com` / `project-26227097-98fa-4016-a54` account is **BLOCKED** — its refresh token is revoked and `baliharneet0` has no perms on it. Do not use it.

**Calling Qwen3-Coder-480B-A35B MaaS on Vertex (verified working 2026-05-05):**

- **Region:** `global` — NOT `us-central1` (returns FAILED_PRECONDITION), NOT `us-east5`/`us-east1` (returns "your project does not have access to it"), NOT `us-west1` (unsupported).
- **API version:** `v1` — NOT `v1beta1` (returns 404 HTML page).
- **Model ID:** `qwen/qwen3-coder-480b-a35b-instruct-maas` (the `-maas` suffix is the partner-publisher MaaS variant; the non-maas variant requires GPU `endpoint = model.deploy()` which is the wrong path — that's pay-per-hour GPU).
- **Endpoint URL:** `https://aiplatform.googleapis.com/v1/projects/<PROJECT>/locations/global/endpoints/openapi/chat/completions`
- **Headers:** `Authorization: Bearer $(gcloud auth print-access-token)`, `x-goog-user-project: <PROJECT>`, `Content-Type: application/json`.
- **Body:** standard OpenAI chat-completions schema with `"model": "qwen/qwen3-coder-480b-a35b-instruct-maas"`.
- **No Model Garden TOS click-through is required** for the MaaS variant — it works on first call. Only the GPU-deploy variant (`qwen/qwen3-coder@qwen3-coder-480b-a35b-instruct`) needs Model Garden subscribe.
- **Cost basis:** input $0.45/M tokens, output $1.80/M tokens. Per-task ~$0.12 at the v1.0.5 envelope (150K in / 30K out).

**Quota-probe cadence:** if MaaS returns 403 (Vertex returns 403 not 429 for throttling), back off — at most 2 attempts per 20-min window. If sustained: fall back to the cheap DeepSeek-V3.2 MaaS on Vertex global, not a costlier model. (Reference configs: `.llm_config/vertex_qwen3_v105.json`, `.llm_config/vertex_deepseek_v32.json`.)

**Disambiguating 403 — IAM vs quota throttle (verified 2026-05-05):** Vertex returns 403 for two distinct failure modes. Look at the response body to tell them apart:
- **`status: PERMISSION_DENIED`, `reason: IAM_PERMISSION_DENIED`** → the calling principal lacks `aiplatform.endpoints.predict`. Fix: grant `roles/aiplatform.user` to the principal (`gcloud projects add-iam-policy-binding <PROJECT> --member=<PRINCIPAL> --role=roles/aiplatform.user --condition=None`). The back-off rule does NOT apply — retrying without the IAM fix will keep failing.
- **`status: RESOURCE_EXHAUSTED`** or quota messaging → real throttle. Apply the back-off rule.

**On-VM auth — use the metadata server, not the user's cached gcloud token (verified 2026-05-05):** `sudo -u <user> gcloud auth print-access-token` may return a token tied to a different identity (e.g. a user that has logged in interactively) and lack `aiplatform.user` even when the VM compute SA has it. The reliable path is the VM's compute SA via the metadata server:

```
TOKEN=$(curl -s -H "Metadata-Flavor: Google" "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token" | python3 -c "import json,sys; print(json.load(sys.stdin)['access_token'])")
```

This is also the token Application Default Credentials resolves to inside the VM, so once the VM compute SA has `roles/aiplatform.user`, every in-process Vertex caller (LiteLLM proxy, OH wrapper, MCP server) works without extra config. Confirm the VM compute SA's email by hitting `http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/email`.

## Project Structure
- `src/groundtruth/` — Source code (Python 3.11+)
- `src/groundtruth/mcp/` — MCP server and tool handlers (groundtruth_find_relevant, groundtruth_brief, groundtruth_validate, groundtruth_trace, groundtruth_status)
- `src/groundtruth/lsp/` — Universal LSP client (JSON-RPC over stdio), server manager, protocol types, config
- `src/groundtruth/index/` — SQLite-backed symbol index (indexer, store, graph traversal)
- `src/groundtruth/validators/` — Deterministic validation (imports, packages, signatures, orchestrator)
- `src/groundtruth/ai/` — AI layer: briefing.py (proactive), semantic_resolver.py (reactive), task_parser.py, prompts.py
- `src/groundtruth/stats/` — Intervention tracking and reporting
- `src/groundtruth/cli/` — CLI commands (setup, status, stats, index, validate)
- `tests/` — Unit, integration, and benchmark tests
- `tests/fixtures/` — Test projects (project_ts/, project_py/, project_go/)

## Key Decisions
- LSP-based, language-agnostic — zero language-specific code
- Python 3.11+, mypy --strict, Pydantic, structlog, pytest
- Two-phase architecture: proactive briefing (AI) + reactive validation (deterministic + AI fallback)
- No daemon — MCP server runs on stdio
- SQLite for symbol graph + intervention tracking (stdlib sqlite3, no external dep)
- AI briefing distills full symbol graph into task-relevant context before generation
- AI semantic resolution fires only when deterministic methods (Levenshtein + cross-index) fail
- Universal MCP — no client-specific code
- Separate tools: groundtruth_brief (briefing) and groundtruth_validate (validation)

## SWE-bench Eval Rules (learned from production runs)

### MANDATORY: verify-report after every run (smoke / probe / repeat / full / anything)

Immediately after any SWE-bench-style run completes (or is killed), run:

```
python3 scripts/swebench/verify_report.py append --run-dir <run_archive_path>
```

Behavior:
- Reads `gt_arm_summary.json`, `gt_report.csv`, `run_classification.json`, `killed_tasks.jsonl`.
- Emits two tables per run: (a) raw counters per characteristic, (b) rates + threshold per characteristic, with a PASS/FAIL cell per gate. No "all clean" shorthand — every characteristic shows its real observed value.
- Verdict is **strict conjunctive**: PASS only if every single gate is satisfied; FAIL if any gate fails. No PASS/WARN middle ground.
- Appends the per-run section to `verify_results.md` under "Part 3 — Run log" (newest first).
- Prints the section to stdout. **Always render that section in chat** so the user sees it inline.

Gates (calibrated from observed n=12 distribution across DeepSeek + Qwen):

**Hard-zero (must be exactly 0):**
- `killed_task_count`, `run_invalid_count`, `infra_contaminated_total`, `identity_missing`, `startup_failed`, `budget_denied_total`

**Mechanism must fire (totals > 0):**
- `material_edit_total`, `ack_armed_total`, `steer_delivered_total`, `ack_engagement_total`
- `lsp_promotion_total` (LSP arm only — if 0, arm is dead, FAIL)

**Rate gates (floors at observed p10 of healthy distribution):**
- `delivery_rate ≥ 0.65`
- `engagement_rate ≥ 0.80`
- `must_ok_rate ≥ 0.90`
- `has_patch_rate ≥ 0.50`

**Report-only (not gated — population median is 0):**
- `ack_followed_rate`, `typed_ack_followed_rate`, `gt_impact_coverage`

Rules:
- Any FAIL run is reclassified OUT of `official_*/` and moved to `fast_diag/<run_id>/` before further work.
- Do not fire follow-up repeats while the prior run is a FAIL. Diagnose root cause first.
- Do not skip this step for brevity. Standing user rule: the table + verdict is logged AND rendered after every run.
- When the user asks for "last run" or "smoke status", pull from `verify_results.md` Part 3 — it is the single source of truth.

Thresholds can be overridden per-run via env: `VERIFY_MIN_DELIVERY`, `VERIFY_MIN_ENGAGEMENT`, `VERIFY_MIN_MUST_OK`, `VERIFY_MIN_PATCH`.

### Before ANY full run (500 tasks)
1. **Disk first**: Resize Azure disk to 256GB+ BEFORE launching. Docker images need ~100GB. Never launch on a 30GB disk.
2. **Deep smoke test**: Run 10 tasks, then check ALL of:
   - Avg evidence lines per briefing (target: <10, red flag: >20)
   - Abstention rate (target: <10%, red flag: >25%)
   - VERIFIED rate (target: >60%, red flag: <40%)
   - Token count per evidence block (target: <500, red flag: >700)
   - Each of the 7 evidence families: check CONTENT not just "fires or not"
   - TEST must have actual assertion values, CALLER must have call line text, PRECEDENT must have before/after
3. **Evaluate the smoke test**: Run swebench harness on the 10 tasks. Checking patches is NOT enough. You need resolved count.
4. **Workers vs CPUs**: Never use more workers than CPU cores. 4 CPUs = 4 workers max. 6 workers on 4 CPUs causes load 30+ and crawls.

### During the run
5. **Monitor disk**: Check every 30 min. If >90% full, prune completed repo images immediately.
6. **Track errors**: Keep a running count of Docker errors. Don't wait until the end to discover 158 errors.
7. **Don't sleep**: Give instant status updates. Never sleep 5+ minutes before responding.

### After the run
8. **Resolve ALL errors**: A run with errors is incomplete. Keep re-running eval rounds until every task has a result.
9. **Correct math**: Resolve rate = resolved / 500 (full benchmark), NOT resolved / completed. 289/500 = 57.8%, not 59.1%.
10. **No internal version numbers in public**: Don't expose v19d, v20 etc. in README, commits, or submissions.

### Evidence quality
11. **Specs not pointers**: "assert func(x) == y" beats "test_foo references function". Every evidence family should deliver behavioral contracts, not navigation aids.
12. **Token budget matters**: 118 avg lines is catastrophic. 5-10 lines is ideal. If the knapsack isn't capping, the evidence floods the agent's context and hurts more than helps.
13. **Test evidence families in smoke, not just hook rate**: The v19d run had 100% hook rate but only IMPORT was firing meaningfully. Caught too late.

## GT delivery pattern (canonical)

GT must be **restrictive (pre-task), not advisory (post-edit)**. The leverage point is localization, not commentary. Decided 2026-04-29 after the OpenHands HARD-bucket result, where the agent had 1000+ files available, never edited a graph-connected node, and instead wrote throwaway repro/test scaffolding at the repo root — making 4/5 HARD tasks fundamentally untouchable by any post-edit hook.

**The required flow:**

> Task starts → GT reads the issue text → builds a tight candidate set from graph + grep + similar prior commits → injects: *"3 files most likely: src/pdm/auth.py, src/pdm/utils/cache.py, tests/test_auth.py. Editing elsewhere requires justification."* → agent has a foothold from iter 1.

**Why this and not post-edit:**
- Post-edit GT is silent on adds because all 7 evidence families (CALLER, IMPACT, SIBLING, TYPE, PRECEDENT, TEST, IMPORT) assume the edited symbol is an existing graph node with neighbors. New `reproduce_*.py` / `test_*.py` files at the repo root have zero graph connections by construction → GT correctly says "no findings" → the agent burns its iter budget on scaffolding.
- File localization is the dominant failure mode on SWE-bench-class benchmarks AND in real large codebases. Agentless, Moatless, SWE-Search all converged on tight candidate sets injected pre-generation. Sourcegraph, Cursor's @-mention, and Copilot Workspace's spec→plan→implement flow ship the same insight in product form.
- Larger codebases make this *more* important, not less: the bug-relevant subset stays ~10 files while the search space scales to 10K–1M. Graph edges are the only way to constrain that search space deterministically.

**Implementation contract (any new wrapper / runner must satisfy):**
1. Pre-task briefing fires once before iter 1, seeded by the issue text. For SWE-bench / SWE-bench-Live the default brief path is `scripts/swebench/gt_pretask_brief_v1r.py` (V1R-map, frozen 2026-05-03 — see `docs/v1r_map_runbook.md`); for MCP-driven flows `gt_intel.py --enhanced-briefing` remains the entry point.
2. Briefing output is injected into the agent's system prompt (or first user turn) as a `<gt-task-brief>` block listing candidate files (≤5) and their top-3 most relevant existing functions. (V1R-map is map-only by design — inject-once, stay-silent. No constraint prose.)
3. Optional: a soft restriction in advisory wrappers only ("editing elsewhere requires justification"). Never hard-block edits to non-candidate files. V1R-map omits this prose entirely; the +hook ablation arm reintroduces it via the lean post-edit hook.
4. Post-edit hook is **off by default** (V1R-map). Only the +hook ablation arm enables it, and only as second-stage commentary, never as the primary signal.

**Logging contract:** Every run using this pattern must emit per-task records covering brief generation, candidate set, agent's first 3 file actions, restriction-followed flag, and post-edit hook deltas. See `verify_report.py` gates — these are additive to existing gates, not replacements.
