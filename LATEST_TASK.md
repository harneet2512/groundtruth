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
## TWO PATHWAYS (FINAL, 2026-06-10): different benchmark x model x platform
### PATH A — GCP VM gt-sweep-1: **DeepSWE-113 with gemini-3-flash** (Vertex)
- Baseline: gemini-3-flash IS on the DeepSWE leaderboard (5%) -> direct GT lift measurement.
- Auth: VM identity/ADC (zero keys on the box). Bills to the $271 credits.
- NOW: the 113 proof sweep running (the 4 answers). THEN (each gated on user OK):
  enable Vertex API + widen VM scopes + pier install -> 5-task gemini trial -> full 113.
- Needs built: VM agent-runner (pier agent step port) + gemini model config (vertex_ai/gemini-3-flash).
### PATH B — GHA: **SWE-bench Verified-500 with deepseek-v4-flash**
- Baseline: the ~79% vendor-scaffold line (user decision). DEEPSEEK_API_KEY already a GHA secret.
- Needs built: the Verified GT adapter (mini-swe swebench path; the pier wrapper is DeepSWE-only)
  + a verified workflow (pull official task image -> substrate proof -> GT-injected mini-swe agent
  -> official Princeton harness eval) + the Verified manifest (builder in flight).
- Trial: 5 Verified tasks with v4-flash (cheap; cache-discounted) before any 500 run.
### Rules: per-launch permission ALWAYS, both paths. No secrets on the VM (deepseek key never
touches GCP; gemini uses ADC). gt_gt is the audit surface for both.

## LAUNCH PARAMETERS (locked 2026-06-10)
- PATH A (VM, DeepSWE x gemini-3-flash-preview): PARALLEL=4 (8 vCPU + compile/test bursts), step_limit=300 (official), cost_limit=3.0/task, STOP_AT_COST=25 trial / 200 full, temp=1.0, VERTEXAI_LOCATION=us-east1 (the southeast-US region; NOT global - shared-pool 429 risk; fallback us-east4/us-central1 if the model isn't served there - one env var, no code change), SA-key bind-mount (ADC blocked in-container). PARALLEL stays 4.
- PATH B (GHA, Verified x deepseek-v4-flash): max_parallel=20, step_limit=250 + cost_limit=3.0 (the EXACT official leaderboard config), temp=1.0/top_p=0.95/max=8192, num_retries=3.
- POST-RUN RULE: download EVERYTHING to local disk D (.claude/reports/runs/<run>/) — full trajectories,
  patches, gt_artifacts, deep metrics, rows, reports — from BOTH platforms, before the run is "done".
  VM: tarball OUT_DIR -> pull back. GHA: download all artifacts. Then ledgers + the submission bundle.
