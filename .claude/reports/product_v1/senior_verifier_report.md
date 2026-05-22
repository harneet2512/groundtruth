# Senior Verifier Report: Product-v1 Stage 1 Bug Findings

**Date:** 2026-05-22
**Scope:** 5 frozen task artifacts from Product-v1 Stage 1 runtime proof
**Method:** Chronological reading of output.jsonl content fields (agent perspective), cross-referenced with gt_layer_events JSONL and full_run.log

---

## BUG 1: GT_STATUS diagnostic lines leak into agent observations (CONFIRMED, 4/5 tasks affected)

### Severity: HIGH -- agent sees internal telemetry as if it were evidence

### Evidence from artifacts

| Task | Entry | Exact leaked text | Context |
|---|---|---|---|
| sh-744 | entry[17] | `[GT_STATUS] success:test_targets:8` | Appended after test file L3b evidence |
| sh-744 | entry[45] | `[GT_STATUS] no_evidence:no_graph_edges` | Sole GT content on a file view -- agent sees "no evidence" as a signal |
| conan-17102 | entry[23] | `[GT_STATUS] success:test_targets:8` | Appended after test file L3b evidence |
| pylint-10044 | entry[15] | `[GT_STATUS] no_evidence:no_graph_edges` | Agent sees this on `bad.py` |
| pylint-10044 | entry[17] | `[GT_STATUS] no_evidence:no_graph_edges` | Agent sees this on `good.py` |
| pylint-10044 | entry[31] | `[GT_STATUS] success:test_targets:8` (partial, cut by line limit) | On test file |

### Exact code path

**File:** `src/groundtruth/hooks/post_view.py` lines 663-701

The `main()` function in post_view.py unconditionally prints `[GT_STATUS]` lines to **stdout**:

```python
# Line 663-666 (test file path)
status = _status_line("success", f"test_targets:{len(targets)}")
print(status)

# Line 699-700 (no-evidence path)
status = _status_line("no_evidence", "no_graph_edges")
print(status)
```

These `print()` calls go to stdout. The wrapper (`oh_gt_full_wrapper.py`) captures stdout from the post_view hook and prepends it to the agent's observation. The wrapper does NOT strip `[GT_STATUS]` lines before prepending.

Contrast: `[GT_META]` lines in post_edit.py use `print(..., file=sys.stderr)` or `flush=True` to stderr. But post_view.py sends `[GT_STATUS]` to stdout.

### Impact

1. **sh-744 entry[45]**: The agent reads a section of sh.py (lines 2380-2410) and sees `[GT_STATUS] no_evidence:no_graph_edges` as the ONLY GT annotation. This tells the agent "GT has nothing useful here" -- a negative signal that could discourage the agent from looking for patterns in that code region. The task resolved anyway, but this is noise.

2. **pylint-10044 entries[15,17]**: The agent sees `[GT_STATUS] no_evidence:no_graph_edges` on `bad.py` and `good.py`. These are tiny example files in `doc/data/messages/` that legitimately have no graph edges. But the agent now receives a diagnostic marker as if it were evidence content.

3. **RECALL contamination (sh-744 entry[47])**: The L3 post-edit evidence at entry[47] includes `[RECALL] from earlier: [GT_STATUS] no_evidence:no_graph_edges`. The RECALL mechanism is faithfully echoing back the leaked status line as if it were prior evidence the agent should remember. This means the GT_STATUS leak propagates through the recall chain into future evidence.

### Fix required

In `post_view.py`, change all `print(status)` calls for `[GT_STATUS]` lines to `print(status, file=sys.stderr)`. Alternatively, the wrapper should strip any line matching `^\[GT_STATUS\]` from the stdout capture before prepending to the observation.

---

## BUG 2: RECALL mechanism echoes back diagnostic leaks and stale context (CONFIRMED)

### Severity: MEDIUM

### Evidence

