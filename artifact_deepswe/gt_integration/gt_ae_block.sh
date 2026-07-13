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

  # ── B-16/B-17 RL-profile members that had NO --ae entry (Brief-F4). The resolver above
  #    EXPORTS these host-side, but pier DROPS host env — only GT_AE_ARGS crosses into
  #    the container. Without them, a GT_RL_PROFILE run activates only part of the profile on
  #    any caller that splices ONLY GT_AE_ARGS (the codespace witness path), shipping an
  #    INCOHERENT pair (GT_GATEWAY_NATIVE=1 with GT_GATEWAY dark). Forward them here so
  #    THIS block is the single source (don't rely on callers). Default 0 → byte-identical
  #    when GT_RL_PROFILE is unset; the in-container reads treat 0/'' as OFF. ──
  --ae "GT_GATEWAY=${GT_GATEWAY:-0}"
  --ae "GT_EDIT_CHECK=${GT_EDIT_CHECK:-0}"
  --ae "GT_VERIFY_EXECUTE=${GT_VERIFY_EXECUTE:-0}"
  --ae "GT_POST_SEARCH_NATIVE=${GT_POST_SEARCH_NATIVE:-0}"
  # ── Task #63 (2026-07-12): the SCOPE-surface FORM arm (<gt-scope> tag -> native compiler
  #    `note:` constraint via render_scope_constraint_native). Mirrors GT_POST_SEARCH_NATIVE;
  #    read in-container by gt_mini_patch._consensus_scope_block. pier DROPS host env, so forward
  #    it here or the FORM flip stays DARK in-container. Default 0 → byte-identical when unset;
  #    Profile-1/2 fan it to 1 via resolve_profile (rl_profile._PROFILE_1_MEMBERS). ──
  --ae "GT_SCOPE_NATIVE=${GT_SCOPE_NATIVE:-0}"
  # ── ITEM 1-6 (2026-07-13): the RL-native FORM sweep over the remaining tagged model-facing
  #    surfaces + in-seam metrics. SAME FORM-arm family as GT_POST_SEARCH_NATIVE/GT_SCOPE_NATIVE.
  #    <gt-contract> -> native compiler diagnostics + rg caller rows (GT_CONTRACT_NATIVE);
  #    <gt-evidence> -> bare path:line:code rows (GT_EVIDENCE_NATIVE); <gt-nudge>x7 -> body-only
  #    imperative (GT_NUDGE_NATIVE); the brief obligations block -> a plain `- [ ]` checklist
  #    (GT_BRIEF_NATIVE, read at brief-gen). GT_INSEAM_METRICS = host-side ledger instrumentation
  #    (never model-visible, zero observation bytes). Read in-container by gt_mini_patch / v1r_brief;
  #    pier DROPS host env so forward here or the flips stay DARK. Default 0 -> byte-identical when
  #    unset; Profile-1/2 fan them to 1 via resolve_profile (rl_profile._PROFILE_1_MEMBERS). ──
  --ae "GT_CONTRACT_NATIVE=${GT_CONTRACT_NATIVE:-0}"
  --ae "GT_EVIDENCE_NATIVE=${GT_EVIDENCE_NATIVE:-0}"
  --ae "GT_NUDGE_NATIVE=${GT_NUDGE_NATIVE:-0}"
  --ae "GT_BRIEF_NATIVE=${GT_BRIEF_NATIVE:-0}"
  --ae "GT_INSEAM_METRICS=${GT_INSEAM_METRICS:-0}"
  # ── T0->T2 localization RE-SLOT GO-LIVE (2026-07-12). GT_LOC_RESLOT is read by
  #    gt_mini_patch (the post_search ABSTAIN branch delivers GT's ranked localization answer
  #    on a broad/behavior grep) AND by gateway._loc_reslot_on; pier drops host env, so forward
  #    it here or the live seam stays DARK. Default 0 → byte-identical when unset; Profile-2
  #    activates it (rl_profile PROFILE_MEMBERS). ──
  --ae "GT_LOC_RESLOT=${GT_LOC_RESLOT:-0}"
  # ── SM-3 "Super Mode" engine-activation flags (2026-07-11). Read by gt_mini_patch;
  #    forwarded so they are enable-able in-container (else the engines stay DARK — the
  #    exact defect SM-3 closes). Default 0 → byte-identical when unset; Profile-2 fans
  #    them to 1 via resolve_profile. ──
  --ae "GT_COMPLETION_CERT=${GT_COMPLETION_CERT:-0}"
  --ae "GT_HYPOTHESIS=${GT_HYPOTHESIS:-0}"
  --ae "GT_VERIFICATION_PLAN=${GT_VERIFICATION_PLAN:-0}"
  --ae "GT_EDIT_OVERLAY=${GT_EDIT_OVERLAY:-0}"
  # ── SM-3 D7 delivery (2026-07-12): the CompletionCertificate rendered MODEL-FACING at the
  #    submit turn (native pre-commit-hook failure block). Companion to GT_COMPLETION_CERT
  #    (which only builds/host-records the cert). Read by gt_mini_patch; forwarded so it is
  #    enable-able in-container (else D7 delivery stays DARK). Default 0 → byte-identical when
  #    unset; Profile-2 fans it to 1 via resolve_profile. ──
  --ae "GT_CERT_DELIVERY=${GT_CERT_DELIVERY:-0}"
  # ── SM-5 "Super Mode" — the ONE global ranked competition over all delivery planes
  #    (2026-07-11). GT_GLOBAL_ARBITER is read by gt_mini_patch (the collapse); the two
  #    Gateway-producer flags gate change_surface (W-A) / patch_delta (W-C) IN-CONTAINER,
  #    so the arbiter's pool is COMPLETE. Default 0 → byte-identical when unset; Profile-2
  #    fans all three to 1 via resolve_profile. ──
  --ae "GT_GLOBAL_ARBITER=${GT_GLOBAL_ARBITER:-0}"
  --ae "GT_CHANGE_SURFACE=${GT_CHANGE_SURFACE:-0}"
  --ae "GT_PATCH_DELTA=${GT_PATCH_DELTA:-0}"
  # ── SM-9c "Super Mode" — the cross-session learned-delivery policy (2026-07-11). Read by
  #    gt_mini_patch; forwarded so it is enable-able in-container. Default 0 → byte-identical
  #    when unset; Profile-2 fans it to 1 via resolve_profile. The durable per-repo store dir
  #    GT_XSESSION_DIR is WIRED (BUG-2, 2026-07-12) — forwarded below into ${GT_C_OUT}/gt_xsession
  #    (a writable, host-mounted dir on every pier caller: /gt_out is bind-mounted rw), so the
  #    suppress/rank-up policy actually WRITES a store within the run. CROSS-RUN persistence
  #    (upload/download that dir between runs) is the remaining OWED follow-up (option b). ──
  --ae "GT_XSESSION_MEMORY=${GT_XSESSION_MEMORY:-0}"
  # ── Task #62 AE-forward completeness (2026-07-12): Profile-2 members that were read
  #    in-container (or at brief-gen) but had NO --ae entry here, so under an active
  #    GT_RL_PROFILE=2 they went DARK on any caller that splices ONLY GT_AE_ARGS (the
  #    codespace witness path). pier DROPS host env — only GT_AE_ARGS crosses in — so the
  #    profile fan-out's `export` alone is not enough; the flag must ALSO be forwarded here.
  #    The pin tests/runtime/test_ae_forward_profile2_completeness_20260712.py enforces
  #    PROFILE_MEMBERS["2"] ⊆ this forward list so the hole cannot recur. Default 0 →
  #    byte-identical when GT_RL_PROFILE is unset; Profile-2 fans each to 1. ──
  #    SM-0 registry enforcement (gateway.py:300, _registry_enforce, in-container gateway).
  --ae "GT_REGISTRY_ENFORCE=${GT_REGISTRY_ENFORCE:-0}"
  #    SM-9c rank-up / winner-promotion (gateway.py:406, _xsession_rankup_on) — the KNOWN
  #    hole: read by the SYNCED gateway module, so invisible to the gt_mini_patch-scoped R1
  #    parity invariant and to the Profile-1-only ae-boundary test. Dark in-container until now.
  --ae "GT_XSESSION_RANKUP=${GT_XSESSION_RANKUP:-0}"
  #    SM-6 step-0 baked-brief reduction (v1r_brief.py:1550, read at brief-generation). Read at
  #    BAKE time not in-container, so forwarding is inert on the run itself — carried for pool
  #    + pin completeness (the single-source invariant covers every member, bake-time or not).
  --ae "GT_BRIEF_MINIMAL=${GT_BRIEF_MINIMAL:-0}"
  #    SM-10 body-content legs (graph_localizer.localize). GT_SEM_BODY default 0 matches
  #    deepswe_full.yml's own :-0 forward — byte-identical there and here.
  --ae "GT_SEM_BODY=${GT_SEM_BODY:-0}"
  #    GT_CONTENT_LEG is the ONE deviation from the :-0 convention: deepswe_full.yml already
  #    forwards it `GT_CONTENT_LEG=${GT_CONTENT_LEG:-1}` (default ON — the leg is landed +
  #    witness-pending) and splices GT_AE_ARGS AFTER its own block, so pier/docker last-wins
  #    would let a :-0 here OVERRIDE that intended 1→0 (a regression on the primary paid path).
  #    :-1 preserves full.yml exactly and aligns the codespace witness path to the same landed
  #    default (the de-drift this block exists for). Profile-2 fans it to 1 regardless.
  --ae "GT_CONTENT_LEG=${GT_CONTENT_LEG:-1}"
  #    #48 per-turn in-container L6 reindex (gt_mini_patch._db_path, `GT_L6_FRESH == "1"`).
  #    Default 0 here is byte-identical: on deepswe_full.yml the caller appends its own
  #    `--ae "GT_L6_FRESH=1"` to GT_AE_ARGS AFTER this entry (last-wins → 1 preserved); the
  #    codespace path appends no L6 flag, so it stays OFF exactly as before.
  --ae "GT_L6_FRESH=${GT_L6_FRESH:-0}"
  # ── SS-1..SS-N SUPER-SEAM adherence sweep (2026-07-13) — the 8 SS members from the 29-task
  #    causal audit of run 29236533134. Each is default-OFF byte-identical seam/arbiter code
  #    (NOT a new tag). pier DROPS host env so forward here or the profile fan-out's `export`
  #    stays DARK in-container on any caller that splices only GT_AE_ARGS (the codespace path).
  #    Default 0 → byte-identical when GT_RL_PROFILE is unset; Profile-2 fans each to 1 via
  #    resolve_profile. Pinned in test_ae_forward_profile2_completeness_20260712 (PROFILE_MEMBERS
  #    ["2"] ⊆ this forward list). GT_SS_ARBITER_V2 is read by BOTH gt_mini_patch (arbitrate) and
  #    the synced gateway module (augment empty-payload guard). ──
  --ae "GT_SS_NOVELTY=${GT_SS_NOVELTY:-0}"
  --ae "GT_SS_DEDUP2=${GT_SS_DEDUP2:-0}"
  --ae "GT_SS_COHERENCE_V2=${GT_SS_COHERENCE_V2:-0}"
  --ae "GT_SS_RECOVERY_V2=${GT_SS_RECOVERY_V2:-0}"
  --ae "GT_SS_PROVENANCE=${GT_SS_PROVENANCE:-0}"
  --ae "GT_SS_LATE_DROP=${GT_SS_LATE_DROP:-0}"
  --ae "GT_SS_ACK_METRICS=${GT_SS_ACK_METRICS:-0}"
  --ae "GT_SS_ARBITER_V2=${GT_SS_ARBITER_V2:-0}"

  # ── Deep 8-dp telemetry sinks (CLAUDE.md mandate) -> host-mounted /gt_out ─────
  # Without these the in-container producers default to /tmp/* and DIE with the
  # container (gap G11). Point them into the writable mount so they survive.
  --ae "GT_ORACLE_EVENTS=${GT_ORACLE_EVENTS:-${GT_C_OUT}/gt_oracle_events.jsonl}"
  --ae "GT_RUNTIME_LEDGER=${GT_RUNTIME_LEDGER:-${GT_C_OUT}/gt_runtime_ledger.jsonl}"
  --ae "GT_HOOK_FIRE_COUNTS=${GT_HOOK_FIRE_COUNTS:-${GT_C_OUT}/gt_hook_fire_counts.json}"

  # ── SM-9c cross-session store DIR (BUG-2, 2026-07-12) -> host-mounted /gt_out ──
  # The durable per-repo causal-consumption ledger (xsession_memory) needs a WRITABLE
  # dir or the seam no-ops. Same path-valued-writable-env pattern as the sinks above:
  # default into ${GT_C_OUT} (=/gt_out, bind-mounted rw on deepswe_full.yml AND the
  # codespace path) so the store survives the container without a NEW mount. A host-side
  # GT_XSESSION_DIR override wins. Within-run durable; cross-RUN upload is OWED (option b).
  --ae "GT_XSESSION_DIR=${GT_XSESSION_DIR:-${GT_C_OUT}/gt_xsession}"
)
