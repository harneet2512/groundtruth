# shellcheck shell=bash
###############################################################################
# gt_ae_block.sh — SINGLE SOURCE OF TRUTH for the GT runtime `--ae` block.
#
# WHY THIS FILE EXISTS (catalog gaps G03 + G04, HIGH):
#   pier does NOT blanket-forward the host's os.environ into the task container.
#   A plain host `export GT_FOO=1` is DROPPED. The ONLY verified forwarding path
#   is `pier run --ae KEY=VALUE` -> AgentConfig.env (cli/jobs.py) -> factory
#   extra_env -> agent._extra_env -> build_process_env -> exec(env=) ->
#   DockerEnvironment.exec appends `-e KEY=VALUE`. (See deepswe_full.yml "hole #5"
#   comment for the full trace.) The pier env whitelist in deepswe_gt_pier.yaml
#   (`environment.env`) carries ONLY PAGER/MANPAGER/LESS/PIP_PROGRESS_BAR/
#   TQDM_DISABLE — NO GT_* vars. So every in-container, env-gated GT producer
#   (structural edit-risk axis, oracle two-lane route, the 8-dp deep telemetry
#   sinks) runs with EMPTY GT env unless we pass `--ae` explicitly.
#
#   Before this file, deepswe_full.yml carried a partial `--ae` set inline while
#   the TRIAL path (deepswe_trial.yml + codespace_deepswe_run.sh) passed NO `--ae`
#   at all -> the entire structural risk axis + oracle telemetry was dark on the
#   witness path. Centralizing the block here means trial and full CANNOT drift.
#
# CONTRACT for callers:
#   1. Define GT_C_OUT before sourcing — the IN-CONTAINER directory the deep 8-dp
#      telemetry sinks write to. It MUST be a host-mounted, WRITABLE bind target
#      (so the records survive the container; gap G11). full.yml uses /gt_out;
#      the trial/codespace paths mount a host dir to /gt_out too. Defaults to
#      /gt_out if unset.
#   2. Optionally pre-set any GT_* var below in the environment to override the
#      default (e.g. GT_VERIFY_STRUCTURAL_RISK, GT_ORACLE_ROUTE).
#   3. `source` this file, then splice "${GT_AE_ARGS[@]}" into the `pier run`
#      command line.
#
# RANK-SAFETY NOTE (I2): none of these vars touch the localizer reach/RANK
# surface. They gate (a) the verify-axis structural edit-risk advisory, which the
# catalog confirms is a SCOPE/RISK substrate signal node-local-or-quiet, never a
# rank term, and (b) telemetry sink paths. No depth edge enters reach/rank via
# this block.
###############################################################################

# In-container writable telemetry dir (host-mounted). Caller may override.
GT_C_OUT="${GT_C_OUT:-/gt_out}"

# Build the canonical `--ae` array. Each entry honors a host-side override but
# defaults to the architecture-of-record value.
GT_AE_ARGS=(
  # ── Verify-axis structural edit-risk (gaps G03/G04) ──────────────────────────
  # Default-OFF in-container (byte-identical legacy) — the HARNESS turns it ON.
  # GT_VERIFY_STRUCTURAL_RISK must be present in --ae on EVERY path incl full.yml.
  --ae "GT_VERIFY_STRUCTURAL_RISK=${GT_VERIFY_STRUCTURAL_RISK:-1}"
  --ae "GT_VERIFY_RISK_TRIGGER=${GT_VERIFY_RISK_TRIGGER:-0.5}"

  # ── Oracle two-lane route (steer lane on; legacy unconditional appends off) ──
  --ae "GT_ORACLE_ROUTE=${GT_ORACLE_ROUTE:-1}"

  # ── Deep 8-dp telemetry sinks (CLAUDE.md mandate) -> host-mounted /gt_out ─────
  # Without these the in-container producers default to /tmp/* and DIE with the
  # container (gap G11). Point them into the writable mount so they survive.
  --ae "GT_ORACLE_EVENTS=${GT_ORACLE_EVENTS:-${GT_C_OUT}/gt_oracle_events.jsonl}"
  --ae "GT_RUNTIME_LEDGER=${GT_RUNTIME_LEDGER:-${GT_C_OUT}/gt_runtime_ledger.jsonl}"
  --ae "GT_HOOK_FIRE_COUNTS=${GT_HOOK_FIRE_COUNTS:-${GT_C_OUT}/gt_hook_fire_counts.json}"
)
