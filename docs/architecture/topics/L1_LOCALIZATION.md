# Topic Dossier: L1 Localization / Orientation / Edit Target

**Source:** DOC_OF_HONOR §2.1 (L1 Brief), §2.1+ (L1+ Edit Plan / Key Contracts)
**Risk level:** HIGH — 3/6 tasks got wrong edit target, 1/6 got wrong file entirely

---

## 1. DOC_OF_HONOR Intent

### L1 Brief (§2.1)
- Ranked file list with callers, callees, contracts, tests from graph.db
- Trigger: task initialization (wrapper startup)
- Agent sees: `<gt-task-brief>` with numbered file list
- Problem solved: orient the agent before it starts reading files

### L1+ Edit Target (§2.1+)
- Append `<gt-edit-target>` with Key function, signature, caller count
- Append `[GT KEY CONTRACTS]` with guard clauses, conditional returns
- Gates: pre-built graph.db exists, brief exists, not baseline
- Problem solved: narrow agent from file-level to function-level targeting

### DOC invariant (§ Phase 4, Batch 2):
> "high" requires `_direct AND _kw_overlap >= 3`. Common-part stopwords filtered.
> "Key function:" not "Edit X first". `<gt-orientation>` for fallback.

---

## 2. Current Branch Implementation

### File ranking: v1r_brief.py → v7_4_brief.py
- `src/groundtruth/pretask/v1r_brief.py` calls `run_v74()` from `v7_4_brief.py`
- v7.4 is a multi-signal scorer: W_SEM (0.15), W_LEX (0.50), W_REACH (0.05), W_PROX (0.05), W_HUB (0.10), W_PATH (0.45)
- **On GHA containers:** sentence-transformers unavailable → W_SEM=0 → BM25+path+graph only
- Stage A anchor selection via `anchor_select.py:_symbol_anchors()` — containment match: all word parts of function name must appear in issue text
- Stage B scoring produces ranked file list

### Edit target: oh_gt_full_wrapper.py:5846-5906
- Searches ONLY within brief's ranked files (`_l1_brief_files[:5]`)
- For each file: queries top 5 exported, non-test functions ordered by caller count DESC
- Scoring: `_direct` (name in issue text) → +1000; `_kw_overlap * 10`; `min(caller_count, 5)` as tiebreak
- Picks highest-scoring candidate across all brief files

### Critical bug: search space limitation
The edit target ONLY evaluates functions from files already in the brief.
If the brief misses the correct file, the edit target can never find the correct function.

### Known tests
- tests/invariants/: 111 tests pass, but none test issue-keyword → function matching
- tests/behavioral/test_evidence_behavioral.py: tests post-edit evidence, not localization

---

## 3. jedi__branch Implementation

Identical code in anchor_select.py and v7_4_brief.py. Same edit-target logic in wrapper.
Same bugs present in both branches.

**KEEP:** The multi-signal scoring architecture (v7.4) is sound.
**REPAIR:** The edit-target search space must expand beyond brief files.
**REPAIR:** The brief file ranking must weight issue-keyword function matches higher.

---

## 4. Runtime Trajectory Reality

### pypsa-1172 (HARMFUL misdirection)
- Issue text: `expanded_capacity(comps='Generator')` and `optimal_capacity(comps='Generator')`
- Brief ranked: networks.py, spatial.py, examples.py, io.py
- Actual target: `pypsa/statistics/expressions.py` (contains `expanded_capacity()` and `optimal_capacity()`)
- Edit target: `Network()` in networks.py (97 callers)
- Agent: 102 actions, never found correct file, EMPTY PATCH
- L1 event: all 4 candidates confidence=0.0, source=graph_db
- Consensus (e28): listed abstract.py as primary — closer but still not expressions.py

### flexget-4306 (wrong function, right file)
- Brief #1: qbittorrent.py (CORRECT)
- Edit target: `Session()` in requests.py (246 callers) — WRONG
- Agent: correctly ignored Session, went to qbittorrent.py, made close-but-wrong fix
- Issue points to ratio_limit handling in qbittorrent upload

### sh-744 (wrong function, trivially right file)
- Brief: sh.py (only source file — trivially correct)
- Edit target: `bake()` (23 callers) — WRONG. Fix is in `__await__()`
- Agent: ignored bake, found __await__ independently at e42
- Issue text literally mentions `__await__` and `_return_cmd`

### arviz-2413 (correct)
- Brief: hdiplot.py (CORRECT)
- Edit target: `plot_hdi()` (CORRECT)
- Key contracts preserved guard clause (CORRECT)

### weasyprint-2300 (correct file, wrong function harmless)
- Brief: flex.py (CORRECT for investigation, fix is in block.py)
- Edit target: `flex_layout()` (CORRECT for investigation)
- Agent traced from flex.py to block.py naturally

### cfn-lint-3875 (wrong file)
- Brief: Type.py, Properties.py, template.py, FindInMap.py — actual target `_language_extensions.py` not listed
- Edit target: `Properties()` — WRONG
- Agent took 60+ turns to independently find actual target

---

## 5. Research-Backed Ideal Behavior

### SweRank (ICLR 2025)
- Issue-keyword matching to function names is the primary localization signal
- Exact function name mention in issue text should be the strongest anchor

