# SQUASH-LIST

Trace confirmed. The two pipes are: Pipe A = `_loc.candidates` → `_localization_header` → `<gt-localization>`; Pipe B = `top_records` (re-sorted by `_loc_rank` with `_exact_issue_named` forced to -1, on a lexical base) → `entries` → `files[0]` → `<gt-task-brief>`/`<gt-graph-map>`/EDIT-TARGET CONTRACTS/L1-SCOPE. The `_emit_old` gate only suppresses the legacy confident line, not the sibling blocks. Producing the squash-list.

---

# SQUASH-LIST — 14-review synthesis, ranked by HARM

Harm order: **wrong-fact / misdirection to agent** → **inert-delivery** → **hygiene**. Deduped across all 14 files. Confidence: H/M/L.

## THE L1 CROSS-WIRE (top of list — exact 2-pipe trace)

The single dominant bug, confirmed live (cfn-lint-3749). It appears in 4 files as the same wound. **Two independently-ordered candidate lists both reach the agent's observation, naming different #1 files.**

```
PIPE A (localizer-authoritative):
  graph_localizer.localize()  → _loc.candidates  [sorted graph_localizer.py:1944]
    → _localization_header(_loc,…)  [v1r_brief.py:2942]
    → <gt-localization>  block

PIPE B (lexical base, re-sorted ~7× AFTER _loc ran):
  run_v74 lexical set → top_records
    → re-sort _loc_rank (_exact_issue_named forced -1) [v1r_brief.py:2484-2497]
    → keyword boost / hub-demote / exact-name front-back [v1r_brief.py:2598-2790]
    → entries → files[0]
    → <gt-task-brief> #1           [render_brief @2946]
    → <gt-graph-map> focus         [_with_graph_map files[:3] @1312]
    → EDIT-TARGET CONTRACTS        [_edit_target_contracts_block(files[0]) @1594]
    → L1-SCOPE anchor              [entries[0].path @2884]

GATE THAT FAILS: _emit_old = (_loc_header=="")  [v1r_brief.py:2943]
  → suppresses ONLY the legacy confident-line; EDIT-TARGET CONTRACTS,
    graph-map, L1-SCOPE all still key off files[0] (Pipe B) → contradiction.

THIRD ORDERING: _localization_header HIGH path picks _high_pick
  (first non-hub issue-witnessed cand) [v1r_brief.py:1989] ≠ candidates[0] ≠ files[0].
```

**THE FIX (one structural change kills all of it):** when `_loc` fired, make Pipe B honor Pipe A by stable-sorting `top_records` with `_loc_rank_by_file` as the PRIMARY key (keyword/hub/exact-name re-rankers only order *within* localizer tiers), so `entries[0] == _loc.candidates[0]` by construction. Then every sibling block reads the same #1. Additionally fold the hub/`_hub_p80` demotion INTO the localizer's final sort so `candidates[0]` == the HIGH `_high_pick`, collapsing the third ordering. Thread the "header owns localization" signal so EDIT-TARGET CONTRACTS/graph-map/L1-SCOPE point at `_loc.candidates[0]`, not silenced.

| rank | file:line | function | class | LIPI | wrong-thing (1 line) | generalized fix | conf |
|---|---|---|---|---|---|---|---|
| **L1** | v1r_brief.py:2484-2497, 2942-2954, 1594, 1312, 2884 | generate_v1r_brief + 4 sibling blocks | cross-wire | Integration | `<gt-localization>` (Pipe A) and `<gt-task-brief>`/graph-map/EDIT-TARGET/L1-SCOPE (Pipe B `files[0]`) name different #1 files | stable-sort top_records by `_loc_rank_by_file` PRIMARY when `_loc` fired → entries[0]==candidates[0] | **H** |
| **L1b** | graph_localizer.py:1944; 1989 | localize / _localization_header HIGH | cross-wire | Integration | localizer owns no ordering; consumer re-sorts; HIGH path picks a 3rd file (`_high_pick`) | fold hub demotion into localizer final sort; consumer consumes `candidates` verbatim | **H** |
| **L1c** | v1r_brief.py:2943, 1593-1597, 1692 | render_brief gate | cross-wire | Integration | `_emit_old` suppresses only the legacy line; EDIT-TARGET CONTRACTS + graph-map still fire on `files[0]` | thread header-owns-localization; key both off `_loc.candidates[0]` | **H** |
| **L1d** | v7_4_brief.py:7541-7654 | run_v74 `<gt-orientation>` | cross-wire | Integration | a SEPARATE composite scorer appends `<gt-orientation>` that can rank a different #1 than `<gt-localization>` | derive orientation candidates from the v1r ranked set (shared #1) | **H** |

---

## FIX NOW (clear, safe, generalized — no decision needed)

Ranked by harm within the batch.

| rank | file:line | function | class | LIPI | wrong-thing (1 line) | generalized fix | conf |
|---|---|---|---|---|---|---|---|
| 1 | resolver.go:959-962, 1142, 1176 (+ central) | computeConfidence / all emit sites | wrong-fact | Implementation | 0.85 stamped CERTIFIED on some paths, CANDIDATE on others — tier not derived from confidence | single `tierFor(conf)`: ≥0.9 CERTIFIED, 0.5-0.9 CANDIDATE, <0.5 SPEC at every emit; 0.85 can never be CERTIFIED | **H** |
| 2 | parser.go:3102, 2292, 1723, 4210, 4349, 4462 | 6 property extractors | wrong-fact | Plumbing | every interior fact (config_read, call_order, concurrency…) stamped with the function-body START line | convert byte-offset→line (count `\n` in body[:idx]); per-hit extractors use hit line | **H** |
| 3 | incremental.go:51,127,256 (+resolver.go:721-758) | Snapshot/GetAllNodes/ResolveIncomingEdgesTx | inconsistent-filter | Integration | incremental `-file` path lacks the `qualifiedUnresolved` demotion → stdlib-shadow re-laundered CERTIFIED | carry `qualified_name` into snapshot/GetAllNodes; apply the same demote predicate as the full path | **H** |
| 4 | incremental.go:129-131 | ResolveIncomingEdgesTx | wrong-fact | Logic | `conf<0.5 → 1.0` re-certifies an intentionally-demoted caller on reindex | floor only the literal 0.0/NULL pre-v14 sentinel via computeConfidence; preserve any conf>0 | **H** |
| 5 | resolver.go:919-963 | Strategy 1.94 impl_method | wrong-fact | Logic | method-name global uniqueness asserted as CERTIFIED receiver edge with ZERO receiver-type check | cap name-uniqueness-only impl_method at CANDIDATE (≤0.6); CERTIFIED only when receiver type proven | **H** |
| 6 | resolver.go:727 vs 1193 | Strategy 1.9 vs Strategy 2 builtin drop | inconsistent-filter | Integration | single-candidate qualified `get/items/append` skips the broad builtin drop → laundered internal edge | apply broad `builtinMethodNames` set to the single-candidate qualified-unresolved path too | **H** |
| 7 | gt_intel.py:270 | get_callers | inconsistent-filter | Integration | CALLER family uses `_resolution_sql_in()` (name_match IN); callee twin uses deterministic-only | switch get_callers to deterministic gate, OR tag name_match callers `[POSSIBLE]` in BOTH paths consistently | **H** |
| 8 | gt_intel.py:301-304 | get_siblings | wrong-fact | Plumbing | sibling query filters on `parent_id` alone, no `file_path` → cross-file "siblings" via parent_id collision | add `AND file_path = (target's file_path)` — a class's methods are same-file in every language | **H** |
| 9 | post_view.py:1232-1286 | _file_function_spec | wrong-fact | Logic | emits file-top-by-`start_line` function as `Spec:` with no issue-relevance — re-introduces the 3749 noise | apply the contract-pillar anchor-front-load + `_relevance==0→[]` correct-or-quiet gate | **H** |
| 10 | post_edit.py:1539, 1368, 1430, 1230 vs 928 | consistency queries (twins/peers/override/siblings) | inconsistent-filter | Integration | `[TWIN]`/`[PEER]`/`[OVERRIDE]` select by name alone, no edge gate; caller block IS gated | route all consistency queries through the same categorical trust gate the caller query uses | **H** |
| 11 | post_edit.py:2706 vs 288 | behavioral-contract inline resolver | wrong-fact | Plumbing | contract node resolved by name w/o label filter; diverges from `_resolve_node_id` → props on wrong node | delete inline resolver; call `_resolve_node_id` so contract + callers describe one node | **H** |
| 12 | post_edit.py:3068 | callee block (`Calls into:`) | wrong-fact | Plumbing | `nt.file_path != ?` bound with possibly-None resolver result → `!= NULL` disables self-exclusion | skip callee block when resolved path is None (correct-or-quiet); audit all `!= ?` binds | **H** |
| 13 | post_edit.py:1886 | _signature_has_varargs | logic | Logic | `"*" in signature` treats keyword-only `*,`/type-hint `*` as varargs → kills arity contract | detect `*name`/`**name` token only (regex `\*\*?\w`), not bare `*` | **H** |
| 14 | curation_map.py:660-666, 493 | _verified_caller_count | wrong-fact | Plumbing | verified-caller COUNT truncated by 5-row display cap → drift block understates blast radius 6× | dedicated `COUNT(DISTINCT)` with the gate, no `max_neighbors` cap | **H** |
| 15 | curation_map.py:450 | _dynamic_neighbors sparseness | logic | Logic | 2-hop rescue gated on TOTAL visible count → name_match noise suppresses rescue on isolated targets | gate sparseness on FACT count (`len(fact_neighbors)`), seed rescue from those facts | **H** |
| 16 | curation_map.py:225-228 (+contract_map.py:430) | _node_ids | wrong-fact | Plumbing | exact `file_path=?` (no normalize) vs witness twin's normalized LIKE → whole map silently abstains | normalize path identically (replace`\`→`/`, lstrip `./`) + suffix LIKE, shared normalizer | **H** |
| 17 | contract_map.py:428, 173-195 | _node_sig_line / _evidence_for | wrong-fact | Plumbing | callee sig+line+props re-resolved by lowest `start_line` over same-name union → wrong overload's contract | thread resolved `node_id` on Edge (already JOINs nodes n — add n.id); key all node reads by id | **H** |
| 18 | anchor_select.py:104, 181, 308 | select_anchors merge keys | cross-wire | Plumbing | sem/symbol maps key off RAW `nodes.file_path`; lexical is forward-slashed → trust-upgrade merge fails | normalize all 3 ingress points to canonical form before keying | **H** |
| 19 | v7_4_brief.py:447, 900-922 | _total_score | wrong-fact | Plumbing | `path`/`frame`/`code_def` NOT max-normalized while `lex`/`reach` are → unnormalized terms over-weight hubs | max-normalize all six component maps to [0,1] before scoring | **H** |
| 20 | v7_4_brief.py:480, 990-995 | run_v74 RRF mode | inert | Integration | RRF fusion omits hub_pen → switching `GT_RRF_FUSION=on` silently disables hub defense | subtract hub-rank demotion in RRF mode so both fusion paths carry hub defense | **H** |
| 21 | v7_4_brief.py:819 vs 895 | run_v74 BM25 (two calls) | cross-wire | Integration | `lexical_file_search` called twice with different `max_files` → candidate-membership BM25 ≠ scoring BM25 | call once with `max_files=max(50,…)`, reuse for both seeding and scoring | **H** |
| 22 | gt_intel.py:358-364, 369-374 | classify_caller_usage | wrong-fact | Implementation | call-line text taken at fixed window index 1 (off-by-one at file head); score regexes scan whole window | read single call_line for spec text; run classification regexes against call_line only | **H** |
| 23 | gt_intel.py:1361-1382 | _format_import_for_language | wrong-fact | Logic | Go/Java/C# import path fabricated from on-disk dirname (≠ module path) → hallucinated import | read package/namespace decl from file header, else neutral `(from {path})` form | **H** |
| 24 | gt_intel.py:1448-1453 | SIBLING return-type vote | logic | Logic | agreement ratio denominator = ALL siblings but numerator = typed only → unanimous-among-typed suppressed | divide by `len(ret_types)` with min-support floor ≥2 | **H** |
| 25 | gt_intel.py:1492 | TYPE upgrade (`"destruct"`) | inert | Implementation | consumer greps `"destruct"` but producer writes `"called as:"` → upgrade dead, TYPE stuck at score 1 | producer returns structured `usage="destructure"`; consumer checks the enum | **H** |
| 26 | gt_intel.py:1692 | rank_and_select negative-spec boost | logic | Logic | `"not"`/`"false"` substring-match boosts TEST nodes via `note`/`notification`/`cannot` | word-boundary regex, or drive boost from assertion kind not summary | **H** |
| 27 | gt_intel.py:1152 vs 1164 | generate_pretask_briefing | inconsistent-filter | Integration | top-caller gated deterministic, adjacent TEST query gated name_match-IN → phantom test link | one gate for both, or tag name_match TEST line `[POSSIBLE]` | **H** |
| 28 | resolve.py:506-534 | _resolve_edges definition→node | wrong-fact | Logic | edge DELETEd on line-window/external-file miss → real edge destroyed on NULL-end_line / stdlib target | only delete when target file IS indexed but no node matches; never on external-file or NULL-window | **H** |
| 29 | resolve.py:511 | _resolve_edges node match | wrong-fact | Implementation | matches by stale `target_name` → LSP-corrected (renamed) edge never matches → deleted | match by (file, line-window); name is tiebreaker not hard filter | **H** |
| 30 | resolve.py:50-66 vs config.py:21-30 | _KNOWN_SERVERS / LSP_SERVERS | inconsistent-filter | Integration | dispatch advertises c/cpp/ruby/kotlin that LSP_SERVERS can't serve → silent full no-op | derive both tables from one source; `_KNOWN_SERVERS` keys ⊆ `LSP_SERVERS` keys | **H** |
| 31 | post_view.py:45-64, 1168 | _l3b_line_priority | inert | Integration/Logic | ego block first-line (`name() in file:line`) matches no priority prefix → trimmed FIRST | classify by real ego shape `^\w+\(\) in .+:\d+$`, or carry priority at append time | **H** |
| 32 | post_view.py:890-893, 613-621 | hub-scale degree query | wrong-fact | Integration | 3 different edge populations (`_ef` / unfiltered / `conf≥0.7`) feed one hub ranking → miscalibrated | thread the single `_ef` clause through all three queries | **H** |
| 33 | post_view.py:1294-1300 vs 829-839 | _test_file_targets | inconsistent-filter | Integration | `Calls into:` lacks the stdlib-shadow guard its caller twin has → `items()`/`join()` as targets | shared `_render_edge_target` applying the shadow guard once | **M** |
| 34 | post_view.py:301-307 | _contract_pillar flows | wrong-fact | Plumbing | flow looked up by `n.name` not node-id → homonym method's flow stapled to wrong overload | add `id` to the signature SELECT; join flow on exact node id | **M** |
| 35 | curation_map.py:269-278 (+v1r_brief.py:633) | _neighbors | wrong-fact | Integration | `<gt-graph-map>` lacks the `_is_stdlib_shadow` guard the witness twin has → laundered fact rendered bare | factor shadow guard into shared helper; apply in _neighbors (SELECT code/source_line) | **M** |
| 36 | v1r_brief.py:722-738 | _resolved_witnesses_for_file (callee) | wrong-fact | Plumbing | callee `code` snippet read at call-site line but travels with callee def file:line | set `code=_code_at(callee_file, def_line)` or drop code for callee records | **M** |
| 37 | v1r_brief.py:1253-1254 | _entry_confidence_tier path_match | logic | Logic | `_stem in _it` unanchored substring → `core`/`base`/`data` promote generic files to [WARNING] | word-boundary match + specificity floor (len≥5 or contains `_`) | **M** |
| 38 | v1r_brief.py:2264-2290 vs 2737-2783 | _exact_issue_named_files (×2) | inconsistent-filter | Integration | first pass front-injects unconditionally, defeating the second pass's coincidence-demotion | delete first unconditional injection; keep only the corroborated/coincidence-split pass | **M** |
| 39 | resolver.go:557 | Strategy 1 same-file | logic | Logic | same-file resolution abandoned on local name overload → speculative cross-file edge emitted instead | prefer best same-file target (CANDIDATE) over any cross-file name_match | **M** |
| 40 | resolver.go:638-652 | Strategy 1.5 import "pick best" | logic | Implementation | comment promises same-dir preference; code takes `importCandidates[0]`, stamps multi-candidate CERTIFIED | implement same-dir tie-break / lexical-min; demote when >1 and no same-dir winner | **M** |
| 41 | resolver.go:776-783 | Strategy 1.93 import_type | logic | Implementation | dead `::` re-assign no-op; missing `Self` guard (vs 4 sibling strategies) | delete dead branch; add `&& qualifier != "Self"` | **M** |
| 42 | parser.go:197 | goReceiverType | wrong-fact | Logic | `IndexByte(s,')')` takes FIRST `)` not receiver's → generic receiver mis-typed → method unparented | paren-depth match to the receiver's close `)`; or use AST `receiver` field | **H** |
| 43 | parser.go:750 | extractAssignments constructor | wrong-fact | Logic | `Capital()` → constructor applied to Go (`Marshal()`) → false TYPE fact pollutes resolver | gate capital→constructor by language; Go uses ViaReturn | **H** |
| 44 | parser.go:927 | extractCalleeInfo literal-receiver | inconsistent-filter | Logic | guard tests only depth-1 receiver → chained `"x".strip().split()` literal not dropped | unwrap parenthesized + chain head to the literal root | **M** |
| 45 | sqlite.go:616 vs 660 | InsertAssertion | inconsistent-filter | Plumbing | single-row inserter omits `resolution_score`; batch path includes it → silent 0.0 | add `resolution_score` column + bind to InsertAssertion | **M** |
| 46 | v7_4_brief.py:934 | run_v74 sem fallback | logic | Logic | `sem_all if sem_all else sem_scores` falls back to bounded seed map — re-creates the spurious-0 bug | drop fallback; use `sem_all` unconditionally (empty ⇒ 0 everywhere) | **M** |
| 47 | anchor_select.py:13-17, 361 | structural_seed_expand | inert | Integration | documented "v7.5 H1" lever has ZERO callers — dead code, docstring misrepresents pipeline | delete function + strike docstring paragraph (H1 falsified per census) | **H** |
| 48 | oh_gt_full_wrapper.py:6313, 6355 | finish-handler L5/L5b | inert | Integration | appends to `obs` AFTER `state=FINISHED`, self-labeled `finish_handler_dead_write` | delete the two append_observation calls; governance must fire mid-trajectory | **H** |
| 49 | oh_gt_full_wrapper.py:1131, 1497-1498 | _maybe_fire_l5 / render_l4_tool_footer | inert | Implementation | `_maybe_fire_l5` zero call sites; `render_l4_tool_footer` returns `""` → L4 tools never announced | delete dead `_maybe_fire_l5`; decide L4 tools (delete or restore hint — see DECISION) | **H** |
| 50 | oh_gt_full_wrapper.py:1867-1884 | _build_rescue_payload | wrong-fact | Plumbing | file = `top_cand`, evidence = `next(iter(evidence_cache))` (arbitrary other file) → mispaired | `evidence_cache.get(top_cand)`; omit evidence line if absent | **H** |
| 51 | oh_gt_full_wrapper.py:4461-4462 | governor after_interaction gate | inert | Integration | invoked only on `event.kind=="skip"` → source-edit/finish dispatch unreachable | call after_interaction for all classified events (post_edit + finish on live path) | **H** |
| 52 | oh_gt_full_wrapper.py:4512-4515, 533-543 | goku_check diff bridge | inert | Plumbing | wrapper computes diff-collapse but never passes `diff_size` to goku → collapse detector blind | call `_l5_gov.state.record_diff_snapshot(...)` inside `_record_diff_snapshot` | **H** |
| 53 | v1r_brief.py:2987-2990 | _l1_signal_counts align | inert | Plumbing | `_rec_by_path` keyed raw, looked up by entry path (normalized) → MISS → signals under-report | key + lookup by `_gl_normalize` (corrupts the fail-closed CI gate) | **M** |
| 54 | graph_localizer.py:418 vs 1624 | _path_decay_scores vs witness BFS | inconsistent-filter | Integration | path-decay SQL admits SUPPRESSED/stdlib edges the witness BFS rejects → decay mass, 0 witnesses | share ONE admission predicate across both traversals | **M** |
| 55 | graph_localizer.py:289-293 | _fts5_candidates | logic | Plumbing | opens a SECOND writable conn and CREATE/INSERTs `nodes_fts` during a read-only localize pass | move FTS5 population to indexer; treat missing nodes_fts as "BM25 unavailable" | **M** |
| 56 | graph_localizer.py:1770 | localize lex_hits | logic | Logic | `t in s or s in t` substring → 3-char `set` matches settings/reset/offset (path-seed already fixed this) | token-boundary/component match, floor ≥4 (mirror path-seed discipline) | **M** |
| 57 | graph_localizer.py:514 | Witness.strength | logic | Logic | DEFINES at hop-0 (0.55) beats verified edge at hop-1 (0.50) → lexical out-ranks structural | carry tier into score; cap DEFINES strictly below min verified-edge strength | **M** |
| 58 | contract_map.py:653 | _diff_contract dropped-guard | logic | Logic | suppression regex `raise\s+Name` is Python-only → Go/Rust/TS guards always read as hard drops | gate emission on whether `raises` set shrank, not on the `raise` token | **M** |
| 59 | curation_map.py:347, 450; 384-409 | _apply_dynamic_budget / _second_hop | logic | Logic | fact-count off the 19-row over-fetch on mega-hubs; rescue truncates-before-excludes | true COUNT for budget; push `exclude` into SQL or over-fetch `+len(exclude)` | **M** |
| 60 | v1r_brief.py:291 vs 326-330 | _top_function_names | logic | Logic | SQL CASE uses len>2 filter; Python partition uses unfiltered terms → contradictory ranks | one filtered term set for both; drop redundant Python re-partition | **L** |

---

## NEEDS A DECISION (policy / design call before squashing)