sh-744 entry[47] content:
```
[GT] Post-edit: sh.py
[RECALL] from earlier: [GT_STATUS] no_evidence:no_graph_edges
<gt-evidence trigger="post_edit:sh.py">
  L897: return wait_for_completion().__await__()
[SIGNATURE] def __await__(self):
[PATTERN] sibling __init__() does:
...
```

The `[RECALL]` mechanism records what was delivered on the LAST view of a file and replays it when an edit happens. If the last view delivered a `[GT_STATUS]` leak, the recall faithfully replays it. This means:

1. The agent sees `[RECALL] from earlier: [GT_STATUS] no_evidence:no_graph_edges` right before real post-edit evidence.
2. This creates a contradictory signal: "no evidence" followed by actual evidence items.

### Code path

The RECALL is generated in the wrapper's L3 router. It stores the last emitted text per file and prepends it on the next event for the same file. Since `[GT_STATUS]` was part of the emitted text (because it was in stdout), it gets stored and replayed.

### Fix required

The RECALL storage should strip `[GT_STATUS]` lines before saving. Or better: fix Bug 1 (redirect GT_STATUS to stderr), which eliminates this automatically.

---

## BUG 3: Conan-17102 -- GT showed callers/test assertions but NOT the build_args serialization pattern (CONFIRMED context gap)

### Severity: HIGH -- this is the missed context that could have flipped the task

### Evidence

The conan-17102 FAIL_TO_PASS test expects a `ProfileArgs` class (or equivalent) to serialize `profile_args` in the build-order JSON output. The gold patch adds profile argument handling to the build-order serialization.

**What GT delivered (entry[15] L3b for install_graph.py):**
```
Called by: conans/client/installer.py:205 `install_graph = InstallGraph(graph)`
          conan/api/subapi/install.py:25 `install_graph = InstallGraph(deps_graph)`
Calls into: conans/client/graph/graph.py::report_graph_error,add_edge (3x)
            conans/util/files.py::save,load (1x)
            conan/api/output.py::info,warning (4x)
```

**What GT delivered (entry[23] L3b for test file):**
```
[TEST] assert bo_json == result
[TEST] assert bo_json["order_by"] == "recipe"
[TEST] assert bo_json["order"] == result
[TEST] assert bo_json["order_by"] == "configuration"
```

**What was MISSING:**
- No `[PATTERN]` sibling evidence for `install_build_order()` showing the serialization pattern (`"order_by": self._order, "reduced": self.reduced, "profiles": self._profiles`)
- No evidence showing that `_InstallRecipeReference` and `_InstallConfiguration` have `serialize()` methods with specific key structures
- No evidence showing the test expects `"profile_args"` key in the serialized output

The agent made 11 edits to `install_graph.py`, all focused on `deserialize()` and profile-data handling during load/merge. The agent never saw the serialization pattern that would hint at adding `profile_args` to the output. GT delivered L3b (navigation edges) but NOT L3 (post-edit contract/sibling evidence) for any of the 11 edits to install_graph.py.

### Why L3 did not fire on conan edits

Looking at entries 105-187, none of the edit observations contain `[GT]` prefixes, `[PATTERN]`, `[SIGNATURE]`, `[CONTRACT]`, or any L3 evidence markers. The edits show raw file content only. This means the L3 post-edit hook either:
1. Did not fire (wrapper did not call post_edit.py)
2. Fired but produced empty output (G7 silence gate suppressed because `install_build_order` has 0 callers + 0 siblings + 0 peers in graph.db)
3. Fired but output was filtered by dedup (Patch D)

The gt_run_summary confirms: `l3_edit_events_seen: 0` is NOT present in the conan summary (it shows L3_router_v2 emitted 32 events). But the layer events show many `on_edit` and `on_view` events -- the L3 contract evidence may have been suppressed by the G7 silence gate (Patch C), since `InstallGraph.__init__` and `deserialize` are static methods that may have 0 callers + 0 siblings + 0 peers in the graph.

### Fix required

