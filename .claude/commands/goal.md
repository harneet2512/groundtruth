# /goal — GroundTruth 5-language parity loop

THE GOAL (hold it fixed for the rest of the session): bring the **code (current state) to PARITY
with `gt_main` (`gt_gt.md` / `gt_new.md` — the DESIRED state)** so GroundTruth delivers correct
context **equally across all 5 languages — python, go, typescript, javascript, rust**. Generalized,
never benchmaxxing, never overfit to one language/repo/task.

**Lead EVERY turn with the marker** (this is the visible proof the goal is on):

```
🎯 /goal — parity(code ⇄ gt_main) × 5 langs | substrate: <built|consuming|none> | phase: <diagnose|fix|run|audit> | py:<verdict> go:<verdict> ts:<verdict> js:<verdict> rust:<verdict>
```

where each `<verdict>` is one of `·`(not run) `DEL`(delivered only) `gt_caused` `MISS`(no gt_caused) — filled from the §4 audit, never asserted.

## Non-negotiable gates

- **Before writing ANY code, READ `CLAUDE.md`** (`.claude/CLAUDE.md` + `D:/Groundtruth/CLAUDE.md`):
  the goal + the four pillars (generalized · research-backed · correct-or-quiet · dynamic/hybrid/
  confidence-gated) + the ONE PRODUCT rule. Every fix MUST generalize — **mutation-check on a
  held-out language (go AND rust), not just the one in front of you.** A fix that only helps the
  tested language is overfit = benchmaxxing = fail.
- **Desired = `gt_main`.** Where the code lags the desired behavior → fix the code. Where `gt_main`
  is stale-on-fact about the code → re-sync the doc. Never implement *to* a stale anchor; verify
  the desired behavior against the code first.
- **No claim without the observable.** "Green" / "done" / "works" are hypotheses until pinned to a
  run. LIPI every diff (4 avenues) before commit.

## The loop (worst-first × leverage)

1. **DIAGNOSE** the parity gaps (code vs `gt_main`) per surface, per language — worst-first, file:line.
2. **FIX** each gap: generalized → test-first with the mutation check (RED → GREEN → break-to-confirm-it-bites → restore) → LIPI → commit.
3. **ONE SUBSTRATE** (mandatory): a single substrate-consume harness — graph MOUNTED (never baked),
   `brief.txt` host-produced, `GT_HOST_GRAPH_DB`/`GT_CERT_DIR` handoff — that runs **identically for
   all 5 languages**. If you find yourself writing a per-language delivery path, you have fragmented;
   stop (ONE PRODUCT).
4. **SHOW IT LIVE**: a live codespace (or GHA — any github id authorized) run per language on that
   ONE substrate. The graph/depth/naming/LSP/embedder metrics print every run.
5. **AUDIT — EVERY run, in this order** (`gt_trial §4`, stored append-only at `task_ledgers/<task>.md`):
   - **5a. DEPTH-FIRST PER-LAYER BEHAVIORAL-GAP audit FIRST** — run `scripts/gt_layer_audit.py`
     (language-agnostic). **Start from DEPTH (Layer 0), walk every layer** (depth → naming → nodes →
     LSP → embedder → L1.brief → L3b.evidence → L3.contract → consensus.scope → cochange →
     oracle.nudge) and report, per layer, **did it FIRE this run + the live behavioral GAP**
     (intended-per-gt_main vs actual). This is the at-a-glance "which layer is dark on this language."
   - **5b. then the per-component proof** — read the trajectory CHRONOLOGICALLY (never grep). PREREQS
     table (P1 det_pct/name_match/typing · P2 calls/resolution · P3 embedder class/cos/w_sem) + ONE
     GT-SENT-vs-AGENT-DID table per gt_gt component (missing = `DELIVERED=NO — reason`), then the
     verdict: **`gt_caused = DELIVERED ∧ CORRECT(zero test-name/FAIL_TO_PASS leakage) ∧ CONSUMED ∧
     FAIR-PROBE ∧ RIGHT-TRAJECTORY`**. "Resolved" is a footnote to "the trajectory was right."
   - **5c. archive the substrate** (`graph.db` + certs + `brief.txt`) per task before the next run
     overwrites it — every run's §4 source must be preserved.
   - **A behavioral gap on ONE language is assumed present on the others** until a run proves
     otherwise (the audit is identical for all 5) → the fix is 5-language-generalized, never a
     per-language patch.

## Definition of done

Parity is proven ONLY when, on the ONE substrate, the live trajectory shows GT behaving as `gt_main`
specifies **for all 5 languages** — code parity (logical) AND working parity (the trajectory).
Green tests, clean compiles, or resolves alone NEVER count. The same substrate, all five, audited
the same way.