| # | file:line | function | the fork | why it needs a human |
|---|---|---|---|---|
| D1 | gt_intel.py:270 / 998 vs 1148-1157 | CALLER "fact" policy | name_match callers: **facts-only (deterministic gate)** OR **delivered with `[POSSIBLE]` tag** | the codebase contradicts itself — briefing path is deterministic-only, post-edit path tags name_match. Pick ONE policy and apply to both. (Touches #7, #27.) The "agentic-RAG noise is bait" memory argues for tagged-delivery; CLAUDE.md "phantom caller" argues facts-only. |
| D2 | oh_gt_full_wrapper.py:1497, 3496-3560 + gt_validate chain | L4 tool surface | **delete** install_l4_tools/render_footer/gt_validate (0% adoption, pure cost) OR **restore a minimal tool hint** so adoption is measurable | deleting removes a measurement capability; restoring re-introduces agent-controlling surface. Product call, not a code call. (Covers #6/#49's `gt_validate` half.) |
| D3 | resolve.py:925, 918-921 | LSP_METRICS `resolved` denominator | credit `deleted` as resolved OR emit `deleted=` as a 3rd field — GATE 2 currently reads a clean delete-heavy pass as FAIL | changes a CI gate's verdict semantics; needs the gate owner's sign-off. |
| D4 | v1r_brief.py:1528-1584; post_edit.py:2073-2181, 1628-1701 | disabled VERIFY/assertions blocks | **delete** the `if False:`/early-return dead bodies OR keep as documented leakage-guard stubs | deletion is hygiene, but the dead code is one revert from re-leaking test names (run12 `test_plot_hdi`). Decide whether to physically remove or keep the guard comment. |
| D5 | graph_localizer.py:1931 + v1r_brief.py:1986 | embedder-absent HIGH gate | make agreement threshold relative to `n_signals_available` (2 vs 3) OR keep absolute `>=2` | absent the ONNX/ST embedder, `>=2` silently demands unanimity — a real behavioral discontinuity. Whether to loosen the in-container gate is a product tradeoff (precision vs HIGH-steer frequency). |
| D6 | v7_4_brief.py:106-128, 730→866 | _adapt_weights_for_issue | make gates genuinely additive + ablation-aware OR accept that Dim-2 `max()` overrides Dim-1 and re-inflates ablated weights | the "compose additively" docstring is a lie vs the `max()` code; fixing it changes weight behavior on every real issue and the ablation contract. Needs a weighting-policy decision. (Covers v7_4 #2 + #8.) |
| D7 | v7_4_brief.py:614-618 (+traces.py) | _compute_frame_scores | confirm `parse_stack_traces` ordering BEFORE trusting `idx=0 → 1.0` | if frames are printed-order (outermost-first), the entire depth-decay signal is INVERTED. Must verify the cross-module contract — flagged MODERATE, load-bearing. |

---

## Coverage / counts

- **14 files reviewed → 60 FIX-NOW items + 7 DECISIONS + 4 L1 cross-wire rows**, deduped.
- **The L1 cross-wire is one wound across 4 files** (v1r_brief, graph_localizer, v7_4_brief orientation, render_brief gate) — fix once at the localizer-ordering source.
- **Highest-harm cluster (wrong-fact, fix immediately):** resolver tier-from-confidence (#1), parser interior-line attribution (#2), incremental stdlib-shadow re-launder (#3-4), the contract-pillar node-id threading (#17), and the consistency-query gate gap (#10).
- **Largest inert-delivery cluster is `oh_gt_full_wrapper.py`** (#48-52): finish-handler dead writes, dead `_maybe_fire_l5`, governor starved on `skip`-only, goku diff bridge missing. All structural, all safe to fix except the L4-tool DECISION (D2).
- **Cross-cutting structural primitives that recur:** (a) one canonical path-normalizer used at every dict-key ingress (#16, #18, #53, and post_edit/post_view path sites); (b) thread the resolved `node_id` on `Edge` so sig/line/props/flows read the same node (#17, #34, contract_map #1-3); (c) `tierFor(conf)` single mapping (#1); (d) one shared edge-admission predicate / trust gate across every traversal (#3, #10, #32, #33, #35, #54).


# PER-FILE (full)

I have everything needed. Now let me write the exhaustive LIPI review.

---

# LIPI REVIEW — `src/groundtruth/pretask/v1r_brief.py` (3174 lines)

**Two pipes that feed the agent are NOT the same list.** The whole brief is assembled from TWO independently-ordered candidate sources that both reach the agent's observation:

- **PIPE A — `_loc.candidates`** (from `graph_localizer.localize()`): sorted at `graph_localizer.py:1944`, capped `top_k` at 1952. This is the *authoritative structural* ranking. **It is consumed ONLY by `_localization_header(_loc, …)`** to render `<gt-localization>` (v1r_brief.py:2942). `_loc.candidates[0]` = the `<gt-localization>` #1.
- **PIPE B — `top_records` → `entries` → `files`**: starts as `_loc` candidates **but is then reordered ~7 more times AFTER `_loc` is computed** (issue-keyword boost 2647-2711, hub-demotion 2598-2632, exact-name front/back guarantee 2715-2790, path-rescue, neighbor expansion). `files[0]` = `entries[0]` = `top_records[0]` after all that. **`render_brief`, `_with_graph_map`, `_edit_target_contracts_block(files[0])`, and the L1-SCOPE query all key off PIPE B's `[0]`.**

Because Pipe B is re-sorted by **keyword count / hub-degree / exact-name** AFTER the localizer ran, `files[0]` (`<gt-task-brief>` #1, `<gt-graph-map>` focus, `EDIT-TARGET CONTRACTS` subject, L1-SCOPE anchor) routinely diverges from `_loc.candidates[0]` (`<gt-localization>` #1). **The agent receives two top-level localizers that name different files** — the confirmed cfn-lint-3749 self-contradiction. This is finding #1 and the dominant bug.

---

## NUMBERED FINDINGS

**1. `generate_v1r_brief` (assembly) + `_localization_header` / `render_brief` / `_with_graph_map` / `_edit_target_contracts_block` · INTENT: emit one coherent localization steer · BUG: L1 CROSS-WIRE — `<gt-localization>` ranks `_loc.candidates` (Pipe A) while `<gt-task-brief>` / `<gt-graph-map>` / `EDIT-TARGET CONTRACTS` / L1-SCOPE all key off `files[0]` (Pipe B), which is re-sorted by keyword/hub/exact-name AFTER `_loc` is computed → the two blocks name different #1 files · LIPI: Integration (two sibling sub-blocks sourced from different pipes that misdirect) ·**
- `v1r_brief.py:2942` `_loc_header = _localization_header(_loc, graph_db, issue_text)` ← Pipe A
- `v1r_brief.py:2946-2954` `render_brief(entries, …)` ← Pipe B (`entries` built at 2791-2851 from the heavily re-sorted `top_records`)
- `v1r_brief.py:1594` `_edit_target_contracts_block(graph_db, files[0])` ← Pipe B `[0]`
- `v1r_brief.py:1321` `build_function_map(graph_db, focus)` where `focus` = `files[:3]` ← Pipe B
- `v1r_brief.py:2884` `_top_path = entries[0].path` (L1-SCOPE) ← Pipe B `[0]`
- The reorders that desync Pipe B from Pipe A: keyword boost `2706-2711` (`_issue_scores.sort(key=lambda x:(x[0],x[1],-x[2],x[3]))`), hub-demotion `2623-2627`, exact-name front/back `2781-2783`.

  **GENERALIZED FIX (single source of truth):** When `_loc` fired (non-empty `_loc.candidates`), derive the edit-target identity for ALL sibling blocks from `_loc.candidates[0]` — the same list `<gt-localization>` ranked. Concretely: after building `entries`, if `_loc` is non-empty, compute `loc_primary = _gl_normalize(_loc.candidates[0].file_path)` and (a) pass it into `render_brief` as an explicit `edit_target_path` param that `_edit_target_contracts_block` and the confident-line use instead of `files[0]`; (b) build `_with_graph_map` focus from the localizer's top-3 (`[_gl_normalize(c.file_path) for c in _loc.candidates[:3]]`) instead of `files[:3]`; (c) seed L1-SCOPE from `loc_primary` not `entries[0].path`. Equivalently and more robustly: make `entries` itself honor the localizer order by stable-sorting `top_records` by `_loc_rank_by_file` as the PRIMARY key whenever any candidate is localizer-ranked, so Pipe B == Pipe A by construction and `files[0]` is always `_loc.candidates[0]`. No file/task/benchmark-specific logic — it is "one localizer ranks, every block reads that one rank."

**2. `_localization_header` · INTENT: render the localizer's top-K · BUG: `<gt-localization>` candidate set (`shown = cands[:K]`, raw `_loc` order) is itself NOT the same as the file-list the agent sees in `<gt-task-brief>`, and even WITHIN the header the LOW-region path and the flat-option path can pick different K than the file list → a third ordering · LIPI: Integration ·**
- `v1r_brief.py:1929-1930` `K = min(max(3,_evidenced),6,len(cands)); shown = cands[:K]`
- This `shown` is never reconciled with `files` rendered by `render_brief`. Same root as #1; the fix in #1 (single localizer-ordered source) subsumes it. **GENERALIZED FIX:** render the file list in `<gt-task-brief>` from the same `shown` slice the header used (pass the localizer-ordered candidate set down), so header K and body K cannot diverge.

**3. `_resolved_witnesses_for_file` (CALLEE branch) · INTENT: surface a callee's DEFINITION location · BUG (RESIDUAL WRONG-FACT RISK in the rendered tail): the SELECT pairs `nt.file_path` with `e.source_line` in the column list, and although the code correctly re-fetches `def_line = nt.start_line` for the rendered line, the `code` snippet is read at `_code_at(file_path, source_line)` — `source_line` is the CALL SITE in the candidate file, but it is paired into a record whose `file_path`/`line` are the CALLEE's def. The `code` field therefore describes a DIFFERENT (file,line) than the `file_path`+`line` it travels with · LIPI: Plumbing (field from row A paired with row B) ·**
- `v1r_brief.py:722` `SELECT nt.file_path, e.source_line, nt.name, nsrc.name, nt.start_line`
- `v1r_brief.py:727` `code = _code_at(file_path, source_line)` then `v1r_brief.py:730-738` emits `{file_path: callee_file, line: def_line, code: code}`
- So `code` = the call-site line text in the *candidate* file, but `file_path:line` = the callee's def in *another* file. Only `_resolved_witness_tail` (which drops `code`) consumes this safely; the L1 STRUCTURED JSON path (`l1_graph_edge`, 3092-3102) emits `symbol`+`edge_file`+`line` without code so it's safe too — but any future consumer reading `.code` alongside `.file_path/.line` gets a mismatched snippet. **GENERALIZED FIX:** for `direction=='callee'`, set `code = _code_at(callee_file, def_line)` (snippet of the DEFINITION, matching the rendered file:line), or drop `code` entirely for callee records. The comment at 723-726 acknowledges the file:line was historically wrong and fixed it for file:line but left `code` keyed to the wrong location.

**4. `render_brief` — confident-line gate vs the `<gt-localization>` header · INTENT: don't double-steer · BUG: suppression of the legacy "Highest-confidence candidate" line is gated on `_emit_old = (_loc_header == "")` (passed as `emit_confident_line`), but the `EDIT-TARGET CONTRACTS` block and `_with_graph_map` are NOT suppressed when the header fires — so when `<gt-localization>` fires about `_loc.candidates[0]`, the EDIT-TARGET CONTRACTS block STILL renders about `files[0]` (a possibly different file), re-introducing the contradiction the suppression was meant to prevent · LIPI: Integration (one path gated, its twin not) ·**
- `v1r_brief.py:2943` `_emit_old = _loc_header == ""` → only the confident-line is conditioned
- `v1r_brief.py:1593-1597` EDIT-TARGET CONTRACTS fires unconditionally on `files[0]`
- `v1r_brief.py:1692` `_with_graph_map(...)` fires unconditionally on `files[:3]`
  **GENERALIZED FIX:** thread the same "header owns localization" signal into `render_brief` and, when the header fired, key EDIT-TARGET CONTRACTS + graph-map off the header's primary (`_loc.candidates[0]`), not `files[0]` (this is the same single-source remedy as #1). Do not merely silence them — they carry real callee-contract value; just point them at the same file the header named.

**5. `_top_function_names` · INTENT: return raw issue-relevant function names · BUG: DOUBLE re-ranking with a SQL `LIMIT 20` cut that the Python re-rank cannot undo — and the two halves disagree. The SQL orders issue-matched-THEN-refcount and caps 20; the Python then re-partitions `issue_matched + others`. But the SQL's `CASE WHEN LOWER(n.name) IN (…)` uses `_terms` (filtered to `len>2`), while the Python partition uses the raw `issue_terms` (unfiltered) → a 1-2 char issue term can match in Python a row the SQL already deprioritized, and more importantly an issue function ranked 21st by the SQL `ORDER BY refcount` is invisible to the Python re-rank · LIPI: Logic + Implementation (inconsistent filter between the two ranking stages) ·**
- `v1r_brief.py:291` `_terms = sorted({t.lower() for t in (issue_terms or set()) if t and len(t) > 2})`
- `v1r_brief.py:301` SQL `CASE WHEN LOWER(n.name) IN ({_ph})` uses `_terms`
- `v1r_brief.py:326-330` Python uses `terms_lower = {t.lower() for t in issue_terms}` (no `len>2` filter)
  **GENERALIZED FIX:** use one filtered term set for both the SQL CASE and the Python partition; since the SQL already sorts issue-matched to the front (THEN 0), the Python re-partition is redundant — drop it and just `return [r[0] for r in rows[:limit]]`. One ranker, no second contradictory pass.

**6. `_caller_contract_for_file` — facts-vs-unverified mixing · INTENT: never mix a name_match guess with verified facts · BUG: the per-`fname` loop breaks on `len(fact_parts) >= 3` but `unverified_parts` accumulates across ALL funcs even when facts exist for a LATER func; the final `if fact_parts: return facts` correctly drops unverified — but the early `break` at 600-603 can exit the OUTER loop after func #1 produced 3 facts, never examining func #2, which is fine; the real defect is subtler: `func_names[:2]` (line 528) caps at 2 funcs, but `_top_function_names` already returned issue-ranked names — so a 3rd issue-relevant edit-target function NEVER gets caller evidence · LIPI: Logic (arbitrary `[:2]` truncation drops the issue-relevant tail) ·**
- `v1r_brief.py:528` `for fname in func_names[:2]:`
  **GENERALIZED FIX:** iterate all `func_names[:MAX_FUNCTIONS_PER_FILE]` (already 3) and let the `len(fact_parts)>=3` cap bound output, rather than capping the *input* funcs at 2. The cap belongs on rendered facts, not on which functions are even considered. (Low harm; flag as cleanup.)

**7. L1-SCOPE block (2873-2934) · INTENT: derive multi-file scope from the top file's callers · BUG: anchors on `entries[0].path` (Pipe B `[0]`), so when Pipe B `[0]` ≠ `_loc.candidates[0]`, the "Likely multi-file scope" hint is computed for the WRONG file and contradicts `<gt-localization>` · LIPI: Plumbing/Integration (scope data computed off the divergent pipe) ·**
- `v1r_brief.py:2884` `_top_path = entries[0].path`
  **GENERALIZED FIX:** seed `_top_path` from the localizer primary when `_loc` fired (`_gl_normalize(_loc.candidates[0].file_path)`), falling back to `entries[0].path` only when the localizer abstained. Same single-source remedy as #1.

**8. `_exact_issue_named_files` — duplicated work + divergent gates · INTENT: guarantee an issue-named function's file renders · BUG: called TWICE (`2269` and `2737`) with the SAME inputs but the FIRST call front-injects unconditionally (`top_records = _promote[:3] + top_records`, 2290) while the SECOND splits corroborated/coincidence (2749-2783). The first injection can front-load a COINCIDENCE file that the second pass was specifically rewritten to demote — but the second pass only handles files `not in _in_top`, and the first-pass injection already put it in top → the coincidence file is now protected from the very demotion meant to catch it · LIPI: Integration (two passes with different gates, earlier one wins) ·**
- `v1r_brief.py:2269-2290` first pass: unconditional `_promote[:3]` front-inject
- `v1r_brief.py:2737-2783` second pass: corroborated→front, coincidence→back-capped
  **GENERALIZED FIX:** delete the first (2264-2290) unconditional injection; keep ONLY the second corroborated/coincidence-split pass. The first pass predates the KINK#5 fix (its own comment 2719-2734 documents why unconditional front-injection is wrong) and now actively defeats it. One guarantee pass, with the confidence-split gate.

**9. `_l1_signal_counts` / alignment (2985-2990) · INTENT: read per-candidate `components` for observability · BUG: `_rec_by_path` keys on RAW `path` only (`str(_r.get("path",""))`), but `entries` paths may be normalized differently than `top_records` paths (the whole file fights raw-vs-`_gl_normalize` forms — see 2819 `_pn = path.replace…lstrip`). A delivered entry whose path was normalized at FileEntry build but stored raw in `top_records` → `_rec_by_path.get(e.path, {})` MISS → its sem/lex/reach silently counted as 0 → `semantic_signal_count`/`structural_signal_count` under-report · LIPI: Plumbing (path not normalized consistently between the two dicts) ·**
- `v1r_brief.py:2987` `_rp = str(_r.get("path", ""))` (raw)
- `v1r_brief.py:2990` `_rec_by_path.get(e.path, {})` (entry path, possibly normalized)
  **GENERALIZED FIX:** key `_rec_by_path` by `_gl_normalize(path)` and look up by `_gl_normalize(e.path)`. Observability-only (the GT_DEBUG_L1 join at 2995-2996 literally prints MATCH/MISS to detect this), but it corrupts the fail-closed signal-provenance gate, so it's load-bearing for CI verdicts.

**10. `_with_graph_map` focus selection · INTENT: 1-hop map of the top files' focus functions · BUG: focus = `(f.function_names or [])[:1]` for `f in files[:3]` — takes only the FIRST function name per file. `_top_function_names` issue-sorts names, so `[0]` is usually right, but on a file where the issue term matched NO function, `[0]` is the highest-ref-count (hub) function, so the graph-map renders callers/callees of an UNRELATED hot function, not the edit target · LIPI: Logic (single-function focus picks the wrong function on no-issue-match files) ·**
- `v1r_brief.py:1313` `for fn in (f.function_names or [])[:1]:`
  **GENERALIZED FIX:** only add a focus pair when the chosen function is issue-corroborated (name ∈ issue terms OR has a verified witness); otherwise omit that file from focus (correct-or-quiet) rather than mapping a hub function. Generalized: gate focus on issue-relevance, don't blindly take `[0]`.

**11. `render_brief` — `_func_overlap` spec gate uses `f.functions` (signatures) not `f.function_names` · INTENT: gate the Spec line on whether a function name overlaps the spec text · BUG: `f.functions` holds SIGNATURES (`def foo(...) -> T:`), so `fn.lower() in _spec_lower` substring-matches a whole signature string, almost never true — the same signatures-vs-names confusion the file documents at 178-182 and fixes in `_entry_confidence_tier` (1244) but NOT here · LIPI: Implementation (wrong variable: signatures where names are needed) ·**
- `v1r_brief.py:1467-1469` `_func_overlap = any(fn.lower() in _spec_lower for fn in f.functions)`
  **GENERALIZED FIX:** use `f.function_names or f.functions` (mirror line 1244). Low harm (it's an OR with `_spec_overlap`), but it makes `_func_overlap` effectively dead.

**12. `_entry_confidence_tier` — `path_match` uses substring `_stem in _it` · INTENT: a file whose stem matches an issue keyword is localization evidence · BUG: `_stem in _it` is an unanchored substring test on the whole issue text; a 4-char stem like `core`, `base`, `data`, `test` (the `len>3` floor permits these) matches almost any issue → spurious [WARNING] promotion of generic-named files, the exact "confident on weak signals" inversion · LIPI: Logic (substring match too permissive; no word-boundary / specificity gate) ·**
- `v1r_brief.py:1253-1254` `_stem = …; path_match = len(_stem) > 3 and _stem in _it`
  **GENERALIZED FIX:** require a word-boundary match (`re.search(rf"\b{re.escape(_stem)}\b", _it)`) AND raise the specificity floor (stem length ≥5 OR contains `_`), mirroring `_exact_issue_named_files`' own anti-generic gate (2113 `len(name) < 5 and "_" not in name`). Reuse that exact rule — it's already the codebase's generalized specificity test.

**13. `_detect_overconfident_convergence` · INTENT: detect symptom-clustering to trigger cross-domain expansion · BUG: `bm25_dominant` uses `all(... for r in top_records[:5] if r.get("score",0)>0)` — Python `all()` over an empty/all-filtered iterable returns `True`, so if every top-5 record has `score==0` (witness-promoted candidates carry `score=cand.score` which can be 0; neighbor candidates `score=rec.score*0.8`), the generator is empty and `bm25_dominant=True` vacuously → convergence falsely detected → unnecessary git-log co-change expansion fires · LIPI: Implementation (vacuous `all()` on empty filtered set) ·**
- `v1r_brief.py:1070-1074` `bm25_dominant = all(... for r in top_records[:5] if r.get("score",0)>0)`
  **GENERALIZED FIX:** guard with an explicit non-empty check: `_scored = [r for r in top_records[:5] if r.get("score",0)>0]; bm25_dominant = bool(_scored) and all(r['components'].get('lex',0) > 0.5*r['score'] for r in _scored)`. (Correctness; low frequency.)

**14. `_co_change_from_table` vs `_co_change_files` — divergent doc/config filters · INTENT: co-change files, excluding docs · BUG: the two co-change sources apply DIFFERENT exclusion sets. `_co_change_from_table` (SQL, 931-932) excludes `.md/.rst/.txt/.yml/.yaml`. `_co_change_files` (git, 886/894) excludes `.md/.rst/.txt/.yml/.yaml` too — consistent there — BUT `_expand_via_cochange` (1110/1120/1132) applies NO doc/config filter at all, so cross-domain co-change bridges can inject `CHANGELOG.md`/`docs/*.rst` as edit candidates · LIPI: Integration (sibling co-change paths, one lacks the filter its twins have) ·**
- `v1r_brief.py:1110` `for f in current_files: if os.path.dirname(f) not in symptom_dirs and f not in symptom_files:` — no extension filter
  **GENERALIZED FIX:** apply the same `_NON_SOURCE_EXTS`/doc-suffix filter (already a module constant set, 2310-2314) inside `_expand_via_cochange` before counting. Single shared non-source predicate used by every candidate-producing path.

**15. INERT/UNDELIVERED — disabled VERIFY/assertions block (1528-1584) · INTENT: surface test assertion bodies as intended-behavior spec · BUG: the entire block is `if False and …:` — dead code carrying a live SQL against the off-limits `assertions` table. It's correctly disabled for leakage, but it remains ~57 lines of dead path that a future edit could re-enable by flipping `False` · LIPI: Implementation (dead path) · note: this is intentional (leakage-safety), so the "fix" is deletion, not re-enabling.**
- `v1r_brief.py:1528` `if False and graph_db and files:`
  **GENERALIZED FIX (hygiene):** delete the block; the leakage rationale (1523-1527) is the permanent decision. Keeping `if False` invites accidental reactivation.

**16. `generate_v1r_brief` — anchors JSON written to hardcoded `/tmp` (2415) · INTENT: persist issue anchors for in-container consumers · BUG: `/tmp/gt_issue_anchors.json`, `/tmp/gt_issue_terms.txt` (2641), `/tmp/gt_l1_structured.json` (3168) are POSIX-absolute; on the Windows dev/test host these silently `OSError`→pass, so the contract pillar / post_view consumers get NO anchors when not in the Linux container, and unit tests can't exercise the consumer wiring · LIPI: Plumbing (non-portable path; cross-platform consumer break) ·**
- `v1r_brief.py:2415`, `2641`, `3168`
  **GENERALIZED FIX:** route through a single `_gt_tmp_path(name)` helper using `tempfile.gettempdir()` (or a `GT_TMP_DIR` env override), so host and container both resolve a writable dir and the consumer contract is testable off-container. Same path constant in writer and reader.

**17. `_top_functions` over-fetch math · INTENT: dedup-then-cap function titles · BUG: SQL `LIMIT ?` is bound to `max(limit*8, 24)` (line 249) to leave room for dedup, but `_top_function_names` (the issue-ranked twin) uses a hardcoded `LIMIT 20` (302/315). The two "top functions" queries for the SAME file thus draw from different candidate pools and orderings → `entry.functions` (signatures, from `_top_functions`) and `entry.function_names` (names, from `_top_function_names`) can describe DISJOINT function sets, so the rendered `(funcs)` signature list and the contract/caller evidence (keyed off `function_names`) refer to different functions · LIPI: Integration (two queries, two orderings, results assumed parallel but aren't) ·**
- `v1r_brief.py:249` `(file_path, max(limit * 8, 24))` vs `v1r_brief.py:302/315` `LIMIT 20`
- both feed the same `entries.append(... functions=funcs, function_names=func_names ...)` (2837/2845)
  **GENERALIZED FIX:** unify into one query that returns `(name, signature, ref_count)` issue-sorted, and derive both `functions` (signatures) and `function_names` (names) from that single ordered result, so the two fields are guaranteed parallel.

---

## SUMMARY / SEVERITY

| # | Function | Class | Avenue | Severity |
|---|---|---|---|---|
| 1 | assembly + 4 sibling blocks | (a) CROSS-WIRE | Integration | **P0 — the confirmed bug** |
| 4 | render_brief gate | (a) CROSS-WIRE (partial-suppress) | Integration | **P0** (same root as #1) |
| 7 | L1-SCOPE | (a) CROSS-WIRE | Plumbing | **P1** |
| 8 | _exact_issue_named ×2 | (c) inconsistent gate | Integration | **P1** |
| 3 | _resolved_witnesses (callee) | (b) WRONG-FACT (code snippet) | Plumbing | P1 |
| 9 | _l1_signal_counts align | (d)-adjacent (corrupts gate) | Plumbing | P1 (CI gate) |
| 12 | _entry_confidence_tier path_match | (e) weak-signal promote | Logic | P1 |
| 17 | _top_functions vs _names | parallel-list mismatch | Integration | P1 |
| 5 | _top_function_names | (c) inconsistent filter | Logic/Impl | P2 |
| 10 | _with_graph_map focus | hub-bias | Logic | P2 |
| 13 | _detect_overconfident_convergence | vacuous all() | Impl | P2 |
| 14 | _expand_via_cochange | (c) missing filter | Integration | P2 |
| 6 | _caller_contract func[:2] | input over-truncation | Logic | P3 |
| 11 | render_brief _func_overlap | wrong var (dead) | Impl | P3 |
| 15 | disabled VERIFY block | dead path | Impl | hygiene |
| 16 | /tmp hardcoded paths | non-portable | Plumbing | hygiene (blocks off-container test) |

**The single highest-confidence, load-bearing finding is #1 (with #4/#7 as the same wound):** make every localization-bearing sub-block derive its edit-target from `_loc.candidates[0]` (Pipe A) — one localizer ranks, every block reads that one rank — instead of `files[0]` (Pipe B, re-sorted by keyword/hub/exact-name after the localizer ran). The cleanest structural implementation is to stable-sort `top_records` by `_loc_rank_by_file` as the PRIMARY key when `_loc` fired, so `entries[0] == _loc.candidates[0]` by construction and the keyword/hub re-rankers only order *within* the localizer's structural tiers. That removes the divergence at the source rather than patching each consumer.

Relevant file: `D:\Groundtruth\src\groundtruth\pretask\v1r_brief.py` (all line refs above). Cross-pipe confirmation in `D:\Groundtruth\src\groundtruth\pretask\graph_localizer.py:1944-1952` (localizer sort/cap) and `:570-586` (`LocalizerResult.candidates` / `agreement_by_file`).

---

I have everything I need. Let me do the final granular LIPI pass function-by-function and produce the numbered findings. Let me re-examine one subtle item in `_apply_dynamic_budget` / `_neighbors` interaction and the `_second_hop_facts` over-fetch dedup behavior carefully against the visible-edge ordering, plus the `_dynamic_neighbors` sparse-threshold logic.

I have read all five focus functions plus their cross-consumers. Here is the final product-pass review.

---

# GRANULAR LIPI REVIEW — `src/groundtruth/pretask/curation_map.py`

Reviewed in order. Clean functions skipped. Each finding: FUNCTION · INTENT · BUG · LIPI avenue · file:line + quote · generalized fix.

---

**1. `_neighbors` · INTENT: return visible 1-hop CALLS neighbors, facts-first, deduped, capped. · BUG: INCONSISTENT FILTER — the deterministic-method SQL gate is gated behind `if has_method`, but the `(unverified)` rendering path in `_fmt_edge`/`Edge.is_fact` still exists, creating a self-contradiction in the no-method-column case.**
- LIPI avenue: **Integration / Plumbing.**
- `curation_map.py:276-278`: `if has_method: … sql += " AND LOWER(TRIM(e.resolution_method)) IN (…)"`. When `has_method=False`, the gate is skipped, `method_sel = "''"`, so EVERY returned edge has `resolution_method=""` → `is_fact=False` → all are forced down the `confidence >= 0.5` path. But `conf_sel = "0.0"` when `has_conf=False`. So on a legacy DB with neither column, `_neighbors` returns **nothing** (correct-or-quiet, fine). The real asymmetry: on a DB with `confidence` but NO `resolution_method` column, edges survive purely on `confidence >= 0.5`, are ALL rendered `(unverified)`, and the `is_test=0`/`type='CALLS'` filters apply but the name_match-vs-fact distinction is silently lost. This is *acceptable* by design (the docstring claims it), so this is **not** the bug.
- The actual bug: **the deterministic gate and the `n.is_test = 0` filter are both applied to the NEIGHBOR node, but the witness twin in `v1r_brief._resolved_witnesses_for_file` applies `is_test=0` to the SOURCE (`nsrc.is_test = 0`) only, and additionally applies a `_is_stdlib_shadow` guard per `(code, target_name)` that `_neighbors` has NO equivalent of.** `v1r_brief.py:682` `nsrc.is_test = 0` + the stdlib-shadow guard (`v1r_brief.py:633` "the stdlib guard is the secondary defense"). `_neighbors` has the provenance gate but **NOT the stdlib-shadow secondary defense.** So a `verified_unique` edge where `os.walk` name-matched to a project `walk` passes `_neighbors`'s gate as a FACT and renders bare in `<gt-graph-map>`, while the SAME edge is dropped in the witness path. The two symmetric surfaces disagree edge-for-edge — exactly the cross-wired/inconsistent-filter class. This is the highest-harm finding: `<gt-graph-map>` can render a laundered stdlib-shadow fact bare.
- Generalized fix: factor the `_is_stdlib_shadow(code, target_name)` guard out of `v1r_brief` into a shared helper and apply it in `_neighbors` after row construction (needs `source_line`/code, currently not SELECTed — add it). Structural: any qualified-stdlib name-match resolved to a same-named project symbol is dropped regardless of recorded provenance, on BOTH surfaces.

---

**2. `_neighbors` · INTENT (cont.) · BUG: WRONG-FACT / PLUMBING — the query never SELECTs `n.is_test` but also never SELECTs the SOURCE node's `is_test`; a TEST FUNCTION that is the *caller* (source) is surfaced as a real "called by:" edge.**
- LIPI avenue: **Plumbing.**
- `curation_map.py:269`: `WHERE {match_col} IN ({placeholders}) AND e.type = 'CALLS' AND n.is_test = 0`. Here `n` is the JOINED neighbor (`JOIN nodes n ON {join_col} = n.id`). For `direction='callers'`, `join_col = e.source_id`, so `n` = the caller, and `n.is_test=0` correctly drops a test caller. For `direction='callees'`, `n` = the callee, and `n.is_test=0` drops a test callee. So the comment at `:267-268` ("never surface a test node as a caller/callee … is_test nodes are excluded") IS satisfied for the neighbor. **But the FOCUS node itself is never checked** — if the focus function lives in a test file, its callees (production helpers) are surfaced, fine; not a bug. **This sub-item is CLEAN** — withdraw. The real `is_test` asymmetry is finding #1 (the witness twin guards source-test, neighbor twin guards neighbor-test; for callees the semantics differ but both are defensible). Low confidence; not a confirmed bug.

---

**3. `_node_ids` · INTENT: union all Function/Method node ids matching `(file_path, name)`. · BUG: PLUMBING — exact `file_path = ?` match, while the witness twin normalizes (`replace("\\","/")` + `lstrip("./")` + `LIKE '%norm_fp'`). If `focus` paths and `nodes.file_path` differ in separator or `./` prefix, `_node_ids` returns `[]` and the WHOLE map silently abstains.**
- LIPI avenue: **Plumbing (path not normalized).**
- `curation_map.py:225-228`: `"SELECT id FROM nodes WHERE file_path = ? AND name = ? AND label IN ('Function','Method')"`. Compare `v1r_brief.py:654` `_norm_fp = file_path.replace("\\", "/").lstrip("./").lstrip("/")` then `:680` `nt.file_path LIKE ?` with `(f"%{_norm_fp}", …)`. The contract_map twin (`contract_map.py:430`) ALSO uses exact `file_path = ?`. The caller `_with_graph_map` passes `f.path` (FileEntry.path) raw. **If FileEntry.path is repo-relative `beets/importer.py` but the graph stored `./beets/importer.py` or a backslash variant (Windows indexing), the exact match returns `[]` → empty map → silent abstention on a file that DOES have edges.** This is the INERT/UNDELIVERED class: the data is in the DB, the map computes nothing, the agent gets no graph-map. The fact that the witness path normalizes and this one doesn't proves the two surfaces can disagree (one delivers, one is blank) on the same file.
- Generalized fix: normalize `file_path` identically in `_node_ids` (and `contract_map`) using the SAME shared normalizer as the witness path, and match with `LIKE '%' || ? ` (suffix match) or pre-normalize both sides at index time. Structural, no task/file-specific logic.

---

**4. `_apply_dynamic_budget` · INTENT: ALL facts up to `fact_ceiling`, then `max(0, k - fact_count)` unverified hints. · BUG: LOGIC — `unverified_allowed` is computed from `raw_fact_count` (pre-cap), but the input `edges` was already truncated by `_neighbors`'s internal `max_neighbors` cap, so `raw_fact_count` is NOT the true fact count — it is the post-`_neighbors`-cap count.**
- LIPI avenue: **Logic / Integration (off-by-cap).**
- `curation_map.py:347`: `raw_fact_count = sum(1 for e in edges if e.is_fact)`. The docstring (`:343-346`) explicitly says "Shrink the unverified budget from the RAW pre-cap fact count, not the post-cap len(facts)". But `_dynamic_neighbors` calls `_neighbors(…, max_neighbors=fact_ceiling + unverified_k + 8)` (`:440`) — so `edges` here is already capped at `fact_ceiling + unverified_k + 8 = 8+3+8 = 19`. With default knobs this over-fetch (19) safely exceeds any realistic fact count, so `raw_fact_count` is effectively the true count and the intent holds. **BUT** `_neighbors` ALSO drops below-floor and dedups, and its `edges[:max_neighbors]` cap means a fact-rich hub with >19 visible neighbors would under-count facts → `unverified_allowed` computed too high → guesses leak onto a fact-rich hub. The "RAW pre-cap" claim is only true relative to the 19-cap, not relative to the actual graph. On a mega-hub (e.g. a `__init__` with 50 callers) the cap bites and the anti-guess invariant is violated. Moderate confidence; depends on hub size exceeding 19.
- Generalized fix: have `_neighbors` (or a sibling) return the TRUE fact count separately (a cheap `COUNT(*)` with the same gate) so `_apply_dynamic_budget` shrinks the guess budget against the real fact count, not the over-fetch window. Or set the over-fetch to "unbounded for the COUNT, capped for the rows."

---

**5. `_second_hop_facts` · INTENT: verified-only 2-hop rescue, dedup against `exclude`, cap at `limit`. · BUG: LOGIC / INTEGRATION — it re-calls `_neighbors` with `max_neighbors=limit*4`, but `_neighbors` returns edges ALREADY budget-unaware and re-sorted facts-first; the over-fetch `limit*4` (≤12) can truncate facts on a high-degree seed BEFORE the `exclude`/fact filter runs, so a legitimate 2-hop fact is dropped while an excluded one consumed a slot.**
- LIPI avenue: **Logic (truncate-before-filter ordering).**
- `curation_map.py:384-391`: `raw = _neighbors(conn, seed_ids, …, max_neighbors=limit * 4)`. Then `:393-409` filters `is_fact` and `(name,file) not in exclude`. Since `_neighbors` caps at `limit*4` and `_neighbors`'s sort is facts-first, the facts that survive the cap are the highest-confidence ones — but if the top `limit*4` are ALL in `exclude` (the seeds' mutual 1-hop neighbors heavily overlap the focus's 1-hop set), the loop yields **0** rescued edges even though uncapped there were valid 2-hop facts beyond position `limit*4`. The truncation happens in `_neighbors` before `_second_hop_facts` can apply `exclude`. Lower harm (rescue path, sparse targets only) but it defeats the rescue's own purpose precisely when the seed is well-connected. Moderate confidence.
- Generalized fix: over-fetch more aggressively in the rescue (e.g. `max_neighbors=limit*4 + len(exclude)`), or push the `exclude` set into the SQL `WHERE NOT IN` so truncation happens AFTER exclusion. Structural.

---

**6. `_dynamic_neighbors` · INTENT: 1-hop under dynamic budget + optional verified-only 2-hop rescue when sparse. · BUG: LOGIC — the sparseness test `len(edges) > _SECOND_HOP_SPARSE_THRESHOLD` counts UNVERIFIED edges toward "not sparse", so a target with 0 facts and 2 floor-clearing name_match guesses is treated as NOT sparse and gets no verified rescue — the exact isolated case the rescue exists for.**
- LIPI avenue: **Logic (wrong predicate input).**
- `curation_map.py:450`: `if len(edges) > _SECOND_HOP_SPARSE_THRESHOLD: return edges`. `edges` here = facts + allowed-unverified (from `_apply_dynamic_budget`). `_SECOND_HOP_SPARSE_THRESHOLD = 1`. A focus with **0 facts** but 2 unverified hints has `len(edges)=2 > 1` → rescue SKIPPED. But the docstring/knob intent (`:127-134`, `:448-449`) is "expand only the isolated/low-reach targets" — measured by VERIFIED reach (reach≈0), not by guess count. The rescue should fire on `fact_count <= threshold`, not `total_visible <= threshold`. As written, name_match noise *suppresses* the verified rescue on exactly the targets that have no real edges. Self-defeating. High confidence — the predicate uses the wrong set.
- Generalized fix: gate sparseness on the FACT count: `fact_neighbors = [e for e in edges if e.is_fact]; if len(fact_neighbors) > _SECOND_HOP_SPARSE_THRESHOLD: return edges`. Then seed from those same facts (already computed at `:453`). Structural.

---

**7. `_dynamic_neighbors` · INTENT (cont.) · BUG: PLUMBING — the 2-hop seed expansion re-runs `_node_ids(conn, e.file, e.name)` (`:456`), which has the SAME un-normalized exact-`file_path=?` match as finding #3; a 1-hop fact whose stored `file_path` differs from the form re-derived from the edge row yields no seed ids → rescue silently empties.**
- LIPI avenue: **Plumbing.**
- `curation_map.py:455-456`: `for e in fact_neighbors: seed_ids.extend(_node_ids(conn, e.file, e.name))`. `e.file` came from `n.file_path` in `_neighbors`'s SELECT, so it IS the stored form here (consistent), so round-tripping is self-consistent — **this sub-case is likely CLEAN** because `e.file` is sourced from the same column it's matched against. Low confidence it bites; flag only because it inherits #3's fragility if any normalization is interposed later. Withdraw as an independent finding; fold into #3.

---

**8. `_fmt_edge` · INTENT: render an edge as agent text; tag `(unverified)` for non-facts and `(2-hop)` for transitive. · BUG: INTEGRATION — the docstring asserts "2-hop edges are verified-only, hence always facts" so it orders `(2-hop)` before the `(unverified)` check, but `_fmt_edge` is a PUBLIC-shaped helper with no guard enforcing that invariant; a non-fact edge constructed with `hops=2` (possible via the `Edge` dataclass directly, or a future caller) renders `… (2-hop) (unverified)`, contradicting the "2-hop ⇒ fact" promise silently.**
- LIPI avenue: **Implementation (unenforced invariant / dead-comment trust).**
- `curation_map.py:569-572`: `if e.hops >= 2: base = f"{base} (2-hop)"` then `if not e.is_fact: base = f"{base} (unverified)"`. The invariant that hop-2 ⇒ fact is enforced ONLY in `_second_hop_facts` (`:394` `if not e.is_fact: continue`). `_fmt_edge` trusts it but doesn't assert it. Low harm today (only one constructor of hop=2 edges), but it's a latent self-contradiction the moment a second hop-2 producer appears. Low-moderate confidence; defensive.
- Generalized fix: either assert `e.is_fact` when `e.hops >= 2` in the `Edge.__post_init__`, or make `_fmt_edge` render a hop-2 non-fact as a hard error/skip. Structural invariant, not task-specific.

---

**9. `render_map` · INTENT: emit `<gt-graph-map>` with calls/called-by per focus that has a visible connection. · BUG: LOGIC / INTEGRATION — `fm.has_visible` gates the whole block, but the block can still render a focus line whose `callees`/`callers` are present yet the ranked-#1 in `<gt-graph-map>` need not match the ranked-#1 file the brief's `<gt-localization>`/edit-target points at (the proven cfn-lint cross-wired class), because `focus` is built from `files[:3]` order in `_with_graph_map` independent of the localization ranking.**
- LIPI avenue: **Integration (cross-wired pipes).**
- `curation_map.py:584-592` renders strictly the `focus` list it was given, in the order `_with_graph_map` (`v1r_brief.py:1312` `for f in files[:3]`) supplied. That ordering is `files` (the brief's file ranking). IF `<gt-localization>` ranks files by a DIFFERENT key than `files[:3]` (the exact cfn-lint-3749 failure: task-brief→`transform.py` vs localization→`_language_extensions.py`), then `<gt-graph-map>`'s first block keys off `files[0]` while `<gt-localization>` #1 is a different file → the agent sees a graph map centered on a file the localizer did NOT rank first. `curation_map.py` itself can't fix this (it renders what it's handed), but it is the surface where the contradiction becomes visible, and it provides NO consistency check that `focus[0]` == the localizer's #1. Moderate confidence the wiring still diverges; this is the documented codebase bug class.
- Generalized fix: `_with_graph_map` (the caller) must build `focus` from the SAME ranked list `<gt-localization>` emits, and `render_map`/`build_function_map` should accept the ranked order as authoritative (sort `maps` by the localization rank, not `files` order). Add an assertion that `focus[0].file == localization_rank_1`. Structural ordering contract, no file-specific logic.

---

**10. `render_map` · INTENT (cont.) · BUG: IMPLEMENTATION — a focus with `has_visible=True` but where ALL visible edges are unverified still emits a `<gt-graph-map>` block full of `(unverified)` guesses with NO fact, partially contradicting the module's "correct-or-quiet … when no confident connection, the map says so rather than guessing" promise (docstring `:16-18`).**
- LIPI avenue: **Logic (visible ≠ confident).**
- `curation_map.py:585` `if not fm.has_visible: continue` — `has_visible` (`:181-182`) is `is_fact OR confidence >= _NAME_MATCH_FLOOR`. So a focus with only floor-clearing name_match edges (0 facts) renders a full block of `(unverified)` lines. The docstring at `:14-18` says name_match above the floor is "shown marked `(unverified)` so the agent's grep stays the filter" — so this is INTENDED, not a bug. **Withdraw** — this is the designed correct-or-quiet posture (mid-confidence rendered as bait, per the project's agentic-RAG memory). Not a finding.

---

**11. `build_function_map` · INTENT: build maps for each focus; dynamic budget by default, legacy flat-cap when `dynamic=False`. · BUG: INTEGRATION — `dynamic=False` callers (`contract_map._verified_caller_count:663`, `contract_delta:236`) get the legacy `_neighbors` path which applies the deterministic SQL gate but NOT the dynamic budget, so `_verified_caller_count` counts facts capped at `max_neighbors=5` (default), silently under-counting verified callers on a hub and under-stating "N verified callers depend on this" in the drift block.**
- LIPI avenue: **Integration / Plumbing (cap leaks into a COUNT).**
- `curation_map.py:493` `max_neighbors: int = _DEFAULT_MAX_NEIGHBORS` (=5). `_verified_caller_count` (`contract_map.py:663`) calls `build_function_map(…, dynamic=False)` with default `max_neighbors=5`, then counts `e.is_fact` callers (`:666`). `_neighbors` caps at 5 (`:323` `return edges[:max_neighbors]`). So a function with 30 verified callers reports **5**. The drift framing (`contract_map.py:679` "{caller_count} verified caller{s} depend on this") then understates blast radius by 6×. A caller-COUNT must never be subject to a presentation cap. High confidence — a count is being truncated by a display cap.
- Generalized fix: give `_verified_caller_count` a dedicated `SELECT COUNT(DISTINCT …)` with the deterministic gate (no `max_neighbors`), or pass `max_neighbors=10**9` for the count path. Structural.

---

## Highest-priority (ship-blocking) findings, ranked
1. **#11** — verified-caller COUNT truncated by a 5-row display cap → drift block understates blast radius (high confidence, deterministic).
2. **#6** — 2-hop rescue gated on TOTAL visible count, so name_match noise suppresses the verified rescue on exactly the isolated targets it exists for (high confidence, defeats the feature's purpose).
3. **#3** — `_node_ids` exact `file_path=?` (no normalization) vs the witness twin's normalized `LIKE` → whole map silently abstains on a path-separator/`./`-prefix mismatch (high confidence on Windows-indexed or `./`-prefixed graphs).
4. **#1** — `_neighbors` lacks the `_is_stdlib_shadow` secondary guard the symmetric witness path has → `<gt-graph-map>` can render a laundered stdlib-shadow edge BARE as a fact (high harm, the documented WRONG-FACT class).
5. **#9** — `focus` ordering for `<gt-graph-map>` is `files[:3]`, not the `<gt-localization>` rank → the proven cfn-lint cross-wired self-contradiction can recur; no consistency assertion (moderate confidence, documented bug class).
6. **#4 / #5 / #8** — budget under-counts facts beyond the 19-row over-fetch on mega-hubs; rescue truncates-before-excludes; `_fmt_edge` trusts an unenforced "2-hop⇒fact" invariant (moderate/low, latent).

Withdrawn after verification (clean): #2, #7, #10 — designed behavior or self-consistent column round-trip.

Reviewed file: `D:\Groundtruth\src\groundtruth\pretask\curation_map.py`. Cross-consumers confirming the asymmetries: `D:\Groundtruth\src\groundtruth\pretask\v1r_brief.py` (`_resolved_witnesses_for_file` :614-689, `_with_graph_map` :1297-1326), `D:\Groundtruth\src\groundtruth\pretask\contract_map.py` (`_verified_caller_count` :660-666, exact-match `_node_ids` twin :430).

---

I have everything I need. The critical facts for the LIPI:

1. `_node_ids` deliberately **unions ALL** same-name Function/Method nodes (overloads, same-name methods on different classes).
2. `_node_meta` and `_node_sig_line` then pick the **lowest `start_line`** among that union — an **arbitrary** node when names collide across classes/overloads.
3. `_neighbors` returns `Edge(name, file, confidence, resolution_method)` — **no node_id, no start_line, no parent/class**. So when `edit_target_callee_contracts` re-resolves `(edge.file, edge.name)` via `_node_sig_line`, it re-derives sig+line from a **fresh** lowest-line query, NOT from the edge's actual target node.

Let me confirm one more thing: whether `properties.line` exists and how `_read_props` orders, and whether the `data_flow` Tier-B kind is in `_CONTRACT_KINDS` ordering vs caps. I've read enough. Let me write up the findings.

Here is the granular LIPI review.

---

# LIPI Review — `src/groundtruth/pretask/contract_map.py`

Read in full (725 lines) plus the upstream data sources it depends on (`curation_map._node_ids`, `_neighbors`, `Edge`, `_node_meta` semantics) and `runtime/sanitizer`. Findings are ordered by harm/confidence. Each item: FUNCTION · INTENT · BUG · LIPI avenue · file:line + quote · generalized fix.

---

### 1. `edit_target_callee_contracts` / `_node_sig_line` — WRONG-FACT: callee sig+line re-resolved from a DIFFERENT node than the edge actually points to (overload/homonym misdisambiguation)

**INTENT:** For each edit-target function, emit its verified 1-hop callees with the callee's signature + definition line — "call it with these args, defined at file:line."

**BUG (this is exactly the focus-note concern, and it is real and HIGH-confidence):** The edge that `_neighbors` returns carries only `(name, file, confidence, resolution_method)` — **no node_id**. The resolver picked a *specific* target node id structurally (e.g. `type_flow`/`impl_method` resolved `self.set_parse()` to the `Foo.set_parse` overload on line 880, not the `Bar.set_parse` on line 120). But `edit_target_callee_contracts` throws that resolved identity away and calls `_node_sig_line(conn, edge.file, edge.name)`, which re-queries by **name+file only** and returns the **lowest `start_line`** match:

`contract_map.py:428` —
```
"SELECT signature, start_line FROM nodes WHERE file_path = ? AND name = ? AND label IN ('Function','Method') ORDER BY start_line LIMIT 1"
```

When a name occurs more than once in one file (overloads, two classes with a same-named method, a `@property` + setter), `ORDER BY start_line LIMIT 1` returns an **arbitrary** node — the topmost, not the one the edge resolved to. So the emitted `CalleeContract.signature` and `.line` describe a **different function than the one being called**. This is the (e.source_line vs nt.start_line) wrong-fact class, generalized: sig+line are sourced from a node the verified edge never pointed at. The whole `CalleeContract` docstring claims "the signature + location the agent must call correctly" — but it can be the wrong overload's signature at the wrong line. `_node_ids`'s own docstring (`curation_map.py:219`) admits the multiplicity: *"A name can occur more than once in a file (overloads, methods on different classes)."*

**LIPI avenue:** Plumbing (field from the resolved row discarded; sig/line re-derived from row B) + Integration (the `_neighbors` → `_node_sig_line` boundary loses the resolved node identity).

**GENERALIZED fix:** `_neighbors` must carry the resolved neighbor `node_id` on the `Edge` (it already JOINs `nodes n` — add `n.id`). Then `_node_sig_line` (and `_node_meta`) take a `node_id` and `SELECT signature, start_line FROM nodes WHERE id = ?`, so sig+line come from **the same node the edge resolved to**. This is structural (no benchmark logic) and fixes the entire same-name-collision class for every language. Until the id is threaded, a partial mitigation is to keep the lowest-line behavior only when exactly one node matches `(file,name,Function/Method)`, and otherwise abstain (correct-or-quiet) rather than emit an arbitrary overload — but threading the id is the correct fix.

---

### 2. `_evidence_for` (own-contract path) — same WRONG-NODE defect via `_node_meta` + `_read_props` over the UNIONED id set

**INTENT:** Build a function's own contract (signature, raises, guards, return shape) from its node(s).

**BUG:** `_evidence_for` resolves `ids = _node_ids(conn, file_path, name)` — the **union of every same-name node in the file** — then:
- `_node_meta(conn, ids)` returns the signature/return_type of the **lowest-line** node only (`_node_meta`: `ORDER BY start_line LIMIT 1`, `contract_map.py:173`), while
- `_read_props(conn, ids)` reads properties across **ALL** the unioned ids (`WHERE node_id IN (placeholders)`, `contract_map.py:195`).

So for an overloaded/homonym name, the emitted `signature` belongs to ONE node but the `raises`/`guards`/`return_shape` are a **merge of all same-named functions in the file**. The agent is told `def foo(self, x: int) -> None` (overload A) with `raises: KeyError` that actually only comes from overload B. That is a self-inconsistent contract — sig from node A, behavioral facts from {A,B,C}. For a benign single-definition function this is fine; for collisions it fabricates a contract no single function has.

**LIPI avenue:** Plumbing (sig from one row, props from a different/merged row-set) + Logic (union for props, singleton for sig — asymmetric over the same id set).

**GENERALIZED fix:** Decide one node deterministically (e.g. the node whose `start_line` matches the focus anchor, or simply the lowest-line node) and read **both** `_node_meta` and `_read_props` from **that single id**, not props-over-union + sig-over-one. If the consumer genuinely wants a per-overload contract, emit one `ContractEvidence` per node id. Either way, sig and props must come from the **same** node set. Structural, language-agnostic.

---

### 3. `build_contract` callee branch vs `_evidence_for` — INCONSISTENT FILTER: the verified-edge gate is duplicated but the two paths use different downstream resolution

**INTENT:** Append verified 1-hop callee contracts that add a raise/guard/signature.

**BUG (integration, secondary to #1):** `build_contract` resolves callee evidence via `_evidence_for(conn, edge.file, edge.name, is_callee=True)` (`contract_map.py:328`) — which re-runs `_node_ids(edge.file, edge.name)` and inherits defect #2 (sig over lowest-line, props over union). The sibling lever `edit_target_callee_contracts` resolves the SAME conceptual callee via `_node_sig_line` (defect #1). Two symmetric "verified callee" paths, two **different** node-resolution helpers, both lossy in the same way but inconsistently — one merges props, the other doesn't read props at all. They can disagree about the same callee's contract. This is the two-symmetric-paths class (one gated/sourced differently than its twin).

**LIPI avenue:** Integration (two code paths computing "the verified callee's contract" diverge in how they pick the node).

**GENERALIZED fix:** Once #1's `node_id` is threaded through `Edge`, route both callee paths through a single `_evidence_for_node(conn, node_id, ...)` so the edit-target-callee and the build_contract-callee agree edge-for-edge on the same resolved node. Removes the divergence structurally.

---

### 4. `_diff_contract` — dropped-guard suppression regex assumes the guard text literally contains `raise <Name>`; silently mis-suppresses for non-raise guards and non-Python

**INTENT:** Suppress false "dropped guard" drift when the guard's exception is still raised (capture-artifact guard against the arviz add-guard FP).

**BUG:** `contract_map.py:653`:
```
_excs = re.findall(r"raise\s+([A-Za-z_][A-Za-z0-9_]*)", dropped)
if _excs and any(e in post_raises for e in _excs):
    continue
```
The suppression only fires when the guard string contains the literal token `raise Name`. But `guard_clause` values are conditional expressions (per `valid_guard_clause` → `is_well_formed_clause`), e.g. `not user` or `if x is None: return`. A guard rendered as `not user→raise ValueError` would match; a guard rendered as a bare boundary check `count > limit` (no `raise` token) never matches, so the capture-artifact suppression **cannot fire** for guards that don't textually embed `raise <Name>`. Two consequences: (a) the FP this code exists to suppress still fires for guard styles that don't inline the raise; (b) for Go/Rust/TS (`return errors.New(...)`, `panic(...)`, `throw new X()`), the regex `raise\s+Name` never matches at all → the language-agnostic claim in the module header is false for this branch; non-Python guards always read as hard drops.

**LIPI avenue:** Logic (regex keyed to one surface syntax of one language) + Implementation (silent: when the regex misses, it falls through to "dropped guard" with no signal that suppression was even attempted).

**GENERALIZED fix:** Don't pattern-match the raise keyword out of the guard text. Extract the exception identifier(s) from the guard with a language-neutral identifier scan and intersect with `post_raises`; or, better, gate the whole "dropped guard" emission on whether the function's `raises` set actually shrank (the real signal that a guard was removed rather than displaced by the indexer's per-function capture cap). That removes the Python-`raise`-token assumption and generalizes the capture-artifact guard to every language.

---

### 5. `build_contract` callee loop — `_neighbors` is called WITHOUT a confidence threshold, then the post-filter relies only on `resolution_method`; on a db with `has_method=False` the verified gate is silently a no-op (admits name_match-grade callees as "verified")

**INTENT:** "Verified edges only, so a name_match callee is never claimed."

**BUG (integration/plumbing):** Both `build_contract` (`:311`) and `edit_target_callee_contracts` (`:476`) call `_neighbors(...)`. Inside `_neighbors`, the deterministic-method SQL gate is applied **only when `has_method` is true** (`curation_map.py:276` `if has_method:`). When the edges table lacks a `resolution_method` column (`has_method=False`), `method_sel` is `''`, every edge's `resolution_method` is `""`, and the post-filter `(edge.resolution_method or "").strip().lower() not in _DETERMINISTIC_METHODS` is **always true → every callee skipped**. That direction is safe (over-suppresses). But the docstrings/comments at `contract_map.py:323` and `:488` ("never surface a name_match callee as a fact") imply the gate enforces verification; on a `has_method=False` db the contract pillar emits **zero** callees regardless of real provenance — a silent capability loss, not the intended "verified-only." This is the inert/undelivered class for older-binary dbs: the callee lever degrades to nothing with no diagnostic.

**LIPI avenue:** Plumbing (column-presence branch turns the whole sub-feature off silently) + Integration (`_neighbors`'s conf=0.0 sentinel + empty method together make the post-filter unconditionally reject).

**GENERALIZED fix:** Acceptable as a fail-closed default, but it should be **observable**: when `has_method=False`, record/log that callee contracts were suppressed for lack of provenance (so an old-binary db is detected, not silently mistaken for "no callees"). No benchmark logic — just don't conflate "db can't prove verification" with "function has no verified callees."

---

### 6. `contract_line` — INCONSISTENT with `_fmt_one`: drops `exc_flows`/`boundaries` and emits `guards` UNvalidated-for-render-collision, so the inline per-file `Contract:` line can contradict the full `<gt-contract>` block for the same function

**INTENT:** Compact single-line inline contract for the per-file brief entry; "the edit-target's own contract."

**BUG (self-contradiction / cross-pipe):** `contract_line` (`:379`) renders a **different subset** of the same `ContractEvidence` than `_fmt_one` (`:342`): it emits `raises | preserve(guards[:2]) | returns | flows[0]` but **omits** `exc_flows` (raises-when), `boundaries`, and renders only the **first two** guards (`ev.guards[:2]`) and only the **first** flow. Both functions consume the identical `build_contract` output, so the same function can surface as two non-overlapping contract claims in the same brief — the inline line says `preserve A; B` while the block says `preserve A | B | C | bounds … | raises-when …`. That is the self-contradiction/cross-pipe class within one file: two renderers off the same evidence, neither a strict subset framing of the other (the inline form silently truncates guards to 2 with no "…").

**LIPI avenue:** Integration (two render paths over one evidence object diverge) + Logic (`guards[:2]` arbitrary truncation with no continuation marker).

**GENERALIZED fix:** Make the inline form a declared **strict prefix** of the block form (same field order, same cap policy, with an explicit truncation marker), or render both from one shared formatter parameterized by `compact: bool`. Eliminates the possibility of the inline line and the block disagreeing for the same node. Structural.

---

### 7. `_callee_sig_args` — DEAD/UNDELIVERED within this module: defined for compact callee rendering but `CalleeContract` is never rendered here; risk that the consumer renders the raw multi-overload-wrong signature instead

**INTENT:** Render a callee signature compactly as `name(args)`, stripping `def`/`-> ret`/trailing colon.

**BUG (inert path):** `_callee_sig_args` is defined (`:517`) but **never called anywhere in this file**. `edit_target_callee_contracts` returns `CalleeContract.signature` as the raw `_node_sig_line` output (post-`_sanitize_signature`), and there is no `render_*` for `CalleeContract` in this module. So whether the compact form is ever applied depends entirely on an external consumer; within the file's own contract, the "compact callee" rendering is dead. If a downstream consumer forgot to call it, the brief ships `def set_parse(self, key, string: str) -> None:` verbatim — and (per #1) possibly the **wrong overload's** verbatim signature. Combined with #1 this is the cross-pipe risk made concrete: the only place that would normalize the callee signature is unwired here.

**LIPI avenue:** Integration (helper exists but is not connected to the only producer of `CalleeContract`) + Plumbing (computed-capability never reaches an observation from within this module).

**GENERALIZED fix:** Either render `CalleeContract` through `_callee_sig_args` inside this module (add the missing `render_callee_contracts`) so the compaction is guaranteed, or delete `_callee_sig_args` if the consumer owns rendering — but then document that the consumer MUST compact. Don't leave a normalization helper orphaned next to the raw-signature producer.

---

### 8. `_node_meta` / `_node_sig_line` ordering — LOW-confidence note: `ORDER BY start_line` with NULL start_line is non-deterministic across SQLite, and the int-cast in `_node_sig_line` papers over a NULL that `_node_meta` doesn't normalize

**INTENT:** Pick a single representative node deterministically.

**BUG (minor):** `_node_sig_line` coerces `int(row[1]) if row[1] is not None else 0` (`:438`) — a NULL `start_line` becomes line `0` and is emitted as `CalleeContract.line=0`, which the dataclass comment calls "0 if unknown," fine. But `ORDER BY start_line` places NULLs first in SQLite, so a node with NULL `start_line` will be **preferentially selected** as the representative over a real-line node — i.e., the worst (location-unknown) overload wins the `LIMIT 1`. `_node_meta` has the same `ORDER BY start_line LIMIT 1` and would then pull that node's signature too. This compounds #1/#2: not just an arbitrary overload, but specifically the line-unknown one when any same-name node has NULL start_line.

**LIPI avenue:** Logic (NULL ordering semantics) + Implementation (NULL→0 only handled in one of the two twin queries).

**GENERALIZED fix:** `ORDER BY start_line` → `ORDER BY (start_line IS NULL), start_line` (or `start_line NULLS LAST`) in **both** `_node_meta` and `_node_sig_line`, so a node with a known line is always preferred. Trivial, structural, removes cross-DB nondeterminism.

---

## Summary of the load-bearing findings

- **#1 + #2 are the dominant, HIGH-confidence bugs** and are precisely the focus-note target: **sig + line + behavioral props are NOT read from the same node the verified edge resolved to.** `_neighbors` discards the resolved `node_id`, forcing `_node_sig_line`/`_node_meta` to re-pick by lowest `start_line` over the same-name union — so on any overloaded name or two-classes-one-method-name file, the contract pillar emits an **arbitrary overload's signature/line** and a **union-merged** set of raises/guards. The single structural fix (thread the resolved `node_id` on `Edge`, key all node reads by id) closes #1, #2, #3, and #8 at once.
- **#4** breaks the language-agnostic claim and the FP-suppression for non-Python / non-`raise` guards.
- **#5** silently disables the callee lever on older dbs with no diagnostic.
- **#6** lets the inline `Contract:` line contradict the `<gt-contract>` block for the same function.
- **#7** leaves the callee-signature compactor orphaned, risking raw (and possibly wrong-overload) signatures shipping.

No edits made (review only). Relevant file: `D:\Groundtruth\src\groundtruth\pretask\contract_map.py`; upstream data-source defects originate in `D:\Groundtruth\src\groundtruth\pretask\curation_map.py` (`_neighbors`/`Edge` at lines 137–323, `_node_ids` at 218).

---

`_gl_normalize` is the localizer's own `_normalize` — so the agreement-key path is consistent. Good; that closes one false-positive avenue. I have everything. Here is the review.

---

# GRANULAR LIPI REVIEW — `src/groundtruth/pretask/graph_localizer.py`

Scope: the localizer's composite ranking → what becomes `candidates[0]` → whether the ranked list the localizer produces is the SAME list the brief sub-blocks consume. I read the whole file (2061 lines) plus the two consumer paths in `v1r_brief.py` (`_localization_header` @1873, the `top_records` re-rank @2429-2522, and the `files[0]` sibling block @1620-1700). Findings are ordered by confidence/harm.

---

### 1. `localize` (final sort, L1944) · INTENT: produce the canonical ranked candidate list · BUG: the localizer's `candidates[0]` is NOT the file the brief's edit-target/contract/assertion sub-blocks key off — a CROSS-WIRED PIPE between two separately-ordered lists · LIPI: **Integration (a)** · `graph_localizer.py:1944` + `v1r_brief.py:2494`

The localizer sorts by `(_grep_floor, _depth_authority, -_rrf3, file_path)` and returns `candidates`. But the brief does NOT render its file list / `top = files[0]` / `_edit_target_contracts_block(graph_db, files[0])` / assertions from `loc.candidates`. It rebuilds a *different* ordering in `v1r_brief.py:2494-2497`:
```python
_all_verified = sorted(_verified_promoted + _existing_verified, key=_loc_rank)
top_records = _all_verified + _existing_rest + _unverified_promoted
```
`_loc_rank` returns **-1 for any `_exact_issue_named` record** (`v1r_brief.py:2485-2486`), and the base `top_records` is the *lexical* run_v74 set, not the localizer set. So `files[0]` (which feeds the edit-target contracts @1594, the assertions @1519, `top = files[0]` @1630, the `<gt-graph-map>`) can be a different file than `loc.candidates[0]` (which feeds `<gt-localization>` via `_localization_header`). The two steers are sourced from two pipes with two orderings. This is the exact confirmed cfn-lint-3749 failure class. The localizer cannot fix it from inside itself, but the file's own contract is the root: it exposes only an opaque list and the consumer is free to re-sort it. **Generalized fix:** the localizer must own the single ordering — emit `candidates` as THE list and have the brief consume `loc.candidates` order verbatim for BOTH the file-list/`top` AND `<gt-localization>` (drop the `_loc_rank`/`_exact_issue_named` re-sort in the consumer), so #1 is #1 everywhere. No file/task logic; it is a "single-source-of-ordering" invariant.

---

### 2. `_localization_header` HIGH path vs `files[0]` · INTENT: pick the imperative "Edit target" file · BUG: HIGH renders about `_high_pick` (highest-ranked **non-hub** issue-witnessed cand), which is explicitly NOT `candidates[0]` and NOT `files[0]` · LIPI: **Integration / self-contradiction (a/e)** · `v1r_brief.py:1989`

```python
_high_pick = next((c for c in _high_elig if _degree_of(c.file_path) <= _hub_p80), None)
```
`_high_elig` is filtered (issue-edge + agreement≥2 + ≥2 distinct anchors) and then the first non-hub is taken. The `<gt-localization confidence="high">` block then names `tgt = _high_pick`. Meanwhile the sibling `<gt-task-brief>` names `top = files[0]` (a third ordering). Three sub-blocks, three candidate selectors (`candidates[0]`, `files[0]`, `_high_pick`). When `_high_pick != files[0]`, the brief tells the agent "Edit target: A :: func" in one block and "Highest-confidence candidate: B" in another. The localizer's responsibility here: `_witness_tier`/score already encode hub-ness weakly; the *separate* hub gate living in the consumer means the localizer's #1 and the consumer's HIGH pick are computed by different rules. **Generalized fix:** fold the hub/`_hub_p80` demotion INTO the localizer's final sort (it already has `degrees` and `_HUB_SCALE` in scope), so the localizer's `candidates[0]` IS the non-hub HIGH pick — one selection rule, consumed everywhere.

---

### 3. `localize` RRF tiebreak vs hard floor keys (L1944) · INTENT: rank within the grep floor · BUG: `-_rrf3(c)` is the 3rd sort key but `_rrf3` is built from `_struct_rank`/`_grep_rank`/`_sem_rank` keyed on `id(c)`, while a file can have MULTIPLE `Candidate` objects — the rank dicts and the agreement map disagree on which row "wins" · LIPI: **Implementation (b)** · `graph_localizer.py:1881,1886,1908,1938`

`candidates` is built one row per `fp` in `witnesses_by_file` (L1763 iterates `witnesses_by_file.items()` — so actually one row per file, good), BUT `_agreement_by_file` (L1934-1936) defensively does "A file may have multiple candidate rows; keep the MAX" — implying the author believes duplicates can exist. If a duplicate ever arises (e.g. via the promote/inject path *in the consumer* re-introducing the same path), `_struct_rank[id(c)]` is per-object and the RRF fusion silently ranks the duplicate objects independently while agreement is de-duped by path. The two consumers then read inconsistent ranks for the same file. **Generalized fix:** key `_struct_rank`/`_grep_rank`/`_sem_rank` by `_normalize(c.file_path)` (as agreement already is), not `id(c)`, so all four structures agree on the unit of ranking. Confidence: moderate (latent; depends on the consumer ever producing dup rows, which the inject path at `v1r_brief.py:2514` can).

---

### 4. `Witness.strength` (L506) · INTENT: rank witnesses; DEFINES must score below edges · BUG: a DEFINES witness's hop is always 0, so its decay factor is `1/(1+0)=1.0`, but a verified 1-hop EDGE gets `1/(1+1)=0.5` — a name-match DEFINES (0.55·conf·1.0) can BEAT a verified CALLS edge at hop 1 (1.0·conf·0.5) on raw strength · LIPI: **Logic (formula)** · `graph_localizer.py:514`

```python
return base * conf * (1.0 / (1.0 + self.hop))
```
With `_WITNESS_DEFINES=0.55` at hop 0 → 0.55·1.0 = **0.55**. A `_WITNESS_VERIFIED=1.0` edge at hop 1 with conf 1.0 → 1.0·1.0·0.5 = **0.50**. So `best_strength` (→ `W_WITNESS` term, → `confidence` field, → the `[VERIFIED]` render gate) is *higher* for the lexical DEFINES than for the real structural edge. This contradicts the module's stated invariant ("DEFINES must score BELOW edge witnesses so the graph adds value"). The hop-decay is correctly applied to edges but DEFINES gets a free pass at hop 0. **Generalized fix:** the verified/DEFINES ordering must be lexicographic (tier first), not collapsed into one scalar — `_witness_tier` already does this for the *sort*, but `Candidate.confidence` (= `best_strength`, L1806) and the `W_WITNESS` score term (L1788) use the raw scalar where the inversion bites. Cap DEFINES strength strictly below the minimum possible verified-edge strength (e.g. multiply DEFINES by a factor that keeps it under `_WITNESS_VERIFIED * conf_min * decay_maxhop`), or carry the tier into the score. Confidence: high — this is a real numeric inversion.

---

### 5. `localize` composite score (L1785) · INTENT: blend 6 signals · BUG: `_text_discount` (Herbold role discount) is applied to BM25, path-decay, witness, lex, AND subject — but path-decay and BM25 are FILE-level signals unrelated to the DEFINES function's triviality; discounting them double-penalizes a file whose *seed* function is trivial even though a *different*, non-trivial function in the file carries the real edge · LIPI: **Logic** · `graph_localizer.py:1784-1790`

```python
_best_is_defines = _best_wit and _best_wit.direction == "defines_anchor"
_text_discount = _rd if _best_is_defines else 1.0
_raw_score = (W_BM25*bm25_norm*_text_discount + W_PATH_DECAY*decay_norm*_text_discount + ...)
```
The role discount `_rd` is computed for the file's DEFINES anchor function only (L1698 `_role_discount_for_function(conn, fp, best_def.anchor)`). If that one anchor is a trivial validator (`_rd=0.2`) but the file also has a verified CALLS edge from another issue anchor, `_best_is_defines` is False only if the edge witness outstrengths the DEFINES — but per finding #4 the DEFINES can win strength, flipping `_best_is_defines` True and slashing the WHOLE file score (including its legit BM25/decay/edge contributions) by 5×. A trivial-named function shouldn't suppress the file's independent structural and lexical evidence. **Generalized fix:** apply `_text_discount` ONLY to the witness term that the DEFINES contributes (or only to `W_SUBJECT`+`W_LEX`, the lexical signals), never to BM25/path-decay/edge-witness which are independent. Confidence: moderate-high.

---

### 6. `_path_decay_scores` (L364) vs witness BFS (L1572) · INTENT: continuous decay signal over the same graph · BUG: two INDEPENDENT traversals over the same edges apply DIFFERENT admission filters — path-decay filters `e.confidence >= min_edge_conf` in SQL (L418) but the witness BFS filters in Python including the SUPPRESSED `trust_tier` hard-exclude and the `_STDLIB_ATTRS` guard (L1624-1637); a suppressed/stdlib edge that path-decay counts but witness-BFS drops gives a file a `decay_norm` boost with no corresponding witness · LIPI: **Integration (c — inconsistent filter between twins)** · `graph_localizer.py:418` vs `1624`

`_path_decay_scores` SQL has only `AND e.confidence >= {min_edge_conf}` and `n.is_test = 0`. It does NOT check `trust_tier='SUPPRESSED'` and does NOT apply the `_STDLIB_ATTRS` shadow guard. So a file reachable ONLY through a SUPPRESSED edge or an `os.walk→project.walk` stdlib-shadow gets a non-zero `decay_norm` (→ `W_PATH_DECAY=0.30` of the score) while having zero admitted witnesses. It enters `candidates` purely on a phantom decay path the witness layer explicitly rejected. **Generalized fix:** the two traversals must share ONE admission predicate. Either pass the SUPPRESSED/stdlib filter into the path-decay SQL/loop, or derive decay from the already-admitted witness edges instead of a second raw query. This is the "caller query lacks the deterministic gate its callee twin has" class. Confidence: high.

---

### 7. `localize` — `agreement_by_file` semantic-rank limb (L1931) · INTENT: count how many of {grep, struct, semantic} agree · BUG: the agreement count silently caps at 2 whenever the embedder is absent (`_sem_rank` empty), but the consumer's HIGH gate requires `agreement >= 2` (`v1r_brief.py:1986`) — so on every container WITHOUT the ONNX/ST embedder, the max attainable agreement is 2 and HIGH is gated on grep+struct BOTH hitting top-3, an unintended tightening that the localizer neither logs nor signals · LIPI: **Plumbing / inert-signal (d)** · `graph_localizer.py:1931`

```python
if _sem_rank and _sem_rank.get(id(c), _BIG) < _TOP_N_AGREE:
    _agree += 1
```
The module comment even admits "max attainable agreement is 2 in the deterministic path." But `LocalizerResult.agreement_by_file` carries no flag for whether the semantic ranker actually ran. The downstream HIGH gate (`>=2`) therefore means "2 of 3" when the embedder is present and "2 of 2" (i.e. unanimous) when it is absent — a silently different, much stricter bar on exactly the runs (no embedder) that the memory notes are common in-container. The agent gets fewer HIGH steers and nothing records why. **Generalized fix:** carry `n_signals_available` (2 or 3) in `LocalizerResult` and make the consumer's threshold relative (`>= ceil(2/3 * n_signals)` or "majority of available"), so absence of the embedder degrades gracefully and observably rather than silently raising the bar. Confidence: high (this is a real behavioral discontinuity tied to env).

---

### 8. `_fts5_candidates` (L289) · INTENT: read-only BM25 retrieval · BUG: opens a SECOND writable connection to the same `graph.db` and `CREATE`s + `INSERT`s + `COMMIT`s `nodes_fts` during what every caller treats as a read-only localization pass · LIPI: **Plumbing / Implementation** · `graph_localizer.py:289-293`

```python
_fts_conn = sqlite3.connect(_db_path)
_fts_conn_owned = True
_fts_conn.execute(_FTS5_CREATE); _fts_conn.execute(_FTS5_POPULATE); _fts_conn.commit()
```
The main `conn` is `_open_ro` (read-only WAL snapshot). This side connection writes the DB mid-brief. On a shared/concurrent graph.db (multiple tasks, or the indexer still attached) this can lock/race, and on a read-only mount it throws (caught, returns `[]`, so FTS5 silently OFF — losing the `W_BM25=0.35` signal with only a stderr line). The localizer's correctness then silently depends on whether some *earlier* run happened to write `nodes_fts`. **Generalized fix:** FTS5 population belongs in the indexer (build time), not in the query path; the localizer should treat missing `nodes_fts` as "BM25 unavailable" and proceed, never mutate graph.db. At minimum, gate the write behind an explicit opt-in env and never on the default brief path. Confidence: high (architectural — also a `mypy`/concurrency hazard).

---

### 9. `localize` lex_hits (L1770) · INTENT: count issue terms intersecting the file's symbol/path identifiers (BLUiR field-level) · BUG: the substring test `t in s or s in t` makes lexical matching non-specific — a 3-char issue term like "set" matches `set_fields`, `settings`, `reset`, `offset`, `dataset` — inflating `lex_norm` (W_LEX=0.30) for generic files and partially re-introducing the exact lexical-overconnect this module exists to kill · LIPI: **Logic** · `graph_localizer.py:1770`

```python
lex_hits = sum(1 for t in terms if any(t == s or t in s or s in t for s in symset if len(s) > 2))
```
`terms` come from `_issue_terms` with min len 3 (L597); `s` can be any symbol stem ≥3. `t in s` for `t="set"` fires on every `set*`/`*set*`. The path-seed code (L665) explicitly learned this lesson ("3 is too short for path matching — 'set' matches settings/, dataset/, reset.py") and bumped to ≥4 with component-boundary patterns — but the lex scorer here did NOT get the same fix. Inconsistent rule between two lexical sites in the same file. **Generalized fix:** require token-boundary / whole-identifier-component match (split `s` on `_`/camelCase and test membership), or raise the substring floor to ≥4 and require `t == component`, matching the path-seed discipline. Confidence: high.

---

### 10. `_grep_to_seeds` strength vs final grep score (L1460) · INTENT: per-file grep token coverage for within-floor fusion · BUG: `grep_score_by_file` is recomputed in `localize` (L1460-1467) over `[t for t in terms if len(t) >= 4]` but `_grep_to_seeds` itself selected hit files using a DIFFERENT token set (its own stoplist + `[:10]` cap, L751-759) — so a file recalled by token #11 has grep_score computed from a token list that excludes the very token that recalled it, yielding `grep_score_by_file[fp]=0` for a legitimately-recalled floor file · LIPI: **Integration (wrong-fact pairing, b)** · `graph_localizer.py:1460` vs `751`

`_grep_to_seeds` ranks/limits with a stoplist-filtered, length-≥4, top-10 token set. The post-hoc strength recompute uses `[t for t in terms if len(t) >= 4]` — no stoplist, no `[:10]` cap. The two token sets differ, so the strength used for `_grep_rank` (the spine of within-floor fusion, L1882-1886) is not the strength that earned recall. A grep-#1 gold can get `grep_score=0` here and sink in the fusion — the precise "go-cli regression" this fusion was built to fix. **Generalized fix:** have `_grep_to_seeds` RETURN the per-file distinct-token coverage it already computes (`file_scores`, L837-848) and use that authoritative value, instead of recomputing with a divergent token list. Confidence: high.

---

### 11. `_dynamic_conf_floor` / `_path_decay_scores` min_edge_conf coupling · INTENT: admit only reliable edges · BUG: `_dyn_conf` (the witness-BFS Python floor, L1626) is passed as `min_edge_conf` to path-decay (L1681), but path-decay's SQL clause `AND e.confidence >= {min_edge_conf}` is string-interpolated as a float that, when `has_conf` is False, becomes a no-op via `conf_where=""` (L418) — meanwhile the witness BFS with `has_conf=False` sets `conf_f=0.0` (L1608) and then `if not verified and conf_f < _dyn_conf: continue` DROPS every unverified edge · LIPI: **Integration (c)** · `graph_localizer.py:418` vs `1626`

When a graph lacks the `confidence` column: path-decay admits ALL CALLS/IMPORTS edges (no conf filter), so `decay_norm` is computed over the full graph; the witness BFS admits only `verified` edges (every unverified edge has `conf_f=0.0 < _dyn_conf`, dropped). Again the two traversals diverge on a column-presence branch, and a file gets decay mass with no witness. Same root as #6 but triggered by the `has_conf` branch specifically. **Generalized fix:** unify the no-confidence behavior — if `has_conf` is False, both layers must apply the SAME policy (either both admit-all-verified-only or both admit-all). Confidence: moderate-high (depends on encountering a pre-v14 graph, but those exist per the "old graph.db" comments).

---

### 12. `localize` early-return loses `agreement_by_file` and `_loc_conf` (L1508, L1669) · INTENT: bail when no seeds/witnesses · BUG: the `no_anchor_hit`/`no_witness` early returns construct `LocalizerResult` without `agreement_by_file` (defaults to `{}`) AND with `confidence=0.0`, but the `top_unverified` return at L1985 DOES pass `agreement_by_file` — so whether the agreement map reaches the consumer depends on which exit fired, and the consumer's `getattr(loc, "agreement_by_file", None) or {}` masks the difference as "no agreement" rather than "not computed" · LIPI: **Plumbing (d — computed-or-defaulted indistinguishable)** · `graph_localizer.py:1508,1669,1985`

Not a wrong number per se, but the consumer cannot distinguish "3 rankers ran and none agreed" from "we bailed before fusion." Both render LOW. For telemetry/8-dp logging (mandated) this conflates two very different localizer states. **Generalized fix:** include a `signals_ran: bool`/`gate_reason` discriminator the consumer can log; keep the field population uniform across all return sites. Confidence: moderate (observability bug, not a ranking bug).

---

### 13. `render_witness` 2-hop branch (L558) · INTENT: human-facing witness one-liner · BUG: for `hop >= 2` and `direction == "called_by_anchor"`, `far = w.dst_symbol`, but at hop≥2 `dst_symbol` is the *neighbor* symbol set during BFS where `src/dst` were assigned from a 1-hop perspective (L1640-1642) — the rendered "anchor -> ... -> far" can cite an intermediate symbol as the endpoint, not the actual 2-hop neighbor · LIPI: **Implementation (wrong var)** · `graph_localizer.py:558-563`

At hop 2 the witness's `src_symbol`/`dst_symbol` were computed (L1639-1642) using `src_name = name_of_id.get(fr_id)` where `fr_id` is the *hop-1* frontier node, and `nbr_name` the hop-2 node. So `dst_symbol` for a `called_by_anchor` 2-hop edge is the hop-1 intermediate, not the far endpoint, yet the render labels it `far`. The displayed chain `anchor -> ... -> far` may name the wrong terminal symbol. This is display-only (doesn't change ranking) but it's a wrong-fact shown to the agent. **Generalized fix:** record the true seed-anchor and true far-endpoint names per witness explicitly (the BFS already has `anchor_of_id`), and render from those rather than reusing the 1-hop `src/dst` slots. Confidence: moderate.

---

### 14. `_file_degrees` (L913) · INTENT: in-degree centrality prior · BUG: counts ALL incoming edges (`COUNT(e.id)` with no `type` or `confidence` filter), so `deg` includes name_match and non-CALLS/IMPORTS edges — the degree prior and the `_HUB_SCALE` tanh are computed over a noisier edge population than the witness/decay layers (which filter to CALLS/IMPORTS + confidence) · LIPI: **Plumbing (wrong column scope)** · `graph_localizer.py:923-926`

```sql
SELECT n.file_path, COUNT(e.id) FROM nodes n JOIN edges e ON e.target_id = n.id
WHERE n.file_path IN (...) GROUP BY n.file_path
```
No `e.type IN ('CALLS','IMPORTS')`, no confidence floor. A file with many low-confidence name_match in-edges gets an inflated degree → larger `deg_norm` (W_DEGREE=0.10) and a higher chance of tripping the consumer's `_hub_p80` hub gate (which also reads degree). Low weight, but it is the SAME degree number reused for hub detection downstream, so the noise propagates. **Generalized fix:** filter `COUNT` to `e.type IN ('CALLS','IMPORTS')` (and confidence ≥ floor when present), matching every other traversal in the file. Confidence: moderate.

---

### 15. `_path_to_seeds` LIKE patterns (L686) · INTENT: seed from path-matched files · BUG: the root-level pattern `f"{token}.%"` matches `token` anywhere a path STARTS with it, but paths in `nodes.file_path` are normalized relative (`_normalize` strips leading `./` and `/`) so a token like "util" matches `utils/...`? No — but `f"{token}.%"` with token "test" matches `test.py` AND, because LIKE is unanchored only at the wildcard, it's fine here; the real issue is patterns run in priority order but the per-pattern `LIMIT 5` + outer `limit` interact so the WEAKEST (root-level) pattern can consume the budget before the strongest (directory) pattern for a later token · LIPI: **Logic (ordering)** · `graph_localizer.py:686-710`

The loop is `for token: for pat in [dir, dir2, root]:` with a shared `out` budget of 10. A high-priority token's directory match and a low-priority token's root match compete for the same 10 slots in token order, so a strong directory hit on token #3 can be starved by root-level hits on tokens #1-2. The intent ("directory patterns first (stronger)") is only enforced *within* a token, not *across* tokens. **Generalized fix:** two-pass — collect all directory-pattern hits across all tokens first, then root-level, then truncate to `limit`. Confidence: low-moderate (path-seed is additive and capped, harm is bounded).

---

## Summary of the highest-harm, ranking/delivery findings

- **#1 + #2 (cross-wired pipes)** are the dominant product bug and exactly the class the prompt flagged: the localizer's `candidates[0]` is not authoritatively consumed — the brief re-orders into `top_records` (`_loc_rank`/`_exact_issue_named`, lexical base) for `files[0]`/contracts/assertions, and `_localization_header` picks a third file (`_high_pick`). Three sub-blocks can name three different #1 files. **Fix: one ordering, owned by the localizer, consumed verbatim.**
- **#4 + #9 (lexical inversions)** partially re-open the lexical-overconnect this module was built to close: DEFINES strength can beat a real edge (hop-0 vs hop-1 decay), and `lex_hits` uses unbounded substring matching the path-seed code already rejected.
- **#6 + #8 + #11 (twin-traversal filter divergence)** — path-decay and FTS5 run independent traversals/writes with DIFFERENT admission rules than the witness BFS, so files earn `W_PATH_DECAY`/`W_BM25` mass with zero admitted witnesses (suppressed/stdlib/no-conf edges).
- **#7 (silent embedder discontinuity)** — agreement caps at 2 without the embedder, silently making the consumer's HIGH gate (`>=2`) demand unanimity on exactly the in-container runs that lack it, with no flag.
- **#10 (grep strength recompute)** uses a divergent token set from the recall that earned floor membership — the go-cli regression can recur.

All fixes proposed are structural (single-source-of-ordering, shared admission predicate, boundary-aware lexical match, env-graceful signal counting) — no per-task/file/benchmark logic, consistent with Pillar 1.

Relevant files:
- `D:\Groundtruth\src\groundtruth\pretask\graph_localizer.py` (reviewed)
- `D:\Groundtruth\src\groundtruth\pretask\v1r_brief.py` (consumer — confirms #1, #2, #7)
- `D:\Groundtruth\src\groundtruth\confidence.py:263,320` (`is_seed_pollutant`, `dynamic_cutoff` — verified clean for this review's purposes)

---

This confirms a real path-normalization inconsistency. The downstream pipeline normalizes everything to forward slashes + strips `./` and `/` prefixes (lines 548, 680, 701, 873, 976). But inside `anchor_select.py`, the semantic and symbol maps key off raw `nodes.file_path` (no normalization), while `lex_files` arrives already forward-slashed from `SignalHit.file`. Let me confirm the merge keys collide on Windows-indexed graphs by checking line 312-344 once more — the `anchor_map` merge depends on `fp in anchor_map` matching across signals.

I have all I need. Let me also confirm the `compute_reach`/`graph_expand_candidates` keying to assess whether the `trusted` list (raw-keyed) matches the normalized candidate_set — but that's downstream of this file. The findings within `anchor_select.py` itself are now complete.

Here is the granular LIPI review of `src/groundtruth/pretask/anchor_select.py`.

---

**GRANULAR LIPI REVIEW — `src/groundtruth/pretask/anchor_select.py`**

Functions read in order: `_normalize_identifier`, `_extract_issue_tokens`, `_issue_word_parts`, `_symbol_anchors`, `_file_summary`, `_embed`, `_cache_key`, `_get_file_embeddings`, `semantic_top_k`, `select_anchors`, `structural_seed_expand`. Clean/trivial functions skipped.

---

**1. `select_anchors` · merges semantic + symbol + lexical into one `anchor_map` keyed by `file_path` · CROSS-WIRED PATH KEYS — the three signal sources key the merge dict with INCONSISTENTLY-normalized paths, so the de-dup / trust-upgrade merge silently fails on any Windows-indexed graph.**
- **LIPI avenue:** Plumbing (path not normalized consistently across two sibling pipes) + Integration (two symmetric paths, one normalized, one not).
- **Evidence:** `sem_scores` and `sym_files` key directly off raw `nodes.file_path`:
  - `anchor_select.py:312` `for fp, score in sem_scores.items():` — `sem_scores` is `semantic_top_k`'s output, keyed by raw `row[0]`/`file_path` from `_get_file_embeddings` (`anchor_select.py:181` `file_paths = [row[0] for row in c.fetchall()]` — no `.replace("\\","/")`).
  - `_symbol_anchors` likewise: `anchor_select.py:104` `file_path: str = row["file_path"] or ""` — raw.
  - But `lex_files` is forward-slash-normalized inside `hybrid.py`: `SignalHit.file` is set at `hybrid.py:397` `file_path = fr.file.replace("\\", "/")` / `:455` `file=file_path`, and `anchor_select.py:308` `lex_files = {h.file for h in lex_hits}` inherits that normalization.
- **Why it's a bug:** the merge at `anchor_select.py:332-337` does `if fp in anchor_map:` to UPGRADE trust ("Upgrade trust for files already found by another signal"). If `nodes.file_path` is stored with backslashes (Windows indexer) OR with a `./`/leading-`/` prefix, the lexical key `src/foo.py` will NOT equal the semantic key `src\foo.py`, so the same physical file lands as TWO separate `AnchorRecord`s with conflicting `reason`/trust — and the "both"/"+lexical" upgrade never fires. Every other stage in `v7_4_brief.py` normalizes (`:548,:680,:701,:873,:976,:1008` all do `.replace("\\","/").lstrip("./").lstrip("/")`); this file is the one place that does not, so the keys it returns are off-contract with the candidate set built downstream from `sem_scores.keys()` (`v7_4_brief.py:813 sem_files = set(sem_scores.keys())`).
- **Generalized fix:** normalize every file_path to the project-canonical form (`p.replace("\\","/").lstrip("./").lstrip("/")`) at the THREE ingress points — when reading `nodes.file_path` in `_get_file_embeddings` (line 181), in `_symbol_anchors` (line 104), and when consuming `lex_hits` (line 308) — BEFORE any are used as dict keys. Structural, no benchmark/task logic. (Highest-confidence finding.)

---

**2. `select_anchors` · asymmetric trust gate across the three signal pipes · INCONSISTENT FILTER — semantic anchors are confidence-gated (`score >= tau_anchor`) but symbol and lexical anchors are unconditionally `trusted_for_expansion=True`, so the "symmetric confidence gate both directions" the focus note expects does NOT exist.**
- **LIPI avenue:** Logic (one pipe gated by a threshold, its two siblings ungated) + Integration (symmetric paths where one is gated and the others are not).
- **Evidence:**
  - Semantic: `anchor_select.py:317` `"trusted_for_expansion": score >= tau_anchor,` (gated, τ=0.30 default `:271`).
  - Symbol: `anchor_select.py:323` and `:329` both hard-set `"trusted_for_expansion": True` — no score check at all.
  - Lexical: `anchor_select.py:335` `anchor_map[fp]["trusted_for_expansion"] = True` and `:343` `"trusted_for_expansion": True` — no BM25-score check; `lexical_file_search`'s `SignalHit.score` is discarded (`:308` keeps only `h.file`).
- **Why it's a bug:** `trusted` (`v7_4_brief.py:766 trusted = [a.path for a in anchors if a.trusted_for_expansion]`) is what SEEDS `graph_expand_candidates` / `compute_reach` / `compute_anchor_proximity` (`:787-796`). A weak BM25 or a loose symbol-containment match (see #3) therefore seeds full graph expansion with the SAME authority as a strong semantic hit, while a semantic file at cosine 0.29 is denied seeding. This is the exact "confident on weak signals" inversion CLAUDE.md §"Three Mandatory Properties" warns against — the gate is not confidence-gated symmetrically. CLAUDE.md Pillar 4 / `feedback_dynamic_hybrid_confidence_gated` require explicit confidence gating on every injection path.
- **Generalized fix:** apply a per-signal confidence floor symmetrically — gate the lexical path on its own normalized BM25 score (`hit.score >= tau_lex`, carry `SignalHit.score` through instead of dropping it at `:308`) and gate the symbol path on match strength (e.g. ≥2 matched parts or a normalized symbol-overlap fraction), with thresholds derived from the per-task score distribution (dynamic, not hardcoded). Trust should be a function of each signal's own calibrated score, not a blanket `True`. Structural and language-agnostic.

---

**3. `_symbol_anchors` · subset-containment match `sym_parts <= issue_parts` · LOGIC over-match — short/common symbols whose every normalized part appears anywhere in the issue are admitted as TRUSTED anchors, with no candidate-count or commonness floor.**
- **LIPI avenue:** Logic (threshold/condition too permissive) — mirrors the codebase's documented `name_match`/laundering hazard at the anchor layer.
- **Evidence:** `anchor_select.py:111` `if sym_parts <= issue_parts:` then `:112` `matched.setdefault(file_path, []).append(sym_name)`. A symbol named `get`/`run`/`data`/`load` (one part, ≥3 chars) is admitted whenever the issue text contains that word — and `_extract_issue_tokens` (`:68-69`) pulls EVERY ≥3-char word from prose, so issue narrative words ("data", "file", "test", "value") match utility symbols across unrelated files. Those files then become `reason="symbol_match"`, `trusted_for_expansion=True` (#2), and seed graph expansion.
- **Why it's a bug:** identical in shape to the `name_match` builtin-method laundering CLAUDE.md calls out ("Never name_match builtin methods `join/get/append/items/split`… a high-candidate-count name_match is not a fact"). Here it pollutes the SEED set, which is worse than polluting an edge — it misdirects the whole BFS. The hub-bias / self-contradiction failure mode (sub-block ranks a common-named hub over gold) is the downstream symptom.
- **Generalized fix:** require a minimum match specificity before trusting — e.g. drop single-part matches whose part is a high-frequency token (compute token document-frequency over the index, demote parts in the top frequency band), require ≥2 matched parts for multi-word identifiers, and/or floor by how many DISTINCT files share that symbol name (a symbol present in N files is ambiguous → low trust). Structural, no per-task keys.

---

**4. `select_anchors` · returns `(anchors, sem_seed_scores, sem_all_scores)` but `sem_all_scores` is the ONLY consumer-visible widening · INERT FULL-MAP SECOND PASS partially unused on the seed side, and a redundant matmul.**
- **LIPI avenue:** Implementation (dead/duplicated computation) + Plumbing (a computed value's relationship to the agent observation).
- **Evidence:** `semantic_top_k` is called TWICE (`:292` seed, `:297` `score_all=True`), each doing its own `file_embs @ issue_emb` + full `sorted()` (`anchor_select.py:248,:250`). The docstring claims it "only re-runs the matmul + sort" cheaply, but the matmul over every file embedding plus a full O(N log N) sort is run twice per task; the seed pass's sort result is a strict prefix of the `score_all` pass's sort, so the second sort is recomputable from the first. Minor, but it is genuinely duplicated work on the hot path, and `sem_all` is only consumed as a component lookup downstream (`v7_4_brief.py:758` `sem_all`), never for seeding — so the two-call split is real but the second sort is wasteful.
- **Why it matters (low):** not a correctness bug, an efficiency one; flagged per the exhaustive pass. The `score_all` branch returning ALL strictly-positive cosines is correct-or-quiet and fine.
- **Generalized fix:** compute scores + sort ONCE inside `semantic_top_k` (or a shared helper), then derive both the bounded slice and the full positive map from that single sorted list; return both from one call. No behavior change, removes the duplicate matmul/sort.

---

**5. `structural_seed_expand` · returns non-hub 1-hop neighbors as secondary BFS seeds · INERT / UNDELIVERED — the entire function (its symmetric callee+caller queries, hub filter, edge-weight ranking) has NO call site anywhere in `src/`; it is dead code that never reaches an agent observation.**
- **LIPI avenue:** Integration/Plumbing (computed-but-never-wired layer — the proven "INERT/UNDELIVERED" bug class (d)).
- **Evidence:** repo-wide grep for `structural_seed_expand` returns only the definition in `anchor_select.py:361` and its own docstring mention `:14`; the only other hits are stale copies under `.claude/worktrees/`. Zero callers in `src/groundtruth/`, zero in `tests/`. The module docstring (`anchor_select.py:13-17`) advertises it as the "v7.5 H1 — structural seed expansion" lever that "recovers gold files… (GRAPH_MISS bucket)" — but `select_anchors` returns `(anchors, sem_seed_scores, sem_all_scores)` (`:348`) and never invokes it, and `v7_4_brief.py:758` consumes only those three. So the documented gold-recovery mechanism is not in the live pipeline.
- **Why it's a bug:** matches `feedback_wired_means_used_not_registered` and the DEFINITION OF DONE rule — a layer that "computes + is documented" but never delivers to the agent is inert. Either it was reverted (H1 "FALSIFIED" per the v15.2 census notes) and the dead code + advertising docstring should be removed, or it is supposed to be wired and silently isn't. Right now the file's own docstring misrepresents the live behavior.
- **Generalized fix:** decide deliberately — either wire `structural_seed_expand` into `select_anchors`/`v7_4_brief.py` so its output joins the `trusted` seed set (and gate it under the same confidence floor as #2), OR delete the function and strike the "v7.5 H1 structural seed expansion" paragraph from the module docstring so the file doesn't claim a lever it doesn't ship. No task-specific logic either way.

---

**6. `structural_seed_expand` · symmetric callee+caller neighbor queries · INCONSISTENT-FILTER residue + edge-type fallback weight masks unknown types (only relevant if #5 is wired).**
- **LIPI avenue:** Logic (default weight) + Plumbing (path keys, hub-filter keying).
- **Evidence:**
  - The callee query (`anchor_select.py:386-398`) and caller query (`:402-414`) are correctly symmetric on the confidence gate (`COALESCE(e.confidence,0.5) >= ?` in BOTH) — good, the focus note's "symmetric confidence gate both directions" IS satisfied here. But:
  - `:422` `score = _EDGE_TYPE_WEIGHT.get(etype.upper(), 0.3) * float(conf)` — any edge `type` not in `{CALLS,USES,IMPORTS,CONTAINS,INHERITS}` (e.g. the resolver's `type_flow`, `impl_method`, `inherited`, `inheritance`, `data_flow` edges that CLAUDE.md says graph.db stores) silently collapses to the 0.3 fallback. Real structural edges (`impl_method`, `inheritance`) are thereby ranked BELOW a `CONTAINS` (0.4), inverting their structural value.
  - The hub filter (`:433-443`) computes in-degree over `n.file_path IN (candidates)` where `candidates` are raw-keyed from `n2.file_path`/`n1.file_path` — consistent within this function, but the returned list is raw-keyed (`:453`), so if wired it would feed raw paths into a downstream `trusted` set that's normalized elsewhere → re-triggers bug #1's mismatch.
- **Generalized fix:** extend `_EDGE_TYPE_WEIGHT` to cover the resolver's actual structural edge types (or weight by `confidence` alone and treat type as a multiplier with a sane minimum for known-structural types), and normalize the returned paths to the canonical form. Only actionable if #5 is wired.

---

**Net assessment.** Two HIGH-confidence, live-path defects: **#1 (inconsistent path normalization breaks the multi-signal anchor merge/trust-upgrade)** and **#2 (asymmetric trust gate — semantic gated by τ, symbol+lexical blanket-trusted), compounded by #3 (over-permissive subset-containment symbol match).** Together they mean the seed set fed to graph expansion can be both under-merged (same file split across reasons) and over-trusted (weak BM25/common-symbol files seeding BFS with full authority), which is the "confident on weak signals" inversion. **#5** is a documented-but-dead lever (`structural_seed_expand` has no caller) whose docstring misrepresents the live pipeline. #4 and #6 are lower-severity (efficiency / conditionally-relevant).

The file is at `D:\Groundtruth\src\groundtruth\pretask\anchor_select.py`; the consuming caller is `D:\Groundtruth\src\groundtruth\pretask\v7_4_brief.py:758-766`; the path-normalization contract this file violates is established in `D:\Groundtruth\src\groundtruth\pretask\hybrid.py:397` and `v7_4_brief.py:548,701,873,976`.

---

I have everything I need. All key dependencies are confirmed. Let me now write up the granular LIPI review.

Key confirmations from the dependency reads:
- `select_anchors` returns `(anchors, sem_seed_scores, sem_all_scores)` — `sem_all` is the full cosine map. Confirmed.
- `compute_hub_penalties` queries `target_id` in-degree filtered to `CALLS` + confidence ≥ 0.7. Confirmed.
- `lexical_file_search(issue_text, repo_root, graph_db, anchors, *, max_files=...)` — confirmed signature; note `score_all`-style filtering is internal.
- `IssueAnchors` has `symbols`, `paths`, `test_names`, `code_symbols`. Confirmed.
- `frame`/`path`/`code_def` components are NOT max-normalized (unlike `lex` and `reach`). Confirmed.

Here is the final product-pass review.

---

# GRANULAR LIPI REVIEW — `src/groundtruth/pretask/v7_4_brief.py`

Reviewed function-by-function in source order. Clean functions skipped. High-confidence findings first; lower-confidence flagged as such. No edits made.

---

### 1. `_adapt_weights_for_issue` · Dim-2 scope detection contradicts the LIVE candidate pipeline (INCONSISTENT FILTER / cross-wired source)
**INTENT:** Adapt weights from three gates: signal-presence, single-vs-multi-file scope, graph confidence.
**BUG:** The scope gate (lines 106–128) computes `_anchor_files` from `issue_anchors.symbols` via a *raw* `SELECT DISTINCT file_path FROM nodes WHERE name = ? AND is_test = 0` — a **different, ungated query** than every other file-resolution path in this file. It counts ALL files where a symbol name appears, with **no confidence gate, no test/docs filter beyond `is_test`, no basename-uniqueness**. A common method name (`get`, `run`, `__init__`) resolves to dozens of files → the gate concludes ">= 3 files = multi-file scope" → boosts `W_LEX`/`W_PATH` (lines 127–128) on nearly every real issue. The "single-file" branch (`len==1`) almost never fires because symbols are rarely globally unique. So Dim-2 is **structurally biased toward the multi-file branch** regardless of the true task scope.
**LIPI:** Logic + Integration. The scope signal is sourced from a different (raw-name) pipe than the code_def/frame resolvers that use suffix/unique-basename gating — they will disagree about how many files a symbol touches.
**file:line:** L111–116 `for sym in list(issue_anchors.symbols)[:10]: ... "SELECT DISTINCT file_path FROM nodes WHERE name = ? AND is_test = 0"` then L124 `elif len(_anchor_files) >= 3:`.
**GENERALIZED FIX:** Gate this count the same way the rest of the file does — restrict to non-ambiguous names (skip a symbol whose `COUNT(DISTINCT file_path) > N_hub`, e.g. drop names appearing in >5 files as non-discriminating), or reuse `_resolve_against_graph_files`'s unique-basename discipline. Scope must be measured on *discriminating* anchors, not on every name collision.

---

### 2. `_adapt_weights_for_issue` · `max()`/`min()` clamps silently DISCARD the signal-presence weights set 20 lines earlier (LOGIC — order-dependent override)
**INTENT:** Compose three gates additively, each with safe fallback.
**BUG:** Dim-1 sets `w["W_LEX"]=0.25` / `0.30` / `0.35` when frames/code-defs exist (lines 94/98/102). Dim-2 then does `w["W_LEX"] = max(w.get("W_LEX", 0.50), 0.55)` (L127). Because it's `max`, the multi-file branch **overwrites the Dim-1 down-weighting of LEX back up to 0.55**, even though Dim-1 deliberately *lowered* LEX because a stronger frame/code_def signal exists. The two gates are not "additive and composable" as the docstring claims (L82) — Dim-2 is a hard `max` that **erases** Dim-1's decision. A traceback issue (LEX should be 0.30) that also has ≥3 symbol-collision files ends with LEX=0.55, drowning the W_FRAME=0.80 signal it was supposed to defer to.
**LIPI:** Logic. Docstring says "compose additively" (L82–83); code composes by `max`, which is not additive and is order-sensitive.
**file:line:** L98 `w["W_LEX"] = 0.30` vs L127 `w["W_LEX"] = max(w.get("W_LEX", 0.50), 0.55)`.
**GENERALIZED FIX:** Either make the gates genuinely additive (apply multiplicative/additive deltas, not absolute `max`), or make Dim-2 respect Dim-1 by only raising LEX when Dim-1 did NOT fire (guard on `not (has_frames or has_code_defs)`). The "stronger explicit signal wins" invariant must hold across all three gates.

---

### 3. `_total_score` · `path` component read here but NOT in the RRF signal-symmetry it claims to mirror — and `path` is never normalized like `lex`/`reach` (PLUMBING / wrong-scale)
**INTENT:** Linear weighted sum of components, hub-penalized.
**BUG (scale):** `lex` and `reach` are max-normalized to [0,1] (L900–903, L910–922). `path`, `frame`, `code_def` are **NOT** — they carry raw construction values (path ∈ {0.4,0.5,0.7,1.0}; frame = `1/(idx+1)`; code_def = `1/n`). With `W_PATH=0.45`, `W_FRAME=0.60`, `W_CODE_DEF=0.70` and these raw magnitudes near 1.0, the unnormalized path/frame/code_def terms systematically **out-weigh** the normalized sem/lex/reach terms whose post-normalization values are spread across [0,1] but typically small for the gold file. This is the documented hub-bias failure-shape: an entry/hub file that matches an issue keyword in its basename gets `path=0.7 * 0.45 = 0.315` "for free," competitive with a gold file's entire evidence stack.
**LIPI:** Plumbing (incommensurate scales summed) + Logic.
**file:line:** L447 `+ weights.get("W_PATH", 0) * components.get("path", 0.0)` (no normalization upstream) vs L900–903 lex normalization.
**GENERALIZED FIX:** Max-normalize `path`, `frame`, `code_def` component maps to [0,1] before scoring (same treatment as `lex`/`reach`), so all six linear terms are scale-commensurate and the weights mean what they say.

---

### 4. `run_v74` · RRF fusion uses `_RRF_SIGNALS_FULL` that OMITS `hub_pen` and `commit`, AND the legacy path's hub penalty is silently dropped in RRF mode (INERT / self-contradiction)
**INTENT:** Optional rank-based fusion as an alternative to the linear sum.
**BUG:** `_RRF_SIGNALS_FULL = ("sem","lex","reach","anchor_prox","path","frame","code_def")` (L480) — there is **no hub term**. In RRF mode (L990–995) the score is `_rrf.get(fp)` with **zero hub penalty applied at all**, whereas the linear path applies `hub_sub` (L469). So switching `GT_RRF_FUSION=on` silently disables the entire B4 hub-mislocalization defense that the file's own comment (L455–468) calls load-bearing. The two ranking paths are not equivalent modulo fusion math — one defends against hubs, the other does not.
**LIPI:** Integration (two symmetric ranking paths; one gated/defended, the other not) + Inert (hub_pen is computed at L783, lives in components_map, but is never consumed in RRF mode).
**file:line:** L480 signal tuple (no hub) vs L469 `hub_sub = w_hub * hub_pen` in the linear path only.
**GENERALIZED FIX:** In RRF mode, subtract a hub-rank demotion (rank files by `hub_pen` ascending as a negative signal, or post-multiply the RRF score by `max(0, 1 - w_hub*hub_pen)`), so both fusion paths carry the same hub defense.

---

### 5. `run_v74` · `effective_w_sem` reports a weight that is FALSE whenever sem was never consumed — the observability field it exists to power is itself wrong (PLUMBING / undelivered-truth)
**INTENT:** Report the W_SEM actually applied after all three zeroing branches, for a fail-closed precheck.
**BUG:** `effective_w_sem = 0.0 if _sem_dropped_by_rrf else float(effective_weights.get("W_SEM", 0.0))` (L1122). This captures branches ① (`_SEMANTIC_AVAILABLE`) and ③ (RRF det). But in **legacy linear mode with a Zero embedder that still set `_SEMANTIC_AVAILABLE=False`** the W_SEM was already zeroed at L745 so that's fine — HOWEVER, the field reports the *nominal weight* even when **every** `sem_component` is 0 (embedder present but produced all-zero cosines, or `sem_all` empty). The docstring admits this ("whether the embedder was CONSUMED ... is a separate fact" L1119), but the field is **named `effective_w_sem`** and will read e.g. `0.15` while contributing exactly 0 to every score. A precheck asserting `effective_w_sem > 0 ⇒ semantic is working` is satisfiable with zero actual semantic influence. The field misleads the exact fail-closed gate it was built for.
**LIPI:** Plumbing — a number "computed but the truth it implies is never actually delivered." The truth lives in `sem_components_full`; the headline number contradicts it.
**file:line:** L1122 `effective_w_sem = ... float(effective_weights.get("W_SEM", 0.0))`.
**GENERALIZED FIX:** Make `effective_w_sem` reflect consumption, not just nomination: `eff = nominal_w_sem if any(c > 0 for c in sem_components_full) else 0.0`. Then the field cannot read positive while sem contributes nothing.

---

### 6. `run_v74` · the candidate-set BM25 recall (`_lex_candidates`) and the component-scoring BM25 (`_lex_hits`) are TWO SEPARATE `lexical_file_search` calls with different `max_files` — divergent rankings feed candidate membership vs component scores (INTEGRATION / two pipes, different ordering)
**INTENT:** Use BM25 both to seed candidates and to score the `lex` component.
**BUG:** `_lex_candidates = lexical_file_search(..., max_files=max(20, len(candidate_set)))` (L819) drives candidate-set membership and the `bm25_raw` diagnostic (L1066). A SECOND call `_lex_hits = lexical_file_search(..., max_files=max(50, len(all_files)))` (L895) drives the actual `lex` component scores (L902–903). Two calls with different `max_files` → BM25's internal df/idf corpus and the `_max_lex` normalizer (L900) differ between the membership decision and the scoring decision. A file admitted to the candidate set by call #1 can receive a **different normalized lex score** from call #2 (different `_max_lex` denominator), and the diagnostic `bm25_raw` (from call #1) won't match the `lex` component (from call #2). This is the cross-wired-pipe class: the brief's diagnosis says one BM25 number, the ranker used another.
**LIPI:** Integration. Same callee invoked twice with different params; downstream consumers pair row-A's BM25 (diagnostic) with row-B's BM25 (score).
**file:line:** L819 (`max(20, ...)`) and L895 (`max(50, ...)`) — two calls.
**GENERALIZED FIX:** Call `lexical_file_search` ONCE with `max_files=max(50, len(all_files))`, reuse the result for both candidate seeding (top-10) and component scoring. One BM25 pass → one consistent normalizer → diagnostic matches the score.

---

### 7. `run_v74` · path-rescue (L826–842) and path-prior (L948–972) re-implement the SAME issue-word logic TWICE with divergent normalization — files rescued but not scored, or scored on a different basename (IMPLEMENTATION / duplicated divergent logic)
**INTENT:** Add path-keyword-matched files to candidates (rescue), then score them (prior).
**BUG:** Rescue at L830 builds `_issue_words_fn` from `findall(r"[A-Za-z_]\w{2,}")` filtered `len>=4`, matches against `os.path.basename(fp).rsplit(".",1)[0].lower()`. The prior at L951 rebuilds `_issue_words` with the **identical** regex/filter but then *also* checks directory parts (L964–970) and `basename.replace("_","")` (L961). The rescue uses bidirectional substring on the **raw** basename; the prior uses three tiers. A file can be rescued into the candidate set by the rescue's looser basename test yet receive `path=0.0` from the prior if the prior's stricter `==`/substring tiers miss it — OR vice versa. Two copies of "issue words" that can drift. Additionally the rescue queries `SELECT DISTINCT file_path FROM nodes WHERE is_test = 0` while resolution elsewhere normalizes slashes — rescue adds the **raw** `fp` (L839), but the prior keys on `os.path.basename(fp)` of that raw path. Inconsistent path normalization between the two stages.
**LIPI:** Implementation (duplicated logic) + Integration (rescue list and prior scores can disagree on the same file).
**file:line:** L830 `_issue_words_fn = set(... if len(w) >= 4)` vs L951 `_issue_words = set(... if len(w) >= 4)` — two independent computations.
**GENERALIZED FIX:** Compute issue-words and the path-match score ONCE in a helper `path_match_score(fp, issue_words)`; use it both to decide rescue (score>0 ⇒ add) and to fill the `path` component. Single source → rescued ⇔ scored, guaranteed.

---

### 8. `run_v74` · `_adapt_weights_for_issue` is called AFTER `_ablation_weights` zeroed W_REACH/W_PROX for ablation A, but Dim-2/Dim-3 re-inflate them (INTEGRATION / gate fights ablation)
**INTENT:** Weights flow: defaults → ablation zeroing → signal/scope/confidence adaptation.
**BUG:** For `ablation="A"`, `_ablation_weights` sets `W_REACH=0.0, W_PROX=0.0` (L512). Then `_adapt_weights_for_issue` (called at L866 unconditionally) can do `w["W_REACH"]=max(w.get("W_REACH",0.05),0.15)` (L122) or `0.12` (L150), **re-inflating reach/prox that ablation A explicitly zeroed**. Variant A is supposed to be "dense only" (docstring L15, L511). The scope/confidence gates silently break the ablation contract — A is no longer dense-only when the graph is high-confidence. Same for B0/B1 which the comment claims are W_SEM=W_LEX=0 graph-only, yet Dim-2 multi-file can set `W_LEX=0.55` (L127) on a B0 run. Note: `_score_variant_A`/`_B` zero the components anyway so the *score* may survive, but `effective_weights` (reported in hyperparameters L1105 and the diagnosis L1076) is now **lying** about the ablation — and for variant C/D the re-inflation is fully live.
**LIPI:** Integration. The adaptation gate is not ablation-aware; it overrides the ablation's deliberate zeroing.
**file:line:** L730 `_ablation_weights(...)` → L866 `_adapt_weights_for_issue(...)` runs afterward with no ablation guard.
**GENERALIZED FIX:** Pass `ablation` into `_adapt_weights_for_issue` and skip (or re-apply ablation zeroing after) for non-C/D variants; OR re-run `_ablation_weights` once more after adaptation so the ablation contract is the final word.

---

### 9. `_compute_frame_scores` · stack-frame depth decay assumes `parse_stack_traces` returns DEEPEST-FIRST, but indexes by enumeration order with no verification (LOGIC — unproven ordering invariant)
**INTENT:** Score frames deepest=1.0, shallower less, via `1/(idx+1)`.
**BUG:** `for idx, fr in enumerate(frames): s = 1.0/(idx+1)` (L614–618). The docstring (L584–586) and `W_FRAME` comment assert `frames` is deepest-first. If `parse_stack_traces` returns frames in **printed order** (Python tracebacks print *outermost first, deepest last*), then `idx=0` is the OUTERMOST (least specific) frame getting 1.0, and the deepest in-repo frame — the 98.3%-correlation one the design is built on (L585) — gets the smallest score. This inverts the entire signal. The ordering is a load-bearing invariant consumed here but **defined in another module** and not asserted at this boundary.
**LIPI:** Logic (decay direction) + Integration (consumes `traces.parse_stack_traces` ordering contract without verifying it).
**file:line:** L614–618 `for idx, fr in enumerate(frames): ... s = 1.0 / (idx + 1)`.
**GENERALIZED FIX:** Do not rely on positional order for "depth." Have `parse_stack_traces` return an explicit `depth`/`is_deepest` per frame (or reverse to canonical deepest-first at this boundary) and decay on that, with an assertion. Confidence: MODERATE — depends on the documented contract of `parse_stack_traces`, which I did not open; flagging as a must-verify because the whole frame signal flips if it's wrong.

---

### 10. `_compute_code_symbol_scores` · `lookup_name` takes ONLY the last dotted component, collapsing `a.trusted_hosts` and `b.trusted_hosts` and inflating false definition sites (LOGIC — over-broad resolution)
**INTENT:** Resolve a backtick symbol to its definition file(s) via `nodes.name`.
**BUG:** `parts = sym.split("."); lookup_name = parts[-1]` (L665–666) then `SELECT ... WHERE name = ?`. For `request.trusted_hosts` it searches `name='trusted_hosts'` across the ENTIRE graph — every class/module with an attribute/method named `trusted_hosts`. The receiver (`request`) is discarded, so this is exactly the unresolved-method-call ambiguity the project's own CLAUDE.md flags as the 58% garbage problem. The `1/n` weight (L678) damps it, but a symbol defined in 8 files still seeds 8 candidates each at 0.125, polluting the candidate set (L875–877 adds all of them). The `len(lookup_name) < 3` guard (L667) only drops `id`/`os`-length names, not the ambiguity.
**LIPI:** Logic. Receiver type discarded → name-only match → the documented method-ambiguity bug.
**file:line:** L666 `lookup_name = parts[-1] if parts else sym`.
**GENERALIZED FIX:** When the symbol is dotted, prefer rows where `qualified_name` ends with the full dotted form (`...request.trusted_hosts` or a class whose name matches `parts[-2]`), falling back to last-component only when the qualified match is empty AND the last component is globally unique (same unique-or-quiet discipline as `_resolve_against_graph_files` §2 fallback). Drop high-`n` (e.g. n>5) matches entirely rather than seeding noise.

---

### 11. `run_v74` · `code_def`/`frame` candidate injection adds the NORMALIZED path, but `all_files` then mixes raw and normalized keys → component lookup can miss (PLUMBING — path normalization split)
**INTENT:** Add frame/code_def-resolved files to the candidate set; later read their components by normalized key.
**BUG:** Candidate injection adds `resolved_norm` (already slash-normalized, from `code_def_scores`/`frame_scores` keys) at L877/L884. But other candidates entered raw (e.g. rescue adds raw `fp` at L839; sem/graph paths come from `nodes.file_path` which may carry `./` or backslashes). Then `all_files = list(candidate_set)` (L886) holds a MIX of normalized and raw keys. The component injection loop (L975–984) re-normalizes each `fp` to `_fp_norm` to look up frame/code_def — good — BUT `path_scores` (L952) is keyed on the **raw** `fp`, and `lex_scores`/`sem`/`reach` are keyed on whatever `nodes.file_path` raw form `lexical_file_search`/`compute_reach` returned. If the SAME file is present once as `src/x.py` (from frame_norm) and once as `./src/x.py` (raw from another source), it becomes **two distinct candidates** ranked separately, splitting its evidence across two rows — and the gold can land in the weaker of the two.
**LIPI:** Plumbing — path not normalized consistently across the sources merged into `candidate_set`.
**file:line:** L877 `candidate_set.add(resolved_norm_cd)` (normalized) vs L839 `candidate_set.add(fp)` (raw) vs L886 `all_files = list(candidate_set)`.
**GENERALIZED FIX:** Normalize every path to one canonical form (`replace("\\","/").lstrip("./").lstrip("/")`) at the moment it enters `candidate_set`, for ALL sources (sem, graph, lex, rescue, frame, code_def), and key all component maps on the canonical form. One path identity, no row-splitting.

---

### 12. `_total_score` · reach is hub-discounted by `max(0, 1 - hub_pen)` but sem/lex/path/frame are NOT — asymmetric hub treatment across evidence (LOGIC — inconsistent penalty application)
**INTENT:** Discount reach through hubs; then subtract a global hub penalty.
**BUG:** `reach_contrib = W_REACH * reach * max(0.0, 1.0 - hub_pen)` (L440) applies a **per-term** hub discount to reach ONLY. Then a **global** `hub_sub = w_hub * hub_pen` is subtracted from the total (L469). So a hub file is penalized once on its reach term (multiplicatively) and again globally (subtractively) — **double-penalized on reach**, single-penalized on sem/lex/path/frame. There's no stated rationale for reach getting both treatments. For a legitimate cross-cutting hub that is the true gold (the case the file comment at L4–7 of hub_penalty.py explicitly warns about — "hub files are sometimes legitimate fix sites"), reach is suppressed twice, pushing the genuine hub-gold down.
**LIPI:** Logic. Two different hub-penalty mechanisms stacked on one term, one on the others — asymmetric and unjustified.
**file:line:** L440 `reach_contrib = ... * max(0.0, 1.0 - hub_pen)` AND L469 `hub_sub = w_hub * hub_pen`.
**GENERALIZED FIX:** Choose one hub mechanism. Either keep the global `hub_sub` and drop the per-term reach discount (so all evidence types are treated symmetrically), or keep the per-term path-specificity discount and apply it to all structural terms uniformly. Do not double-count on reach alone.

---

### 13. `run_v74` · docs-penalty / source-boost is applied AFTER scoring but BEFORE the tie-break sort, multiplicatively scaling RRF scores too (INTEGRATION — adjustment valid for linear sum, distorts RRF)
**INTENT:** Demote docs files, boost source files post-scoring.
**BUG:** `sc *= (1.0 - _docs_penalty)` / `sc *= _source_boost` (L1010/L1012) runs on `scored` regardless of fusion mode. For the linear-sum path the scores are additive magnitudes where a multiplicative nudge is sensible. For RRF (L991/L994) the scores are tiny reciprocal-rank sums (`Σ 1/(60+rank)`, order 0.01–0.1) where a `*1.1` boost is a **rank-order-meaningless** perturbation that can arbitrarily reorder near-tied files — RRF's whole premise (L474–478) is scale-invariance, and this multiply re-introduces scale sensitivity. The doc-penalty was designed for one scorer and silently leaks onto the other.
**LIPI:** Integration. One post-processing step applied to two incompatible score spaces.
**file:line:** L1005–1014 adjustment block runs after the `if _rrf_mode ...` branch with no mode guard.
**GENERALIZED FIX:** Apply docs/source as a **rank-space** adjustment in RRF mode (e.g. add/subtract a fixed reciprocal-rank bonus, or demote docs by appending a penalty rank), and keep the multiplicative form only for the linear-sum path.

---

### 14. `run_v74` · `sem_component_scores = sem_all if sem_all else sem_scores` falls back to the BOUNDED seed map, re-introducing the spurious-zero bug it was built to fix (LOGIC — fallback defeats the fix)
**INTENT:** Use the full cosine map for the `sem` component; fall back to seed map if full is empty.
**BUG:** L934 `sem_component_scores = sem_all if sem_all else sem_scores`. The comment (L925–933) explains `sem_all` exists precisely so candidates outside the top-k get their REAL cosine instead of 0. But when `sem_all` is empty (truthy-falsy on `{}`), it falls back to `sem_scores` (the bounded top-k seed map) — which is exactly the "spurious 0 outside top-k" behavior the decoupling was meant to kill. Worse: `sem_all` is empty **only when the embedder produced no strictly-positive cosines** (per `semantic_top_k` `score_all` filter, anchor_select L252–258), i.e. the embedder is OFF — in which case `sem_scores` is *also* empty or near-empty. So the fallback is mostly harmless BUT semantically wrong: if `sem_all` is empty due to all-negative-but-present cosines (a degenerate but real embedder state) while `sem_scores` (top-k, unfiltered for positivity) holds values, the component silently uses the wrong (seed) map. Low-harm but a latent correctness hole and a contradiction with the stated invariant.
**LIPI:** Logic (fallback contradicts the documented decoupling intent).
**file:line:** L934 `sem_component_scores = sem_all if sem_all else sem_scores`.
**GENERALIZED FIX:** Drop the fallback — use `sem_all` unconditionally (empty map ⇒ component 0 everywhere, the correct no-op). If a fallback is wanted, fall back to `{}`, never to the bounded seed map.

---

### 15. `run_v74` · `k_sem_top_effective = max(int(k_sem_top), len(ranked_records))` is a tautological/misleading observability number (IMPLEMENTATION — meaningless metric)
**INTENT:** Report the cap actually in force for the sem-component map, for a precheck.
**BUG:** L1135 `k_sem_top_effective = max(int(k_sem_top), len(ranked_records))`. `ranked_records` length == candidate count, which is almost always ≫ `k_sem_top` (20). So this field essentially always reports `len(ranked_records)` — it's just the candidate count under a different name, carrying no information about any actual "cap." The comment (L1130–1134) claims it proves "the cap scaled with candidates shown," but since `sem_all` is uncapped (every positive cosine), there is no cap at all — reporting `max(20, N)` dresses up "no cap" as a scaled cap. A precheck reading this learns nothing.
**LIPI:** Implementation. A telemetry field whose value is definitionally redundant with `candidate_set_size`.
**file:line:** L1135 `k_sem_top_effective = max(int(k_sem_top), len(ranked_records))`.
**GENERALIZED FIX:** Report the real fact: `k_sem_top_effective = len(sem_components_full nonzero)` (how many candidates actually carried a positive sem component), or just drop the field and let `sem_components_full` carry the truth. Don't synthesize a "cap" that isn't one.

---

### 16. `_get_model` · global `_SEMANTIC_AVAILABLE` is process-cached and never reset, so a per-call `weights` override or a second repo with a different embedder state reads a STALE flag (PLUMBING / INTEGRATION — cross-call state bleed)
**INTENT:** Cache the embedder and its availability per process.
**BUG:** `_CACHED_MODEL`/`_SEMANTIC_AVAILABLE` are module globals (L261–263) set once on first `_get_model()` (L322, L338/348/367). `run_v74` then reads the global `_SEMANTIC_AVAILABLE` at L744 to zero `W_SEM`. In a long-lived server process (the MCP server is exactly this) the FIRST task's embedder-probe result is frozen for ALL subsequent tasks. If the first call happened before models were baked (Zero fallback → `_SEMANTIC_AVAILABLE=False`), **every later task zeros W_SEM forever** even after the embedder becomes available — and the `effective_w_sem` field (Finding 5) will report 0.0 across the whole run, masking the real state. No invalidation, no per-graph keying.
**LIPI:** Plumbing/Integration. Config (embedder availability) does not refresh across calls in a persistent process.
**file:line:** L263 `_SEMANTIC_AVAILABLE: bool | None = None` (module global) read at L744 `if not _SEMANTIC_AVAILABLE:`.
**GENERALIZED FIX:** Either re-probe on each `run_v74` (cheap: the model load is still cached separately), or carry `_SEMANTIC_AVAILABLE` on the returned model object and read it from the instance, not a frozen module global. Confidence: MODERATE on real-world impact (depends on whether the process is reused before models are baked), HIGH on the mechanism.

---

### Clean / not flagged
`_is_docs_file`, `_is_source_dir`, `_score_variant_A/B/C`, `_rrf_fuse`, `_ablation_weights` (correct given its inputs; the ablation-violation is in the *caller* — Finding 8), `_resolve_against_graph_files` (the suffix/unique-basename discipline is sound and is the correct pattern the rest of the file should reuse), `graph_file_paths_for_frame` — read, no high-confidence bug.

---

### Highest-priority (ship-blocking) ranked
1. **Finding 3** (unnormalized path/frame/code_def vs normalized sem/lex/reach — systematic hub/keyword over-weighting; this is the live hub-bias mechanism).
2. **Finding 4** (RRF mode silently drops the hub defense).
3. **Finding 6 + 7 + 11** (BM25 called twice / path logic duplicated / path normalization split — the cross-wired-pipe + row-splitting class this codebase has been bitten by before).
4. **Finding 2 + 8** (`_adapt_weights_for_issue` `max`-override erases signal-presence weights and re-inflates ablated weights).
5. **Finding 5** (`effective_w_sem` lies to the fail-closed precheck it exists to feed).
6. **Finding 9 + 10** (frame depth-order invariant unverified; code_def last-component name match re-introduces method ambiguity).

All proposed fixes are structural (normalization symmetry, single-source helpers, canonical path identity, ablation-aware gating, consumption-based observability) — no task/file/benchmark-specific logic. Relevant file: `D:\Groundtruth\src\groundtruth\pretask\v7_4_brief.py`. Cross-module invariant to verify externally: `D:\Groundtruth\src\groundtruth\pretask\traces.py::parse_stack_traces` frame ordering (Finding 9).

---

I have everything I need. Let me verify two more load-bearing details: the `_signature_has_varargs` behavior against type hints (potential false suppression) and the `_resolve_node_id` label filter vs the behavioral-contract inline resolver (which queries by name with NO label filter).

I've confirmed the divergence. Now I'll produce the granular LIPI review.

# GRANULAR LIPI REVIEW — `src/groundtruth/hooks/post_edit.py` (4541 lines, read in full)

Findings are ordered by confidence/severity. Each is FUNCTION · INTENT · BUG · LIPI avenue · file:line + quote · generalized fix. Clean functions skipped.

---

**1. `_get_callers_from_graph` (callers) vs `_get_interface_peers_from_graph`/`_get_name_match_peers`/`_find_same_name_twins`/`_get_siblings_from_graph` (consistency) — INCONSISTENT FILTER (bug class c)**
- INTENT: surface cross-file callers (pillar 3) and structural twins/peers/siblings (pillar 2) as FACTS.
- BUG: the **caller** query gates on the categorical edge filter (`_edge_filter_for_db`, line 920) so only `same_file`/`import`/`type_flow`/CERTIFIED edges pass. But the **consistency** queries that pull nodes/edges have NO such gate: `_get_interface_peers_from_graph` uses raw `COALESCE(confidence,0.5) >= 0.5` on EXTENDS/IMPLEMENTS (line 1230, 1284), `_get_name_match_peers` (line 1368) and `_find_same_name_twins` (line 1539) select nodes by **name alone with zero edge/confidence gate**, and `_get_override_chain` (line 1430) walks EXTENDS/IMPLEMENTS with no confidence filter at all. The `[TWIN]`/`[PEER]`/`[OVERRIDE]` blocks therefore launder name_match-grade structural guesses as facts on the SAME edit where the caller block correctly suppressed them. This is exactly the divergence CLAUDE.md warns about (method-call edges are 58% name_match; a same-name twin in another file is "a coincidental name clash" the code itself acknowledges at line 1518 but only de-dupes same-file/same-class — it never checks the edge that links them is real).
- LIPI: Integration (two symmetric structural paths, one gated, one not).
- `post_edit.py:1539` — `"SELECT id, file_path, parent_id, start_line FROM nodes WHERE name = ? AND label IN ('Function', 'Method') AND id != ? AND is_test = 0"` (no confidence/edge gate); vs `post_edit.py:928` — `AND {edge_filter}`.
- FIX: route every consistency query (peers, twins, name-match-peers, override chain) through the same categorical trust gate the caller query uses — share one helper that asserts the relating edge (EXTENDS/IMPLEMENTS/same parent_id provenance) is in `DETERMINISTIC_RESOLUTION_METHODS` or a verified hierarchy edge, not bare `confidence>=0.5`. Structural, no task logic.

---

**2. Behavioral-contract inline resolver vs `_resolve_node_id` — WRONG-FACT / cross-wired node (bug class b)**
- INTENT: find the edited function's node to pull its `properties` (the contract).
- BUG: the contract path (lines 2705–2725) reimplements node resolution but queries `SELECT id,start_line,end_line,file_path FROM nodes WHERE name = ?` with **NO `label IN ('Function','Method')` filter and NO is_exported tiebreak**, whereas the canonical `_resolve_node_id` (line 288) filters to Function/Method and disambiguates by is_exported then lowest id. On a name collision where a Class/Variable node shares the function's name (or two same-name methods exist), the contract resolver picks `_bc_node_id` by longest-path-suffix only, with ties resolved by **iteration order** (`> _best_match_len`, line 2718, never `>=`, so first-seen wins arbitrarily). Result: `_bc_node_id` can point at a DIFFERENT node than `resolved_target_id` used by the caller/callee/signature blocks in the SAME func iteration → the `[BEHAVIORAL CONTRACT]` properties (PARAMS/RAISES/RETURNS) get paired with row A while callers/signature describe row B. Self-contradicting evidence for one function.
- LIPI: Plumbing (field from row A paired with row B) + Implementation (divergent resolver, off-by-tiebreak).
- `post_edit.py:2706` — `"SELECT id, start_line, end_line, file_path FROM nodes WHERE name = ?"` (no label filter); diverges from `post_edit.py:288`.
- FIX: delete the inline resolver; call `_resolve_node_id(...)` for `_bc_node_id` too, then a single `SELECT start_line,end_line FROM nodes WHERE id=?`. One resolver → contract and callers describe the same node by construction.

---

**3. `_get_callers_from_graph` hop-2 wrapper expansion — INCONSISTENT FILTER bleak (bug class c)**
- INTENT: when only 1 caller exists, follow a thin wrapper one hop for more context.
- BUG: the hop-2 block (lines 1040–1053) selects callers-of-the-wrapper but the `edge_filter` is interpolated into the `JOIN ... AND {edge_filter}` — good — yet the wrapper-loop appends results with `"confidence": str(float(wrapper.get("confidence","0.5")) * 0.9)` (line 1081) and **no `resolution_method`/`return_usage`/`arg_mapping`/`pre_context` keys**. Downstream `format_risk_evidence` computes `aggregate_confidence` from these synthetic 0.9×-decayed values (line 3036), and `_check_arity_mismatch` reads `c.get("resolution_method","")` which is now absent for hop-2 callers → they silently fall to the "medium" branch (line 1974) and can manufacture a `[GT_CONTRACT]` arity warning against a 2-hop transitive caller that doesn't call the function directly at all. The "[via wrapper]" prefix (line 1073) is also fed verbatim into `_extract_call_arity(c.get("code",""), func_name)` — but the wrapper's code line calls the WRAPPER, not `func_name`, so `_extract_call_arity` returns None and the row is skipped — except the synthetic confidence still pollutes the median.
- LIPI: Plumbing (computed field pollutes a downstream aggregate it shouldn't) + Integration (hop-2 rows lack the schema the consumers assume).
- `post_edit.py:1081` — `"confidence": str(float(wrapper.get("confidence", "0.5")) * 0.9)`.
- FIX: tag hop-2 rows with an explicit `"hop": 2` and exclude them from `caller_confidences` and from `_check_arity_mismatch` (arity must only use direct callers). Or give them a sentinel low confidence that the risk/arity gates skip. Structural.

---

**4. `_categorical_edge_filter_clause` — name_match admit clause is structurally dead / mislabeled (bug class a, logic)**
- INTENT (per docstring lines 157–163): admit edges where `resolution_method` is strong OR `name_match with candidate_count<=1` OR trust_tier CERTIFIED/CANDIDATE.
- BUG: the docstring promises a "`name_match` with `candidate_count <= 1`" admit branch, but the **emitted SQL has no candidate_count term at all** (lines 169–175). The actual clause is `(res_method IN strong) OR (trust_tier IN (CERTIFIED,CANDIDATE) AND res_method != 'name_match')` AND `trust_tier != SUPPRESSED`. So a CERTIFIED-but-name_match edge is admitted by neither branch (excluded by the `!= 'name_match'` guard) — fine — but the docstring's unique-name admit is a phantom; any reader trusting it (and the MEMORY note "100% of cc<=1 name_match are qualified_unresolved") will mis-reason about what passes. More importantly: `DETERMINISTIC_RESOLUTION_METHODS` now **includes `unique_method`** (confirmed in curation_map.py:92), which is the renamed unique-name case — so the intended behavior moved to the strong set, but the stale docstring still describes the deleted clause. Low runtime harm, high audit-confusion risk; a future edit "restoring" the documented clause would re-admit raw name_match.
- LIPI: Logic (docstring/contract vs implementation mismatch) + Implementation (dead documented path).
- `post_edit.py:157` docstring vs `post_edit.py:169-175` body.
- FIX: delete the obsolete docstring branch; state the clause is `strong-method OR (strong-tier AND not name_match), excluding SUPPRESSED`. No code change to behavior.

---

**5. `_get_targeted_verification_suggestion` / `_get_test_assertions_from_graph` — DEAD CODE after early `return` (bug class d-adjacent, implementation)**
- INTENT: now intentionally disabled (swap-invariant leakage guard) — both `return ""`/`return []` at the top (lines 2073, 1628).
- BUG: ~100 lines of unreachable code follow each early return (the whole verify-query body 2074–2181, the assertions body 1629–1701), importing `VERIFY_LABEL_HIGH_METHODS` etc. inside the dead block. This is not a runtime bug but it is a live LIPI hazard: `_get_targeted_verification_suggestion` is still CALLED at line 3479 (`verify_line = _get_targeted_verification_suggestion(...)`) and its result appended to `output_parts` — currently always `""`, so inert, but the call site reads as if `[GT_VERIFY ...]` can still reach the agent. Anyone re-enabling by deleting the top `return` reactivates a known leakage (the comment says "run12 leaked test_plot_hdi"). The `_L3_REAL_MARKERS`/`_L3_HEADER_ONLY_MARKERS` tables still list `[TEST]`, `[COMPLETENESS]`, `Verify:`, `Impact:` (lines 2475–2495) that the disabled paths can no longer emit, so `_l3_account_evidence` counts a marker class that is now impossible — slightly inflating the "real" denominator semantics is fine, but it's stale.
- LIPI: Implementation (dead path that a one-line revert reactivates into a known leak).
- `post_edit.py:2073` — `return ""` then live `from ... import VERIFY_LABEL_HIGH_METHODS` at 2074.
- FIX: physically delete the dead bodies (keep a one-line stub + comment), and drop `[TEST]`/`[COMPLETENESS]`/`Verify:` from the marker tables so accounting reflects what can actually render. Prevents accidental re-leak.

---

**6. `_check_arity_mismatch` via `_signature_has_varargs` — over-broad suppression (bug class a, logic)**
- INTENT: skip the arity check when the signature has `*args`/`**kwargs` (true varargs absorb extra args).
- BUG: `_signature_has_varargs` returns True if `"*" in signature` (line 1886). A modern Python signature routinely contains `*` as a **keyword-only marker** (`def f(a, *, b)`) or in type hints/defaults; any of those makes the function look variadic and **silently disables the entire arity-mismatch contract** even though arity IS fixed. This is the dominant real contract signal (`[GT_CONTRACT high]`, line 1992) and it gets suppressed on a large fraction of real functions. Correct-or-quiet is fine, but here it's quiet-when-it-should-speak on a structural property the agent needs (a caller passing too few args after a signature change).
- LIPI: Logic (wrong predicate — `*` ≠ varargs).
- `post_edit.py:1886` — `return "*" in signature`.
- FIX: detect varargs by parsing the param list for a token that is exactly `*name`/`**name` (a `*` immediately followed by an identifier and not the bare keyword-only `*,`), e.g. regex `(?<![\w)])\*\*?\w` on the inner param string from `_signature_param_count`'s extractor. Structural, language-aware for Python; for other langs fall back to the existing `_EXT_TO_LANG`-style guard.

---

**7. `_co_change_reminder` cochanges fast-path — WRONG-PARTNER via unanchored LIKE (bug class b, plumbing)**
- INTENT: pull pre-mined co-change partners for the edited file from the `cochanges` table.
- BUG: it matches `file_a LIKE '%<esc>'` / `file_b LIKE '%<esc>'` (lines 690–693) — a **suffix** match on the normalized path. `norm_fp = beets/importer.py` will also match a stored `tests/test_importer.py`? No — but it WILL match `other/sub/importer.py` and any path ending in `importer.py` in a different package. The de-dupe guard `not norm_fp.endswith(partner) and not partner.endswith(norm_fp)` (line 696) only filters the file against ITSELF, not against the wrong-package homonym that the LIKE pulled in. So `[CO-CHANGE]` can list a partner that co-changed with a same-basename file in a DIFFERENT directory. The git-log fallback (lines 703–727) is exact (`norm_fp in current_commit_files`), so the two sources disagree — the fast path is looser than its fallback twin.
- LIPI: Plumbing (suffix LIKE pairs row A's partner with row B's file) + Integration (fast path looser than fallback).
- `post_edit.py:690` — `"... WHERE file_a LIKE ? ESCAPE '\\' ..."` with `(f"%{_esc}", ...)`.
- FIX: require a path-boundary on the suffix (`'%/' || file` or exact match) so `importer.py` can't match `x/importer.py` across packages; or match on the resolved stored path equality like the other queries via `_resolve_file_path`. Structural.

---

**8. `generate_improved_evidence` U-shaped reorder vs primacy markers — SELF-CONTRADICTION with the budget cap (bug class e, logic/integration)**
- INTENT: budget-trim `func_parts` (lines 3439–3447), THEN U-shape reorder so PRESERVE/REVIEW/SIGNATURE lead (3454–3459).
- BUG: order of operations drops high-value content before it can be promoted. The budget loop (3442) trims from the **tail of the current (pre-U-shape) order**, which at that point has been re-ranked by `issue_grounding` (3430–3432, sorted by issue score), NOT by pillar value. So a high-value `PRESERVE:`/`[SIGNATURE]` line sitting late in the issue-grounding order gets cut by the char budget, and THEN the U-shape reorder runs on the survivors — promoting whatever PRESERVE/SIGNATURE happened to survive while the cut one is already gone. The contract-normalization step earlier (`_normalize_contract_lines`, line 2916) correctly pre-sorts guards-first specifically so a downstream cap keeps them — but that ordering is then destroyed by issue_grounding's re-sort (3432) before the cap, defeating C1d's stated purpose ("high-value content sorts ahead ... survives a downstream cap").
- LIPI: Logic (cap-before-promote) + Integration (issue_grounding re-sort undoes contract_sort_rank ordering that the cap depends on).
- `post_edit.py:3430-3447` (issue_grounding sort → budget cap) precede `post_edit.py:3454-3459` (U-shape).
- FIX: apply the U-shaped/pillar-priority reorder BEFORE the budget cap so the cap pops from the tail of the value-ranked order (low-value last), and have issue_grounding re-rank only WITHIN the middle tier, not across primacy/recency. Structural.

---

**9. `_annotate_evidence_header` Phase-4 connected-file query — never delivered + wrong join (bug class d + b)**
- INTENT: when callers have 0 issue-keyword overlap, suggest connected files that DO overlap.
- BUG (delivery): `_annotate_evidence_header` is **never called anywhere** in the file (grep: only its def). It computes a `[NOTE]`/"Connected file ... keyword matches" header that no code path appends to `output_parts`. Pure INERT/UNDELIVERED layer (class d). Separately, its SQL (lines 397–407) is a malformed self-join: `JOIN edges e ON (e.source_id = n1.id OR e.target_id = n1.id) AND COALESCE(e.confidence,0.5) >= 0.7` then `JOIN nodes n2 ON (n2.id = e.source_id OR n2.id = e.target_id)` — the `OR` join can bind n2 back to n1's own node (n2.id = the same endpoint), so `n2.file_path != ?` is the only thing preventing self-suggestion, and a single edge yields the file on BOTH sides → duplicate/degenerate rows. Dead, but if revived it misdirects.
- LIPI: Plumbing (computed, never reaches agent) + Implementation (OR-join binds n2 to n1).
- `post_edit.py:366` def with no call site; SQL `post_edit.py:397`.
- FIX: either wire it into the caller-render path (append the header when `relevant_count==0`) with a corrected directional join (`e.source_id=n1.id JOIN n2 ON n2.id=e.target_id` UNION the reverse), or delete it. Don't ship a dead misdirecting query.

---

**10. `_l3_line_is_real` indentation heuristic — counts wrapper noise as REAL (bug class d, logic)**
- INTENT: classify a delivered line as real evidence vs metadata-only shell, for the fail-closed observability gate.
- BUG: the final clause `if line.startswith("  ") and len(s) > 3: return True` (line 2530) treats ANY 2-space-indented line >3 chars as real evidence. But many INDENTED lines are not evidence: the `[GT L3: post_failure]` header path, `Calls into:` continuation, and crucially the budget-cap can slice a block mid-line leaving a dangling indented fragment that counts as "real." The accounting (`l3_real_evidence`) is the metric a fail-closed GATE consumes (per CLAUDE.md "DEFINITION OF DONE: metrics changed"), so an over-counting heuristic makes a hollow L3 look populated — the exact "fired ≠ delivered" failure the constitution forbids. Indented `  PARAMS: x [required]` is genuinely real, but `  else:` from a full-body fallback (line 2745, `func_parts.append(f"  {_bl.rstrip()}")`) is also counted real though it's a raw source line with no contract semantics.
- LIPI: Logic (indentation is not a content predicate).
- `post_edit.py:2530` — `if line.startswith("  ") and len(s) > 3: return True`.
- FIX: require the indented line to carry a known contract token (`PRESERVE:`/`L\d+:`/`[RAISES]`/`FIELD:`/`file:line` shape) rather than mere indentation; otherwise count as metadata. Tightens the gate's denominator honestly.

---

**11. Callee block — `_resolve_callees_fp` may be None, silently widening the result (bug class b, plumbing)**
- INTENT: list cross-file callees (`Calls into:`), excluding the edited file itself via `nt.file_path != ?`.
- BUG: `_resolved_callees_fp = _resolve_file_path(_callees_conn, file_path)` (line 3058) returns `None` when the path is unknown/ambiguous (documented at line 67). The query then binds `nt.file_path != NULL` (line 3068) — in SQL `!= NULL` is `NULL` (never true) for EVERY row, so the `!= file` self-exclusion is **silently disabled** and the edited file's own functions get listed as "Calls into:" entries. The caller-side blocks guard against None (a None bind to `= ?` yields 0 rows, the documented safe behavior), but here None is used with `!=`, which is the opposite — it widens instead of narrowing.
- LIPI: Plumbing (None path not normalized → `!= NULL` disables the filter).
- `post_edit.py:3068` — `f"AND nt.file_path != ? "` bound with possibly-None `_resolved_callees_fp`.
- FIX: when `_resolved_callees_fp is None`, skip the callee block entirely (correct-or-quiet) or use `COALESCE(nt.file_path,'') != COALESCE(?, '\x00')`. Same fix applies to `_detect_edit_propagation` (line 571, `nsrc.file_path != nt.file_path` is safe there since both from the join, OK) — the vulnerable ones are any `!= ?` bound with a resolver result. Audit all `!= ?` binds.

---

**12. `_normalize_contract_lines` dedup key vs `_get_callers`/twins — cross-block dedup gap (bug class a, minor)**
- INTENT: drop exact-duplicate and empty contract lines, sort high-value first.
- BUG: dedup keys on `ln.strip()` (line 2301) — exact string. But `PRESERVE: if x then return` and `PRESERVE: if x  then return` (whitespace variance from `clip_balanced`) won't collapse, and a `[RAISES] ValueError` aggregated line (inserted at index 0, line 2886) can duplicate an individual `[RAISES] ValueError` exception_flow line (line 2797) that has different surrounding text — the Tier-A/Tier-B dedup at line 2843 only strips `exception_type` values found in the `exception_flow` BLOB, not the reverse, so a flow line and a types line naming the same exception both render. Low harm (redundant, not wrong), but it burns the 800-char contract budget on dupes, pushing real guards past the cap.
- LIPI: Logic (string-exact dedup misses semantic dupes across the two raise-emit paths).
- `post_edit.py:2301` (`key = ln.strip()`) + the two RAISES emit sites 2797/2886.
- FIX: normalize whitespace in the dedup key (`re.sub(r"\s+"," ",...)`) and dedup `[RAISES]`/`[RETURNS]` by extracted value set across both paths before rendering. Structural.

---

## Summary of the highest-leverage items
- **#1 and #2 are the dominant product bugs**: the consistency family (twins/peers/override/siblings) is NOT gated by the categorical trust filter that the caller family IS gated by, and the behavioral-contract node resolver diverges from `_resolve_node_id` — together these mean a single edit can ship a `[TWIN]`/`[PEER]`/`[BEHAVIORAL CONTRACT]` keyed to a name-collision node while the caller/signature blocks describe a different (correctly-gated) node. That is the self-contradicting, false-map failure the codebase docs flag as "flying blind."
- **#11 (`!= NULL` disabling self-exclusion)** is a concrete plumbing defect that pollutes "Calls into:" with the edited file's own symbols whenever the path doesn't resolve.
- **#6 (`"*" in signature`)** silently kills the strongest contract signal on any keyword-only/typed Python signature.
- **#5/#9** are inert/dead paths that are one revert away from re-leaking test names (#5) or misdirecting (#9).

All fixes proposed are structural (gate by edge provenance, share the one resolver, parse params instead of substring `*`, anchor LIKE on path boundaries, reorder cap-after-promote) — none key on a task, repo, file, or gold label (Pillar 1 preserved).

No files were edited. The reviewed file is `D:\Groundtruth\src\groundtruth\hooks\post_edit.py`; cross-referenced `D:\Groundtruth\src\groundtruth\pretask\curation_map.py` (shared `DETERMINISTIC_RESOLUTION_METHODS`, line 82) and `D:\Groundtruth\src\groundtruth\graph\ego.py` (`change_impact`, line 381).

---

I have everything needed. Here is the granular LIPI review of `src/groundtruth/hooks/post_view.py`.

---

# LIPI Review — `src/groundtruth/hooks/post_view.py`

Reviewed function-by-function in source order. Clean functions skipped. Findings are concrete; confidence stated per item.

---

### 1. `_l3b_line_priority` (lines 45–64) · INTENT: rank rendered lines so the cap-trimmer drops least-important first · BUG: dead priority bands → contract pillar lines are MISCLASSIFIED as lowest-priority and dropped FIRST · LIPI: **Integration + Logic** · HIGH confidence

The trimmer is keyed off line-prefix strings that **the renderer never produces**. Priority 0 matches `[CONTRACT]`/`[SIGNATURE]`/`[RAISES]`/`PRESERVE:` (line 49); priority 1 matches `[TEST]` (line 52). But:
- The contract pillar (`_contract_pillar`, line 274/276) emits `"[CONTRACT] {sig} -> {ret}"` — that DOES start with `[CONTRACT]`, so it maps to 0. Good.
- BUT the ego-graph text (`out.insert(0, _ego_text)`, line 1168) is a multi-line block whose first line is `"<name>() in <file>:<line>"` (ego.py:97) — it starts with neither `[FOCUS:` nor `[Progress:` nor `ego` (line 55 checks `"ego" in s.lower()[:6]` — the literal substring "ego" in the first 6 chars, which `"set_fields() in importer.py"` does NOT contain). So the **entire ego block falls to priority 5 and is trimmed before generic caller lines.** The most-confident pillar (RepoGraph ego, gated at min_confidence=0.9) is dropped first.
- `Called by:` (line 986) → priority 3. `[CONTRACT] flows:` (line 312) → priority 0 (rides with contracts, fine).
- `[TEST]` (line 52, priority 1) is **dead** — every `[TEST]` emitter in this file is DISABLED (lines 1200–1211, 1307–1313). Priority band 1 never fires.

**Net effect:** when the cap trims (the file's stated reason for existing — "runs showed 5355 tokens"), it preserves `[CONTRACT]` and `Called by:` but **discards the ego-graph first**, inverting the stated preservation order ("PRESERVING the highest-confidence evidence … ego-graph", line 35/81). 

**Generalized fix:** classify by the actual structured shape, not magic prefixes. Tag each line with its pillar at append time (carry a parallel `(priority, line)` list out of `graph_navigation` instead of re-deriving priority from a string post-hoc), or make `_l3b_line_priority` recognize the ego block's real first-line shape (`r"^\w+\(\) in .+:\d+$"`).

---

### 2. `_enforce_l3b_cap` (lines 74–107) · INTENT: trim rendered block to ≤600 tokens, worst-first, order-preserving · BUG: O(n²) re-estimation + the "never drop the last priority-0 line" guard can leave the block ABOVE budget silently while reporting `cap_enforced=True` · LIPI: **Implementation** · MODERATE confidence

Two issues:
- **(a) Quadratic rebuild (perf, not correctness):** line 96 rebuilds `kept` from scratch (full list comprehension + re-join token estimate) on every iteration of `drop_order`. For the 5000-token blocks this exists to handle, that is fine in practice (small n), but it is gratuitously O(n²·chars).
- **(b) Silent over-budget with a truthy `cap_enforced`:** the loop breaks (line 104–105) when only priority-0 lines remain even if still over budget — correct-or-quiet by design. But `_enforce_l3b_cap` then returns `(kept, tokens, True)` and `_emit_l3b_cap_event` records `l3b_exceeded_cap = final_tokens > 600` (line 126). So the telemetry says "cap enforced" AND "exceeded cap" simultaneously. That's internally contradictory for a metrics reader auditing whether the budget held. Not agent-facing harm, but per the DEEP-LOGGING rule this corrupts the cap audit.

**Generalized fix:** (a) maintain a running token count and subtract the dropped line's contribution instead of re-joining; (b) when the priority-0-floor break fires, set a distinct flag (`cap_floored=True`) rather than overloading `cap_enforced`, so telemetry distinguishes "trimmed to fit" from "couldn't fit, delivered anyway."

---

### 3. `_contract_pillar` (lines 149–313) · INTENT: deliver signature/return contract for the ISSUE function in the viewed file, suppress generic top-of-file noise · This is the function the focus note flags ("3749 wrong-function bug"). Multiple findings.

**3a · BUG: anchor SQL pre-filter sorts anchors to front, but `LIMIT 30` is still applied AFTER the CASE — when there are >30 anchor-matched functions OR the anchor isn't in this file, a deep non-anchor relevant function is still cut. More importantly, the `_relevance` re-rank (line 244) re-sorts the 30 fetched rows but `issue_terms` term-overlap can promote a WRONG same-prefixed function over the anchor.** · LIPI: **Logic** · MODERATE confidence**

Line 239–242: when `issue_terms` is present, every candidate gets `score += len(parts & issue_terms)`. A code-symbol anchor scores 300; but a non-anchor function sharing two issue terms scores `0 + 2 = 2` — fine, anchor still wins. However a *title* anchor (200) vs a *code* anchor (300) plus term bonuses can reorder within-tier in ways that aren't obviously what you want, and the term bonus is added to the anchor tiers too, so a body-anchor (100) + 3 terms (103) can beat a title-anchor (200)? No — 103 < 200. The tiering holds. **Lower-confidence sub-finding; the tier gaps (100/200/300) are wide enough to dominate term bonuses (typically 0–4).** Not a confirmed bug — withdrawing to LOW.

**3b · BUG (CONFIRMED, the homonym/wrong-file class): `_contract_pillar` queries `WHERE file_path = ? (needle)` but `needle` passed in is the result of `_resolve_file_path` from `graph_navigation` — EXCEPT the contract pillar is re-invoked at line 1186 with the SAME `needle`, which is correct. HOWEVER the anchor match is purely by NAME (`LOWER(name) IN (anchors)`, line 211) with NO file scoping beyond `file_path = needle`. That's correct here. The real cross-wire is elsewhere (see 3c).** · No bug in scoping. Skip.

**3c · BUG: the suppression gate (line 262) only checks `_relevance(ranked[0]) == 0`, but the FLOWS block (lines 297–312) appends `[CONTRACT] flows:` for EVERY delivered function name regardless of whether that function is the anchor — and flows are pulled by `n.name = ?` across the WHOLE FILE with no file-identity tie to the rendered signature.** · LIPI: **Plumbing (row A paired with row B)** · MODERATE-HIGH confidence

Lines 301–307: the data_flow query is `WHERE n.file_path = ? AND n.name = ?`. If two functions in the file share a name (overload, or method + module function), `LIMIT 1` picks an arbitrary one's flow and renders it under a `[CONTRACT] flows:` line that the agent associates with the signature shown above. The signature dedup (line 277) drops duplicate *rendered signatures*, but `_delivered_fns` still appends the name once per surviving signature — and the flow lookup re-resolves by name, not by the node id of the signature that was actually shown. **A homonym method's def-use flow can be stapled to a different overload's contract.** This is the "wrong-fact — row A paired with row B" class, exactly the bug class the prompt flags.

**Generalized fix:** carry the node `id` of each delivered signature (add `id` to the SELECT at lines 207–222), and join the flow query on `p.node_id = ?` (the exact id), not on `n.name = ?`. Eliminates the homonym mis-pairing structurally.

**3d · BUG: the `return_type` render guard `"->" not in sig_text` (line 273) suppresses the explicit return type whenever the raw signature string already contains any `->`, including a `->` inside a parameter default or a lambda/Callable type annotation.** · LIPI: **Implementation** · LOW-MODERATE confidence

`def f(cb: Callable[[int], str] = lambda x: x) -> bool` — `sig_text` contains `->`? No, `Callable[[int],str]` has no `->`. But `def f(cb: Callable[..., int]) -> X`… still no literal `->` in the param. A param-default of a lambda has no `->`. The realistic trigger is a signature already rendered WITH its return (`name(args) -> ret`), in which case skipping the appended `ret` is correct. **This is mostly defensive and correct; the false-suppression case (a `->` token inside an annotation) is rare.** LOW confidence — leaving as a noted edge case, not a confirmed harm.

---

### 4. `_same_stored_file` (lines 405–427) · INTENT: C2 homonym guard — confirm ego center lives in the viewed file · BUG: component-aligned suffix match treats a bare basename as matching ANY path ending in that basename, so two different files named `models.py` in different packages are judged "same file" · LIPI: **Logic** · HIGH confidence

Lines 422–427: `n = min(len(parts_a), len(parts_b))` then `parts_a[-n:] == parts_b[-n:]`. If `a = "importer.py"` (bare, as ego.center.file_path could be a basename-only stored path) and `b = "beets/importer.py"`, `n=1`, compares `["importer.py"] == ["importer.py"]` → True. **Correct for the intended case.** But if `a = "core/models.py"` and `b = "api/models.py"`, `n=2`, compares `["core","models.py"] == ["api","models.py"]` → False. Good. The failure is the **bare-basename-vs-full-path** case: `a="set_fields"`'s node stored as `"importer.py"` and the viewed `needle="zero.py"` → not same (good). But `a="importer.py"` vs `b="vendor/copy/importer.py"` → `n=1` → True, even though they're genuinely different files. 

This is precisely the C2 guard's own stated risk inverted: it is **too permissive on bare basenames** (n collapses to 1) — a homonym in `vendor/importer.py` would pass the guard against a viewed `src/importer.py`. The guard reduces but does not eliminate the wrong-file class when stored paths are basename-only.

**Generalized fix:** require the suffix match to be **anchored at a path boundary AND of length ≥2 components when both paths have ≥2 components**; only allow the n=1 (basename-only) match when ONE of the two paths is genuinely a bare basename (no `/`). I.e. gate the `n==1` branch on `("/" not in pa) or ("/" not in pb)`.

---

### 5. `_is_test_file` (lines 439–444) · INTENT: route test files to the test-target path · BUG: `/fixtures/` classified as a test file, but the prefix probe `base.startswith("test_")` and the `/test/` substring will also match legitimate source dirs like `/latest/` (no — `/test/` requires slashes) · LIPI: **Logic** · LOW confidence

`"/testing/"` and `/test/` are slash-bounded so `latest`/`contest` won't match. `base.startswith("test_")` is fine. **`/fixtures/` is the one debatable inclusion** — a fixtures dir often contains non-test sample source the agent may genuinely need to edit, and routing it to `_test_file_targets` (which only emits `Calls into:` and suppresses everything else) silences the contract/nav pillars for those files. LOW confidence; defensible either way. Noted, not a confirmed bug.

---

### 6. `_score_by_issue_relevance` (lines 522–548) · INTENT: re-rank neighbor files by issue-term + anchor match · BUG: anchor symbol matched as a raw substring against the full lowercased file PATH, so a short anchor like `id`, `os`, or `db` matches spuriously inside unrelated path components · LIPI: **Logic** · MODERATE confidence

Line 536: `anchor_hits = sum(2 for s in _anchor_syms if s in fp_lower)`. `s in fp_lower` is an unanchored substring test over the whole path string. A two/three-char anchor symbol (`id`, `os`, `db`, `add`) hits inside `models/`, `oslib/`, `dbutils/`, `address.py` etc., awarding +2 spuriously and reordering neighbors toward noise. The contract pillar already strips generic/dunder anchors but does NOT strip short symbols, and this consumer applies no length floor.

**Generalized fix:** match anchor symbols against **path COMPONENTS with word-boundary identity** (split `fp_lower` on `/`, `.`, `_` and test set membership), not raw substring; and apply a minimum anchor length (≥3) consistent with the localizer's anchor hygiene. Structural, no task-specific logic.

---

### 7. `graph_navigation` — hub-scale degree query (lines 890–893) · INTENT: compute p90 in-degree to scale the hub penalty repo-relatively · BUG: this query hardcodes `COALESCE(e.confidence,0.5) >= 0.7` while every OTHER edge query in the function uses the categorical `_ef` filter — the hub denominator is computed on a DIFFERENT edge population than the caller/callee numerators · LIPI: **Integration (symmetric paths, one gated differently)** · HIGH confidence

Line 891: 
```
"... JOIN edges e ON e.target_id = n.id AND e.type = 'CALLS' AND COALESCE(e.confidence, 0.5) >= 0.7 GROUP BY n.file_path ..."
```
versus callers (line 764) and callees (line 854) which use `AND {_ef}` (the categorical `resolution_method/trust_tier/candidate_count` clause from `_edge_filter_for_db`). On a post-merge schema, `_ef` admits a DIFFERENT set of edges than `confidence >= 0.7` (categorical can admit conf-NULL deterministic edges and reject high-conf name_match). So `_in_degree_for_file` (called via `_hub_penalized_score`, line 896) ALSO uses raw `e.type='CALLS'` with **no confidence/categorical filter at all** (lines 613–621) — a THIRD population. **Three different edge sets feed one ranking formula:** numerator `cnt` from `_ef`, `in_deg` from unfiltered edges, `hub_scale` from `conf>=0.7`. The penalty is therefore miscalibrated — `in_deg` counts speculative name_match edges the numerator already excluded, over-penalizing files whose incoming edges are mostly low-confidence noise.

**Generalized fix:** thread the single `_ef` clause through all three: `_in_degree_for_file` and the `hub_scale` query must take and apply the same `edge_filter` string the caller/callee queries use. One edge population, one ranking.

---

### 8. `graph_navigation` — `_edge_filter` confidence-floor mismatch (lines 134–147 vs post_edit `_edge_filter_for_db` default `min_conf=0.6`) · INTENT: share the L3 edge filter as single source of truth · BUG: the in-function numeric FALLBACK (line 146) uses `>= 0.7`, but the delegated `_edge_filter_for_db` numeric fallback uses `min_conf=0.6` (post_edit.py:188) — two different "fallback" floors depending on which except path fires · LIPI: **Integration** · MODERATE confidence

`_edge_filter` (line 142–146): on success returns `_edge_filter_for_db(db_path, alias)` → numeric fallback `0.6`. On `except` returns the local `>= 0.7`. So the same hook on the same DB applies 0.6 or 0.7 depending solely on whether the import succeeded. Inconsistent threshold for "low-confidence edge." Minor, but it means the suppression boundary is non-deterministic across environments — violates Stage-1 determinism.

**Generalized fix:** make the local fallback floor identical to `_edge_filter_for_db`'s default (`0.6`), or better, have `_edge_filter` call `_edge_filter_for_db` and on exception still return its `_legacy_confidence_filter_clause` rather than an ad-hoc literal.

---

### 9. `graph_navigation` — stdlib-shadow caller guard reach (lines 785–845) · INTENT: drop false "Called by:" callers that are stdlib-attribute calls name-matched to a project symbol · BUG: the guard only fires when (a) `_is_stdlib_shadow` is importable, (b) the call line literally contains `<stdlib_module>.<target>(`, and `target` is the GRAPH TARGET NAME (`nt.name`) — but the rendered `Called by:` line (line 985/966) shows `_top_functions_for_file` names, NOT the target name the guard checked. The guard validates against one symbol and the render shows a different one. · LIPI: **Integration / Plumbing (guard keyed to a different field than the render)** · MODERATE-HIGH confidence

The guard (lines 816–841) fetches `e.source_line, nt.name` (the TARGET function in the viewed file) and tests `_is_stdlib_shadow(caller_line, nt.name)`. It drops the caller file iff EVERY edge's target is a stdlib shadow. But `_format_neighbor` (line 942) renders the caller via `_top_functions_for_file(cur, fp, ...)` — the top-referenced functions in the CALLER file — and the code snippet via `_caller_source_lines.get(fp)`. So:
- The guard's shadow decision is made on `nt.name` (target side).
- `_STDLIB_MODULES` is a 29-entry allowlist (os, sys, re, io, json, …). A shadow via a stdlib module NOT in that set (`hashlib`, `base64`, `struct`, `socket`, `csv`, `sqlite3`, `urllib`, `xml`, …) passes the guard. So `hashlib.update()` name-matched to a project `update()` is NOT caught. The guard is **allowlist-limited**, not general.
- More subtly: the guard picks `_picked` = first NON-shadow `source_line` (line 833), but `_caller_source_lines[fp]` is only set when `_any_clean`. If the representative line chosen by `_top_functions_for_file`/render differs, the snippet shown may still be a shadow line that simply wasn't the one inspected (only 5 edges fetched, line 822 `LIMIT 5`). 

**Generalized fix:** (a) replace the hardcoded `_STDLIB_MODULES` allowlist with a structural test (is `head` resolvable as an import in the caller file's import table? if it resolves to an external/stdlib module, it's a shadow) — drives off graph.db's IMPORTS edges, language-agnostic; (b) render the SAME line the guard cleared (use `_caller_source_lines[fp]` as the only snippet source and skip the file if that exact line is a shadow).

---

### 10. `graph_navigation` — importers block double path-resolve (lines 1000–1016) · INTENT: show "Imported by:" files · BUG: `needle` was ALREADY resolved at line 699; line 1000 re-resolves it (`_resolve_file_path(conn, needle)`) — resolving an already-canonical stored path. If the resolver is not idempotent (e.g. it expects a container/host path and a stored path returns None), this yields `None`, silently zeroing the importers block · LIPI: **Plumbing (path normalized twice, second may null)** · MODERATE confidence

Line 699: `needle = _resolve_file_path(conn, needle)` → canonical stored path. Line 1000: `_resolved_imp = _resolve_file_path(conn, needle)` re-runs the resolver on the already-stored path. `resolve_to_stored_path` is documented to return `None` when "the path is unknown or ambiguous." Feeding it a stored path that doesn't match its container/host heuristics can return `None`, and line 1016 binds `(_resolved_imp, _resolved_imp, limit)` → `WHERE file_path = NULL` → 0 rows → importers silently empty. The earlier comment (line 994–996) explicitly worries about FALSE "un-imported" impressions; this double-resolve is a path TO that false impression.

**Generalized fix:** drop the re-resolve at line 1000 — `needle` is already canonical. Bind `needle` directly. (Same applies to `_ef_imp = _edge_filter(db_path)` at line 1001, which recomputes the identical clause already in `_ef`.)

---

### 11. `graph_navigation` — exception-evidence reads issue terms from disk, ignoring `state` (lines 1037–1045) · INTENT: only emit RAISES/CATCHES when issue mentions errors · BUG: reads `/tmp/gt_issue_terms.txt` directly instead of `_load_issue_terms(state)`, so in the in-process AgentState path (FINAL_ARCH_V2 Layer 2) the file may be stale/absent and the error-keyword gate silently never fires · LIPI: **Integration (two sources of truth for issue terms)** · MODERATE confidence

Everywhere else in this function issue terms come from `_load_issue_terms(state)` (lines 876, 1094, 1186). Here (lines 1038–1042) it bypasses that and reads the legacy `/tmp` mirror. When the hook runs in-process with a populated `state` but no `/tmp` mirror (the documented FINAL_ARCH_V2 path), `_issue_terms_exc` is empty, `_issue_has_error_kw` is False, and exception evidence is suppressed even on an error-themed issue. **Inconsistent plumbing — the exception gate uses a colder data source than its siblings.**

**Generalized fix:** `_issue_terms_exc = {t.lower() for t in _load_issue_terms(state)}`. One loader, `state`-aware, with the legacy file as its internal fallback (which `_load_issue_terms` already does).

---

### 12. `graph_navigation` — ego center-file disambiguation already done, C2 guard partially redundant but `_best_func` selection is hub-biased in the FALLBACK it claims to avoid (lines 1097–1112) · INTENT: pick an issue-relevant center function (not most-called) · BUG: `ego_graph` is called WITH `needle` as `file_path` (line 1112), so the center lookup is already file-scoped (`file_path LIKE '%needle'`, ego.py:214–219) — but the C2 guard comment (lines 1113–1121) claims ego uses "LIMIT-1 / most-called tiebreak [that] can land on a homonym in a DIFFERENT file." That branch (ego.py:222–227, the most-called ORDER BY) only runs when `file_path` is EMPTY. Since `needle` is non-empty, the homonym-in-different-file scenario the guard defends against **cannot occur via this call** — the guard is defending a path that isn't taken. · LIPI: **Logic (guard premise false for this call site)** · MODERATE confidence

Not harmful (the guard is a correct no-op here), but it's dead defense — and worse, it masks the REAL residual risk: `file_path LIKE '%needle'` (ego.py:215, suffix match) can match `vendor/importer.py` when `needle="importer.py"` (the same bare-basename over-match as finding #4). So the actual homonym risk is the `LIKE '%suffix'` in ego_graph, which `_same_stored_file` then re-checks — and `_same_stored_file` has the same n=1 permissiveness (finding #4). **The two layers share the same blind spot rather than covering each other.**

**Generalized fix:** pass a fully-qualified stored path (not a bare needle) to `ego_graph`, and make ego's center query use exact `file_path = ?` when a canonical path is available, falling back to `LIKE` only for genuinely partial paths. Then `_same_stored_file` becomes a true belt-and-suspenders rather than a duplicate of the same weak check.

---

### 13. `graph_navigation` — `_l3b_primary` band rendering drops callees entirely AND the primary-edge token-cap check is on the WRONG variable (lines 973–981) · INTENT: after early band, render only the single primary edge under a char cap · BUG: `_char_cap` (line 971) is derived from `_iteration_band`, which is only meaningfully computed when `rebuild_l3b` is set (line 727); when `_l3b_primary=1` but `rebuild_l3b=0`, `_iteration_band` stays the default `"early_0_25"` → `_char_cap=1000` always, so the per-band shrink (640/320/0) never applies despite `iteration_ratio` being high · LIPI: **Integration (two feature flags whose state must agree but don't)** · MODERATE confidence

`GT_L3B_PRIMARY_EDGE` and `GT_REBUILD_L3B` are independent env flags. `_iteration_band` is assigned from `get_iteration_band(...)` ONLY inside `if rebuild_l3b:` (line 723–727). With primary-edge ON and rebuild OFF, `_iteration_band` never updates from `"early_0_25"`, so `_char_caps.get(_iteration_band)` always returns 1000 and the late/final suppression (320/0) is dead. The primary-edge path then renders the full primary line at every iteration. The two flags encode overlapping iteration logic but only one computes the band.

**Generalized fix:** compute `_iteration_band` unconditionally from `iteration_ratio` (it's a pure function of the ratio) before either flag block; both `rebuild_l3b` decay and `_l3b_primary` capping then read a consistent band.

---

### 14. `_file_function_spec` (lines 1232–1286) · INTENT: show parallel patterns in the viewed file's top functions (pre-edit completeness) · BUG: takes top-5 functions by `start_line` (line 1244) with NO issue-relevance ranking and NO suppression gate, then emits `specs[0]` — the FIRST function in the file by line number — unconditionally · LIPI: **Logic (hub/position bias — emits file-top function regardless of issue)** · HIGH confidence

This is the SAME class of bug the contract pillar was explicitly fixed for (the "39th of 102 functions" / `set_fields` bug, lines 168–174), but `_file_function_spec` never got the fix. It:
- selects `ORDER BY start_line LIMIT 5` (line 1244) — first 5 functions by position;
- builds a template-cluster "Spec:" line for each;
- returns `"Spec: " + specs[0]` (line 1286) — the FIRST function that has a 2–8 line template group, i.e. effectively the top-of-file function.

It applies **zero issue-anchor relevance and zero correct-or-quiet suppression.** It is appended to `nav_lines` in `main()` (line 1367) and printed to the agent (line 1370). So on exactly the large-file case the contract pillar was hardened against, `_file_function_spec` re-introduces "generic top-of-file function as salient evidence" — `progress_write`/`_setup_logging`-class noise — under a `Spec:` label, AFTER the contract pillar correctly suppressed it. **Self-contradiction at the file level: one pillar suppresses the file-top noise, a sibling re-emits it.**

**Generalized fix:** apply the same anchor-front-load + `_relevance`-rank + correct-or-quiet suppression as `_contract_pillar` (reuse the anchor set and the `_relevance(top)==0 → []` gate). Do not emit a position-ranked spec when a relevance signal exists and the top-by-position function matches none of it.

---

### 15. `_test_file_targets` (lines 1289–1314) · INTENT: for a viewed test file, show the source functions it calls · BUG: `Calls into:` targets are NOT filtered for stdlib-shadow / name_match laundering the way the caller block is (lines 791–845) — a test file's `self.assertEqual`-adjacent name_match edges to same-named project functions render as confident `Calls into: <file>::<name>()` · LIPI: **Integration (caller block has the shadow gate; this twin lacks it)** · MODERATE confidence

The query (lines 1294–1300) gates on `_edge_filter(db_path)` (categorical/numeric) but does NOT apply `_is_stdlib_shadow` per-line as the caller block does (lines 829–839). Per CLAUDE.md, a categorical-passing edge can still be a stdlib/builtin shadow (`join`, `get`, `items` are the named examples). So a test file calling `result.items()` or `os.path.join(...)` can surface `Calls into: <project_file>::items()` / `::join()` as a localization target — false. This is the "INCONSISTENT FILTER — caller query lacks the deterministic gate its callee twin has" bug class, in the test-file path which is otherwise low-traffic.

**Generalized fix:** route both the caller and the test-target `Calls into:` rendering through one shared `_render_edge_target(...)` that applies the shadow guard once. Single source of truth, no duplicated/diverging filter.

---

### 16. `main()` — `os.environ["GT_L3B_TOTAL_CANDIDATES"]` set but never reset; `graph_navigation` ignores the `state`/`structured_output` for the spec line · INTENT: pass total-candidates to the nav layer · BUG: line 1354 writes a process-global env var that persists across invocations in a long-lived process (in-process FINAL_ARCH_V2 path), so a later view with `--total-candidates 0` (default, no override) keeps the STALE prior count and renders a wrong `[Progress: visited X/STALE]` line (line 1074) · LIPI: **Plumbing (mutable global persists across calls)** · MODERATE confidence

`main()` only sets the env var `if args.total_candidates > 0` (line 1353) and never clears it. In a subprocess-per-call model this is harmless. In the documented in-process Layer-2 model (where state is passed in-process and these hooks may run in one interpreter), the env var leaks: invocation A with `--total-candidates 80` sets it; invocation B without the flag reads the stale `80` at line 741 and renders progress against the wrong denominator.

**Generalized fix:** thread `total_candidates` as a function parameter to `graph_navigation` (it already takes `state`); stop round-tripping it through `os.environ`. If env must stay for subprocess compat, set/reset it in a `try/finally` around the single call.

---

## Cross-cutting summary (highest-harm first)

| # | Function | Class | Avenue | Harm |
|---|---|---|---|---|
| 14 | `_file_function_spec` | hub/position bias, re-emits file-top noise the contract pillar suppressed | Logic | **HIGH** — directly the 3749 wrong-function class, unfixed |
| 7 | hub-scale degree query | 3 different edge populations feed one ranking | Integration | **HIGH** — miscalibrated neighbor ranking |
| 1 | `_l3b_line_priority` | ego block misclassified → dropped first by the cap | Integration/Logic | **HIGH** — trims the most-confident pillar first |
| 3c | `_contract_pillar` flows | flow by name not node-id → homonym mis-pairing | Plumbing | **MOD-HIGH** |
| 9 | stdlib-shadow caller guard | allowlist-limited + guard-keyed-to-different-field-than-render | Integration | **MOD-HIGH** |
| 4 | `_same_stored_file` | n=1 bare-basename over-match (vendor/ vs src/) | Logic | MOD |
| 15 | `_test_file_targets` | missing the shadow gate its caller twin has | Integration | MOD |
| 13 | `_l3b_primary` band | band never updates when rebuild flag off → caps dead | Integration | MOD |
| 11 | exception-evidence terms | reads /tmp not `state` → gate dead in-process | Integration | MOD |
| 10 | importers double-resolve | re-resolving canonical path can null → false "un-imported" | Plumbing | MOD |
| 16 | `main()` env var | stale GT_L3B_TOTAL_CANDIDATES across in-process calls | Plumbing | MOD |
| 8 | `_edge_filter` fallback | 0.6 vs 0.7 floor depending on import success | Integration | MOD (determinism) |
| 2 | `_enforce_l3b_cap` | cap_enforced+exceeded both true; O(n²) | Implementation | LOW (telemetry) |
| 6 | `_score_by_issue_relevance` | short anchor substring matches path noise | Logic | MOD |

**The single most important finding for the focus note (the 3749 wrong-function bug):** the contract pillar (`_contract_pillar`) WAS hardened against position-bias, but **`_file_function_spec` (finding #14) is the un-hardened twin** — it still emits the file-top-by-`start_line` function as a salient `Spec:` line with no anchor relevance and no correct-or-quiet gate, re-introducing exactly the noise the contract pillar suppresses, in the same observation. That is the live re-occurrence of the wrong-function class on this file's render path.

File: `D:\Groundtruth\src\groundtruth\hooks\post_view.py` (all line refs above).

---

I have enough to confirm the plumbing chain. Now let me verify the `--lang` mismatch concern in `_KNOWN_SERVERS` vs `LSP_SERVERS` and one more thing: the `resolve_main` guard `servers.get(args.lang)` against `_detect_servers()` keys vs the langs that `get_server_config` actually supports. I've now read every relevant function. Let me also confirm one detail in the Go return-type parser logic for the `: ` TypeScript branch ordering by re-reading lines 654-681 — already have it.

I have everything needed. Let me compile the granular LIPI findings.

Final review complete. Here are the findings, function-by-function, in file order.

---

**GRANULAR LIPI REVIEW — `src/groundtruth/resolve.py`**

**1. `_resolve_edges` (dispatch map) · INTENT: map `--lang` name → file ext → LSP server config so the right server handles each language · BUG: the dispatch map is internally inconsistent — `_KNOWN_SERVERS`/`_LANG_TO_EXT` advertise `c/cpp/ruby/kotlin`, but `LSP_SERVERS` (config.py) has NO entries for `.c/.cpp/.rb/.kt`. · LIPI: Integration (two symmetric tables that must agree; one lists languages the other can't serve). · `resolve.py:50-66` `_KNOWN_SERVERS` lists `"ruby": "solargraph"`, `"kotlin": "kotlin-language-server"`, `"c"/"cpp": "clangd"`; `config.py:21-30` `LSP_SERVERS` stops at `.java`. Net effect: `resolve_main`'s `servers.get(args.lang)` gate (line 872) passes for ruby/c/cpp/kotlin if the binary is on PATH, the run proceeds, then `get_server_config(ext)` returns `Err` at line 320 → `stats["skipped"]=len(edges)` and the whole pass silently no-ops. The diagnostic `_print_summary` also tells users to "install clangd/solargraph" (line 280-281) for a precision pass that can never run. · FIX: derive the three tables from ONE source of truth — make `_KNOWN_SERVERS`/`_LANG_TO_EXT` keys a subset of `LSP_SERVERS` keys (or add the missing `.c/.cpp/.rb/.kt` configs to `LSP_SERVERS`). Structural, no per-task logic.**

**2. `_resolve_edges` — column-finder for the call site · INTENT: locate the character column of the call on the source line so `definition()` is queried at the right position · BUG: `col = line_text.find(target_name)` returns the FIRST textual occurrence of the bare name, not the occurrence at the actual call. On `x = foo(); y = foo.bar()` resolving `bar`, or any line where `target_name` appears earlier as a substring/different symbol (`result = process(); process.run()`), the LSP `definition` is asked at the WRONG token → wrong definition → false verify/correct/delete of a legitimate edge. · LIPI: Logic (wrong position; first-match instead of call-site match). · `resolve.py:470` `col = line_text.find(target_name)` and `:471-472` `if col == -1: col = 0` — the `col=0` fallback is worse: it queries column 0 (usually indentation/a keyword) and whatever symbol sits there. The edge already carries `source_line` but no column, so the call site is genuinely ambiguous on multi-call lines. · FIX: the indexer should persist the call-site column on the edge (it has the tree-sitter node) and `definition()` should use it; absent that, at minimum search for `target_name` as a word-boundary token preceded by `.`/whitespace and, on `col==-1`, SKIP (correct-or-quiet) rather than query column 0. Generalized.**

**3. `_resolve_edges` — definition→node match (the verify/correct/DELETE arm) · INTENT: find the graph node the LSP definition points to; if none, the edge is a false positive and is DELETED · BUG: the node-match query requires `start_line <= target_line <= end_line` AND exact `file_path`, but when the LSP definition lands in a stdlib/third-party/generated file that the indexer never ingested (the overwhelmingly common case for `join/get/append/loads` per CLAUDE.md), `row is None` → the edge is `DELETE`d (line 533). That is correct ONLY if "not in graph" means "false positive." For an external-library target it means "correctly resolved to outside the repo" — deleting it is fine, but for an in-repo target whose node row has `end_line IS NULL` or a slightly-off `start_line` (tree-sitter vs LSP line drift on decorators/comments), the `start_line <= ? AND (end_line >= ? OR end_line IS NULL)` window misses and a REAL edge is destructively deleted. A read-pass that DELETEs on a fuzzy line-window miss is the highest-harm path in the file. · LIPI: Logic + Plumbing (destructive action gated on a line-window join that can miss legitimately). · `resolve.py:506-512` the SELECT, `:531-534` the `else: DELETE`. · FIX: split "definition outside the indexed repo" (target_path not under `abs_root` → leave edge alone or demote, never delete) from "definition inside repo but no node" (genuine FP). Only delete when the target file IS indexed but no node matches; never delete on an external-file definition or a NULL-end_line window miss. Structural.**

**4. `_resolve_edges` — `name` collision in node-match · INTENT: match the LSP target by `file_path = ? AND name = ?` · BUG: it pairs `target_rel` (from LSP) with `target_name` (the edge's recorded callee name), but if the LSP CORRECTED the call to a differently-named symbol (e.g. an alias, a re-export, `super().__init__` resolving to the parent class name), `name = target_name` will not match the real target node → falls to the `else` → DELETE of an edge the LSP actually resolved correctly. The correction path (`corrected`) can therefore never fire for any case where the resolved symbol's name differs from the syntactic callee name — exactly the ambiguous-method case this pass exists to fix. · LIPI: Implementation (wrong key: filters by the OLD name when the whole point is the name may be wrong). · `resolve.py:511` `(target_rel, target_name, target_line, target_line)`. · FIX: match the node primarily by `(file_path, line-window)` and treat `name` as a tiebreaker, not a hard filter — the LSP's location is the authority, not the pre-resolution name. Generalized.**

**5. `_resolve_edges` — Go return-type parser (`func` branch) · INTENT: extract the return type from a gopls hover for type enrichment · BUG: the "last balanced paren = param list" heuristic misclassifies multi-return Go signatures. For `func (r *T) M(x int) (T, error)` the LAST balanced paren group is `(T, error)` (the return), so `_param_end` points at the RETURN group's close, and `_after = _hover_clean[_param_end+1:]` is empty/`{...}` → return type is dropped. For `func F(x int) T` it works, but for the idiomatic multi-return `(T, error)` — the majority of Go funcs — it yields nothing. · LIPI: Logic (the "last paren wins" rule is correct for single unparenthesized returns but inverts for parenthesized multi-returns). · `resolve.py:663-675`, esp. `:671` `_param_end = _ci  # keep updating — last one wins`. · FIX: walk paren groups left-to-right tracking each top-level group; the param list is the FIRST top-level group after the (optional) receiver group, and the return is everything after it (which may itself be a parenthesized tuple). Don't equate "last paren" with "param list." Generalized across Go signatures.**

**6. `_resolve_edges` — TypeScript return-type parser · INTENT: extract `: ReturnType` from a tsserver hover · BUG: `_after_colon = _hover_clean.split(")")[-1]` takes text after the LAST `)`, but TS return types are themselves parenthesized/generic-laden — `function f(x): Promise<(A|B)>` or `function f(cb: () => void): void`. Splitting on the last `)` lands inside the type or after a callback-param paren, so `_after_colon` won't start with `:` and the return type is dropped (or, worse, a fragment is captured). · LIPI: Logic (naive last-`)` split breaks on nested parens in params/return). · `resolve.py:677-680`. · FIX: balanced-paren scan to find the param-list close, then read the `:Type` immediately after it (same fix family as #5 — both branches assume parens don't nest). Generalized.**

**7. `_resolve_edges` — type-enrichment node selection vs LSP_METRICS denominator · INTENT: enrich the top-50 most-referenced nodes' signatures/return types in the same warm LSP session · BUG (INERT relative to the metric): the enrichment loop (lines 559-728) picks top-50 by global `ref_count` across the WHOLE graph and is NOT scoped to `source_files`, while the edge-resolution pass and the `LSP_METRICS residual` denominator ARE scoped to the issue subgraph. So in the demand-driven path (`--source-files=<edited file>`, the only path the wrapper uses) the enrichment spends its budget on globally-popular nodes that are almost never in the 1-file issue scope, and its work (`_enriched` count) is reported to stderr only (line 723-728) — it never appears on the machine-parseable `LSP_METRICS` line and is never asserted by any gate. Computed, logged, never delivered to the contract a consumer reads. · LIPI: Integration/Plumbing (enrichment sourced from a DIFFERENT population than the sibling edge-pass; its output is telemetry-only). · `resolve.py:559-571` (unscoped top-50), vs `:861-863` scoped residual. · FIX: scope the enrichment node query to `source_files` when provided (same demand-driven principle), and surface `nodes_enriched` on the `LSP_METRICS` stdout line so it is gate-visible, not stderr-only. Generalized.**

**8. `_resolve_edges` — `did_open` languageId for the enrichment file · INTENT: open each enrichment file in the LSP with the correct languageId · BUG: minor inconsistency — the EDGE pass opens every source file with `_lang_id_for_ext(ext)` using the RUN's `ext` (line 456), not the file's own extension. With the `n.language = ?`/`_node_ext != ext` guards this is mostly fine, but the edge-pass `did_open` (line 456) hard-codes the run `ext`'s language id even though the source file could in principle differ; the enrichment pass correctly uses the file's own `_node_ext` (line 602-603). The two sibling did_open sites disagree on which extension drives the languageId. · LIPI: Integration (two symmetric did_open paths, one uses run-ext, one uses file-ext). · `resolve.py:456` vs `:602-603`. · FIX: both should derive languageId from the FILE's extension (`os.path.splitext(source_file)[1]`), consistently. Low harm, but it is a real asymmetry. Generalized.**

**9. `resolve_main` — `--lang` filter vs residual denominator (metric self-consistency) · INTENT: `LSP_METRICS resolved=X residual=Y` should let a consumer compute the true resolution fraction · BUG: `resolved_promoted` counts only `verified+corrected` and EXCLUDES `deleted` (lines 925, 918-921 comment), but `residual` (`_count_residual_method_edges`) counts name_match edges that the delete path REMOVES. A pass that correctly deletes N stdlib-shadow false-positive method edges shows `resolved=0` against a non-zero `residual`, i.e. the contract reports "0% resolved" for a pass that did exactly the right thing (removed garbage). GATE 2 (`foundational_gates.py:273+`) keys on this fraction, so a clean delete-heavy pass reads as a FAIL. · LIPI: Logic (denominator population includes edges the numerator's sibling outcome removes, but those removals aren't credited). · `resolve.py:925` `resolved_promoted = verified + corrected`; CLAUDE-style comment `:918-921` explicitly drops deletes. · FIX: either credit deletes as "resolved" (a deleted false-positive IS a resolution of the residual), or emit `deleted=` as a third field on the `LSP_METRICS` line so the gate can score `(resolved+deleted)/residual`. Structural; no benchmark logic.**

**10. `resolve_main` — `lang_edges` re-cap after `_get_ambiguous_edges` already capped · INTENT: take only this language's edges, bounded by `--max-edges` · BUG (latent, low harm now): `_get_ambiguous_edges` already applied `ORDER BY confidence ASC LIMIT max_edges` across ALL languages (lines 212-213), THEN `resolve_main` filters to one language and re-slices `[:max_edges]` (line 880). When the graph is multi-language, the global confidence-ordered LIMIT can be saturated by another language's lower-confidence edges before this language's edges appear, so `lang_edges` is silently short — fewer edges resolved than `--max-edges` would imply, with no signal. Today the wrapper passes `--lang` AND a 1-file `--source-files`, masking it, but the function is not generalized for multi-lang whole-graph use. · LIPI: Integration (the LIMIT is applied pre-language-filter in one function, the language filter post-LIMIT in another — the cap and the filter are in the wrong order). · `resolve.py:203-213` (lang filter is OPTIONAL inside the SQL) vs `:880` (post-hoc python filter + re-cap). · FIX: when `--lang` is set in `--resolve` mode, pass it INTO `_get_ambiguous_edges` so the SQL `LIMIT` applies to the already-language-filtered set, not the global set. Generalized.**

**Clean (skipped):** `_path_to_uri`, `_uri_to_path`, `_lang_id_for_ext`, `_detect_servers`, `_count_residual_method_edges` (correct, deliberately uncapped/structural), `_print_summary`, `_rebuild_closure`, and the pyrightconfig-drop block — all checked across the four LIPI avenues, no defects found.

**Highest-confidence / highest-harm, in priority order:** #3 (destructive DELETE on a line-window/external-file miss), #4 (correction arm filters by the stale name, so the pass's core purpose — re-pointing mis-named method calls — can't fire and instead deletes), #1 (dispatch-map inconsistency → silent full no-op for 4 advertised languages), #9 (metric undercounts deletes → GATE 2 misreads a correct pass as failing). #2/#5/#6 are correctness bugs in position/return-type extraction that quietly degrade precision rather than crash.

Relevant files: `D:\Groundtruth\src\groundtruth\resolve.py`, `D:\Groundtruth\src\groundtruth\lsp\config.py`, `D:\Groundtruth\src\groundtruth\lsp\client.py`, `D:\Groundtruth\scripts\swebench\oh_gt_full_wrapper.py` (caller, lines 3966-3984), `D:\Groundtruth\scripts\metrics\foundational_gates.py` (GATE 2 consumer of `LSP_METRICS`, lines 257-312).

---

Confirmed: `_record_diff_snapshot` writes diff state to `config` and a file, but it does NOT call `_l5_gov.state.record_diff_snapshot(diff_size)`. So the governor's `goku_check` patch-collapse detector (governor.py 612, `self.state.patch_collapsed`) is fed ONLY when `goku_check` is called with `diff_size` — which the wrapper never passes (line 4513-4515 and 6336-6338 both omit `diff_size`). So goku's PATCH_COLLAPSED_OR_LOST check (#1) can never see a collapse. The wrapper computes diff-collapse into `config._diff_*` independently and never bridges it to the governor. This is a plumbing disconnect: collapse is detected on the wrapper side, the governor that's supposed to act on it is starved.

I now have a complete, exhaustive LIPI review. Let me compile the final numbered findings. I have all the concrete file:line evidence I need.

Based on my granular function-by-function LIPI of `scripts/swebench/oh_gt_full_wrapper.py`, here are the findings.

---

# GRANULAR LIPI — `scripts/swebench/oh_gt_full_wrapper.py` (8028 lines)

**Delivery model (the load-bearing premise for the whole INERT class):** GT never writes to OH's EventStream. Every layer "delivers" by mutating `obs.content` in place via `append_observation`/`prepend_observation` (3083–3127) and returning `obs` from `patched_run_action`. The wrapper itself documents this assumption at 3200–3205 ("OH's `runtime.run_action` does not add to the EventStream by itself"). **Consequence: any layer that mutates `obs` AFTER the agent has already committed to / consumed that observation is INERT by construction.** That is the structural root of every L4/L5/L5b "fires-in-telemetry-never-delivered" symptom below.

---

### 1. `patched_run_action` finish-handler L5/L5b · INTENT: governor + goku intervene before submit · BUG: writes to `obs` it *labels* a dead write
- **LIPI: Integration / Plumbing (INERT class d — the dominant one).**
- 6288–6362. The finish-handler fires `after_interaction` (6291) and `goku_check` (6335), emits telemetry as **`emitted=False, suppressed=True, suppression_reason="finish_handler_dead_write"`** (6299–6300, 6310–6311, 6343–6344, 6352–6353), then **STILL calls `obs = append_observation(obs, f"\n\n{_l5d.message}\n")`** at 6313 and 6355. OH has already set `state=FINISHED` (comment 6296/6350); the agent never steps again. The code self-contradicts: it knows it's dead, marks it dead in telemetry, and appends anyway. `_log_gt_interaction` (6314) then records `gt_sent=<message>` so the interaction log shows a "delivery" that no agent observation ever contained. **This is exactly "fires in telemetry but never reaches an agent observation."**
- **Fix (generalized):** delete the two `append_observation` calls in the finish handler; a payload the consumer cannot read must not be appended. If pre-submit governance is wanted, it must fire at the **edit→review transition** while the agent can still act (the pattern `_maybe_fire_presubmit_verify` already uses), not post-FINISHED. No file/task logic involved.

### 2. `render_l5_advisory` · INTENT: pre-submit review summary · BUG: computed every finish, never delivered
- **LIPI: Plumbing (INERT class d).**
- Built at 6394, but the next line states the contract: **`# Fix 6: Keep advisory for state/telemetry but remove agent-visible injection`** (6396). It is stored to `instance_ref["gt_advisory"]` (6401) and NEVER appended to `obs`. So the entire `render_l5_advisory` body (2007–2073) — pending checks, unresolved summaries, scaffold/edit-loop redirect — is pure compute-and-log. The `[GT_GATE]`/`[GT_ADVISORY]` markers it emits exist only so `_compute_has_real_evidence` (2084–2085) can flag a log line that no agent saw.
- **Fix:** either delete the function and its call, or relocate its computation to a mid-trajectory hook that delivers. Do not keep a "telemetry-only" advisory that pollutes `has_real_evidence` accounting.

### 3. `_maybe_fire_l5` + `_render_scaffold_advisory` · INTENT: redirect on non-source/scaffold edit · BUG: `_maybe_fire_l5` has ZERO call sites — dead code
- **LIPI: Implementation (dead path) + Integration.**
- `_maybe_fire_l5` is defined at 1131 and **never called** anywhere in the file (grep: only the `def` line). It is the only caller of `_render_scaffold_advisory` (1146) and the only writer of `_l5_scaffold_fired`/`_l5_last_scaffold_file`/`l5_fire_count` (1142–1145). The whole `<gt-advisory layer="L5" trigger="non_source_without_progress">` payload (1121–1127) is unreachable. Scaffold redirection is instead done by the governor's `scaffolding_trap_early` (governor.py 165–206), so this is an orphaned duplicate.
- **Fix:** delete `_maybe_fire_l5` + `_render_scaffold_advisory` + the dead `_l5_scaffold_fired`/`_l5_last_scaffold_file` fields, OR wire `_maybe_fire_l5` into the `post_edit` non-source branch. Leaving it defined misleads any audit into thinking an L5 scaffold path exists.

### 4. L5 governor `after_interaction` call gate · INTENT: governor sees every action · BUG: invoked ONLY on `event.kind == "skip"`, starving its source-edit/finish dispatch
- **LIPI: Integration (gate asymmetry — caller feeds callee a filtered subset).**
- 4461–4462: `if _l5_gov is not None and not _GT_BASELINE and event.kind == "skip":`. But `after_interaction`'s internal dispatch (governor.py 208–230) routes `_is_finish_action` → `_handle_finish`, `FileEditAction`/`FileWriteAction` → `_handle_source_edit` (→ `premature_commitment`), and non-source edits → `_handle_non_source_edit` (→ `no_durable_source_progress`). A real source edit is classified `post_edit`, NOT `skip`, so **`_handle_source_edit`/`premature_commitment`/`no_durable_source_progress` can NEVER fire from this call site.** On the post_edit path the wrapper only pokes `_l5_gov.state.record_source_edit(...)` directly (5210–5213), bypassing the decision logic. Half the governor's intervention surface is structurally unreachable.
- **Fix:** call `after_interaction` for ALL classified events (or at minimum for `post_edit` and `finish` on the live, non-FINISHED path), not just `skip`. The governor was designed to be the single action sink; the wrapper must feed it every action.

### 5. `goku_check` patch-collapse detector · INTENT: detect diff→0 regression · BUG: wrapper never passes `diff_size`; collapse computed on wrapper side is never bridged to the governor
- **LIPI: Plumbing (data computed in one component, needed in another, never connected).**
- `goku_check` signature takes `diff_size` (governor.py 559) and only records a diff snapshot / can flag `patch_collapsed` (612) when given it. Wrapper call sites pass action/obs/counts/`file_path` but **omit `diff_size`** (4512–4515 live; 6335–6338 finish). Meanwhile the wrapper computes collapse fully on its OWN side in `_record_diff_snapshot` (533–543, sets `config._diff_collapsed_count`, prints "DIFF COLLAPSED TO ZERO") but never calls `_l5_gov.state.record_diff_snapshot(...)`. So goku's #1 check is permanently blind, and the wrapper's own collapse signal drives nothing.
- **Fix:** pass the computed diff size into `goku_check` (or call `_l5_gov.state.record_diff_snapshot(files_changed)` inside `_record_diff_snapshot`). One-line bridge; fully generalized.

### 6. `render_l4_tool_footer` · INTENT: tell the agent the L4 tools exist · BUG: returns `""` unconditionally → `gt_validate` (and all L4 CLI tools) are a dead no-op from the agent's view
- **LIPI: Implementation (stubbed-out) + Integration.**
- 1497–1498: `def render_l4_tool_footer(...): return ""`. It is the ONLY thing concatenated at 7770 (`msg.content + render_l4_tool_footer(...)`). The tools `gt_query/gt_search/gt_navigate/gt_validate` are physically installed (`install_l4_tools` 3496–3560, PATH-wired 3536–3538), but **the agent is never instructed they exist** and there is no passive hook that invokes them. Combined with the documented "0% autonomous adoption" (7275–7281), the entire agent-invoked L4 tool surface — `gt_validate` especially — is inert: installed, never called, never rendered.
- **`gt_validate` is dead end-to-end:** even its registration chain is a closed loop with no agent-visible output. `register_gt_validate_paths` (1990) records args into `verified_checks`; `_path_covered_by_validation` (1983) → `_l5_unresolved_paths` (2002) feeds ONLY `render_l5_advisory`, which (finding #2) is never delivered. So `gt_validate` produces no observation and its bookkeeping feeds a dead advisory.
- **Fix:** decide one of two — (a) if tools are intentionally suppressed for benchmark runs, delete `render_l4_tool_footer`, `install_l4_tools`, and the `gt_validate` registration/`verified_checks`/`_l5_unresolved_paths` chain (they are pure cost); or (b) restore a minimal tool hint so adoption can be measured. Do not ship a function that exists only to return `""`.

### 7. `<gt-orientation>` vs `<gt-localization>` — two independent rankings in ONE brief that can name different primary targets (the confirmed cfn-lint-3749 self-contradiction)
- **LIPI: Integration (cross-wired pipes — class a) + Self-contradiction (class e).**
- The brief that reaches the agent contains BOTH: `<gt-localization>`/`<gt-graph-map>` produced upstream by `generate_v1r_brief` (a FTS5/semantic/structural fusion pipeline), AND `<gt-orientation>` produced HERE by a completely separate composite scorer (`composite_score`, 7541; sorted by `composite` at 7566; top candidate is the "edit target" 7584–7591). The orientation block is appended to `brief` at 7654 with **no reconciliation** against the localization ranking. The two scorers use different inputs (v1r = retrieval fusion; orientation = issue-keyword × callers × SLOC × fan-out, 7400–7460) and can rank different files #1. The brief then literally contradicts itself on the primary target.
- **Fix:** single ranking authority. Either derive `<gt-orientation>` candidates from the v1r ranked set (so #1 is shared), or have orientation annotate the localization entries rather than re-rank. One ordered candidate list feeds every brief sub-block.

### 8. `_build_rescue_payload` · INTENT: rescue with the confirmed file + ITS evidence · BUG: file and evidence sourced from two different orderings → mismatched "Key evidence"
- **LIPI: Plumbing (row A paired with row B — wrong-fact class b).**
- `top_base`/`top_cand` is chosen from consensus / non-consensus evidence (1838–1859), but `top_evidence` is pulled as **`next(iter(config.evidence_cache))`** (1867) — the FIRST arbitrary key in the cache dict, which is generally a DIFFERENT file than `top_cand`. The rendered message then asserts `"{top_base} was confirmed earlier. Key evidence: {top_evidence}"` (1882–1884) pairing one file's name with another file's cached evidence. This is reachable on the live path (`HARMFUL_SILENT` rescue at 4564–4571, delivered via `append_observation`).
- **Fix:** look up `config.evidence_cache.get(top_cand)` (keyed to the chosen file); if absent, omit the evidence line rather than grabbing an unrelated cache entry. Correct-or-quiet.

### 9. Consensus `<gt-scope>` "you are viewing this — primary target" · INTENT: confirm localization · BUG: fires in `post_view`, AFTER the open already executed → confirmatory, never causal
- **LIPI: Logic (the mechanism cannot do what its wording implies).**
- The consensus block lives in the `post_view` handler (4722–4795) which runs only after `obs = orig_run_action(action)` (4369) has already executed the file open. It then **prepends** `1. <file> — primary target` / `— in scope (you are viewing this)` (4760/4762) onto the observation of a file the agent ALREADY chose to read. By construction it cannot have caused that read. The "primary target" wording (4760) implies GT directed the agent there; the timing proves it only ratified a decision already made. At best it confirms; it never converts (the Stage-2 "changes the decision the agent was about to make" test fails by position).
- **Fix:** this signal belongs UPFRONT in the L1 brief (where localization can actually steer the first read), not as a post-hoc view annotation. If kept in `post_view`, drop "primary target" framing — state only the verifiable structural scope (the neighbor list), never a causal claim about a read that already happened. (This mirrors the file's own `_render_scaffold_advisory` rationale at 1113–1117: "File candidates belong UPFRONT in L1… NOT in a late reminder.")

### 10. L4 auto_query vs L4 prefetch — `<gt-prefetch>` is not in the marker contract (latent, currently masked)
- **LIPI: Integration (marker-contract gap).**
- The L4 *prefetch* block is tagged `<gt-prefetch>` (6690) but `L3B_MARKERS`/`L3_MARKERS` (evidence_markers.py 8–40) do NOT include `<gt-prefetch`. Today this is masked because the prefetch is injected via the brief (7218 → `sanitize_evidence_block`, no marker gate) rather than via `_deliver_or_trace`. But if anyone routes the prefetch through `_deliver_or_trace`/`has_gt_evidence` (the documented delivery invariant at 1913–1919), it will silently fail the marker check and be dropped as `ROUTER_EMIT_MARKER_MISMATCH` (1942–1957) — a brand-new INERT path. The L4 *auto_query* (`[GT_AUTO]`, 4688) is safe only because `[GT_AUTO]` IS in the list (markers line 15).
- **Fix:** add `<gt-prefetch` to `L3B_MARKERS` now so the tag is recognized by the single delivery authority regardless of which path emits it. Cheap, generalized, prevents a future silent drop.

### 11. L4 prefetch timing/emptiness · INTENT: front-load issue-symbol evidence in turn 1 · BUG: frequently returns `""` yet records a "fired" prefetch event → the telemetry/observation mismatch the audit sees
- **LIPI: Plumbing (producer empties, telemetry still counts a fire).**
- `_run_l4_prefetch` (6608) records `tel.record_l4_prefetch(0,0)` and returns `""` whenever symbol selection yields nothing (6620–6623) or every `gt_query` line is filtered (6653/6682–6683). At 7216–7231 an empty prefetch logs an `L4 prefetch … suppressed … no_prefetch_results` event. So L4 shows a prefetch *event* with zero delivered content — precisely the "fires in telemetry, nothing in observation" pattern on tasks where `_select_issue_seeded_symbols` (6495) misses (homonym filter 6571 too aggressive, or candidate files lack the issue symbol). Not strictly a logic bug, but it is the concrete generator of the INERT-looking L4 telemetry.
- **Fix:** keep the empty-return (correct-or-quiet) but stop emitting a "prefetch" structured event on empty output — emit nothing, so telemetry "L4 fired" count equals L4 delivered count. Aligns the AGENT-OBSERVATION truth rule with the event log.

---

## Summary of the INERT/UNDELIVERED root cause (the question asked)
L4/L5/L5b show up in telemetry but never in an agent observation for four distinct, compounding reasons:
1. **Finish-handler dead writes** (#1, #2): governor/goku/`render_l5_advisory` run after `state=FINISHED`; the code even labels them `finish_handler_dead_write` and appends anyway.
2. **Dead code** (#3 `_maybe_fire_l5`; #6 `render_l4_tool_footer→""`, `gt_validate` chain): defined/installed but never invoked or never rendered.
3. **Gate asymmetry starving the live path** (#4 governor only sees `skip` events; #5 goku never gets `diff_size`): the governor's deliverable branches are unreachable, so the only L5 events that DO log are the non-deliverable ones.
4. **Empty producers that still log a fire** (#11): L4 prefetch records an event with zero content.

The consensus `<gt-scope>` (#9) is a separate, structural problem: it is wired into `post_view`, so it is **confirmatory by position** and cannot be causal. The two brief rankings (#7) and the rescue file/evidence mispairing (#8) are the cross-wired/wrong-fact bugs in the parts of L1/L5b that DO reach the agent.

**Genuinely-live agent-facing paths (for contrast, not bugs):** L1 brief, L3b post_view (`_deliver_or_trace`, 4911/5169), L3 post_edit (`append_observation`, 5843/legacy 6224), L4 auto_query (4689), consensus `<gt-scope>` (4791 — delivered but confirmatory), and `_maybe_fire_presubmit_verify` (4444→1231, the one L6 path that fires mid-trajectory with real data 5181).

---

I now have the full file. Let me focus the LIPI on the IMPORT/CALLER/SIBLING families, the deterministic gate, marker threading, and the sibling same-file check, cross-referencing the helpers each touches. I have all the code I need; producing the findings.

A few items I want to nail precisely before writing — the `classify_caller_usage` call-line indexing and the SIBLING `read_lines` path vs same-file check. I've read both (lines 353-385, 1431-1453, 213-225). Producing the final review.

---

# LIPI review — `benchmarks/swebench/gt_intel.py` (IMPORT / CALLER / SIBLING focus)

Numbered findings. Each: FUNCTION · INTENT · BUG · LIPI avenue · file:line + quote · generalized fix. Clean functions skipped.

---

### 1. `get_callers` — CALLER family lacks the DETERMINISTIC gate its twin `get_callees` has (INCONSISTENT FILTER + WRONG-FACT)
**Intent:** return cross-file callers of the target so the CALLER family can warn the agent who depends on it.
**Bug:** `get_callees` (L1/IMPORT, line 1320) gates on `_deterministic_sql_in()` — name_match excluded, "correct-or-quiet." Its symmetric twin `get_callers` (CALLER family) gates on `_resolution_sql_in()` (line 270), which returns the **full active set INCLUDING `name_match`**. So a name_match caller — a same-name guess across files/classes — is rendered to the agent as a real dependent with `[POSSIBLE: name match]`. CLAUDE.md is explicit: "rendering one as ... a phantom caller is maximally-harmful." The two halves of the same call-relationship pillar (callers vs callees) use different gates; callers is the leaky one.
**LIPI avenue:** Integration (two symmetric paths, one gated stricter than the other).
**Evidence:** line 270 `ph, methods = _resolution_sql_in()` inside `get_callers`, vs line 1320 `ph, methods = _deterministic_sql_in()` inside `get_callees`. The module docstring for `get_callees` at 1312 even says callers should be deterministic-only too: "a name_match caller is a phantom: a same-name guess across files/classes, never a real dependent."
**Note on intent vs reality:** the suffix `[POSSIBLE: name match]` shows the *author intended* to deliver name_match callers WITH a calibration tag (the "agentic-rag noise is bait" philosophy). That is a legitimate design choice and contradicts the deterministic-only `generate_pretask_briefing` top-caller. **This is a real self-contradiction in the codebase's own policy** — the briefing path (`generate_pretask_briefing` line 1148-1157) gates the top-caller on `det_methods`, but `compute_evidence`→`get_callers` (the post-edit path) does not. Same fact ("who calls X"), two policies.
**Generalized fix:** pick ONE policy for "caller as fact" and apply it to both `get_callers` and the briefing top-caller query. If name_match callers are delivered, they must carry the `[POSSIBLE]` tag in BOTH paths; if they are facts-only, `get_callers` must switch to `_deterministic_sql_in()`. Structural, no task logic. Confidence: HIGH.

---

### 2. `classify_caller_usage` — call-line text is picked by a fixed window offset, not by the actual call line (WRONG-FACT / off-by-window)
**Intent:** read source around a call site and return the exact call-line text as the spec the agent sees.
**Bug:** the function reads a 4-line window `read_lines(root, file_path, max(1, call_line-1), call_line+2)` (line 358) then extracts the "call line" as `lines[min(1, len(lines)-1)]` (line 364) — i.e. it **always takes the 2nd line of the returned window** (index 1), assuming the window starts at `call_line-1`. But `read_lines` clamps `start` to `max(1, …)` AND **dedents** the chunk. When `call_line == 1`, the window starts at line 1 (not 0), so index 1 is line 2 — the WRONG line. The "called as:" text the agent is told is the call site is off by one whenever the call is on line 1. More importantly the window start passed here (`call_line-1`) and the index assumption (`1`) are coupled by hand; any clamp at the file head desynchronizes them.
**LIPI avenue:** Implementation (off-by-one tied to a clamp) + Plumbing (line text paired with the wrong source line).
**Evidence:** line 358 `text = read_lines(root, file_path, max(1, call_line - 1), call_line + 2)`; line 364 `call_text = lines[min(1, len(lines) - 1)].strip()`.
**Generalized fix:** read with a known anchor and compute the offset from the actual returned start, or read the single `call_line` directly for the spec text (`read_lines(root, file, call_line, call_line)`), keeping the window only for the regex classification. Confidence: HIGH (the edge case is real; head-of-file calls are not rare).

---

### 3. `classify_caller_usage` — destructure/attribute regexes match the surrounding window, not the call line → score 3 misattributed (WRONG-FACT)
**Intent:** score how the caller *uses the return value* (destructure / isinstance / attribute access = score 3).
**Bug:** every `re.search` runs over `text` — the **full 4-line window** (line 358) — not over the call line. So a destructuring assignment or `isinstance(` or `.attr` on a NEIGHBORING line (the line before or two lines after the call) promotes this caller to score 3 ("called as: …") even though the call itself does nothing with the return value. The score (which drives ranking and the `[VERIFIED]` vs `[WARNING]` tier) is sourced from a different line than the `call_text` it's paired with. Sibling sub-signals (the score and the displayed line) come from different rows of the window.
**LIPI avenue:** Logic (regex scope wrong) → Integration (score from window, displayed text from one line — they can disagree).
**Evidence:** lines 369-374, all `re.search(..., text)` where `text` is the 4-line window; the returned `call_text` is only `lines[1]`.
**Generalized fix:** run the classification regexes against `call_text` (the single call line) — or against `call_text` plus the immediately-following continuation line only — so the score and the displayed spec describe the same statement. Confidence: HIGH.

---

### 4. `get_siblings` — no same-file containment check; cross-file "siblings" leak via parent_id collision (WRONG-FACT / SIBLING leak)
**Intent (per task focus):** SIBLING family should surface behavioral norms from methods *in the same class* (= same file).
**Bug:** `get_siblings` selects `WHERE parent_id=? AND label IN ('Function','Method') AND id!=?` (line 302) with **no `file_path` constraint**. The focus note explicitly asks for the "sibling same-file check" — it is absent. `parent_id` is an integer FK into `nodes`; if the indexer ever assigns the same `parent_id` to nodes in different files (the Go-receiver-method reparenting in CLAUDE.md `linkGoReceiverMethods`, partial-classes in C#, reopened classes in Ruby, or any parent_id=0/NULL-ish collision), the query returns cross-file "siblings." Then `compute_evidence` reads `best_sib.file_path` (line 1437) and the return-type-norm vote (line 1448) is computed over a class that spans files — a fabricated "(N/M siblings agree)" contract. The IMPORT family (line 1399) and CALLER family (line 276) both have explicit same-file / cross-file guards; SIBLING has none.
**LIPI avenue:** Plumbing (no file_path normalization/containment) + Logic (class-membership assumed from parent_id alone).
**Evidence:** line 301-304 — the SELECT has `parent_id=?` and `id!=?` but no `file_path`. Contrast CALLER line 276 `AND e.source_file != ?` and IMPORT line 1399 `if callee.file_path == target.file_path: continue`.
**Generalized fix:** add `AND file_path = (SELECT file_path FROM nodes WHERE id = :target_id)` to the sibling query (or pass the target's file_path and filter). Structural, language-agnostic — a class's methods are same-file by definition in every supported language. Confidence: HIGH that the guard is missing; MODERATE that it bites in practice (depends on indexer parent_id behavior, but the Go reparenting note in CLAUDE.md makes collision plausible).

---

### 5. SIBLING family — return-type-norm threshold counts ALL siblings as denominator but only typed ones as candidates (WRONG denominator → diluted vote)
**Intent:** upgrade SIBLING to score 3 only when ≥70% of siblings agree on a return type.
**Bug:** `ret_types = [s.return_type for s in siblings if s.return_type]` (line 1448) drops untyped siblings, but the agreement ratio is `common[1] / max(len(siblings), 1)` (line 1451) — denominator = **ALL siblings, including untyped ones**. In a partially-annotated class (3 of 10 methods typed, all 3 `Optional[User]`), the vote is `3/10 = 0.30 < 0.7` → the genuine, unanimous-among-typed contract is suppressed. Conversely the displayed string `"({common[1]}/{len(siblings)} siblings agree)"` (line 1453) reports `3/10`, which understates the actual agreement and misleads the agent. The numerator's universe (typed) and the denominator's universe (all) differ.
**LIPI avenue:** Logic (wrong denominator for a ratio threshold).
**Evidence:** line 1448 filters to typed; line 1451 `common[1] / max(len(siblings), 1)`; line 1453 `{common[1]}/{len(siblings)}`.
**Generalized fix:** compute the ratio over `len(ret_types)` (typed siblings) with a minimum-support floor (e.g. require `len(ret_types) >= 2` so two typed siblings can't trivially hit 100%). Report `common[1]/len(ret_types)`. Generalized — applies to any partially-typed language. Confidence: HIGH.

---

### 6. `_resolution_suffix` vs `_evidence_to_finding_dict` — `name_match` callers carry a `[POSSIBLE]` tag in text but are silently rebranded by confidence math in JSON; and IMPORT's deterministic-only invariant is not enforced at the suffix (INCONSISTENT / latent WRONG-FACT)
**Intent:** thread the resolution method so the agent can calibrate trust; same evidence rendered to text and to findings-JSON should agree.
**Bug:** In text, a name_match CALLER prints `[POSSIBLE: name match]` (line 998-999). In findings-JSON, `_evidence_to_finding_dict` maps the SAME node: `name_match` → `conf = 0.3 + score*0.15` (line 1967-1968). A name_match CALLER with score 3 (the `classify_caller_usage` window can produce score 3, finding #3) → `conf = 0.3 + 0.45 = 0.75` → `tier = "WARNING"`, `severity = "warning"`. So the surface meant to be downstream-consumable upgrades a same-name guess to a 0.75-confidence WARNING finding with `agent_action: verify`. The two renderings of one node disagree on how strongly the name_match guess is asserted. (IMPORT is safe here only because `get_callees` already excludes name_match upstream — but `_resolution_suffix` would still stamp `[VERIFIED: import]` on anything labeled import, so the invariant lives entirely in the query gate, not the renderer.)
**LIPI avenue:** Integration (two output surfaces, divergent confidence semantics for the same node).
**Evidence:** line 998 `return " [POSSIBLE: name match]"` vs line 1967-1968 `elif rm == "name_match": conf = min(1.0, 0.3 + node.score * 0.15)`.
**Generalized fix:** derive the JSON confidence tier from the SAME resolution-class buckets the text suffix uses (deterministic → high, name_match → cap below the WARNING threshold regardless of score). Floor name_match findings at INFO so a same-name guess can never reach `severity: warning`. Confidence: MODERATE-HIGH (depends on the findings-JSON surface actually being consumed; if inert, demote to latent).

---

### 7. `rank_and_select` — negative-spec boost matches `"not"` / `"false"` as substrings of `summary`, over-boosting unrelated CALLER/TYPE/IMPACT lines (WRONG-FACT / hub-style over-rank)
**Intent:** boost TEST evidence that encodes a constraint (raises/error/exception) to score 3 because constraint violations are highest-value.
**Bug:** the guard is `if c.family == "TEST" and any(kw in c.summary.lower() for kw in ("raises","error","exception","false","not"))` (line 1692). The keywords `"not"` and `"false"` are matched as **bare substrings** of a lowercased summary. TEST summaries are mostly `"{n} assertions"` or `"test function references {target}"` — but `"not"` is a substring of common tokens (`note`, `notify`, `another`, `cannot`, `notification`) and `"false"` appears in any summary mentioning a boolean. A TEST node whose summary happens to contain `"cannot"` or `"notification"` gets boosted to score 3 and outranks a genuine deterministic CALLER/IMPORT. This is a generalized over-ranking bug, not benchmark-specific, but it pollutes ordering.
**LIPI avenue:** Logic (substring match where token/word match is intended).
**Evidence:** line 1692 `any(kw in c.summary.lower() for kw in ("raises", "error", "exception", "false", "not"))`.
**Generalized fix:** match on word boundaries (`re.search(r'\b(raises|error|exception|false|not)\b', …)`) or, better, drive the boost from the assertion KIND (the `assertions` table `kind` column or the `raises`/`assertRaises` spec strings already produced in `_extract_assertions_ast`), not from the human-readable summary. Confidence: HIGH.

---

### 8. `compute_evidence` TYPE-upgrade reads `"destruct"` from `summary`, but `classify_caller_usage` never writes that word → permanently dead upgrade path (DEAD PATH)
**Intent:** upgrade TYPE evidence to score 2 when a CALLER destructures the return value (a strong return-type-contract signal).
**Bug:** the condition is `any(c.score >= 2 and "destruct" in c.summary for c in candidates if c.family == "CALLER")` (line 1492). But `classify_caller_usage` (lines 369-385) **never emits the substring "destruct"** in any summary — score-3 destructure cases produce `summary = f"called as: {call_text}"`. So `"destruct" in c.summary` is always False. The TYPE→score-2 upgrade is dead code; TYPE evidence is permanently stuck at score 1 and gets out-prioritized/truncated. The signal the author wanted (destructuring caller ⇒ strong type contract) never fires.
**LIPI avenue:** Implementation (dead branch — string the producer never writes) + Integration (producer/consumer vocabulary mismatch: producer writes `"called as:"`, consumer greps `"destruct"`).
**Evidence:** line 1492 `"destruct" in c.summary`; producer at lines 369-374 returns `f"called as: {call_text}"` for the destructure case, never the word "destruct."
**Generalized fix:** have `classify_caller_usage` return a structured usage-kind enum (e.g. `usage="destructure"`) on the EvidenceNode and check `c.usage == "destructure"`, rather than substring-grepping a display string. Or, minimally, tag the destructure branch's summary with a stable marker. Confidence: HIGH.

---

### 9. `_format_import_for_language` (IMPORT) — Go/C# import path is keyed off the file's directory, fabricating a module path that is not the import path (WRONG-FACT, generalized)
**Intent:** emit a copy-pasteable, correct import statement for a cross-file callee — the "#1 hallucination prevention signal" (line 1394).
**Bug:** For Go, `pkg = os.path.dirname(path)` then `import "{pkg}"` (line 1361-1362). The Go import path is the **module path + package import path**, NOT the on-disk relative directory — `internal/foo/bar.go` is imported as `"github.com/org/repo/internal/foo"`, never as `"internal/foo"`. The emitted `import "internal/foo"` is a fabricated, copy-pasteable, WRONG import — exactly the `HALLUCINATED-IMPORT` failure this family is taxonomy-labeled to prevent (line 981). Same class of error for C# (`os.path.dirname(path).replace("/",".")` ≠ the C# `namespace`, which is declared in-file and need not track the directory) and Java (`os.path.splitext(path)[0].replace("/",".")` assumes the package == the path from repo root, which is false unless the source root is the repo root). The IMPORT family is gated deterministic-only at the EDGE level (good), but the rendered string is then synthesized from a path heuristic that is wrong for several Tier-1 languages.
**LIPI avenue:** Logic (module-path model is wrong for Go/C#/Java) — the edge is a fact, the *rendered import* is not.
**Evidence:** line 1361 `pkg = os.path.dirname(path)`; line 1362 `return f'import "{pkg}"  // {name}'`.
**Generalized fix:** for languages where the import path ≠ on-disk path (Go, Java, C#, Rust crate paths), either (a) read the actual `package`/`namespace`/`module` declaration from the callee's file header (the indexer can store it as a node/file property), or (b) downgrade to the neutral `"{name} (from {path})"` form (the `else` branch, line 1382) and let the agent resolve the import — correct-or-quiet. Never synthesize a structural import path from a directory guess. Confidence: HIGH (Go import path ≠ dirname is categorical).

---

### 10. IMPORT family — `same_file` callees that the deterministic gate admits are silently dropped, but `import` vs `same_file` is the wrong axis for "needs an import" (minor INCONSISTENT FILTER)
**Intent:** only emit IMPORT evidence for callees in OTHER files (same-file needs no import).
**Bug:** `get_callees` admits the full deterministic set including `same_file` (it's in `_DETERMINISTIC_RESOLUTIONS`, line 75). `compute_evidence` then drops same-file callees with `if callee.file_path == target.file_path: continue` (line 1399). That's correct, but it means every `same_file`-resolution edge is fetched (LIMIT 10, line 1329) and then discarded — same-file edges can consume the entire LIMIT-10 budget, starving genuine cross-file `import`/`type_flow` callees that would actually produce IMPORT evidence. On a target that calls many same-file helpers plus a few cross-file ones, the cross-file callees can be pushed out of the top-10 and never rendered.
**LIPI avenue:** Plumbing (the LIMIT is applied before the same-file filter, so the budget is spent on rows that are guaranteed to be dropped).
**Evidence:** line 1324-1330 fetches `LIMIT 10` over all deterministic methods; line 1399 then drops `callee.file_path == target.file_path`.
**Generalized fix:** push the cross-file predicate into SQL: `JOIN nodes n … WHERE … AND n.file_path != :target_file` before the `LIMIT 10`, so the budget is spent only on callees that can yield an import. Pure SQL, language-agnostic. Confidence: HIGH (ordering bug, mirrors the documented `set_fields` "LIMIT before relevance-rank" class in CLAUDE.md).

---

### 11. `format_output` — `[VERIFIED] TARGET … (1.00)` is emitted unconditionally even when the target came from the fuzzy basename fallback (SELF-CONTRADICTION / over-confident)
**Intent:** label the target the agent is editing.
**Bug:** `get_target_node` has two fallbacks: an exact `file_path=?` match, then a `file_path LIKE '%basename'` fuzzy match (line 252-260), then (in `--file` mode with no function) "node with most incoming CALLS." Regardless of which path produced it, `format_output` prints `[VERIFIED] TARGET: {name} (…) (1.00)` (line 1837) with a hardcoded `(1.00)`. When the node came from the fuzzy `LIKE '%basename'` match — which can resolve to a DIFFERENT file that merely shares a basename (e.g. two `utils.py` in different packages) — the agent is told `[VERIFIED] … (1.00)` about a function in the wrong file. This is the over-confident-on-weak-signal inversion the constitution names explicitly (`.claude/CLAUDE.md` "confident on weak signals").
**LIPI avenue:** Logic (confidence label decoupled from how the target was resolved) + Integration (resolver tier not threaded to the formatter).
**Evidence:** line 1837 `lines.append(f"[VERIFIED] TARGET: {target.name} ({target.file_path}:{target.start_line}) (1.00)")`; resolver fuzzy path at lines 252-260.
**Generalized fix:** have `get_target_node` return a resolution tier (exact-file / fuzzy-basename / most-callers-heuristic) and have `format_output` emit `[VERIFIED]…(1.00)` only for exact-file matches, `[LIKELY]` otherwise. Confidence: MODERATE-HIGH.

---

### 12. `generate_pretask_briefing` — TEST sub-query uses `res_methods` (name_match allowed) while the TWIN top-caller uses `det_methods` (INCONSISTENT FILTER within one function)
**Intent:** within the briefing, render a FIX-HERE node plus its top caller (fact) and a referencing test.
**Bug:** the top-caller query is gated `e.resolution_method IN ({det_methods})` (line 1152) — deterministic-only, correct. The test query immediately below is gated `e.resolution_method IN ({res_methods})` (line 1164) — the FULL active set including name_match. So a `TEST: {file}::{name}` line (line 1168) can be produced from a name_match edge: a same-name guess that a test "calls" the target. The agent is told a specific test file exercises the function when the linking edge is a guess. The two adjacent sub-blocks of the same briefing entry are sourced from different gates with no calibration tag on the test line.
**LIPI avenue:** Integration (sibling sub-queries in one function, asymmetric gates) + Plumbing (no resolution tag on the rendered TEST line, unlike `_resolution_suffix` elsewhere).
**Evidence:** line 1152 (`det_methods`) vs line 1164 (`res_methods`) within `generate_pretask_briefing`.
**Generalized fix:** decide whether a "this test references X" claim is a fact (→ `det_methods`) or a hint (→ keep `res_methods` but append a `[POSSIBLE: name match]` tag, matching `_resolution_suffix`). Pick one and apply consistently. Confidence: HIGH (asymmetry is plain in the source).

---

## Summary of highest-confidence, in-focus findings

| # | Family | Avenue | Severity |
|---|---|---|---|
| 1 | CALLER | Integration — missing deterministic gate vs `get_callees` twin | HIGH |
| 4 | SIBLING | Plumbing/Logic — no same-file check (the named focus item) | HIGH |
| 5 | SIBLING | Logic — wrong denominator on return-type vote | HIGH |
| 8 | IMPORT/TYPE | Implementation — dead `"destruct"` upgrade (producer/consumer vocab mismatch) | HIGH |
| 9 | IMPORT | Logic — Go/Java/C# import path fabricated from dirname | HIGH |
| 2,3 | CALLER | Implementation/Logic — call-line window off-by-one + score from wrong line | HIGH |
| 12 | TEST-in-briefing | Integration — name_match gate on TEST vs det gate on caller | HIGH |
| 10 | IMPORT | Plumbing — LIMIT-before-same-file-filter starves cross-file callees | HIGH |
| 7 | TEST/ranking | Logic — `"not"`/`"false"` substring over-boost | HIGH |
| 6 | CALLER/JSON | Integration — text `[POSSIBLE]` vs JSON 0.75 WARNING for same node | MOD-HIGH |
| 11 | target label | Logic — hardcoded `(1.00)` on fuzzy-resolved target | MOD-HIGH |

**The two findings that most directly answer the focus note:** (#1) the CALLER deterministic-gate gap — `get_callers` uses `_resolution_sql_in()` (name_match in) while its callee twin `get_callees` uses `_deterministic_sql_in()` (name_match out); and (#4) the SIBLING same-file check is entirely absent — `get_siblings` filters on `parent_id` alone with no `file_path` containment. Marker threading (#6, #12) is inconsistent across the text and JSON surfaces and across the two TEST gates in the briefing.

I did not edit anything. File reviewed: `D:\Groundtruth\benchmarks\swebench\gt_intel.py`.

---

`CalleeName` is the last component, `CalleeQualified` is the full qualified name. This confirms that in Strategy 1.94, `methodName194 := calleeName` is correct (the method name = last component). But it also means the `dotIdx194` extraction of `methodName` from CalleeQualified at the separator is redundant — and crucially, when separator is `::` the slice math differs. Let me verify the exact separator-length bug in 1.94 (it splits qualifier but never recomputes methodName from the right separator offset — it uses `calleeName` directly, so that's safe). 

Let me now confirm the candidate-ordering nondeterminism concern in Strategy 1.94's map iteration and Strategy 2's "first target" pick by checking whether `nodeIDs`/`methodClassCount` ordering is deterministic. These are Go maps, so iteration order is randomized. Let me write up the complete findings.

Based on my exhaustive read of all 2237 lines, here are the findings.

---

**GRANULAR LIPI REVIEW — `gt-index/internal/resolver/resolver.go`**

Scope: resolution strategies in `Resolve()` (lines 464–1237) plus the index-builders they depend on. I read every function in order; clean helpers (ParseTSConfig, ExpandTSConfigPath, FindGoModulePath, appendUnique, BuildNodeMeta, stripTypeWrapper) are skipped. Findings ordered by harm.

---

**1. `Resolve` / Strategy 1.94 (impl_method) · INTENT: resolve `obj.method()` when the method name belongs to 1–3 classes anywhere, with graduated confidence · BUG: confidence/tier mismatch — a 1-class hit is stamped `CERTIFIED` + conf `0.85` purely on *global method-name uniqueness*, with ZERO check that the receiver `obj` is actually that class.**
- LIPI: **Logic.** Lines 919–933, 959–963: `if classes194 ... len==1 { conf194 = 0.85; tier194 = "CERTIFIED" }` then `Method: "impl_method", TrustTier: tier194`. The qualifier is explicitly *not* a known class (it was excluded at 906–917 `qualifierIsClass`), and its declared type is never consulted here. So this fires on a receiver of *unknown* type and asserts CERTIFIED because the method name happens to exist in exactly one class. That is RTA-without-the-receiver — the exact "unique_method ignoring receiver" failure mode. A free function and a single-class method sharing a name (e.g. one class has `run()`, the receiver is actually an unrelated stdlib/3rd-party object) yields a confident false edge.
- Quote (959–962): `Method: "impl_method", Confidence: conf194, ... TrustTier: tier194` with `tier194 = "CERTIFIED"` for `numClasses == 1`.
- GENERALIZED fix: `impl_method` resolved by global name-uniqueness alone (no receiver-type evidence) must never reach CERTIFIED. Cap the 1-class case at the CANDIDATE tier (conf ≤ ~0.6), reserving CERTIFIED for stages that *prove* the receiver type (1.75 self, 1.93/1.94a import/declared-type, 1.95/1.96 type_flow). This is a structural rule (uniqueness-of-name ≠ proof-of-receiver), not benchmark logic.

**2. `Resolve` / Strategy 1.98 (unique_method) · INTENT: if a method name is defined in exactly one class, any `x.method()` resolves to it · BUG: DEAD CODE — it can never fire, because Strategy 1.94 (above) already handles the 1-class case and `continue`s first.**
- LIPI: **Integration (two strategies, one gated upstream).** 1.94 fires for `len(classes) in [1,3]` and on success does `continue` (971–973). 1.98 (1163–1186) targets `uniqueMethodClass[calleeName]` = exactly the `len(classes)==1` subset (built at 535–542). Every input that reaches 1.98 with a 1-class method has already been consumed (and `continue`d) by 1.94, OR was rejected by 1.94's `qualifierIsClass`/`bestTarget194==0` guards — and 1.98 has no such guards, so the only way 1.98 *can* fire is on a case 1.94 rejected, which is precisely when 1.94 chose *not* to resolve. Net: 1.98 is either shadowed or fires on a residue 1.94 deliberately skipped. The two are mutually inconsistent: same fact, two different `Method`/`TrustTier`/`Confidence` labels (`impl_method`/`CERTIFIED`/0.85 vs `unique_method`/`CANDIDATE`/0.85). The downstream tier therefore depends on *which strategy won*, not on the evidence.
- Quote (1175–1178): `Method: "unique_method", Confidence: 0.85, ... TrustTier: "CANDIDATE"` vs 1.94's `CERTIFIED` for the identical 1-class fact.
- GENERALIZED fix: collapse 1.94's 1-class branch and 1.98 into ONE stage with ONE (tier, conf) for "method name unique to one class, receiver type unknown." Pick the *honest* tier (CANDIDATE) for both. Eliminates the self-contradiction where the same structural fact is CERTIFIED on one path and CANDIDATE on another.

**3. `Resolve` / Strategy 1.94 best-target selection · INTENT: among the ≤3 candidate classes, pick the best receiver class · BUG: nondeterministic target on multi-candidate ties — iterates a Go map (`classes194`) and takes "first" with no stable ordering, so a 2- or 3-class method resolves to a *different* class across runs.**
- LIPI: **Implementation / Plumbing (determinism).** Lines 936–948: `for classID := range classes194 { ... if bestTarget194 == 0 { bestTarget194 = targetID } }`. `classes194` is `map[int64]bool`; Go randomizes map iteration. Only same-file wins deterministically (940–943); when no candidate is same-file, "first" is whichever the runtime yields. Same bug pattern in Strategy 2 (1200–1208, `for _, targetID := range targets { if bestTarget == 0 ...}`) — there `targets` is a slice so it's stable, but 1.94 is a map. This violates CLAUDE.md Stage-1 ("same input → same correct output"): the graph.db edge target for these calls is run-dependent.
- Quote (936): `for classID := range classes194 {` then (944–946) `if bestTarget194 == 0 { bestTarget194 = targetID }`.
- GENERALIZED fix: sort candidate class IDs (or pick min `classID`, or sort by `(file, start_line)`) before the same-file/first selection so the tie-break is deterministic. Pure structural ordering, language-agnostic.

**4. `Resolve` / Strategy 1.9 (verified_unique) · INTENT: a globally unique unqualified name is 99% correct → CERTIFIED 0.95 · BUG: `qualifiedUnresolved` demote is checked, but the per-file same-name shadow that should also demote is not — and more importantly the unqualified single-candidate edge is stamped `CERTIFIED`/`verified_unique` even when the name is a 1-candidate-but-still-a-guess fallback that Strategy 2 would have floored.**
- LIPI: **Logic (tier inflation on the single-candidate path).** Lines 737–760: when `len(candidates)==1` and NOT qualifiedUnresolved → `verified_unique, 0.95, CERTIFIED, name_unique`. This is the ONLY single-candidate path (Strategy 2 requires `candidateCount > 1`, line 1210), so EVERY name with exactly one global definition is CERTIFIED regardless of whether the call is an actual import/same-file reference or just a coincidental unique name. The ACG citation in the comment (711–720) applies to *unqualified function calls in dynamic languages*; the code applies it to every unqualified single-candidate name in every language including ones where same-name-by-coincidence is common. The demote path (743–747) correctly handles qualified-unresolved, but the unqualified branch has no shadow/locality check.
- Quote (742): `method, conf, tier, evidence := "verified_unique", 0.95, "CERTIFIED", "name_unique"`.
- GENERALIZED fix: this is defensible AS A RULE (global uniqueness is strong) but the comment-vs-code research scope mismatch means it should be gated: keep CERTIFIED only when the call is genuinely unqualified (already true) AND the target is in an import-reachable or same-package file; otherwise demote to CANDIDATE. Structural (import-reachability), not benchmark.

**5. `Resolve` / Strategy 1.5 import-verified "pick best" · INTENT: when an import yields multiple candidate target files, pick the best · BUG: comment claims "prefer same dir" but code blindly takes `importCandidates[0]`, and stamps conf=1.0/CERTIFIED on a MULTI-candidate import (CandidateCount can be >1).**
- LIPI: **Implementation (comment/code divergence) + Logic (tier on ambiguous import).** Line 580 comment: "collect all matching imported targets, pick best (prefer same dir)". Lines 638–640: `bestTarget := importCandidates[0]` — no same-dir preference, no sort; just first. Then 649–652 emits `Method: "import", Confidence: 1.0, CandidateCount: len(importCandidates), TrustTier: "CERTIFIED"`. When two files both export `calleeName` and both are imported (wildcard + specific, or re-export chains), this is CERTIFIED 1.0 on an *ambiguous* pick decided by map/slice order. The `CandidateCount: len(importCandidates)` is even recorded as >1, contradicting the 1.0/CERTIFIED stamp.
- Quote (640): `bestTarget := importCandidates[0]` after a comment promising same-dir preference.
- GENERALIZED fix: implement the promised tie-break (prefer target file in the caller's directory, else lexicographically smallest path for determinism); when `len(importCandidates) > 1` and no same-dir winner, demote below CERTIFIED. Both are structural.

**6. `Resolve` / Strategy 1.93 import_type · INTENT: scope class lookup to the imported file when `Qualifier.method()` and `Qualifier` is imported · BUG: dead `sep` handling — `::` separator is detected but `methodName` is sliced with `len(sep)` which is correct, yet the `qualifier` recompute at 779–781 is a no-op, and the self/this guard omits `Self`, so a Rust `Self::method()` that slips past 1.75 (no parent meta) is mis-scoped.**
- LIPI: **Implementation (dead branch) + Logic (missing `Self` guard).** Lines 776 `_ = sep` then 779–781: `if sep == "::" { qualifier = call.CalleeQualified[:dotIdx] }` — identical to the value already assigned at 778, so the branch does nothing. Line 783 guards only `qualifier != "self" && qualifier != "this"` — `Self` (Rust) is not excluded here (unlike 1.75/1.94/1.94a/1.95 which all exclude `Self`). If 1.75 didn't fire (caller has no ParentID), a `Self::foo()` reaches 1.93 with `qualifier=="Self"`, and if a class literally named `Self` is imported it mis-resolves; more practically the missing guard is an inconsistency vs every sibling strategy.
- Quote (779–781): `if sep == "::" { qualifier = call.CalleeQualified[:dotIdx] }` (no-op) and (783) `if qualifier != "self" && qualifier != "this" {`.
- GENERALIZED fix: delete the dead `sep=="::"` re-assignment; add `&& qualifier != "Self"` to match the other four strategies. Pure consistency.

**7. `Resolve` / Strategy 1.94 separator math · INTENT: extract qualifier from `CalleeQualified` for both `.` and `::` · BUG: `dotIdx194 <= 0` for `.` then falls back to `::`, but the qualifier slice `[:dotIdx194]` is correct for both while the method is taken from `calleeName` (fine) — the latent bug is that a qualifier containing BOTH (e.g. `mod::Type.method` mixed forms, or `a.b.c`) takes only `LastIndex`, so `qualifier194` = `a.b` is looked up in `nodeIDs` as a class and silently fails the class check, suppressing resolution.**
- LIPI: **Logic (multi-segment qualifier).** Lines 897–902: `dotIdx194 := LastIndex(".")`; qualifier = everything before the LAST dot. For `self.obj.method()` the `self.` prefix is NOT stripped here (only 1.96 strips `self.`), so `qualifier194 = "self.obj"`, which is not a class name → `qualifierIsClass=false` → it then resolves by global method-name uniqueness (bug #1) attributing a `self.obj.method()` call to an arbitrary unique class. This is the receiver-ignoring path firing on a chained receiver.
- Quote (902): `qualifier194 := call.CalleeQualified[:dotIdx194]` with no `self.`/`this.` stripping before the class check.
- GENERALIZED fix: 1.94 should bail when the qualifier still contains a separator (chained/compound receiver it cannot type) rather than fall through to name-uniqueness. Structural guard.

**8. `Resolve` / Strategy 1.96 assignment-flow · INTENT: `x = ClassName(); x.method()` resolves via assignment tracking · BUG: handles only `.` separator — `if dotIdx := LastIndex(call.CalleeQualified, "."); dotIdx > 0` — so Rust `::` qualified assignment chains never resolve here, an asymmetry vs 1.93/1.94/1.94a/1.95 which all handle `::`.**
- LIPI: **Integration (inconsistent separator support across sibling strategies).** Line 1026: `if dotIdx := strings.LastIndex(call.CalleeQualified, "."); dotIdx > 0`. No `::` fallback. PyCG is Python-specific so this is partly intentional, but the surrounding strategies advertise multi-language `::` support; the result is that the same call shape resolves on Python and silently drops on Rust, which violates the "generalized across languages" pillar for this stage.
- Quote (1026): `if dotIdx := strings.LastIndex(call.CalleeQualified, "."); dotIdx > 0 {`.
- GENERALIZED fix: either add `::` fallback (mirroring 1.94a) or document that 1.96 is intentionally dynamic-language-only; today the asymmetry is undocumented and inconsistent.

**9. `Resolve` / Strategy 2 builtin-drop vs Strategy 1.9 strong-builtin-drop · INTENT: drop builtin-method `name_match` garbage (`join`,`get`,…) · BUG: INCONSISTENT FILTER between the two qualified-call paths — `strongBuiltinMethodNames` (1.9, single-candidate, line 727) and `builtinMethodNames` (Strategy 2, multi-candidate, line 1193) are DIFFERENT sets, so a single-candidate qualified `get`/`items`/`append` is NOT dropped by 1.9 and instead launders into a `name_match` edge at 743–747.**
- LIPI: **Integration (asymmetric gate on twin paths).** 1.9 drops only `strongBuiltinMethodNames` (join/split/loads/… — 379–387) for single-candidate; the broad `builtinMethodNames` (get/items/append/keys/… — 359–369) is applied ONLY in Strategy 2 (1193), which requires `candidateCount > 1` (1210). So a qualified `obj.get()` whose name has exactly ONE global definition skips the Strategy-2 builtin guard (single candidate → handled by 1.9), and 1.9's set excludes `get`, so it is emitted as `name_match_qualified_unresolved` to an arbitrary single same-named internal method — exactly the laundering the design says to prevent. The CLAUDE.md note ("100% of cc≤1 name_match are qualified_unresolved") confirms these single-candidate qualified edges exist and survive.
- Quote (727): `if qualifiedUnresolved && strongBuiltinMethodNames[calleeName] { continue }` (narrow set) vs (1193) `... && builtinMethodNames[calleeName] { continue }` (broad set, multi-candidate only).
- GENERALIZED fix: apply the BROAD `builtinMethodNames` set to the single-candidate qualified-unresolved path in 1.9 as well (drop, not demote), so a qualified builtin name is never emitted regardless of candidate count. One set, both paths.

**10. `Resolve` / Strategy 1 same-file · INTENT: resolve `name()` to a same-file definition only when unambiguous · BUG: requires `len(targetIDs) == 1` so a same-file call to an OVERLOADED name (Java/TS overloads, or a class+function same name) falls straight through to cross-file `name_match` — losing the *certain* same-file edge and emitting a speculative cross-file one instead.**
- LIPI: **Logic (over-strict gate downgrades a high-confidence local fact to a low-confidence remote one).** Lines 557: `len(targetIDs) == 1`. When a file defines two same-named symbols (e.g. a `Foo` class and a `Foo` factory, common in TS/Python), the same-file call is abandoned (576 comment: "fall through to name_match") and the agent's map gets a cross-file guess instead of the obviously-correct same-file target. Same-file locality is the single strongest signal and it's discarded on the first sign of local ambiguity.
- Quote (557): `if targetIDs, ok := fileNodes[calleeName]; ok && len(targetIDs) == 1 && targetIDs[0] != callerID`.
- GENERALIZED fix: when `len(targetIDs) > 1` same-file, still prefer same-file targets over any cross-file name_match — emit a same-file edge to the best local candidate (e.g. nearest by line, or the function-labelled one) at CANDIDATE tier rather than abandoning locality entirely. Structural (locality dominates).

**11. `computeConfidence` vs emitted tiers · INTENT: single source of truth for confidence by method · BUG: confidence and TrustTier are computed in TWO places that disagree — `computeConfidence` is bypassed for most strategies (hardcoded conf literals at each emit site), so the function is near-dead and the conf↔tier mapping is not centralized, allowing the mismatches in #1/#4/#5.**
- LIPI: **Implementation (dead/parallel logic).** `computeConfidence` (293–314) is called only at 745 and 1211 (both name_match). Every other strategy hardcodes its own `Confidence:` literal and `TrustTier:` literal independently (e.g. 0.85/CERTIFIED in 1.94, 0.85/CANDIDATE in 1.98 for the same fact). There is no invariant tying conf→tier, so CLAUDE.md's tier thresholds (`CERTIFIED ≥0.9`) are violated repeatedly: impl_method 0.85→CERTIFIED (1.94), return_type 0.85→CERTIFIED (1142–1144), unique_method 0.85→CANDIDATE (1176–1178), import_type 0.95 OK. 0.85 is mapped to CERTIFIED, CANDIDATE, AND CERTIFIED-again across strategies.
- Quote: 1.94 `Confidence: conf194(0.85) ... TrustTier: "CERTIFIED"` (960–962) vs 1.98 `Confidence: 0.85 ... TrustTier: "CANDIDATE"` (1176–1178) vs 1.97 `Confidence: 0.85 ... TrustTier: "CERTIFIED"` (1142–1144).
- GENERALIZED fix: derive `TrustTier` from `Confidence` via a single function (`tierFor(conf)`: ≥0.9 CERTIFIED, 0.5–0.9 CANDIDATE, <0.5 SPECULATIVE) applied at every emit site, so 0.85 can NEVER be CERTIFIED. This single change structurally fixes #1, #4, #5, and the 1.97 case.

**12. Strategy 1.97 return_type · INTENT: `get_user().save()` → resolve `save` on get_user's return type · BUG: 0.85 confidence stamped CERTIFIED (same tier violation as #11) AND it resolves the FIRST function named `qualifier` with a non-empty return type, ignoring overloads/same-name functions — a wrong-receiver pick when multiple functions share the qualifier name.**
- LIPI: **Logic (first-match over ambiguous function name) + tier.** Lines 1110–1124: `for _, funcID := range nodeIDs[qualifier] { ... retType := fm.ReturnType ... }` takes the first function with a return type; if two functions named `make_x` exist with different return types, the receiver class is whichever the slice yields first. Then 1142–1144 emits CERTIFIED at 0.85.
- Quote (1132): `if targetID, ok := methods[methodName]; ok && targetID != callerID` after picking the first return-typed `funcID`.
- GENERALIZED fix: only resolve when the qualifier name maps to a SINGLE function (or all candidates share a return type); demote to CANDIDATE (0.85<0.9). Structural.

---

**HIGHEST-CONFIDENCE, HIGHEST-HARM (fix first):**
- **#11 (tier derived from confidence)** — one structural change kills the CERTIFIED-on-0.85 laundering across #1, #4, #5, #12 simultaneously. This is the dominant correctness bug: graph.db marks 0.85-confidence guesses as CERTIFIED facts, and per CLAUDE.md every downstream FACT filter trusts CERTIFIED.
- **#1 / #2 (impl_method receiver-ignoring + unique_method dead/contradictory)** — the "unique_method ignoring receiver" the prompt flagged: a method-name's global uniqueness is asserted as a CERTIFIED receiver-typed edge with no receiver evidence, and the same fact gets two different tiers on two paths.
- **#9 (builtin single-candidate laundering)** — the named "name_match laundering": qualified `get`/`items`/`append` with one global candidate escapes both builtin guards and emits a garbage internal edge.

All fixes are structural properties of call resolution (locality dominance, name-uniqueness ≠ receiver-proof, tier-follows-confidence, one builtin set on both paths) — no task/file/repo/benchmark-specific logic. I did not edit any code.

---

I now have the full file. Let me verify one detail on the three flagged areas — the `goReceiverType` paren/comment parsing concern. The note says "goReceiverType paren/comment parsing, literal-receiver." Let me confirm the signature source. The signature is `extractSignature` (first line of node content, truncated to 200 chars). For a Go method like `func (r *T /* comment */) M()`, the receiver parsing uses `strings.IndexByte(s, ')')` which finds the FIRST `)`. Let me also confirm the literal-receiver `extractCalleeInfo` path — `funcChild.Child(0)` is the object/receiver in an attribute node. I have enough to write the findings.

Let me double check the `goReceiverType` edge case with a generic receiver containing a paren-like construct, and confirm `_tryExtractSideEffect`'s handling of `==` in a string. I have all the code. Now compiling the final granular LIPI review.

GRANULAR LIPI REVIEW — `gt-index/internal/parser/parser.go` (4920 lines, ~85 functions). I read the entire file. Findings below are ordered roughly by harm to the context graph. The three flagged areas (property Line attribution; goReceiverType paren/comment; literal-receiver) are findings #1, #2, #3, #6.

---

**1. `extractFunctionFingerprint` / `extractReturnShape` / `extractDataFlow` / `extractConcurrencyPatterns` / `extractConfigReads` / `extractCallOrdering` · INTENT: emit a structural fact about a function with the LINE the fact lives on · BUG: every one of these attributes `Line: int(bodyNode.StartPoint().Row)+1` (the function-body-start line) to a fact that occurs at an arbitrary INTERIOR line.** A `config_read` for `os.getenv("KEY")` 40 lines into the body is stamped with the body's first line; the agent (or any line-keyed consumer / `gt_intel` precedent join) is pointed at the wrong line. This is the **WRONG-FACT bug class (b): a value computed from row A is paired with the line of row B.** · **LIPI: Plumbing (wrong line field delivered) + Implementation (the correct line is reachable but discarded).** · file:line `parser.go:3102` `Line: int(bodyNode.StartPoint().Row) + 1` (fingerprint), `:2292` (return_shape), `:1723` (data_flow), `:4210/:4227` (concurrency), `:4349/:4381/:4411` (config_read), `:4462` (call_order). · **GENERALIZED fix:** carry the matching child/statement node to the emit and use `int(matchNode.StartPoint().Row)+1`. For text-scan extractors (config/concurrency) convert the byte offset `idx` to a line by counting `\n` in `bodyText[:idx]` and add `bodyNode.StartPoint().Row`. Where a single fact genuinely summarizes the whole function (return_shape aggregate, fingerprint), prefer the FUNCTION node's start line (`funcNode.StartPoint`), not the body's — but the precise per-hit extractors (config_read, call_order, concurrency) must use the hit line. This is structural and language-agnostic.

**2. `goReceiverType` · INTENT: pull the receiver type out of a Go method signature `func (r *T) M()` → `T` · BUG: it finds the receiver close-paren with `strings.IndexByte(s, ')')` — the FIRST `)` in the string. The signature is the truncated first line (`extractSignature`, 200-char cap). For a multi-line receiver, a receiver with a parenthesized type, or any signature whose first `)` is not the receiver's, this slices the wrong span.** Concretely: `func (s *Service[K, func() error]) Do()` — the first `)` closes the inner `func()`, so `s[len(pfx):end]` = `s *Service[K, func(` and `Fields(...)[last]` = `func(` → after `[`-strip → `func`. Receiver mis-typed → method never parents to its struct → the 58%-method-gap fix silently no-ops on exactly the generic-receiver methods. **LIPI: Logic (wrong delimiter chosen — first `)` ≠ receiver `)`; needs paren-depth matching).** · file:line `parser.go:197` `end := strings.IndexByte(s, ')')`. · **GENERALIZED fix:** walk from `len(pfx)` tracking paren depth (`(`/`)`, also `[`/`]`), take the `)` that returns depth to zero — the true receiver close. Pure string logic, language-agnostic within Go. (Comment-in-receiver, e.g. `func (r *T /*x*/) M()`, is the same class: `Fields` last token would be `*/` — depth-matching plus a comment strip closes it.)

**3. `extractCalleeInfo` literal-receiver guard · INTENT: drop method calls on literal receivers (`",".join()`, `[].append()`) so they don't become bogus internal `name_match` edges · BUG: the receiver is taken as `funcNode.Child(0)`, but for a CHAINED call the receiver child is itself a `call_expression`/`attribute`, not a literal — so `"x".strip().split()` has `funcNode = (…).split`, `Child(0) = "x".strip()` (a call node, not `string`), and the guard does NOT fire. The dominant garbage the comment cites (`join`, `split`) frequently appears chained.** Also: `isLiteralReceiver` checks only the immediate child's TYPE; a parenthesized literal `("a")` is `parenthesized_expression`, not in the set. **LIPI: Logic (guard tests only the depth-1 receiver, misses transitive/wrapped literal roots) — an INCONSISTENT-FILTER (c): the sibling direct-literal call is filtered, the chained-literal call is not, so they MISDIRECT differently.** · file:line `parser.go:927` `if recv := funcNode.Child(0); recv != nil && isLiteralReceiver(recv.Type())`. · **GENERALIZED fix:** resolve the receiver to its root by unwrapping `parenthesized_expression` and, for a chained call, taking the head of the chain; if the chain's ultimate base is a literal node type, treat it as builtin. Conservatively at minimum unwrap `parenthesized_expression`. Keep it type-set driven (language-agnostic).

**4. `extractAssignments` ViaReturn / constructor heuristic · INTENT: record `x = Foo()` → x:Foo and `x = factory()` → x via-return, for x.method() resolution · BUG: the capitalization test `simple[0] >= 'A' && <= 'Z'` is applied to ALL languages uniformly. In Go, exported funcs are Capitalized (`x := Marshal()` is NOT a constructor — it's a function returning `[]byte`); this stamps `TypeName="Marshal"` as if Marshal were a class, so the resolver later treats `x.foo()` as a method on a non-existent type `Marshal`. The PyCG capitalized-constructor heuristic is Python/JS-shaped, not Go-shaped.** **LIPI: Logic (heuristic mis-applied across languages — a false TYPE fact, exactly the wrong-fact pollution CLAUDE.md warns of).** · file:line `parser.go:750` `if len(simple) > 0 && simple[0] >= 'A' && simple[0] <= 'Z' { typeName = simple }`. · **GENERALIZED fix:** gate the capitalized-bare-name→constructor branch by language: for Go, a bare `Capitalized()` call should go down the ViaReturn path (bridge through the callee's return type), not be assumed a constructor; reserve the "capital = constructor" rule for Python/JS/TS. Per-language dispatch on `sf.Language`, no task/file logic.

**5. `_tryExtractSideEffect` · INTENT: detect `self.x = …` mutations · BUG: it operates on flattened node TEXT and finds the assignment `=` with `strings.Index(text, "=")` — the FIRST `=`. If the LHS contains a subscript with a string/expr that has `=` (e.g. `self.cache[k == v] = ...`) or the RHS appears first in some grammar's content ordering, the split is wrong. More concretely the `==`/`!=`/`<=`/`>=` guard only inspects the byte immediately after/before the first `=`; `self.x = (a == b)` is fine, but `self.d[", ="] = v` finds `=` inside the string literal first and bails or mis-splits.** **LIPI: Implementation (text-based `=` scan instead of using the assignment node's `left`/`right` fields that the grammar already provides).** · file:line `parser.go:2488` `eqIdx := strings.Index(text, "=")`. · **GENERALIZED fix:** use `node.ChildByFieldName("left")` / `"right"` (as `_walkFieldReads` already does at `:3165`) instead of string-splitting on `=`; fall back to text only when fields are absent. Removes literal-`=`-in-subscript false splits across all languages.

**6. `extractSignature` truncation feeds `goReceiverType` and Rust return-type checks · INTENT: a one-line signature · BUG: it hard-cuts at 200 bytes with no balance (`text[:200]`), unlike `clipBalanced` used elsewhere. A long generic Go receiver/param list whose receiver `)` sits past byte 200 yields a signature with no `)` at all → `goReceiverType` returns "" → method unparented.** Compounds #2. **LIPI: Implementation (inconsistent truncation — the codebase HAS `clipBalanced` for exactly this, but the signature path doesn't use it; the receiver-bearing prefix can be amputated).** · file:line `parser.go:981` `if len(text) > 200 { text = text[:200] }`. · **GENERALIZED fix:** for the receiver-extraction consumer, parse the receiver from the AST `receiver` field (Go grammar exposes `node.ChildByFieldName("receiver")` on `method_declaration`) rather than re-parsing a truncated signature string; at minimum raise/space-trim the signature so the receiver clause survives. AST-field based, language-agnostic.

**7. `extractDocstring` Strategy 1 vs 1a/1b ordering · INTENT: attach a doc comment to a function · BUG: Strategy 1 fires on `prevSibling.Type() == "comment"` and RETURNS; but for languages whose doc comment node is `block_comment`/`line_comment` (Rust `///` is `line_comment`, Java `/** */` is `block_comment`), Strategy 1 is skipped and 1b handles it — however 1a (parent's prev sibling) is gated on `prevSibling == nil || (type != "comment" && != "block_comment")`, which means when `prevSibling` is a `line_comment` (NOT in that exclusion list), 1a is SKIPPED, then 1b catches it. The three strategies have overlapping-but-not-identical type sets ("comment" vs "block_comment"/"line_comment"), so a `line_comment` immediately-preceding sibling is handled by 1b but a `line_comment` on the PARENT's prev sibling (TS `export` wrapping) falls through ALL of them.** **LIPI: Integration (two symmetric paths — direct-sibling vs parent-sibling — with different comment-type sets; the parent path omits `line_comment`).** · file:line `parser.go:1900` `prevSibling.Type() != "comment" && prevSibling.Type() != "block_comment"` (omits `line_comment`) and `:1904` parent check only accepts `comment`/`block_comment`. · **GENERALIZED fix:** define one `isCommentType(t)` helper (`comment`/`line_comment`/`block_comment`/`///`-variants) and use it in all three strategies and in the 1a gate, so the parent-sibling path accepts the same node types as the direct path.

**8. `extractGuardFromStmt` keyword scan over full `if` TEXT · INTENT: flag `if cond: return/raise` guards · BUG: `strings.Contains(text, kw)` is run over the ENTIRE if-statement content including its ELSE/nested bodies and any string literals. `if ok { log("return code") }` matches `"return"` inside the string → false guard. Worse, the `guardType` switch keys off `Contains(text,"raise ")` etc. over the same whole-if text, so a guard whose THEN-branch returns but whose ELSE-branch raises is misclassified.** **LIPI: Logic (substring match over un-scoped text, including string literals and the alternative branch).** · file:line `parser.go:2037-2047`. · **GENERALIZED fix:** restrict the keyword scan to the consequence node only (`ChildByFieldName("consequence")`/`"body"`), which the function already locates at `:2074` for `consequenceText` — scan THAT subtree's statement types (`return_statement`/`raise_statement`/…) rather than substring-matching the whole if. AST-typed, not text.

**9. `extractExceptionFromNode` `return_statement` → "error" · INTENT: treat Go `return fmt.Errorf(...)` as an exception_type · BUG: `strings.Contains(text, "errors.New")` etc. matches ANY return whose text contains those substrings, including `return errors.New` passed through a variable name or a return that merely *references* an error without raising (`return wrapIfErr(errors.New)`), and it fires on EVERY such return in the body (recurses, no dedup) → N duplicate `exception_type: error` properties on one function. Also a plain `return nil` after an `errors.New` assignment elsewhere isn't caught, so it's both over- and under-inclusive.** **LIPI: Logic (text-substring exception detection) + Implementation (no dedup → property spam).** · file:line `parser.go:2223` and `:2249`. · **GENERALIZED fix:** check that the return's VALUE expression is a call whose callee is `Errorf`/`New`/`Wrap` (via `extractCalleeInfo` on the return's child call node), and dedup identical `(kind,value)` per node before append.

**10. `classifyAssertion` `pytest.` and `t.` over-broad · INTENT: identify assertion calls · BUG: `strings.HasPrefix(lowerQual, "pytest.")` returns true for `pytest.fixture`, `pytest.mark.parametrize`, `pytest.skip` — none are assertions — flooding `Assertions` with non-assertions. Likewise `to`-prefix + contains `expect` matches `toString()` on anything in an expect chain.** **LIPI: Logic (prefix too broad; `pytest.` ≠ assertion).** · file:line `parser.go:3667` `strings.HasPrefix(lowerQual, "pytest.")` and `:3690` `HasPrefix(lowerSimple,"to") && Contains(lowerQual,"expect")`. · **GENERALIZED fix:** whitelist the assertion-bearing pytest calls (`pytest.raises`, `pytest.warns`, `pytest.approx`) instead of all `pytest.*`; for Jest, require the matcher to be a known `to*` matcher set or at least exclude `tostring`/`tolocale*`.

**11. `extractConfigReads` `settings.` / `config[` patterns · INTENT: detect config reads · BUG: `settings.` and `config.get(` / `config[` are matched as raw substrings anywhere in body text, including inside comments, strings, and unrelated identifiers like `mysettings.foo` (no left boundary check — unlike the security/boundary extractors which DO use `containsKeywordAtBoundary`). `update_settings.apply()` → emits `config: apply`.** **LIPI: Implementation (inconsistent filter — sibling extractors gate on word boundary, this one doesn't — bug class (c)).** · file:line `parser.go:4397` `settingsPrefix := "settings."` scanned via plain `strings.Index`. · **GENERALIZED fix:** apply the same left-boundary check used by `containsKeywordAtBoundary`/`_containsBoundaryLiteral` before accepting `settings.`/`config` hits (preceding char must not be alnum/`_`/`.`), and skip hits inside string/comment ranges where feasible.

**12. `_walkConditionalReturns` dedup-guard depends on `StartByte` equality across two lookups · INTENT: avoid double-emitting the elif found via both the `alternative` field and the child loop · BUG: the skip in the child loop only triggers when `child.StartByte() == altStartByte` AND `child.Type()` is in a fixed set (`elif_clause/else_clause/else/if_statement`). If a grammar names the alternative node `else_if`/`elsif`/`alternative` (Ruby `elsif`, others), the type isn't in the skip set → the alternative is walked twice → duplicate `conditional_return` properties.** **LIPI: Integration (the field-path and child-loop-path both process the same node; the de-dup set is hardcoded and grammar-specific).** · file:line `parser.go:2399-2403`. · **GENERALIZED fix:** skip purely on `child.StartByte() == altStartByte` (identity) regardless of type, since `altVisited` already proves that byte-range was handled via the field path.

**13. `linkRustImplMethods` "no children ⇒ canonical struct" heuristic · INTENT: pick the struct_item node (not an impl block) as the canonical parent · BUG: it decides a `Class` node is the canonical struct iff it has NO method children. A struct defined with a body of fields but written so methods land elsewhere is fine, but a *marker/empty* `impl Foo {}` (no methods) is ALSO childless → it gets registered into `structNodes[Foo]` and may shadow the real `struct_item` (first-write-wins at `:259`), so later impl blocks re-parent to an empty impl block instead of the struct.** **LIPI: Logic (proxy "hasChildren" conflates empty-impl with struct-def; node origin AST type is discarded — the comment at `:233` admits "we don't store the AST node type").** · file:line `parser.go:257` `if !hasChildren[idx1]`. · **GENERALIZED fix:** persist the originating AST node type (struct_item/enum_item vs impl_item) on the node at creation in `walkNode` (a label discriminator or a side map) and key canonicality on that, not on the child-count proxy.

**14. `extractSideEffects` augmented-assignment LHS strip · INTENT: strip `+=`,`-=`… to isolate the LHS field · BUG: at `:2502` it decrements `lhsEnd` only ONE char for the augmented op, but `text[:eqIdx]` was already computed with `eqIdx = first '='`. For `self.x //= 2` (no such op in Py, but `self.x **= 2` exists) or shift-assign `self.x <<= 1`, the operator is 2 chars before `=`; stripping one leaves `<` glued to the field → field name corrupted. Not the dominant case, but a wrong-fact.** **LIPI: Implementation (single-char op strip assumes 1-char augmented operators).** · file:line `parser.go:2502`. · **GENERALIZED fix:** use the AST `left` field (same as #5) rather than text arithmetic; the field gives the exact LHS node regardless of operator width.

---

Functions audited and judged CLEAN (no high-confidence finding): `ParseFile`, `linkGoReceiverMethods` (logic correct given `goReceiverType` is fixed), `walkNode` core dispatch, `extractCalls`/`extractCallsWithParent`, `classifyCallContext`, `extractFieldText`, `extractFirstIdentifier`, `clipBalanced` (well-constructed, balance-aware), `isWordByte`, `stripTrailingOp`, `extractReturnShape`/`countReturns` (value logic fine; only the Line is wrong — covered in #1), `rustTailExpr`, `_findReturnsInBlock`, `_walkSideEffects`, `extractStructuredParams` (uses per-`param` line correctly at `:2731` — good), `containsKeywordAtBoundary`, `extractSecurityTags`, `_walkExceptionFlow`/`_findRaisesInBlock`, `extractExceptionHandlers`, `_collectCallNames`, `_walkFieldReads` (correctly uses `left`/`right` fields), `_walkBoundaryConditions`, `_containsBoundaryLiteral`, `extractClassFields`, `extractClassDecorators`, `findAssertions` (arg-skip logic correct), all import extractors (`extractPythonImports`…`extractLuaImports`), `_walkResourcePatterns`, `extractVisibility`, and the `last*Component`/`stripQuotes` helpers.

Note on scope: most property extractors here emit to `result.Properties` which the indexer writes to graph.db `properties`; whether each property reaches an agent OBSERVATION is a consumer-side (post_edit/L3) question outside this file — but the **Line field is consumed downstream for snippet anchoring and precedent joins**, which is why #1 is the highest-leverage finding in this file: it is a systematic wrong-line attribution across six extractors, the WRONG-FACT class proven in this codebase.

Highest-confidence, highest-harm: **#1 (systematic Line mis-attribution), #2 (goReceiverType first-`)` — directly breaks the 58%-method-gap parenting on generic receivers), #4 (Go capitalized-call mis-typed as constructor — pollutes the type map the method resolver reads).** I did not edit anything.

---

I have everything I need. The confidence table in `computeConfidence` (resolver.go:303-311) exactly matches the incremental restore's name_match table (incremental.go:136-149), confirming intentional mirroring. The critical divergence is the `qualifiedUnresolved` demotion (resolver.go:721-758) which the incremental path has NO equivalent of. Let me do one final confirmation: verify the snapshot SELECT does not pull `qualified_name`, and check the schema column count vs the incremental edge INSERT to confirm the `metadata` NULL positioning is correct.

I have all the evidence. Here is the granular LIPI review.

---

# GRANULAR LIPI REVIEW — `gt-index/internal/store/sqlite.go` (+ its sibling `incremental.go`, per focus note)

Method: function-by-function, in order. Clean functions skipped. Column-ordering and the incremental snapshot/restore path are the focus. The two snapshot/restore findings are the load-bearing ones; the rest are real but lower-harm.

---

### 1. `ResolveIncomingEdgesTx` (incremental.go:82) · INTENT: re-attach cross-file callers to freshly-reinserted target nodes after a `-file` reindex, by name lookup. · BUG: **QUALIFIED-STDLIB RE-LAUNDER on the incremental path** — the snapshot/restore has NO equivalent of the resolver's `qualifiedUnresolved` demotion, so a single-candidate match whose original method was `import`/`same_file` is restamped `CERTIFIED conf≥1.0` even when the call was a qualified stdlib-shadow (`os.walk` → project `walk`). · LIPI: **Integration** (two symmetric resolution paths; the full-index path has the deterministic P0 gate, the incremental twin does not — the exact "INCONSISTENT FILTER" class). · Evidence:
  - incremental.go:127 `if len(ids) == 1 && (r.ResolutionMethod == "same_file" || r.ResolutionMethod == "import") { conf = r.Confidence … method = r.ResolutionMethod; tier = "CERTIFIED" }`
  - vs the full path's guard, resolver.go:721/742-747: `qualifiedUnresolved := call.CalleeQualified != "" && call.CalleeQualified != calleeName` … `if qualifiedUnresolved { method = "name_match"; … tier = "SPECULATIVE"; evidence = "name_match_qualified_unresolved" }`.
  - The snapshot SELECT (incremental.go:51) never reads the target's `qualified_name`, so `ResolveIncomingEdgesTx` is *structurally incapable* of knowing the call was qualified-unresolved. This is the P0 stdlib-shadow laundering (CLAUDE.md `55ab30eb`/`c7e7e5d0`) reopened on the `-file` path. After any incremental reindex of the target file, every restored cross-file caller into it loses the demotion. · GENERALIZED fix: carry `n.qualified_name` (and ideally the original `evidence_type`) into `IncomingEdgeRef` in the snapshot SELECT; in the restore's unambiguous branch, if the snapshot's resolution carried a qualified-unresolved marker (or the restored edge's stored callee-qualifier ≠ target name), demote to `name_match`/`SPECULATIVE`/`name_match_qualified_unresolved` exactly as resolver.go:743-747 does. No file/task-specific logic — it is the same structural predicate.

### 2. `ResolveIncomingEdgesTx` (incremental.go:124-133) · INTENT: preserve a genuinely-verified caller's high confidence across reindex. · BUG: **STALE-CONFIDENCE FLOOR-TO-1.0 RESTORE** — when `len(ids)==1` and method was `import`/`same_file`, it copies the *old* snapshot confidence and, if `<0.5`, hard-sets `conf = 1.0`. A pre-v14 edge stored at `0.0`, OR an edge the LSP/resolver had *deliberately demoted* to a sub-0.5 confidence, is silently promoted back to `1.0 CERTIFIED`. · LIPI: **Logic** (wrong threshold/override — the `<0.5 → 1.0` rule assumes "low conf == pre-v14 default", but it also catches *intentionally-low* confidences and re-certifies them). · Evidence: incremental.go:129-131 `if conf < 0.5 { conf = 1.0 // pre-v14 databases have 0.0 default; restore to verified }`. The comment only justifies the `0.0` case; the code fires for *every* value in `(0,0.5)`. Combined with finding #1, a demoted stdlib-shadow caller that was correctly sitting at `0.2`/`0.4` gets snapped to `1.0 CERTIFIED` on reindex. · GENERALIZED fix: only floor the literal `0.0`/NULL pre-v14 sentinel to a *method-appropriate* value via `computeConfidence(method, len(ids))` (not a blanket `1.0`); for any stored `conf` already `>0`, preserve it verbatim. Never re-certify a confidence the pipeline previously lowered.

### 3. `ResolveIncomingEdgesTx` (incremental.go:163-164) · INTENT: insert the restored edge. · BUG: **`verification_status` regression to `'unverified'`** — the INSERT hardcodes `verification_status` to `'unverified'` (incremental.go:94) for *every* restored edge, even ones it just stamped `CERTIFIED`. An edge the LSP resolve pass had marked `verified` loses that status after an incremental reindex of its target file. · LIPI: **Plumbing** (a column carried by the full schema is dropped/reset on the restore path; data computed upstream never survives to the rebuilt row). · Evidence: incremental.go:94 `VALUES (?, …, 'unverified')`; the snapshot SELECT (line 51) does not read `verification_status` either, so it cannot be preserved. · GENERALIZED fix: select `verification_status` in the snapshot and round-trip it; or, if not preserving, never pair `tier="CERTIFIED"` with `verification_status="unverified"` (internally contradictory tiering). Low harm today only because `CERTIFIED` here is itself suspect per #1.

### 4. `SnapshotIncomingEdgesTx` (incremental.go:46) · INTENT: capture cross-file callers into the file before deleting it, to restore them. · BUG: **SILENT TRUNCATION at `cap` with no signal** — the snapshot `LIMIT ?` (default 50,000) drops incoming edges beyond the cap *silently*; those callers are then deleted by `DeleteFileEdgesAndNodesTx` and never restored, with no count returned and no warning. On a hub file (the 58%-method-call reality from CLAUDE.md, where one file can be the target of >50k edges) the file silently loses inbound connectivity after a reindex. · LIPI: **Implementation** (silent dead-data path; the cap is a correctness-affecting truncation disguised as a "defensive bound"). · Evidence: incremental.go:57 `LIMIT ?`; the function returns only `[]IncomingEdgeRef` with no truncation flag, and the doc calls cap "a defensive upper bound" (line 45) without noting the data loss. · GENERALIZED fix: detect `len(out) == cap` and return/log a truncation indicator so the caller can fall back to a full reindex of affected sources, or raise the cap to be unbounded for the snapshot (it is a transient in-tx slice, not persisted). Structural; no task logic.

### 5. `BatchInsertAssertions` vs `InsertAssertion` (sqlite.go:651 vs 614) · INTENT: persist test assertions with their resolution score. · BUG: **INCONSISTENT COLUMN SET between the single-row and batch inserters** — `BatchInsertAssertions` (and `BatchInsertAssertionsTx`, incremental.go:401) inserts the `resolution_score` column; `InsertAssertion` (sqlite.go:616) **omits `resolution_score`** entirely, so any assertion written through the single-row path silently gets `resolution_score = 0.0` (the schema default, line 215). · LIPI: **Plumbing** (a field computed in the `Assertion` struct — `ResolutionScore`, the "multi-signal score that produced the link", line 79 — is never delivered for one of two write paths; the column is in the struct and the batch SQL but missing from the single SQL). · Evidence: sqlite.go:616 `INSERT INTO assertions (test_node_id, target_node_id, kind, expression, expected, line)` — no `resolution_score`; contrast sqlite.go:660 which includes it. · GENERALIZED fix: add `resolution_score` to `InsertAssertion`'s column list and bind `a.ResolutionScore`, matching the batch path. (Verify call-sites: if `InsertAssertion` is only ever used where score is unset, it is latent; still a contract bug.)

### 6. `GetAllNodes` (incremental.go:254) · INTENT: rebuild the resolver's name/file indexes during incremental reindex. · BUG: **PARTIAL SCAN — `qualified_name`, `signature`, `parent_id`, `start_line/end_line` NOT selected**, so every `Node` rebuilt for the in-memory resolver index has empty `QualifiedName`/`Signature`/`ParentID=0`. The resolver's qualified-call and self/super (`parent_id`-driven, CHA) strategies — resolver.go:601-611 (`call.CalleeQualified`), 1.75/1.94 — operate on a graph where parented receiver methods and qualifiers are blank. · LIPI: **Plumbing** (wrong/insufficient columns SELECTed; the rebuilt node set is a degraded copy of the persisted one, so incremental re-resolution runs against a lobotomized index — directly enabling finding #1's re-launder, since `CalleeQualified` can't be reconstructed). · Evidence: incremental.go:256 `SELECT id, label, name, file_path, language, is_test FROM nodes` — 6 of 13 columns. · GENERALIZED fix: select the columns the resolver's strategies consume (`qualified_name`, `signature`, `parent_id`) so incremental resolution has parity with full resolution. This is the root enabler of the qualified-unresolved gap on the `-file` path.

### 7. `PopulateFTS5` DROP+recreate recovery (sqlite.go:305-317) · INTENT: self-heal a corrupt external-content FTS5 index. · BUG: **the recreated vtable can desync from a binary built WITHOUT the `sqlite_fts5` tag** — `CREATE VIRTUAL TABLE … USING fts5` at line 309 will *error* on a no-FTS5 build, but that error path returns `fmt.Errorf` (line 313), turning a non-fatal degradation into a hard `PopulateFTS5` failure; whereas the *initial* create (createSchema:269) treats the same condition as non-fatal (`log.Printf("[WARN] …")`). · LIPI: **Integration** (two symmetric "create fts5" sites with opposite fatality contracts — one warns-and-continues, the recovery twin hard-errors). · Evidence: createSchema:269-271 logs and returns nil on `ftsErr`; PopulateFTS5:312-314 returns the error. A graph.db that *had* an FTS5 table (from a tagged build) opened by an *untagged* binary on the recovery branch will now fail the whole populate. · GENERALIZED fix: mirror createSchema's non-fatal contract in the recovery branch — if the CREATE VIRTUAL TABLE fails, log `[WARN]` and return nil (the Python localizer's name-match fallback already covers absence). Keep both FTS5-create sites under one fatality policy.

---

## Summary of severity

- **#1 + #2 + #6 are one compound P0 on the incremental (`-file`) path**: `GetAllNodes` strips `qualified_name`/`parent_id` (#6) → the resolver can't recompute `qualifiedUnresolved` → `ResolveIncomingEdgesTx` re-stamps qualified stdlib-shadow callers as `CERTIFIED` (#1) and floors any sub-0.5 confidence to `1.0` (#2). This is the exact stdlib-shadow laundering CLAUDE.md records as "CLOSED end-to-end" (`55ab30eb`) — **but it is closed only on the full-index path; the incremental path re-opens it.** Because `-file` reindex is the documented incremental/Bazel-pattern hot path, any LSP-resolve-then-reindex cycle (the `-rebuild-closure`/resolve flow) re-launders.
- **#3** is an internal-contradiction (CERTIFIED + unverified) on the same restore rows.
- **#4** silently severs hub inbound edges past 50k.
- **#5** drops `resolution_score` on the single-row assertion writer.
- **#7** is an inconsistent fatality contract between the two FTS5-create sites.

All fixes are structural (column parity, predicate parity, threshold correction, fatality parity) — no benchmark/task/file-specific logic, satisfying Pillar 1.

Relevant files: `D:\Groundtruth\gt-index\internal\store\incremental.go` (findings 1-4, 6), `D:\Groundtruth\gt-index\internal\store\sqlite.go` (findings 5, 7), with the parity reference at `D:\Groundtruth\gt-index\internal\resolver\resolver.go:721-758` (the `qualifiedUnresolved` demotion the incremental path lacks) and `:293-313` (`computeConfidence`, the table the restore mirrors).