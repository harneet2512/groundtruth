# spec: v1r_brief._issue_relevant_neighbors + the generate_v1r_brief 1-hop expansion

**Invariant:** I2 / I6 — the call-neighbor surface ("callers/callees", the brief's
"Calls:" / candidate expansion) is defined by **CALLS edges only**. A promoted edge
(DATA_FLOW standalone, CO_SERIALIZES) is NOT a call and must not add a neighbor. Restores
gt_gt behavior (before the promote pass these edge types did not exist); no silent
divergence.

- **READS:** edges (type='CALLS'), nodes (file_path, language, is_test).
- **RETURNS:** neighbor file paths (callers + callees), issue-ranked.
- Reference sibling: `_static_callees:455` is already `type='CALLS'`-scoped; these two
  paths are the inconsistent siblings.

**Cases (>=2 languages):**
1. (py) file A has a CALLS edge to file B → B is a neighbor.
2. (go) file A is connected to file C ONLY by a promoted CO_SERIALIZES / standalone
   DATA_FLOW edge → C is **NOT** a neighbor.
3. abstain: file with no CALLS neighbors → [].

**Bug:** untyped `JOIN edges e ON … {conf_clause}` (no `type='CALLS'`) in both queries of
`_issue_relevant_neighbors` (`:403,:409`) and the 1-hop expansion (`:2906,:2911`) — the
depth landing silently broadened the call-neighbor surface to promoted edges. Fix: add
`AND e.type = 'CALLS'` to all four JOIN ON clauses.
