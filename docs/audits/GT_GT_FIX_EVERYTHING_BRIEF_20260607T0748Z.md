# GT_GT FIX-EVERYTHING BRIEF — 20260607T0748Z

## THE GOAL (the thing we are fixing)
Fix the **whole of gt_gt** — the context graph (substrate) **and** every layer — so it **genuinely
works**, and **PROVE each fix works with the gate-check**. Build → make it work → verify. **No fix is
"done" until the verification shows it.** Asserting it works, "code compiles", "tests pass", "audit
clean" — none count. Only a gate-check / agent-observation showing the gate flipped counts.

This is the lesson of the 2026-06-07 session in one line: **a layer over a dead substrate measures
nothing.** The deps step silently failed → embedder zero + LSP-client dead + name_match-garbage edges →
GT ran as a grep+graph baseline on every run, undetected, for ~14 runs. So: **substrate must be proven
ON before any layer is touched, and every fix is verified, never asserted.**

## THE STRUCTURE — prerequisites (substrate) → layers (consumers)
**PREREQUISITES (the substrate — fix + prove FIRST; everything reads them):**
- **P1 — EDGES / receiver-type resolution (LSP / JARVIS / CHA).** The call-graph accuracy.
- **P2 — graph.db DEPTH.** The substrate's correctness + richness (incl. the closure built over edges).
- **P3 — EMBEDDER.** Real ONNX semantic, non-zero, *consumed* (not provisioned-but-unconsumed).

**LAYERS (consume the substrate — only meaningful once P1–P3 are real):**
- **L1** brief / `v1r_brief` + localizer ranking · **L3** post-edit (contracts/drift) · **L3b** post-view
  (contract pillar + callers) · **consensus / scope** · **GT_VERIFY** · **drift / orientation**.

## THE METHODOLOGY (the whole goal — make it work + verify, for EVERY bucket)
For each fix, in order:
1. **LIPI** — diagnose across all 4 avenues (Logic / Implementation / Integration / Plumbing).
2. **FIX** — generalized (works on any repo / language / agent), research-backed (venue+year, measured),
   optimized (cheap-first, reuse existing, demand-driven at scale), ONE surface (no per-language tools).
3. **MAKE IT WORK** — build it (Go indexer / Python).
4. **VERIFY** — the cheap `gate_check.yml` (~5 min, NO agent, NO LLM):
   - **Prereqs (P1–P3):** the gt_trial §1.5 3-gate check — print `embedder=ON/OFF · resolution=ON/OFF
     (name_match X→Y, det N%) · graph=nodes/edges/det%`. The number must MOVE in the right direction.
   - **Layers (L1–L6):** the gt_trial §4 verifier-agent (DELIVERED + CORRECT + CONSUMED from
     `output.jsonl`, never telemetry).
   Build → verify. **A fix without its gate-check delta is NOT done.**

## THE BUCKETS (what's wrong / the fix / the proof)

### P1 — EDGES / receiver-type resolution (ONE surface, dispatched by typing discipline)
**Wrong:** 98% of `name_match` edges are method calls = name-guesses → ~58% of the context graph is
false → the agent flies blind, can't reach gold (measured conan-17123: 7308 name_match, `join`×1106).
**Fix (cost-ordered tiers, stop-at-first-hit, then shared CHA walk):**
- T1 declared-type (join `param`/`signature` types into `NodeMeta`) — reuse, O(1).
- T2 builtin-exclude (drop `str.join`/`dict.get`) — DONE for variable receivers (−1070 proven); extend
  to **literal receivers** via parser literal-typing (`","`→str) to catch `join`×1106.
- T3 assignment-flow → **JARVIS flow-sensitive** (per-function lattice; the one R&D piece).
- T4 demand-driven LSP scoping (`--source-files`), issue-scoped residual.
- **BUG:** `-file` incremental never wires `SetAssignmentIndex`/`SetInheritanceMap` → degraded resolver.
**Research:** PyCG ICSE'21 (99.2%/69.9%); JARVIS'23 (+84%/+20%/+67%); XTA OOPSLA'00 (+88% vs RTA); CHA
ECOOP'95; demand-driven Heintze-Tardieu PLDI'01.
**Proof:** gate-check `name_match X→Y`, det% up, top targets show `join/get` gone, gates intact.
**PROVEN 20260607 — T2 COMPLETE:** name_match 7351→4890 (−33.5%), `join`/`split`→0, det 63.7%, gates
intact (LSP 47→85, embedder ON). 3 increments (var Strat-2 −1070 / literal parser −219 / strong-builtin
1.9 −1172). Remaining residual = INTERNAL methods (`with_requires`/`with_settings`/`assert_listed_binary`)
→ need T1+T3 receiver-typing, NOT exclusion. **NEXT: T1 declared-types + T3 assignment-flow.**

