# PROMPT — GT Total Output Correctness Audit ("bug in everything GT sends")

> Endgame audit. NOT just localization. Every claim GT emits to the agent is a
> separate factual assertion that is true or false against the repo / graph.db /
> git. This prompt verifies EACH claim type against ground truth, per task,
> across a corpus, and produces a per-claim-type bug ledger.

## Mission
For every artifact GT delivers to the agent — L1 brief, L3b post-view, L3 post-edit,
L6 reindex — decompose it into TYPED CLAIMS and prove each claim true or false
against a deterministic ground-truth source. Output: which claim types GT gets
wrong, how often, and whether each wrong claim **misdirects the agent (HARMFUL)**
or merely **clutters (NOISE)**. Correct-or-quiet: a HARMFUL claim is worse than
silence.

## Definition of "correct" (binary, deterministic — no model-behavior excuses)
A claim is CORRECT iff its assertion matches the ground-truth source. "GT emitted
it" ≠ "GT is right." A non-empty, well-formatted, confidently-worded WRONG claim is
a FAIL and a logged bug, not a pass. Localization correctness is ONE row in this
table, not the whole table.

Classify every claim into exactly one of:
- **CORRECT** — matches ground truth.
- **NOISE** — true-but-useless, or a hygiene defect (leak/dup/empty) that does not
  point the agent anywhere wrong.
- **HARMFUL** — asserts something false that, if believed, sends the agent to the
  wrong file / wrong edit / wrong contract (active misdirection). This is the
  category that kills flips.

## The GT emission inventory — verify EACH (grounded in a real cfn-lint-3798 brief)
For each task, generate the full GT output through the REAL delivery path, capture
the DELIVERED agent-facing string (post wrapper render+strip; never hand-sanitize),
then check every claim below.

### A. LOCALIZATION / RANKING  (L1 brief candidates + graph-map + "highest-confidence" line)
| Claim | Ground truth | PASS iff | Bug class if FAIL |
|---|---|---|---|
| Candidate file list (ranked) | `gold_files` | ≥1 gold ∈ top-k (report k=1,3,5 + first_gold_rank) | mislocalization (HARMFUL) |
| "Highest-confidence candidate: X" line | gold_files | X is a gold file | confident-wrong (HARMFUL) |
| graph-map `::symbol` is the edit symbol | the symbol the fix actually touches | symbol on/near a gold hunk | wrong-anchor (HARMFUL) |
| Mislocalization mechanism (for every miss) | repo source + graph.db | classify: **M1** lexical-string-hit (issue tokens appear as string literals/comments in the ranked file, not the fix site) · **M2** hub/homonym (ranked a high-in-degree hub or homonym symbol like `value`/`items`/`run`) · **M3** gold-disconnected (gold IS in graph.db but no edge path to issue anchors) · **M4** gold-unindexed (gold ABSENT from nodes — verify by querying nodes for the gold basename) | — |

### B. EDGE / GRAPH claims  (Witness · Callers · Calls · graph-map calls/called-by · EDIT-TARGET CONTRACTS `[file:line]`)
| Claim | Ground truth | PASS iff | Bug class if FAIL |
|---|---|---|---|
| "Witness: A called by B [CALLS]" | repo source at the call site | B actually calls A; edge is import/same_file verified (NOT name_match) | name_match-laundered-as-fact (HARMFUL) |
| Caller list `file:line (unverified)` | source at `file:line` | the call exists there; "(unverified)" honestly marks name_match | phantom-edge / mislabeled-trust (HARMFUL if unmarked) |
| "Calls: f1, f2, f3" (downstream) | edges table + source | each callee edge real | phantom-callee (NOISE→HARMFUL) |
| EDIT-TARGET CONTRACTS `sym -> calls T [path:LINE]` | source at `path:LINE` | LINE contains that call to T | wrong-line / wrong-symbol (HARMFUL) |
| graph-map `calls:` / `called by:` | edges table | each edge real + trust ≥ verified for "fact" framing | phantom-edge (HARMFUL) |

