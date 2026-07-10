# shellcheck shell=bash
###############################################################################
# gt_ae_block.sh — canonical `--ae` block for the GT runtime env.
#
# SCOPE / WIRED-STATUS (read before trusting this as de-drift):
#   This block is CURRENTLY sourced by ONE caller — railway/codespace_deepswe_run.sh
#   (the codespace witness path). It is NOT yet sourced by the GHA workflows:
#     • .github/workflows/deepswe_trial.yml — its `pier run` passes NO --ae / --mounts-json.
#     • .github/workflows/deepswe_full.yml  — forwards only a PARTIAL inline --ae set
#       (GT_ORACLE_ROUTE + GT_ORACLE_EVENTS), not the full trio below.
#   So on the two GHA paths the structural edit-risk axis (G03/G04) and the G11 deep-
#   telemetry trio (GT_RUNTIME_LEDGER / GT_HOOK_FIRE_COUNTS / full GT_ORACLE_EVENTS sink)
#   remain DARK. Making this a TRUE single source is OWED: source this file from those
#   run-steps and splice "${GT_AE_ARGS[@]}" + a writable --mounts-json. Until then this is
#   the codespace path's helper — do NOT read it as proof that trial/full are de-drifted.
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
#   deepswe_full.yml carries a partial `--ae` set inline; the TRIAL paths
#   (deepswe_trial.yml + codespace_deepswe_run.sh) historically passed NO `--ae`
#   at all -> the entire structural risk axis + oracle telemetry was dark on the
#   witness path. This block is the canonical definition that ELIMINATES that drift
#   ON ANY CALLER THAT SOURCES IT — today that is codespace_deepswe_run.sh only (see
#   SCOPE above). It does not retroactively de-drift a path that does not source it.
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
  # The in-container CODE defaults this axis OFF (byte-identical legacy). This block's
  # JOB is to turn it ON via --ae (that is the G03/G04 fix — the axis was dark in-
  # container). Default ON here; a host-side export of GT_VERIFY_STRUCTURAL_RISK=0
  # overrides back to OFF. SHOULD be present in --ae on every path; currently wired
  # only where this block is sourced (codespace) — OWED on trial/full.yml (see SCOPE).
  --ae "GT_VERIFY_STRUCTURAL_RISK=${GT_VERIFY_STRUCTURAL_RISK:-1}"
  --ae "GT_VERIFY_RISK_TRIGGER=${GT_VERIFY_RISK_TRIGGER:-0.5}"

  # ── Oracle two-lane route (steer lane on; legacy unconditional appends off) ──
  --ae "GT_ORACLE_ROUTE=${GT_ORACLE_ROUTE:-1}"

  # ── FORM native-render arms (D-8 gateway / RL-3 steer): render facts + steers in
  #    the native environment voice (tag-free) instead of the <gt-*> tagged block.
  #    Same content, different FORM. Behavioral flags, default OFF in-code (byte-
  #    identical); forwarded so they are enableable in prod. ──
  --ae "GT_GATEWAY_NATIVE=${GT_GATEWAY_NATIVE:-0}"
  --ae "GT_STEER_NATIVE=${GT_STEER_NATIVE:-0}"

  # ── RL-1 envelope unification (Lane-A/Lane-B -> the ONE EvidenceEnvelope
  #    contract: shared chain + dedup stamp-at-seal + receipts). Behavioral flag,
  #    default OFF in-code (byte-identical); forwarded so it is enableable in prod. ──
  --ae "GT_LANE_ENVELOPE=${GT_LANE_ENVELOPE:-0}"

  # ── O-2 obligation freshness (stale-PASS -> EDITED demotion on a post-test edit).
  #    Default OFF (byte-identical): the obligation path has early-fire fragility, so
  #    this ships behind a flag until measured. ──
  --ae "GT_OBLIGATION_FRESHNESS=${GT_OBLIGATION_FRESHNESS:-0}"

  # ── S-1 D7 relatedness gate (an edit/test credits a delivered kind as consumed only
  #    when it TOUCHES that block's target). Default OFF (byte-identical): the D7 counts
  #    drive live severity-boost + skip, so ships behind a flag until measured. ──
  --ae "GT_D7_RELATEDNESS=${GT_D7_RELATEDNESS:-0}"

  # ── Deep 8-dp telemetry sinks (CLAUDE.md mandate) -> host-mounted /gt_out ─────
  # Without these the in-container producers default to /tmp/* and DIE with the
  # container (gap G11). Point them into the writable mount so they survive.
  --ae "GT_ORACLE_EVENTS=${GT_ORACLE_EVENTS:-${GT_C_OUT}/gt_oracle_events.jsonl}"
  --ae "GT_RUNTIME_LEDGER=${GT_RUNTIME_LEDGER:-${GT_C_OUT}/gt_runtime_ledger.jsonl}"
  --ae "GT_HOOK_FIRE_COUNTS=${GT_HOOK_FIRE_COUNTS:-${GT_C_OUT}/gt_hook_fire_counts.json}"
)
