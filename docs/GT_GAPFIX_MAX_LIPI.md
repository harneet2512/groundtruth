# GT Gap-Fix — Adversarial MAX-LIPI Synthesis

**Date:** 2026-06-14
**Scope:** The gap-fix diff (G01–G18 catalog) across 6 review surfaces — rank-safety/I2, Go indexer incremental+promote, delivery/graph plumbing, oracle/verify, brief, harness/env/mounts.
**Method:** 4-avenue LIPI (Logic / Implementation / Integration / Plumbing) per surface; every load-bearing claim re-verified against the working tree before synthesis.

---

> **RESOLUTION (2026-06-15) — ALL BLOCKERS CLOSED.** Both HIGH findings are fixed and test-pinned;
> 10 of the 11 MEDIUM/LOW/NIT items resolved; 1 (§2.11) deliberately deferred as optional. Full
> write-up in `gt_new.md` §10.1. Highlights:
> - **§2.1 (W_PROX rank leak) — FIXED + PINNED.** `anchor_proximity` now applies the D5
>   `_degree_edge_filter`; `tests/test_anchor_proximity_i2.py` mutation-checked (RED without the
>   predicate, GREEN with). **Rank-leak count is now 0.**
> - **§2.2 (G09 disabled) — RE-ENABLED + a SECOND bug found.** The mutation check exposed an
>   independent incremental-path defect (caller `ParentID` zeroed in the `nodeMeta` copy → CHA rungs
>   dead even with the map). Both fixed; the e2e test was rebuilt to BITE (was tautological via the
>   SHA-256 short-circuit + a globally-unique fixture name) and now goes RED on either bug alone.
> - **MEDIUM/LOW/NIT:** §2.3/§2.4/§2.5/§2.6/§2.7/§2.8/§2.9/§2.10/§2.12/§2.13 all resolved; §2.11
>   deferred (optional; mutates cc semantics shared with DATA_FLOW).
> - **Gates:** `gt-index` build exit 0; Go resolver+cmd suites green (lone pre-existing
>   `TestRoutePatternMatching/comment` failure is orthogonal); 31 py tests (contract/brief/I2) green.

---

## 1. Headline

| Metric | Count |
|---|---|
| Surfaces reviewed | 6 |
| Surfaces CLEAN | 1 (oracle/verify) |
| Surfaces FLAW_FOUND | 5 (rank-safety/I2, Go indexer, delivery, brief, harness) |
| **Rank leaks (`is_rank_leak=true`) — MUST BE 0** | **1 — RELEASE BLOCKER** |
| Regressions (`is_regression=true`) | 0 |
| HIGH findings | 2 |
| MEDIUM findings | 2 |
| LOW findings | 5 |
| NIT findings | 4 |

**Verdict:** **NOT RELEASABLE.** One confirmed rank leak (I2 violation via `W_PROX`) and one HIGH non-functional-fix-claimed-as-resolved (G09 disabled in committed HEAD). Both are independently blocking: the first violates the cardinal invariant the whole diff exists to protect; the second falsifies a "resolved+verified" claim against the binary.

All claims below were re-verified by direct file reads (not grep-scan alone). Every numeric line cite was confirmed against the working tree on 2026-06-14.

---

## 2. Findings, worst-first

### 2.1 HIGH — I2 rank leak via `W_PROX` (anchor proximity untyped SELECT) [RELEASE BLOCKER]

- **File:line:** `src/groundtruth/pretask/anchor_proximity.py:36-48` (untyped edge SELECT) → consumed at `src/groundtruth/pretask/v7_4_brief.py:1206` (`prox_scores = compute_anchor_proximity(...)`) → ranked at `v7_4_brief.py:788` (`+ weights.get("W_PROX", 0) * components.get("anchor_prox", 0.0)`).
- **Avenue:** Plumbing.
- **Invariant violated:** **I2 — depth never enters reach/RANK; byte-identical-rank-with-vs-without-promoted-edges.**
- **`is_rank_leak`: TRUE.** **`is_regression`: false.**

