# Live streaming GT run in Gitpod

Watch a real OpenHands + GroundTruth eval run stream live in a terminal — the
thing GHA can't show you. This runs **Path 2** (the OH "Live Lite" integration:
`oh_gt_full_wrapper.py` + OpenHands 0.54 Docker runtime + a SWE-bench-Live task
image), via the proven self-bootstrapping runner `railway/codespace_run.sh`.

## One-time setup
1. **Open the repo in Gitpod** (uses `.gitpod.yml`, base image `gitpod/workspace-full` = Docker + Go + Python + gcc):
   ```
   https://gitpod.io/#https://github.com/harneet2512/groundtruth/tree/gt-consensus-curation
   ```
2. **Set your DeepSeek key once** (persists across all your workspaces):
   ```bash
   gp env DEEPSEEK_API_KEY=sk-xxxxxxxx
   ```
   Then **reopen** the workspace so it's in the env. (Or, for the current shell only: `export DEEPSEEK_API_KEY=sk-xxxx`.)
   > The key is never committed — it lives in Gitpod's per-user variable store.

## Run it (streams live)
In a Gitpod terminal:
```bash
bash railway/gitpod_run.sh                        # default task, GT ON
GT_TASK=<instance_id> bash railway/gitpod_run.sh   # a specific SWE-bench-Live task
GT_BASELINE=1 bash railway/gitpod_run.sh           # pure OpenHands (GT OFF) for A/B
```
You'll see, live: venv + OpenHands install → `gt-index` build → image pulls →
`/testbed` pre-index → the agent loop (every GT brief/hook + every agent action)
→ patch + eval verdict. It also tee's to a log under `/tmp/gt_debug/`.

## What the run does (so the stream makes sense)
`railway/codespace_run.sh` self-bootstraps everything (idempotent):
- clones **OpenHands 0.54.0**, installs it + GroundTruth + SWE-bench-Live into `/tmp/ohvenv`
- builds **`gt-index`** (Go + CGO) → `/tmp/gt-index`
- pulls the **OH runtime** image + the **task** image
- pre-indexes `/testbed` → `graph.db`
- runs `scripts/swebench/oh_gt_full_wrapper.py` (L1 brief + L3b post-view + L3 post-edit + grep + L5/L6)
- default task `beetbox__beets-5495`, arm `v2_live` (GT live)

## Disk note (read if a `docker pull` fails on space)
The OH runtime image + task image are multi-GB. Gitpod's default disk can be
tight (same constraint that bit Codespaces). If you hit "no space left":
```bash
sudo service docker stop
sudo mv /var/lib/docker /workspace/docker
sudo dockerd --data-root /workspace/docker &     # or set "data-root" in /etc/docker/daemon.json
```
`/workspace` is the persistent, roomier mount. The first run is the slow one
(bootstrap + pulls); reopening a stopped workspace reuses `/workspace` but `/tmp`
(venv, OH clone, images under default data-root) is ephemeral — so re-bootstrap
happens unless you moved data-root to `/workspace`.

## Notes
- This is the **same production wrapper** the GHA `live_lite` pipeline runs — just
  streamed locally instead of post-hoc logs.
- Baseline (`GT_BASELINE=1`) is best-effort here; the rigorous paired A/B is the
  GHA `live_lite` `GT_BASELINE` arm.
- After a run, audit the agent's actual observations in the run's `output.jsonl`
  (not telemetry) to judge whether GT helped.