### Agentless (ICSE 2024)
- Two-stage localization: file-level then function-level
- Function-level uses issue text overlap, not caller count

### CodeScout (2026)
- Pre-exploration with issue-relevant file ranking improves resolution by 20%

### Implication
Issue-keyword → function name matching must be the PRIMARY signal for edit targeting.
Caller count is a structural signal that should be a TIEBREAKER, not the primary ranker.

---

## 6. Gap Analysis

### DOC intent vs current implementation
- DOC says "issue-named function beats high-caller functions" (Invariant 3, Batch 2)
- Code has this scoring at wrapper:5869-5878 BUT only within brief files
- If brief misses the file, invariant is unreachable

### DOC intent vs runtime reality
- pypsa: invariant violated — Network(97 cal) beat expanded_capacity which wasn't even evaluated
- flexget: invariant violated — Session(246 cal) selected, add_entries not scored
- sh: invariant violated — bake(23 cal) selected, __await__ not scored

### Current vs research ideal
- SweRank says: scan ALL function names against issue text FIRST, then rank files containing matches
- Current code: rank files first (v7.4), then scan functions within those files
- The order is backwards for issue-keyword localization

---

## 7. Invariant

**L1-INV-1: Issue-symbol supremacy**
If the issue text contains an exact function/class name that exists in graph.db,
the file containing that function MUST appear in the brief's top 5 files,
regardless of graph connectivity or hub score.

**L1-INV-2: Edit target issue-relevance**
The edit target function must have a higher issue-relevance score than any other
candidate. Caller count is tiebreaker only. If no function has issue relevance,
emit `<gt-orientation>` (file list), not `<gt-edit-target>` (authoritative function).

**L1-INV-3: Search space must include issue-matched files**
The edit-target search must scan functions from ALL issue-symbol-matched files,
not only from brief-ranked files.

---

## 8. Test Plan

### Invariant tests: tests/invariants/test_l1_issue_symbol_localization.py

1. **pypsa fixture:** graph.db has `expanded_capacity` in expressions.py and `Network` in networks.py (97 callers). Issue text mentions `expanded_capacity(comps='Generator')`. Expected: expressions.py outranks networks.py in brief. Edit target = expanded_capacity, not Network.

2. **flexget fixture:** graph.db has `add_entries` in qbittorrent.py and `Session` in requests.py (246 callers). Issue text mentions qbittorrent/ratio_limit context. Expected: qbittorrent.py primary. Edit target = add_entries (if issue-relevant), not Session.

3. **high-degree hub fixture:** graph.db has `my_func` (mentioned in issue) with 2 callers and `BigHub` with 500 callers. Expected: my_func's file outranks BigHub's file.

4. **no-match fixture:** issue has no graph-matchable symbols. Expected: no `<gt-edit-target>`, only `<gt-orientation>` with fallback file list.

5. **exact-name-in-issue fixture:** issue text contains `foo_bar()`. Graph has function `foo_bar` in `src/utils.py`. Expected: utils.py in top 3 regardless of other scores.

---

## 9. Minimal Repair Plan

### Change 1: Add issue-symbol file injection to edit-target search space
**File:** oh_gt_full_wrapper.py, around line 5853
**What:** Before the `for _bf in _l1_brief_files[:5]` loop, query graph.db for files containing functions whose names appear in the issue text (same logic as `_symbol_anchors` but directly in the wrapper). Add those files to `_l1_brief_files` so they are searched for edit targets.

### Change 2: In the wrapper edit-target code, scan issue-matched functions FIRST
**File:** oh_gt_full_wrapper.py, around line 5856
**What:** Query `SELECT id, name, file_path, signature, start_line FROM nodes WHERE name IN (?) AND is_test = 0` for all exact function names found in issue text. Score these with the existing scoring logic (direct=+1000). These candidates compete with brief-file candidates on equal footing.

### Change 3: No change to v7_4_brief.py file ranking
The file ranking already has symbol anchoring via `_symbol_anchors()`. The bug is that even if this produces an anchor, it may not survive the scoring to appear in top 5. The fix is to ensure issue-matched files bypass the v7.4 ranking for edit-target purposes.

### Files touched:
- oh_gt_full_wrapper.py (edit-target search space expansion)
- tests/invariants/test_l1_issue_symbol_localization.py (new)
- docs/architecture/HONORED_ARCHITECTURE.md (add invariant)

### Forbidden:
- No changes to v7_4_brief.py scoring weights
- No changes to post_edit.py, post_view.py, or any other layer
- No wrapper-wide rewrite

### Risk:
LOW — adding candidates to the search space cannot remove existing candidates.
The scoring logic is unchanged. Only the search space expands.

---

## 10. Commit Plan

One commit:
```
layer(gt): align L1 edit target with issue-symbol matching invariant

Add issue-symbol file injection: query graph.db for functions whose names
appear in the issue text, add their files to the edit-target search space.
This ensures issue-named functions compete with hub-connected functions
regardless of whether the v7.4 brief ranked their file in the top 5.

Invariant: if issue text names a function in graph.db, that function's
file MUST be searched for edit targeting. Caller count is tiebreaker only.
```
