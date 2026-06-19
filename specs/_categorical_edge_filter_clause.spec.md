# spec: post_edit._categorical_edge_filter_clause

**Invariant:** I3 — `name_match` is NEVER admitted as a fact, regardless of
candidate_count or confidence. A name_match edge is a NAME GUESS, not a verified edge
(CLAUDE.md P0 stdlib-shadow closure: `os.walk` → `account.walk` must never launder).

- **READS:** edge columns `resolution_method`, `trust_tier` (via the emitted SQL).
- **RETURNS:** a SQL boolean fragment, True iff the edge is a deterministic FACT.
- **WRITES/RAISES:** none.

**Admit iff:** `resolution_method ∈ strong (deterministic) set`, OR
`trust_tier ∈ {CERTIFIED, CANDIDATE}` **AND** `resolution_method != name_match`.
**AND** `trust_tier != SUPPRESSED` (hard exclude).

**Cases (≥2 languages):**
1. (py) `resolution_method='import'`, any tier non-SUPPRESSED → **admitted**.
2. (go) `resolution_method='name_match'`, `candidate_count=1`, `conf=0.95` → **NOT admitted** (the launder case the docstring wrongly claimed).
3. (ts) `resolution_method='name_match'`, `trust_tier='CERTIFIED'` → **NOT admitted** (the tier clause excludes name_match).
4. abstain: `trust_tier='SUPPRESSED'` with a strong method → **NOT admitted** (hard exclude).

**Bug:** docstring↔SQL drift — the docstring claims a `name_match cc<=1` admit the SQL
correctly does not implement; a "make code match docstring" edit would re-introduce the
launder. Fix the docstring + lock with a regression that fails if name_match is admitted.
