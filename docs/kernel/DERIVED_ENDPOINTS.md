# Derived-table endpoints (`gt_*`)

Module location: `src/groundtruth/mcp/endpoints/` (sync cores + async handlers), `src/groundtruth/mcp/composite.py` (`gt_*_impl` wrappers), `src/groundtruth/mcp/composite_server.py` (FastMCP transport), `src/groundtruth/mcp/tools.py` (`handle_gt_*` registry wrappers + `groundtruth_do` explicit steps). Shared read helpers live in `endpoints/_graph_db.py`.

Shipped in `fceae84c` (ruff-format pass `df9b983b`). Tests: `tests/unit/test_gt_derived_endpoints.py` seeds a producer-verbatim DDL graph.db and asserts the typed results on both the store-backed and composite-impl paths.

Six read-only surfaces over the Go indexer's graph.db **derived schema** — `closure`, `processes`/`process_steps`, `communities`/`community_members`, `HANDLES_ROUTE`/`API_CALL` edges, `resolution_symbols` — plus `nodes`/`edges` and, for `gt_detect_changes`, the git working-tree diff. Nothing is inferred at read time: when the store or the table a surface needs is absent, the endpoint returns a typed abstention rather than fabricating rows.

## Registration

Three exposure paths over the same sync cores:

- **Tools registry** — `handle_gt_*` in `tools.py`, also reachable as `groundtruth_do` explicit steps (`trace_path`, `detect_changes`, `route_map`, `api_impact`, `closure`, `community`). The registry wrapper attaches a `reasoning_guidance` string to the result.
- **Composite impls** — `gt_*_impl` in `composite.py` serialize the typed result inside `<gt-evidence tool="...">` JSON (the `server.py` `gt_replan` serialization precedent). Uncapped: read-only graph lookups, not gt_intel evidence pulls.
- **Composite FastMCP server** — `composite_server.py` registers the six beside `gt_lookup` / `gt_impact` / `gt_check`.

