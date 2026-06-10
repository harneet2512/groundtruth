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
