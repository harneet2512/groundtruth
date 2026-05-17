# Runtime Parity Audit — FINAL_ARCH_V2

Status: **first-pass audit, blockers identified, not yet remediated.**
Date: 2026-05-17
Branch: `jedi__branch`
Scope: local dev (Windows) vs Google VM (`gt-v1`) vs GitHub Actions (`swebench_30task.yml`).

## How to read this file

Every claim must be backed by a file/line/artifact citation. Anything not cited is **NOT PROVEN** and must be re-investigated. Internal tests are admission gates only — they do not appear here as evidence of correctness.

## Claim ledger

| Claim | File | Lines | Exact quote / artifact | Why it matters |
|---|---|---:|---|---|
| Go source defines `trust_tier`/`candidate_count`/`evidence_type`/`verification_status` on `edges` table | `gt-index/internal/store/sqlite.go` | 136–139 | `trust_tier TEXT DEFAULT 'SPECULATIVE',\ncandidate_count INTEGER DEFAULT 1,\nevidence_type TEXT,\nverification_status TEXT DEFAULT 'unverified'` | Defines the schema the GHA pre-flight depends on |
| Go source declares indices on `trust_tier` | `gt-index/internal/store/sqlite.go` | 167–168 | `idx_edges_trust_tier`, `idx_edges_target_tier` | Confirms schema is current Go-source intent |
| Deployed graph.db lacks `trust_tier` columns | `.tmp_holdout/bugs/axum-3661/graph.db` | n/a (inspected via PRAGMA) | edges cols = `['id','source_id','target_id','type','source_line','source_file','resolution_method','confidence','metadata']` | The agent on every machine where this DB lives is running against the OLD schema |
| GHA pre-flight asserts the NEW schema | `.github/workflows/swebench_30task.yml` | 127 | `expected={'trust_tier','candidate_count','evidence_type','verification_status'}; missing=expected-set(cols); … 'FAIL: stale binary, missing: {missing}'` | Any agent run on GHA with a stale binary will hard-fail at CHECK 2 |
| Pip cache key does not include Go-source hash | `.github/actions/setup-eval/action.yml` | 27 | `key: eval-env-oh054-pip-${{ hashFiles('pyproject.toml') }}` | Python deps cached on Python deps only — fine for Python, but `/tmp/gt-index` is downstream of Go-source changes and needs its own gate |
| gt-index rebuild gate uses Go-source hash | `.github/actions/setup-eval/action.yml` | 54 | `GT_SRC_HASH=$(find $GITHUB_WORKSPACE/gt-index -name '*.go' -exec sha256sum {} + \| sort \| sha256sum \| cut -c1-12)` | Rebuild trigger is correct in principle |
| `/tmp/gt-index` and `/tmp/gt-index.sha` are NOT in the cache path list | `.github/actions/setup-eval/action.yml` | 21–26 | cache path is `~/.cache/pip`, `/tmp/OpenHands`, `/tmp/SWE-bench-Live`, `/opt/hostedtoolcache/Python/...` — no `/tmp/gt-index` | `EXISTING_HASH` read at line 56 will always be empty on a fresh runner → rebuild happens every run. Side-effect: the rebuild gate is moot on GHA, the build always runs from current Go source. Pre-flight CHECK 2 will therefore PASS on GHA. The mismatch surfaces wherever a pre-built or carried-over graph.db is reused (local dev, VM `.tmp_holdout/`, manually-shipped artifacts). |
| GHA workflow does NOT set `GT_ROUTER_V2` | `.github/workflows/swebench_30task.yml` | 140–160 | env block lists GT_REBUILD_L1, GT_REBUILD_L3, GT_REBUILD_L3B, GT_REBUILD_L5, GT_LAYER_EVENTS, GT_STRUCTURED_EVENTS, GT_STRUCTURAL_NEXT_ACTION, GT_L3B_PRIMARY_EDGE, GT_L5_STRUCTURAL_UNVERIFIED, GT_L5_GOKU_EVENTS, GT_DEEP_LAYER_GROUNDED_METRICS, GT_L5B_SAFETY_REQUIRED, GT_LSP_VERIFY — no `GT_ROUTER_V2` | Every GHA run today is **router-off**, regardless of what the V2 implementation does |
| `GT_ROUTER_V2` is currently boolean | `scripts/swebench/oh_gt_full_wrapper.py` | 1464–1473 | `def _router_v2_enabled() -> bool: … return os.environ.get("GT_ROUTER_V2","0") == "1"` | Required Track-B refactor to off/shadow/live not yet present |
| Router is shadow-only by construction | `scripts/swebench/oh_gt_full_wrapper.py` | 1467–1471, 1507–1512 | "The legacy graph_navigation / generate_improved_evidence paths are NOT replaced — this is shadow mode." / "Logs the result to gt_interactions but does NOT mutate the observation — activating agent-visible emission is a *separate* future change gated on paired-replay parity." | Confirms there is no agent-visible emission path even when the flag is on |
| Wrapper invokes router AND legacy path in same event | `scripts/swebench/oh_gt_full_wrapper.py` | 2233–2240 | `if event.kind == "post_view": … _router_v2_on_view(config, event.path) … hook_out = _run_internal(orig_run_action, make_view_hook_command(event, config), 30)` | When live mode is implemented, the legacy `make_view_hook_command` path must be skipped for the same event — currently both will fire |
| Wrapper invokes router AND legacy path in post_edit too | `scripts/swebench/oh_gt_full_wrapper.py` | 2425 (cited from prior session search) | "FINAL_ARCH_V2 shadow router (default OFF; opt-in via GT_ROUTER_V2=1)." | Same double-injection risk on edit |
| Binary provenance is stamped at build | `scripts/swebench/build_gt_index_linux.sh` | (from prior session) | -ldflags `-X main.commitSHA=…`, `-X main.buildTimeUTC=…`, `-X main.goToolchain=…` | The Go binary can self-report its commit and build time — but graph.db files do not carry this metadata, so consumers cannot verify which gt-index produced a given DB |
| graph.db has no `project_meta` provenance row for gt-index commit | `.tmp_holdout/bugs/axum-3661/graph.db` | inspected | `project_meta` table exists in schema (sqlite.go:149–152) but no `gt_index_commit` / `gt_index_build` row is written by the Go indexer | A stale graph.db is indistinguishable from a current one at read time — no way to fail fast on schema drift |
| Pre-flight CHECK 2 only validates a probe DB, not the agent's actual DB | `.github/workflows/swebench_30task.yml` | 125–127 | `mkdir -p /tmp/preflight && echo 'def hello(): pass' > /tmp/preflight/test.py; /tmp/gt-index -root /tmp/preflight -output /tmp/preflight/test.db …` | Confirms binary can produce new schema. Does NOT confirm the per-task DB used by the wrapper has the new schema — the wrapper rebuilds per-task DBs at runtime, but if any path serves a pre-built artifact, it bypasses this check |

