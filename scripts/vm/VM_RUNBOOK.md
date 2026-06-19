# VM_RUNBOOK — generic proof-sweep on a single Linux VM

Runs `scripts/vm/gt_proof_sweep.sh` (the manifest-driven port of
`.github/workflows/deepswe_proof_sweep.yml`) on any Linux host with docker.
NO LLM, NO agent — per task it runs only the pinned-substrate `gt-run-proof`
($0 LLM cost; pure compute + image pulls).

---

## CLOUD / GCP RULES (non-negotiable)

> - **NEVER run `gcloud auth login`** (or any auth/credential command) on the VM
>   or anywhere else. No exceptions.
> - **No project IDs, account names, or credentials in any file** — this runbook,
>   the scripts, the manifests, logs committed to git. Placeholders only.
> - Provisioning happens **from the operator's already-authenticated local
>   `gcloud` only** (their own workstation). The VM itself needs zero cloud
>   credentials: every image pull in this sweep is anonymous/public.
> - The runner is host-generic: any Linux box with docker works (cloud VM,
>   bare metal, local). Nothing in it is provider-specific.

---

## 1. Machine shape

| Sweep | Tasks | Suggested shape | Disk |
|---|---:|---|---|
| DeepSWE | 113 | 32 vCPU / 128 GB (e.g. `n2-standard-32` class or any equivalent) | 100 GB SSD |
| SWE-bench-Pro | 731 | 32 vCPU / 128 GB | **200–300 GB SSD** |

- The runner deletes each task image right after source extraction
  (`docker rmi`, disk-bounded), so peak image disk ≈ `PARALLEL` × largest task
  image + the substrate. Pro images are large — take the 200–300 GB SSD.
- CPU drives `PARALLEL` (default `nproc/2`, capped 24). 32 vCPU → 16-par
  default; pass `PARALLEL=24` to max out.
- No GPU. SWE-bench style work is CPU + Docker only.

Provision with your **local, already-authed** tooling, e.g. (placeholders only):

```
# FROM YOUR WORKSTATION — never authenticate on the VM itself
gcloud compute instances create <VM_NAME> \
  --zone <ZONE> --machine-type n2-standard-32 \
  --boot-disk-size 300GB --boot-disk-type pd-ssd \
  --image-family ubuntu-2404-lts-amd64 --image-project ubuntu-os-cloud
gcloud compute ssh <VM_NAME> --zone <ZONE>
```

(Any other provider / bare-metal box is equally fine — the runner does not care.)

## 2. One-time VM setup

```bash
# docker (official convenience script) + group access
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"
# log out / back in (or `newgrp docker`) so docker works without sudo
docker run --rm hello-world

# python3 is preinstalled on Ubuntu; verify:
python3 --version
```

## 3. Ship the scripts + manifest

Option A — git clone (branch `<BRANCH>`, e.g. `gt-trial`):

```bash
git clone --depth 1 --branch <BRANCH> <REPO_URL> ~/groundtruth
cd ~/groundtruth
```

Option B — scp only what's needed (4 files + manifest):

```bash
# FROM YOUR WORKSTATION
scp scripts/vm/gt_proof_sweep.sh scripts/vm/aggregate_sweep.py \
    scripts/vm/build_pro_manifest.py scripts/metrics/embedder_certificate.py \
    <USER>@<VM_IP>:~/sweep/
scp artifact_deepswe/repo_manifest.json <USER>@<VM_IP>:~/sweep/
# keep aggregate_sweep.py next to gt_proof_sweep.sh;
# embedder_certificate.py goes either next to the runner or set
# GT_METRICS_DIR=<dir containing it> (the row builder imports it for the
# canonical embedder verdict; without it rows degrade to CLASSIFY_ERROR).
chmod +x ~/sweep/gt_proof_sweep.sh   # on the VM
```

## 4. Run — (a) DeepSWE-113

```bash
MANIFEST=artifact_deepswe/repo_manifest.json \
GT_SUBSTRATE_DIGEST='ghcr.io/<OWNER>/gt-substrate@sha256:<DIGEST>' \
PARALLEL=24 \
OUT_DIR="$HOME/sweep_deepswe" \
  ./scripts/vm/gt_proof_sweep.sh 2>&1 | tee "$HOME/sweep_deepswe.log"
```

- The digest is REQUIRED and must be the immutable `@sha256:` form — the runner
  fail-closes with `GT_SUBSTRATE_DIGEST_MISSING` otherwise (mutable tags are not
  a valid proof input). Take the exact digest the GHA sweep used
  (repo variable `GT_SUBSTRATE_DIGEST`).
- Expected wall-time: **~20–30 min** at 24-par (pull + extract + proof per task;
  substrate pulled once, cached).

## 5. Run — (b) SWE-bench-Pro-731

```bash
# build the manifest (public HF dataset, anonymous; ~30 s)
python3 scripts/vm/build_pro_manifest.py --out pro_manifest.json --spot-check 2

MANIFEST=pro_manifest.json \
GT_SUBSTRATE_DIGEST='ghcr.io/<OWNER>/gt-substrate@sha256:<DIGEST>' \
PARALLEL=24 \
OUT_DIR="$HOME/sweep_pro" \
  ./scripts/vm/gt_proof_sweep.sh 2>&1 | tee "$HOME/sweep_pro.log"
```

