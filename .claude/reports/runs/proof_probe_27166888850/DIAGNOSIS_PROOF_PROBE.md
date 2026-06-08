# Diagnosis — proof probe 27166888850 (6 fail-closed, generalized + research-backed)

## "All 3 off" vs "1 off"
**No task has >1 gate off.** Every failure is a SINGLE-gate fail; the 4 passes are all-3-GREEN.
So each substrate gap is cleanly isolated to one dimension — no compound failures.

| gate off | tasks | dimension |
|---|---|---|
| G3 embedder | beets-5495, haystack-8489 | semantic non-discriminative on the RENDERED set |
| G1 resolution | faker-2142, flask-5626 | whole-graph name_match dominance |
| G2 lsp | checkov-6893, matplotlib-28933 | demand-resolve below floor |

## Your question: "weights change dynamically but embedding should be higher?"
**No — raising W_SEM does not fix G3, and is the wrong layer.** Proof from the data:
- **beets**: the per-file sem is the IDENTICAL constant `0.83886` on 9/11 candidates (`mad=0`). A
  constant × a bigger weight is still a constant — it shifts every candidate equally and changes
  NOTHING in the ranking, while the dispersion gate still sees `mad=0`. Higher W_SEM = amplify a
  flat signal.
- **haystack**: the rendered top-5 are witness-only files with `sem=0.0`; the sem-scored files
  (0.84/0.82/0.83/0.80) ranked BELOW them. Raising W_SEM *would* pull the sem-scored files up — but
  that's tuning the weight to win a gate, i.e. benchmaxxing, not localization quality, and per
  BRIEFING §3-4 the research lever is field-lexical (BLUiR ASE 2013) OVER dense, with W_SEM
  deliberately the smallest weight. The dynamic adapter (`_adapt_weights_for_issue`) is correct to
  keep semantic secondary.

The real G3 defect is that the **semantic signal is not present-and-discriminative on the final
rendered candidate set** — two distinct sub-modes, neither a weight:

1. **beets — flat/degenerate sem (low variance).** e5-small-v2 cosines over short name+signature
   FILE summaries cluster tightly (~0.80–0.84); the bit-identical `0.83886` ×9 means the summary
   text for those files is effectively non-distinguishing to the model. **Research:** dense
   retrievers have poor *dispersion/calibration* at coarse (file) granularity — SWERank (ICLR 2025)
   and RepoRift embed at FUNCTION/snippet granularity precisely for this; BLUiR (ASE 2013) shows
   field-level lexical beats flat-blob dense for code localization. **Fix (real GT improvement):**
   embed at finer granularity (function/snippet) or enrich per-file summaries so files are
   distinguishable — NOT a weight bump.

2. **haystack — sem-scored files not rendered.** The embedder discriminated (4 distinct nonzero
   sems) but the COMPOSITE ranking (witness/lex/path-dominant) rendered witness-only files that
   bypassed semantic scoring, so the rendered top-5 is all `sem=0`. **The gate measures the wrong
   population:** GATE-3 checks sem dispersion over the composite-selected rendered top-5, but the
   embedder's consumption should be judged over the candidate universe IT scored. The embedder WAS
   consumed → this is partly a GATE-3 false-negative of "consumption." **Fix:** measure dispersion
   over the sem-scored set, AND/OR guarantee semantically-strong candidates are represented in the
   render (a min-sem-candidate guarantee, mirroring the existing min-BM25 guarantee).

## G1 — name_match dominance (faker, flask): static call-graph ceiling on DYNAMIC Python
- faker: det 42.94731731%, name_match 2350 > det 1769, **ev:assignment_tracked=0**.
- flask: det 44.81361426%, name_match 681 > det 553, ev:assignment_tracked=26.
- contrast (passes): conan ev:assignment_tracked=4148 → det 77.81%, haystack 730 → 78.07%,
  checkov 1257 → 69.37%. **The assignment-flow tier is the differentiator** — it barely/never fired
  on faker.
- **Root cause (generalized):** faker is a FACTORY/PROVIDER library — its API is dynamic dispatch
  (`fake.<provider>()` via `__getattr__`/registry); flask uses decorator-based dynamic routing.
  Static CHA/RTA + assignment-flow CANNOT resolve dynamic dispatch, so name_match dominates. This
  is the inherent ceiling of static call-graph construction, not a bug. **Research:** PyCG (Salis
  et al., ICSE 2021) names dynamic features (getattr/dynamic dispatch) as its hard limit; XTA
  (Tip & Palsberg, OOPSLA 2000) and CHA (Dean et al., ECOOP 1995) are static-only.
- **The demand-scoped LSP pass is NOT the lever** (it PASSED GATE-2 on both). The fix is WHOLE-GRAPH
  type inference (SCIP/LSP batch over the full repo, per CLAUDE.md scale section), or accepting that
  a highly-dynamic repo's map is genuinely mostly-guesses → fail-closed is CORRECT (the agent WOULD
  fly blind; do not relax the floor to "pass" it — that's benchmaxxing).

## G2 — demand-resolve below floor (checkov, matplotlib): two sub-causes
- **checkov: resolved=0, residual=1, scoped=1158 files** → frac 0.0. The demand-scope OVER-INCLUDED
  (1158 files) yet only 1 in-scope name_match method edge, unresolved. **Root cause:** scope
  PRECISION — 1158 files is the opposite of demand-driven. **Research:** Heintze & Tardieu
  (*Demand-Driven Pointer Analysis*, PLDI 2001) — the scope must be MINIMAL/query-relevant; an
  over-broad seed both dilutes and makes a residual of 1 a coin-flip vs the 0.1 floor. **Fix:**
  rank+cap the scope seed so the residual reflects the true issue subgraph.
- **matplotlib: resolved=3, residual=31, scoped=5 files** → frac 0.09677419 (barely < 0.1).
  matplotlib is C-extension-heavy; many method calls target compiled/C functions pyright cannot
  resolve via go-to-definition → residual stays unresolved. **Root cause:** LSP language boundary
  (Python→C extension). **Fix:** exclude calls into compiled/non-Python targets from the residual
  denominator (they are not Python-resolvable), or classify them as a separate tier — not a
  silent failure.

## Net (per CLAUDE.md: GT gaps, never "model")
- **G3 is the most actionable GT-code improvement** (finer-granularity embedding + min-sem-candidate
  guarantee + judge dispersion over the sem-scored set). Locally testable.
- **G1** is a real static-analysis ceiling on dynamic repos → whole-graph type inference (scale lever),
  not the demand-pass. Fail-closed is the honest behavior.
- **G2** is demand-scope precision (checkov) + a Python→C boundary (matplotlib) — both fixable in the
  scope seed + residual denominator without touching ranking.
- Every fix above is GENERALIZED (repo/language-agnostic property), research-cited, and changes the
  SUBSTRATE/gate definition, not task-specific tuning. NONE is a W_SEM bump.
