# SWE-bench-Live Lite: 20-30 Task Run Playbook

## Quick Reference

```
VM:       gt-v1 (e2-standard-8, us-central1-a)
OH:       /home/ubuntu/OpenHands-0.54.0/
GT:       /home/ubuntu/Groundtruth/
LiteLLM:  localhost:4000, key=sk-gt-local
Model:    Qwen3-Coder-480B via Vertex AI MaaS (global endpoint)
Project:  GCP_OLD_PROJECT_PLACEHOLDER (baliharneet0@gmail.com)
```

---

## 1. Start VM

```bash
gcloud compute instances start gt-v1 --zone=us-central1-a \
  --project=GCP_OLD_PROJECT_PLACEHOLDER
```

gt-t0 (n2d-standard-16) frequently hits ZONE_RESOURCE_POOL_EXHAUSTED. gt-v1 (e2-standard-8) is more reliable. Use gt-v1 with 3-4 workers.

## 2. Start LiteLLM Proxy

LiteLLM handles Vertex AI token refresh automatically via the VM's compute SA (no manual token management needed).

```bash
gcloud compute ssh ubuntu@gt-v1 --zone=us-central1-a \
  --project=GCP_OLD_PROJECT_PLACEHOLDER --command="
bash /home/ubuntu/start_litellm.sh
sleep 10
curl -s -H 'Authorization: Bearer sk-gt-local' localhost:4000/health | python3 -c 'import json,sys; d=json.load(sys.stdin); print(f\"Healthy: {d.get(\"healthy_count\",0)}\")'
"
```

If LiteLLM isn't installed or broken:
```bash
source /home/ubuntu/sweagent_venv/bin/activate
pip install 'litellm[proxy]' websockets 'google-cloud-aiplatform>=1.38'
```

Config at `/home/ubuntu/litellm_config.yaml`:
```yaml
model_list:
  - model_name: qwen/qwen3-coder-480b-a35b-instruct-maas
    litellm_params:
      model: vertex_ai/qwen/qwen3-coder-480b-a35b-instruct-maas
      vertex_project: GCP_OLD_PROJECT_PLACEHOLDER
      vertex_location: global
  - model_name: openai/qwen/qwen3-coder-480b-a35b-instruct-maas
    litellm_params:
      model: vertex_ai/qwen/qwen3-coder-480b-a35b-instruct-maas
      vertex_project: GCP_OLD_PROJECT_PLACEHOLDER
      vertex_location: global

general_settings:
  master_key: sk-gt-local
```

## 3. Write config.toml

**CRITICAL GOTCHA:** OH has TWO config.toml files that serve different purposes:

| File | Purpose | Read by |
|------|---------|---------|
| `/home/ubuntu/OpenHands-0.54.0/config.toml` | LLM config (`[llm.eval]`) | `get_llm_config_arg()` (reads from CWD) |
| `/home/ubuntu/OpenHands-0.54.0/evaluation/benchmarks/swe_bench/config.toml` | Task filtering (`selected_ids`) | `filter_dataset()` in run_infer.py |

**The GT wrapper's `--instance-ids` flag OVERWRITES the swe_bench config.toml** with just `selected_ids`. It does NOT touch the root config.toml.

So you MUST write the root config.toml with LLM settings:

```bash
cat > /home/ubuntu/OpenHands-0.54.0/config.toml << 'EOF'
[core]
max_iterations = 100
default_agent = "CodeActAgent"

[llm.eval]
model = "openai/qwen/qwen3-coder-480b-a35b-instruct-maas"
api_key = "sk-gt-local"
base_url = "http://localhost:4000/v1"
temperature = 0.7
top_p = 1.0
max_output_tokens = 8192
caching_prompt = false

[sandbox]
runtime_container_image = "ghcr.io/all-hands-ai/runtime:0.54-nikolaik"
EOF
```

## 4. Checkout GT Code

```bash
cd /home/ubuntu/Groundtruth
git fetch origin
git checkout <commit>          # e.g., fcea7f9 for pre-gen
git checkout -b <branch_name>  # e.g., gen_lab
```