The G7 silence gate (Patch C) needs an exception: it currently suppresses ALL evidence when `callers=0 AND siblings=0 AND peers=0`. But for serialization functions like `deserialize()` and `install_build_order()`, the SIBLING pattern evidence (showing the parallel serialization structure) is exactly what the agent needs. The gate should preserve sibling evidence even when callers are 0.

---

## BUG 4: Confidence filter (Patch A) is unobservable -- cannot verify from artifacts (CONFIRMED observability gap)

### Severity: MEDIUM -- not a functional bug, but blocks debugging

### Evidence

Patch A added `AND COALESCE(e.confidence, 0.5) >= 0.7` to 15 SQL queries in post_view.py and post_edit.py. None of these queries emit a log line showing how many edges were filtered.

From the artifacts:
- sh-744: L3b shows `Called by: tests/sh_test.py:96` -- this is a same-file or import-verified edge (confidence 1.0), so the filter did not change anything.
- conan-17102: L3b shows `Called by: conans/client/installer.py:205` and `conan/api/subapi/install.py:25` -- these are likely import-verified (confidence 1.0).
- pylint-10044: `[GT_STATUS] no_evidence:no_graph_edges` on small example files -- these files have no edges at all, so the confidence filter is irrelevant.

**The problem:** We cannot determine from any artifact whether the confidence filter REMOVED edges that would have been useful. There is no log line like `[GT_META] confidence_filter: removed N edges below 0.7 for file X`. Without this, we cannot tell if the filter is too aggressive.

### Fix required

Add a stderr diagnostic line in post_view.py `graph_navigation()` and post_edit.py `_get_callers_from_graph()` that reports: total edges found, edges after confidence filter, confidence distribution. This should go to stderr (not stdout) to avoid Bug 1.

---

## BUG 5: Anchor ranking (Patch E) loaded symbols but evidence shows no reordering effect (INCONCLUSIVE)

### Severity: LOW -- cannot confirm bug, but cannot confirm benefit either

### Evidence

From full_run.log for sh-744: `[GT_META] anchors: 40 symbols, 0 paths, 0 tests`

40 symbols were loaded for sh-744. But the L3b evidence at entry[11] shows:
```
Called by: tests/sh_test.py:96 `sh.Command(prog)`
```

This is a single caller. With only 1 caller, anchor ranking cannot change the order. The anchor ranking code (Patch E in post_edit.py lines 1576-1591) sorts `ordered_callers` by `_anchor_score`, but when there is only 1 caller, sorting is a no-op.

For conan-17102, L3b entry[15] shows 2 callers:
```
Called by: conans/client/installer.py:205 `...`, conan/api/subapi/install.py:25 `...`
```

Neither log nor JSONL records the pre-ranking vs post-ranking order. We cannot determine if anchors changed the caller ordering.

For flask-5637, entry[11] shows 3 callers for config.py. The order is: `tests/test_config.py`, `examples/tutorial/flaskr/__init__.py`, `examples/celery/src/task_app/__init__.py`. Whether this order was changed by anchors is unknown.

### Fix required

When anchor ranking changes the order of callers, emit a stderr diagnostic: `[GT_META] anchor_rerank: file=X callers=N reordered=True/False top_before=Y top_after=Z`. Without this, Patch E's effectiveness is unmeasurable.

---

## BUG 6: L1 brief text is structurally incomplete for sh-744 (MINOR)

### Severity: LOW

### Evidence

The L1 brief injected for sh-744 (from layer events line 2):
```
<gt-task-brief>
1. sh.py (def debug(self, msg, *a):, def read(self):, def wait(self):)
   Context: get_num_args | Last: a90f61c chore: windows error before other imports
   Tests: tests/sh_test.py
2. tests/sh_test.py
   Context: Last: 484399f Avoid manual async loop management
   Calls: sh.py

Edit sh.py first. Verify: pytest tests/sh_test.py
</gt-task-brief>
```

