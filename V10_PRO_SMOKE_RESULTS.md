# GT v10 SWE-bench Pro Smoke Test — 2026-03-28

## Setup
- Scaffold: mini-swe-agent 2.2.7 + Qwen3-Coder-480B
- GT: v10 ego-graph with real code + behavioral obligations
- Dataset: ScaleAI/SWE-bench_Pro (731 tasks, 11 repos)
- 5-task smoke: 3 qutebrowser + 2 ansible (+ accidentally 2 NodeBB)
- Docker images: jefzda/sweap-images:{dockerhub_tag}

## What Worked
1. SWE-bench Pro dataset loads (731 tasks, 11 repos)
2. Docker images pull from DockerHub (jefzda/sweap-images)
3. mini-swe-agent runs with /app root (Pro uses /app not /testbed)
4. docker_image mapping from dockerhub_tag works
5. gt_hook.py injects and runs understand inside Pro containers
6. Both baseline and GT produced 5/5 predictions
7. Baseline: 24 min, GT: 20 min for 5 tasks

## What Failed
1. **File detection: 0/5 tasks got useful GT context**
   - Regex extracted false positives: `log.py`, `a/b/example/com.py`, `content/blocking/enabled.py`
   - Pro issue descriptions use URLs, code blocks, and HTML that confuse the file path regex
   - The class name grep fallback ran but found nothing useful

2. **Container lifecycle timing**
   - Some grep commands failed with "No such container" — the Docker container was cleaned up before GT precompute finished

## Root Cause: File Detection Not Tuned for Pro

SWE-bench Lite issues mention explicit Python file paths (django/contrib/auth/tokens.py).
SWE-bench Pro issues are GitHub issues with URLs, screenshots, HTML, and code blocks.

The current file detection regex picks up:
- URL paths: `a/b/example/com.py` from URLs like `http://a.b.example.com/...`
- Test fragments: `assert/test.py` from `assert test == ...`
- Log references: `log.py` from log output

**Fix needed:** Strip URLs, code blocks, and HTML before file detection. Add Pro-specific patterns.

## Ego-Graph Pipeline: Validated

When understand successfully ran on a real file (tested manually on ansible/utils/encrypt.py), the v10 ego-graph output showed:
- TARGET with 6 lines of real source code
- CALLS with real callee implementations
- OBLIGATIONS with norms and test coverage
- All working on a Pro container with /app root

## Next Steps

1. Fix file detection for Pro issue format (strip URLs, code blocks, HTML)
2. Re-run 5-task smoke with fixed detection
3. If >=3/5 get GT context: proceed to full Pro run
4. Full Pro run: all 731 tasks (or Python subset: 266 tasks)
