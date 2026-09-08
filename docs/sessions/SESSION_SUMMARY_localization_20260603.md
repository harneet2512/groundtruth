# Session Summary — Localization delivery fixes + semantic isolation

## Date / Branch / Commit
2026-06-03 · gt-consensus-curation · fixes at bf57918d (unification), HEAD 560983e9

## Objective
Make GT's *delivered* localization correct so the agent reaches the right code,
and answer (with measurement, not vibes) whether the semantic embedder earns its place.

## Implementation changes (all committed, on the run branch)
- **brief-decouple (cc2cbd11):** the 600-token evidence trim was popping `entries -> .files`,
  gutting the delivered candidate list to 1-2 files and dropping golds the ranker placed at
  #0-#5. Fix: snapshot the full rank-ordered list before the trim; `.files = list[:max_files]`;
  trim only the rendered evidence. **Canary-proven in-container** (see below).
- **URI fix both directions (ce31329c):** `file:////home` four-slash broke pyright `initialize`
  on POSIX -> 0 lsp edges on every Linux run. Fixed with `Path.as_uri()` forward + url2pathname
  reverse.
- **lsp_on telemetry (206d011c):** count real lsp edges, not db_lsp file existence.
- **unification (bf57918d):** ported the 3 localization files onto the consensus run branch
  (which keeps the OH wrapper + consensus gate). Run branch now has the full stack.

## Metrics
- **PolyLoc held-out (n=15), controlled A/B (same graphs, one-file diff):**
  delivered C@5 0.40 -> **0.60** (= grep parity), MRR 0.37 -> **0.48** (> grep 0.42),
  @1 0.33 (> grep 0.27), harmful regressions 2 -> **0** (gate passes). Delivered == bare localizer.
- **Live canary (GHA, beets-5495, GT v2_live, in-container):** RESOLVED. Agent edited the GOLD
  `beets/importer.py`. `importer.py in brief: True`. Ran semantic-OFF (no torch in container).
- **Semantic strong-ON (bge) vs OFF A/B (n=11), corrected counting:**
  Acc@1 ON **0.55** vs OFF 0.36 · Acc@5 0.64 vs 0.55 · MRR 0.62 vs 0.49.
  Semantic HELPS (2 helps, 1 hurt, 8 ties). **Earlier "semantic doesn't help" was a falsy-zero
  counting bug (rank 0 treated as worst) — withdrawn.** Caveat: semantic is OFF in the real
  benchmark (no torch) -> the benchmark currently loses this +0.18@1. The ONNX embedder
  (memory/enrich/embed.py, already present) is the in-container path if we want that gain.

## Graph depth — NOT used to full effort (verified)
Localizer uses edges + bounded BFS (max_hop=3) computed live. UNUSED in ranking:
`closure` (14,838-row precomputed multi-hop reachability — the KGCompass lever, the deep one),
`assertions`, and (until d1afb028) `cochanges`.

## Parallel branch d1afb028 (gt-mini-canary) — review
GOOD: data-sized breadth via Kneedle (Satopaa 2011, replaces magic RENDER_CAP); cochange wiring
(uses the dead cochanges table). MISSING: the brief-decouple fix (ABSENT -> would regress the
proven gutted-brief bug), the consensus gate, and the `closure` table (big lever). Unvalidated.
Directive: make gt-consensus-curation the BASE, port Kneedle+cochange onto it, wire closure,
fix the HIGH-header single-target collapse (dynamic set + abstain), then measure head-to-head.

## Result
Delivered localization beats grep (directional, gate-clean) AND is proven in-container by the
canary. Semantic is a real (small) positive but inert in the benchmark. Closure is the next lever.

## Next allowed action
Port d1afb028's breadth+cochange onto the proven base + wire `closure` into structural confidence,
then PolyLoc + 1 canary head-to-head before adoption.
