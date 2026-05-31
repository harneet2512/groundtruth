# PRODUCER_RUNTIME_SMOKE.md — Phase 4/6 runtime proof (local fixture, fresh graph.db)

Date 2026-05-31. Code under test: audited `eaa45b9c` + applied Table-1 (`47eecf43`).
Fixture: `.tmp_gt_architecture_audit/fixtures/pyfix/` (app/models.py, app/service.py, tests/test_models.py)
— a cross-file caller (`service.handle` → `models.get_user`), a **0-caller** `isolated_helper`, 2 tests.
Graph built **fresh** with `gt-index-t1t2.exe` (NOT stale local). No Docker / Go / dirty-file dependency.

**This proves AXIS-1 (logic correctness) at RUNTIME.** AXIS-2 (efficacy: hit@k, flips) stays a live-run question.

---

## Layer 0 — gt-index → graph.db : **PASS**
- `schema_version = v15.1-trust-tier`; `indexer_version = v16-multilang`.
  - ⚠ **DISCREPANCY:** DOC_OF_HONOR §0.6 claims `v15.2-trust-tier`; the actual binary writes **v15.1**. (doc-vs-code)
- 7 tables present; edges carry the categorical columns (`resolution_method, trust_tier, candidate_count, evidence_type, verification_status`) → the categorical filter **engages** (no numeric fallback).
- **Provenance correct:** all 3 callers of `get_user` resolved via `import` → `trust_tier=CERTIFIED, confidence=1.0, candidate_count=1` (FACTS). Build stats: `edges_import=3, edges_same_file=1, edges_name_match=0`.
- Assertions linked: 2 asserts → `get_user` (target_node_id=3).

## Layer 2.1 — L1 `generate_v1r_brief` : **PASS** (hygiene + provenance + Table-1 change)
Delivered text (verbatim, abbreviated):
```
<gt-task-brief>
1. app/models.py (def get_user(...)-> Optional[User], ..., def isolated_helper()->None)
   Witness: get_user called by User [CALLS]
   Contract: raises ValueError | preserve raise: user_id <= 0 -> raise ValueError(...); return: user_id==1 -> return User(1,"root") | returns value|User(1,"root")
   Callers: handle() in app/service.py:5 `user = get_user(uid)`
   Calls: app/service.py   Tests: tests/test_models.py
2. app/service.py (def handle(...)-> str) ...
Highest-confidence candidate (graph + issue signals): app/models.py — graph witness ... — covering test: tests/test_models.py
</gt-task-brief>
<gt-graph-map> app/models.py :: get_user  calls: User  called by: handle, test_..., test_... </gt-graph-map>
```
- `<gt-task-brief>` + `<gt-graph-map>` present.
- **Hygiene CLEAN:** 0 tier labels (`[VERIFIED]/[WARNING]/[INFO]`), 0 `[GT_META]/[GT_STATUS]/__GT_STRUCTURED__`, 0 `v22`/dead markers → runtime-proves `L1_TIER_AS_FILTER_004` + render-hygiene (claim group F).
- CERTIFIED import caller rendered as a **FACT with code** (`handle() in app/service.py:5`) — correct provenance → correct fact.
- Behavioral contract is **grounded** (real raises/returns, not a `body_len=` placeholder).
- Highest-confidence line **de-prescribed** (states evidence + witness + covering test; no "edit X").
- **Table-1 categorical change works:** brief renders correctly; the categorical filter admitted the CERTIFIED edges. **No regression.**
- FINDING: the em-dash in the highest-conf note renders as `�` on the **Windows cp1252 console** — display artifact only (UTF-8 container runtime unaffected). Delivery-encoding note, low concern.

