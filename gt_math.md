# gt_math — the living parity matrix + functional-bug ledger

> Updated every loop fire (8 min). Structure = `gt_audit` components (rows) × 5 languages.
> Bug surface = `.claude/reports/GT_FUNCTIONAL_CODE_REVIEW_20260615T1900Z.md` (the REAL ~45
> functional bugs found despite clean *structural* parity — "architecture built ≠ code functioning").
> Flow = `gt_gt` §2→§3→§5→§4→§6→§7 (bottom-up, ONE cumulative substrate). Judge each cell by its
> §12 role, never a generic template. Method: fable-mode diagnoses/documents, fable-code fixes.
> Last update: 2026-06-17 (verification sweep — the review was already largely closed by `d5b1e59a`).

---

## ⭐ LOAD-BEARING FINDING — the review is ALREADY FIXED (commit `d5b1e59a`)

The `GT_FUNCTIONAL_CODE_REVIEW_20260615T1900Z` was **acted on**: commit **`d5b1e59a` "fix(pipeline):
close ~40 functional-review bugs across all live surfaces"** touched EVERY file the fleet targets —
gt_mini_patch.py (+275, +`test_scope_steer_failclosed.py` 421L), v1r_brief.py (+218, +test_v1r_brief.py
265L), v7_4_brief.py (+140), resolve.py (+244), lsp/client.py (+51), graph_localizer.py (+108),
anchor_proximity/graph_reach/hub_penalty, embed.py (+101), anchor_select.py (+20),
deepswe_full.yml (+35), foundational_gates.py (+36), mcp/tools.py (+36), path_policy.py (+25),
post_view.py (+646) — plus ~10 new test files. **So this is a VERIFICATION sweep, not a fix sweep:**
confirm each of the ~40 fixes is real AND its guarding test actually BITES (a fix behind a non-biting
test silently regresses — the real risk on an already-closed bug), and find the ≤5 residual `d5b1e59a`
didn't fully close. **F1 already verified its 3 Go P0s CLOSED with mutation-checked biting tests.**

**Harness note (LIPI integration):** worktree `fresh` baseRef branched every agent off `origin/master`
(c4d531f7, the stale 198-line pre-campaign tree). Diligent agents (F1/F3/F5/F6) reset to gt-trial HEAD
`b3398595`; F2/F4 did not → invalid → re-dispatched with an explicit base-reset-to-b3398595 first step.

---

## WHERE WE ARE (TRAIN → VALIDATE → TEST, ONE substrate)

- **Split (frozen, `.claude/reports/PARITY_15TASK_SET_FROZEN.md`):**
  - **TRAIN (develop+fix):** expr(go) · testem(js) · mashumaro(py) · superjson(ts) · pest(rust)
  - **VALIDATE (generalize-check):** scc(go) · katex(js) · textual(py) · sql-formatter(ts) · wasmi(rust)
  - **TEST (HELD OUT — final proof only):** go-critic(go) · yjs(js) · python-statemachine(py) · drizzle(ts) · boa(rust)
- **Stage:** Stage-1 parity (deterministic, `+`=`+` across 5 langs). NO flips until the matrix is green.
- **This phase:** killing the functional-bug surface on TRAIN-5, then VALIDATE-5, then TEST-5.
  Fixed-this-session: embedder-gate false-fail (cert-reconcile `cbce3687`), tsserver readiness budget
  (`e46acccd`), depth return_type cap removed (`b3398595`). Verified-closed on HEAD: BUG-A (test paths
  in scope/cochange), BUG-B (L5b wrong target), BUG-C (L3b examples witness).

---

## THE MATRIX (gt_audit structure × 5 languages)

Legend: ✅ green (observed at full bar) · ⏳ fixing (fleet dispatched) · ⚠️ partial/ceiling · N/A by role.

