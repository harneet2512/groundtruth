# artifact_verified — PATH B: SWE-bench Verified, GT-on via mini-swe-agent (prep, 2026-06-10)

SWE-bench Verified-500 with `deepseek/deepseek-v4-flash` on GHA, GT-on through
**mini-swe-agent's own swebench path** (NOT pier). Mirrors the proven DeepSWE
substrate-consume pattern (`artifact_deepswe/gt_agent.py` + `gt_mini_patch.py` +
`.github/workflows/deepswe_full.yml`; spec: `gt_gt.md` §6/§13).

Files:

| File | Role |
|---|---|
| `gt_verified_agent.py` | The GT-injected runner: brief prepend + host-side observation interception + §H `[GT_META]` witness, fail-closed |
| `verified_gt.yaml` | mini-swe-agent config (upstream swebench.yaml base + GT awareness section + DeepSeek paper-locked sampling) |
| `../.github/workflows/verified_run.yml` | `workflow_dispatch`-ONLY per-task matrix (substrate proof → agent → official Princeton eval → witness verify → upload) |
| `../tests/test_verified_adapter.py` | Dry unit tests of both injection points + workflow guards |

---

## 1. Wiring map — where each injection lands in the INSTALLED minisweagent (2.2.8)

Package: `…/site-packages/minisweagent/` (source-read evidence, exact lines):

### Harness launch shape (`run/benchmarks/swebench.py`)
- `process_instance()` **:136-191** — the per-instance unit. `task = instance["problem_statement"]` (**:149**), `env = get_sb_environment(config, instance)` (**:160**), `agent = ProgressTrackingAgent(model, env, …)` (**:161-167**), `info = agent.run(task)` (**:168**), `update_preds_file(output_dir / "preds.json", …)` (**:190**).
- `get_swebench_docker_image_name()` **:82-90** — `instance.get("image_name") or instance.get("docker_image")` (**:84**) wins over the derived Docker Hub `_1776_` default (**:87-89**). → setting `instance["image_name"]` routes the agent to the pre-pulled GHCR-epoch image with zero upstream changes.
- `get_sb_environment()` **:93-108** — `environment_class` default `docker` (**:95**), `env_config["image"] = image_name` (**:98**), optional `env_startup_command` (**:103-107**).
- The batch CLI (`main()` **:215-286**) hardcodes `ProgressTrackingAgent` and offers no agent-class hook — hence our own thin runner (`run_instance()` in `gt_verified_agent.py`) that mirrors `process_instance` and reuses `update_preds_file`/`remove_from_preds_file`/`get_sb_environment` by import.

### Injection point (a) — INSTRUCTION (the brief)
- `agents/default.py` `DefaultAgent.run()` **:77-84** — `extra_template_vars |= {"task": task}`; the first user message renders `instance_template` with `{{task}}` (`config/benchmarks/swebench.yaml:4-8`).
- So **prepending the substrate brief to the `task` string before `agent.run(task)`** lands it verbatim at the top of the first user message — the same consume-only flow as `gt_agent.run()` (`artifact_deepswe/gt_agent.py:791-836`): `_emit_gt_meta_witness()` (:807) → `_generate_brief()` (:814, fail-closed) → `_prepend_brief()` (:815, single `<gt-task-brief>` tag invariant, :561-575) → preamble append (:820-823) → `delivered_instruction.txt` persist (:825-834).
- These functions are **imported, not ported** (zero drift): `gt_verified_agent.py` loads `artifact_deepswe.gt_agent` after a pier import-shim (`_ensure_pier_importable()`) — gt_agent's module header imports pier (`gt_agent.py:59-62`) solely for the pier class surface, which the Verified path never uses; inert stubs satisfy the import when pier is absent.

### Injection point (b) — ENVIRONMENT execute (per-turn evidence)
- `environments/docker.py` `DockerEnvironment.execute()` **:101-138** — every agent action is one `docker exec` against the task container. **The agent process runs ON THE HOST** (key difference vs pier/DeepSWE, where mini-swe-agent ran inside the container).
- `artifact_deepswe/gt_mini_patch.py` patches environment **classes** generically: `_ENV_CLASSES` already names `("minisweagent.environments.docker", "DockerEnvironment")` (**gt_mini_patch.py:1217-1221**); `_install()` (**:1224-1243**) wraps `cls.execute` and stamps `_gt_patched`. So on this path the attach is just `import gt_mini_patch` **in the host process** (`attach_gt_patch()`), no `.pth`/base64/container injection needed. Class-level wrap ⇒ safe before or after env instantiation.
- The pillars consume the substrate graph via env: `_db_path()` reads `GT_HOST_GRAPH_DB` unconditionally (**gt_mini_patch.py:228-246**) — a **host** path here (`/tmp/gt/graph.db`); `_connect_ro` opens it `mode=ro&immutable=1` in substrate mode (**:394-436**); L6 reindex is gated OFF in substrate mode (**:1081-1082**) so the certified graph is never mutated. `GT_ROOT_FILE` is pointed at `/tmp/gt/gt_root.txt` → `/tmp/gt/src` (the extracted repo copy) so `_code_at` snippet resolution works host-side.
- **Env → container path (verified, available if ever needed):** `DockerEnvironment.execute` forwards `config.forward_env` (**docker.py:109-110**) and `config.env` (**:111-112**) as `docker exec -e KEY=VALUE`. GT itself does not need in-container vars on this path (everything GT runs host-side), but the mechanism is the `verified_gt.yaml environment.env` block.