### C. CONTRACT / SEMANTIC claims  (brief Contract line · Spec/handles · L3b [CONTRACT] · L3 [SIGNATURE]/[BEHAVIORAL CONTRACT])
| Claim | Ground truth | PASS iff | Bug class if FAIL |
|---|---|---|---|
| `Contract: raises X \| return: Y \| returns Z` | the function body | it actually raises X / returns Z; Y is a real return contract not a random body expr | semantic-nonsense-contract (B3) (HARMFUL) |
| `[SIGNATURE] def f(...) -> T` | the def line | byte-matches the real signature incl. return type | wrong-signature (HARMFUL) |
| `[BEHAVIORAL CONTRACT] (full body — N lines)` | the body | real guards/returns present, not a placeholder (`body_len=`) | empty/placeholder-contract (B3b) (NOISE) |
| `[CONTRACT] def value(` ×3 near-dups | nodes table | distinct, non-homonym, non-duplicate | homonym-pollution / dup-contract (NOISE→HARMFUL) |
| `Spec: handles: except E:` | the body | the function actually catches E | false-spec (HARMFUL) |
| return_type shown | nodes.return_type / source | matches | wrong-return-type (HARMFUL) |

### D. TEST claims  (brief Tests line · "covering test" · L3b "Called by … [test] (Nx)")
| Claim | Ground truth | PASS iff | Bug class if FAIL |
|---|---|---|---|
| "Tests: t1, t2" | test files | each test references the symbol/file under edit | irrelevant-test (NOISE→HARMFUL) |
| "covering test: T" | T's body | T exercises the edit target | false-coverage (HARMFUL) |
| "Called by … (25x) [test]" count | edges table | the count is real and the caller is a test | inflated/wrong-count (NOISE) |

### E. PROVENANCE claims  (brief "Last: <sha> <msg>" · co-change pairs)
| Claim | Ground truth | PASS iff | Bug class if FAIL |
|---|---|---|---|
| "Last: 41061fd43 <msg>" | `git log -1 <file>` at parent_commit | sha+msg = the real last commit touching the file | wrong-provenance (NOISE) |
| co-change "pairs=N" | git history | pairs are real co-changed files | false-cochange (HARMFUL if it nudges) |

### F. RENDER / DELIVERY hygiene  (check on the RAW delivered string — NEVER a hand-sanitized copy)
| Claim | Ground truth | PASS iff | Bug class if FAIL |
|---|---|---|---|
| No `[GT_META]` / `[GT_STATUS]` / `__GT_STRUCTURED__` in agent-facing content | the delivered observation text | zero diagnostic lines reach the agent | meta-leak (NOISE, but pollutes context) |
| `<gt-evidence>` well-formed | the delivered text | exactly one open + one close, no nesting | double-wrap (NOISE) |
| No empty tags `<gt-evidence … />` | delivered text | every tag has content | empty-injection (NOISE) |
| No dead markers (v22 / legacy) | delivered text | none present | dead-marker (NOISE) |
| No truncated/glued markers (`[CALL…`, `…text wit# SPDX`) | delivered text | markers intact, no glue across blocks | marker-cut / glue (HARMFUL — breaks parsing) |
| No cross-turn duplicate blocks | full trajectory (multi-turn) | each evidence block delivered once | cross-turn-dup (NOISE) — *trajectory-only, mark separately* |

### G. REINDEX integrity  (L6 incremental reindex log)
| Claim | Ground truth | PASS iff | Bug class if FAIL |
|---|---|---|---|
| `incoming_restored=R / incoming_unresolved=0` | pre-edit incoming edge count for the node | R == pre-edit incoming count (no silent drops); report the denominator | silent-edge-drop (HARMFUL — graph degrades after edit) |
| `nodes_replaced / edges_replaced` | source delta | plausible vs the actual edit | reindex-miscount (NOISE) |

