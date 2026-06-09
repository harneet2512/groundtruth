# LATEST TASK — Unleash full-power GT on DeepSWE (mini-swe-agent), multilingual

**Status:** IN PROGRESS — localization levers landing; DeepSWE/mini-swe unify mid-build.
**Branch:** `gt-trial` @ `8a65b4ce` (pushed to origin/harneet2512 + hbali-stack).
**Last updated:** 2026-06-09
**Canonical detail:** `gt_gt.md` §11 (findings), §12 (per-layer roles), §13 (this pivot + build order);
`SESSION_SUMMARY.md`. (Older `RESPEC.md` / `jedi_WORK.md` / the CLAUDE.md ICEMAN block are historical.)

---

## The current goal
Validation surface = **Datacurve DeepSWE** (`github.com/datacurve-ai/deep-swe`): 113 tasks, 91 repos,
**5 languages (TS 35 / Go 34 / Py 34 / Rust 5 / JS 5 — 70% non-Python)**, contamination-free,
unsaturated, harness = **pier + mini-swe-agent**. (NOT SWE-Live-Lite — low mindshare; NOT saturated
Verified.) 113 images cached to GHCR.

Bring the **FULL OH-depth GT** to mini-swe-agent, **language-agnostic**, by **unifying onto the deep
`v1r/run_v74` engine** (where the levers live) — NOT the shallow `gt_intel`/`gt_hook` (`ast`, Python-only)
engine. The brief already reaches the mini-swe agent via `gt_agent._generate_brief` → `generate_v1r_brief`.

## Decision-grounding rule (this session)
Decisions are grounded on **research + code + performance** (measured, RED→GREEN), NOT on docs.
Docs (CLAUDE.md/gt_gt) are authoritative only when CURRENT — keep them updated; never let a stale doc
drive a decision. Example: drop `ast` because **70% of DeepSWE is non-Python → `ast.parse` emits empty
evidence on 70% of tasks** (performance), the tree-sitter graph already exists for all langs (code), and
multilingual graph/retrieval is SOTA (research) — proven by a per-language RED→GREEN test, not by the doc.

## Build order (each Stage-1 deterministic before any flip claim)
1. ✅ **CHANGE 1** — per-symbol MaxSim granularity (`33970b9f`; gold #1 3/7→7/7 @ e5/384).
2. ✅ **Fusion + dense floor** (`8a65b4ce`; W_SEM_FLOOR=0.25, base 0.40, query-adaptive; 15/15).
3. 🔄 **CHANGE 2** — open-source code embedder `gte-modernbert-base` (Apache-2.0), configurable, e5 fallback.
4. 🔄 **Unify ast-residue → cross-language evidence** (`gt_mini_patch` `gt_hook`/`ast` → v1r/graph SQL pillars).
5. ⏭ **DeepSWE substrate Docker image** — bake GT wheel + embedder + pyright/gopls/rust-analyzer/tsserver
   (so in-container v1r evidence + L6 reindex preserve LSP across all 5 langs).
6. ⏭ **Validate** — GT-off baseline + paired GT-on on DeepSWE across all 5 languages (Wilcoxon).

## Open / verify
- Step-0: confirm a real DeepSWE trajectory fires the `<gt-task-brief>` on a Go task (trial dispatched).
- CHANGE 2 live model-load pending `setup_models` fetch / image bake.
- Unify per-turn evidence needs the GT package in-container (substrate bake) or host-side.

## Key facts (from code, not docs)
- Two engines existed (ONE-PRODUCT violation): `v1r/run_v74` (deep, multilingual, tree-sitter+embedder)
  vs `gt_intel`/`gt_hook` (`ast`, Python-only). Unify onto v1r; retire `run_mini_gt_hooked.py` + the `ast` routes.
- L6 = REINDEXER (works, but strips LSP because pyright is absent from the task image → bake it).
- L4 = EVENT hook. `GRAPH_FAIL_MISSING_HANDOFF` = a cert false-fail. (See gt_gt §12.)