### Submission / trajectory plumbing (unchanged upstream)
- `exceptions.py:9-11` `Submitted(InterruptAgentFlow)` raised by `DockerEnvironment._check_finished` (**docker.py:140-151**) on `COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`; `DefaultAgent.run` catches `InterruptAgentFlow` (**default.py:88-89**) and returns `{"exit_status", "submission"}` (**:95-97**).
- `models/__init__.py:45-62` `get_model` → `LitellmModel` default; `cost_tracking: "ignore_errors"` is a valid config field (**models/litellm_model.py:35**) — required because litellm has no price-map entry for `deepseek-v4-flash`.

---

## 2. Fail-closed contract (identical to DeepSWE, no pier swallow)

In proof/substrate mode (`GT_PROOF_MODE=1` / `GT_HOST_GRAPH_DB`/`GT_CERT_DIR`/`GT_PORTABLE_SUBSTRATE` set):
- witness mismatch / unconsumable graph → `[GT_META] … error=DEEPSWE_ADAPTER_FAIL` + `DeepSweAdapterError` (gt_agent `_emit_gt_meta_witness`, :578-767);
- missing/empty `brief.txt` → fail-closed (gt_agent `_substrate_brief`, :442-502 — no host `generate_v1r_brief`);
- observation patch failing to attach → `GT_PATCH_NOT_ATTACHED` fail-closed (`attach_gt_patch()` — Verified-path addition: a half-delivered GT-on arm is unprovable);
- the runner itself exits rc=2 on `DeepSweAdapterError` — there is **no pier here to swallow the raise**; the workflow's grep is belt-and-braces only.
- `GT_BASELINE=1` → control arm: no witness, no brief, no patch, untouched instruction (read at call time).

## 3. Workflow (`verified_run.yml`) — dispatch-only, per-task matrix

Per task: image pull (GHCR-epoch primary `ghcr.io/epoch-research/swe-bench.eval.x86_64.<iid>:latest`, Docker Hub `_1776_` fallback — conventions validated in `scripts/vm/build_verified_manifest.py:71-89`; the adapter `--print-image` is the single naming source, parity-tested) → source extract (`/testbed` → `/tmp/gt/src`) → **pinned substrate proof** (mirror of `deepswe_full.yml:369-533`: immutable-digest assert, `gt-run-proof` on `/work:ro`, 8-artifact check incl. `brief.txt`, `$GITHUB_ENV` handoff — HOST paths) → GT agent run (pipefail + `${PIPESTATUS[0]}`, `tee trial_output.log`) → witness verify → **official Princeton eval** (`python -m swebench.harness.run_evaluation`, swebench==4.1.0, `-n swebench` against the pre-tagged task image — the CARDINAL no-custom-eval rule) → outcome extract → upload `if: always()`. INFRA markers: `TASK_IMAGE_PULL_FAIL`, `GT_SUBSTRATE_PULL_FAIL`, `GT_RUN_PROOF_FAIL`, `GT_ARTIFACT_MISSING`, `GT_ISSUE_MISSING`, `DEEPSWE_ADAPTER_FAIL`, `AGENT_RUN_FAIL`, `EVAL_HARNESS_FAIL`. `timeout-minutes: 60`, `max-parallel` input (default 12).

Deliberate divergences from `deepswe_full.yml` (each with a reason):
- **No `HF_*_OFFLINE`** — the dataset (problem_statement) + official eval need HF; the substrate stays offline internally.
- **No pier / no `--ae` / no `--mounts-json`** — the agent and the GT pillars run host-side; env handoff is plain `$GITHUB_ENV` with host paths.
- **Transitional mode (`require_pinned_substrate=0` + no digest)** runs the task **GT-OFF (`GT_BASELINE=1`)** with a loud warning, instead of DeepSWE's brief-off-but-proof-on shape (which would fail-closed in the adapter).

### Dispatch — 5-task trial
```bash
gh workflow run verified_run.yml --ref gt-trial \
  -f max_tasks=5 -f shard=0/1 \
  -f model=deepseek/deepseek-v4-flash \
  -f max_parallel=5 \
  -f gt_substrate_digest=ghcr.io/<org>/gt-substrate@sha256:<digest>   # or rely on vars.GT_SUBSTRATE_DIGEST
# baseline control arm: add -f baseline=true
# targeted re-run:      -f instance_ids=astropy__astropy-12907,django__django-11099
```

## 4. UNKNOWNs only the live 5-task trial can answer (ranked)

1. **Substrate vs Verified repos** — does `gt-run-proof` (built/gated on DeepSWE repos) pass its full-stack gates on the 12 large Verified Python repos (django/sympy scale) within the job budget? Gate FAIL or >60-min indexing are the highest-probability killers.
2. **Brief relevance at Verified scale** — `brief.txt` is generated in-substrate from `/tmp/issue.txt`; Verified problem statements are longer/noisier than DeepSWE instructions. Localization quality is unmeasured here.
3. **Host-side per-turn evidence hit-rate** — agent commands use container-relative paths (cwd `/testbed`); `_norm_fp` exact-match against graph `file_path` should align, but absolute `/testbed/...` paths fall to correct-or-quiet empties. Live trajectories will show the actual `<gt-evidence>` fire rate.
4. **Official-harness image reuse** — the eval step pre-tags `swebench/sweb.eval.x86_64.<slug>:latest`; whether swebench 4.1.0 uses the local tag or re-pulls/builds (disk/time) needs one live run.
5. **deepseek-v4-flash thinking-disable via litellm extra_body** on mini-swe-agent 2.2.8 (proven on the pier path with the same yaml keys; unproven on `LitellmModel` host-side).
6. **60-min ceiling** — agent (≤250 steps) + eval (≤30-min harness timeout) in one job; flash latency makes this likely-but-unproven.
7. **Disk** — task image + substrate image + HF cache on a 14GB-free hosted runner.
