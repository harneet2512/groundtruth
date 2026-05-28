#!/usr/bin/env python3
"""Pre-flight checks for GT architecture invariants.

Run before any GHA canary or benchmark run to verify the codebase
is consistent with DOC_OF_HONOR and HONORED_ARCHITECTURE invariants.

Usage:
    python scripts/preflight_checks.py
    python scripts/preflight_checks.py --strict  # fail on warnings too

Exit codes:
    0 = all checks pass
    1 = critical check failed (do not deploy)
    2 = warning (deploy at your risk)
"""
import os
import re
import sys
import sqlite3
import subprocess

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STRICT = "--strict" in sys.argv

passed = 0
warned = 0
failed = 0


def check(name, condition, detail=""):
    global passed, failed
    if condition:
        print(f"  PASS  {name}")
        passed += 1
    else:
        print(f"  FAIL  {name}: {detail}")
        failed += 1


def warn(name, condition, detail=""):
    global passed, warned
    if condition:
        print(f"  PASS  {name}")
        passed += 1
    else:
        print(f"  WARN  {name}: {detail}")
        warned += 1


# ─── 1. Schema version ───

print("\n[1] Schema version")
main_go = os.path.join(REPO_ROOT, "gt-index", "cmd", "gt-index", "main.go")
if os.path.exists(main_go):
    with open(main_go, encoding="utf-8") as f:
        content = f.read()
    m = re.search(r'schemaVersion\s*=\s*"([^"]+)"', content)
    check("schema version exists", m is not None)
    if m:
        check("schema version is v15.2+", "v15.2" in m.group(1) or "v16" in m.group(1),
              f"got {m.group(1)}")
else:
    warn("main.go exists", False, "gt-index not found")

# ─── 2. Assertion resolution score column ───

print("\n[2] Assertion resolution_score column")
sqlite_go = os.path.join(REPO_ROOT, "gt-index", "internal", "store", "sqlite.go")
if os.path.exists(sqlite_go):
    with open(sqlite_go, encoding="utf-8") as f:
        content = f.read()
    check("assertions table has resolution_score", "resolution_score" in content)
    check("Assertion struct has ResolutionScore", "ResolutionScore" in content)
else:
    warn("sqlite.go exists", False)

# ─── 3. L1 edit-target issue-symbol injection ───

print("\n[3] L1 edit-target issue-symbol injection")
wrapper = os.path.join(REPO_ROOT, "scripts", "swebench", "oh_gt_full_wrapper.py")
if os.path.exists(wrapper):
    with open(wrapper, encoding="utf-8") as f:
        content = f.read()
    check("issue_symbol_files search exists",
          "_issue_symbol_files" in content,
          "L1-INV-1: issue-named functions must be in edit-target search space")
    check("consensus bridge exists",
          "brief_candidates.add" in content and "_issue_symbol_files" in content,
          "issue-symbol files must be added to brief_candidates")
else:
    warn("wrapper exists", False)

# ─── 4. L5b noise control ───

print("\n[4] L5b noise control")
if os.path.exists(wrapper):
    with open(wrapper, encoding="utf-8") as f:
        content = f.read()
    check("L5b cap at 2",
          "_l5b_injection_count" in content and ">= 2" in content,
          "L5B-INV-1: max 2 L5b firings per task")
    check("L5b relevance gate",
          "brief_candidates" in content and "l5b" in content.lower(),
          "L5B-INV-2: only suggest files in brief_candidates")
    check("L5b file dedup",
          "_l5b_suggested_files" in content,
          "L5B-INV-3: same file never suggested twice")

# ─── 5. L3b per-file-once dedup ───

print("\n[5] L3b per-file-once dedup")
if os.path.exists(wrapper):
    check("per-file-once gate",
          "l3b_file:" in content,
          "DEDUP-INV-1: L3b delivers evidence at most once per file between reindexes")
    check("reindex reset clears l3b gates",
          'l3b_file:' in content and 'stale_pfo' in content.lower() or '_stale_pfo' in content,
          "DEDUP-INV-1 hybrid: L6 reindex resets per-file-once gates")

# ─── 6. L3b callee suppression ───

print("\n[6] Callee suppression during exploration")
post_view = os.path.join(REPO_ROOT, "src", "groundtruth", "hooks", "post_view.py")
if os.path.exists(post_view):
    with open(post_view, encoding="utf-8") as f:
        pv_content = f.read()
    # Find graph_navigation function
    gn_start = pv_content.find("def graph_navigation(")
    gn_end = pv_content.find("\ndef ", gn_start + 1) if gn_start >= 0 else len(pv_content)
    gn_body = pv_content[gn_start:gn_end] if gn_start >= 0 else ""
    active_callee = [
        l.strip() for l in gn_body.split("\n")
        if "Calls into:" in l and "out.append" in l and not l.strip().startswith("#")
    ]
    check("callees suppressed in graph_navigation",
          len(active_callee) == 0,
          f"Rule 3: suppress callees during exploration. Found: {active_callee}")

