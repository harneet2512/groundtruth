# spec: contract_map._read_props

**Invariant:** I1/I3 — a property mined BELOW the fact floor (confidence < 0.5) is not a
contract FACT and must not be delivered (correct-or-quiet). The contract pillar must be
confidence-gated like the curation pillar (Three Mandatory Properties).

- **READS:** `properties(kind, value, confidence, line)` for the contract kinds.
- **RETURNS:** `{kind: [value, ...]}` deduped, capped, line-ordered.

**Cases:**
1. a property with `confidence=0.8` (e.g. data_flow) → **kept**.
2. a property with `confidence=0.3` → **dropped** (below the fact floor).
3. legacy schema WITHOUT a `properties.confidence` column → **ungated/permissive**
   (preserves gt_gt behavior — graceful degrade, I5).

**Bug:** the SELECT had no confidence predicate. Fix: `AND COALESCE(confidence, 1.0) >= 0.5`
(NULL → permissive), with an OperationalError fallback to the ungated query for legacy
schemas. 0.5 is the universal fact floor (not a per-repo/per-language literal).
