# shellcheck shell=bash
###############################################################################
# gt_ae_block.sh — canonical `--ae` block for the GT runtime env.
#
# SCOPE / WIRED-STATUS (read before trusting this as de-drift):
#   This block is sourced by TWO callers today (Brief-F10 — the prior "ONE caller /
#   full.yml does not source it" claim was STALE):
#     • railway/codespace_deepswe_run.sh — sources it (~L280) + splices "${GT_AE_ARGS[@]}"
#       (~L291). This is the ONLY forwarding on the codespace witness path, so EVERY
#       member below MUST live in the array (that is why the Brief-F4 trio was added).
#     • .github/workflows/deepswe_full.yml — sources it (~L1052) + splices "${GT_AE_ARGS[@]}"
#       into GT_AE_ARM (~L1104), AND additionally passes its OWN explicit --ae set
#       (GT_VERIFY_EXECUTE / GT_EDIT_CHECK / GT_GATEWAY at ~L1081-1084, telemetry sinks,
#       etc.) — so full.yml forwards the members even where the array historically did not.
#   Still NOT sourcing it: .github/workflows/deepswe_trial.yml — its `pier run` passes NO
#   --ae / --mounts-json, so on the trial path the structural edit-risk axis (G03/G04) and
#   the G11 deep-telemetry trio (GT_RUNTIME_LEDGER / GT_HOOK_FIRE_COUNTS / full
#   GT_ORACLE_EVENTS sink) remain DARK. Making trial a TRUE single source is still OWED:
#   source this file from that run-step and splice "${GT_AE_ARGS[@]}" + a writable
#   --mounts-json.
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
#   The TRIAL path (deepswe_trial.yml) historically passed NO `--ae` at all -> the
#   entire structural risk axis + oracle telemetry is dark there. This block is the
#   canonical definition that ELIMINATES that drift ON ANY CALLER THAT SOURCES IT —
#   today that is codespace_deepswe_run.sh AND deepswe_full.yml (see SCOPE above). It
#   does not retroactively de-drift a path that does not source it (deepswe_trial.yml).
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

# ── B-16/B-17: official RL profile fan-out (src/groundtruth/runtime/rl_profile.py) ──
# GT_RL_PROFILE is ONE versioned toggle for the coherent RL-adherence stack
# (GT_GATEWAY·GT_GATEWAY_NATIVE·GT_STEER_NATIVE·GT_LANE_ENVELOPE·GT_EDIT_CHECK·
# GT_VERIFY_EXECUTE·GT_D7_RELATEDNESS·GT_OBLIGATION_FRESHNESS). When it is set we
# EXPORT its member flags here — BEFORE the GT_AE_ARGS array below and before the
# caller's own `--ae GT_X="${GT_X:-0}"` sites — so both pick up the resolved values.
# An EXPLICITLY-set member wins (per-flag control). If a requested member capability
# is unavailable the resolver exits non-zero and we ABORT the run before any model
# spend (fail-closed). When GT_RL_PROFILE is UNSET/"0" this block is a strict no-op
# (no member flag changed, no python invoked) → byte-identical to the legacy path.
if [ -n "${GT_RL_PROFILE:-}" ] && [ "${GT_RL_PROFILE}" != "0" ]; then
  _GT_RL_PY="${GT_RL_PROFILE_PY:-python}"
  command -v "${_GT_RL_PY}" >/dev/null 2>&1 || _GT_RL_PY=python3
  # Resolver reads GT_RL_PROFILE + explicit members + optional GT_RL_PROFILE_AVAILABLE
  # from the environment; prints `export GT_X=...` on success, or GT_RL_PREFLIGHT_ABORT
  # to stderr and a non-zero exit when the profile is partially unavailable.
  if _GT_RL_EXPORTS="$("${_GT_RL_PY}" -m groundtruth.runtime.rl_profile --emit-exports)"; then
    eval "${_GT_RL_EXPORTS}"
    echo "GT_RL_PROFILE=${GT_RL_PROFILE} fan-out: ${_GT_RL_EXPORTS//$'\n'/ }"
  else
    echo "FATAL(GT_RL_PROFILE=${GT_RL_PROFILE}): RL-profile resolver aborted (fail-closed)" \
         "— a requested member capability is unavailable; refusing to spend model budget." >&2
    exit 1
  fi
  unset _GT_RL_PY _GT_RL_EXPORTS
fi

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

  # ── B-19/B-20 contract enrichment + B-3 gateway edit bridges. Behavioral flags,
  #    default OFF in-code (byte-identical); forwarded so they are enableable in prod.
  #    B-19 mode-conditions the [CALLERS] 'preserve' narration (suppress on ADD, emit a
  #    signature-delta on an intentional change); B-20 adds the bilateral [CONSUMED]
  #    caller-consumption line; B-3 reconstructs changed_files + edit_before_after so the
  #    Gateway's patch_delta producer becomes reachable on an edit turn. ──
  --ae "GT_CONTRACT_MODE=${GT_CONTRACT_MODE:-0}"
  --ae "GT_CONTRACT_BILATERAL=${GT_CONTRACT_BILATERAL:-0}"
  --ae "GT_GATEWAY_EDIT_BRIDGES=${GT_GATEWAY_EDIT_BRIDGES:-0}"

  # ── B-16/B-17 RL-profile trio that had NO --ae entry (Brief-F4). The resolver above
  #    EXPORTS these host-side, but pier DROPS host env — only GT_AE_ARGS crosses into
  #    the container. Without them, a GT_RL_PROFILE run activated only 8/11 members on
  #    any caller that splices ONLY GT_AE_ARGS (the codespace witness path), shipping an
  #    INCOHERENT pair (GT_GATEWAY_NATIVE=1 with GT_GATEWAY dark). Forward them here so
  #    THIS block is the single source (don't rely on callers). Default 0 → byte-identical
  #    when GT_RL_PROFILE is unset; the in-container reads treat 0/'' as OFF. ──
  --ae "GT_GATEWAY=${GT_GATEWAY:-0}"
  --ae "GT_EDIT_CHECK=${GT_EDIT_CHECK:-0}"
  --ae "GT_VERIFY_EXECUTE=${GT_VERIFY_EXECUTE:-0}"

  # ── Deep 8-dp telemetry sinks (CLAUDE.md mandate) -> host-mounted /gt_out ─────
  # Without these the in-container producers default to /tmp/* and DIE with the
  # container (gap G11). Point them into the writable mount so they survive.
  --ae "GT_ORACLE_EVENTS=${GT_ORACLE_EVENTS:-${GT_C_OUT}/gt_oracle_events.jsonl}"
  --ae "GT_RUNTIME_LEDGER=${GT_RUNTIME_LEDGER:-${GT_C_OUT}/gt_runtime_ledger.jsonl}"
  --ae "GT_HOOK_FIRE_COUNTS=${GT_HOOK_FIRE_COUNTS:-${GT_C_OUT}/gt_hook_fire_counts.json}"
)
