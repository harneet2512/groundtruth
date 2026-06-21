#!/usr/bin/env bash
# Dump FULL docker state when pier's env-start hangs (no container appeared in the watch window) —
# names the root cause across both failure modes: 'container created but exited' (inspect exit/OOM +
# the container's own logs) and 'container never created' (dockerd journal + dmesg + stuck procs).
# Args: $1 = attempt number, $2 = watch ticks (each = 10s).
_A="${1:-?}"; TICKS="${2:-90}"
echo "::warning::[ENV-START] NO container in $((TICKS*10))s on attempt $_A — hung deploy; capturing docker state"
echo "[ENV-START-DIAG] docker ps -a:"; docker ps -a 2>&1 | head -10
for _cid in $(docker ps -aq 2>/dev/null | head -3); do
  echo "[ENV-START-DIAG] container $_cid inspect:"; docker inspect --format '  status={{.State.Status}} exitcode={{.State.ExitCode}} oomkilled={{.State.OOMKilled}} error={{.State.Error}}' "$_cid" 2>&1
  echo "[ENV-START-DIAG] container $_cid logs:"; docker logs --tail 25 "$_cid" 2>&1 | tail -25
done
echo "[ENV-START-DIAG] dockerd journal:"; { sudo journalctl -u docker --no-pager -n 40 2>/dev/null || journalctl -u docker --no-pager -n 40 2>/dev/null; } 2>&1 | tail -40
echo "[ENV-START-DIAG] kernel/OOM (dmesg):"; { sudo dmesg 2>/dev/null || dmesg 2>/dev/null; } | tail -15
echo "[ENV-START-DIAG] stuck procs:"; ps -eo pid,rss,comm,args --sort=-rss 2>/dev/null | grep -iE 'pier|swerex|docker|python' | grep -v grep | head -8
echo "[ENV-START-DIAG] pier output tail:"; tail -25 trial_output.log 2>/dev/null
