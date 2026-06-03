# Findings — 1 OH + 1 mini-swe GHA trial (2026-06-03 ~15:05 UTC)

Branch `gt-consensus-curation`. Trajectories read from the agent's OWN observation
records (output.jsonl history / pier result.json step_results), not GT-side telemetry.
**Corrects earlier same-session claims that credited GT off emission counts.**

## OH — canary_3arm, beetbox__beets-5495 (run 26892081558)

- **Resolution: REAL.** SWE-bench `run_evaluation` applied the patch and reported
  "Instances resolved: 1". Patch = `str()` coercion in `beets/importer.py::ImportTask`
  (and SingletonImportTask). Model = deepseek-v4-flash (Qwen3-Coder is only the
  cosmetic eval-report FILENAME — a stale `model_name_or_path` in convert_to_submission;
  would mislabel a real submission → FIX before submitting).
- **GT did NOT cause it — and emitted a CONFIDENT WRONG localization.** The brief the
  agent saw (history event 1) led with:
  `<gt-localization confidence="high"> Edit target: beets/ui/commands.py :: import_files`.
  The gold fix is in `importer.py`. The agent **never opened commands.py** (0 events);
  at event 8 it ran `grep "set_fields" beets/importer.py` — straight to gold — using the
  issue text's own symbol, then edited and verified importer.py. GT's high-confidence
  recommendation was wrong and ignored.
- **Fired ≠ delivered.** evidence_metrics.json claims 63 injections "covering gold";
  only **5 of 152** agent events carry any GT marker, and the single delivered
  localization was wrong. The "63 covering importer.py" is GT-side emission, not agent
  receipt.
- **Verdict:** parity (baseline also resolves beets-5495), GT not causal, and GT
  committed a Cursor-mentality + confidence-gating violation (high-confidence
  mislocalization on a task whose issue text names the symbol). On a weaker agent this
  could have CAUSED a failure. This is a GT bug, not a win.
- Internal inconsistency to check: a prior memory says "consensus fired & correct
  (importer.py)" for a beets OH run, yet the delivered `<gt-localization>` here = commands.py.
  Either consensus and L1 localization disagree (a bug) or the memory described a
  different layer.

## mini-swe — deepswe_trial, arktype-json-schema-refs-dependencies (run 26893077974)

- **HARNESS BROKEN — agent ran 0 steps.** pier result.json: `step_results=[]`,
  `n_agent_steps=null`, `agent_result` all null, `verifier_result.reward=0.0`,
  `exception=NonZeroAgentExitCodeError`.
- **Root cause:** `mini-swe-agent` v2.3.0 crashes in `run/mini.py:100 main ->
  get_environment(config.get("environment",{}), default_type="local")` while building
  config from `['mini.yaml','agent.cost_limit=0','custom.yaml']`. The pier
  mini_swe_agent adapter / config is incompatible with mini-swe-agent's **v2 migration**.
  The crash is BEFORE the task/brief is read.
- **"BRIEF_REACHED_AGENT: YES" is misleading.** gt_agent.py writes the augmented
  instruction to delivered_instruction.txt, THEN spawns the agent subprocess that dies
  at exit 1. The agent never consumed the brief. The gate should assert
  `n_agent_steps>0`, not "brief written to disk".
- Brief also shows the `<gt-task-brief>` **double-wrap** + a `if/then/else…Semantics`
  concatenation glitch (cosmetic vs the launch crash).
- **Consequence:** every mini-swe / DeepSWE "5-language full-potential" claim this
  session and in the prior confirm is hollow — the agent has never executed.

## Net

- OH harness works; the GT brief mislocalized at high confidence and the strong agent
  resolved despite it (parity, not a flip, plus a real GT confidence bug).
- mini-swe harness does not run the agent at all (mini-swe-agent v2 incompat).
- Nothing here is benchmark-team-sendable. To send anything: (1) fix mini-swe-agent v2
  compat OR pin the pre-v2 version; (2) fix the GT high-confidence-mislocalization +
  the submission model label + the brief double-wrap; (3) run a PAIRED N-task eval with
  baseline vs GT, report flips/regressions + Wilcoxon, per-task verified from agent
  observations.

