# v7.3 verification gate -- runbook

Gate spec: `D:\Groundtruth\localization_docs\v7\HANDOFF_GT_TO_FULL_POTENTIAL.md` section 4.

## Context

Phase 0 of the kernel + adapter plan does NOT start until v7.3 clears a paired n=10 vs v7.2 smoke on Live-Lite. The gate measures: per-arm verify_report PASS/FAIL, paired pass@1 (Wilcoxon signed-rank), localization metrics (first_gold, precision, coverage), brief_chars distribution, sanitizer-affected line count, and confidence bucket distribution. v7.3 only lands as default if both arms PASS and there is no statistically significant regression.

## Sequence

Run these in order. The first script runs from your workstation (Windows + Git Bash, WSL, or any POSIX shell with `gcloud` installed). The remaining three run on VM1 (`robbymd-umls-t0`, project `project-26227097-98fa-4016-a54`, zone `us-central1-a`).

1. **Workstation**: build the paired task-id list (one `instance_id` per line). Reference: the same 10 task IDs used in the v7.2 smoke. Reference dir on workstation: `C:\Users\Lenovo\Downloads\gt_v7_2_smoke_analysis\`. If you have a `gt_report.csv` from the v7.2 smoke run, extract IDs with:

   ```
   ! awk -F, 'NR>1 {print $1}' C:/Users/Lenovo/Downloads/gt_v7_2_smoke_analysis/gt_report.csv | sort -u > C:/Users/Lenovo/Downloads/gt_v7_2_smoke_analysis/instance_ids.txt
   ```

2. **Workstation**: deploy the bundle + gate scripts + task IDs to VM1.

   ```
   ! bash D:/Groundtruth/scripts/swebench/v7_3_gate/deploy.sh C:/Users/Lenovo/Downloads/gt_v7_2_smoke_analysis/instance_ids.txt
   ```

3. **VM1**: launch paired arms.

   ```
   ! gcloud compute ssh robbymd-umls-t0 --project=project-26227097-98fa-4016-a54 --zone=us-central1-a -- 'bash /opt/gt_bundle/v7_3_gate/run_gate.sh --instance-ids-file /opt/gt_bundle/v7_3_gate/instance_ids.txt'
   ```

4. **VM1**: monitor progress (foreground, prints every 60s, exits when both arms finish).

   ```
   ! gcloud compute ssh robbymd-umls-t0 --project=project-26227097-98fa-4016-a54 --zone=us-central1-a -- 'bash /opt/gt_bundle/v7_3_gate/monitor.sh'
   ```

5. **VM1**: verify both arms (calls `verify_report.py append`), compute paired Wilcoxon, render `~/runs/v7_3_gate_<ts>_VERDICT.md`.

   ```
   ! gcloud compute ssh robbymd-umls-t0 --project=project-26227097-98fa-4016-a54 --zone=us-central1-a -- 'bash /opt/gt_bundle/v7_3_gate/verify_and_report.sh'
   ```

6. **VM1 or workstation**: consult `decision_tree.md` -- the bash one-liner inside it reads the latest VERDICT.md, extracts the `OUTCOME` marker, and prints the recommended action verbatim.

   ```
   ! gcloud compute ssh robbymd-umls-t0 --project=project-26227097-98fa-4016-a54 --zone=us-central1-a -- 'bash -c "$(awk "/^\`\`\`bash$/,/^\`\`\`$/" /opt/gt_bundle/v7_3_gate/decision_tree.md | sed "/^\`\`\`/d")"'
   ```

   (Alternative: `cat /opt/gt_bundle/v7_3_gate/decision_tree.md` and copy-paste the one-liner block.)

## Files in this directory

| File | Where it runs | Purpose |
|---|---|---|
| `deploy.sh` | Workstation | gcloud auth check, local SHA256 verify, scp bundle + scripts + task IDs to VM1, remote SHA256 verify. |
| `run_gate.sh` | VM1 | Pre-flight checks (bundle present, workers <= nproc, containerd disk >50GB, archive prior `output.jsonl`), launch v7.3 + v7.2 arms paired with `nohup`, write `/tmp/v7_3_gate.pids`. |
| `monitor.sh` | VM1 | Print disk + per-arm progress + ETA every 60s. Exits when both arms PID-finished AND `output.jsonl` line count == expected. |
| `verify_and_report.sh` | VM1 | Run `verify_report.py append` per arm. Compute paired Wilcoxon on per-task pass@1. Compute localization metrics, brief_chars distribution, sanitizer counts, confidence buckets. Render `~/runs/v7_3_gate_<ts>_VERDICT.md`. |
| `decision_tree.md` | VM1 or workstation | Reference table mapping VERDICT.md outcome to recommended action. Includes a copy-pasteable bash one-liner that prints the matching branch. |
| `README.md` | Reference only | This file. |

## Known failure modes + recovery

- **gcloud auth missing on workstation.** `deploy.sh` Step 0 bails with `FAIL: no active gcloud account`. Run `gcloud auth login` and retry.
- **Local bundle SHA256 mismatch.** `deploy.sh` Step 1 bails. Either the bundle was rebuilt (compute new SHA256 with `sha256sum D:/Groundtruth/.tmp_v7_bundle/gt_pretask_brief_v7_full.py` and update `BUNDLE_SHA256_EXPECTED` in deploy.sh) or the file was corrupted. Refusing to deploy a drifted artifact is intentional.
- **v7.2 bundle missing on VM1.** `run_gate.sh` pre-flight 1 bails. Default expected path is `/opt/gt_bundle/gt_pretask_brief_v7_2_full.py`; if your prior v7.2 deployed elsewhere, pass `--v72-bundle <path>`.
- **Workers > vCPUs.** `run_gate.sh` pre-flight 3 refuses to start (CLAUDE.md rule). Default is `min(nproc, 4)`. Override with `--workers N`.
- **Containerd disk under 50GB.** `run_gate.sh` pre-flight 4 bails. Recovery: `docker system prune -af --volumes`, or resize the data disk per `reference_swebench_disk.md` memory.
- **Stale `output.jsonl`.** `run_gate.sh` pre-flight 5 archives any pre-existing `output.jsonl` to `output.jsonl.<ts>.bak` per `feedback_oh_resume_skips_errors`. No-op if nothing pre-existed.
- **PIDs end with arms short of expected.** `monitor.sh` exits non-zero with a clear message. Inspect `run.log` in each arm dir before re-launching.
- **`verify_report.py` not at `/opt/groundtruth/scripts/swebench/`.** `verify_and_report.sh` falls back to `~/groundtruth/scripts/swebench/verify_report.py`. If neither exists, locate the file and re-run with `VERIFY=<path>` in env.
- **scipy not installed.** `verify_and_report.sh` falls back to "wilcoxon not computed" with a note. Install with `pip install scipy` on the VM.
- **Arm verify_report FAIL.** Per CLAUDE.md, move the failing run dir to `~/runs/fast_diag/<run_id>/` and do NOT compare. Diagnose root cause before any follow-up repeat.

## Assumptions surfaced by this runbook (override if wrong)

The following are user-prompted, not hardcoded:

- **Path to the v7.2 instance-ID list on workstation.** The runbook asks you to point at `C:\Users\Lenovo\Downloads\gt_v7_2_smoke_analysis\` and either reuse an existing `instance_ids.txt` or extract it from `gt_report.csv`. If your v7.2 smoke artifacts live elsewhere, pass that path to `deploy.sh` directly.
- **v7.2 bundle path on VM1.** Default is `/opt/gt_bundle/gt_pretask_brief_v7_2_full.py`. Override via `run_gate.sh --v72-bundle <path>`.
- **Launcher script path on VM1.** Default is `/opt/groundtruth/scripts/swebench/oh_run_v7_smoke.sh` and the launcher must accept `--bundle`, `--task-ids`, `--model`, `--workers`, `--output-dir`. If your existing launcher uses different flags, override via `run_gate.sh --launcher <path>` and adapt the call inside `run_gate.sh` (single block, clearly marked).
- **Model name.** Default `MiMo-V2-Flash` per gate spec. Override via `run_gate.sh --model <name>`.
