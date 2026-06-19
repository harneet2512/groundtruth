# LIPI — Logic, Implementation, Integration, Plumbing

LIPI is the mandatory 4-avenue bug diagnosis framework for GroundTruth.
When diagnosing ANY bug, check ALL four avenues. Do not stop at the first
one that looks wrong — bugs compound across avenues.

## The Four Avenues

### L — Logic
Is the algorithm correct?

- Wrong conditions, inverted checks, wrong sort order
- Wrong threshold, wrong weight, wrong comparison
- Does the formula do what the research says it should?
- Is the data structure appropriate?

Example: assertion sort order was `DESC` (longest first = worst assertions
shown). Should be `ASC` (shortest = most actionable).

### I — Implementation
Does the code do what the logic intends?

- Silent failures, swallowed exceptions
- Dead code paths, division by zero
- Wrong variable, off-by-one, missing await
- Connection leaks, type mismatches
- Resource exhaustion (file descriptors, memory)

Example: `_fts5_candidates` tried to CREATE TABLE on a read-only connection.
Silent sqlite3.Error caught, FTS5 never created, function returned [].

### P — Integration
Do the components connect correctly?

- Does the output of module A reach module B in the right format?
- Are there two code paths (e.g. router_v2 vs legacy) where one has the
  fix and the other doesn't?
- Does the caller match the callee's signature?
- Are the rendering paths consistent across layers?

Example: router_v2 L3b path had ZERO dedup checks. The per-file-once gate
only existed in the legacy path. Every file re-view re-delivered evidence.

### I — Plumbing
Does the data flow end-to-end?

- Is the data in the DB? Does the query SELECT the right columns?
- Is the file path normalized consistently (forward slash, relative)?
- Does the config persist across turns?
- Is the connection read-only when it needs to write?
- Does the schema version match between producer and consumer?

Example: `target_node_id = 0` for ~100% of assertions. The VERIFY query
filtered `WHERE target_node_id > 0` — returned nothing. The entire VERIFY
section was silently empty.

## How to Apply

### When diagnosing a bug
1. For EACH of the 4 avenues, state:
   - What you checked (file:line)
   - What you found (quote the code)
   - Whether it's broken (YES/NO)
   - If YES: the exact bug and fix
2. Even if avenue 1 explains the symptom, CHECK avenues 2-4
3. The diagnosis is COMPLETE only when all 4 avenues are checked

### When spawning diagnostic agents
- Each agent checks ALL 4 avenues for its assigned bug
- NOT one avenue per agent — one BUG per agent, four avenues each

### When the user says "lipi"
It means: ultrathink + diagnose across all 4 avenues + fix + verify the
fix doesn't break the other 3 avenues.

## Evidence: Why All 4 Matter

Session 2026-06-01 found 4 bugs in the GT pipeline. Each was a DIFFERENT
avenue:

| Bug | Avenue | What happened |
|-----|--------|---------------|
| Ranking wrong (test file #1) | Logic | `_walk_text_files` included test files, no filter |
| Duplicate observations | Integration | Router_v2 path bypassed dedup gate |
| VERIFY section empty | Plumbing | `target_node_id=0` for all assertions |
| gt-scope leaking internals | Implementation | Raw `resolution_method` in agent text |

If we had stopped at "Logic" (the ranking bug), the other 3 would have
shipped undetected. LIPI ensures nothing hides.

## The Rule

This is codified in `.claude/CLAUDE.md` under "LIPI — Mandatory 4-Avenue
Bug Diagnosis." It applies to every bug, every session, every worktree.