| gt_audit component (row) | go | py | ts | js | rust | open bug(s) blocking green |
|---|---|---|---|---|---|---|
| **§2 graph.db** build/resolve/trust | ✅ | ✅ | ✅ | ✅ | ✅ | **F1-verified CLOSED** (d5b1e59a): PRECEDES fail-closed · incremental qname-demote · closure best-conf — mutation-checked biting tests |
| **§2.6 depth** (cols+22 props) | ✅ | ✅ | ✅ | ✅ | ✅ | cap removed; type knowledge in return_type∪signature (drizzle 60→87, adaptix 71→100) |
| **§2 naming / name_match-trust** | ⏳ | ⏳ | ⏳ | ⏳ | ⏳ | **name_match-gate class** (the dominant defect, ~6 sites) |
| **§3 LSP** enrichment | ✅ | ✅ | ✅ | N/A | ✅ | **F5-verified CLOSED**: jdtls 180s budget · regex call-column finder · latency-free warmth — mutation-checked |
| **§5 embedder** semantic | ✅ | ✅ | ✅ | ✅ | ✅ | **F7**: 128-tok cap + e5-cache CLOSED; passage-budget priority-order residual **FIXED+MERGED** (0247e641) |
| **§4 L1 localization** | ✅ | ✅ | ✅ | ✅ | ✅ | **F3**: path-key charset-strip **FIXED** (472df22c); RRF tie-break + generated-demote CLOSED, mut-checked. (BUG-D additive = P2 research ceiling, not a spec-FAIL — greenfield recall wired) |
| **§5 brief** (4 pillars) / L3 contract | ✅ | ✅ | ✅ | ✅ | ✅ | **F4′-verified CLOSED**: _edge_conf deterministic-gate · HIGH-loc exact-path-first · _top_fn anchor-first — mutation-checked |
| **L3b post_view** witness | ✅ | ✅ | ✅ | ✅ | ✅ | BUG-C closed (DeepSWE [WITNESS] filters examples) |
| **L4** event hook | N/A on DeepSWE (MCP event absent) — LIVE for Cursor/Codex (P1 name_match launder there) |
| **consensus scope · cochange** | ⏳ | ⏳ | ⏳ | ⏳ | ⏳ | P0 _query_scope name_match admit · P0 "X of N" grab-bag (BUG-A test-path half CLOSED) |
| **L5 nudge · L5b verify** | ✅ | ✅ | ✅ | ✅ | ✅ | BUG-B closed (L5b scoped to edited∩obligations); P1 L5 phase-filter latch (next wave) |
| **L6** reindex | N/A (DeepSWE substrate gated-off, by design) ×5 |
| **§7 gates / certs** · wiring | ✅ | ✅ | ✅ | ✅ | ✅ | **F6**: GT_VERIFY forwarding + 3 telemetry sinks CLOSED (single-sourced); gate_lsp witness-reconcile **FIXED+MERGED** (d1ff9744) |

---

## THE BUG LEDGER (45 from the functional review — worst-first, with current status)

Status: `OPEN` (fleet assigned) · `FIXED` (this session, committed) · `CLOSED` (verified on HEAD) ·
`PARTIAL` (recall wired / ceiling) · `N/A` (dead path on DeepSWE).

### §1 graph.db (Go indexer) — fleet **F1**
| sev | bug | site | status |
|---|---|---|---|
| P0 | PRECEDES fabricates ordering edges (type-blind global first-match) | `promote.go:789` | OPEN |
| P0 | incremental restore launders CERTIFIED tier onto renamed target | `incremental.go:208` | OPEN |
| P1 | closure dedup keeps first-seen not best confidence (`bestEdgeConf` dead) | `closure.go:136` | OPEN |
| P1 | Go factory-return false IMPLEMENTS · JSX COMPOSES nondeterministic · Rust generic-impl mis-parse | parser/resolver | OPEN |

