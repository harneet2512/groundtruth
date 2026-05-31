# REMOTE_SOURCE_OF_TRUTH.md — GT Deep SWE / SWE-Live Readiness Audit (Phase 0)

Generated: 2026-05-31 (session continuation, branch gt-consensus-curation)
Audit output root: `.tmp_gt_turn_correctness_audit/github_sot/`
Principle: GitHub is the source of truth. Local files are execution/cache artifacts only.

---

## A. GT source

| Field | Value |
|---|---|
| Remote (`origin`) | `https://github.com/harneet2512/groundtruth.git` (fetch + push) |
| Current branch | `gt-consensus-curation` |
| Local HEAD | `eaa45b9c45ac22694d3fe8ae048ba578c54c9bff` |
| `origin/gt-consensus-curation` (ls-remote) | `eaa45b9c…` |
| Local HEAD == remote HEAD (this branch) | **YES — MATCH** ✓ |
| Remote default branch | `master` (`origin/master` = `27893ec2…`) — divergent line; GT product work lives on `gt-consensus-curation`, NOT master |

**HEAD provenance (note — HEAD moved during the session):** `eaa45b9c` = `feat(gitpod): one-command live streaming OH+GT run` — adds `.gitpod.yml`, `GITPOD_LIVE_RUN.md`, `railway/{gitpod_run.sh,codespace_run.sh}` only. `c6b44b65` (prior session tip) is a clean ancestor (`git merge-base --is-ancestor` = YES). **eaa45b9c touches NO GT product code → audited GT product logic == the c6b44b65 state.**

**Dirty tracked files (NOT part of audited SHA — must be EXCLUDED; materialize clean):**
```
 M gt-index/internal/store/incremental.go
 M scripts/swebench/oh_gt_full_wrapper.py      ← producer/integration code, DIRTY
 M scripts/verify/check_brief_delivery.py
 M src/groundtruth/hooks/post_edit.py           ← producer code, DIRTY
 M src/groundtruth/telemetry/schemas.py
 M tests/openhands/test_oh_gt_full_wrapper.py
 M tests/preflight/test_check_brief_delivery.py
 M tests/unit/test_post_edit_categorical_filter.py
```
**AUDITED GT SHA = `eaa45b9c` (committed state, dirty tree excluded).** Two of the dirty files are producer/integration code; auditing the working tree would audit uncommitted code. Materialize via a clean clone/worktree to guarantee no dirty contamination.

---

## B. Benchmark source

| Field | Value |
|---|---|
| Benchmark | DeepSWE |
| Remote | `https://github.com/datacurve-ai/deep-swe.git` |
| Reachable? | **YES (public)** — `git ls-remote … HEAD` = `2f0f41255912c9199a1dafa405ca068cd903624b`, exit 0 |
| Local manifest (selection source of truth) | `artifact_deepswe/repo_manifest.json` |
| `total_tasks` | **113** |
| `total_repos` | 92 |
| `language_distribution` | **typescript 35, go 34, python 34, rust 5, javascript 5** |
| Distinct languages | **EXACTLY 5** ✓ (Python, TypeScript, Go, Rust, JavaScript — matches expected set; no fifth-language fabrication) |

**Per-task fields IN manifest:** `instance_id`, `repo_url`, `commit_hash`, `docker_image` (`public.ecr.aws/d3j8x8q7/swe-bench-202605:<tag>`), `language`, `category`, `display_title`, `agent_timeout_sec`, `instruction_chars`.

**Per-task fields NOT in manifest** (must source from `deep-swe` `tasks/<id>/` and/or the docker image): **issue/instruction TEXT, gold patch/solution files, tests, env metadata.** → Phase 1 materialization dependency.

**Checkout anchor = `commit_hash`** (the plan's instruction; there is no `parent_commit` field — do not invent one). NOTE: a few `commit_hash` values are short SHAs (e.g. `eicrud` `68dafce`, `langchain` `7cef35b`) → must full-resolve on clone.

**DATA QUALITY (record, do NOT fix) — manifest `language` mislabels:**
- `httpx-*` tasks on `encode/httpx` tagged `typescript` (repo is Python).
- `koota-entity-snapshot-rollback` on `pmndrs/koota` tagged `python` (repo is TypeScript).
- `prometheus-transactional-reload-status` on `prometheus/prometheus` tagged `typescript` (Prometheus has a TS web UI — ambiguous).
→ Language-stratified selection must verify language from the repo, not trust the label blindly.

**Stale local corpus (HISTORICAL DIAGNOSTIC ONLY — not readiness proof):** `.tmp_gt_turn_correctness_audit/` + `corpus_metadata.json` = a 60-task / **4-lang** subset (rust 12, ts 12, go 12, python 24; **no javascript**). Superseded by the 113-task / 5-lang manifest above.

---

## C. Workflow source

| Workflow | Purpose | Role |
|---|---|---|
| `.github/workflows/deepswe_preindex.yml` | Reads `artifact_deepswe/repo_manifest.json`, pulls ECR docker images, runs `gt-index` (CGO linux build) INSIDE each container → `graph.db`, uploads artifacts | Reference for in-container indexing + task source |
| `.github/workflows/deepswe_trial.yml` | Clones `datacurve-ai/deep-swe`, runs ONE task via `pier run` + `GTMiniSweAgent` (`artifact_deepswe/gt_agent.py`) + `gt_hook.py` injection | DeepSWE / mini-swe-agent harness (GT "Path 3") |

**HARNESS DISTINCTION (critical):** the layers this audit verifies — **L1** (`v1r_brief`), **L3b** (`post_view`), **L3** (`post_edit`), **L6** (gt-index incremental reindex) — are the **OpenHands wrapper layers** in `scripts/swebench/oh_gt_full_wrapper.py` (GT "Path 2"). The deepswe workflows drive a **different** harness (pier / mini-swe-agent). Therefore this audit drives the **wrapper layers over the deep-swe CORPUS via `CONTROLLED_TURN_TRACE`**, not the pier autonomous trajectory. An `AUTONOMOUS_TRACE` would require running the OH integration on a deep-swe task end-to-end — out of smoke scope.

**No GHA artifacts consumed this run** (no run ID / artifact download / checksums). GHA existence ≠ proof.

---

## Phase 0 gate assessment

| Gate | Result |
|---|---|
| Exactly 5 languages in source-of-truth manifest | **PASS** (no stop) |
| Benchmark URL discoverable + reachable | **PASS** (`datacurve-ai/deep-swe`, HEAD `2f0f4125`) |
| GT remote discoverable + HEAD pinned | **PASS** (`harneet2512/groundtruth`, HEAD `eaa45b9c` == local) |
| Local HEAD == remote HEAD | **PASS** |

→ No stop condition triggered. Next: **GT code audit (architecture / implementation / integration / plumbing)** per user redirect, then Phase 1 materialization + Phase 2 selection.