# ─── 7. RAISES/CATCHES issue-keyword gate ───

print("\n[7] RAISES/CATCHES issue-keyword gate")
if os.path.exists(post_view):
    with open(post_view, encoding="utf-8") as f:
        pv_content = f.read()
    check("RAISES/CATCHES gated on error keywords",
          "_ERROR_KEYWORDS" in pv_content or "_issue_has_error_kw" in pv_content,
          "Rule 5: only emit exception evidence for error-related issues")

# ─── 8. L4a L3b dedup ───

print("\n[8] L4a suppression when L3b fired")
if os.path.exists(wrapper):
    check("L4a checks l3b_file key",
          "l3b_already_fired" in content or "l3b_file:" in content,
          "Rule 2: suppress L4a when L3b already fired for same file")

# ─── 9. U-shaped ordering with REVIEW first ───

print("\n[9] U-shaped ordering — REVIEW/PRESERVE first")
post_edit = os.path.join(REPO_ROOT, "src", "groundtruth", "hooks", "post_edit.py")
if os.path.exists(post_edit):
    with open(post_edit, encoding="utf-8") as f:
        pe_content = f.read()
    m = re.search(r'_PRIMACY\s*=\s*\(([^)]+)\)', pe_content)
    if m:
        primacy_str = m.group(1)
        check("PRESERVE in primacy list",
              "PRESERVE" in primacy_str,
              f"Rule 4: REVIEW/PRESERVE must precede SIGNATURE. Got: {primacy_str}")
        check("REVIEW in primacy list",
              "REVIEW" in primacy_str,
              f"Rule 4: REVIEW must be in primacy. Got: {primacy_str}")
    else:
        check("_PRIMACY tuple found", False, "U-shaped ordering not found in post_edit.py")

# ─── 10. Test naming convention fallback ───

print("\n[10] Test naming convention fallback")
if os.path.exists(post_edit):
    with open(post_edit, encoding="utf-8") as f:
        pe_content = f.read()
    check("_discover_test_files_by_convention exists",
          "_discover_test_files_by_convention" in pe_content,
          "TEST-INV-1: naming convention fallback for test discovery")

# ─── 11. Confidence thresholds ───

print("\n[11] Confidence thresholds")
if os.path.exists(post_view):
    with open(post_view, encoding="utf-8") as f:
        pv_content = f.read()
    conf_matches = re.findall(r'confidence.*>=\s*([\d.]+)', pv_content)
    check("all post_view confidence >= 0.7",
          all(float(c) >= 0.7 for c in conf_matches if float(c) > 0.5),
          f"Phase 4 B4: uniform 0.7. Found: {conf_matches}")

graph_map = os.path.join(REPO_ROOT, "src", "groundtruth", "brief", "graph_map.py")
if os.path.exists(graph_map):
    with open(graph_map, encoding="utf-8") as f:
        gm_content = f.read()
    conf_matches = re.findall(r'confidence.*>=\s*([\d.]+)', gm_content)
    check("graph_map confidence >= 0.7",
          all(float(c) >= 0.7 for c in conf_matches),
          f"Bug 10 fix: 0.7. Found: {conf_matches}")

# ─── 12. Run invariant tests ───

print("\n[12] Invariant + topology tests")
result = subprocess.run(
    [sys.executable, "-m", "pytest", "tests/invariants/", "tests/topology/", "-q", "--timeout=30"],
    capture_output=True, text=True, cwd=REPO_ROOT, timeout=120,
)
test_output = result.stdout.strip().split("\n")[-1] if result.stdout else "no output"
check("invariant tests pass",
      result.returncode == 0,
      f"exit={result.returncode}: {test_output}")

# ─── Summary ───

print(f"\n{'=' * 60}")
print(f"PREFLIGHT RESULTS: {passed} passed, {warned} warnings, {failed} failed")
print(f"{'=' * 60}")

if failed > 0:
    print("\nBLOCKED — fix failures before deploying")
    sys.exit(1)
elif warned > 0 and STRICT:
    print("\nBLOCKED (strict mode) — fix warnings before deploying")
    sys.exit(2)
elif warned > 0:
    print("\nPROCEED WITH CAUTION — warnings present")
    sys.exit(0)
else:
    print("\nALL CLEAR — safe to deploy")
    sys.exit(0)
