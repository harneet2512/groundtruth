#!/bin/bash
set -euo pipefail

# Readiness subset: historically edit-heavy tasks, with at least one task that
# has already shown a concrete armed window in prior diagnostics.
TASKS="astropy__astropy-12907 astropy__astropy-13033 astropy__astropy-13236"

if systemctl --user is-active smoke5_nolsp.service >/dev/null 2>&1; then
  systemctl --user stop smoke5_nolsp.service || true
fi
if systemctl --user is-active smoke5_lsp.service >/dev/null 2>&1; then
  systemctl --user stop smoke5_lsp.service || true
fi
pkill -f 'sweagent.run-batch' 2>/dev/null || true
sleep 2

nohup env TASKS="$TASKS" bash /home/Lenovo/run_5smoke_nolsp.sh >/home/Lenovo/launch_nolsp_3.log 2>&1 &
nohup env TASKS="$TASKS" bash /home/Lenovo/run_5smoke_lsp.sh >/home/Lenovo/launch_lsp_3.log 2>&1 &

echo "launched 3-task smoke for both arms"
