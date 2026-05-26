# DeepSWE + GroundTruth Dry Run

## Date: 2026-05-26

## Environment

- **Machine:** Windows 11, no Docker, no Go compiler
- **gt-index status:** Binary exists (gt-index.exe 58MB, gt-index-linux 47.5MB) but exe crashes with STATUS_DLL_NOT_FOUND (CGO/MinGW dependency)
- **Python:** 3.12 with groundtruth package installed
- **sentence-transformers:** Available (model loading observed)

## What Was Tested

Since gt-index and Docker are unavailable on this machine, the dry run verifies the **host-side pipeline** that runs before the agent starts:

1. **Anchor extraction** — `extract_issue_anchors()` from issue text + graph.db
2. **v22 brief generation** — `generate_brief()` from v22_brief.py (latest RRF ranker)
3. **v1r brief generation** — `generate_v1r_brief()` from v1r_brief.py (hybrid BM25+graph)
4. **inject.py wrapper** — `generate_deepswe_brief()` (v22 → v1r fallback chain)

Tested on 3 existing holdout graph.db files covering TypeScript (hono), Go (crossplane), and Python (marimo).

## What Was NOT Tested (requires Docker/Linux)

- Container startup from DeepSWE Docker images
- gt_hook.py injection via chunked base64
- In-container gt-index execution
- Agent interaction with GT hook (understand/verify commands)
- Full end-to-end task execution with LLM

## Test Results

_(Results from `scripts/dry_run_brief.py` — all 3 test cases PASSED)_

### Test Case 1: hono-4876 (TypeScript)

- **Graph:** 2,518 nodes, 2,308 edges, 12.6% deterministic
- **Issue:** "The Context.header() method in Hono does not properly handle multiple Set-Cookie headers..."
- **v22 brief:** OK, 1,130 chars
- **v1r brief:** OK, 854 chars, 5 files ranked
- **Anchors:** OK
- **inject.py:** OK, 1,130 chars (v22 brief selected)
- **Top candidates:** `src/context.ts` (rank 1), `src/hono.ts` (rank 2), `src/jsx/context.ts` (rank 3)
- **Observation:** `src/context.ts` correctly ranked #1 (Context.header() is defined there). Even with 12.6% deterministic edges, BM25 compensates. `L1_SCOPE=low` correctly reflects limited graph signal.

### Test Case 2: crossplane-7332 (Go)

- **Graph:** 2,962 nodes, 5,234 edges, 26.8% deterministic
- **Issue:** "Crossplane provider revision controller does not reconcile..."
- **v22 brief:** OK, 1,595 chars
- **v1r brief:** OK, 1,899 chars, 5 files ranked
- **Anchors:** OK
- **inject.py:** OK, 1,595 chars (v22 brief selected)
- **Top candidates:** `internal/xpkg/config.go` (rank 1), `internal/xpkg/fake/config.go` (rank 2), `internal/controller/apiextens...` (rank 3)
- **Observation:** BM25 correctly matched "config" and "controller" to relevant Go files. `L1_SCOPE=low` with `distinct=1` shows some graph diversity.

### Test Case 3: marimo-9408 (Python)

- **Graph:** 28,835 nodes, 51,497 edges, 42.1% deterministic
- **Issue:** "The marimo notebook cell output is not properly handling HTML content with embedded JavaScript..."
- **v22 brief:** OK, 1,352 chars
- **v1r brief:** OK, 2,204 chars, 7 files ranked
- **Anchors:** OK
- **inject.py:** OK, 1,352 chars (v22 brief selected)
- **Top candidates:** `marimo/_utils/scripts.py` (rank 1), `frontend/src/utils/iframe.ts` (rank 2), `marimo/_output/formatters/if...` (rank 3)
- **Observation:** `L1_SCOPE=high` with `distinct=2, high=2` — Python graph has strong edges. Both Python (`scripts.py`) and TypeScript (`iframe.ts`) files ranked correctly for this polyglot issue about HTML/JS output.

### Pipeline Summary

| Component | hono (TS) | crossplane (Go) | marimo (Python) |
|-----------|-----------|-----------------|-----------------|
| v22 brief | OK (1130) | OK (1595) | OK (1352) |
| v1r brief | OK (854) | OK (1899) | OK (2204) |
| Anchors | OK | OK | OK |
| inject.py | OK (1130) | OK (1595) | OK (1352) |
| L1 Scope | low | low | **high** |

**All 12 checks passed (4 components x 3 languages).** Total dry run time: ~5 min on CPU (dominated by sentence-transformers model loading).

## Pipeline Verification

| Component | Status | Notes |
|-----------|--------|-------|
| `repo_manifest.json` | PASS | 113 tasks from 92 repos extracted from DeepSWE task.toml files |
| `inject.py` imports | PASS | v22_brief, v1r_brief, anchors all importable |
| `deepswe_gt.yaml` | PASS | Valid YAML, multi-language instance_template, step_limit=300 |
| `patch_mini_swe.py` structure | PASS | Monkey-patch pattern matches proven run_mini_gt_v7.py |
| `run_deepswe.sh` | PASS (syntax) | Bash syntax valid, argument parsing correct |
| `run_all.sh` | PASS (syntax) | Batch runner with --smoke/--language/--workers flags |
| `preindex.sh` | PASS (syntax) | Docker-based indexing pipeline with skip-if-exists |

## Reproduction Steps

To reproduce a full GT+DeepSWE run on a Linux machine:

```bash
# 1. Clone the artifact branch
git clone -b artifact_deepswe https://github.com/<repo>/Groundtruth.git
cd Groundtruth

# 2. Install GT
pip install -e .

# 3. Pre-build indexes for all repos (requires Docker)
cd artifact_deepswe/gt_integration
./preindex.sh --workers 4 --output-dir ../indexes

# 4. Run a single task
export DEEPSEEK_API_KEY="..."
export GT_PREBUILT_INDEXES_ROOT="../indexes"
./run_deepswe.sh dateutil-rfc5545-timezone-interop --model deepseek/deepseek-v4-flash

# 5. Run a smoke test (5 random tasks)
./run_all.sh --smoke 5 --model deepseek/deepseek-v4-flash

# 6. Run all Python tasks
./run_all.sh --language python --workers 2

# 7. Run all 113 tasks
./run_all.sh --workers 4
```

## Known Limitations

1. **gt-index.exe DLL issue:** The Windows binary needs MinGW runtime DLLs. Workaround: use gt-index-linux on Linux/Docker.
2. **No observation augmentation in mini-swe-agent:** Unlike OpenHands, mini-swe-agent doesn't support automatic post-edit/post-view hooks. GT evidence is available only via explicit `python3 /tmp/gt_hook.py understand <file>` calls.
3. **DeepSWE containers may block network:** `allow_internet = false` means the agent can't install pip packages. The gt_hook.py script must be self-contained (it is — no external dependencies).
4. **Repo root varies:** DeepSWE containers may use `/home/user`, `/testbed`, `/workspace`, or other paths. `patch_mini_swe.py` probes for `.git` directory to detect the root.
