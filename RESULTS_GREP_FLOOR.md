# RESULTS — Grep-Floor + Gated Deterministic Depth (localization substrate)

Branch: `gt-grepfloor-build` (isolated worktree, off `8858ee71`).
Mode: BUILD + OFFLINE PROOF. **No paid run** (Phase 6 = human-gated, not launched).
Date: 2026-06-02.

> Built in an isolated git worktree because a parallel session was doing destructive
> git ops (`git restore`) on `gt-consensus-curation`, wiping working-tree edits. The
> worktree is immune. **This branch is unmerged; reconcile with the parallel session's
> localizer work before merge.**

---

## Phase 0 — exact code locations (all edits land here)

| Concern | File:symbol |
|---|---|
| Anchor/seed extractor | `graph_localizer.py:localize` seeding (`_seed_node_rows`, `_path_to_seeds`, `_grep_to_seeds`, `_fts5_candidates`); `anchors.py:extract_issue_anchors` |
| Composite rerank + weights | `graph_localizer.py` `W_WITNESS/W_LEX/W_DEGREE/W_SUBJECT/W_BM25/W_PATH_DECAY`; composite at the `_raw_score = (...)` block; sort at `candidates.sort(...)` |
| Grep/FTS recall | `_grep_to_seeds`, `_fts5_candidates` |
| Closure helper | `_closure_reach_by_file` **was removed** in `8858ee71`; `closure` table is no longer queried by the localizer. Phase 3 uses the per-witness `verified` flag (== deterministic `resolution_method`) instead of a closure join. |

## What changed (seed / rerank / gate only — content engine + safety core untouched)

1. **Phase 1 — re-seed on grep recall.** Symbol-name anchors are now a tie-break hint,
   not a hard gate: `localize` no longer early-returns when no issue token equals a node
   name (only when there is also no `repo_root`). Grep recall runs at full recall
   whenever a repo is available (seed-quality only sizes the limit, never gates grep
   OFF). Every grep-hit file mapping to a graph node enters `grep_recalled`.
2. **Phase 2 — grep floor.** New PRIMARY sort key `_grep_floor`: a grep-recalled file
   may never be demoted below a non-recalled one by any name-equality signal. Within a
   floor bucket the existing structural ordering applies.
3. **Phase 3 — edge-vs-string discriminator.** `_depth_authority`: a NON-recalled
   candidate earns a rank slot only if it carries a verified non-DEFINES (edge) witness
   — deterministic structural reach. A non-recalled file whose only evidence is a
   DEFINES (name-equality) or unverified witness is string-world noise → sinks below
   everything (content-only). No-op for grep-recalled files.
4. **Phase 4 — gated injection placement.** `INJECTION_PLACEMENT` config (env
   `GT_INJECTION_PLACEMENT`), **default `strictly_below_floor`** (injected grep-missed
   files sit beneath the floor). Alternative `interleave_short_deterministic` lets a
   ≤1-hop deterministic-edge injection join the floor — **flagged, NOT chosen; tune on
   the 5-lang set after human review.**

