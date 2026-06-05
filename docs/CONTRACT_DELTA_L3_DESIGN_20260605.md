# Contract-DELTA — design (fold "drift" into L3 Post-Edit, research-backed)

**Status:** design (supersedes the standalone `drift_hook`/`drift_cli`/`<gt-drift>` approach).
**Date:** 2026-06-05. **Author note:** this corrects an architectural mistake — I built drift as a
parallel layer; it belongs *inside* L3 Post-Edit, which already owns contract-on-edit.

---

## 1. Architecture understanding — where this comes from

GT is one topological pipeline (DOC_OF_HONOR):

- **Layer 0 (gt-index → graph.db)** is the *source of all signal*: 8 passes, 7 tables, **23 property
  kinds**, edges+trust, closure (transitive reach, depth 1-3), assertions, serde/structural twins,
  cochanges. Nothing downstream computes contract facts — they all just *read* graph.db.
- **Layer 2 (passive delivery)** consumes graph.db into agent observations. **L3 Post-Edit
  (`post_edit.py :: generate_improved_evidence`) is the home for contract evidence on every edit** —
  priority-ordered (DOC §2.2): `[BEHAVIORAL CONTRACT]` (guards/returns/raises/boundaries/side-effects)
  → caller code with `return_usage` tags → callees → signature/arity → peers → override chain →
  tests/completeness → fingerprint similarity → twins/co-change → mismatch. It has an **evidence
  budget** (`_MAX_EVIDENCE_CHARS=2000`), a **categorical edge filter** (`_categorical_edge_filter_clause`),
  a **G7 isolation gate** (`g7_filter_isolated`), **dedup** (`_normalize_contract_lines`), and a
  delivery accounting ledger.

**Motivation (Layer 6 research):** the 30-category failure taxonomy's core finding — *"LOCAL
CORRECTNESS WITHOUT GLOBAL AWARENESS: agents write locally correct code that breaks callers, contracts,
and cross-file invariants."* L3 already *shows* the contract to preserve; it does **not** tell the agent
when its edit **broke** one.

## 2. The capability gap (verified by reading post_edit.py)

- L3's improved path renders the **current** contract ("PRESERVE this") but runs **no before/after
  diff** (confirmed: the only diff is `ChangeAnalyzer` in the *legacy fallback*, AST-only, fires only
  when improved is empty).
- The **full pre-edit file is already available in `main()`** as `old_content_text` (sources in
  priority: `args.old_content` → `_reconstruct_old_content_from_diff` → `_git_show_head_file`,
  post_edit.py:4153-4165). **The "before" state is in hand — L3 just never diffs it.**

So the missing capability is one operation: **diff the edited function's contract old-vs-new, and name
the dependents at risk** — at the seam where L3 already builds the contract block.

## 3. Design — `[CONTRACT-DELTA]` inside L3

**Seam:** inside `generate_improved_evidence`, right after the `[BEHAVIORAL CONTRACT]` block is built
(post_edit.py ~2916). Add `"[CONTRACT-DELTA]"` to `_G7_PILLAR_KEEP_PREFIXES` (it's a Contract-pillar
marker — must survive the isolation gate). It accumulates into `func_parts` → flows through the existing
budget, dedup, G7, and emit — **no new layer, no new block type, no advisory prefix.**

**Correctness fix (the root of the false-positive bug): same-path before/after indexing.** The old
"drift" diffed *full-build* properties against *incremental-reindex* properties — two different indexer
paths → phantom diffs. Instead:
1. Write `old_content_text` to a temp file; index it **single-file** into a scratch db.
2. Index the current file the **same single-file way** into a scratch db (or reuse graph.db for the
   post state — but both sides must use the *same* extraction path).
3. Diff the two property sets for the **edited functions only** (scoped by the git-diff line ranges
   L3 already has via `diff_text`).

Same extraction path both sides ⇒ an unedited function cannot show phantom drift. Language-agnostic
(tree-sitter, all 23 kinds) — strictly more than `ChangeAnalyzer`'s Python-AST 5 kinds, and reusing
GT's own extractor rather than a second parser.

**Diff the full depth, not 4 fields.** For each edited function compare every contract-bearing kind:
`guard_clause, conditional_return, boundary_condition, exception_type, exception_flow,
exception_handler, return_shape, side_effect, field_read, resource_pattern, call_order`. Render only
**material** changes (added/removed/changed), correct-or-quiet.

**Attach the dependency consequence from the depth** (this is what makes it actionable, not a bare
diff). For each change, pull from graph.db (current):
- `caller_usage` — the *exact* way callers consume it ("6 callers `destructure_tuple` the return").
- verified callers via `_categorical_edge_filter_clause` (no `name_match` laundering; precondition:
  resolver stdlib-laundering test green).
- `closure` — transitive blast radius (depth 1-3).
- `structural_twin` / `serialization_pair` — "you changed `get_X`; its twin `set_X` is untouched."

