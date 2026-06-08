# LIPI VERIFY VERDICT

I'll synthesize the 15 per-file verdicts into the three required outputs. The verdicts are already rendered; I just need to extract the BROKEN findings, count VERIFIED hunks per file, and assign per-file decisions.

---

## (1) BROKEN TABLE — must be FIXED or that hunk REVERTED

| file:line | fix (squash #) | what's broken — exact missing piece | avenue | severity |
|---|---|---|---|---|
| `post_view.py:48,69-73` | #31 ego-block trim priority | `_EGO_FIRST_LINE_RE` has no `re.MULTILINE`; `$` never anchors before the mid-string `\n` of the multi-line `_ego_text` (always ≥3 lines when it fires) → regex always False → ego still band 5, trimmed first. Match the first line only: `_EGO_FIRST_LINE_RE.match(s.split("\n",1)[0])` OR drop trailing `$` OR compile `re.MULTILINE`. | Logic/Impl | logic (inert no-op, no regression) |
| `incremental.go:293-294,306-307` | #4/#3 GetAllNodes column expansion | Comment claims "non-lobotomized node view" but the load-bearing field `BuildNodeMeta` consumes — `ReturnType` — is NOT selected; added `signature`/`qualified_name` feed no consumer of `filteredNodes` (inert). Strategy 1.96 via-return chain stays DEAD on incremental path. Add `COALESCE(return_type,'')` to SELECT (293) + `&n.ReturnType` to Scan (306). | Integration | logic (degraded resolution, no crash) |
| `oh_gt_full_wrapper.py:4477` | #51 governor gate widening | Comment claims edit routes source/non-source edits to `_handle_source_edit`/`_handle_non_source_edit`; it does NOT — `after_interaction` short-circuits every `CmdRunAction` at `governor.py:213` before the edit-dispatch; OH never emits `FileEditAction`. Dispatch surface stays unreachable. Fix = correct/accept the comment; the claimed coverage is structurally unreachable on the OH path. | Integration | logic (safe but misdescriptive; intent unmet) |
| `oh_gt_full_wrapper.py:549` | #52 goku diff-collapse bridge | `record_diff_snapshot` (`agent_state.py:398-399`) co-sets `patch_collapsed=True` AND `durable_edit_lost=True`, but the firing gate `governor.py:612` requires `patch_collapsed and not durable_edit_lost` → gate structurally dead, hook never fires. Fix = set `patch_collapsed` WITHOUT pre-setting `durable_edit_lost` (let `governor.py:618` fire-once), OR drop the `not durable_edit_lost` clause at gate 612. | Logic | logic (state-bridge works, detection inert) |

No compile-break or runtime-crash exists in any file (every file AST-parses / inspects clean; the Go files were read-verified, `go` not on PATH). All 5 BROKEN items are **inert/logic-only** — none regresses behavior; each simply fails to deliver its stated intent.

---

## (2) VERIFIED TABLE — kept

| file | # fixes passing all 4 LIPI avenues |
|---|---|
| `src/groundtruth/pretask/v1r_brief.py` | 4 (#60, #36, #37, #1) |
| `src/groundtruth/pretask/graph_localizer.py` | 3 (#54, #56, #57) — #55 correctly untouched |
| `src/groundtruth/pretask/contract_map.py` | 5 (#17a, #17b, #17-helpers, #14) |
| `src/groundtruth/pretask/curation_map.py` | 5 (#14, #15, #16, #35, #59) |
| `src/groundtruth/pretask/anchor_select.py` | 2 (#18, #47) |
| `src/groundtruth/pretask/v7_4_brief.py` | 4 (#21, #46, #19, #20) |
| `src/groundtruth/hooks/post_view.py` | 4 (#32, #33, #34, #9) — #31 BROKEN |
| `src/groundtruth/hooks/post_edit.py` | 4 (#10, #11, #12, #13) |
| `src/groundtruth/resolve.py` | 3 (#28, #29, #30) |
| `benchmarks/swebench/gt_intel.py` | 8 (#7, #8, #22, #23, #24, #25, #26, #27) — #22/#25 propagation INCOMPLETE (latent, pre-existing) |
| `gt-index/internal/resolver/resolver.go` | 6 (#1, #5, #6, #39, #40, #41) |
| `gt-index/internal/parser/parser.go` | 8 (#42, #42b, #43, #43b, #44, #2a, #2b, #2c, #2d) |
| `gt-index/internal/store/incremental.go` | 3 (#H1/#3, #H2/#3, #H3/#3+#4) — H4 BROKEN |
| `gt-index/internal/store/sqlite.go` | 1 (#45) |
| `scripts/swebench/oh_gt_full_wrapper.py` | 4 (#48a, #48b, #49, #50) — #51, #52 BROKEN |

**Total: 67 hunks VERIFIED clean across all 4 avenues; 5 BROKEN (all inert/logic-only).**

---

## (3) PER-FILE VERDICT (one line each)

| file | verdict |
|---|---|
| `src/groundtruth/pretask/v1r_brief.py` | CLEAN — 4/4 verified; pre-existing INFO-filter desync is unreachable in header-fired path, optional 1-line hardening only |
| `src/groundtruth/pretask/graph_localizer.py` | CLEAN — 3/3 verified; #56 docstring "len>=4" drift is cosmetic |
| `src/groundtruth/pretask/contract_map.py` | CLEAN — 5/5 verified; dead `build_function_map` import is `ruff F401` hygiene only |
| `src/groundtruth/pretask/curation_map.py` | CLEAN — 5/5 verified; docstring "imports THIS set" claim is cosmetically false (sets byte-identical) |
| `src/groundtruth/pretask/anchor_select.py` | CLEAN — 2/2 verified; lexical-ingress comment misattributes forward-slashing (fix more correct than claimed) |
| `src/groundtruth/pretask/v7_4_brief.py` | CLEAN — 4/4 verified |
| `src/groundtruth/hooks/post_view.py` | NEEDS-FIX — #31 inert (multi-line `$` never matches); fix: `_EGO_FIRST_LINE_RE.match(s.split("\n",1)[0])` or drop `$` or `re.MULTILINE`. #32/#33/#34/#9 commit |
| `src/groundtruth/hooks/post_edit.py` | CLEAN — 4/4 verified |
| `src/groundtruth/resolve.py` | CLEAN — 3/3 verified |
| `benchmarks/swebench/gt_intel.py` | NEEDS-FIX (follow-up, non-blocking) — 8/9 clean; propagate the 3→4-tuple at `gt_v2_hooks.py:129` + `gt_v2_bridge.py:235` (`score, summary, *_ = ...`); already-broken-at-HEAD, not worsened — gt_intel.py squash itself COMMITS |
| `gt-index/internal/resolver/resolver.go` | CLEAN — 6/6 verified |
| `gt-index/internal/parser/parser.go` | CLEAN — 8/8 verified; call-order raw-`Index` anchor may be off-by-a-line on non-boundary substring (matches existing twin, not a break) |
| `gt-index/internal/store/incremental.go` | NEEDS-FIX — H1/H2/H3 commit; H4 must add `COALESCE(return_type,'')` to SELECT:293 + `&n.ReturnType` to Scan:306 (or it leaves the via-return chain dead on the incremental path) |
| `gt-index/internal/store/sqlite.go` | CLEAN — 1/1 verified, end-to-end round-trip proven |
| `scripts/swebench/oh_gt_full_wrapper.py` | NEEDS-FIX — #48a/#48b/#49/#50 commit; #51 fix/accept the misdescriptive comment (claimed dispatch unreachable on OH); #52 fix the `patch_collapsed`/`durable_edit_lost` co-set at `agent_state.py:398-399` (or gate `governor.py:612`) or the collapse hook stays inert |

**No file requires a full REVERT.** Every BROKEN item is a completable one-spot fix (named above); no hunk is unsalvageable. Four files are NEEDS-FIX (`post_view.py`, `incremental.go`, `oh_gt_full_wrapper.py`, plus the non-blocking follow-up on `gt_intel.py`'s two external consumers); the remaining 11 files are CLEAN.

# PER-FILE

No leftover positional score slice; `_loc_primary` is defined once and used once (2892-2893). The old `_scores = [r.get("score", 0.0) for r in top_records[: len(entries)]]` is fully replaced. No duplicate `_localization_header` call remains.

I have now verified all four hunks across all four LIPI avenues by reading the actual code. Here is my final verdict.

---

## LIPI VERIFICATION — `src/groundtruth/pretask/v1r_brief.py` (4 hunks)

**#60 — `_top_function_names` redundant Python re-partition removed | v1r_brief.py:326-333 | VERIFIED | Logic**
The SQL `ORDER BY CASE WHEN LOWER(n.name) IN (_ph) THEN 0 ELSE 1 END` (line 301) already front-sorts issue-matched names using the single filtered `_terms` set (len>2, bound at 304). The deleted Python block re-partitioned on the UNFILTERED `issue_terms`, contradicting the SQL order. New code returns `rows[:limit]` verbatim — one filtered ranker decides. IMPL: `rows` is already ordered by the SQL; the bare `[row[0] for row in rows[:limit]]` is complete. INTEGRATION: no other caller relies on the old re-promotion. PLUMBING: same `rows`. Does not break the other 3 avenues.

**#36 — `_resolved_witnesses_for_file` callee `code` field | v1r_brief.py:733,743 | VERIFIED | Plumbing**
The emitted record's location is `(callee_file, def_line)` (738-740); the `code` field now reads `_code_at(callee_file, def_line)` (743) instead of the call-site code, so every field references the same callee location — symmetric with the caller branch (691-702, where shadow-line == rendered-line). LOGIC: the shadow check correctly still reads at the CALL SITE `_code_at(file_path, source_line)` (733) because `os.walk(` must be detected where it is called. IMPL complete: `code` guarded `if def_line else ""`; the `finally: conn.close()` block (750-755) is intact (diff just didn't show it). INTEGRATION: consistent with the caller twin. Does not break the other 3 avenues.

**#37 — `_entry_confidence_tier` path_match anchoring | v1r_brief.py:1268-1272 | VERIFIED | Logic**
`_stem_specific = len(_stem) >= 5 or "_" in _stem` is the exact De-Morgan complement of the codebase's own anti-generic skip rule in `_exact_issue_named_files` (line 2139: `if len(name) < 5 and "_" not in name: continue`) — claim in the comment is accurate. `_re.search(rf"\b{_re.escape(_stem)}\b", _it)` replaces the unanchored `_stem in _it`, so `base`-in-`database` no longer promotes. IMPL: `os` (line 12) and `_re` (line 13) are module-level imports; `_re.escape` prevents regex injection; empty `_stem` short-circuits via `and` (no empty-pattern run). LOGIC/INTEGRATION/PLUMBING clean. Does not break the other 3 avenues.

**#1 (L1 cross-wire) — `_localization_header` → `tuple[str,str]` + early-compute + `entries` reorder | v1r_brief.py:1891,1915,2043,2069,2092,2891-2899,2989 | VERIFIED | Integration**
The bug the prompt warned about is ABSENT: ALL FOUR return paths now return 2-tuples — 1915 `"",""`; 2043 `…, tgt.file_path`; 2069 `…, shown[0].file_path`; 2092 `…, shown[0].file_path`. The fall-through at 2044 (weak func anchor) lands on 2069/2092 — no bare `return`, no implicit `None`. The signature/return change is COMPLETE: the ONLY call site is 2891 and it unpacks `_loc_header, _loc_primary = …`; the old render-site recompute was deleted (2995 comment). Every downstream `_loc_header` use is str-correct: `_emit_old = _loc_header == ""` (2997), `_loc_header + "\n" + brief_text` (3012/3028/3031), `_tier_from_loc_header(_loc_header)` (3060). No caller does `header + '\n'` on a tuple.
- LOGIC: reorder (2898-2899 `entries.insert(0, entries.pop(_pi))`) is a stable move-to-front, guarded `_pi not in (None,0)` → adds/drops nothing, no-op when not found or already #1 (correct-or-quiet).
- INTEGRATION (the necessary companion fix): `_scores` switched to `[float(getattr(e,"score",0.0)) for e in entries]` (2989). Verified `FileEntry.score` (166) is set at 2862 from `rec.get("score",0.0)` (2820) — identical value to the old positional slice but now tracks the reorder; the old `top_records[:len(entries)]` slice would have mispaired scores after the reorder. Token-trim loop (3024-3026) pops tail of both lists, stays aligned.
- PLUMBING: reorder matches with `_gl_normalize` on both sides (2893/2895); same normalizer the header itself uses on candidate paths (1976, 2012; imported line 42 `_normalize as _gl_normalize`). `_loc` defined at 2412/2451/2454, not reassigned between 2891 and the old site → early compute is content-equivalent.

**Residual to flag (NOT broken, pre-existing): one narrower desync survives.** The reorder makes `entries[0]` the header primary, and L1-SCOPE keys off raw `entries[0].path` (2932). But `render_brief` FILTERS `[INFO]` entries before `files[0]` (1430-1432), so IF the localizer-named primary scored `[INFO]`, `<gt-task-brief>`/`_edit_target_contracts_block`/graph-map (1612/1648/1330) would key off a different file than `<gt-localization>` and L1-SCOPE. In practice a header-fired primary is a localizer candidate carrying `localizer_confidence > 0.1` → `[WARNING]` (1296), surviving the filter, so the corner is essentially unreachable; and this INFO-filter desync pre-dated the edit (render_brief already filtered; L1-SCOPE already used raw `entries[0]`). The edit strictly improves consistency and does not regress it. Optional hardening: have render_brief's INFO-filter never drop `files[0]` when `_loc` fired, OR seed L1-SCOPE/contracts from `_loc_primary` directly.

**COMMIT verdict:** all four hunks VERIFIED — correct, complete (every return + the single caller updated), internally symmetric, generalized, correct-or-quiet. No avenue broken by any hunk. The one residual (INFO-filter primary-drop) is pre-existing, unreachable in the header-fired path, and worth a one-line follow-up, not a revert.

---

Both names are defined (`_DETERMINISTIC_METHODS` imported at line 49, `_STDLIB_ATTRS` defined at line 76). No undefined references.

Now one final integration check on #54: the path-decay traversal previously dropped edges via SQL `conf_where` (`AND e.confidence >= {min_edge_conf}`) which only filtered on confidence — it did NOT do the SUPPRESSED or stdlib-shadow filtering, and critically it filtered VERIFIED edges too (a verified edge with conf < min_edge_conf would have been SQL-dropped). The new code removes `conf_where` and routes through `_edge_admitted`, which does NOT confidence-filter verified edges. So the new behavior **admits more verified edges** into path-decay (verified low-conf edges that SQL previously dropped) while **dropping** SUPPRESSED/stdlib-shadow edges that SQL previously admitted. This is the intended convergence with the witness BFS. Both directions are correct-or-quiet improvements: verified edges are facts (should not be conf-gated), and SUPPRESSED/shadow edges are noise (should be dropped). No regression — it's the intended symmetry.

Let me do one last check: confirm `_is_verified` is the same predicate used in both walks (it is — line 482 path-decay, line 1723 localize, both call `_is_verified(method)`), and that the `_path_decay_scores` cost-weight default (1.0 when conf missing) is preserved separately from the admission conf (0.0). Both confirmed in the diff: `admit_conf` (0.0 default) for admission, `conf_f` (1.0 default) for the cost weight. This separation is deliberate and correct — admission uses the witness-BFS convention (0.0 → unverified low-conf gets dropped) while the Dijkstra cost weight keeps 1.0 to avoid div-by-zero inflation.

I have verified all hunks across all four LIPI avenues. Let me compile the final verdict.

---

**LIPI VERIFICATION — `src/groundtruth/pretask/graph_localizer.py` (working-tree diff vs HEAD).** Read-only. The diff implements squash-list items **#54, #56, #57**; **#55 was correctly left untouched.** Four hunks. All VERIFIED.

**#57 (DEFINES strength-inversion cap) | graph_localizer.py:165-166, 547-562 | VERIFIED | all 4 avenues clean**
- LOGIC: intent = a hop-0 DEFINES scalar must not beat a verified edge. `_WITNESS_DEFINES_CEIL = 1.0*(1/4)*0.95 = 0.2375`. Old DEFINES `0.55*conf*1.0` (=0.55 at conf 1.0) beat a 1-hop verified edge `1.0*conf*0.5` (=0.50). New `min(0.55*conf*decay, 0.2375)` caps it below the weakest verified-edge strength. Ceiling derivation is real: `_dynamic_max_hop` (1109-1138) caps at 3, `_dyn_hop = min(max_hop, …)` with `localize(max_hop=3)` (1474) → max hop = 3 confirmed. The "hop-3 verified edge (0.225) < capped DEFINES (0.2375)" edge case is NOT a regression — the module's own doctrine (1919-1921) intends DEFINES to outrank a weak hop-3 edge.
- IMPLEMENTATION: edit is COMPLETE — `conf`/`decay` hoisted above the branch and reused in BOTH the DEFINES return (557) and the edge return (562); both `return` paths updated; `defines_anchor` direction string unchanged from the pre-diff code and matches construction at 1666. No undefined names.
- INTEGRATION: the scalar feeds `Candidate.confidence` (1908) → `_loc_conf_by_file` → `FileEntry.localizer_confidence` → `_entry_confidence_tier` (v1r_brief.py:1295-1297). The ONLY scalar gate there is `if _loc_conf > 0.1: [WARNING]`; **0.2375 > 0.1, so no tier flips.** The `[VERIFIED]` gate (1280) keys on the BOOLEAN `witness_verified`/`has_verified_witness`, untouched by the cap → a verified-DEFINES gold stays `[VERIFIED]`. The file-sort key `(_witness_tier, -score, …)` (1980) is tier-dominant; the cap only moves the within-tier scalar, which `_witness_tier` (1922-1929) already orders correctly.
- PLUMBING: no DB/path change. Does NOT break the other 3 avenues.

**#54 (one shared `_edge_admitted` predicate across witness-BFS + path-decay) | graph_localizer.py:1030-1052, 387-389/442-444/477-493, 1734, 1780-1784 | VERIFIED | all 4 avenues clean**
- LOGIC: `_edge_admitted` reproduces the old inline 3-rule order exactly (SUPPRESSED hard-exclude → unverified `conf < min` → unverified stdlib-shadow), verified edges never conf/shadow-filtered.
- IMPLEMENTATION: both call sites pass matching args; `nbr_name` is coerced non-None at both (`str(nbr_name or "")` at 1718 and 491); `_STDLIB_ATTRS` (76) and `_DETERMINISTIC_METHODS` (49) defined; `_is_verified(method)` is the shared verified predicate in both walks (482, 1723).
- INTEGRATION: threshold is consistent — both walks use `_dyn_conf` (localize passes `min_edge_conf=_dyn_conf` at 1782; admission at 1734 uses `_dyn_conf`). The deliberate `admit_conf` (0.0 default, for admission) vs `conf_f` (1.0 default, for Dijkstra cost weight) split is preserved (487 vs 495) — matches the witness-BFS 0.0 convention so the SAME edge is admitted/rejected in both, while the cost weight keeps 1.0 to avoid div-by-zero. Old SQL `conf_where` removed; admission moved to Python. Net effect is the intended convergence: path-decay now admits verified low-conf edges SQL used to drop, and drops SUPPRESSED/shadow edges SQL used to admit — both correct-or-quiet, no regression.
- PLUMBING: `has_method`/`has_trust_tier` plumbed into the SELECT (443-444, 464-465) and into the call (1783); columns guarded by `method_sel`/`tier_sel` defaults (`''`) when absent. Reaches the consumer.

**#56 (boundary-aware `_lex_hit` replaces unbounded substring) | graph_localizer.py:649-685, 1872 | VERIFIED (one cosmetic docstring drift, non-functional)**
- LOGIC: old `t == s or t in s or s in t` made `set` match `settings`/`reset`/`offset`. New `_lex_hit` = exact `t==s` OR `t in _ident_components(s)` (camelCase/snake/digit split). Worked examples confirm: `set`→`set_fields` HIT (component), `set`→`settings`/`reset`/`offset` MISS. Strictly tighter than the old substring — kills the lexical over-connect.
- IMPLEMENTATION: COMPLETE — sole consumer (1872) rewired; `_IDENT_SPLIT_RE`/`_ident_components`/`_lex_hit` self-contained; `len(s) <= 2` skip (679) mirrors old `len(s) > 2`. **One cosmetic defect:** the docstring (671) claims "distinctive terms (len >= 4 … full symbol stem)" but the code uses `_MIN_ANCHOR_LEN` (=3) at 676 with NO separate >=4 branch; `_issue_terms` already pre-filters terms to len>=3 (644-645). The >=4 gate the docstring describes is not implemented, but the actual behavior (exact-equality OR component-membership at floor 3) is sound and tighter than before — **docstring/impl drift only, no functional bug, no consumer depends on the >=4 distinction.**
- INTEGRATION/PLUMBING: `lex_hits`→`lex_norm` (1873)→`W_LEX` score term only; no path/DB change. Does NOT break the other avenues.

**#55 (FTS5 second writable conn) | graph_localizer.py:282-301 | CORRECTLY UNTOUCHED** — this M-confidence item was scoped out of the squash; the writable-conn fallback is unchanged, so no regression was introduced here. It remains an open finding, not part of this commit.

**COMMIT VERDICT: all three implemented fixes (#54, #56, #57) are correct and complete across Logic/Implementation/Integration/Plumbing; none breaks another avenue. COMMIT.** The single nit (the #56 docstring's "len >= 4" claim that the code does not enforce at floor 3) is cosmetic — optionally correct the docstring to say "floor `_MIN_ANCHOR_LEN` (3), exact-equality or component-membership," but it is not a blocker and does not warrant a revert.

Relevant file: `D:\Groundtruth\src\groundtruth\pretask\graph_localizer.py` (hunks at lines 152-166, 380-518, 547-562, 649-685, 1030-1052, 1734, 1780-1784). Downstream tier consumer that confirms #57 is tier-safe: `D:\Groundtruth\src\groundtruth\pretask\v1r_brief.py:1280-1297`.

---

`build_function_map` is now imported (line 38) but its only remaining occurrence is in a docstring (line 746) — it is no longer CALLED anywhere in contract_map.py. This is a now-dead import. It does not break anything (no NameError, no runtime effect), but it's an incomplete cleanup: a linter (`ruff F401`) would flag it as unused. Worth flagging as a minor implementation hygiene item, not a functional break.

Now let me verify the one remaining correctness concern on the `_resolved_callee_node_id` helper: the `source_ids` passed are the focus function's `ids` (from `_node_ids(conn, fpath, fname)`), and `_neighbors` was called with the SAME `ids` as `node_ids`. So `e.source_id IN (ids)` in the helper exactly mirrors `_neighbors`'s `match_col IN (placeholders)` for callees (`match_col = e.source_id`). The helper re-derives the resolved target by filtering on `(callee_name, callee_file)` among edges from those same source ids. This correctly recovers the resolver's target node. 

One edge-case: `_neighbors` deduped by `(name, file)` and sorted facts-first, so the returned `edge` may carry a `resolution_method` from the best-provenance row. The helper does NOT re-check that the SPECIFIC returned edge's method — it gates on `LOWER(TRIM(e.resolution_method)) IN (_det_in)` (when `has_method`). Since the caller already checked `edge.resolution_method in _DETERMINISTIC_METHODS` before calling the helper, and the helper independently filters to deterministic-method edges, the helper will find at least the row(s) that produced that verified edge. Correct.

I have completed the full LIPI verification across all 4 avenues for all 3 fixes. Here are my verdicts.

---

**LIPI verdict — `src/groundtruth/pretask/contract_map.py` (3 fixes / 5 hunks)**

**#17a — build_contract callee branch (item #17) | contract_map.py:329-340 | VERIFIED | Logic+Impl+Integration+Plumbing**
LOGIC: replaces `_evidence_for(...)` over the same-name union with `_resolved_callee_node_id(...)` → `_evidence_for(..., ids=[callee_id])`. Correct: passing a single resolved id makes `_node_meta` (sig, ids:166) and `_read_props` (props, ids:184) read from the SAME node — closes the "sig-over-lowest-line + props-over-union" defect. IMPL: complete — abstains (`continue`) when `callee_id is None` (correct-or-quiet). PLUMBING: `_resolved_callee_node_id` joins `e.target_id=n.id WHERE e.source_id IN(ids)`, exactly `_neighbors`'s callees direction (curation_map:360,368); `callee_file=edge.file` exact-matches the raw `n.file_path` `_neighbors` returns (curation_map:366,414). INTEGRATION: gate set `_DETERMINISTIC_METHODS` == `DETERMINISTIC_RESOLUTION_METHODS` (curation_map:102) used by `_neighbors` (curation_map:380) — identical `_det_in`. No avenue broken.

**#17b — edit_target_callee_contracts (item #17, #1) | contract_map.py:565-575 | VERIFIED | same**
LOGIC: `_node_sig_line(conn,file,name)` (lowest-line over union, DELETED) → `_resolved_callee_node_id(...)` + `_node_sig_line_by_id(conn,node_id)` (`SELECT ... WHERE id=?`, :441-457). Sig+line now from the edge's actual target node. IMPL: complete — `if callee_id is None: continue`, then `if not sig: continue`; `CalleeContract(...line=line)` return unchanged (`int`). Old `_node_sig_line` has ZERO live callers (grep: only the NOTE comment at :437) — no dangling reference, no half-applied rename. No avenue broken.

**#17 helpers — _node_sig_line_by_id / _resolved_callee_node_id | contract_map.py:441-508 | VERIFIED | all**
`_node_sig_line_by_id` mirrors the old return shape (`(sanitized_sig, int|0)`). `_resolved_callee_node_id` correct-or-quiet: returns None when no verified edge, else lowest-line among ONLY the resolver's target ids (`rows.sort` NULL-last, :507) — pinned to edge targets, never the file-wide union. The `has_method` branch gates exactly as `_neighbors`; when `has_method=False` it (correctly) does not filter, matching `_neighbors`'s conf=0.0-sentinel behavior. No avenue broken.

**#14 — _verified_caller_count UNCAP | contract_map.py:740-750 | VERIFIED | Logic+Plumbing+Integration**
LOGIC: was `sum(is_fact for e in build_function_map(dynamic=False)[0].callers)` — truncated at `max_neighbors=5` (curation_map:768) → now `verified_caller_count(...)` = uncapped `COUNT(DISTINCT name||file)` with the same deterministic+is_test=0 gate, NO row cap (curation_map:471-475). Fixes the 6x blast-radius understatement. IMPL/INTEGRATION: wrapper preserves the exact `(graph_db_path,file_path,name)->int` signature; sole consumer (build_drift:806 → `ContractDrift(...,cnt)`) unaffected — no signature/return-type change, no caller breakage. PLUMBING: `verified_caller_count` is exported and imported (contract_map:39). No avenue broken.

---

**Minor (non-blocking, does NOT gate commit):**
- `build_function_map` (contract_map:38) is now a **dead import** — its only remaining occurrence is the docstring at :746; no live call after the #14 rewrite. No NameError/runtime effect, but `ruff F401` would flag it. Recommend dropping the import in a cleanup.

**Decision: COMMIT.** All 5 hunks VERIFIED across all four LIPI avenues. The signature/return-type-change failure class the prompt warned about (the `_localization_header→tuple` bug) does NOT recur here: `_node_sig_line_by_id` keeps the `tuple[str,int]` shape its caller unpacks (`sig, line =`, :575), and the `_verified_caller_count` wrapper keeps `->int`. The deleted `_node_sig_line` has no surviving callers. The only residue is the unused `build_function_map` import (cosmetic).

---

Both files compile; all new functions are defined. Now one last verification for #15 (the second-hop sparseness gate change) and #59 (the `_apply_dynamic_budget` true-count + `_second_hop` over-fetch). I've read the bodies. Let me confirm the `_second_hop_facts` `exclude` is applied AFTER the over-fetch in Python (line 575-579) — yes, the over-fetch `limit*4 + len(exclude)` widens the window and the Python loop applies `exclude` per-row. The `_neighbors` it calls now also passes `repo_root` — consistent. And the budget's `windowed_fact_count` vs `true_fact_count` with the `max()` floor (line 520) — verified the defensive floor prevents a smaller bad override.

One subtle thing on #15: the sparseness gate now reads `fact_neighbors = [e for e in edges if e.is_fact]` and gates `> _SECOND_HOP_SPARSE_THRESHOLD` (i.e., >1). Previously it gated on `len(edges)` (total visible). The seed-building block right below (line 649-652) reuses `fact_neighbors` — the diff removed the now-duplicate re-computation (old `fact_neighbors = [...]` after the gate). Confirmed the variable is computed once and reused, no NameError. 

I have verified all 5 squash-list items touching this file across all 4 LIPI avenues, plus call-compatibility of every consumer. Here is the verdict.

---

**LIPI VERIFICATION — `src/groundtruth/pretask/curation_map.py` (git diff vs HEAD). All 5 squash-list rows (#14, #15, #16, #35, #59) + shared helpers. READ-ONLY, nothing run except `py_compile` + AST name-check (both green).**

**#16 — `_node_ids` path-normalize + suffix-LIKE | curation_map.py:271-298 | VERIFIED | Logic/Implementation/Integration/Plumbing**
- LOGIC ✓ `normalize_file_path` (138-151) = `replace("\\","/").lstrip("./").lstrip("/")` — byte-identical to the witness twin's inline norm at v1r_brief.py:528/655. SQL `REPLACE(file_path,'\','/') LIKE '%'||norm_fp` matches the twin's `nt.file_path LIKE ?`+`f"%{_norm_fp}"` (v1r_brief.py:539/547). Empty-guard returns `[]` (288).
- IMPLEMENTATION ✓ binding is `f"%{norm_fp}"` (positional), label filter retained.
- INTEGRATION ✓ `contract_map.py:36` IMPORTS this exact `_node_ids` (not a private copy), so the "+contract_map.py:430" twin in the squash-list is the SAME function — no inconsistent twin. All 3 `_node_ids` uses in contract_map (244/300/543) inherit the fix.
- PLUMBING ✓ matches the real stored column; suffix-LIKE is the same shape the witness already relies on. Does NOT break the other 3.
- One residual (pre-existing, not introduced): suffix-LIKE `%foo/bar.py` can over-match `…/xfoo/bar.py`; harmless here (the witness twin has the identical property, parity is the goal), not a regression.

**#35 — stdlib-shadow guard in `_neighbors` | curation_map.py:361-406 (+helpers 154-187, 301-319) | VERIFIED | Logic/Implementation/Integration/Plumbing**
- LOGIC ✓ `is_stdlib_shadow` (170-187) and `_STDLIB_MODULES` (158-165) are byte-identical to v1r_brief.py:467-483 / 457-464. Guard fires on `<stdlib>.<target_name>(` at the call site.
- IMPLEMENTATION ✓ NOT dead despite the FACTS-ONLY SQL gate (379-381): that gate keeps only deterministic rows; the guard then drops the laundered-deterministic stdlib-shadow subset — exactly its purpose. New JOIN `nodes ntgt ON e.target_id=ntgt.id` is row-count-neutral because `target_id` is `INTEGER NOT NULL REFERENCES nodes(id)` (sqlite.go:160) — no row can be silently dropped. 7-tuple unpack (396) matches the 7-col SELECT (366-367). `repo_root` unset → guard no-ops (403).
- INTEGRATION ✓ both directions correct: callees → `os.walk(` at focus line vs callee name `walk`; callers → call-site line vs focus name. Mirrors the twin's caller (`_is_stdlib_shadow(code, target_name)` v1r_brief.py:693) and callee (734) checks.
- PLUMBING ✓ `_read_code_line` (301-319) = the twin's `_code_at` (v1r_brief.py:658-670). Does NOT break the other 3. (Docstring INACCURACY, hygiene-only — see note below.)

**#14 — `verified_caller_count` uncapped COUNT | curation_map.py:437-481, 762-791 | VERIFIED | Plumbing/Logic/Implementation/Integration**
- LOGIC ✓ dedicated `COUNT(DISTINCT name||sep||file_path)`, deterministic-method gate, `is_test=0`, NO `max_neighbors` cap → real fact count (old path truncated at 5).
- IMPLEMENTATION ✓ source literal line 471 is `'\\x00'` → SQL constant separator `\x00` (4 literal chars, verified by byte-inspection) — a collision-safe DISTINCT separator (name/path never contain it); even if it were a true NUL it'd be valid. `has_method=False` → returns 0 (correct: can't judge provenance). Never raises.
- INTEGRATION ✓ consumer `contract_map._verified_caller_count` (740-750) now calls `verified_caller_count` and is itself called by `build_drift` (806) → `ContractDrift.caller_count` → render "{n} verified caller(s) depend on this" (render_drift 761-763). End-to-end wired; `verified_caller_count` imported at contract_map.py:39.
- PLUMBING ✓ reuses `_node_ids`/`_has_columns`/`_open_ro`; `direction="callers"`. Does NOT break the other 3.

**#15 — second-hop sparseness gated on FACT count | curation_map.py:638-683 | VERIFIED | Logic/Implementation/Integration/Plumbing**
- LOGIC ✓ gate now `len(fact_neighbors) > _SECOND_HOP_SPARSE_THRESHOLD` (1) using VERIFIED edges, not total visible — so a 0-fact target with name_match guesses still triggers the verified rescue (previously suppressed). Matches the intent.
- IMPLEMENTATION ✓ `fact_neighbors` computed once (646) and reused as the seed source (651); the old duplicate re-assignment was removed (no NameError). `remaining`/`limit` logic unchanged and correct.
- INTEGRATION ✓ seeds 2-hop only from fact neighbors; `_second_hop_facts` keeps facts-only (576). `repo_root` threaded through (681).
- PLUMBING ✓ `_node_ids` seed lookups inherit the path-normalize fix. Does NOT break the other 3.

**#59 — `_apply_dynamic_budget` true-count + `_second_hop_facts` over-fetch | curation_map.py:484-526, 529-592, 626-636 | VERIFIED | Logic/Implementation**
- LOGIC ✓ budget shrinks against `true_fact_count` (uncapped `_verified_neighbor_count`, 630-632) not the over-fetch window → mega-hub no longer leaks guesses. `_second_hop_facts` over-fetch = `limit*4 + len(exclude)` (564) so the cap can't bite before Python-side `exclude` runs (575-579).
- IMPLEMENTATION ✓ defensive `raw_fact_count = max(true, windowed)` (520) blocks a smaller bad override from re-opening the guess budget. `true_fact_count: int|None=None` default keeps legacy callers intact.
- INTEGRATION ✓ `true_facts` uses the SAME deterministic-method gate as `_neighbors`, so window-vs-count provenance agrees. Does NOT break the other 3.

**Cross-cutting call-compatibility — VERIFIED**
- `repo_root` is keyword-only on `build_function_map`/`_neighbors`/`_dynamic_neighbors`/`_second_hop_facts` (after `*,`), default `""`. All external callers safe: `contract_map.py:312/546` (`_neighbors`, no `repo_root` → no-op guard, 7-col SELECT/unpack still consistent), `contract_delta.py:236` + `v1r_brief.py:1339` + `v22_brief.py:190` (`build_function_map(dynamic=False)`, 2 positional args only).
- `DETERMINISTIC_RESOLUTION_METHODS` / `_DETERMINISTIC_METHODS` / `_NAME_MATCH_FLOOR` all defined (83-107); `import re as _re` added (37).

**ONE NON-BLOCKING DEFECT (hygiene, does NOT warrant revert):**
- `#docstring | curation_map.py:157 & 177 | BROKEN (docstring only) | Integration-comment** — comments claim "Kept in sync with v1r_brief._STDLIB_MODULES (the witness twin **imports THIS set**)" and "the witness twin … imports THIS set." FALSE: v1r_brief.py:23-28 imports only `DETERMINISTIC_RESOLUTION_METHODS, _DETERMINISTIC_METHODS, _NAME_MATCH_FLOOR, _has_columns` — it DEFINES its own duplicate `_STDLIB_MODULES` (v1r_brief.py:457) and `_is_stdlib_shadow` (467). The sets ARE currently byte-identical so behavior is correct, but the "imports" claim is wrong and the two copies can silently drift. The code is correct; only the comment lies. Fix = change wording to "duplicated in v1r_brief — keep in sync" OR have v1r_brief import these from curation_map. No runtime impact; do not block the commit on it.

**VERDICT: COMMIT.** All 5 functional hunks (#14, #15, #16, #35, #59) are LOGIC-correct, IMPLEMENTATION-complete (no half-applied signature change — `verified_caller_count`'s consumer at contract_map.py:740-750→806 IS updated; the 7-tuple SELECT/unpack is symmetric; `repo_root` threaded through every internal call site), INTEGRATION-consistent with the witness twin (`v1r_brief._resolved_witnesses_for_file` / `_caller_contract_for_file`), and PLUMBING-correct (NOT-NULL `target_id` makes the new JOIN safe; shared normalizer reaches the real column). No hunk breaks any of the other 3 avenues. The only finding is a cosmetic docstring inaccuracy (curation_map.py:157/177) — fix-forward, not revert.

---

File AST-parses clean. `math` import is still used (line 273, `math.isfinite`) — so deleting `structural_seed_expand` (which used `math.tanh`) did NOT orphan the import. No dead import. The deletion is complete and clean.

I have verified everything needed. Both fixes in the diff are complete, the deletion has no dangling references (src + tests both clean, tests updated to assert removal), and the path-normalization fix is a no-op-or-correct on every code path because the Go indexer guarantees canonical `nodes.file_path`. Final verdict.

---

**LIPI VERDICT — `src/groundtruth/pretask/anchor_select.py` (2 fixes in the diff; #18 path-norm, #47 dead-code delete)**

**#18 semantic ingress | anchor_select.py:190-196 (`_get_file_embeddings`) | VERIFIED | Plumbing/Integration | reason:** `file_paths = list(dict.fromkeys(_norm_path(row[0]) ...))` canonicalizes node paths before they become `semantic_top_k` keys, so `sem_seed_scores` and `sem_all_scores` are both canonical (both derive from this one list — confirmed line 259). `dict.fromkeys` preserves order + de-dups separator-variant paths. No downstream break: `nodes.file_path` is ALREADY canonical (Go indexer `walker.go:99-100` `filepath.Rel`+`ToSlash` at write time), so `_norm_path` here is a guaranteed no-op → the normalized keys still match raw `nodes.file_path` in `compute_reach`/`compute_anchor_proximity`/`graph_expand_candidates` SQL `IN` clauses (graph_reach.py:54, anchor_proximity.py:42, both raw-keyed). Does NOT break Logic/Impl/Integration/Plumbing.

**#18 symbol ingress | anchor_select.py:115-117 (`_symbol_anchors`) | VERIFIED | Plumbing | reason:** `file_path = _norm_path(row["file_path"] or "")` canonicalizes the symbol pipe's dict key to match the semantic + lexical pipes; `matched.setdefault(file_path, ...)` (line ~123) now keys on the canonical form. No-op in practice (indexer-canonical paths), correct in principle. The `if not sym_name or not file_path:` guard still works (`_norm_path("")` → `""`, falsy). No avenue broken.

**#18 lexical ingress | anchor_select.py:327 (`select_anchors`) | VERIFIED | Integration | reason:** `lex_files = {_norm_path(h.file) for h in lex_hits}` runs the lexical keys through the same normalizer so the merge-upgrade at lines 340/352 (`if fp in anchor_map`) compares like-for-like. **Inline comment is slightly inaccurate** ("hybrid.py forward-slashes h.file") — for `lexical_file_search`, `SignalHit.file = rel_path` is RAW `graph_file_paths` output (hybrid.py:175, `str(r[0])`, no normalize) or `_walk_text_files` `.as_posix()`; it is NOT pre-forward-slashed by hybrid. This makes the comment's rationale wrong but the fix MORE correct than claimed (it also fixes backslashes the comment assumes were gone). Behavior is correct; only the comment over-credits hybrid. No avenue broken.

**#18 normalizer | anchor_select.py:32-45 (`_norm_path`) | VERIFIED | Logic | reason:** `path.replace("\\","/").lstrip("./").lstrip("/")` is byte-identical to the canonical normalizer at `v7_4_brief.py:548` (and :884-885, :891-892, :1000) — same `lstrip("./")` char-set quirk (mangles a leading-dot dotfile e.g. `.hidden`→`hidden`), but identical everywhere, so it does NOT introduce a NEW inconsistency. Verified `r"src\foo.py"`→`"src/foo.py"`, `"./src/foo.py"`→`"src/foo.py"`. On-contract.

**#47 dead-code delete | anchor_select.py:13-17 (docstring) + :349-453 (`structural_seed_expand` + `_STRUCT_SEED_*`/`_EDGE_TYPE_WEIGHT`) | VERIFIED | Integration | reason:** The H1-falsified, zero-caller function and its 4 support constants are deleted AND the "v7.5 H1 — structural seed expansion" docstring paragraph is struck. Confirmed ZERO residual callers in `src/` (grep: no matches) and `tests/`; `tests/pretask/test_anchor_select.py:170-182` was already updated to ASSERT the removal (`not hasattr(...structural_seed_expand/_STRUCT_SEED_K/_EDGE_TYPE_WEIGHT)` + docstring no longer mentions it/`GRAPH_MISS`). `select_anchors` return tuple (`anchors, sem_seed_scores, sem_all_scores`) unchanged and never invoked the function, so nothing in the live pipeline (v7_4_brief.py:758-766) is affected. No avenue broken.

**Collateral check | VERIFIED:** Deleting the only `math.tanh` user did NOT orphan `import math` (still used at line 273 `math.isfinite`). File AST-parses clean. No undefined names, no half-applied rename, no signature/return-type change (so no v1r-style bare-str/tuple consumer mismatch — the `_localization_header` failure class does NOT apply: no return type changed, all callers untouched).

**COMMIT.** Both fixes are complete and correct across all four avenues. The path-norm fix is no-op-or-correct on every path because the Go indexer guarantees canonical `nodes.file_path` (no Windows-merge regression possible). The dead-code deletion is fully clean with tests already updated. **The only blemish is cosmetic:** the lexical-ingress comment at :324-326 misattributes forward-slashing to `hybrid.py` when `lexical_file_search` actually returns raw paths — fix the comment, but it does not affect correctness and does not block the commit. Note (out of scope, not in this diff): findings #2 (asymmetric trust gate — symbol/lexical pipes still hard-set `trusted_for_expansion=True` at :342/348/354/362, semantic gated by `tau_anchor` at :336) and #3 (over-permissive `sym_parts <= issue_parts`) remain UNADDRESSED — the diff does not touch the trust-gate logic.

---

All confirmed. Exactly ONE `lexical_file_search` call remains (L831); the second is gone (`_lex_hits = _lex_candidates`, L912). No orphan, no undefined name, parse OK. `_lex_candidates` feeds seeding (L835), scoring (L912), and BOTH diagnostics (L1124 top20, L1143 bm25_raw). All four hunks verified across all four LIPI avenues.

---

# LIPI VERDICT — `src/groundtruth/pretask/v7_4_brief.py` (4 hunks, items #21, #46, #19, #20)

**#21 (BM25 single-pass — seed call) | v7_4_brief.py:831-836 | VERIFIED | Integration/Plumbing | The one surviving `lexical_file_search` call now uses `max(50, len(candidate_set))`. Verified against `hybrid.py:266-282`: df/idf is whole-corpus (`n_docs=len(docs)`), independent of `max_files`; results are `ranked.sort(desc)[:max_files]`. So `max_files` only truncates the tail — per-file BM25 scores and the global top-10 (`_lex_top_paths`, L835) are byte-identical to the old `max(20,…)` slice → candidate-seed set unchanged (the comment's "strict superset" claim is exact). No avenue broken.**

**#21 (BM25 single-pass — scoring reuse) | v7_4_brief.py:912 | VERIFIED | Integration | `_lex_hits = _lex_candidates` replaces the deleted second `lexical_file_search(max(50, len(all_files)))`. `_max_lex` (L914) = `ranked[0].score`, identical in the 20- and 50-slice (both contain rank-1), so the `lex` normalizer is unchanged for previously-scored files AND is now the SAME object that feeds the `bm25_raw` diagnostic (L1124→L1143) and `gold_in_bm25_top20` (L1130). Diagnostic and score can no longer disagree — the cross-wire is closed. Old cap was `len(all_files)` vs new `len(candidate_set)`; since `all_files == list(candidate_set)` (L898) and only the tail past rank-50 is affected, no scored file changes. Not broken.**

**#46 (drop sem fallback) | v7_4_brief.py:958 | VERIFIED | Logic | `sem_component_scores = sem_all` (was `sem_all if sem_all else sem_scores`). Confirmed against `anchor_select.py:267-275`: `sem_all` (score_all=True) keeps only `score > 0.0`; `sem_scores` (seed, `ranked[:k_sem_top]`) is UNFILTERED for positivity. So the old fallback could substitute a bounded seed map holding non-positive/top-k-only cosines → spurious-0 outside top-k, the exact bug the decoupling killed. Empty `sem_all` now ⇒ component 0 everywhere = correct no-op (= embedder-off). Consumed ONLY at L968 by `_score_variant_C` (C/D). Ablation A (L960) and B (L963) use `sem_scores` directly — UNAFFECTED, so the "ablations keep the seed map" invariant holds. Not broken.**

**#19 (max-normalize path/frame/code_def) | v7_4_brief.py:1021-1027 | VERIFIED | Plumbing/Logic | Loop divides each of `path`/`frame`/`code_def` by its own observed max (skip when `_cmax==0` ⇒ correct-or-quiet no-op on absent signal; `if _v:` guard avoids touching zeros). Mirrors lex (L914-917) and reach (L924-936). Downstream consumers exhaustively checked: `_total_score` linear sum (L447-452) — the intended fix; `_rrf_fuse` (L500-501) — rank-based, invariant to monotone scaling (comment's "RRF scale-invariant" claim correct); RankedFile `components` (L1098) — rounded observability copy, no magnitude-dependent logic. `hub_pen` is NOT normalized (correctly excluded — it's already tanh∈[0,1) and used raw by both paths). `path_scores`/`frame_scores`/`code_def_scores` source dicts are untouched, so the L1144 `path_score` diagnostic is unaffected. Not broken.**

**#20 (RRF hub demotion) | v7_4_brief.py:1041-1053 | VERIFIED | Integration | `_w_hub_rrf = min(W_HUB_MAX, effective_weights.get("W_HUB", 0))` is byte-identical to `_total_score`'s `w_hub` (L454) — same clamp, same dict, same default → both paths defend identically (comment's claim verified). `_hub_demote` multiplies RRF score by `max(0.0, 1.0 - _w_hub_rrf*hub_pen)`: `hub_pen==0` (non-hub, or hub_penalties empty when ablation∉C/D) ⇒ factor 1.0 ⇒ exact no-op; monotone demotion of hubs, never negative. Applied to BOTH RRF branches (full L1050, det L1053). `hub_pen` is populated in `components_map` by `_score_variant_C` (L432) for C/D and read via `.get("hub_pen", 0.0)` (safe default). `_RRF_SIGNALS_FULL/_DET` (L480-481) correctly EXCLUDE `hub_pen` → no double-count. Not broken.**

---

**Cross-avenue regression sweep:** No undefined names; file parses clean (`ast.parse` OK). Exactly one `lexical_file_search` call remains (L831) — the second is fully removed, no orphan. The signature/return type of `run_v74` is unchanged; no caller/consumer contract is touched (this is purely internal scoring-pipeline rewiring — none of the four hunks change a function signature, return type, or the `V74BriefResult` shape). The feared `_localization_header→tuple`-class bug (bare-str return left behind callers expecting str) does **not** apply here — no return type changed.

**COMMIT.** All four hunks are VERIFIED across Logic / Implementation / Integration / Plumbing, edits are COMPLETE (every consumer of each changed value checked: `_total_score`, `_rrf_fuse`, the diagnostics, the RankedFile records, and the A/B ablation branches), and none breaks another avenue.

---

File compiles. Now let me re-examine the #31 finding once more to be certain — specifically whether the EXISTING band-2 conditions (`s.startswith(("[FOCUS:", "[Progress:"))` and `"ego" in s.lower()[:6]`) ever matched the real ego block before, which would mean my "still broken" verdict needs nuance. The real first line is `set_fields() in importer.py:142` — it does NOT start with `[FOCUS:` or `[Progress:`, and `s.lower()[:6]` = `set_fi` which does not contain `"ego"`. So before the fix, the ego block returned 5. After the fix, the new regex clause is the only new path — and I proved it returns False on the multi-line block. So the ego block STILL returns 5. The fix does not change behavior on the real ego block.

Let me also double check the basename in the first line: `_basename` returns `os.path.basename(path)`, e.g. `importer.py`. The regex segment after ` in ` is `.+:\d+` — `.+` matches `importer.py` greedily then backtracks to leave `:142` for `:\d+`. On a single line this works (proven True for `single`). The ONLY failure is the multi-line `$` anchoring. My verdict stands: BROKEN on the real (multi-line) input.

Let me verify there's no scenario where `_ego_text` is a single line (which would make the regex match). The ego block fires only when `len(_eg.callers) > 0` (line 1172), and `render()` always emits `parts[0]` (center line) PLUS a "Called by:" header PLUS ≥1 caller line when callers exist. So `_ego_text` is always ≥3 lines when it fires. The defensive filtering loop (1197-1212) could in principle strip lines, but it only strips test lines and the center line is never a test line, and "Called by:" + non-test callers remain (the homonym/center guard already passed). So in the firing path the block is always multi-line. The single-line case never occurs. Confirmed BROKEN.

Final consolidated verdicts below.

---

# LIPI VERDICTS — post_view.py squash hunks

Method: `git diff` read; surrounding code (returns, callers, symmetric twins, the SQL column/alias plumbing) read; `_is_stdlib_shadow` and ego `render()` signatures cross-checked; regex semantics and byte-compile checked read-only. No edits, no tests run.

**#31 — ego-block trim priority | post_view.py:48, 69-73 (called at 110, on `out` built at 1214→capped at 1265) | BROKEN | Logic/Implementation | The regex never matches the real input, so the fix is inert.**
The ego block is inserted into `out` as ONE multi-line element (`out.insert(0, _ego_text)`, 1214; `_ego_text = _eg.render()` = `"\n".join(parts)`, ego.py:148). `_l3b_line_priority` receives that whole multi-line string and runs `_EGO_FIRST_LINE_RE.match(s)` (72). With pattern `^[\w.]+\(\) in .+:\d+$` and NO `re.MULTILINE`, `$` only anchors at end-of-string or before a TRAILING newline — not before the mid-string `\n` that separates the header from `  sig:`/`Called by:`. Verified empirically: `match("set_fields() in importer.py:142\n  sig: ...")` → **False**; only the bare single line matches. The ego block fires solely when `len(_eg.callers)>0` (1172), so render() is always ≥3 lines → the regex always fails → ego still returns band 5 and is trimmed FIRST. The pre-existing `[FOCUS:`/`"ego" in s.lower()[:6]` clauses also never match (`set_fi` has no "ego"). **Net: zero behavioral change; the stated intent (lift ego to band 2) is not achieved.** Exact missing piece: match against the FIRST line only — e.g. `_EGO_FIRST_LINE_RE.match(s.split("\n", 1)[0])` (or `s.splitlines()[0] if s else ""`), or anchor with `re.match(r"^[\w.]+\(\) in .+:\d+", s)` (drop `$`), or compile with `re.MULTILINE`. Does NOT break the other 3 avenues (it's simply a no-op as written; no regression, but no fix).

**#32 — hub-scale single edge population | post_view.py:638-664 (`_in_degree_for_file`), 935-943 (`all_degrees`/`_hub_penalized_score`) | VERIFIED | Integration | All three populations now share `_ef`, alias-consistent.**
`_ef = _edge_filter(db_path)` (796) defaults to alias `e`. Numerator (804), `all_degrees` (936-937), and `_in_degree_for_file(cur, fp, edge_filter=_ef)` (942) all reference the `e` alias and append `AND {_ef}` to well-formed `FROM edges e`/`JOIN edges e` queries. `_in_degree_for_file` is called at exactly ONE live site (942) with `edge_filter=_ef`; its literal default (`confidence>=0.7`) is now only a fallback for absent callers (none exist — verified by grep: def + 1 call). Plumbing/Logic/Implementation intact; no other avenue broken.

**#33 — `_test_file_targets` stdlib-shadow guard | post_view.py:1398-1447 | VERIFIED | Integration/Plumbing | 3-col SELECT, 3-tuple unpack, correct-or-quiet guard, correct `_is_stdlib_shadow(code, name)` arg order.**
SELECT now returns `(nt.name, nt.file_path, e.source_line)`; comprehension unpacks `for name, fpath, src_line in rows` (1445) — arity matches. `_target_is_stdlib_shadow` reads the call line from the TEST file (`nsrc.is_test=1`, `_test_src_lines` from `test_file_path`) at `e.source_line` and calls `_is_stdlib_shadow(line_text, name)` — matches the def `_is_stdlib_shadow(code, target_name)` (v1r_brief.py:467) and the caller-twin's order (843). Correct-or-quiet: import-fail / unreadable / bad line → False → target KEPT (no over-suppression). No avenue broken.

**#34 — contract-pillar flows bound by node id | post_view.py:235/238 (SELECT +id), 295/305 (`_delivered_ids`), 321-340 (flows by `p.node_id=?`) | VERIFIED | Plumbing | id threaded end-to-end; homonym cross-staple eliminated; scoping preserved.**
Both SELECT branches add `id` as the last column; `_relevance` still keys on `r[0]`=name (correct). Loop unpacks `for name, sig, ret, _nid in ranked` (295) — 4-tuple matches the 4-col SELECT. `_delivered_ids.append(_nid)` (305) carries the EXACT node; flows query binds `(_nid,)` to `p.node_id = ?` (330-334), dropping the old `n.name`/`n.file_path` join. Because `_nid` came from a SELECT scoped to the viewed file, file-scoping is preserved through the id and made MORE precise (no overload cross-staple). `properties` columns used (node_id/kind/value/confidence/line) all pre-existed in the old query. No avenue broken.

**#9 — `_file_function_spec` correct-or-quiet | post_view.py:1295-1345 (anchors/relevance), 1349-1350 (suppress), 1362 (iterate `ranked`) | VERIFIED | Logic | Mirrors `_contract_pillar`; consumer str-contract honored; the key `rows`→`ranked` swap is applied.**
Adds anchor front-load (`CASE ... THEN 0`, 1320-1324) and `_spec_relevance` (keys on `r[0]`=name, 1337-1343), `ranked = sorted(...)` (1345), and the `if (_anchor_syms or _issue_terms) and _spec_relevance(ranked[0])==0: return ""` suppression (1349) — exactly the contract-pillar pattern. Crucially the emit loop now iterates `ranked` (1362), not stale `rows`, so `specs[0]`/`"Spec: "+specs[0]` (1384) is the relevance top, not file-top-by-start_line. All 4 return paths return `str` (`""` ×4, `"Spec: "+...`); sole consumer (1506-1508) does `if spec_line: nav_lines.append(spec_line)` — str expected, no tuple/str mismatch (the `_localization_header` class of bug does not recur). `_load_issue_terms()`/`_load_issue_anchors()` no-arg calls are valid (defaults). No avenue broken.

---

**COMMIT/REVERT RECOMMENDATION:** #32, #33, #34, #9 are VERIFIED — commit. **#31 is BROKEN (inert no-op due to multi-line `$` anchoring) — do not claim it fixed; either revert the #31 hunk or repair it by matching the first line only (`_EGO_FIRST_LINE_RE.match(s.split("\n",1)[0])` or drop the trailing `$`).** #31 introduces no regression (it is purely dead), so it is safe to leave in only if you accept it does nothing; to actually deliver the intent it must be corrected. File byte-compiles clean.

---

All consumers are within post_edit.py and accounted for. The `verified` field is consumed only at the render site (3279). The override chain dict (`method`/`file`/`signature`/`class`) is unchanged — no `verified` field added there, and its render (3302-3304) is `[OVERRIDE]` unconditionally. That's acceptable: the override chain is NOW gated, so every walked edge is verified by construction → `[OVERRIDE]` is always a fact. No need for a `[OVERRIDE?]` variant. INTEGRATION consistent.

Now let me verify there's no syntax error introduced by compiling the file (read-only static check via py_compile is allowed as a verification, but the prompt says "do NOT run anything"). I'll instead rely on the reads. Let me do one final check: confirm the diff's `_get_interface_peers_from_graph` still returns the same shape and that the `else` branch fall-through to `_get_name_match_peers` (1321, 1365) means peers can be a MIX is impossible — it's either verified peers (all "verified"="1") OR name_match peers (all "verified"="0"), never mixed. The render handles each per-row via `peer.get("verified","1")`, so even a hypothetical mix renders correctly. ✓

Let me also confirm `_STRONG_TRUST_TIERS` and `_SUPPRESSED_TRUST_TIER` (used by the new hierarchy clause) are defined before line 219. From my read: 147-148 define them. The new helper at 219+ references them — defined-before-use ✓.

I have enough to render all verdicts. Let me do a final confirmation that no caller outside post_edit imports these gating helpers (none — confirmed above), and verify the file has no obvious unbalanced f-string in the new clause by re-reading the clause output construction once more (already read 236-244, balanced parens). 

All four squash hunks verified. Final verdicts below.

---

# LIPI VERIFICATION — `src/groundtruth/hooks/post_edit.py` (4 squash hunks)

All changes confirmed against the diff, the consumers, the symmetric twins, the Go resolver provenance strings (`relationships.go`), and `curation_map.py`'s `DETERMINISTIC_RESOLUTION_METHODS`. **Verdict: COMMIT all four. No avenue broken.**

---

**#10 (consistency-edge gate: `_HIERARCHY_VERIFIED_METHODS` + `_hierarchy_edge_filter_clause/_for_db`, applied to peers + override) | post_edit.py:207-265, 1294-1358, 1505-1514, 3279-3289 | VERIFIED | Logic/Implementation/Integration/Plumbing**
- LOGIC ✓: `_HIERARCHY_VERIFIED_METHODS = {"inheritance","implements"} | DETERMINISTIC_RESOLUTION_METHODS` is exactly right — `curation_map.py:83-97` confirms the CALLS fact-set omits both hierarchy methods on purpose; the EXTENDS/IMPLEMENTS edge IS the relating fact for peers/override so it must trust those two provenances. Go resolver confirms `EXTENDS→inheritance@1.0` and `IMPLEMENTS→implements@0.8-1.0` (`resolver/relationships.go:148/190/209/266/284/317/332/362`). Gate admits method-match OR strong-tier-not-name_match, excludes SUPPRESSED — mirrors the caller gate.
- IMPLEMENTATION ✓: complete — gate threaded into all 4 edge-traversing sites (count 1298, parent_edges 1334, siblings 1351, override recursion 1513); all alias `edges e` matching the clause default `alias="e"`. New `verified="1"`/`"0"` set on BOTH peer producers (1400 verified path, 1473 name_match fallback); renderer reads `peer.get("verified","1")` (default-safe) → `[PEER]` vs `[PEER?]` + verify note (3279-3289). Helpers defined before use; `_STRONG_TRUST_TIERS`/`_SUPPRESSED_TRUST_TIER` defined at 147-148.
- INTEGRATION ✓: never looser than pre-squash — parent_edges and override recursion were previously UNFILTERED, now gated (strictly tighter); count/siblings were `confidence>=0.5`, now categorical on modern schema / identical `>=0.5` on legacy. Override dicts unchanged and always-`[OVERRIDE]` is correct since every walked edge is now gated.
- PLUMBING ✓: legacy-schema fallback `confidence>=0.5` (265) admits all 0.8-1.0 hierarchy edges → no real loss.
- Does NOT break the other 3.
- SCOPE NOTE (not a break): report #10 names FOUR consistency queries incl. twins (`_find_same_name_twins`:1587) and siblings (`_get_siblings_from_graph`:1189); the squash gated only the two that traverse EXTENDS/IMPLEMENTS **edges**. Twins relate by `same_file`/`parent_id==parent_id` and siblings by `parent_id=?`/`file_path=?` — node-identity facts the indexer wrote, NOT name_match edges, so there is no edge to launder and no gate applies. Defensible partial scope; the inconsistent-FILTER class (the one that could launder a name_match edge) is fully closed.

**#11 (behavioral-contract resolver → `_resolve_node_id`) | post_edit.py:2805-2828 | VERIFIED | Plumbing/Implementation**
- LOGIC ✓: replaces the inline `WHERE name=?` (no label filter, `>`-tiebreak by iteration order) with the canonical `_resolve_node_id` (filters `label IN ('Function','Method')`, suffix path-match, is_exported→lowest-id tiebreak, returns None on no match — HEAD:288-330). Contract now reads the SAME node as callers/signature/callee blocks by construction.
- IMPLEMENTATION ✓: COMPLETE — `func_start/func_end/_bc_node_id` all pre-initialized to None (2796-2799); set only on success; `if func_start is None` no-node log preserved (2827). EVERY downstream consumer verified consistent: `func_start/func_end` at 2832/2837/3043 all guarded by `if func_start and func_end`; `_bc_node_id` at 2854/2859/2961/3009/3366/3375 all guarded `if _bc_node_id is not None`. Lines 4151-4154 are a different AST-scope function, unaffected.
- INTEGRATION ✓: matches the resolver the rest of the contract pipeline already uses (e.g. callee 3157, siblings 1197). PLUMBING ✓: single `SELECT start_line,end_line WHERE id=?` on the resolved id.
- Does NOT break the other 3.

**#12 (callee `Calls into:` skip-when-path-None) | post_edit.py:3162-3186 | VERIFIED | Plumbing**
- LOGIC ✓: `_resolve_file_path` is documented to return None on unknown/ambiguous path (58-69); binding `nt.file_path != ?` with None makes `!= NULL` → NULL (never true) for every row, disabling self-exclusion → edited file's own funcs leak as callees. Fix skips the block when None (correct-or-quiet).
- IMPLEMENTATION ✓: `_callees=[]` default; conn closed in BOTH branches (3171 None, 3186 query); `if _callees:` (3187) stays False on the None path. No double-close, no leak.
- INTEGRATION/PLUMBING ✓: source side bound by `resolved_target_id` (a node id, gated by `if resolved_target_id` at 3158) is unaffected; only the path bind was the hazard.
- Does NOT break the other 3.
- AUDIT RESIDUAL (benign, not in #12 scope): the same `!= ?`-with-None pattern exists at line 464 (`n2.file_path != ?` bound with `_resolved_eh`), but there `n1.file_path = ?` is ALSO bound to the same None → `= NULL` → 0 rows → query returns empty, not self-polluted. Not exploitable; not a break. Line 1444 (name_match peers) is already guarded by `if _resolved_peer is None: return []` (1432). Joined-column `!=` at 633/1108/633 are NULL-safe (both from the join).

**#13 (`_signature_has_varargs` token-aware) | post_edit.py:1966-1989 | VERIFIED | Logic**
- LOGIC ✓: replaces `"*" in signature` (matches keyword-only `*,`, PEP-3102 markers, `*` in hints/defaults — falsely suppressing the arity check) with inner-param extraction + `re.search(r"\*\*?\w", inner)` — matches `*args`/`**kwargs` (star(s) followed by a word char) but NOT bare `*,`/`*)`. Traced: `f(a,*,b)`→False, `f(a,*args)`→True, `f(**kw)`→True. Correct intent: don't suppress arity on keyword-only sigs.
- IMPLEMENTATION ✓: `re` imported at module level (24); inner-paren regex is IDENTICAL to the twin `_signature_param_count` (1955) → both degrade the same way on nested-paren defaults (no divergence introduced). Caller `_check_arity_mismatch:2051` uses it as an early-suppress guard — semantics preserved.
- INTEGRATION/PLUMBING ✓: pure structural string parse, no DB/path.
- Does NOT break the other 3.
- EDGE NOTE (benign): PEP-646 `*Ts` in an annotation (`Tuple[int,*Ts]`) would match and over-suppress — but that errs toward silence (correct-or-quiet), is rare, and is strictly safer than the old over-broad suppression. Not a break.

---

**Bottom line:** All four squash hunks (#10, #11, #12, #13) are LOGIC-correct, IMPLEMENTATION-complete (every return/consumer/twin updated), INTEGRATION-consistent with their symmetric gates, and PLUMBING-correct (right columns, None-safe binds, alias-matched SQL). None breaks another avenue. **COMMIT.** The only two open items are non-breaking observations: (1) #10 leaves twins/siblings ungated by design (they relate via node-identity, not launderable edges); (2) a benign self-masking `!= NULL` residual at line 464 (`_localization_header`-style block) outside #12's scope.

---

The test confirms the #29 corrected case precisely: edge 100 currently targets `helper` (id 2); when LSP definition lands at line 55 (inside `renamed`, id 3, lines 50-60) with `target_name="helper"` (the STALE pre-rename name), the OLD query `WHERE file_path=? AND name=?` would filter `name='helper'` and never find node id 3 (named "renamed") → fall to DELETE. The NEW query matches by window (50≤55≤60) → finds id 3 → corrects target_id 2→3. This is exactly the bug #29 describes and the fix is correct.

I have verified all three hunks across all four LIPI avenues with direct code reads. Final verdict.

---

**LIPI VERIFICATION — `src/groundtruth/resolve.py` (3 hunks, squash items #28/#29/#30)**

**#30 — dispatch tables derived from one source | resolve.py:49-110 | VERIFIED | Integration/Plumbing**
`_build_lang_to_ext`/`_build_known_servers`/`_build_ext_to_lang_id` derive from `lsp/config.py` (`LSP_SERVERS` keys 22-30, `LANGUAGE_IDS` 32-41). Runtime-confirmed: c/cpp/ruby/kotlin GONE; `_KNOWN_SERVERS.keys() == _LANG_TO_EXT.keys()` (so the resolve_main gate `servers.get(args.lang)` @952 and the ext lookup `_LANG_TO_EXT.get(language)` @419 can never disagree — closes the "gate passes, then get_server_config Err → silent full no-op" wound the comment cites); every `_LANG_TO_EXT` value is a real `LSP_SERVERS` key (no `.python` fall-through). Plumbing checked: both tables carry language-NAME keys (`python`,`go`,…) AND short ext aliases (`py`,`ts`); consumers key on `nodes.language`, which real DBs store as names (django.db→`python`/`javascript`, deepswe→`go`) and `args.lang` is a name — match holds. `_EXT_TO_LANG_ID`=`dict(LANGUAGE_IDS)`; `_lang_id_for_ext` @113 falls back to `ext.lstrip(".")` (unchanged). No external importer of these three names (only `background_promotion.py` imports `_get_ambiguous_edges`/`_resolve_edges`). Does NOT break the other 3 avenues.

**#29 — match by (file, line-window), name is tiebreaker | resolve.py:336-343 | VERIFIED | Implementation**
Query dropped `AND name = ?` gate; `name` moved to `ORDER BY (name = ?) DESC, start_line DESC` (SQLite evaluates `(name=?)` as 1/0, DESC ranks the name-match first — correct tiebreaker). Window `start_line <= line <= end_line OR end_line IS NULL` unchanged. Exact `file_path = ?` (not `LIKE '%basename'`) — consistent with the pre-existing `target_rel` normalization (`os.path.relpath(...).replace("\\","/")` @598, identical to old code → no new path-normalization mismatch). Implementation COMPLETE: every return path present; `edge["target_id"]`/`edge["id"]` exist in the `_get_ambiguous_edges` SELECT (@201). Confirmed against the test's corrected case (stale `name="helper"`, LSP lands in node `renamed` id 3 via window → corrects 2→3 instead of the old destructive DELETE). Does NOT break the other 3 avenues.

**#28 — destructive-delete guard | resolve.py:362-383 | VERIFIED | Logic**
On window miss: DELETE fires ONLY when (a) `target_rel` is NOT external (not-empty AND not `..`-escaped AND not absolute via `os.path.isabs`) AND (b) the file IS indexed (`SELECT 1 FROM nodes WHERE file_path=?`). External (empty/`..`/abs — the stdlib `join/loads` case) → `skipped`, edge preserved (correct-or-quiet). Indexed-but-no-window-node → `deleted` (genuine FP). Not-indexed (vendored/generated, incl. NULL-end_line drift) → `skipped`. `os` imported @17. Matches the test cases (`../../usr/lib/...`→skip, `C:/Python311/...`→skip, `vendored.py` unindexed→skip, `other.py`/indexed-miss→delete). Does NOT break the other 3 avenues.

**Extraction integrity (cross-cutting):** `_apply_lsp_resolution` is pure/synchronous; production call @606-614 passes identical kwargs; `conn.row_factory=sqlite3.Row` set in `_resolve_edges` @513 before the loop (function re-sets it @336 defensively — harmless, `edges` already materialized as a list so mid-iteration row_factory change is safe); `_has_trust_tier` threaded @613→`has_trust_tier`; `_tier_clause` logic byte-identical to old inline code. Verify/correct UPDATE statements unchanged. Same logic now shared with `tests/pretask/test_resolve_lsp_match.py`.

**VERDICT: all 3 hunks VERIFIED — COMMIT.** No BROKEN avenue found; no half-applied rename, undefined name, type mismatch, or consumer left expecting the old shape. (Out of scope but noted from the reference file: D3 — the `LSP_METRICS resolved`/`deleted` denominator @966 is a separate CI-gate-semantics decision, untouched by these hunks.)

---

AST parses clean. All hunks verified. Final verdict per fix.

---

# LIPI VERIFICATION — `benchmarks/swebench/gt_intel.py` (9 hunks against the squash-list intents)

Diff: 193 insertions, 84 deletions. AST parses clean. Each fix maps to a numbered squash-list item. Every consumer/twin traced.

**#7 (get_callers → deterministic-only gate) | gt_intel.py:316-318, 324 | VERIFIED | Integration** — Swaps `_resolution_sql_in()` → `_deterministic_sql_in()` + `if not methods: return []`, byte-for-byte identical to the symmetric twin `get_callees` (1387-1389). `VERIFIED_RESOLUTIONS ∩ _DETERMINISTIC_RESOLUTIONS = {same_file, import}` (non-empty), so the empty-guard never fires spuriously; bind order `(target_id, target_file, *methods)` matches placeholders; cross-file `source_file != ?` preserved. Resolves D1 toward facts-only. Existing test `test_low_confidence_edge_filtered` stays green (the `render→validate_input` fixture edge is `import`=deterministic → survives; `format_value` is `name_match` → excluded as intended). Breaks no other avenue.

**#8 (get_siblings → same-file constraint) | gt_intel.py:351-360 | VERIFIED | Plumbing** — `SELECT parent_id, file_path`; guard `not row[0]` (parent_id) unchanged; unpack `parent_id, target_file = row[0], row[1]`; adds `AND file_path=?` bound `(parent_id, target_id, target_file)` in correct placeholder order. `SELECT *` shape unchanged → `_row_to_node(r)` still valid. Feeds the #24 return-type vote a single-file class. Breaks nothing.

**#22 (classify_caller_usage → single-call-line read) | gt_intel.py:410, 419, 422-437 | VERIFIED | Implementation** — `read_lines(root, file, call_line, call_line)` returns exactly `lines[call_line-1:call_line]` (confirmed at read_lines:224) → `.strip()` = the bare call line, killing the old off-by-one (`lines[min(1,len-1)]` picked window-line-2 = line 2 when call_line==1). All classification regexes now run against `line` (single call line), not the 4-line `text` window → no neighbor-line score inflation. Empty-guard returns the 4-tuple `(1,"invokes","","invoke")`. Signature `tuple[int,str,str]`→`tuple[int,str,str,str]`.

**#22/#25 signature propagation | gt_intel.py:1505 (in-module) VERIFIED; gt_v2_hooks.py:129 + gt_v2_bridge.py:235 NOT UPDATED | Implementation/Integration | PARTIAL** — The in-module consumer (compute_evidence:1505) correctly unpacks the new 4-tuple `score, summary, call_text, usage`. BUT two live in-package consumers were left unupdated: `benchmarks/swebench/gt_v2_hooks.py:129` and `gt_v2_bridge.py:235` both do `score, summary = gt_intel.classify_caller_usage(...)` (2-target unpack) — reachable via `agent.py`/`runner._init_gt_v2_pull` (GROUNDTRUTH_V2_PULL mode). NOT a regression introduced by this edit: at HEAD the function was already a 3-tuple, so `score, summary = (3-tuple)` already raised `ValueError: too many values to unpack (expected 2)`; 3→4 leaves that identical pre-existing failure. The edit does NOT make it worse, but the signature change is INCOMPLETE per the full-propagation rule — those two sites stay broken (latent, in a stale V2 path). Recommend a 1-line fix each (`score, summary, *_ = ...`) but it does not block this commit since it neither introduces nor worsens breakage.

**#23 (_format_import_for_language → in-file package decl) | gt_intel.py:233-262, 1401-1452, 1486 | VERIFIED | Logic** — Old nested function (dirname-fabricated `import "internal/foo"` / `import src.main.Foo` / `use a::b`) fully removed (exactly 1 def remains). New module-level fn reads the real `package`/`namespace` from the file header via `_read_package_decl` (regex map keyed go/java/kotlin/csharp/php), falling back to neutral `{name} (from {path})`; Python/JS/TS legitimately path-based and unchanged; Rust→neutral. `os`/`re` imported (29-30). Call site updated to pass `root` (1486). java/kotlin branch passes `language` and both keys exist in `_PKG_DECL_PATTERNS`. No hallucinated copy-pasteable import survives. Breaks nothing.

**#24 (SIBLING return-type vote denominator) | gt_intel.py:1539-1546 | VERIFIED | Logic** — `if ret_types:` → `if len(ret_types) >= 2:` (min-support floor); ratio denominator `max(len(siblings),1)` → `len(ret_types)` (numerator and denominator now share the typed-only universe); summary text updated to `.../len(ret_types) typed siblings agree`. Fixes the dilution bug (untyped methods no longer suppress a unanimous-among-typed contract) and the 1-sibling-trivially-100% bug. The pre-existing `candidates[-1]` hazard (upgrade is outside `if code:`, could touch a non-SIBLING node if `code` was empty) is UNCHANGED by this edit and actually narrowed (now needs ≥2 typed siblings) — not introduced here, out of scope. Breaks no avenue.

**#25 (TYPE upgrade via structured enum) | gt_intel.py:191-194 (field), 1514 (set), 1589-1590 (read) | VERIFIED | Implementation** — Adds `EvidenceNode.usage: str|None = None`; producer sets `usage="destructure"` (and the other enums) in classify_caller_usage; consumer branches `c.usage == "destructure"` instead of the permanently-False `"destruct" in c.summary` (the summary only ever says `"called as: ..."`). Now the TYPE→score-2 upgrade can actually fire. Field has a `None` default and ALL 8 `EvidenceNode(...)` constructions are keyword-based → backward-compatible; no `asdict`/`EvidenceNode(**r)`/dict round-trip exists in the live file (the only `EvidenceNode(**r)` is in a frozen `baseline_confirm_nolsp` bundle copy, out of scope). Breaks nothing.

**#26 (rank_and_select negative-spec word-boundary) | gt_intel.py:1779-1781 (regex), 1801-1804 | VERIFIED | Logic** — Old `any(kw in c.summary.lower() for kw in (...))` substring test → `_NEGATIVE_SPEC_RE.search(...)` with `\b(raises|error|exception|false|not)\b`. Same 5 terms; `\b` anchors correctly stop "not" matching note/notify/cannot/notification and "false" matching inside larger tokens. Applied only to `c.family == "TEST"` (unchanged). Minor recall narrowing on plurals ("errors"/"exceptions" no longer match the singular pattern) — aligned with the stated "as words" intent, acceptable, not broken. Breaks nothing.

**#27 (generate_pretask_briefing TEST query gate) | gt_intel.py:1162-1168, 1231 | VERIFIED | Integration** — TEST sub-query `IN ({res_methods})` → `IN ({det_methods})`, now matching the adjacent FIX-HERE (1191) and top-caller (1215) deterministic-only gate; the now-unused `res_methods = ...` assignment was deleted and has zero remaining executable references (grep: only a docstring mention at line 115) → no NameError. Resolves the name_match-IN asymmetry (phantom test link). Minor residual: the TEST query omits `{conf_clause}` that FIX-HERE/top-caller carry — but that confidence-floor gap is outside #27's stated scope (resolution-method symmetry), pre-existing, low harm. Breaks no avenue.

---

## BOTTOM LINE
**8 of 9 hunks: fully VERIFIED — commit-safe.** Logic, implementation, integration (twins consistent), and plumbing (bind order, columns, path) all correct; no undefined name, no half-rename, no off-by-one, no dead branch, no type mismatch introduced; AST clean.

**1 INCOMPLETE (does not block this commit): the classify_caller_usage 3→4-tuple signature change was not propagated to the two live in-package consumers** `benchmarks/swebench/gt_v2_hooks.py:129` and `benchmarks/swebench/gt_v2_bridge.py:235` (both `score, summary = ...`, 2-target unpack, reachable via GROUNDTRUTH_V2_PULL). These were ALREADY broken at HEAD (3-tuple vs 2-target unpack already raised `ValueError`), so this edit neither introduces nor worsens the failure — but to honor the full-propagation rule, follow up with `score, summary, *_ = gt_intel.classify_caller_usage(...)` at both sites. Verdict: **COMMIT** (the squash to gt_intel.py is sound); open a one-line follow-up for the two gt_v2 unpack sites.

---

All emit sites confirmed. Note one behavioral observation worth flagging on the central `tierFor` change: Strategy 1.98 `unique_method` was previously hardcoded `CANDIDATE` at conf 0.85 → `tierFor(0.85)` = CANDIDATE (unchanged). Strategy 1.97 `return_type` conf 0.85 was previously CERTIFIED → now CANDIDATE (the intended #1 fix: 0.85 can never be CERTIFIED). Strategy 1.93/1.94a/1.95/1.96 at conf 0.9/0.95 → CERTIFIED (unchanged, receiver-proven). This is exactly the intent.

I have all four avenues verified for all six hunks. Producing the verdict.

---

# LIPI VERDICT — gt-index/internal/resolver/resolver.go (6 hunks)

All six squash items VERIFIED. Diff is self-contained (400 lines, one file), compiles cleanly by inspection (all imports present, no undefined names, no leftover dead variables), and the test file already carries matching red→green assertions. Verdict: **COMMIT.**

**#1 central tierFor (squash-list FIX-NOW #1)** | resolver.go:301-309 + 18 emit sites | **VERIFIED** | Logic+Implementation+Integration | New `tierFor(conf)` exactly mirrors CLAUDE.md:222 (≥0.9 CERTIFIED / ≥0.5 CANDIDATE / else SPECULATIVE). Grep for `TrustTier: "CERTIFIED|CANDIDATE|SPECULATIVE"` literal returns ZERO matches → EVERY emit site converted; no half-applied rename. The only behavioral flip is the intended one: 1.97 return_type (0.85) and 1.98… (0.85) — 1.97 was CERTIFIED→now CANDIDATE (correct), 1.98 was already CANDIDATE (no change). Receiver-proven stages (1.93/1.94a/1.95/1.96 at 0.9-0.95) stay CERTIFIED. Does NOT break Plumbing: `Method`/`resolution_method` strings unchanged, so the Python consumer's `VERIFIED_RESOLUTIONS={same_file,import,name_match}` gate (gt_intel.py:58) is unaffected; tier is consumed categorically. Test helper `tierForConf` (resolver_test.go:500) is byte-identical to production `tierFor`.

**#5 1.94 impl_method cap (FIX-NOW #5)** | resolver.go:1071-1084,1113 | **VERIFIED** | Logic | 1-class conf 0.85→0.6, and the hardcoded `tier194` ("CERTIFIED" for 1 class) is fully removed → `tierFor(0.6)`=CANDIDATE. Premise confirmed by reading the guards: 1.94 fires only after `!qualifierIsClass` (line 1068) and after receiver-proven 1.93/1.94a/1.95 already ran — so the receiver type is genuinely unproven here; capping at CANDIDATE is correct (name-uniqueness ≠ receiver-proof). 2-class=0.5, 3-class=0.4 unchanged, both CANDIDATE/SPECULATIVE via tierFor as before. No dangling `tier194` reference (grep clean). Does NOT break others: target-pick logic untouched.

**#6 single-candidate builtin drop (FIX-NOW #6)** | resolver.go:861-874 | **VERIFIED** | Integration | The Strategy-1.9 single-candidate guard now tests `(strongBuiltinMethodNames[calleeName] || builtinMethodNames[calleeName])`, matching the broad set Strategy 2 (line 1344) applies on the multi-candidate path. A qualified `cfg.get()` with one global `get` def is now dropped instead of laundered to `name_match_qualified_unresolved`. Both sets are package-level vars defined above (452/472) — no undefined name. Consistent with its twin filter. Does NOT break Logic: unqualified calls (`qualifiedUnresolved==false`) skip the guard, so a legitimate same-named internal free function is unaffected.

**#39 Strategy 1 same-file overload (FIX-NOW #39)** | resolver.go:676-706 | **VERIFIED** | Logic+Implementation | On `len(targetIDs)>1` and `isUnqualified`, picks best LOCAL target via `pickBestLocalTarget` at conf 0.6/CANDIDATE/`same_file_ambiguous` instead of falling through to a cross-file name_match. `pickBestLocalTarget` (line 382) is deterministic (callable-label preference, then min-ID), excludes `callerID`, returns 0 when none eligible (then falls through — correct-or-quiet). `isUnqualified` correctly restricts to receiver-less calls so qualified calls still reach the type-flow strategies. `Method:"same_file"` keeps it in the consumer's recognized-fact set; conf 0.6 passes the ≥0.5 filter as a CANDIDATE fact. metaMap is non-nil on the production path (main.go:414/943 pass `BuildNodeMeta`, which populates `.Label`). Does NOT break Plumbing/Integration: new `same_file_ambiguous` EvidenceType is emit-only, no consumer string-matches it.

**#40 Strategy 1.5 import pick-best (FIX-NOW #40)** | resolver.go:768-797 + helper 343 | **VERIFIED** | Logic+Implementation | Replaces nondeterministic `importCandidates[0]` with `pickBestImportCandidate` (same-dir winner, else lexically-smallest (file,id)). When `>1` candidate and NO same-dir winner, demotes conf 1.0→0.6 + `ast_import_ambiguous`, so `tierFor` yields CANDIDATE not CERTIFIED on a coin-flip. Helper uses `filepath.ToSlash`/`filepath.Dir` (import present, line 8); both sides of the same-dir compare are ToSlash-normalized so the comparison is internally consistent (raw `call.File` vs `m.File` both normalized inside the helper). `.File` is populated in `BuildNodeMeta` (line 235). Single-candidate path keeps conf 1.0/CERTIFIED. `Method:"import"` unchanged → consumer fact-gate intact. Does NOT break the other avenues.

**#41 Strategy 1.93 import_type (FIX-NOW #41)** | resolver.go:925-933 | **VERIFIED** | Implementation+Integration | Dead `if sep == "::" { qualifier = …same slice… }` no-op removed (and `_ = sep` discard removed); `sep` is still live — read at line 929 `call.CalleeQualified[dotIdx+len(sep):]` — so removing the blank-assignment is safe and `sep` is not an unused variable. The `Self` guard added (`qualifier != "Self"`) now matches the four sibling strategies (1.75:813, 1.94:1054, 1.94a:997, 1.95:1139), closing the cross-strategy inconsistency where a Rust `Self::method()` slipping past 1.75 could mis-scope to an imported class literally named `Self`. Does NOT break Logic: `Self` is excluded from import-class lookup only; the legitimate `Self` path is 1.75.

**Cross-avenue / breakage check (all hunks):** No signature/return-type change to `Resolve` or `ResolvedCall` (unlike the v1r `_localization_header→tuple` class of bug) — every consumer of `ResolvedCall` reads the same fields. The two new EvidenceType strings (`same_file_ambiguous`, `ast_import_ambiguous`) and reused `single_implementor`/`name_match_qualified_unresolved` are write-only at the resolver boundary; the Python consumer (`gt_intel.py`) gates on `resolution_method`∈{same_file,import,name_match} + confidence thresholds, all of which the squash preserves. The only data-flow change is the intended demotion of three edge classes from CERTIFIED→CANDIDATE and one from conf-0.9-mislabeled-SPECULATIVE→conf-0.2-true-SPECULATIVE — strictly toward the "correct-or-quiet" / tier-follows-confidence contract. No undefined names, no half-applied rename, no off-by-one, no dead branch, no leftover removed-variable reference (grep clean for `tier194`/literal tiers). Production `Resolve` caller passes `nodeMeta` with `.File`/`.Label` populated, so the two new helpers operate on real data, not nil.

Note: `go`/`go vet` is not on this shell's PATH, so I could not compile; verdict is from read-level LIPI per the no-tests/no-run constraint. Nothing read indicates a compile or contract break.

---

Let me trace the two tricky test cases against the implemented logic to confirm the code actually produces these wanted values.

**`"plain function": "func Handle(count int) int" → ""`**: `strings.HasPrefix(s, "func (")`? The string is `"func Handle..."` — `"func ("` requires `func` + space + `(`. `"func H"` ≠ `"func ("`. HasPrefix false → returns `""`. Correct.

**`"generic func-typed param": "func (s *Service[K, func() error]) Do()" → "Service"`**: Traced earlier → "Service". Correct.

**`"generic two-arg": "func (m *Map[K, V]) Get(k K) V" → "Map"`**: scan from i=6. `m`, ` `, `*`, `M`,`a`,`p`, `[` depth1→2, `K`,`,`,` `,`V`, `]` depth2→1, `)` depth1→0 end. `s[6:end]`=`"m *Map[K, V]"`. fields=`["m","*Map[K,","V]"]`. len=3≥2 → typeTok=fields[1]="*Map[K,". trim `*`→"Map[K,". IndexByte `[`→cut→"Map". Correct.

All cases trace correctly. The test assertions match the new implementation. Now let me verify there are no OTHER callers of these functions outside the parser package (e.g., resolver or store importing them) — they're lowercase/unexported so package-private, but `goReceiverType` etc. are all lowercase = unexported, confirming no cross-package callers. The grep already confirmed only `parser.go` + the test reference them.

Final check: confirm the diff has no leftover/orphaned references to the OLD signatures anywhere in parser.go (e.g., a second `extractReturnShape(bodyNode,...)` call missed). The grep at lines 1862/1954/2384/3172 already showed exactly one caller each with the correct new arg count. 

I have verified every hunk across all 4 LIPI avenues. Producing the verdict.

---

**LIPI VERDICT — `gt-index/internal/parser/parser.go` (READ-ONLY; nothing edited/run)**

All hunks map to squash-list items #2, #42, #43, #44 plus the line-anchoring sub-fixes (data_flow / return_shape / fingerprint). Verdicts below.

**#42 goReceiverType paren-depth scan | parser.go:197-222 | VERIFIED | Logic**
depth tracks `()`+`[]`, stops at the `)` returning depth→0; skips inner `func()`/generic parens. Traced `func (s *Service[K, func() error]) Do()`→end at receiver's real `)`. Sole caller linkGoReceiverMethods:170 still gets a bare type name; empty-string contract preserved. No avenue broken.

**#42b goReceiverType type-token = fields[1] | parser.go:235-238 | VERIFIED | Logic**
Go grammar `<name> <type>`: `fields[1]` is the type; `len>=2` guard falls back to sole field when name omitted. Traced value/pointer/generic/2-arg/func-typed → all correct (`Account`,`RequiredResourceSelector`,`Stack`,`Map`,`Service`). Old `fields[last]` gave `error]` on func-typed receivers — now fixed. Consumer lookup unchanged.

**#43 Go excluded from capital=constructor (Heuristic 1) | parser.go:790 | VERIFIED | Logic/Plumbing**
`sf.Language != "go"` gate stops stamping `TypeName="Marshal"` (non-existent type) for Go exported funcs. Rust/Go `Type::new()` path (Heuristic 2, line 795) keys off `::` independent of the language gate — not affected. No false TYPE fact emitted.

**#43b Go cap-bare bridges via ViaReturn (else branch) | parser.go:824-826 | VERIFIED | Logic**
`isGoCapBare` added so `x := Marshal()` records `ViaReturn=true` with `TypeName="Marshal"` (bridge through return type) instead of being dropped. Guard `qualified=="" || qualified==simple` correctly skips `x := pkg.New()` (qualified `pkg.New` ≠ simple) → demand-driven residual, correct-or-quiet. The two halves (H1 exclude + else include) are consistent — Go cap-bare has exactly one path (ViaReturn), never both, never neither.

**#44 receiverRootIsLiteral chained/paren unwrap | parser.go:971, 998-1048 | VERIFIED | Logic/Integration**
Replaces depth-1 `isLiteralReceiver(recv.Type())` with recursive root-finder. Traced `"x".strip().split()`: outer recv = inner `call` → descend `fn.Child(0)` (the `"x".strip` attribute's receiver) → string literal → true. Paren-unwrap skips `(`/`)` tokens. Depth cap 16, nil-guarded, unknown shapes return false (keeps edge — correct-or-quiet). Child(0)=function-head convention matches the pre-existing extractCalleeInfo twin (line 956 loop) and `_walkCallOrdering`. No avenue broken.

**#2a data_flow line = first param use | parser.go:1774-1792, 1816-1824 | VERIFIED | Plumbing**
`collectFlowUses` gains `firstLine *int`, set once at first matched use (`*firstLine == 0` guard). Recursion threads it (1790). extractDataFlow seeds `firstLine:=0`, uses it when `>0` else body-start. Anchors the fact at the actual use, not body start. Only caller is extractDataFlow (1817); no other caller of changed signature.

**#2b extractReturnShape funcNode-anchored | parser.go:1862, 2384-2397 | VERIFIED | Plumbing**
Signature gains leading `funcNode`; sole caller (1862) passes `node` (the func node, non-nil in extractProperties). nil-guarded fallback to bodyNode (2395). return_shape is an aggregate fact → anchored at declaration. Complete: the one and only caller updated.

**#2c fingerprint funcNode-anchored | parser.go:3217 | VERIFIED | Plumbing**
Only the `Line:` value changed (`bodyNode`→`funcNode`); signature already had `funcNode`. No new nil-risk — `funcNode` is already dereferenced at 3195 before this line. Caller at 1954 passes non-nil `node`.

**#2d concurrency/config/call_order line via bodyOffsetLine | parser.go:4278-4284, 4339, 4357, 4479, 4511, 4541, 4566-4599, 4606-4682 | VERIFIED | Plumbing**
`bodyOffsetLine(bodyNode, bodyText, idx)` = body-start row + `\n`-count in `bodyText[:idx]`; bounds-guarded. Every passed `idx`/`sIdx` is a genuine `bodyText` byte-offset (verified at all 5 config/concurrency sites). Shared-state guard gained `sIdx>=0 &&` — cannot suppress any case `containsKeywordAtBoundary` admitted (boundary-match implies presence); only supplies the anchor. call_order: `receiverLine` map threaded through `_walkCallOrdering` (only caller 4569 + self-recursion 4680 both updated), first-call-wins (`!ok` guard), emit falls back to body-start. Minor residual imprecision (raw `strings.Index` may point at a pre-boundary substring occurrence) is consistent with the pre-existing lock-loop twin (4313/4317) and is still in-body — not a correctness break.

**Cross-avenue / completeness:** Every changed signature has exactly ONE non-recursive caller, all updated (greps at 1862/1954/4569 + recursion sites confirmed). The test file `parser_lipi_squash_test.go` calls only `goReceiverType` (unchanged arity) and references the others solely in comments — no stale old-signature call sites. All functions are unexported (package-private) — no cross-package callers exist. Consumer contracts (linkGoReceiverMethods bare-type lookup; PropertyRef.Line as 1-based absolute source line) preserved.

**OVERALL: COMMIT.** All hunks VERIFIED across Logic/Implementation/Integration/Plumbing; no hunk breaks another avenue; no incomplete edit (no orphaned old-signature caller, no undefined name, no type mismatch). The only non-correctness caveat is the call-order/concurrency raw-`Index` anchor possibly off by a line when a keyword first appears as a non-boundary substring — minor, in-body, and matches existing code; not a revert trigger.

---

I now have everything needed to render verdicts across all four LIPI avenues for each hunk. Let me compile the final verdict.

Summary of what I verified:
- `same_file`/`import` confidence in `computeConfidence` = 1.0, and `tierFor(1.0)` = CERTIFIED. So hunk-3's floor of `conf <= 0.0 → 1.0` restores exactly the verified value, and tier="CERTIFIED" is consistent. The old `conf < 0.5 → 1.0` would have re-promoted an intentionally-lowered 0.3/0.4 caller; the new `<= 0.0` preserves it (but then tier is still hardcoded CERTIFIED — see below).

---

# LIPI VERDICT — gt-index/internal/store/incremental.go

Diff = 4 hunks implementing squash-list items #3 (parity: carry qualified_name/evidence_type into snapshot + GetAllNodes) and #4 (floor only the 0.0 sentinel). Verified read-only against resolver.go, sqlite.go, main.go callers. No edits, no runs.

**#H1 (struct fields) | incremental.go:38-48 | VERIFIED | Implementation | Two new fields `EvidenceType string` + `TargetQualifiedName string` added to `IncomingEdgeRef`; both are populated by the H2 SELECT/Scan and `EvidenceType` is consumed at :142/:192. Types match the COALESCE'd TEXT columns. No undefined name, no partial rename.**

**#H2 (snapshot SELECT+Scan) | incremental.go:62-64, 80-81 | VERIFIED | Plumbing | SELECT adds `COALESCE(e.evidence_type,'')` + `COALESCE(n.qualified_name,'')`; Scan adds `&r.EvidenceType, &r.TargetQualifiedName` in the SAME column order. Both columns exist in schema (sqlite.go:169 `evidence_type TEXT`, :145 `qualified_name TEXT`). Column count (9) == Scan target count (9). Complete and ordered.**

**#H3 (qualifiedUnresolved gate + conf floor) | incremental.go:142, 148, 155-157, 192-194 | VERIFIED | Logic+Integration | The gate `r.EvidenceType == "name_match_qualified_unresolved"` correctly mirrors the resolver's demote marker (resolver.go:896 writes exactly that string). It (a) blocks the CERTIFIED branch via `!qualifiedUnresolved &&` at :148, forcing the demoted edge down the name_match path, and (b) re-stamps `evType` at :192-194 so the restored edge keeps the marker — true parity with resolver.go:894-896 (method=name_match, evidence=name_match_qualified_unresolved). The #4 floor change `conf < 0.5` → `conf <= 0.0` correctly preserves any intentionally-lowered conf>0 while still restoring the literal pre-v14 0.0 sentinel to 1.0; `tierFor`/`computeConfidence` confirm same_file/import = 1.0 = CERTIFIED, so the restore value is right.**

**#H4 (GetAllNodes column expansion) | incremental.go:293-294, 306-307 | BROKEN | Integration | The edit's stated intent is "rebuild the SAME columns the resolver reads (qualified_name, signature, parent_id) … else qualified/self/super (CHA) calls re-resolve against a lobotomized index." It adds `qualified_name`, `signature`, `parent_id` — but the ONLY downstream consumer of these nodes, `BuildNodeMeta` (resolver.go:233-239, fed `filteredNodes` at main.go:925), reads `ParentID`, `Name`, `Label`, `File`, and `ReturnType`. `NodeMeta` (resolver.go:410-416) has NO Signature or QualifiedName field — so the two added columns `signature`/`qualified_name` are INERT (read by nothing on this path). The one field BuildNodeMeta actually needs beyond ParentID — `ReturnType` — is STILL NOT selected. Result: Strategy 1.96's via-return type chain (resolver.go:1199-1213, gated on `fm.ReturnType == ""`) stays DEAD on the incremental path for cross-file callees, exactly the "lobotomized index" the hunk claims to fix. ParentID IS correctly added and IS load-bearing (self/super CHA), so the hunk is a partial fix, not a no-op — but it is INCOMPLETE and its comment over-claims.**

**Exact missing piece for #H4:** add `COALESCE(return_type, '')` to the SELECT (incremental.go:293) and a matching `&n.ReturnType` to the Scan (incremental.go:306). `return_type` is a real column (sqlite.go:150) and is the field BuildNodeMeta consumes. Optionally drop `signature`+`qualified_name` from the SELECT/Scan (they feed no consumer of `filteredNodes`) or leave them as harmless forward-compat — but `return_type` is the load-bearing omission.

**Does any hunk BREAK the other 3 avenues? No data-corruption risk:** I verified `filteredNodes`/`GetAllNodes` rows are used ONLY as read-only in-memory indexes (BuildNameIndex, BuildNodeMeta, incrFileNodeIDs/incrNameToIDs/incrNodeIDToFilePath) and are NEVER re-inserted — `BatchInsertNodesTx` (main.go:873) runs only on freshly-parsed `pr.Nodes`. So the unselected columns (return_type etc.) do not wipe DB data; the damage is confined to degraded in-memory resolution (#H4). The `n.Signature` reads at main.go:1489/1602 are in detectSerdePairs/detectStructuralTwins, called only on the FULL-index path (main.go:609-610) with `allNodePtrs`, never with incremental `filteredNodes` — so the added `signature` column is not even needed there.

---

**COMMIT/REVERT recommendation:** H1/H2/H3 are correct and self-contained (the #3 parity gate + #4 floor) — keep. H4 is safe (no breakage) but its comment over-claims parity and it leaves the real `return_type` gap open while adding two unused columns. Recommend amending H4 to select `return_type` (and trim or keep the inert two) before relying on the "non-lobotomized node view" claim. Net: not a revert, but H4 must be completed to deliver its stated intent.

---

All consumers confirmed. The only callers of the single-row `InsertAssertion` in the live tree are the test (`incremental_test.go`) and the definition itself; the worktrees/`.tmp_railway_ctx` copies are separate checkouts, not consumers of this file. The `Assertion` struct, the schema, and all three insert SQL statements (`InsertAssertion`, `BatchInsertAssertions`, `BatchInsertAssertionsTx`) are now bit-for-bit identical on column list, placeholder count, and bind-argument order.

Let me do the final LIPI cross-checks: confirm column count == placeholder count == bind-arg count, and confirm field-to-column positional alignment, and that the schema actually has the column.

**Column list (7):** `test_node_id, target_node_id, resolution_score, kind, expression, expected, line`
**Placeholders (7):** `?, ?, ?, ?, ?, ?, ?`
**Bind args (7):** `a.TestNodeID, a.TargetNodeID, a.ResolutionScore, a.Kind, a.Expression, a.Expected, a.Line`

Positional alignment is exact and matches both batch twins verbatim. Schema (line 215) has `resolution_score REAL DEFAULT 0.0`. The struct field `ResolutionScore float64` (line 79) exists.

---

**LIPI VERDICT**

**#45 (LIPI finding #5) — InsertAssertion drops resolution_score | gt-index/internal/store/sqlite.go:618-621 | VERIFIED | all 4 avenues clean**

- **LOGIC — VERIFIED.** Intent (doc #45, line 100/1679): single-row inserter omitted `resolution_score` so it silently took the schema default `0.0`, diverging from the batch path. The edit adds the `resolution_score` column + `a.ResolutionScore` bind. That is exactly the intended fix — single-row now persists the real multi-signal link score instead of defaulting.

- **IMPLEMENTATION — VERIFIED, and COMPLETE.** Column count (7) == placeholder count (7) == bind-arg count (7), and positional order is identical to the bind list. No signature/return-type change here (func still returns one `error`), so there is no multi-return / caller-contract fan-out to chase — this is the clean class, unlike the v1r `_localization_header→tuple` case. `a.ResolutionScore` is a real field on `Assertion` (sqlite.go:79, `float64`), so no undefined name; placeholder/arg counts balanced, so no SQLite "values count mismatch" error. Schema (sqlite.go:215) already declares `resolution_score REAL DEFAULT 0.0`, so the INSERT targets a real column.

- **INTEGRATION — VERIFIED.** The single-row `InsertAssertion` (sqlite.go:619) is now byte-identical in column list AND bind order to both symmetric twins: `BatchInsertAssertions` (sqlite.go:663/672) and `BatchInsertAssertionsTx` (incremental.go:441/448). The three writers are fully consistent; the asymmetry the doc flagged is closed.

- **PLUMBING — VERIFIED.** Right column (`resolution_score`), right field (`a.ResolutionScore`), data reaches the consumer: the round-trip test `TestInsertAssertionPersistsResolutionScore` (incremental_test.go:220-257) writes `0.875` via `InsertAssertion` and `SELECT resolution_score ... Scan` reads back `0.875`, asserting `!= 0.875` fails — proving the value lands in the DB and is readable. (Note: that test is the red→green proof; you said no-run, but the assertion logic itself confirms the column/path is correct.)

**Does this fix break any other avenue? No.**
- No other production caller of `InsertAssertion` exists in the live tree (only the new test + the definition; the `.claude/worktrees/*` and `.tmp_railway_ctx` hits are independent checkouts, not consumers).
- The added arg count exactly matches the added placeholder/column, so it cannot raise a bind-count error.
- Pillar-1 safe: pure column/bind parity, no task/repo/benchmark-specific logic.

**Single-hunk diff → COMMIT.** The edit is correct, complete, symmetric with both batch twins, and plumbed end-to-end. No revert needed.

---

The file parses cleanly (PARSE_OK). `_maybe_fire_l5` has zero references anywhere in the codebase (the grep returns only `render_l5_advisory`, a different, still-present function). No external consumer breaks from the #49 deletion.

Let me do one final confirmation for #52: verify that `_l5_gov.state` is always a valid `L5TrajectoryState` object at the call site (line 549) — i.e., the governor is constructed with a `.state`. Already confirmed: governor.py:123 `self.state = L5TrajectoryState.load_or_create(...)`, and the wrapper builds `config._l5_governor = L5Governor(...)` at 6775. The `getattr(config, "_l5_governor", None)` guard + `is not None` check + try/except make the call safe. The bridge is plumbing-safe; its only defect is that it cannot make the goku P0 hook fire due to the pre-existing 612/399 flag-coupling.

I have now verified all 6 hunks across all 4 LIPI avenues. Here is my verdict.

---

## LIPI VERIFICATION — `scripts/swebench/oh_gt_full_wrapper.py` (6 hunks)

**#48a (L5 governor finish dead-write) | oh_gt_full_wrapper.py:6328 | VERIFIED | Integration** — removed `obs = append_observation(obs, f"\n\n{_l5d.message}\n")`; the enclosing `if _l5d.message:` block still holds `_log_gt_interaction`+`_register_pending_next_action` (non-empty, parses), `_l5d` still consumed, `obs` flows unmodified to the return at 6437+. The two `_emit_structured_event` calls already stamp `emitted=False/suppressed=True/finish_handler_dead_write`, so removing the append makes code match its own telemetry. Breaks no other avenue. Confirmed OH-FINISHED semantics: agent never reads finish-handler obs.

**#48b (goku finish dead-write) | oh_gt_full_wrapper.py:6374 | VERIFIED | Integration** — same removal in the goku branch; block still non-empty (two emits + `_log_gt_interaction` consuming `_goku_d.message`), `obs` untouched downstream. Clean.

**#49 (`_maybe_fire_l5` deletion) | oh_gt_full_wrapper.py:1151 | VERIFIED | Implementation** — function had ZERO call sites (whole-repo grep: only the comment now names it; AST parses OK). It was the SOLE writer of `_l5_scaffold_fired`/`_l5_last_scaffold_file`, so at runtime those config fields were ALREADY always their class defaults (`False`/`""`, defined at 723-724). The three readers (617 telemetry `l5_fired`, 5256 metric increment, 5895 metric flag) all gate on `_l5_scaffold_fired` and degrade to the no-op they already executed — no behavior change. `_render_scaffold_advisory` left orphaned (declared out-of-scope; harmless). No external/test reference. The comment's reader line-numbers (5276/5915) drifted slightly from actual (5256/5895) but the symbols are right — cosmetic only.

**#50 (`_build_rescue_payload` evidence mispairing) | oh_gt_full_wrapper.py:1872 | VERIFIED | Plumbing** — replaced `top_key = next(iter(evidence_cache)); evidence_cache[top_key]` (arbitrary first-inserted key, generally a DIFFERENT file than `top_cand`) with `evidence_cache.get(top_cand, "")`. Key-form check: `evidence_cache` is keyed by `_cache_key = rel_view or event.path` (writer 4937); `_gt_delivered_evidence_files` (a `top_cand` source) uses the SAME `_cache_key` (4939) → those sources hit correctly. The consensus sources (`_consensus_scope`/`_consensus_confirmed`) store `_normalize_rel_path(...)` (4751/4757), which may differ from the un-normalized cache key → a MISS → `""` → evidence line omitted. That is the fix's explicit correct-or-quiet design (docstring 1868-1869), strictly safer than the old wrong-file pairing. Breaks no avenue; only residual is a quality opportunity (could normalize `top_cand` to recover more legit hits), not a correctness bug.

**#51 (governor `after_interaction` gate widening) | oh_gt_full_wrapper.py:4477 | BROKEN | Integration** — the COMMENT is wrong about the mechanism, though the change is non-breaking. The fix widens the gate from `event.kind=="skip"` to `in ("skip","post_view","post_edit")`, and the comment claims this routes "source edits → `_handle_source_edit`, non-source → `_handle_non_source_edit`." **It does NOT.** The `action` passed (4480) is always a `CmdRunAction` on this branch, and `after_interaction` short-circuits EVERY CmdRunAction at governor.py:213 (`if cls_name=="CmdRunAction": return self._handle_command(...)`) BEFORE reaching the edit-dispatch at 217-230. Those `_handle_source_edit`/`_handle_non_source_edit` branches fire only for native `FileEditAction`/`FileWriteAction`, which this bash-driven OH harness never produces. The ACTUAL new effect is only that the **scaffold-trap-early** block (governor.py:165-206) now evaluates on post_view/post_edit iterations (verification/test commands already classified as `skip` per classify_tool_event:1359, so `_handle_command`'s failure path was already reachable pre-fix). No double-fire (`_handle_command` returns `_NO_DECISION` for non-verification commands; `_scaffold_trap_fired` is once-only; source edits recorded elsewhere at 5226/goku-581, not here). Verdict: behaviorally SAFE but the rationale is false — the fix does not unlock the dispatch it claims; the missing piece is that `after_interaction` never reaches the FileEdit/FileWrite branch for CmdRunAction, so the "premature_commitment / non-source" intervention surface remains structurally unreachable on the OH path. Commit only if you accept the comment is misdescriptive; do NOT rely on `_handle_source_edit` firing.

**#52 (goku diff-collapse bridge) | oh_gt_full_wrapper.py:549 | BROKEN | Logic** — the plumbing half is correct (calls `_l5_gov.state.record_diff_snapshot(insertions+deletions`, with a nonzero-IFF-diff_nonzero proxy; guarded by getattr+None+`not _GT_BASELINE`+try/except; goku_check at 4527/6354 passes NO `diff_size`, so this is the sole feeder — no double-record). BUT it cannot achieve its intent ("so goku_check can flag patch_collapsed"). Exact missing piece: `record_diff_snapshot` (agent_state.py:392-399) sets `patch_collapsed=True` **AND `durable_edit_lost=True` on the same nonzero→0 transition** (lines 398-399), while the only goku gate that fires the hook (governor.py:612) requires `patch_collapsed and not durable_edit_lost`. Since the sole writer of `patch_collapsed` co-sets `durable_edit_lost`, that gate is structurally dead — the bridge updates state truthfully (telemetry/`save()` now reflect collapse) but the `PATCH_COLLAPSED_OR_LOST` hook STILL never fires to the agent. The fix delivers the state-bridge but not the advertised detection; to actually surface it, `record_diff_snapshot` must set `patch_collapsed` WITHOUT pre-setting `durable_edit_lost` (let governor.py:618 do the fire-once), OR gate 612 must drop the `not durable_edit_lost` clause. As-is: safe, non-breaking, but inert for its stated purpose.

---

### Commit/revert recommendation
- **#48a, #48b, #49, #50** — VERIFIED clean across all 4 avenues. Safe to commit.
- **#51** — code is non-breaking but the comment misdescribes the mechanism; the claimed dispatch never fires on the OH (CmdRunAction) path. Keep only if you correct/accept the comment; it does not deliver `_handle_source_edit`/`_handle_non_source_edit` coverage.
- **#52** — plumbing correct, intent unmet: the goku patch-collapse hook still cannot fire due to the pre-existing `patch_collapsed`/`durable_edit_lost` co-set at agent_state.py:398-399 vs the `not durable_edit_lost` gate at governor.py:612. Either fix that flag coupling too, or do not claim collapse detection is now live.

No hunk introduces a syntax error (AST parse OK), a NameError, a half-applied rename, or breaks a return/caller contract (the `_localization_header→tuple` class of bug is absent here). The two genuine defects (#51, #52) are both "fix does less than its comment claims," not crashes.