## ROOT CAUSE (exact) + FIX APPLIED — why mini-swe never started

- mini-swe-agent v2.3.0 crash: `DockerEnvironmentConfig: image Field required`.
- Cause: `deepswe_gt_pier.yaml` `environment:` set `environment_class: docker` (+ docker-only
  `interpreter`, `pull_timeout`) but **no `image`**. pier runs mini-swe-agent INSIDE the
  pier-provisioned task container (`exec_as_agent`), so the agent's own env must be
  `local`; `docker` made it attempt docker-in-docker and v2 made `image` required →
  pydantic validation died BEFORE model/agent/task. 0 steps, every run.
- Confirmed schemas (mini-swe-agent v2.2.8 local): LocalEnvironmentConfig = {cwd, env,
  timeout}; DockerEnvironmentConfig requires `image`.
- Extra confirmation it should be local: GTMiniSweAgent patches
  `minisweagent.environments.local.LocalEnvironment` for <gt-evidence> — `docker` would
  bypass GT even without the crash.
- **FIX:** `environment_class: docker` → `local`; dropped docker-only interpreter/pull_timeout.
  (`deepswe_gt_pier.yaml`, commit f6060fa3.)
- **FIX #1 (launch crash) PROVEN; but "agent runs" was OVER-CLAIMED — CORRECTED below.**
  rerun 26894331544 (miniswe_v3_envfix): `n_agent_steps` 0 → 284, no more pydantic crash
  — the harness BOOTS and enters the agent loop. That part is real.
- **CORRECTION (read the 284-step mini-swe-agent.trajectory.json, 587 msgs):** the agent
  did ZERO useful work. The 284 "steps" are failure/reprompt cycles, not real execution:
  - **284/284 tool replies = `[Errno 2] No such file or directory: '/home/user'`;
    returncode0(success) = 0.** Not one command ran.
  - **245/284 assistant turns were EMPTY**; 16/17 user msgs = "No tool calls found in
    the response. retry." Run ended on `EOF when reading a line`.
  - Only **5** tool replies carried `<gt-evidence>`, all on FAILED commands. The earlier
    "49 gt-evidence / 178 gt_hook reaching the agent" was instruction text echoed across
    the conversation, NOT successful agent tool use. GT did NOT meaningfully reach a
    working agent.
- **TWO further blockers (mini-swe path still non-functional after fix #1):**
  - **Blocker A — wrong cwd.** `deepswe_gt_pier.yaml environment.cwd: "/home/user"` is
    hardcoded; the arktype repo root differs (gt_agent writes the real root to
    /opt/gt/gt_root.txt). Every command errors on the missing dir. NOT FIXED.
  - **Blocker B — no tool calls.** deepseek-v4-flash produced 245 empty responses; mini-
    swe-agent v2 found "no tool calls" and reprompted to the step limit. A v2
    action-format / model-parser incompatibility (config or model-class). NOT FIXED.
- Lesson (recurring): I claimed success from `n_agent_steps>0` + emission greps without
  reading the agent's tool outputs. returncode0=0 is the real proof, and it was 0.

## PREFLIGHT AUDIT — does it catch this? NO.

`scripts/verify/preflight_pipeline.py` runs 13 checks, ALL GT-internal:
graph_exists, schema_version, fts5, fts5_query, grep_available, path_seeds, edge_quality,
assertions, lsp_enrichment, lsp_edges, semantic_embedder, brief_generation, l3b_delivery.
**None verify the agent harness can launch** (no agent dry-run, no env-config validation,
no `n_agent_steps>0` post-check). That is why it printed "ALL CHECKS PASS" while the agent
crashed at startup. The preflight validates GT's context PRODUCTION and is blind to whether
the agent can CONSUME it. Gap to add later (not fixed this turn): a launch-viability check
(validate the mini-swe env config / agent dry-run) + a post-run assertion that steps > 0.

## Saved artifacts
- `.claude/reports/runs/20260603_143838__FINAL_ARCH_V2_Canary…26892081558__OH_canary_v2live_beets5495/`
- `.claude/reports/runs/20260603_145501__DeepSWE…26893077974__miniswe_v2_capture/`
- Both GCP VMs (gt-vm-1, gt-vm-2) STOPPED.