Example rendered (survives G7, fits budget):
```
[CONTRACT-DELTA] get_user
  return shape: tuple(2) -> dict   (6 callers destructure it as a tuple; 14 funcs transitively reach it)
  dropped raise: KeyError          (2 callers catch it)
  twin not updated: set_user
```

## 4. Research backing (per design choice)

| Choice | Citation |
|---|---|
| Why surface contract *changes* at all | 30-category taxonomy: "local correctness without global awareness" (ENRICHED_HANDOFF, 9,942 cards). Smith, Barr, Le Goues, Brun — *"Is the Cure Worse Than the Disease? Overfitting in Automated Program Repair"*, ESEC/FSE 2015 (patches pass given tests yet introduce regressions). |
| Diff dependents must change together | CodePlan, FSE 2024 (co-edit propagation, 5/7 repos pass with propagation); HUNK4J, ASE 2025 (agents systematically under-edit multi-hunk). |
| Scope diff to edited functions | program-slicing minimal-context (ICSE 2024); OCD/SWEzze 2026 (8.4% of segments needed). |
| Caller-usage / call-graph consequence | PyCG, ICSE 2021 (99.2% MRO precision); RepoGraph, ICLR 2025 (1-hop ego-graph > 2-hop). |
| Fingerprint/twin signals | NiCad, ICPC 2011 (96% Type-3 clone recall); MSR serde-pair behavioral signal. |
| Render delta FIRST, compact | Lost in the Middle, NeurIPS/TACL 2024 (primacy); evidence budget math (DOC §6.3, ≤1% context). |
| Correct-or-quiet (no false delta) | The Distracting Effect, arXiv:2505.06914 (2025) — plausible-but-wrong context drops accuracy 6-11pp. |

## 5. What to retire
The standalone artifacts are architecturally misplaced and should be removed once the L3 delta lands:
`hooks/drift_hook.py`, `hooks/drift_cli.py`, the `<gt-drift>` block, the wrapper `graph.db.orig`
freeze, and the `_drift_adv` prefix wiring in `post_edit.py` + `mini_swe_agent.py`. The mini-swe pull
path becomes: L3 delta is computed server-side and delivered through the same `gt`/hook channel — same
payload, no separate freeze/reindex.

## 6. Honest ceiling (do not overclaim)
This detects **structural** contract changes (the kinds the indexer extracts). It does **not** see
**semantic/implicit** changes — e.g. loguru-1297's internal offset clamp that removes an implicit stdlib
`OverflowError`: the indexer captures it as *no* property change, so the delta is correctly empty but
gives no help. That is the post-localization-correctness ceiling, unchanged. The delta's value is
catching the *interface-break* class (return/raise/guard/twin), not the implicit-semantic class.

## 7. Verification plan
- Red→green unit test on `generate_improved_evidence`: an edit that changes a return shape emits
  `[CONTRACT-DELTA]`; an edit to an unedited-but-reparsed sibling function does NOT (same-path proof).
- Real-binary git-repo proof (already have the harness): only edited functions appear.
- Live re-run on a probe whose gold fix changes an **explicit** return/raise contract with real callers
  (loguru/beets were poor probes — issue pre-localized + non-contract/implicit fixes).
- Gate per the GT-LAYER VERIFICATION PROTOCOL (gt_gt.md): DELIVERED + CORRECT (delta matches git diff) +
  CONSUMED, on a fair probe — before any "works" claim.

---

## 8. Implementation status (2026-06-05)

**BUILT + WIRED (local, tested — not yet live-verified):**
- `src/groundtruth/hooks/contract_delta.py` — `compute_delta(graph_db, file_rel, repo_root, diff_text)`:
  recovers pre-edit content (git HEAD / diff), **same-path single-file index of old vs current**, diffs
  the full property depth (`_DELTA_KINDS`: return_shape, exception_type, guard_clause, boundary_condition,
  conditional_return, exception_handler, side_effect, resource_pattern, field_read, call_order), attaches
  verified caller count + `structural_twin`/serde "twin not updated". Correct-or-quiet; never raises.
- Wired into `post_edit.generate_improved_evidence` (once per file, `output_parts.insert(0, …)` → leads
  the block, primacy). `[CONTRACT-DELTA]` added to `_G7_PILLAR_KEEP_PREFIXES`.
- **Standalone drift retired on the OH path**: removed the `_drift_adv`/`drift_advisory` prefix wiring
  (post_edit). The graph-reindex drift was the false-positive source; the same-path L3 delta replaces it.
- Tests: `tests/unit/test_contract_delta.py` (real-binary) — **3 pass**, including the SAME-PATH proof
  (unedited `open_state` never appears when only `get_user` is edited). Post-edit regression (test-guard,
  categorical filter) green — **28 pass** total.

**FOLLOW-UP (not done):**
- Delete `hooks/drift_hook.py` + `hooks/drift_cli.py` + the wrapper `graph.db.orig` freeze; repoint the
  mini-swe `gt drift` pull path to `compute_delta` (currently gated OFF by `GT_DRIFT_ENABLED`, so inert).
- Live re-run on a fair probe (explicit return/raise contract change, baseline-failure task) under the
  verification protocol before any "works" claim.