### §2 embedder — fleet **F7** (next wave)
| sev | bug | site | status |
|---|---|---|---|
| P1 | 128-token cap truncates issue query (gte=8192); cos-to-gold 0.866→0.617 | embed.py | OPEN |
| P1 | e5 single-passage batch gets `query:` prefix, poisons shared cache (e5 fallback only) | embed.py | OPEN |
| P1 | passage budget skips by `SELECT DISTINCT file_path` DB-row order, not relevance | `anchor_select.py:393` | OPEN |

### §3 LSP — fleet **F5**
| sev | bug | site | status |
|---|---|---|---|
| P0 | jdtls (java) converts 0 every run (readiness budget lists only rust-analyzer) | resolve.py | OPEN (tsserver twin FIXED `e46acccd`) |
| P1 | gopls/tsserver convert 0 on cold cache, gate false-greens | resolve.py / gate | PARTIAL |
| P1 | column finder `str.find(name)` → wrong call site for method majority | resolve.py | OPEN |
| P1 | `lsp_warm` gated on latency>0 fails a genuinely-instant warm server | resolve.py | OPEN |

### §4 localization — fleet **F3**
| sev | bug | site | status |
|---|---|---|---|
| P0 | candidate path-key mismatch (normalized vs raw) splits gold into two half-scored entries | graph_localizer.py | OPEN |
| P0 | RRF tie-break degeneracy → alphabetical path order ranks when embedder off | graph_localizer.py | OPEN |
| P0 | generated-file demote uses unanchored substring → real file under `generated/` buried | graph_localizer.py | OPEN |
| P1 | 0.7 confidence floor drops all name_match from hub/reach/proximity | graph_localizer.py | OPEN |
| P2 | L1 additive over-anchors on existing symbols (greenfield recall wired, no suppression) | graph_localizer.py:2041 | PARTIAL (ceiling; prior fix = proven no-op) |

### §5 brief — fleet **F4**
| sev | bug | site | status |
|---|---|---|---|
| P0 | `_edge_conf_clause` returns "" (no gate) on no-confidence DB → renders name_match as fact | v1r/brief | OPEN |
| P0 | HIGH-localization guard reads guard/return from wrong file's same-named fn (`LIKE … LIMIT 1`) | v1r/brief | OPEN |
| P1 | `_top_functions` ref-count-ranked → freshly-added 0-caller gold fn falls past cap | v1r/brief | OPEN |

### §6 layers (L3/L3b/L4/L5/L6)
| sev | bug | site | status |
|---|---|---|---|
| — | live DeepSWE producers (gt_mini_patch.py) fact-gated, stderr-clean, non-corrupting | gt_mini_patch.py | CLOSED (verified) |
| — | OH hooks (post_view/post_edit/tools.py) dead on DeepSWE | hooks/ | N/A |
| P1 | L4 MCP handle_trace/handle_impact launder name_match (live for Cursor) | mcp/tools.py | OPEN (next wave) |
| P1 | L5 verify/failure/no-test steers phase-filtered out at test-result turn + latch-burned | gt_mini_patch.py | OPEN (next wave) |
| P1 | BUG-C examples witness (L3b) | gt_mini_patch.py:1853/1896 | CLOSED |
| P1 | BUG-B L5b wrong target | edit_risk.py:284 | CLOSED |

### §7 consensus / scope — fleet **F2**
| sev | bug | site | status |
|---|---|---|---|
| P0 | `_query_scope` admits name_match guesses (conf≥0.5, no method gate); false "drops SPECULATIVE" comment | gt_mini_patch.py | OPEN |
| P0 | "you edited X of N in-scope files" — N is a trajectory grab-bag → false premise | gt_mini_patch.py | OPEN |
| P1 | BUG-A test paths in scope/cochange | gt_mini_patch.py:2179/2587 | CLOSED |

