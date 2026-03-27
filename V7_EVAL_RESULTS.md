# GT v7 Evaluation Results — 2026-03-27

## Setup
- **Scaffold**: OpenHands SDK v1.14.0
- **Model**: Qwen3-Coder-480B via Vertex AI (litellm proxy)
- **GT delivery**: Passive post-edit hook (gt_hook.py fires automatically after file_editor tool)
- **Max iterations**: 50 per task
- **VM**: swebench-ab (e2-standard-16), project serious-water-484116-j0

## 7-Task Smoke Test

| Metric | Baseline | GT v7 |
|---|---|---|
| Tasks attempted | 7 | 7 |
| Patches produced | 5 | **7** |
| Resolved | **5/5 (100%)** | **7/7 (100%)** |
| Cost | $1.25 | $2.01 |
| Avg time/task | 2:46 | 3:22 |

**GT recovered 2 tasks** (django-10914, django-11133) that baseline failed on runtime errors.

### Resolved instances
- Shared (5): astropy-12907, django-11099, psf/requests-2317, scikit-learn-13779, sympy-18189
- GT only (2): django-10914, django-11133

## 18-Task Full Run

| Metric | Baseline | GT v7 |
|---|---|---|
| Tasks attempted | 18 | 18 |
| Patches produced | 11 | 11 |
| **Resolved** | **8/11 (72.7%)** | **8/11 (72.7%)** |
| Resolve rate (of 18) | 44.4% | 44.4% |

### Resolved instances
- Shared (7): astropy-12907, astropy-14995, django-10914, django-11099, django-11133, django-11179, django-11999
- Baseline only (1): **django-12125**
- GT v7 only (1): **django-11815**

## Analysis

### What worked
1. **GT hooks fired reliably** — HookExecutionEvent on every file edit across all instances
2. **gt_hook.py injection succeeded** — 115KB file (20 base64 chunks) injected into every Docker container
3. **Passive hook delivery is stable** — the OpenHands HookManager integration works

### Why no resolve rate lift
1. **Passive-only delivery** — the hook fires AFTER edits, catching issues post-hoc. The agent doesn't get cross-file intelligence BEFORE editing.
2. **The `understand` command was not called** — the active tool (which provides callers, test files, norms) requires the agent to explicitly run it. The prompt template includes instructions, but the OpenHands agent uses the default system prompt which may override or deprioritize the instance-level instructions.
3. **Small sample** — 18 tasks, 8 vs 8 resolved. Need 50+ for statistical significance.

### What would make GT win
- **Active tool adoption**: The agent needs to call `python3 /tmp/gt_hook.py understand <file>` during exploration. The passive hook alone catches errors after the fact but doesn't prevent them.
- **Prompt integration**: The GT tool instructions may need to be in the system prompt, not just the instance prompt.
- **Larger sample**: 50+ tasks with pre-built Docker images.

## Infrastructure Notes
- Only 18 of 50 SWE-bench Lite instances have pre-built Docker images on GHCR for SDK version 62c2e7c
- The SDK submodule is missing Dockerfile, preventing local builds
- Qwen3-Coder via Vertex AI has intermittent connection errors causing ~30% of attempts to fail
