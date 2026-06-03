# Assertion Target Resolution Fix

## Problem
`resolveAssertionTarget()` at `cmd/gt-index/main.go:755` resolves 0% of assertions on real repos. 16,971 assertions extracted but all have `target_node_id=0`.

## Root Causes

### 1. Uniqueness constraint too strict (lines 767, 781, 818)
`len(ids) == 1` requires the function name to be globally unique. Common names like `get`, `set`, `run`, `validate` have multiple definitions.

**Fix:** Change `len(ids) == 1` to `len(ids) <= 3` with same-package preference. When multiple candidates exist, prefer the one in the same directory as the test or in the directory the test imports from.

### 2. `extractCalledFunctions()` misses Python assert patterns (line 828)
The function looks for `func(args)` call patterns. But Python assertions use:
- `assert isinstance(token, str)` — `isinstance` is in skip list, nothing extracted
- `assert payload["user_id"] == 42` — no function call at all
- `pytest.raises(ValueError)` — `pytest` extracted but not useful

**Fix:** Add a Strategy 0 before Strategy 1: for Python assertions, extract the subject of the assertion:
- `assert X.Y(...)` → extract `Y`
- `assert X["key"]` → extract `X` (the variable being subscripted)
- `pytest.raises(ExcType)` with context `with pytest.raises(...): func()` → extract `func`

### 3. Same-directory matching too narrow (lines 808-811)
Only matches exact directory, `_test` suffix, `/tests` suffix, `tests/` prefix. Misses common patterns like `test/unit/` vs `src/`.

**Fix:** Add parent-directory matching: if test is in `tests/unit/auth/` and production code is in `src/auth/`, match on the shared `auth/` component.

## Changes Required

```go
// Line 767: relax uniqueness
-if ids, ok := nameToNodeIDs[fname]; ok && len(ids) == 1 {
+if ids, ok := nameToNodeIDs[fname]; ok && len(ids) <= 3 {
+    // Prefer same-package candidate
+    if len(ids) == 1 {
         return ids[0]
+    }
+    // Multiple candidates: pick same directory
+    bestID := pickSamePackage(ids, allNodes, nodeDBIDs, testDir)
+    if bestID > 0 {
+        return bestID
+    }
 }

// Same pattern at lines 781 and 818
```

## Requires
- Go 1.22+, CGO_ENABLED=1, GCC
- Rebuild: `cd gt-index && CGO_ENABLED=1 go build -tags sqlite_fts5 -o gt-index ./cmd/gt-index/`
- Test: run on beancount repo, verify "Assertion targets resolved: >50%"
