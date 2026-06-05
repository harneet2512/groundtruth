# Contract-DRIFT — codespace runbook (2026-06-05)

The drift lever is built and proven locally against the real indexer (engine + transport +
CLI). The remaining steps are **codespace-only** (need CGO/gcc and the SWE-bench artifacts).
Run them in order; do not skip a gate.

Live testing is **GitHub Codespaces only** (never gcloud, never local GHA). Baseline is frozen
(`.claude/reports/full300_baseline_ohdeepseek_20260531/FINAL_resolved_300_20260531.json`) —
never re-run GT-off.

---

## Gate 1 — Precondition: resolver laundering test

```bash
bash scripts/drift/check_precondition.sh
```
- **GREEN** → `DOC_OF_HONOR.md:355-357` is stale; update it; proceed.
- **RED** → the resolver demote isn't in this binary; either fix it or disable edge-derived
  caller counts in `contract_map._verified_caller_count` before any run. Blocks the run.

Build the run binary first if needed:
```bash
cd gt-index && CGO_ENABLED=1 go build -tags sqlite_fts5 -o gt-index ./cmd/gt-index/
```

---

## Gate 2 — Offline ceiling over the 9 NO trajectories (FREE)

The 9 gradeable NO trajectories are from flip run `27011135159`. For each, you need the repo at
the base commit and the agent's patch (`model_patch` from the run output). Then:

```bash
export GTBIN=$PWD/gt-index/gt-index
for task in <task1> <task2> ... ; do        # the 9 ids
  # check out repo at base commit into /tmp/repos/$task (your existing helper)
  python scripts/drift/offline_ceiling.py \
      --root /tmp/repos/$task --binary $GTBIN \
      --patch /tmp/patches/$task.diff --name $task
done
```

Read each `VERDICT` + `<gt-drift>` block. The **ceiling question**: for the tasks whose failure
was a contract/interface break (briefcase, aiogram, loguru, falcon — the consistency subset),
did drift fire and name the breaking change? For pure test-vocabulary failures (haystack "line",
arviz exact message) drift is expected QUIET — a call-graph contract cannot reach those.

Honest expectation: drift reaches ~2-4 of 9. If it fires on **zero** of the consistency subset,
the lever is dead — do not spend a run.

---

## Gate 3 — The single gated run (per scaffold)

Only if Gate 1 GREEN and Gate 2 shows drift fires on ≥1 consistency-subset task.

**Arms (NOT A/B/C):** compare against the on-disk current-GT baseline (already emits contract
context). Treatment = current GT **+ drift**. The delta isolates drift, not contract-presence.

- **OpenHands**: the wrapper already freezes `graph.db.orig` and `post_edit.py` emits the
  `<gt-drift>` block. Run GT-on as usual; the drift block rides the existing post-edit path.
- **mini-swe**: set `GT_DRIFT_ENABLED=1`, `GT_INDEX_BINARY=<path>`, ensure `groundtruth` is on
  `PYTHONPATH`. The agent gets the `gt drift` instruction and pulls drift itself.

**Two gates (what makes a single run interpretable):**
1. **Behavior** — did the agent *use* the drift in-trajectory (reference/act on it)? Extract from
   `output.jsonl` (AGENT-OBSERVATION rule — never telemetry counts). If ignored like the old
   completeness hints, the lever is dead regardless of design.
2. **Flips** — did resolved tasks move vs the frozen baseline? Paired Wilcoxon on per-task delta
   (never avg-subtraction).

**Kill** if ignored or flat.

---

## Deep metrics (mandatory, per CLAUDE.md)

Persist per task at 8-dp: drift eligible/emitted/suppressed, `rendered_tokens_total`,
`utilization_score`, agent `action_count` / `first_edit_action` / `edit_to_gold_action`,
the RAW delivered `<gt-drift>` text from `output.jsonl`, outcome, and paired deltas vs baseline.
Files: `gt_deep_metrics_<task>.json` (+ `gt_metrics_delta_<task>.json` when paired).

---

## Quick local sanity (already passing — for reference)

```bash
python -m pytest tests/unit/test_contract_drift.py tests/unit/test_drift_hook.py -q
python .tmp_drift_realbinary_proof_20260605.py      # engine, real binary
python .tmp_drift_transport_proof_20260605.py       # transport (freeze->reindex->drift), real binary
```

---

## File map

- Engine: `src/groundtruth/pretask/contract_map.py` (`build_drift`, `snapshot_contract`)
- Transport: `src/groundtruth/hooks/drift_hook.py` (frozen-original baseline)
- CLI: `src/groundtruth/hooks/drift_cli.py` (`gt drift`)
- OH push: `src/groundtruth/hooks/post_edit.py` + `scripts/swebench/oh_gt_full_wrapper.py` (freeze)
- mini-swe pull: `deepswe-pier/src/pier/agents/installed/mini_swe_agent.py` (gated `GT_DRIFT_ENABLED`)
- Integration doc for the mini-swe team: `docs/MINISWE_CONTRACT_DRIFT_INTEGRATION_20260605.md`
- Precondition: `scripts/drift/check_precondition.sh`
- Offline ceiling: `scripts/drift/offline_ceiling.py`
