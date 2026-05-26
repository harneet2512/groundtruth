# Mini-SWE-Agent Architecture Map

## Overview

mini-swe-agent is a lightweight, model-agnostic coding agent from SWE-agent. It uses bash-only tool execution in Docker containers. The agent loop is: prompt → LLM → bash command → observe output → repeat.

**Repo:** https://github.com/SWE-agent/mini-swe-agent
**Harness:** Pier (datacurve-ai/pier) — Harbor-compatible eval framework

## Architecture

### Entry Point

```
minisweagent.run.benchmarks.swebench
├── app()              — Typer CLI entrypoint
├── process_instance() — Main per-task function (monkey-patchable)
├── get_sb_environment()  — Docker container setup
├── get_model()        — LLM client from config
├── ProgressTrackingAgent — Agent with step/cost limits
├── update_preds_file()   — Write predictions
└── remove_from_preds_file() — Cleanup on failure
```

### Agent Loop

```
1. System prompt (from config system_template)
2. Task message (from config instance_template, with {{task}} substitution)
3. Agent.run(task, env) loop:
   a. Send messages to LLM
   b. Parse response for bash tool calls
   c. Execute via env.execute({"command": cmd}, timeout=60)
   d. Observe output (truncated if >10K chars)
   e. Repeat until:
      - Agent outputs "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"
      - step_limit reached
      - cost_limit reached
4. Extract patch from final output
5. Save trajectory + predictions
```

### Config Format (YAML)

```yaml
agent:
  system_template: |
    You are a helpful assistant...
  instance_template: |
    <pr_description>{{task}}</pr_description>
    <instructions>...</instructions>
  step_limit: 250
  cost_limit: 3.0

environment:
  cwd: "/testbed"              # Working directory inside container
  timeout: 60                   # Per-command timeout
  interpreter: ["bash", "-c"]
  env:                          # Environment variables
    PAGER: cat
  environment_class: docker
  pull_timeout: 900

model:
  observation_template: |       # Jinja2 template for tool output
    <returncode>{{output.returncode}}</returncode>
    <output>{{output.output}}</output>
  cost_tracking: "ignore_errors"
  model_kwargs:
    temperature: 0.7
    top_p: 0.8
```

### Docker Container Interface

- **Image source:** DeepSWE uses `public.ecr.aws/d3j8x8q7/swe-bench-202605:{ext_id}`
- **Repo root:** Varies per benchmark. SWE-bench Lite uses `/testbed`, SWE-bench Pro uses `/app`
- **Execution:** `env.execute({"command": "bash -c '...'"}, timeout=60)`
- **No stdin:** All commands must be non-interactive
- **Output capture:** stdout + stderr, truncated at 10K chars

### Monkey-Patch Integration Pattern (from run_mini_gt_v7.py)

```python
from minisweagent.run.benchmarks.swebench import (
    app, get_sb_environment, get_model,
    ProgressTrackingAgent, update_preds_file,
    remove_from_preds_file, logger,
)
from minisweagent.run.benchmarks import swebench as swebench_module

def custom_process_instance(config, instance, output_dir):
    env = get_sb_environment(config, instance)
    # === GT injection point ===
    # 1. Inject gt_hook.py via chunked base64
    # 2. Generate brief on host
    # 3. Prepend brief to task string
    task = brief + "\n" + instance["problem_statement"]
    model = get_model(config, instance)
    agent = ProgressTrackingAgent(model, config["agent"])
    result = agent.run(task, env)
    update_preds_file(output_dir, instance, result)

# Override the module-level function
swebench_module.process_instance = custom_process_instance

# Run the CLI
app()
```

### Hook Injection Method (chunked base64)

The gt_hook.py file is ~115KB. Too large for a single `echo` command. Solution:

```python
# Encode at import time
_GT_HOOK_B64 = base64.b64encode(GT_HOOK_PATH.read_bytes()).decode("ascii")
_CHUNKS = [_GT_HOOK_B64[i:i+50_000] for i in range(0, len(_GT_HOOK_B64), 50_000)]

# Inject via env.execute
for i, chunk in enumerate(_CHUNKS):
    op = ">" if i == 0 else ">>"
    env.execute({"command": f"echo -n '{chunk}' {op} /tmp/gt_hook.b64"}, timeout=15)
env.execute({"command": "base64 -d /tmp/gt_hook.b64 > /tmp/gt_hook.py && rm -f /tmp/gt_hook.b64"}, timeout=15)
```

### Prompt Injection Points

1. **`instance_template`** in YAML: Contains the `{{task}}` variable. Add GT tool instructions here.
2. **`task` string** in `process_instance`: Prepend `<gt-task-brief>` before passing to `agent.run(task, env)`.
3. **`system_template`** in YAML: Add general GT context here (e.g., "You have a codebase intelligence tool").

### DeepSWE-Specific Details

- **Task format:** Harbor `task.toml` + `instruction.md` (not SWE-bench JSON)
- **Docker images:** `public.ecr.aws/d3j8x8q7/swe-bench-202605:{ext_id}`
- **Verification:** `test.sh` + `test.patch` applied at grading time
- **Submission:** `git diff > patch.txt && echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT && cat patch.txt`
- **Network:** `allow_internet = false` — agent cannot fetch dependencies or call APIs during task
- **Languages:** Go, TypeScript, Python, JavaScript, Rust (multi-language, not Python-only)
- **Repo root:** Needs probing — may be `/home/user`, `/workspace`, or `/testbed`

### Key Differences from OpenHands Integration

| Aspect | OpenHands | mini-swe-agent |
|--------|-----------|----------------|
| **Observation augmentation** | patched_run_action adds GT text to every tool result | Not available — bash-only, no observation hooks |
| **Post-edit hooks** | L3 fires automatically on every FileEditorAction | Must be agent-initiated: `python3 /tmp/gt_hook.py verify` |
| **Post-view hooks** | L3b fires automatically on every file read | Must be agent-initiated: `python3 /tmp/gt_hook.py understand <file>` |
| **L5 governor** | Monitors agent trajectory and intervenes | Not available — no trajectory access |
| **L6 reindex** | Automatic incremental reindex before hooks | Not available — pre-built index only |
| **Brief injection** | Prepended to system message via wrapper | Prepended to task string before `agent.run()` |
| **Tool budget** | Per-tool call limits enforced by wrapper | Agent decides usage; instructions say "1-2 key files" |