- All 731 images are public on the GHCR mirror
  `ghcr.io/hbali-stack/sweap-images:<dockerhub_tag>` — anonymous pulls, no auth.
- Expected wall-time: **~1.5–2.5 h** at 24-par (Pro images are bigger; pull
  dominates).

## 6. Watch / resume / verify

```bash
tail -f ~/sweep_pro.log                 # per-task one-liners: [n/total] id class=...
ls "$OUT_DIR"/*/row.json | wc -l        # rows done so far
```

- **Resumable:** re-running the same command skips every task whose
  `row.json` exists. `RETRY_FAILED=1` additionally re-runs tasks whose existing
  row is a classified failure.
- **Disk guard:** before each pull the runner checks free space
  (`DISK_MIN_GB`, default 25); when low it prunes dangling images and waits
  (warns on stdout), classifying `DISK_LOW` only after `DISK_WAIT_MAX_S`.
- Per-task artifacts land in `$OUT_DIR/<instance_id>/`
  (row.json, proof_run.log, graph.db, the 4 certificates, run_manifest.json).
  Note: the proof container writes artifacts as root — use `sudo rm -rf` if you
  need to clear an OUT_DIR.
- Final report: `$OUT_DIR/SWEEP_REPORT.md` (per-language table, gates,
  classified failures, optimization verdict). The runner's exit code is the
  sweep verdict — nonzero if ANY task row is failing or missing.

```bash
# pull results back — FROM YOUR WORKSTATION
scp -r <USER>@<VM_IP>:"$OUT_DIR/SWEEP_REPORT.md" .
scp <USER>@<VM_IP>:'<OUT_DIR>/*/row.json' rows/   # rows only (small); skip graph.db
```

## 7. Knobs (env)

| Var | Default | Meaning |
|---|---|---|
| `MANIFEST` | — (required) | tasks json: `{"tasks":[{instance_id,language,docker_image}]}` |
| `GT_SUBSTRATE_DIGEST` | — (required) | immutable `@sha256` substrate ref (fail-closed) |
| `PARALLEL` | nproc/2, cap 24 | concurrent tasks |
| `OUT_DIR` | ./sweep_out | artifacts + report |
| `GHCR_OWNER` | hbali-stack | GHCR cache owner for task-image pulls |
| `MAX_TASKS` | all | truncate the manifest (smoke: `MAX_TASKS=3`) |
| `RETRY_FAILED` | 0 | 1 = re-run classified-failure rows |
| `DISK_MIN_GB` / `DISK_WAIT_MAX_S` | 25 / 1800 | disk guard floor / max wait |
| `GT_GIT_COMMIT` | git HEAD if available | provenance stamped into rows |
| `GHCR_USER` / `GHCR_TOKEN` | unset | OPTIONAL best-effort `docker login` (public images need none; never stored) |

---

## 8. PATH A — the agent run (`scripts/vm/gt_agent_run.sh`, GT-on, DeepSWE×gemini)

Sections 1–7 run `gt_proof_sweep.sh` (NO agent, NO LLM — `gt-run-proof` only). PATH A
runs the **agent**: per task it (re)uses the pinned-substrate proof, then drives
`pier run` + `GTMiniSweAgent` + the gemini model through the squid egress proxy. This
arm spends real money (Vertex), so it has a cost halt and a hardened reuse gate.

### 8.1 Launch (5-task trial)

```bash
# FROM THE VM SHELL — credentials are exported by the OPERATOR, never stored in any file.
# (a) export the model auth (operator's own values — placeholders shown):
export VERTEXAI_PROJECT='<your-gcp-project>'          # pier hard-requires this
export VERTEXAI_LOCATION='us-east1'                   # LOCKED region (NOT global — 429 risk)
export GOOGLE_APPLICATION_CREDENTIALS="$HOME/sa.json" # SA JSON on the VM (ADC is blocked in-container)

# (b) launch the trial — 5 tasks, 4 workers, $25 budget halt:
MANIFEST=artifact_deepswe/repo_manifest.json \
GT_SUBSTRATE_DIGEST='ghcr.io/<OWNER>/gt-substrate@sha256:<DIGEST>' \
MODEL='vertex_ai/gemini-3-flash-preview' \
PIER_CONFIG=artifact_deepswe/gt_integration/deepswe_gt_pier_gemini.yaml \
PARALLEL=4 \
MAX_TASKS=5 \
STOP_AT_COST=25 \
OUT_DIR="$HOME/agent_trial" \
REUSE_PROOF_DIR="$HOME/sweep_deepswe" \
  ./scripts/vm/gt_agent_run.sh 2>&1 | tee "$HOME/agent_trial.log"
```

- **`VERTEXAI_LOCATION` defaults to `us-east1`** (the locked region) when unset — the
  runner fail-safes to it, never to `global`. Override to `us-east4` / `us-central1`
  only if the model isn't served in `us-east1` (one env var, no code change).
