#!/usr/bin/env bash
# scripts/vm/gt_launch_guard.sh — SOURCED guard against the raw-parallel-launch contamination
# class (the 2026-07-07 burst). Source this file, then call the two functions.
#
#   gt_require_orchestrator   Refuse to run UNLESS launched by a known orchestrator
#                             (GT_ORCHESTRATED=1) or with an explicit single-run opt-in
#                             (GT_ALLOW_UNORCHESTRATED=1). Stops a bare `nohup single_task &`
#                             fanout from ever reaching `pier run`.
#   gt_launch_gate <tag>      flock-serialized LAUNCH SPACING: no two container-creations start
#                             within GT_LAUNCH_MIN_SPACING_S (default 20s) of each other, so
#                             concurrent workers cannot burst one docker daemon.
#
# WHY (evidence): a raw 4-way `nohup run_gt_mimo2.sh &` fired 4 `docker compose up` within seconds
# -> daemon "No such container" race (killed one run) + egress-network churn that hung a RUNNING
# task's in-flight LLM call (offunzu, frozen at step 58, waiting on the next model response). The
# official GHA run is immune (one task per runner = its own daemon); the box manual path was not.
# This guard makes any single-daemon launcher safe WITHOUT relying on the operator to remember to
# throttle. It is mechanical, not advisory.
#
# NOTE: it does NOT cap total parallelism — it only SPACES the container-creation phase, so N
# workers still run N tasks concurrently; they just cannot start their compose-ups simultaneously.

# ── refuse a raw unorchestrated launch ───────────────────────────────────────
# Safe orchestrators (gt_agent_run.sh, run_mimo2_smart.sh) export GT_ORCHESTRATED=1.
gt_require_orchestrator() {
  if [ "${GT_ORCHESTRATED:-0}" = "1" ]; then
    return 0
  fi
  if [ "${GT_ALLOW_UNORCHESTRATED:-0}" = "1" ]; then
    echo "[gt-launch-guard] WARNING: unorchestrated single run (GT_ALLOW_UNORCHESTRATED=1)." \
         "Safe ONLY as ONE task at a time — never fan out multiple of these." >&2
    return 0
  fi
  cat >&2 <<'EOF'
FATAL [gt-launch-guard]: refusing to launch a single-task runner directly.

  A raw parallel fanout of single-task scripts on ONE docker daemon caused the
  2026-07-07 contamination: 4 concurrent `docker compose up` -> a daemon race killed one
  run and the egress-network churn hung another run's in-flight LLM call (frozen forever).

  Do NOT:  nohup single_task.sh & ; nohup single_task.sh & ; ...

  Correct, safe launch paths (throttled or isolated):
    - GHA:  .github/workflows/deepswe_full.yml   (one task per isolated runner = own daemon)
    - Box:  scripts/vm/gt_agent_run.sh           (per-task isolation + this launch gate)
            or run_mimo2_smart.sh                (mem/load gate + BUILD_CONC semaphore)

  To run EXACTLY ONE task manually (no fanout), re-run with:  GT_ALLOW_UNORCHESTRATED=1
EOF
  exit 1
}

# ── space container-creation starts so concurrent launches can't burst the daemon ──
gt_launch_gate() {
  local tag="${1:-launch}"
  local spacing="${GT_LAUNCH_MIN_SPACING_S:-20}"
  local lock="${GT_LAUNCH_LOCK:-${TMPDIR:-/tmp}/gt_launch.lock}"
  local stampf="${lock}.stamp"
  local have_flock=0
  if command -v flock >/dev/null 2>&1; then
    if exec 8>>"$lock" 2>/dev/null && flock 8 2>/dev/null; then
      have_flock=1
    fi
  fi
  local now last delta wait
  now=$(date +%s 2>/dev/null || echo 0)
  last=$(cat "$stampf" 2>/dev/null || echo 0)
  case "$last" in ''|*[!0-9]*) last=0 ;; esac
  delta=$(( now - last ))
  if [ "$last" -gt 0 ] && [ "$delta" -lt "$spacing" ]; then
    wait=$(( spacing - delta ))
    echo "[gt-launch-guard] $tag: spacing launch by ${wait}s (last launch ${delta}s ago, min ${spacing}s)" >&2
    sleep "$wait"
  fi
  date +%s > "$stampf" 2>/dev/null || true
  # release the lock (only the container-creation START is serialized, not the whole run)
  if [ "$have_flock" = "1" ]; then
    exec 8>&- 2>/dev/null || true
  fi
}