## Layer 2.3 — L3b `graph_navigation` : **PASS** (Contract always-fire)
Returned lines (verbatim):
```
[CONTRACT] def get_user(user_id: int) -> Optional[User]:
[CONTRACT] def __init__(self, uid: int, name: str) -> None:
[CONTRACT] def isolated_helper() -> None:
Called by: tests/test_models.py:5 `assert get_user(2) is None` [model], app/service.py:5 `user = get_user(uid)` [service]
```
- `[CONTRACT]` ×3 **including the 0-caller `isolated_helper`** → Contract-pillar **ALWAYS-FIRE confirmed at runtime** (the CLAUDE.md:86 constitutional fix). Proves `L3B_CONTRACT_ALWAYSFIRE_001`.
- Callers categorical-filtered, with code + layer tags.
- **Return-path leak: CLEAN** (`[GT_META] contract_pillar...` correctly on **stderr**, not in returned lines).
  - NOTE: `__GT_STRUCTURED__` is emitted only by `post_view.main()` under `--structured-output`, NOT by `graph_navigation()`. The leak check for that marker belongs to the **wrapper dispatch** path → still open (next session).

## Layer 2.7 — L6 `gt-index -file` incremental reindex : **PASS** (no silent edge drop)
Reindex log: `{"file":"app/models.py","nodes_replaced":5,"edges_replaced":4,"incoming_restored":3,"incoming_unresolved":0,"duration_ms":29,"short_circuited":false}`
- **incoming_restored=3 / incoming_unresolved=0** → the 3 cross-file incoming CALLS edges into `get_user` were RESTORED after it got a new node id; **0 silently dropped** (claim group G). Proves `L0_INCREMENTAL_REINDEX_001` (reindex half).
- After reindex: `added_fn` node present; `get_user` has 4 incoming (3 restored CERTIFIED + new `same_file` `added_fn`).

---

## Runtime-PROVEN Axis-1 claims
`L0` schema/provenance/assertions · `L1` hygiene + tier-as-filter + fact-caller + graph-map + Table-1 no-regression · `L3b` Contract-always-fire (incl. 0-caller) · `L6` reindex + incoming_restored (no silent drop).

## NOT proven here (next session / blocked)
- `__GT_STRUCTURED__` split + wrapper render/strip — wrapper dispatch (`oh_gt_full_wrapper.py`, DIRTY).
- `os.walk` laundering — needs a stdlib-call fixture + the Go resolver (this fixture had `name_match=0`, so it didn't exercise it).
- Axis-2 efficacy (hit@k, flips) — Deep SWE live run.
- Multi-language (ts/go/rust/js) producers — Phase 7.

## Verdict (layers exercised): **L0 / L1 / L3b / L6 = PROVEN_RUNTIME_CORRECT** on the controlled fixture.

---

## P0 BUG CONFIRMED at runtime — `os.walk` → project `walk()` laundering (stdlib-shadow)
Fixture `.tmp_gt_architecture_audit/fixtures/shadowpkg/`: `account.walk(path)` (globally-unique name) + `scanner.scan()` calling `os.walk(root)`. Fresh index produced exactly ONE edge:
```
scanner.py.scan -> walk   CALLS   resolution_method=verified_unique   trust_tier=CERTIFIED   confidence=0.95   candidate_count=1   evidence_type=unique_name
```
**Headline Axis-1 logic bug, mechanically confirmed.** The call is to **stdlib `os.walk`**, but the Go resolver's **Strategy 1.9 `verified_unique`** (globally-unique name → CERTIFIED 0.95) fires because `walk` is unique in the project — it does NOT exclude a *qualified* `<module>.<name>` attribute call on an imported stdlib/third-party module. A false caller is stamped DETERMINISTIC/CERTIFIED, and every downstream consumer (L1 caller gate, L3/L3b categorical filter, `<gt-graph-map>`) trusts it as a fact. The v1r `_is_stdlib_shadow` guard only protects the **L1 brief render** — NOT the graph itself, L3/L3b, or the map.

**Exact fix locus (next session, Go/CI):** in the resolver, suppress `verified_unique` (and `type_flow`) when the call expression is `module.name(` and `module` is a known imported non-project module — i.e. a *qualified* attribute call is not a bare-name unique call. Repo-/language-agnostic: key off "qualified call on an imported non-project module," not a hardcoded stdlib list.
