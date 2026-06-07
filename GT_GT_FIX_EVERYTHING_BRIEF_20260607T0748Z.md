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

### P2 — graph.db DEPTH
**Fix/verify:** the closure/reachability is rebuilt over the *resolved* edges (garbage edges poison
traversal — `GT_INDEX_BIN`); `properties` (`param`/`data_flow`/`signature`/`caller_usage`/`assertions`)
populated + correct; edge `confidence`+`trust_tier` honest; FTS5 `nodes_fts` complete.
**Proof:** gate-check shows the population numbers + det-edge fraction up after P1.

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