Every handler takes `(store, graph, root_path)` and resolves its connection through `_graph_db.store_connection` (the store's sqlite3 handle). A store not backed by graph.db gets a typed `unavailable`, never an exception.

## Shared contract

- `status` is the verdict field. `"ok"` is a real answer. Typed non-ok values: `"unavailable"` (with `reason`) when the graph or the required derived table is absent; `"not_found"` when a symbol binds no node; `"ambiguous"` (with `candidates`, `candidates_truncated`) when a name binds two or more distinct nodes — never a silent first-match. `"no_path"` (gt_trace) and `"not_found"` on a filtered gt_api_impact are real answers, not abstentions.
- Symbol resolution is shared (`_graph_db.resolve_symbol`): a name matches `nodes.name` OR `nodes.qualified_name`; candidates cap at 10.
- Every returned list is bounded and every result carries `truncated` — no unbounded dumps.
- Missing facts stay missing: stored NULLs stay `null` (community `cohesion`), an unreadable route decorator renders as the typed name `"unknown"`, an uncomputable risk level is `"unknown"`. A missing value is never rendered as `0` or a guess.
- `gt_detect_changes` additionally carries `degraded` (names of the stages that could not run) and `partial` (some diffed files mapped to no indexed symbol).
- Stable-id joins follow the producer's own convention: `nodes.stable_id` when stamped, else `resolution_symbols.stable_id` on `native_id = nodes.id` — never invented identifiers.

## `gt_trace` — directed path between two symbols

Question: "Is there a path from this symbol to that one, and through what?"

**Inputs:** `from_symbol`, `to_symbol` (required). `max_depth` (default 6) is a wire parameter; `max_expansions` (default 500) is an internal bound.

**Traversal:** bounded BFS from `from_symbol`, forward along `edges` of type `CALLS` (and `HAS_METHOD`, which the producer does not mint today — listed so the surface picks it up unchanged if it ever lands). Containment is recovered from `nodes.parent_id`: the class→method hop is labelled `HAS_METHOD` at confidence 1.0, the inverse method→class hop `MEMBER_OF` at 1.0 — structural facts, not inferred.

**Bounds:** depth ≤ 6, node expansions ≤ 500.

**Output:**

```json
{"status": "ok", "from": {...}, "to": {...},
 "path": [{"symbol", "file", "line", "relation", "confidence"}, ...],
 "hops": 2, "truncated": false}
```

- `from`/`to` resolve independently; a failure returns `{"status": "not_found"|"ambiguous", "endpoint": "from"|"to", "symbol", "candidates"?}` per endpoint.
- `no_path` returns `{"status": "no_path", "from", "to", "path": [], "explored": <visited count>, "truncated"}`. `truncated` is set when the expansion budget was hit **or** the depth bound cut off unexplored neighbours — a bound-limited `no_path` is flagged, not claimed as proven exhaustion.
- `from == to` resolves to the same node → `ok` with a single-step path, `hops: 0`.
- `unavailable` reason: `graph_tables_absent` (`graph_db_missing` on the composite-impl path).

**Reads:** `nodes`, `edges`.

## `gt_detect_changes` — what breaks if I commit this

Question: "Which symbols does this diff touch, and which witnessed flows cross them?" Run before committing, with the working-tree diff (or a passed diff) in hand.

**Inputs:** optional `diff` — a unified diff. Default: `git diff HEAD` of the working tree, falling back to `git diff` + `git diff --cached` on unborn-HEAD repos (15s timeout per call). A successful empty diff is a real "nothing changed", not a failure.

**Pipeline:** each `@@` hunk's new-side range → `nodes` symbol ranges (`Function`/`Method`/`Class`, overlap test) → stable ids → `process_steps` → `processes`. Every affected process is a test-witnessed interprocedural slice and carries its witness (`witnessed_by` test + `witness_assertion_id`), so "affected" means "a witnessed flow crosses the edit", never "looks related".

**Bounds:** 50 changed symbols, 25 affected processes, 20 unmapped files.

**Output:**

```json
{"changed_count": 3, "affected_count": 1, "risk_level": "moderate",
 "changed_symbols": [{"name", "qualified_name", "file", "line", "kind", "callers"}, ...],
 "affected_processes": [{"id", "entry", "terminal", "witnessed_by",
   "witness_assertion_id", "kind", "depth", "trust_floor", "via": [...]}, ...],
 "partial": false, "truncated": false, "unmapped_files": [], "degraded": []}
```

- `callers` per changed symbol = incoming-edge count at the ≥0.5 verified-reach floor.
- `risk_level`: `"high"` when a witnessed process crosses the change or any changed symbol is a hub (≥10 verified callers); `"moderate"` when symbols mapped but no witnessed flow; `"low"` when a parsed diff touched no indexed symbol; `"unknown"` on any failure — never a guessed level.
- `degraded` names the stage that could not run: `graph_tables_absent` / `graph_db_missing`, `git_diff_unavailable`, `diff_parse_failed` (non-empty input with no diff structure), `processes` (the `processes`/`process_steps` tables are absent — `affected_processes` stays `[]`).
- `partial` is true when some diffed files mapped to no indexed symbol; those paths land in `unmapped_files`.

**Reads:** `nodes`, `edges`, `resolution_symbols`, `process_steps`, `processes`, plus the git diff.

## `gt_route_map` — the service-boundary surface

Question: "What routes does this repo serve, who calls them, and what does each handler do next?"

**Inputs:** none.

**Sources (all producer-emitted, never inferred):**

- `HANDLES_ROUTE` edges — handler function node → the file's anchor node, `source_line` pointing at the decorator/registration line. The edge stores no path by design; path + method are recovered by re-reading that one line through the producer's own patterns (`_graph_db.parse_route_line`: Python `@app.get`/`@router.post`/`@app.route`, Java/Kotlin `@*Mapping`, NestJS `@Get`/`@Post`, JS/TS `app.get`/`router.post`, Go `HandleFunc`/`Handle`/`GET`…). An unreadable or unparseable line renders the typed name `"unknown"`.
- `API_CALL` edges — client-call file anchor → route file anchor, `metadata` carrying `route`/`method`/`framework` verbatim. A route known only through `API_CALL` metadata surfaces with `discovered_via="api_call"` and `handler=None`.
- `middleware` is always `[]` — the producer stores no middleware facts, so the field is emitted empty rather than guessed.
- `flows` — the handler's direct outgoing `CALLS` targets (what the route invokes next).

**Bounds:** 50 routes, 5 flows per handler, 25 consumers per route.

**Output:**

```json
{"status": "ok",
 "routes": [{"name", "method", "handler", "handler_file", "handler_line",
   "middleware": [], "confidence", "discovered_via": "handles_route"|"api_call",
   "consumers": [{"file", "line", "route", "method", "confidence",
     "attribution": "route_level"|"file_level"}, ...],
   "flows": [{"symbol", "file", "confidence"}, ...]}, ...],
 "truncated": false}
```

- Consumer `attribution` is `route_level` only when the call's metadata route equals the route's parsed name; otherwise `file_level`. A file-granularity hit is never silently upgraded to a route-granularity one.
- `unavailable` reason: `graph_tables_absent` / `graph_db_missing`.

**Reads:** `edges` (`HANDLES_ROUTE`, `API_CALL`), `nodes`, plus one source line per route re-read from disk.

## `gt_api_impact` — route map + consumer-key analysis

Question: "If this route's behaviour changes, whose traffic is actually affected?"

**Inputs:** optional `route` (normalized through `normalize_route_path` — strips scheme+host, query, fragment, and declared parameter segments `{id}`/`:`/`<>`; concrete literals are kept, so a literal path matches only itself), optional `handler` (exact handler symbol name).

**Adds to the route_map record:** per consumer, `routes_called` — the count of DISTINCT routes that consumer node calls. A consumer fetching ≥2 routes is a multi-fetch consumer and carries `attributionNote` ("impact is shared, not exclusive to this route"). Per route: `consumer_count` and `affected_files` (sorted consumer files).

**Output:**

```json
{"status": "ok", "route": "/users", "handler": null,
 "routes": [{"...route_map fields...",
   "consumers": [{"file", "line", "route", "method", "confidence",
     "attribution", "routes_called": 2, "attributionNote": "multi-fetch consumer: ..."}, ...],
   "consumer_count": 1, "affected_files": ["src/client.py"]}],
 "truncated": false}
```

- A filter that matches nothing returns `{"status": "not_found", "route", "handler", "routes": []}` — a real answer, not an abstention.
- Same bounds and reads as `gt_route_map`.

## `gt_closure` — the precomputed transitive-reach sidecar

Question: "What transitively calls this symbol, and what does it transitively call?" — answered by the `closure` table the producer publishes (C7/RF-4), not by a live traversal.

A `closure` row `(source_id, target_id, depth, min_confidence)` means `source_id` reaches `target_id` in `depth` verified-CALLS hops with a weakest-edge confidence of `min_confidence`. For a symbol, `callers` are rows with `target_id` = the symbol; `callees` are rows with `source_id` = the symbol. Each direction reports the shortest depth per neighbour.

**Inputs:** `symbol` (resolved through the shared resolver). Internal bound: 50 rows per direction.

**Producer bounds are stated, not re-applied.** The table is built at `max_depth=3` over verified edges at `min_confidence ≥ 0.5`; the response echoes them in `bounds` so a reader knows the window the answer covers.

**Output:**

```json
{"status": "ok", "symbol": {...},
 "callers": [{"symbol", "qualified_name", "file", "line", "depth", "min_confidence"}, ...],
 "callees": [...], "stale": false,
 "bounds": {"max_depth": 3, "min_confidence": 0.5}, "truncated": false}
```

**Abstention / staleness:** a pre-C7 graph has no `closure` table → `unavailable` with `reason: "closure_table_absent"` (and the resolved node echoed). A stale closure — a post-incremental partial DROP, detected by the same two-signal probe `ImportGraph._closure_is_fresh` uses — is still reported but flagged `stale: true`: the rows are real, just provably incomplete. `stale` is `null` when determinability cannot be established.

**Reads:** `closure`, `nodes` (`edges` participates in the presence gate).

## `gt_community` — the already-computed community decomposition

Question: "What are the cohesive regions of this codebase?"

Reads the producer's `communities`/`community_members` tables verbatim. The community pass is opt-in and may not have run on a given graph — its absence is a typed abstention, not an empty answer.

**Inputs:** optional `name` (matches `label` or `heuristic_label`, exact or `LIKE %name%`), optional `member` (exact `community_members.member`). Internal bound: 20 communities, 5 member previews each.

**Output:**

```json
{"status": "ok",
 "communities": [{"id", "name", "heuristic_label", "cohesion",
   "cohesion_reason", "member_count", "top_members": [...],
   "keywords": [...], "description"}, ...],
 "truncated": false}
```

- `name` = `label` when present (LLM-enriched), else the `heuristic_label`.
- `cohesion` is the measured edge-internal ratio. The producer stores SQL NULL when cohesion is unmeasurable (zero internal weight, NaN); the surface preserves `null` and reports `cohesion_reason`. Unmeasurable is not zero.
- Ordering is `member_count DESC, id ASC`; `top_members` are members ASC.

**Abstention:** no `communities` table → `unavailable` (`communities_table_absent`, or `graph_tables_absent` when there is no connection). A `member` filter with no `community_members` table → `unavailable` with `community_members_table_absent` — a filter that cannot be applied must not silently widen to "all communities" and fabricate membership.

**Reads:** `communities`, `community_members`.