## Corpus  (default: holdout_v1 — policy-primary, multi-language, gold pre-extracted)
`holdout_v1.jsonl` (60 tasks). Per row: `bug_id`, `repo`, `repo_path`, `parent_commit`,
`gold_files` (use directly — do NOT re-parse a patch), `issue_title`, `issue_body`,
`graph_db_path`, `language`. Multi-language (row 1 = tokio-rs/axum = Rust) → tests
generalization, NOT just Python/cfn-lint. Avoids the SWE-bench data-policy tension.
Alt (Python/Live frozen-30): `scripts/analysis/measure_v1r_localization.py` TASKS — only if asked.

## Procedure (per task)
1. **graph.db**: if `row['graph_db_path']` exists, use it; else checkout `row['repo_path']`
   @ `row['parent_commit']` and `gt-index -root=<repo_path> -output=<db>`; cache by (repo, commit).
2. **issue** = `issue_title + "\n\n" + issue_body`.
3. **Generate GT's full delivered output** through the real code paths:
   - L1: `generate_v1r_brief(issue_text, repo_root=repo_path, graph_db=db, bug_id=bug_id)` → brief text + `.files`.
   - L3b: `groundtruth.hooks.post_view.graph_navigation(<a-candidate-file>, db)` → the DELIVERED string.
   - L3: `groundtruth.hooks.post_edit` evidence for a representative function in the candidate file.
   - L6: run the incremental reindex on a representative edit; capture its log.
   - Pass each through the wrapper's render/strip step (the same one `oh_gt_full_wrapper.py`
     applies before `append_observation`) so you check the DELIVERED text, not the raw producer text.
4. **Decompose** the delivered text into typed claims (A–G) and verify each against its
   ground-truth source. Record CORRECT / NOISE / HARMFUL + the bug class.

## Discipline (CLAUDE.md — non-negotiable)
- `gold_files` is ground truth for SCORING ONLY. Never feed gold back into the brief (benchmaxxing).
- NEVER write "model failure" / "stochastic." Every claim is deterministic; a wrong claim is a GT defect — trace which fact GT asserted and why it's false.
- Verify on the DELIVERED agent-facing string. Do NOT hand-sanitize then check the sanitized copy (that washes the evidence). If a render step strips `[GT_META]`, run the real strip and check its OUTPUT; if it doesn't strip, that's the leak bug.
- "Emitted" ≠ "delivered" ≠ "correct." Report all three separately.
- M4 / phantom-edge / silent-drop claims must be verified by querying graph.db + reading source at the cited line — not assumed.

## Output → timestamped `.tmp_gt_correctness_audit_<YYYYMMDDTHHMMZ>.md`
1. **Per-claim-type correctness matrix** (the headline): for each claim type A–G →
   `emitted | CORRECT | NOISE | HARMFUL | %correct`.
2. **Localization sub-table**: hit@1/3/5, first_gold_rank, mechanism histogram (M1–M4), per-language.
3. **Harm histogram**: total HARMFUL claims by type → the claim types where GT actively misdirects (the flip-killers, fix these first).
4. **Bug ledger**: every HARMFUL + NOISE claim with the exact quoted GT text, the
   ground-truth it contradicts, the task, and the bug class.
5. **Dominant bug**: the single claim type with the most HARMFUL instances + its mechanism + the generalized, research-backed fix direction (e.g. if A/M1 dominates → issue-artifact anchoring: extract error codes / quoted messages / symbols / traceback frames, resolve each to its DEFINITION site via graph.db, rank above lexical-occurrence + hub density — precedent: Agentless, LocAgent, OrcaLoca).

## Stop
Measure only. Do NOT modify any producer/renderer this run. This audit is the
spec for the fix; it is not the fix. After the report, propose the fix per
dominant bug and STOP for approval.
