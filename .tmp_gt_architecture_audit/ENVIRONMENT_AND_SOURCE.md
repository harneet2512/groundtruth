# ENVIRONMENT_AND_SOURCE.md — GT Architecture-to-Output Correctness Audit (Phase 0)

Generated: 2026-05-31. Audit root: `.tmp_gt_architecture_audit/`
Principle: **nothing local, all GitHub** — audit the PUSHED committed code (`eaa45b9c`), NOT the dirty working tree.

---

## 0. Audit scope — TWO AXES, never conflated  (user clarification, 2026-05-31)

**This audit proves AXIS 1 only. AXIS 2 is deferred to the live Deep SWE run.**

- **AXIS 1 — LOGIC CORRECTNESS (this audit's verdict surface).** For each layer: is the mechanism implemented exactly per architecture; is the path *live* (not dead / flag-off); do its CLAIMS verify true against source/graph/git on controlled fixtures; is it correct-or-quiet; is it plumbed to the next consumer; no hidden-diagnostic leak; **no name_match laundered as fact**. "Check the logic too" means: scrutinize the *ranking / gating / SQL logic itself* — not merely that the layer fired and delivered.
- **AXIS 2 — ALGORITHMIC EFFICACY (LIVE RUN ONLY — NOT decided here).** Is the localization/ranking algorithm actually GOOD ENOUGH — hit@1/3/5, first_gold_rank, flips — across real tasks. Only a live benchmark run answers this. **The architecture audit must NOT declare localization "good"** — only "logic correct" or "bug X present."

**Corollaries (load-bearing):**
1. A layer can PASS Axis 1 (applied + plumbed + logic sound) yet be weak on Axis 2 (algorithm not good enough). The audit reports the former, defers the latter.
2. A layer can be "applied" (wired, fires, delivers) yet FAIL Axis 1 — when its LOGIC produces a *wrong* result. Exemplar: the `os.walk` → `account.walk` laundering (mechanism delivers, but the FACT is false). That is an Axis-1 LOGIC bug, caught here (see §E.3).
3. "Localization applied OK" ≠ "localization logic correct" ≠ "localization good enough." Three different statements; the audit owns the middle one.

---

## A. GT source (audited)

| Field | Value |
|---|---|
| Remote (`origin`) | `https://github.com/harneet2512/groundtruth.git` |
| Branch | `gt-consensus-curation` |
| **Audited SHA** | **`eaa45b9c45ac22694d3fe8ae048ba578c54c9bff`** (committed; == `origin/gt-consensus-curation`) |
| local HEAD == remote HEAD | YES |
| `eaa45b9c` content | `feat(gitpod)` — `.gitpod.yml` + railway scripts only; **touches NO GT product code** → audited product logic == `c6b44b65` |
| Dirty tracked (8, EXCLUDED from audit) | `incremental.go`, `oh_gt_full_wrapper.py`, `check_brief_delivery.py`, `post_edit.py`, `schemas.py`, + 3 tests. Two are producer/integration code → reading the working tree for architecture is delta-invariant, but Phase 4+ producer runs use a CLEAN `eaa45b9c` checkout. |

## B. Toolchain

| Field | Value |
|---|---|
| Python | 3.12.0 |
| Docker | client 29.5.2, daemon UP (`docker ps` ok, 0 containers) |
| OS / shell | Windows 11 / MINGW64 (Git-Bash) + PowerShell |
| gt-index binary | `gt-index/gt-index-t1t2.exe` (also `.tmp_railway_ctx/…t1t2.exe`, linux build in `.tmp_pretask/vm2_bundle/gt-index-linux`) |
| gt-index flags | `-root`, `-output`, `-file` (**incremental: re-index ONE file into existing graph.db = Layer 2.7 / L6**), `-max-files` (default 10000), `-workers`. **No `-version` flag.** |

## C. Graph schema (expected by current code)

- Version per docs: **`v15.2-trust-tier`** (DOC_OF_HONOR §0.1 `main.go:53` = v15.1; §0.6 = v15.2 after `assertions.resolution_score`). *To re-verify against a freshly built graph.db in Phase 4.*
- 7 tables: `nodes`(13 col), `edges`(12 col), `file_hashes`, `project_meta`, `properties`(6), `assertions`(7, +`resolution_score`), `cochanges`(3).
- `edges` carries the categorical signals the consumer filters use: `resolution_method` (same_file/import/verified_unique/type_flow/name_match…), `confidence`, `trust_tier` (CERTIFIED/CANDIDATE/SPECULATIVE/SUPPRESSED), `candidate_count`, `evidence_type`, `verification_status`. **Confirmed consumed** by `post_edit._categorical_edge_filter_clause` (post_edit.py:151) and reused by L3b (`post_view._edge_filter`).

## D. Architecture docs (Phase 1 source of truth)

| Doc | Date / branch | Role |
|---|---|---|
| `DOC_OF_HONOR.md` (1763 lines) | "last verified 2026-05-27 / `jedi__branch`" | Primary topological spec, file:line evidence. **PARTIALLY STALE** (line drift + §1.1). |
| `we_did.md` (560 lines) | "2026-05-28" living doc + `wire.md` 2026-05-29 corrections inlined | **CORRECTIVE — wins where later.** Layer-by-layer audit+fixes. |
| `docs/architecture/{HONORED_ARCHITECTURE,ARCHITECTURE_INVARIANTS,INTENT_FROM_DOC_OF_HONOR,TOPIC_INDEX_FROM_DOC_OF_HONOR}.md`, `GT_ARCHITECTURE_CONTRACT.md`, `ARCHITECTURE_CONFORMANCE.md` | — | Secondary / cross-reference. |

## E. Doc-vs-code discrepancies already found (Phase 1/2 seed)

1. **§1.1 `resolve_to_stored_path()` = `NOT_BUILT`** → **SUPERSEDED.** `we_did.md:148-166` records it BUILT 2026-05-28 (`src/groundtruth/index/path_resolver.py`); code confirms it is the canonical resolver USED by L3b (`post_view.py:230`) and L3 (`post_edit.py:70`), returning `None` on ambiguity → silent. **we_did.md corrective wins.** Status = IMPLEMENTED_LIVE.
2. **Line-number drift ~325 lines.** DOC cites `_host_graph_db`/`__post_init__` at `oh_gt_full_wrapper.py:414/422-424`; real current lines = **739 / 752-754**. Mechanism matches; citations stale. IMPLEMENTATION_MAP will use REAL current lines.
3. **`os.walk` → `account.walk` laundering — AXIS-1 LOGIC BUG, OPEN.** v1r caller gate logic is correct (`_caller_contract_for_file`, name_match never a fact), but the live brief still rendered an `os.walk` call as a confident `account.walk` caller FACT because graph.db tags that edge's `resolution_method` as DETERMINISTIC (not name_match), so the gate trusts a false provenance. Secondary defense `_is_stdlib_shadow` (v1r_brief.py:368) exists; primary fix locus = **Go indexer/resolver provenance**. NOT fixed (we_did.md:512-517; DOC §2.1 RUNTIME CAVEAT, run 26619606504).
4. **L4a:** DOC §2.4 "WORKING" → `we_did.md` + code confirm **RETIRED** (`_L4A_AUTO_QUERY_ENABLED=False`, oh_gt_full_wrapper.py:166). Subsumed by L3b.
5. **L2.8 L6 pre-submit:** DOC §2.8 finish-handler review = dead write → **REMOVED**, replaced by `_maybe_fire_presubmit_verify` at the edit→review transition (oh_gt_full_wrapper.py:1076). Corrective in we_did.md §2.8.

## F. Stop-condition check (Phase 0)

| Condition | Result |
|---|---|
| Architecture docs available | YES (DOC_OF_HONOR + we_did + docs/architecture/*) |
| Audited SHA pinned, local==remote | YES (`eaa45b9c`) |
| gt-index binary present + flags known | YES (incremental `-file` = L6) |
| Docker visible | YES (29.5.2, daemon up) |

→ No stop condition. Proceed to **Phase 1 — ARCHITECTURE_CLAIM_LEDGER.jsonl** (extract per-layer claims from DOC_OF_HONOR + we_did, mark corrective overrides), then **Phase 2 IMPLEMENTATION_MAP** using real current line numbers.