## 5. Launch the Run

```bash
cd /home/ubuntu/OpenHands-0.54.0
source .venv/bin/activate
export PYTHONPATH='/home/ubuntu/Groundtruth/src:/home/ubuntu/Groundtruth/scripts/swebench'
export GT_INDEX_BINARY='/home/ubuntu/Groundtruth/gt-index/gt-index-linux'

mkdir -p /home/ubuntu/results/<run_name>

nohup python /home/ubuntu/Groundtruth/scripts/swebench/oh_gt_full_wrapper.py \
  --instance-ids '<comma-separated-task-ids>' \
  -l eval \
  -i 100 \
  --eval-num-workers 4 \
  --eval-output-dir /home/ubuntu/results/<run_name> \
  --dataset 'SWE-bench-Live/SWE-bench-Live' \
  --split lite \
  > /home/ubuntu/results/<run_name>.log 2>&1 &

echo $! > /home/ubuntu/results/<run_name>.pid
```

## 6. The 30 Task IDs

From `benchmarks/smoke_30_split.json` — first 30 from Live Lite 300:

```
delgan__loguru-1297
delgan__loguru-1306
flexget__flexget-4306
flexget__flexget-4244
kozea__weasyprint-2300
kozea__weasyprint-2387
kozea__weasyprint-2405
kozea__weasyprint-2398
kozea__weasyprint-2303
pypsa__pypsa-1172
pypsa__pypsa-1112
pypsa__pypsa-1091
pypsa__pypsa-1195
aiogram__aiogram-1594
amoffat__sh-744
arviz-devs__arviz-2413
aws-cloudformation__cfn-lint-3875
aws-cloudformation__cfn-lint-3890
aws-cloudformation__cfn-lint-3855
aws-cloudformation__cfn-lint-4023
aws-cloudformation__cfn-lint-3862
aws-cloudformation__cfn-lint-4009
aws-cloudformation__cfn-lint-3947
aws-cloudformation__cfn-lint-3798
aws-cloudformation__cfn-lint-4002
aws-cloudformation__cfn-lint-3770
aws-cloudformation__cfn-lint-3805
aws-cloudformation__cfn-lint-3779
aws-cloudformation__cfn-lint-4016
aws-cloudformation__cfn-lint-3767
```

As comma-separated (copy-paste ready):
```
delgan__loguru-1297,delgan__loguru-1306,flexget__flexget-4306,flexget__flexget-4244,kozea__weasyprint-2300,kozea__weasyprint-2387,kozea__weasyprint-2405,kozea__weasyprint-2398,kozea__weasyprint-2303,pypsa__pypsa-1172,pypsa__pypsa-1112,pypsa__pypsa-1091,pypsa__pypsa-1195,aiogram__aiogram-1594,amoffat__sh-744,arviz-devs__arviz-2413,aws-cloudformation__cfn-lint-3875,aws-cloudformation__cfn-lint-3890,aws-cloudformation__cfn-lint-3855,aws-cloudformation__cfn-lint-4023,aws-cloudformation__cfn-lint-3862,aws-cloudformation__cfn-lint-4009,aws-cloudformation__cfn-lint-3947,aws-cloudformation__cfn-lint-3798,aws-cloudformation__cfn-lint-4002,aws-cloudformation__cfn-lint-3770,aws-cloudformation__cfn-lint-3805,aws-cloudformation__cfn-lint-3779,aws-cloudformation__cfn-lint-4016,aws-cloudformation__cfn-lint-3767
```

## 7. Monitor Progress

```bash
# Completed count
wc -l /home/ubuntu/results/<run>/SWE-bench-Live__SWE-bench-Live-lite/CodeActAgent/qwen3-coder-480b-a35b-instruct-maas_maxiter_100/output.jsonl

# Completed task IDs
cat .../output.jsonl | python3 -c 'import json,sys;[print(json.loads(l)["instance_id"]) for l in sys.stdin]'

# Active containers
docker ps --format '{{.Names}}' | wc -l

# LLM call health
tail -5 /home/ubuntu/litellm.log

# Per-task log
tail -20 .../infer_logs/instance_<task_id>.log
```