**What I verified:**
- `anchor_proximity.py:45` gates only on `COALESCE(e.confidence,0.5) >= 0.7`, with **no** type/provenance exclusion. There is no `_degree_edge_filter` and no `promote_%` predicate in this query.
- The sibling reach feeder `graph_reach.py:31-34` *does* import and apply `_degree_edge_filter` (the D5 SSOT). The G01 fix plugged reach but left its sibling `compute_anchor_proximity` untyped — the catalog enumerated `_total_score:783` (reach term) but not `:788` (prox term). Both the catalog and the fix stopped one term short.
- The consumption chain is live: `v7_4_brief.py:1206` feeds `prox_scores`; `:788` adds `W_PROX * anchor_prox` to the rank. `W_PROX` default is `0.05` (`v7_4_brief.py:453`) and is **boosted to 0.12** in the function-level regime (`v7_4_brief.py:181` and `:313`). This is a live, sometimes-amplified rank term, not dead code.
- Promoted DEPTH edges (READS/WRITES/RAISES/CO_SERIALIZES/DATA_FLOW) are minted at confidence 1.0 by `promote.go`, cross files by construction (READS/WRITES target the owning Class node, which may live in another file; CO_SERIALIZES/RAISES/DATA_FLOW are cross-file), so they pass the `>=0.7` gate and inflate the anchor's 1-hop neighbor set. PRECEDES is incidentally safe (demoted to 0.4/0.5, fails the `>=0.7` gate) — but the other five classes leak.

**Net:** once the promotion pass ships, a graph WITH promoted edges produces a DIFFERENT rank than one without — the exact I2 violation G01 claims to close.

**Recommendation:** Append the SAME D5 single-source predicate to `compute_anchor_proximity` — add `AND {graph_localizer._degree_edge_filter('e')}` to the WHERE at `anchor_proximity.py:45`, importing it the cycle-free way `graph_reach` already does. This is byte-identical on pre-depth graphs (0 promoted edges today) and closes the `W_PROX` leak once promotion ships. Then ADD a regression test mirroring `tests/test_graph_reach_i2.py` that asserts `compute_anchor_proximity` returns byte-identical scores with vs without promoted READS/WRITES/RAISES/CO_SERIALIZES/DATA_FLOW edges layered on. The existing I2 suite covers reach/expand but **NOT** proximity — that gap is why this slipped. Audit `W_FRAME`/`W_PATH` feeders the same way; those query traces/path-resolution, not the `edges` table, so they are not at risk.

---

### 2.2 HIGH — G09 (-file inheritanceMap) is DISABLED in committed HEAD; falsely claimed "resolved+verified"

- **File:line:** `gt-index/cmd/gt-index/main.go:999-1001`.
- **Avenue:** Integration.
- **Invariant:** correct-or-quiet (still degrades safely — no wrong fact) **BUT** a non-functional fix is claimed as resolved+verified; gt_gt §2.3 CHA ladder rungs are dead on the `-file` path.
- **`is_rank_leak`: false. `is_regression`: false.**

**What I verified:**
- `main.go:999` reads literally `// TEMP-RED-CHECK: G09 wiring disabled to confirm the e2e test goes red.` followed by `_ = allFiles` / `_ = allLangs` at lines 1000-1001. The 13-line comment above (986-998) describes the *correct* fix but the call to `buildInheritanceMap` + `resolver.SetInheritanceMap` was never re-enabled.
- `SetInheritanceMap` is called ONLY on the full-index path (per the surface verdict, `main.go:428`); the incremental `-file` path never sets it. Because `gt-index -file` runs as a fresh process, the package-level `inheritanceMap` (resolver.go) starts nil and stays nil, so `lookupMethodWithInheritance` short-circuits `return 0,false` on every reindex → CHA rungs 1.75/1.94/1.94a/2b under-resolve inherited-method / parent-field calls to name_match on EVERY `-file` reindex.
- The `_ =` blank assignments exist solely to keep the build green (build verified exit 0 with CGO + sqlite_fts5) — which is exactly what masks the dead fix.
- The claim in gt_new.md ('18/18 gaps addressed — 17 fully resolved + verified', naming G09 `-file inheritanceMap` as resolved) is false against the binary.
- Additional defect noted by the surface review: even re-enabling as the comment describes would not compile as written — `allFiles`/`allLangs` are `[]string` but `buildInheritanceMap` takes `[]walker.SourceFile`; a `[]SourceFile` must be reconstructed first.

**Recommendation:** Re-enable G09 — delete the TEMP-RED-CHECK comment and the `_ = allFiles; _ = allLangs` lines; reconstruct a `[]walker.SourceFile` from `allFiles`/`allLangs` (or from the `filteredNodes` file_path set), call `buildInheritanceMap(...)`, and `resolver.SetInheritanceMap(inhMap)` BEFORE `resolver.Resolve` at `main.go:1025`. Add a deterministic `-file` e2e test asserting an inherited-method call resolves (NOT name_match) after `gt-index -file`. Until then, correct gt_new.md to mark G09 OWED/disabled — not "resolved+verified."

