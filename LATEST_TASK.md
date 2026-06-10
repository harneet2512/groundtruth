# LATEST TASK — Unleash full-power GT on DeepSWE (mini-swe-agent), multilingual

**Status:** IN PROGRESS — stack HARDENED (4 LIPI fixer surfaces) + substrate rebuilt; smoke
wave EXECUTED 5/5; 113 sweep running.
**Branch:** `gt-trial` @ `4253da65` (pushed to origin/harneet2512 + hbali-stack).
**Last updated:** 2026-06-09 (evening)
**Canonical detail:** `gt_gt.md` §11 (findings), §12 (per-layer roles), §13 (pivot + build
order, incl. **§13.7 — the 2026-06-09 hardening**); `SESSION_SUMMARY.md`.

---

## The current goal
Validation surface = **Datacurve DeepSWE** (`github.com/datacurve-ai/deep-swe`): 113 tasks, 91 repos,
**5 languages (TS 35 / Go 34 / Py 34 / Rust 5 / JS 5 — 70% non-Python)**, contamination-free,
unsaturated, harness = **pier + mini-swe-agent**. 113 images cached to GHCR.

Bring the **FULL OH-depth GT** to mini-swe-agent, **language-agnostic**, unified on the deep
`v1r/run_v74` engine. The brief reaches the mini-swe agent via `gt_agent._generate_brief` →
`generate_v1r_brief`; the proof path is fail-closed end-to-end (gt_gt §7 + §13.7).

## Current state (2026-06-09, from code + runs — not labels)
- **4-reviewer LIPI audit → 62 findings → 4 fixer surfaces shipped** (each red→green):
  `9bf106ca` pipeline+gates · `dc5844f8` localization · `ffc6c7dc` delivery · `10368a2f`
  indexer (+ `8ae5584d` gopls launch). Detail: gt_gt §13.7.
- **P0 green-zero-run chain fail-closed** (empty-issue / pier swallow / tee swallow /
  presence-grep — all four links verified).
- **Substrate REBUILT on the fixed stack** (`02b02425` — the image bakes the GT code, so
  pre-rebuild runs exercised PRE-fix code; never cite them as the fixed stack).
- **Wave: smoke EXECUTED 5/5** (`4253da65`, run 27249519490) — identical gt-run-proof command,
  exit 0 per language, warm LSP. All NO_OP_VALID_WITH_WARM_SERVER (fixtures resolve
  structurally on the fixed indexer); **real-repo ACTIVE LSP resolution = the 113-sweep's
  question.**

## Remaining waves (in order — each gates the next)
1. 🔄 **113-task sweep** (non-paid substrate proof, dispatched `0e2489cc`) — per-language
   certs + ACTIVE LSP on real repos.
2. ⏭ **Integration audit** — gt_trial §4 style, agent-observation rule, on the sweep output.
3. ⏭ **1-task dry** (D2) — single paid trajectory on the fixed stack.
4. ⏭ **Decision** — paired GT-on vs GT-off benchmark (Wilcoxon) across all 5 languages;
   Stage-1 deterministic per lever before any flip claim.

## Key facts (from code, not docs)
- Two engines existed (ONE-PRODUCT violation): unify onto `v1r/run_v74`; retire the
  `gt_intel`/`gt_hook` `ast` routes (70% of DeepSWE is non-Python).
- **L6 in substrate/proof mode is OFF BY DESIGN** (authoritative ro graph, witness-hash
  parity); the `-file` restore LSP-strip is fixed in the indexer (`10368a2f`) for the paths
  where L6 runs. L4 = EVENT hook. `GRAPH_FAIL_MISSING_HANDOFF` = cert false-fail. (gt_gt §12.)
- Per-turn evidence reads the ONE mounted graph `mode=ro(+immutable)`, one-time readability
  probe, per-(kind,file)-once dedup (gt_gt §6 note).
- Weights live: `W_SEM=0.40` + `W_SEM_FLOOR=0.25` enforced last; Dimension-0 query-adaptive,
  Dim-1 max-compose (gt_gt §4.2). Embedder: gte-modernbert default, configured-or-raise under
  `GT_REQUIRE_EMBEDDER` (ST + e5 fallbacks skipped); OH pins e5.

---
## TWO PATHWAYS (2026-06-10, written to avoid confusion — per-launch permission ALWAYS)

### PATH A — GCP (VM gt-sweep-1, 8vCPU, us-central1-a) = SUBSTRATE/PROOF work, $0 LLM
- NOW: DeepSWE-113 proof sweep RUNNING (all-fixes digest 6428af2a) → the 4 answers
  (29 SIGKILLs survive? · scc/jdtls? · dynamodb gate-1 flip? · per-language table).
- NEXT (prep only): Verified-500 manifest builder → Verified proof sweep on the VM (awaits launch OK).
- Pro-731: DEFERRED.
- Rules: no secrets/tokens/project-IDs on the VM or in artifacts; images pull anonymously.

### PATH B — GHA (hbali-stack) = AGENT/LLM surface + CI
- ARMED, awaiting explicit "launch": the 5-task probe — deepswe_full × 5 dispatches
  (language ∈ {go,python,typescript,rust,javascript}, max_tasks=1, deepseek-v4-flash, ~$5-10, ~1hr).
  Success = the integration audit's 12 runtime criteria + real $/task from the 8-dp logs.
- CI substrate rebuilds + image pins live here (digest pinned: 6428af2a).
- THEN (gated on probe green): the full DeepSWE-113 GT-on run (~$160 anchored est., ~3.5-4h).

### Cross-path rules
- Sequence: VM sweep 4-answers → user reviews → "launch" the 5-task probe → probe verdict → full-run decision.
- NOTHING launches on either path without explicit per-launch permission.