## 8. Resume After Crash

OH auto-resumes: completed tasks are in output.jsonl, restarting skips them.

```bash
# Check what's done
grep 'Finished instances' <run>.log

# Just relaunch same command — it picks up where it left off
nohup python .../oh_gt_full_wrapper.py --instance-ids '...' -l eval ...
```

## 9. Evaluate Results

After all tasks complete, convert output to predictions and run official eval:

```bash
# Convert output.jsonl to predictions format
python3 -c "
import json
with open('.../output.jsonl') as f:
    preds = []
    for line in f:
        d = json.loads(line)
        preds.append({
            'instance_id': d['instance_id'],
            'model_patch': d.get('test_result', {}).get('git_patch', ''),
            'model_name_or_path': 'gt-pregen'
        })
with open('.../predictions.jsonl', 'w') as f:
    for p in preds:
        f.write(json.dumps(p) + '\n')
print(f'{len(preds)} predictions written')
"

# Run official SWE-bench evaluation
cd /home/ubuntu/OpenHands-0.54.0
source .venv/bin/activate
python -m swebench.harness.run_evaluation \
  --predictions_path .../predictions.jsonl \
  --swe_bench_tasks 'SWE-bench-Live/SWE-bench-Live' \
  --log_dir .../eval_logs \
  --testbed /tmp/swebench_testbed \
  --max_workers 4
```

## 10. Cost

- LLM: ~$0.12/task (Qwen3-Coder-480B at $0.45/M in, $1.80/M out)
- VM: gt-v1 ~$0.27/hr (e2-standard-8)
- 20 tasks: ~$2.40 LLM + ~$0.80 VM = ~$3.20
- 30 tasks: ~$3.60 LLM + ~$1.20 VM = ~$4.80

## 11. Gotchas Learned the Hard Way

1. **Two config.toml files** — root for LLM config, swe_bench dir for task IDs. `--instance-ids` destroys the swe_bench one. Always put LLM config in root.

2. **Wrapper process can die silently** — containers keep running but no new tasks start. Check `ps aux | grep oh_gt | grep -v grep` periodically. If 0 procs but containers active = dead wrapper.

3. **`CLOUDSDK_CORE_PROJECT` env var** — may override gcloud project. Always pass `--project=GCP_OLD_PROJECT_PLACEHOLDER` explicitly.

4. **gt-t0 (n2d-standard-16)** frequently unavailable due to zone capacity. gt-v1 (e2-standard-8) is more reliable but slower.

5. **Qwen3-480B is ~2 min per LLM call** — a 100-iteration task can take 30+ min. Don't assume it's stuck just because it's slow.

6. **Docker image build overhead** — first run of each task builds a runtime image (~2-3 min). Subsequent runs reuse cached images.

7. **max_output_tokens=8192** not 65536 — the OH config caps it. The SWE-agent config uses 65536 but that's a different harness.

## 12. GT Wrapper Flow

```
oh_gt_full_wrapper.py
  |
  +-- main()
  |     +-- parse --instance-ids
  |     +-- write selected_ids to swe_bench/config.toml
  |     +-- patch_run_infer(ri) — monkey-patches:
  |     |     +-- patched_initialize_runtime — installs gt-index, builds graph.db, generates brief, wraps run_action
  |     |     +-- patched_get_instruction — injects <gt-task-brief> into first message
  |     +-- ri.main() OR run_openhands_fork_main()
  |
  +-- Per-task flow (inside patched_initialize_runtime):
        +-- Upload gt-index binary to container
        +-- Build graph.db: gt-index -root /workspace -output /tmp/gt_index.db
        +-- Generate L1 brief (v7_brief or v1r_brief)
        +-- Run L4 prefetch (issue-seeded gt_query)
        +-- Wrap runtime.run_action:
              +-- post_edit → L6 reindex → L3 evidence → append to observation
              +-- post_view → L3b navigation → append to observation
              +-- finish → L5 advisory → download logs
```