---

### 2.3 MEDIUM — codespace telemetry mount missing `chmod 777` → 8-dp deep log silently lost

- **File:line:** `railway/codespace_deepswe_run.sh:133-134`.
- **Avenue:** Plumbing.
- **Invariant:** fail-closed / CLAUDE.md 8-dp deep-log mandate.
- **`is_rank_leak`: false. `is_regression`: false.**

**What I verified:** `codespace_deepswe_run.sh:134` is `mkdir -p "$HOST_GT_OUT"` with NO `chmod 777`. The reference `deepswe_full.yml` deliberately does `mkdir -p ... && chmod 777 ...` with the rationale "the task-image user is arbitrary and must be able to append." DeepSWE task containers run as varied/arbitrary uids; a host dir owned by the codespace user (umask 0755) bind-mounted into a container is effectively read-only to a different in-container uid. The in-container 8-dp telemetry producers (GT_ORACLE_EVENTS / GT_RUNTIME_LEDGER / GT_HOOK_FIRE_COUNTS → /gt_out/*) then fail their writes, the G11 "survive the container" fix silently does nothing, and the copy-out loop (`cp ... 2>/dev/null || true`) swallows the absence — the run stays green with the mandated 8-dp deep log LOST.

**Recommendation:** Mirror full.yml exactly: `mkdir -p "$HOST_GT_OUT" && chmod 777 "$HOST_GT_OUT"`. Make the copy-out loud (warn if zero files recovered) so a failed write is diagnosable instead of swallowed by `|| true`.

---

### 2.4 MEDIUM — `gt_ae_block.sh` "SINGLE SOURCE OF TRUTH" claim is false; G03/G04 + full G11 trio remain UNFIXED on both GHA paths

- **File:line:** `artifact_deepswe/gt_integration/gt_ae_block.sh:2-21,48`; `.github/workflows/deepswe_trial.yml:171`.
- **Avenue:** Integration.
- **Invariant:** non-invention / claim-matches-reality (a fix's stated scope must equal its wired scope).
- **`is_rank_leak`: false. `is_regression`: false.**

**What I verified:** The block declares itself `SINGLE SOURCE OF TRUTH` (line 3), states `trial and full CANNOT drift` (line 20), and `GT_VERIFY_STRUCTURAL_RISK must be present in --ae on EVERY path incl full.yml` (line 48). In reality the block is `source`d by exactly ONE caller — `codespace_deepswe_run.sh:148`. `deepswe_trial.yml`'s `pier run` (line 171) passes NO `--ae`, NO `--mounts-json`, and does not source the block (grep returns the single `pier run` line and no `gt_ae_block`/`--ae` match). Per the surface verdict, `deepswe_full.yml` also does not source the block and forwards only GT_ORACLE_ROUTE + GT_ORACLE_EVENTS — never GT_VERIFY_STRUCTURAL_RISK / GT_VERIFY_RISK_TRIGGER / GT_RUNTIME_LEDGER / GT_HOOK_FIRE_COUNTS. So G03/G04 (structural-risk axis dark in-container) and the full G11 telemetry trio remain UNFIXED on both GHA paths; only the codespace witness path got the wiring. The de-drift fix is not delivered for two of the three named surfaces.

**Recommendation:** Either (a) actually `source` the block from the `deepswe_trial.yml` and `deepswe_full.yml` run-steps and splice `${GT_AE_ARGS[@]}` + the mounts-json, making it a true single source; or (b) downgrade the block header to "helper for the codespace path" and stop claiming trial/full are de-drifted. As written, the comment asserts coverage the code does not provide.

---

### 2.5 LOW — injection decode fail-open leaves a torn `/tmp/graph.db` (build stays green)

- **File:line:** `artifact_deepswe/gt_agent.py:721-726`.
- **Avenue:** Plumbing. **Invariant:** fail-closed. **`is_rank_leak`: false. `is_regression`: false.**
- The decode `base64 -d {b64} | gunzip > /tmp/graph.db && chmod 644 ... || echo '...failed' >&2` truncates `/tmp/graph.db` via `>` BEFORE gunzip runs, and the trailing `||` swallows failure into exit 0. A corrupt/partial chunk leaves a 0-byte/torn db; the Docker build still succeeds; `_db_path` returns it (`os.path.isfile` is True on a 0-byte file). Unlike the size-ceiling case (which raises = fail-closed), an in-container decode failure does NOT fail loud at build time. Downstream is still correct-or-quiet (`_connect_ro` readability probe fails, prints `GRAPH_UNREADABLE_IN_CONTAINER` once, returns None) so not a wrong fact — but the build-time signal is masked, and there is no A2 `_guard_handoff_db` hard-fail on the non-substrate path.
- **Recommendation:** Decode to a temp, assert non-empty, then atomically move: `base64 -d {b64} | gunzip > /tmp/graph.db.tmp && [ -s /tmp/graph.db.tmp ] && mv /tmp/graph.db.tmp /tmp/graph.db && chmod 644 /tmp/graph.db || { rm -f /tmp/graph.db.tmp /tmp/graph.db; echo 'GT: graph.db injection FAILED' >&2; }`. Optionally assert non-empty in the post-inject self-test.

### 2.6 LOW — `.pyi` added to `_SRC_EXT`/`_SOURCE_EXTS` with a false "matches gt-index" justification

- **File:line:** `artifact_deepswe/gt_mini_patch.py:383-390` (and 303-306); contradicted by `gt-index/internal/specs/python.go:10`.
- **Avenue:** Logic. **Invariant:** non-invention / correct-or-quiet. **`is_rank_leak`: false. `is_regression`: false.**
- gt-index's Python spec indexes ONLY `.py`; `.pyi` stubs are never parsed into nodes. (`.mjs`/`.cjs` ARE correct per javascript.go.) Not a wrong fact: a `.pyi` edit fires the contract/cochange/evidence producers, which query graph.db, find no node, and return '' (correct-or-quiet). Only an inaccurate justification comment + `.pyi` edits that can never receive graph-backed evidence.
- **Recommendation:** Either (a) correct the comment to state `.pyi` is added for edit-detection/sensor parity only and is deliberately graph-quiet, or (b) add `.pyi` to `python.go` Extensions if `.pyi` evidence is wanted. Do not leave the comment asserting an indexing alignment that does not exist.

### 2.7 LOW — PRECEDES honest-trust demotion branch (cc>1 → 0.4 SPECULATIVE) is untested

- **File:line:** `gt-index/internal/resolver/promote.go:833-836`.
- **Avenue:** Implementation. **Invariant:** non-invention / honest-trust (§2.6). **`is_rank_leak`: false. `is_regression`: false.**
- `promote_test.go:178` asserts only the cc=1 / 0.5 / CANDIDATE path; the demotion branch never executes in the suite. No red→green proof for the change's whole point.
- **Recommendation:** Add a fixture with a duplicated callee name (>1 candidate) asserting the minted PRECEDES edge carries candidate_count>1, confidence 0.4, tier SPECULATIVE — proving it is filtered by the 0.5 consumer gate, not laundered.

### 2.8 LOW — G05 over-claims "per-turn reindex ENABLED" on the codespace path

- **File:line:** `railway/codespace_deepswe_run.sh:143`; `artifact_deepswe/gt_mini_patch.py:2653`.
- **Avenue:** Integration. **Invariant:** correct-or-quiet (do not report a capability as ENABLED when its precondition is absent). **`is_rank_leak`: false. `is_regression`: false.**
- The codespace path injects NO graph.db into the AGENT container (builds `/tmp/gt/graph.db` on the HOST, exports GT_GRAPH_DB host-side for the brief only; no `--ae GT_GRAPH_DB`, no graph mount, no GT_PORTABLE_SUBSTRATE). In-container `_db_path()` resolves to a nonexistent `/tmp/graph.db`; L6 reindex is guarded by `os.path.isfile(db)` = False → never runs. The mounted binary is inert; "ENABLED" is misleading.
- **Recommendation:** Either inject the graph into the agent container on the codespace path (mount + `--ae GT_GRAPH_DB`, as full.yml does via substrate), or soften the echo to "binary mounted (reindex runs only when an in-container graph.db is present)."

### 2.9 LOW — G17 reader-count is class-granular but rendered as field-granular (over-attributes)

- **File:line:** `src/groundtruth/pretask/contract_map.py:366-369`.
- **Avenue:** Logic. **Invariant:** correct-or-quiet (accurate-or-quiet display). **`is_rank_leak`: false. `is_regression`: false.**
- In `_blast_facts`, the WRITES query selects (owning_class_id, field) per written field, but the reader-count query filters only `e.target_id = owner_class_id AND e.type = 'READS'` — NOT `e.metadata = field`. Since every promoted READS edge points at the owning Class node (promote.go:502), the count is distinct methods reading ANY field, not readers of the specific `<field>`. Rendered text "writes to <field>; N verified readers" overstates field-level precision when the class has multiple fields. NOT a rank leak, NOT name_match laundering (READS are genuine promoted facts at conf>=0.5). Tests use a single-field class (`_count`) so cannot catch this.
- **Recommendation:** Either (a) add `AND e.metadata = ?` (binding `field`) so the count is field-exact, or (b) change the render to class-scoped wording. Add a two-field-class fixture asserting a reader of a DIFFERENT field is not counted. Option (a) preferred.

### 2.10 NIT — unused `_PROMOTED_EDGE_TYPES` import in graph_reach.py

- **File:line:** `src/groundtruth/pretask/graph_reach.py:31-34`. Avenue: Implementation. Confirmed: the sole occurrence of `_PROMOTED_EDGE_TYPES` in graph_reach.py is the import line; only `_degree_edge_filter` is referenced. Trips ruff F401. The filter itself is correct. **Recommendation:** drop `_PROMOTED_EDGE_TYPES` from the import, or reference it.

### 2.11 NIT — resolveByName returns GLOBAL candidate count for a same-file match (over-suppression)

- **File:line:** `gt-index/internal/resolver/promote.go:415-419`. Avenue: Logic. A same-file PRECEDES match is demoted to SPECULATIVE (0.4) because the name also occurs in other files. Strictly correct-or-quiet (never laundered up), fully I2-isolated from rank. DATA_FLOW shares the same cc semantics — consistent existing behavior. **Recommendation (optional):** return a same-file-scoped candidate count so an unambiguous same-file PRECEDES keeps conf 0.5. Low priority.

### 2.12 NIT — redundant `global _l6_no_binary_warned` in `_invalidate_on_edit`

- **File:line:** `artifact_deepswe/gt_mini_patch.py:2642 and 2667`. Avenue: Implementation. Declared at function top and again in the `elif`. Harmless (same name/scope). Latch logic is correct (once-only, wrapped best-effort try/except). **Recommendation:** delete the inner declaration at 2667.

### 2.13 NIT — self-contradicting "Default-OFF" comment in gt_ae_block.sh

- **File:line:** `artifact_deepswe/gt_integration/gt_ae_block.sh:47-49`. Avenue: Logic. Lines 47-48 say "Default-OFF in-container... the HARNESS turns it ON" but line 49 hard-defaults to `${GT_VERIFY_STRUCTURAL_RISK:-1}` = ON. Benign on the current caller (codespace exports =1) but a future caller trusting the comment gets the axis ON. **Recommendation:** make the default match the comment (`:-0`) and drive it ON from the host export, OR fix the comment to "defaults ON; set =0 to disable."

---

## 3. I2 rank-safety line across ALL surfaces

**Did the I2 line (depth never enters reach/RANK) hold across every surface? NO.**

- **Exactly ONE** finding carries `is_rank_leak=true`: §2.1 — the `W_PROX` anchor-proximity leak (`anchor_proximity.py:36-48` → `v7_4_brief.py:788`). Per the cardinal rule, **any `is_rank_leak=true` is a release blocker.** It is verified live (W_PROX default 0.05, boosted to 0.12 at the function-level regime; the untyped SELECT admits five promoted depth classes at conf 1.0).
- Every other surface's I2 posture held: PRECEDES is incidentally rank-safe (demoted below the 0.7/0.5 gates); the harness `--ae` vars are SCOPE/RISK + telemetry, node-local-or-quiet, never rank terms; the brief G17 over-count and the delivery findings touch display/plumbing, not the rank surface; `graph_reach.py` itself was correctly typed by G01.
- The miss is precisely characterized: G01 fixed the reach term (`_total_score:783`) but the catalog and the fix both **stopped one term short** of the sibling prox term (`:788`), and the I2 test suite covers reach/expand but not proximity.

**Conclusion: the rank-leak count is 1 (target: 0). The release is BLOCKED on §2.1 until the D5 predicate is applied to `compute_anchor_proximity` and a proximity I2 regression test is added.**

---

## 4. Avenue coverage (LIPI completeness)

All four avenues exercised across the diff: Logic (§2.6, §2.9, §2.11, §2.13), Implementation (§2.7, §2.10, §2.12, NIT in §2.1), Integration (§2.2, §2.4, §2.8), Plumbing (§2.1, §2.3, §2.5). The two HIGH findings sit on independent avenues (Plumbing vs Integration), confirming the LIPI maxim that finding one avenue's bug does not clear the others.