## Parity-break summary

### B-1: graph.db schema drift (high severity, localized impact)
- **Source of drift**: Go-source schema in `sqlite.go:126–140` defines four columns that none of the currently-deployed `.tmp_holdout/bugs/*/graph.db` files have.
- **Why it didn't blow up yet**: GHA cache layout (`/tmp/gt-index` not cached) means GHA always rebuilds the binary from current `jedi__branch` Go source. The per-task DB is also re-built at runtime by the wrapper. Local dev and the VM, however, can ship/reuse stale DBs from `.tmp_holdout/`, `.tmp_run_20_baseline/`, `.tmp_diag_artifacts/`.
- **What this poisons**: Any code path that reads `edges.trust_tier` against an old DB will silently return no rows (column absent), pretending there's no trusted edge data. This affects any future Layer-4 provider that consumes `trust_tier`.
- **Fix needed**:
  1. Write a `gt_index_schema_version` row to `project_meta` from the Go indexer at write-time (stamped from a Go constant).
  2. Add a `verify_graph_schema(db_path, required_columns)` helper used by the wrapper and shadow_replay both — fail fast.
  3. Pre-flight CHECK 2 already validates a probe DB; add a second check that runs against an existing artifact when `GT_REUSE_GRAPH_DB` is set.

### B-2: GHA workflow does not set `GT_ROUTER_V2`
- **Direct evidence**: `swebench_30task.yml:140–160` lists every GT_* env var; `GT_ROUTER_V2` is absent.
- **Consequence**: Every GHA-launched paired run today runs the **OLD_GT** arm regardless of implementation work on the V2 router. The 3-arm canary cannot be filled from GHA without changing this file.
- **Fix needed**: After Track B (off/shadow/live refactor), add `GT_ROUTER_V2: "shadow"` (or `"live"` for the V2 arm). Make this a workflow input so the 3 arms can be dispatched cleanly.

### B-3: GT_ROUTER_V2 is binary, not tri-state
- **Direct evidence**: `oh_gt_full_wrapper.py:1473` — `return os.environ.get("GT_ROUTER_V2","0") == "1"`.
- **Consequence**: Today the flag can only express "shadow-on" vs "shadow-off". There is no "live" value that swaps the legacy path off. The session directive requires off/shadow/live and the live arm cannot exist until this refactor lands.
- **Fix needed**:
  - Replace `_router_v2_enabled() -> bool` with `_router_v2_mode() -> Literal["off","shadow","live"]`.
  - In live mode at `post_view` (line 2233) and `post_edit` (line 2425): emit ONLY from the router; skip `make_view_hook_command` / `generate_improved_evidence`. Telemetry must record `legacy_path_skipped=True` so paired metrics can distinguish "router substituted" from "router silent".
  - In shadow mode: preserve current behaviour.
  - In off mode: skip `_ensure_v2_router` entirely.

