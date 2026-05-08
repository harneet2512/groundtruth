# OH ↔ GroundTruth full integration — handoff (V3)

**Date:** 2026-05-07  
**Branch (code push):** `feat/oh-gt-full-wrapper-layers`  
**Scope:** OpenHands SWE-bench Live "full potential" GT path: pretask brief (L1/L2), hooks (L3/L3b), L4 CLI tools, L5 finish advisory, L6 incremental reindex.

This document is for engineers continuing the work. It can be committed separately when `.md` is allowed on the branch.

---

## What shipped in code

| Area | Change |
|------|--------|
| **Wrapper** | `scripts/swebench/oh_gt_full_wrapper.py` — `gt_brief` via `v7_brief.generate_brief(..., return_telemetry=True)`; append **L2** line from real `module_6_hybrid` telemetry; **L5** uses `pending_checks` vs `verified_checks` with path matching; **`register_gt_validate_paths`** on `CmdRunAction` lines containing `gt_validate`; **INTERNAL_GT_MARKERS** no longer treats agent `gt_query`/`gt_validate`/… as internal (fixes mistaken skip). |
| **post_edit** | Removed hardcoded filler line that printed a fake `[GT_L5]` advisory when below `max_items`. |
| **post_view** | Coupling / threshold tweaks — structural `[GT_L3B]` output for auditability. |
| **Smoke shell** | `scripts/swebench/oh_gt_full_smoke10.sh` — parallel workers default, env documentation. |
| **Layer audit** | `scripts/swebench/oh_gt_full_layer_audit.py` — task-agnostic `output.jsonl` checker for L1–L6; handles **JSON-escaped** embedded XML (quoted attributes). |

---

## How to run smoke (VM)

- OpenHands checkout on **gt-t0** used for evaluation: **`/home/ubuntu/OpenHands-0.54.0`** (`.venv` there). The generic `/home/ubuntu/OpenHands` path may **not** have a venv — align `OH_DIR` or wrapper invocations accordingly.
- Set `OUT_ROOT`, `GT_INDEX_BINARY`, `PYTHONPATH` to the GroundTruth repo, then run `oh_gt_full_wrapper.py` with `--instance-ids`, `--dataset`, `--split`, `--eval-output-dir`, etc.

---

## Layer audit (post-smoke)

```bash
python3 scripts/swebench/oh_gt_full_layer_audit.py /path/to/.../output.jsonl
```

Stderr summarizes per-layer hit rates; stdout is one line per `instance_id` with `Y`/`N` flags.

**Interpretation:**

- **L3** requires post-edit **family** tags like `[GT_CHANGE]`, not `[GT_L3B]` (view-only). A run can show reindex + empty evidence if abstention wins.
- **L5** advisory appears when edits remain **unvalidated** by observed `gt_validate` paths; if the agent validates, L5 may correctly be **absent**.

---

## 1-task verification reference (2026-05-07)

- Instance: **`amoffat__sh-744`** (Live Lite list index 14).
- After audit script fix: L1, L2, L3b, L4, L6 positive; L3 negative (no edit-family tags); L5 negative (likely `gt_validate` in trace).

---

## Constraints (do not regress)

- No **FAIL_TO_PASS / PASS_TO_PASS / test_patch** shortcuts in harness docs.
- Build **`graph.db` in-container** for the task workspace; avoid shipping host index as truth without matching repo state.
- Do not reintroduce **token-only** proof strings; prefer telemetry and real hook or tool output.

---

## Files touched

- `scripts/swebench/oh_gt_full_wrapper.py`
- `scripts/swebench/oh_gt_full_layer_audit.py`
- `scripts/swebench/oh_gt_full_smoke10.sh`
- `src/groundtruth/hooks/post_edit.py`
- `src/groundtruth/hooks/post_view.py`