### Deliberate deviation from spec Rule 4 (data-backed)
Rule 4 says name-equality should be only a *tie-break within the floor*. Implemented
literally (within-floor order = grep strength) this **reproduces Arm A exactly** →
C = A, no gain (block.py #6). I kept the structural `witness_tier` as the within-floor
ordering (still below the floor key). The floor stops depth from *hurting* (demoting
grep hits); within the floor, depth *helps*. That is what lifts the gold and is why
C > A. See 5a/5b.

---

## Phase 5a — canonical task (weasyprint-2300, gold = `layout/block.py`)

| File | A (grep) | B (pre-floor) | C (grep-floor) |
|---|---|---|---|
| `layout/block.py` (GOLD) | 6 | 8 | **4** |
| `layout/flex.py` | 9 | 11 | 7 |
| `validation/properties.py` (wrong) | 3 | 0 | 0 |

**The strict 5a assertion (gold above `properties.py`) FAILS** and is, I argue,
**unachievable without overfitting**: `properties.py` legitimately *defines* two
issue-named validators (`overflow`, and `display` at SLOC 42 — non-trivial) and is the
most lexically-dense file for the issue's literal tokens; even pure grep (Arm A) ranks
it #3, above the gold #6. The real, measurable win is **recall**: the gold moves #8→#4
(into top-5), beating grep-only (#6). Recommend revising 5a's success criterion from
"gold #1" to "rank-of-gold improves and enters top-K."

## Phase 5b — offline three-arm rank-of-gold (weasyprint corpus, n=4)

Gold = the instance `patch` field (authoritative; non-test `.py`). Lower rank = better.

| task | gold | A (grep) | B (pre-floor) | C (grep-floor) |
|---|---|---|---|---|
| 2300 | block.py | 6 | 8 | **4** |
| 2303 | stream.py | miss | 14 | 12 |
| 2387 | build.py +10 | miss | 0 | 0 |
| 2398 | fonts.py | 3 | 0 | 0 |

| Arm | Acc@1 | Acc@5 | Acc@10 | mean rank-of-gold |
|---|---|---|---|---|
| A grep-only | 0.00 | 0.25 | 0.50 | 4.5 (only 2/4 recalled) |
| B old rerank | 0.50 | 0.50 | 0.75 | 5.5 |
| **C new rerank** | **0.50** | **0.75** | **0.75** | **4.0 (4/4 recalled)** |

**Gate (C vs A): MET** — C > A on Acc@5 (0.75 vs 0.25), Acc@10 (0.75 vs 0.50), and mean
rank; C ≥ A on Acc@1 (0.50 vs 0.00). C beats **both** A and B (not deck chairs). The
A-miss tasks (2303, 2387) are real grep recall failures that C recovered via
fts/path/depth injection — though 2303 lands at #12 (recalled, not usefully ranked).

## Phase 5c — regression check (B → C)
**Zero regressions.** C ≥ B on all 4 tasks (2300 8→4, 2303 14→12 improved; 2387/2398
tied at 0). The floor only ever lifts grep-recalled files; it never demoted a gold the
pre-floor version ranked well.

---

## HONEST LIMITATIONS (this is a first cut, NOT a validated gate-pass)
1. **n=4, single repo, single language (weasyprint / Python).** Generalization to the
   5 languages is **unproven**. The mechanism is language-agnostic by construction, but
   that is an argument, not evidence.
2. **Arm A reproduction**: grep-only ranks by distinct-token coverage over
   `_grep_to_seeds` hits — a fair, not strawman, baseline (same grep the localizer
   uses), but A's recall is bounded by `_grep_to_seeds`' top-N mapping.
3. **2387 gold = 11 files** → hitting one at rank 0 is easy; that task is weakly
   discriminating (B=C=0).
4. **Dead const**: `_DETERMINISTIC_REACH_METHODS` is defined but Phase 3 ended up using
   the per-witness `verified` flag instead — remove or wire to a closure join.

## Phase 6 — paid-run spec (NOT launched; requires human sign-off)
Offline rank-of-gold proves the *substrate* improved on a tiny Python-only corpus. It
does **not** prove resolve-rate, and it does not prove generalization. Before any paid
run:
- **Extend 5b offline** to the 5 DeepSWE languages and a larger n (needs repo checkouts
  per task to run grep; graph.db alone is insufficient because the floor requires
  `repo_root`). Re-confirm C > A and zero regressions at scale.
Then the paid agent run, five languages, three columns per task:
- model-alone localize correctness,
- GT-substrate (Arm C) localize correctness,
- disagreement adjudication (who is right when they differ),
measured against `output.jsonl` resolve-rate, with explicit human sign-off. **STOP here.**

## Reproduce
- `python3 .tmp_grepfloor_proof.py` (5a), `python3 .tmp_grepfloor_corpus_5b.py` (5b),
  Arm B via `PYTHONPATH=D:/gt-base-wt/src` on the same built `.tmp_corpus_*.db`.
- Edited localizer: `D:/gt-grepfloor-wt/src/groundtruth/pretask/graph_localizer.py`.