The brief shows `def debug(self, msg, *a):, def read(self):, def wait(self):` as the key functions. But the actual bug is in `__await__()` (adding `self.wait()` call). The brief's function selection (debug, read, wait) does not include `__await__`. This is because the brief selects top functions by reference count, and `__await__` may have low reference count in graph.db.

This is a limitation, not a bug per se -- the brief cannot know which function the agent will edit. But it shows the brief's function hints can be misleading on small repos.

---

## BUG 7: Flask-5637 and pylint-10044 received NO L3 post-edit evidence (CONFIRMED)

### Severity: HIGH

### Evidence

**Flask-5637:** 137 history entries, 0 entries contain `[PATTERN]`, `[CONTRACT]`, `[SIGNATURE]`, or `<gt-evidence>` markers. The gt_run_summary shows `l3_edit_events_seen: 0` equivalent (L3 section shows 0 for all edit-related metrics). The agent made edits to `ctx.py` but received no post-edit contract evidence.

**Pylint-10044:** 133 history entries, 0 entries contain post-edit L3 evidence markers. The agent edited documentation files (`bad.py`, `good.py` in doc/data/messages/) which are tiny files with no graph edges.

For flask-5637, the edit was to `src/flask/ctx.py`. This is a core file that should have callers and signatures in graph.db. The absence of L3 evidence suggests either:
1. The L3 router classified the edit path incorrectly
2. G7 silence gate fired (0 callers + 0 siblings + 0 peers for `RequestContext.__init__` in the graph)
3. The in-container hook failed silently

The flask gt_run_summary shows L3_router_v2 emitted 17 events, but those are `on_view` and `on_edit` delegations, not evidence content. The actual evidence generation happens in-container and may have returned empty.

### Fix required

Investigate whether the G7 silence gate (Patch C) is suppressing evidence on flask's `ctx.py`. If `RequestContext.__init__` has 0 cross-file callers in graph.db (because it is typically constructed via framework magic, not direct calls), the gate would suppress all evidence. This is exactly the "edge-sparse but important" scenario the gate should NOT suppress.

---

## Summary of Findings

| # | Bug | Severity | Affected Tasks | Fix Complexity |
|---|---|---|---|---|
| 1 | GT_STATUS stdout leak to agent | HIGH | 4/5 (sh-744, conan, pylint x2) | Low: redirect print() to stderr |
| 2 | RECALL echoes leaked diagnostics | MEDIUM | 1/5 (sh-744) | Auto-fixed by Bug 1 fix |
| 3 | Conan: no sibling serialization pattern | HIGH | 1/5 (conan-17102) | Medium: G7 gate exception for siblings |
| 4 | Confidence filter unobservable | MEDIUM | 5/5 | Low: add stderr diagnostics |
| 5 | Anchor ranking unverifiable | LOW | 5/5 | Low: add stderr diagnostics |
| 6 | Brief function selection suboptimal | LOW | 1/5 (sh-744) | Out of scope (brief design) |
| 7 | Flask/pylint: zero L3 post-edit evidence | HIGH | 2/5 | Medium: investigate G7 gate + in-container hook |

### Top priority fixes

1. **Bug 1** (GT_STATUS leak): Trivial fix, high impact. Every `print(status)` in post_view.py `main()` should become `print(status, file=sys.stderr)`.

2. **Bug 3 + Bug 7** (G7 silence gate over-suppression): The G7 gate at post_edit.py line 1736-1743 suppresses all evidence when `total_callers == 0 and not siblings and not peers`. This is too aggressive for:
   - Serialization functions (conan: `deserialize`, `install_build_order`)
   - Framework lifecycle methods (flask: `RequestContext.__init__`)
   - Functions called via decorators/metaclasses/dynamic dispatch

   The fix: preserve `[PATTERN]` sibling evidence and `[SIGNATURE]` even when the gate fires. Only suppress caller-dependent evidence (callers, propagation, co-change).

3. **Bug 4** (observability): Add confidence filter diagnostics to stderr so future runs can be debugged.
