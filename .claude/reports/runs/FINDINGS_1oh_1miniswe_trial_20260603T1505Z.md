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

## Saved artifacts
- `.claude/reports/runs/20260603_143838__FINAL_ARCH_V2_Canary…26892081558__OH_canary_v2live_beets5495/`
- `.claude/reports/runs/20260603_145501__DeepSWE…26893077974__miniswe_v2_capture/`
- Both GCP VMs (gt-vm-1, gt-vm-2) STOPPED.