### §8 wiring / gates — fleet **F6**
| sev | bug | site | status |
|---|---|---|---|
| P0 | `deepswe_full.yml` never forwards `GT_VERIFY_STRUCTURAL_RISK` → edit-risk lever dark on leaderboard | deepswe_full.yml | OPEN |
| P1 | two of three deep-telemetry sinks lost the same way | deepswe_full.yml | OPEN |
| P1 | `gate_lsp` no-cert fallback false-greens a degraded LSP | foundational_gates.py | OPEN |
| — | embedder gate false-fail (cert-reconcile) | foundational_gates.py | FIXED `cbce3687` |

**Rollup:** 9 P0 · ~16 P1 · ~17 P2 · 1 P3. Dominant class = **name_match-gate** (a name-guess at
confidence 0.6 launders as a fact through any `conf≥0.5` filter that doesn't also check
`resolution_method`) — one predicate closes ~6 findings across brief/consensus/L4/localizer.

---

## FIX FLEET STATUS (worktree-isolated; verify-first; base-reset to b3398595)

| fleet | scope | status |
|---|---|---|
| **F1** | Go indexer (promote/incremental/closure.go) | ✅ **DONE** — 3/3 CLOSED (d5b1e59a), biting tests mutation-verified, no change |
| **F5** | LSP (resolve.py) | ✅ **DONE** — 3/3 CLOSED (jdtls/column/warmth), mutation-verified, no change |
| **F6** | wiring (deepswe_full.yml, foundational_gates.py) | ✅ **DONE** — 2 CLOSED + gate_lsp residual FIXED, **merged d1ff9744** |
| **F2′** | consensus/scope (gt_mini_patch.py) | ✅ **DONE** — 2/2 CLOSED (name_match-gate + verified-component denom), real-producer tripwire **merged d4521ba0** |
| **F3** | localizer (graph_localizer.py + signals) | ✅ **DONE** — path-key charset-strip residual FIXED+**merged 472df22c**; RRF/generated CLOSED, 64-test suite 0-regression |
| **F4′** | brief (v1r_brief.py, v7_4_brief.py) | ✅ **DONE** — 3/3 CLOSED (_edge_conf/HIGH-loc/_top_fn), mutation-checked via PYTHONPATH=worktree/src, no change |
| **F7** | embedder (embed.py, anchor_select.py) | ✅ **DONE** — 128-tok/e5-cache CLOSED; passage-budget priority-order residual FIXED+**merged 0247e641** |

**🟩 GRID FULLY BINARY-GREEN ×5 AT THE VERIFICATION LEVEL (2026-06-17).** All 7 fleet agents done:
17 review-bugs verified CLOSED with mutation-checked biting tests + **4 residuals fixed/merged**
(F6 gate_lsp `d1ff9744` · F2′ scope-tripwire `d4521ba0` · F3 path-key `472df22c` · F7 passage-budget
`0247e641`). Cumulative re-witness 59/59, 0 regressions. **This is Stage-1 DETERMINISTIC green (code
paths correct + tests bite), NOT yet LIVE green** — per CLAUDE.md DEFINITION OF DONE, unit-green ≠ done.
**NEXT = the LIVE witness:** rebuild substrate from `0247e641` → run deepswe_full on TRAIN-5 → read each
layer from output.jsonl §4 → then VAL-5 → held-out TEST-5.

**Tally so far:** 9 review-bugs verified CLOSED with mutation-checked biting tests (F1×3, F5×3, F6×2 +
F6 residual fixed). 0 regressions. 4 agents still verifying. The pattern holds: `d5b1e59a` closed the
bulk; the sweep confirms the tests bite and catches residuals (F6 gate_lsp).

---

## NEXT
1. Fleet lands → LIPI each diff (4 avenues) → merge file-disjoint branches → re-witness on TRAIN-5.
2. Generalize-check the fixes on VALIDATE-5 (exercise constructs TRAIN lacks).
3. When the whole matrix is green ×5 (TRAIN+VAL) → fire the held-out TEST-5 paid run (final proof).
4. Each loop fire (8 min): update this file with fleet status + matrix deltas.