### B-4: Double-injection risk in live mode
- **Direct evidence**: At `post_view` (2233–2240) the router emits and the legacy `make_view_hook_command` hook also fires. At `post_edit` (around 2425) the same pattern repeats.
- **Consequence**: If `GT_ROUTER_V2=live` is set without also gating the legacy path, an agent will see **both** the router emission and the legacy `[GT_*]` evidence on the same observation. That doubles `injections_per_task` and the comparison against OLD_GT becomes uninterpretable.
- **Fix needed**: Same as B-3 — the live-mode gate must be additive (router-on) **and** subtractive (legacy-off) in one change.

### B-5: No binary provenance written into graph.db
- **Direct evidence**: `sqlite.go:149–152` defines `project_meta` but `InsertNode`/`InsertEdge` paths (200–211, 215–219) write nothing to it. Confirmed empty on inspection.
- **Consequence**: A graph.db file in `.tmp_holdout/` is indistinguishable from one built with the current binary. Consumers cannot fail fast on schema drift.
- **Fix needed**: At `gt-index` write-time, populate `project_meta` rows: `gt_index_commit`, `gt_index_build_time`, `schema_version`. Add a Python helper that reads these and emits `[GT_FAIL]` if `schema_version` is older than the Python code requires.

### B-6: No fail-fast on wrapper-not-loaded
- **Direct evidence**: `swebench_30task.yml:118–138` validates that the gt-index binary works and that `groundtruth.hooks.post_edit` and `groundtruth.pretask.v1r_brief` import. It does NOT validate that the wrapper itself injected GT into the agent's loop on the first task.
- **Consequence**: A silent regression where the wrapper falls back to `runtime_setup_func=None` because of an early exception will produce 30 "patched but no GT visible" runs and look like a clean OH baseline. The post-run gate at `swebench_30task.yml:180–186` only checks `GT_COST.*call=` count (LLM calls), not GT-event count.
- **Fix needed**: After task 1, assert at least one row in `gt_interactions.jsonl` AND at least one `[GT]`-tagged observation in `output.jsonl`. If both are zero with `GT_BASELINE` unset, fail the whole matrix.

## Required canary artifacts (per task)

Already present per `oh_gt_full_wrapper.py`:
- `output.jsonl` — agent trajectory
- `gt_interactions.jsonl` — structured layer events
- `gt_hooks.log` — hook fire/skip log

Required additions for paired V2 analysis:
- `graph.db` — pulled via `_pull_graph_db_artifact(config)` at task end (already wired per prior session); audit-required field for the canary harness.
- `router_events.jsonl` — derived from `interaction_log` filtered to `layer == "L3_router_v2"`. Already inline in `gt_interactions.jsonl` per `_router_v2_on_view` line 1554–1557.

## Decision: what is proven, what is not

### Proven (by file/line evidence above)
- GHA pre-flight will trip on a stale binary or stale DB — schema is enforced **on the probe**.
- GHA always rebuilds gt-index from current Go source — no schema drift on GHA-built DBs.
- Router-V2 is wired into `post_view`/`post_edit` event paths in shadow mode.
- Router-V2 emits structured events into `gt_interactions.jsonl` with provider/branch attribution.

### NOT proven (cannot claim without paired-VM evidence)
- That the V2 router produces less stale/late/injection than OLD_GT on real tasks. CANARY_COMPARISON.md V2 column is empty.
- That live-mode is safe (no implementation exists yet).
- That schema columns on the current Go source improve any agent-visible metric.
- That `_pull_graph_db_artifact` actually copies a DB on a real VM run (no captured artifact verified post-VM).

## Validation ladder — next allowed actions

1. **Land Track-B refactor** (`GT_ROUTER_V2` → off/shadow/live, live-mode legacy bypass). One PR-scope local commit, all unit tests still green as admission gate only.
2. **Land B-1 fix** (`schema_version` written by indexer + verified on read).
3. **Run shadow canary** (3 tasks × 3 arms, GT_ROUTER_V2 in {off, shadow, live}) — only on VM, never claim from local.
4. **Fill `reports/canary/CANARY_COMPARISON.md` V2 column** from those artifacts.
5. **Stop.** No 15/30/300 until the paired n=5 holdout has run cleanly.

## Open blockers (cannot resolve from this environment)
- VM execution required to fill CANARY_COMPARISON.md V2 column.
- No GHA dispatch is queued; current matrix would run router-off arms only.