### P2 — graph.db DEPTH (the traversal/reach that decides whether the map reaches gold)
**Current state (measured 2026-06-07):** the localizer (`v1r_brief.py:2017-2018` → `graph_reach.compute_reach`)
traverses BFS from trusted anchors with **TWO HARDCODED MAGIC NUMBERS — both violate the dynamic pillar:**
- `max_depth = 3` (`graph_reach.py:77`) — fixed hop cap.
- `min_confidence = EDGE_CONFIDENCE_FLOOR = 0.7` (`v1r_brief.py:49`) — fixed float gate on frontier edges.
Plus path-decay `1/(1+depth)` + hub-penalty (Lao & Cohen PRA 2010). The `3` and `0.7` were tuned on the
OLD substrate where ≥0.7 edges still included LAUNDERED `name_match` (verified_unique stdlib-shadow) →
"deeper-hop recovers 0" (ICEMAN) / "29× BFS explosion, gold flat" (G3) are **stale/garbage-confounded**;
after T2 the ≥fact edges are genuinely real, so depth/gate must be re-derived, not trusted.

**Fix (dynamic + hybrid + confidence-gated — NO `0.7`, NO `3`):**
- **Gate (per-edge frontier admission):** HYBRID composite (≥3 signals) = ① resolution truth (categorical
  fact-method vs `name_match`) · ② confidence · ③ edge-type weight · ④ semantic relevance to the issue ·
  ⑤ hub-penalty. DYNAMIC cutoff = the natural break in *this task's* admission-score distribution, not 0.7.
  Tiered (facts always; mid-tier iff semantically relevant; guesses dropped — correct-or-quiet).
- **Depth (per-path continuation):** EMERGENT — expand until a path's decayed contribution drops below a
  *per-task-relative* floor (fraction of this task's top reach) or the fact-frontier is exhausted; hard cap
  (~5) only as a backstop, never the policy. Deep on long real chains (Java), shallow on flat (Python) —
  one uniform mechanism, language-adaptive depth. (Demand-driven termination, Heintze-Tardieu PLDI'01.)
- Also: closure rebuilt over *resolved* edges; `properties` populated; `confidence`/`trust_tier` honest; FTS5.
**Proof = GOLD-REACH, not the substrate gate-check:** on clean graphs, does the dynamic gate+depth reach
gold more / rank it higher (`gold_file_reached`, `first_gold_rank`) WITHOUT exploding the candidate set?
Reach-gold up + candidate-set flat = serves the goal. Reach-gold flat = revert (motion, not progress).

**Source-residual investigation (conan-17123, 2026-06-07):** top `name_match` after T2 = `with_requires`/
`with_settings`/`with_requirement`/`assert_listed_binary` (685) are all `GenConanfile().with_…()` builder
chains + `client.assert_…` fixtures **in `test/` files** (GT filters tests; gold is in source → low value);
`loads`×188 = all `json.loads` (→ added to strong-builtin set); `run`×127 = MIXED internal (`command.run`/
`self._conanfile.run`, needs T1/T3 typing) + external (`docker…containers.run`). So the *source* map is
materially clean; the genuine remaining typing case is `run`.

### P3 — EMBEDDER
**Fix/verify:** real ONNX e5 loads, non-zero *separating* vectors, AND the brief/localizer actually
*consumes* the semantic signal (not provisioned-but-unconsumed). Deps installed fail-LOUD.
**Proof:** `gate_embedder()` PASS + the localizer's semantic signal nonzero on the gold candidates.

### L1–L6 — LAYERS (after P1–P3 proven)
Each: delivers correct context from the now-trustworthy substrate; verified from the agent's
observation (DELIVERED+CORRECT+CONSUMED), leak-free, correct-or-quiet.

## ORDER
**Substrate first, bottom-up:** P1 (edges) → P2 (graph.db depth) → P3 (embedder), each proven on the
gate-check. **Then** the layers (L1→L6), each verified from agent observation. Never a layer before its
substrate. One variable at a time, each with its gate-check delta.

## DEFINITION OF DONE (per bucket)
The gate-check (prereqs) or the verifier-agent (layers) shows the gate moved in the right direction, on
a real task, generalized — printed, stored, not asserted. That is the whole goal.