- **`GOOGLE_APPLICATION_CREDENTIALS`** must point at a service-account JSON readable on
  the VM. The runner bind-mounts it **read-only** at `/gt_auth/adc.json` and forwards the
  env; its token endpoint (`oauth2.googleapis.com:443`) passes the squid allowlist. The
  GCE metadata server (169.254.169.254) is NOT reachable in-container, so metadata-ADC
  fails there — the SA JSON is required, not optional.
- **`STOP_AT_COST`** (default `200` full / pass `25` for the trial) is a per-run budget
  halt: before launching each task the runner sums the per-task recorded cost across all
  completed tasks (flock'd, race-safe under `-P`); once the total reaches the cap, every
  remaining task is recorded as a classified **`BUDGET_HALTED`** row (not launched, not
  silent). Set `STOP_AT_COST=0` to disable.
- **`REUSE_PROOF_DIR`** consumes a prior proof-sweep OUT_DIR (skips `gt-run-proof`). The
  reuse gate is HARDENED: a proof dir is admitted only if it holds all 8 artifacts AND
  its source `row.json` has an empty `failure_class` AND no cert verdict is `FAIL`
  (lsp/graph/embedder + per-language `lsp_certificate_*.json`) AND
  `run_manifest.json.substrate_digest == $GT_SUBSTRATE_DIGEST`. Any miss → a fresh
  in-task proof (never an error). Drop `REUSE_PROOF_DIR` to always prove fresh.
- The full 113-task run is the same command without `MAX_TASKS` and with `STOP_AT_COST=200`.

### 8.2 Watch / resume

```bash
tail -f "$HOME/agent_trial.log"               # per-task: [n/total] id class=... pier_rc=...
ls "$OUT_DIR"/*/row.json | wc -l              # rows done
cat "$OUT_DIR"/AGENT_SWEEP_REPORT.md          # final classes + resolved tally
```

Resumable (existing `row.json` skipped; `RETRY_FAILED=1` re-runs classified failures).
On a per-task **`PIER_TIMEOUT`** (rc 124) the runner force-removes the leaked task+squid
containers and the per-task network before the next task (a TERMed pier leaves them
RUNNING; `docker container prune` only removes stopped ones). A **`RATE_LIMIT`** class is
recorded when a non-zero pier run shows a 429 / RESOURCE_EXHAUSTED / quota signal — re-drive
with a different region or lower `PARALLEL`.

### 8.3 Disk caveat

OUT_DIR grows large: each of the 113 tasks keeps its own `gt/` (graph.db + 7 certs +
brief) and, when not reusing, an extracted `src/` repo copy. Budget for **113 repo copies
+ 113 graph.dbs**. The runner prunes the docker IMAGES per task, but the OUT_DIR
artifacts persist. To reclaim space after a row is written you may prune the per-task
source tree (optional, post-row):

```bash
# AFTER the run (or as it progresses) — drop extracted src/ once each row.json exists:
for d in "$OUT_DIR"/*/; do
  [ -f "$d/row.json" ] && rm -rf "$d/gt/src" 2>/dev/null || true
done
```

(The proof container writes some artifacts as root — use `sudo rm -rf` if a plain `rm`
is denied.)

### 8.4 Download EVERYTHING to local — with a mandatory secret scan FIRST

The artifacts may contain agent-visible material; the SA-key risk is mitigated by
**scanning the tarball for secrets BEFORE it lands locally** and refusing the pull on any
hit. Run this FROM YOUR WORKSTATION:

```bash
RUN=agent_trial_$(date -u +%Y%m%dT%H%M%SZ)
DEST=".claude/reports/runs/$RUN"
mkdir -p "$DEST"

# (1) tar the WHOLE OUT_DIR on the VM (exclude NOTHING of the artifacts):
ssh <USER>@<VM_IP> "tar -czf /tmp/$RUN.tgz -C \"\$HOME\" agent_trial"

# (2) SECRET-SCAN the tarball ON THE VM before pulling — fail (and delete) on any hit:
ssh <USER>@<VM_IP> bash -s <<'SCAN'
set -e
T="/tmp/'"$RUN"'.tgz"
# private keys and a GCP project-id shaped token must NEVER be in the artifacts.
if tar -xzOf "$T" 2>/dev/null | grep -aE 'private_key|-----BEGIN [A-Z ]*PRIVATE KEY-----|project[-_]?id"?\s*[:=]\s*"?[a-z0-9-]{6,30}' >/dev/null; then
  echo "SECRET_SCAN_FAIL: secret-shaped content found in '"$RUN"'.tgz — refusing to release"; rm -f "$T"; exit 3
fi
echo "SECRET_SCAN_OK: no private_key / project-id pattern in '"$RUN"'.tgz"
SCAN

# (3) only if the scan passed does the tarball still exist — pull it:
scp <USER>@<VM_IP>:/tmp/$RUN.tgz "$DEST/"
tar -xzf "$DEST/$RUN.tgz" -C "$DEST"
```

If the scan fails the VM deletes the tarball and step (3) errors (nothing to pull) — fix
the leak at the source before re-tarring. NEVER pull an unscanned tarball.