## Files cited (for grep convenience)
- `gt-index/internal/store/sqlite.go`
- `.github/workflows/swebench_30task.yml`
- `.github/actions/setup-eval/action.yml`
- `scripts/swebench/oh_gt_full_wrapper.py`
- `scripts/swebench/build_gt_index_linux.sh`
- `.tmp_holdout/bugs/axum-3661/graph.db` (artifact)
- `reports/canary/CANARY_COMPARISON.md`
- `docs/handoff/canary_v2_runbook.md`

## Addendum 2026-05-17 — B-7: V2_LIVE router-host graph.db gap

Discovered from canary run `25995605932` (commit `b3fccb4f`) where every
wiring fix landed but deep telemetry still shows zero agent-visible router
emissions. Details in `reports/canary/V2_LIVE_DEEP_TELEMETRY_2026-05-17.md`.

| Claim | File | Lines | Exact quote / artifact | Why it matters |
|---|---|---:|---|---|
| Router cached on first call with empty host DB | `scripts/swebench/oh_gt_full_wrapper.py` | 1521, 1555 | `db_path = getattr(config, "_host_graph_db", "") or config.graph_db` then `config._router_v2 = router` | First post_view fires before any post-edit L6 reindex, so `_host_graph_db` is empty; container path is not openable from the host wrapper process |
| Host-side graph.db only landed mid-run | run `25995605932` gt_debug log | n/a | `[GT_META] graph.db downloaded to host after L6 reindex: /tmp/tmp...db` appears AFTER first edit | Confirms the host DB is post-edit-only by current wrapper design |
| All 37 router invocations suppressed `no_graph_db` | `gt_layer_events_*.jsonl` for all 3 v2_live tasks | n/a | `event_type: on_view\|on_edit`, `emitted: False`, `suppression_reason: no_graph_db` (17+18+2) | Router runs, decides correctly, but has no graph to query → silent live arm |
| Live mode + silent router = strictly worse than legacy | run `25995605932` vs `25994590953` | n/a | OLD_GT/beets had 9 L3b + 2 L3 events with `Called by:` / `SIGNATURE:` evidence; V2_LIVE/beets had 0 | In live mode the legacy hook is bypassed by design; without router emission the agent gets nothing during localization |

### Fix candidates (not in current Track-B scope)

1. **Pre-fetch graph.db to host at task start** — before first agent action.
2. **Reset router cache after L6 reindex** — `config._router_v2 = None` once
   `_host_graph_db` is populated, so the next call rebuilds the router
   against the real DB.
3. **In-container router invocation** — run router via subprocess inside
   the container so it reads the container-resident DB directly. Mirrors
   how legacy L3b works. Larger refactor.

Do not silently flip V2 to default until at least one of these lands AND
a paired canary shows non-zero `injections_per_task` on tasks where OLD_GT
also delivers.

## Addendum 2026-05-17 — B-8: Downloaded graph.db is malformed

Discovered from canary run `25996587814` (B-7 fix run). B-7 pre-fetch
code works correctly — graph.db is downloaded to host before first
post_view. However, the downloaded file is a **malformed SQLite database**.

| Claim | File | Lines | Exact quote / artifact | Why it matters |
|---|---|---:|---|---|
| graph.db arrives malformed | `full_run.log` (all 3 tasks) | beancount:314, beets:310, loguru:312 | `[GT_META] router_v2 schema check error (DatabaseError): database disk image is malformed` | Router has a path but cannot query it → all calls return `no_evidence` |
| base64 fragmentation is root cause | `scripts/swebench/oh_gt_full_wrapper.py` | 1908-1911 | `tokens = re.findall(r"[A-Za-z0-9+/=]{128,}", b64_content)` then `best = max(tokens, key=len)` | OH observations split base64 stream with noise; longest fragment is a subset of the full binary |
| All router calls emit=False | `full_run.log` (all 3 tasks) | (all on_view/on_edit lines) | `emit=False sup=no_evidence text_len=0` — 0 `emit=True` across 55 total calls | V2 live mode delivers zero evidence — identical to baseline |
| Suppression shifted from no_graph_db to no_evidence | `full_run.log` comparison | B-7 canary `25995605932` vs B-7-fix `25996587814` | Prior: `no_graph_db` (37/37). Now: `no_evidence` (55/55). | B-7 fixed the availability; B-8 is the corruption |

### Fix (applied, not yet verified on GHA)

Changed `_download_graph_db_to_host()` (wrapper line 1905):
1. Concatenate ALL base64 tokens (`"".join(tokens)`) instead of `max(tokens, key=len)`.
2. Validate result with `sqlite3.connect().execute("SELECT count(*) FROM nodes")`.
3. Discard and return empty if validation fails, with `[GT_META] B-8:` marker.

### Status

B-8 fix applied locally. 102 tests pass. Re-canary required before
claiming V2 live delivers evidence.
