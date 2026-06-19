# Pipeline Resilience: Proof Never Kills Trial

## The Problem

Proof, gate, and preflight steps that use `exit 1` kill the entire GHA job before the
trial/agent step can run. When a proof diagnostic fails (gate verdict, LSP cert, artifact
check), the agent never executes — the trajectory is lost, the run is unrecoverable, and
the failure surfaces as a broken job rather than a diagnostic warning.

**This wastes paid runs.** A proof failure is diagnostic information, not a reason to
skip the trial.

## The Fix Pattern (4 rules)

### Rule 1: Proof/gate steps get `continue-on-error: true`

Any step that runs proof diagnostics / gate verdicts / cert checks and could `exit 1`
before the trial must have this attribute:

```yaml
- name: GT substrate proof / 3-GATE VERDICT / pre-flight check
  continue-on-error: true
  run: |
    ...
```

### Rule 2: Trial/agent steps get `if: ${{ !cancelled() }}`

Any trial or agent step downstream of a proof/gate step must run even when the proof
failed. Replace the default `if: success()` with:

```yaml
- name: Run GT trial / Run agent
  if: ${{ !cancelled() }}
  run: |
    ...
```

If the step also has a pre-existing condition (e.g. `if: inputs.gates_only != 'true'`),
combine them:

```yaml
  if: ${{ !cancelled() && inputs.gates_only != 'true' }}
```

### Rule 3: Proof failures emit `::warning::` and set a diagnostic marker

Replace bare `exit 1` inside a proof step with a warning + continue. The pattern:

```bash
# BAD — kills the job silently:
docker run ... gt-run-proof ... || exit 1

# GOOD — emit warning, set PROOF_RC, continue:
PROOF_RC=0
docker run ... gt-run-proof ... || PROOF_RC=$?
if [ "$PROOF_RC" -ne 0 ]; then
  echo "GT_RUN_PROOF_FAIL: rc=$PROOF_RC" | tee -a trial_output.log
  echo "::warning::gt-run-proof exited $PROOF_RC — proof diagnostic failed but trial will proceed (continue-on-error pipeline)"
  exit "$PROOF_RC"   # exits non-zero, but continue-on-error=true absorbs it
fi
```

The `exit` at the end is still present — GHA sees a non-zero exit and marks the step
yellow (warning), but `continue-on-error: true` prevents it from killing the job.

### Rule 4: Upload/collect steps keep `if: always()`

Most upload steps already have this. Do not remove it.

---

## Fatal vs Diagnostic: Which `exit 1`s to Keep

Not every `exit 1` is wrong. The distinction:

### FATAL — keep the `exit 1` (these are always correct even without `continue-on-error`)

These represent infrastructure preconditions that make the entire run meaningless:

| Condition | Why fatal |
|---|---|
| `docker` binary missing | Can't run any container |
| `pier` not installed | Can't run DeepSWE |
| Task image pull failed 4x | Agent has nothing to run against |
| Env not armed (DEEPSEEK_API_KEY missing) | Agent can't make LLM calls |
| Dataset JSONL missing under HF offline | Agent would see 0 tasks |

### DIAGNOSTIC — need `continue-on-error: true` on the step

These describe GT context quality, not run viability. The agent can still attempt the
task and may succeed without GT context (or with degraded GT context):

| Condition | Why diagnostic |
|---|---|
| Graph cert verdict (gate ON/OFF) | GT might deliver partial context |
| LSP cert verdict | Agent can still run without LSP-enriched edges |
| Embedder cert verdict | Agent can run without semantic ranking |
| `foundational_gate_report.json` content | Quality measure, not a blocker |
| `gt-run-proof` non-zero exit | Proof failed, but the task image is fine |
| Missing GT artifacts after proof | Agent runs GT-OFF (baseline-equivalent) |
| `preflight_pipeline.py` non-zero exit | Stack may be degraded, not dead |
| Config checks (caching_prompt, etc.) | Warn the operator, still run |
| Resolution quality gates (det%, name_match) | Measurement, not a blocker |

