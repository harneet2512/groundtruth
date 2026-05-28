# Topic Dossier: L3 Test Evidence — Naming Convention Fallback

**Source:** DOC_OF_HONOR §2.2 priority 3, §0.6 Assertion Resolution
**Risk level:** MEDIUM — flexget had 0 [TEST] despite test_qbittorrent.py existing

## 1. DOC_OF_HONOR Intent
Test assertions from graph.db, ranked by issue-keyword overlap. 2-hop fallback.
File-grep fallback. Anchor-based test discovery. Depends on P5 assertion linking.

## 2. Current Branch Bug
File-grep fallback (`_get_test_assertions_from_file`) depends on graph edges to
find test files. If assertion linking (§0.6 multi-signal scoring, threshold 3.5)
didn't link test functions to the edited function, file-grep has zero files to
search. All three sub-stages (function-name, issue-term, anchor) iterate over
an empty `rows` list.

## 3. jedi__branch
Same code, same bug.

## 4. Trajectory Evidence
- flexget: 0 [TEST] injections. test_qbittorrent.py::test_ratio_limit exists.
  Assertion linking likely failed: test_ratio_limit → add_entries doesn't score
  above 3.5 (naming convention doesn't match, no direct import).
- sh-744: 6 [TEST] (working — assertion linking succeeded)
- arviz: 3 [TEST] (working)
- cfn-lint: 6 [TEST] (working)

## 5. Research
- TCTracer ICSE 2020: naming convention signal (test_foo→foo, weight 2.0)
- RepoGraph ICLR 2025: test functions via is_test flag, k-hop dynamic
- Agentless ICLR 2025: tests supplemental not gating
- C21 category: 1,168 bugs from weak test→impl mapping

## 6. Gap
DOC says file-grep fallback exists. But it's graph-edge-dependent. When edges
are missing (assertion resolution < 3.5), fallback is unreachable. The TCTracer
naming convention signal is used at index time but not at discovery time.

## 7. Fix
New function `_discover_test_files_by_convention()` at post_edit.py:1371.
Searches graph.db nodes (is_test=1) for files matching test_<stem>.py pattern.
Graph-independent — doesn't need edges. Wired into _get_test_assertions_from_file
as fallback when graph-edge query returns empty.

## 8. Tests
tests/invariants/test_test_discovery_naming_convention.py — 5 tests:
- flexget: test_qbittorrent.py found by stem match (no edges needed)
- pypsa: test_statistics.py NOT found (stem doesn't match expressions)
- no-match: unrelated test files not returned
- file-grep integration: assertions found via convention path
