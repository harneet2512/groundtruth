# Session Summary

## Date / Time
2026-06-09

## Branch / Commit
`gt-trial` @ `8a65b4ce` — pushed to **BOTH origin/harneet2512 AND hbali-stack** (user override of the
prior "code→origin only" rule, to enable the DeepSWE runs on hbali).

## Objective
Close LSP-stamping + image-pull + brief-consume gaps; run + §4-audit the 30-task paired agent run;
diagnose 0-flips; build the localization levers; **pivot the validation surface to the Datacurve
DeepSWE benchmark (mini-swe-agent via Pier)** and bring FULL OH-depth GT to it, language-agnostic.

## Files read (key)
`scripts/metrics/foundational_gates.py`, `scripts/swebench/gt_run_proof.py`,
`src/groundtruth/pretask/{v1r_brief,v7_4_brief,anchor_select,graph_localizer,specificity}.py`,
`src/groundtruth/memory/enrich/embed.py`, `gt-index/cmd/gt-index/main.go`,
`scripts/swebench/oh_gt_full_wrapper.py`, `benchmarks/swebench/{gt_hook,gt_intel,run_mini_gt_hooked}.py`,
`artifact_deepswe/{gt_agent,gt_mini_patch}.py`, `.github/workflows/{cache_deepswe_images,deepswe_*}.yml`,
`artifact_deepswe/repo_manifest.json` (113 tasks: TS 35 / Go 34 / Py 34 / Rust 5 / JS 5).

## Exact decisions used
- DeepSWE harness = **pier + mini-swe-agent** (confirmed `gt_deep_metrics.py:117,120`).
- TWO GT engines (ONE-PRODUCT violation): `v1r/run_v74` (deep, multilingual — levers) vs `gt_intel`/`gt_hook`
  (ast, Python-only). **UNIFY onto v1r.** The brief already reaches DeepSWE via `gt_agent._generate_brief`.
- Dense-weight LOCKED: dense-led + `W_SEM_FLOOR=0.25`>0, never throttled, lexical-fused (not monopoly).
- Validation surface: **Datacurve DeepSWE** (113 tasks / 5 langs / contamination-free / unsaturated),
  not SWE-Live-Lite (low mindshare) nor saturated Verified. 113 images cached to GHCR.
- Embedder: swap e5 → **gte-modernbert-base (Apache-2.0, open-source)**, configurable, e5 fallback.

## Research checked
ColBERT MaxSim (SIGIR 2020), MaxP (SIGIR 2019); CoIR (2407.02883), CodeXEmbed (2411.12644),
CodeSage (ICLR 2024); BEIR (NeurIPS 2021), DPR (EMNLP 2020), Sciavolino (EMNLP 2021);
RepoGraph (ICLR 2025). LLM-agent localizers (Agentless/LocAgent/OrcaLoca) NOT adopted (GT is LLM-free).

## Implementation changes (committed + pushed)
`9e7edeca` LSP-stamp fix · `dff3144b` image-pull retry · `8d48360a` brief-consume · `db267869` dead-shim
· `33970b9f` **CHANGE 1 per-symbol MaxSim** · `8a65b4ce` **fusion + dense floor** · gt_gt §11/§12/§13 ·
30 §4 ledgers. **In flight (worktrees):** CHANGE 2 (gte-modernbert), unify ast-residue → cross-lang evidence.

## Metrics before → after
- 30-task PAID run `27214152241`: 30/30 ran, 0 fail, **2/30 resolved, 0 GT-caused flips**, leakage 0.
- CHANGE 1 (Stage-1, real graph e5/384): gold #1 **3/7 → 7/7**; synthetic separation 0.02 → 0.67 (~30×).
- Fusion+floor: 15/15; `effective_w_sem ≥ 0.25 > 0` on every classification.

## Tests / runs executed
30-task GHA agent run (complete); CHANGE 1 23/23; fusion 15/15; full fail_closed 149; no new regressions.
Step-0 DeepSWE Go-task trial: dispatched (verify the `<gt-task-brief>` lands cross-language).

## Result
`v1r` upgraded (granularity + dense-floor fusion), committed/pushed/documented. DeepSWE pivot grounded
in code; the brief engine already reaches the mini-swe agent. CHANGE 2 + unify in flight.

## Regressions
None new (CHANGE 1 flipped 1 prior failure to pass; the pre-existing fail_closed/render failures unchanged).

## Rollback
Each lever is a revertable commit; CHANGE 2 + unify are uncommitted worktrees.

## Open blockers
CHANGE 2 live model-load pending `setup_models` fetch / image bake; unify per-turn evidence needs the GT
package in-container (substrate bake) or host-side; verify a real DeepSWE trajectory fires the brief (Step-0).

## Next allowed action
Integrate CHANGE 2 + unify when validated; bake the DeepSWE substrate image (gte-modernbert + pyright/
gopls/rust-analyzer/tsserver) for in-container v1r + L6 LSP-preservation; paired baseline + GT-on on
DeepSWE across all 5 languages.

---
*Architecture of record: `gt_gt.md` §11–§13. This session's docs live in gt_gt §13 + here.*