---

## Reference Implementation

`deepswe_full.yml` is the canonical correct implementation. Key patterns:

**Proof step (line ~705):**
```yaml
- name: GT substrate proof
  continue-on-error: true
  run: |
    PROOF_RC=0
    docker run ... gt-run-proof ... || PROOF_RC=$?
    if [ "$PROOF_RC" -ne 0 ]; then
      echo "GT_RUN_PROOF_FAIL: rc=$PROOF_RC" | tee -a trial_output.log
      echo "::warning::gt-run-proof exited $PROOF_RC — proof diagnostic failed but trial will proceed (continue-on-error pipeline)"
      exit "$PROOF_RC"
    fi
    # artifact check — warn, not kill:
    for c in graph.db lsp_certificate.json ...; do
      test -s "/tmp/gt/$c" || {
        echo "::warning::missing artifact $c — trial proceeds GT-OFF"
        exit 1
      }
    done
```

**Trial step (line ~1046):**
```yaml
- name: Run GT trial
  if: ${{ !cancelled() }}
  run: |
    pier run ...
```

---

## Workflows and Their Status

| Workflow | Proof/gate step | Trial/agent step | Status |
|---|---|---|---|
| `deepswe_full.yml` | `continue-on-error: true` | `if: ${{ !cancelled() }}` | **REFERENCE — already correct** |
| `swebench_30task.yml` | `continue-on-error: true` on 3-GATE + RESOLUTION-QUALITY | `if: ${{ !cancelled() }}` | Fixed 2026-06-17 |
| `swebench_300task.yml` | `continue-on-error: true` on 3-GATE VERDICT | `if: ${{ !cancelled() && inputs.gates_only != 'true' }}` | Fixed 2026-06-17 |
| `verified_run.yml` | `continue-on-error: true` on GT substrate proof | `if: ${{ !cancelled() }}` | Fixed 2026-06-17 |
| `canary_3arm.yml` | (pre-index action, no change) | `if: ${{ !cancelled() }}` | Fixed 2026-06-17 |
| `deepswe_trial.yml` | `continue-on-error: true` on Preflight | `if: ${{ !cancelled() }}` | Fixed 2026-06-17 |
| `gt_v4flash_30.yml` | `continue-on-error: true` on Pre-flight | `if: ${{ !cancelled() }}` | Fixed 2026-06-17 |
| `deepswe_proof_sweep.yml` | Intentionally fail-closed | No trial step | **No change — proof-only sweep** |
| `gt_language_smoke.yml` | Intentionally fail-closed | No trial step | **No change — smoke/audit only** |
| `live_lite_inference.yml` | No proof-before-trial pattern | Inference step has `if: inputs.dry_run != true` | No change needed |
| `verified_inference.yml` | No proof-before-trial pattern | Inference step has `if: inputs.dry_run != true` | No change needed |
| `live_lite_eval.yml` | No proof-before-trial pattern | Eval step has no gate before it | No change needed |

---

## Adding New Proof Checks Without Breaking Resilience

When adding a new proof/gate step to any workflow that has a trial/agent step downstream:

1. Add `continue-on-error: true` to your new step
2. Use `echo "::warning::..."` to surface failures instead of bare `exit 1`
3. Tee diagnostic markers to `trial_output.log` so the outcome classifier sees them:
   ```bash
   echo "GT_MY_GATE_FAIL: reason" | tee -a trial_output.log
   ```
4. Verify the downstream trial/agent step already has `if: ${{ !cancelled() }}` — if not, add it
5. Check `deepswe_proof_sweep.yml` to confirm your new check should NOT be in that workflow
   (it is intentionally fail-closed; adding soft gates there would defeat its purpose)

The rule: **the proof is evidence about context quality, not a run gate. The trial always runs.**
