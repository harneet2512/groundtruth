#!/usr/bin/env python3
"""Verify gt_hook.py feature parity across all 5 DeepSWE languages.

Runs gt_hook.py understand on 1 file per language from the local audit repos.
Counts evidence lines and checks for each of the 5 Python-AST features:
1. Contract (signature, return type, guard clauses)
2. Guard clause detection
3. Return shape analysis
4. Test assertion relay
5. Caller/callee evidence (graph-based, should work for all)

Outputs a score: features_working * languages = max 25.
Python baseline must not regress.
"""
import subprocess, sys, os, json, re
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HOOK = Path(r"D:\Groundtruth\benchmarks\swebench\gt_hook.py")
AUDIT = Path(r"D:\Groundtruth\artifact_deepswe\audit_repos")

# One file per language to test (must exist in the indexed repo with graph_t1t2.db)
TEST_FILES = {
    "python": {
        "repo": "dateutil-rfc5545-timezone-interop",
        "file": "src/dateutil/rrule.py",
        "db": "graph_t1t2.db",
    },
    "go": {
        "repo": "expr-try-catch-errors",
        "file": "compiler/compiler.go",
        "db": "graph_t1t2.db",
    },
    "typescript": {
        "repo": "ts-pattern-match-each",
        "file": "src/match.ts",
        "db": "graph_t1t2.db",
    },
    "javascript": {
        "repo": "testem-bail-on-test-failure",
        "file": "lib/app.js",
        "db": "graph_t1t2.db",
    },
    "rust": {
        "repo": "pest-character-class-coalescing",
        "file": "pest/src/parser_state.rs",
        "db": "graph_t1t2.db",
    },
}

FEATURES = [
    "callers",       # "Called by:" or "callers:" lines
    "contract",      # signature, return type, guard clause
    "guard",         # guard clause / precondition
    "test_relay",    # test file references or assertions
    "return_shape",  # return shape / return type info
]

FEATURE_PATTERNS = {
    "callers": re.compile(r"(call|caller|called by|referenced by|used by)", re.I),
    "contract": re.compile(r"(signature|contract|param|return.*type|guard|precondition)", re.I),
    "guard": re.compile(r"(guard|precondition|if.*raise|if.*return|early return|check)", re.I),
    "test_relay": re.compile(r"(test|assert|expect|verify|spec|_test\.|test_)", re.I),
    "return_shape": re.compile(r"(return|yields?|produces?|output|result type)", re.I),
}


def run_gt_hook(repo_path, file_path, db_path):
    """Run gt_hook.py understand and capture output.

    gt_hook.py discovers graph.db from --root, so we copy/symlink
    the graph_t1t2.db to graph.db in the repo root.
    """
    # Ensure graph.db exists at repo root (gt_hook.py looks for it there)
    target_db = Path(repo_path) / "graph.db"
    src_db = Path(db_path)
    if src_db.exists() and not target_db.exists():
        import shutil
        shutil.copy2(str(src_db), str(target_db))

    cmd = [
        sys.executable, str(HOOK),
        "understand", file_path,
        f"--root={repo_path}",
        "--quiet", "--max-lines=20",
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30,
            cwd=str(repo_path),
        )
        return result.stdout + result.stderr, result.returncode
    except subprocess.TimeoutExpired:
        return "TIMEOUT", -1
    except Exception as e:
        return f"ERROR: {e}", -1


def check_features(output):
    """Check which features are present in the output."""
    found = {}
    for feat, pattern in FEATURE_PATTERNS.items():
        found[feat] = bool(pattern.search(output))
    return found


def main():
    print("=" * 80)
    print("  GT_HOOK.PY FEATURE PARITY VERIFICATION")
    print("=" * 80)

    total_score = 0
    total_possible = len(TEST_FILES) * len(FEATURES)
    results = {}

    for lang, config in TEST_FILES.items():
        repo_path = AUDIT / config["repo"]
        db_path = repo_path / config["db"]
        file_path = config["file"]

        print(f"\n  {'-'*60}")
        print(f"  {lang.upper()}: {config['repo']}")
        print(f"  File: {file_path}")
        print(f"  DB: {db_path.name}")

        if not db_path.exists():
            print(f"  SKIP: graph.db not found")
            results[lang] = {"features": {f: False for f in FEATURES}, "lines": 0, "error": "no_db"}
            continue

        if not (repo_path / file_path).exists():
            # Try to find a real file
            print(f"  WARN: {file_path} not found, searching...")
            found = None
            ext_map = {"python": ".py", "go": ".go", "typescript": ".ts", "javascript": ".js", "rust": ".rs"}
            ext = ext_map.get(lang, "")
            for f in sorted(repo_path.rglob(f"*{ext}"))[:20]:
                rel = f.relative_to(repo_path)
                if "test" not in str(rel).lower() and "vendor" not in str(rel).lower() and "node_modules" not in str(rel):
                    found = str(rel).replace("\\", "/")
                    break
            if found:
                file_path = found
                print(f"  Using: {file_path}")
            else:
                print(f"  SKIP: no suitable file found")
                results[lang] = {"features": {f: False for f in FEATURES}, "lines": 0, "error": "no_file"}
                continue

        output, rc = run_gt_hook(str(repo_path), file_path, str(db_path))
        lines = [l for l in output.strip().split("\n") if l.strip()] if output.strip() else []
        evidence_lines = len(lines)

        features = check_features(output)
        lang_score = sum(1 for v in features.values() if v)
        total_score += lang_score

        results[lang] = {"features": features, "lines": evidence_lines, "rc": rc}

        print(f"  Exit code: {rc}")
        print(f"  Evidence lines: {evidence_lines}")
        print(f"  Features ({lang_score}/{len(FEATURES)}):")
        for feat, present in features.items():
            tag = "YES" if present else "NO "
            print(f"    [{tag}] {feat}")

        if evidence_lines > 0 and evidence_lines <= 10:
            print(f"  Output preview:")
            for line in lines[:5]:
                print(f"    {line[:100]}")

    # Summary
    print(f"\n\n{'='*80}")
    print(f"  PARITY SCORE: {total_score}/{total_possible}")
    print(f"{'='*80}")

    print(f"\n  {'Language':<14s} {'Lines':>6s} {'callers':>8s} {'contract':>9s} {'guard':>6s} {'test':>5s} {'return':>7s} {'Score':>6s}")
    print(f"  {'-'*62}")
    for lang in TEST_FILES:
        r = results.get(lang, {})
        feats = r.get("features", {})
        lines = r.get("lines", 0)
        score = sum(1 for v in feats.values() if v)
        row = f"  {lang:<14s} {lines:>6d}"
        for f in FEATURES:
            tag = "Y" if feats.get(f) else "N"
            row += f" {tag:>8s}"
        row += f" {score:>5d}/5"
        print(row)

    print(f"\n  TOTAL: {total_score}/{total_possible}")
    if total_score == total_possible:
        print(f"  VERDICT: FULL PARITY")
    else:
        missing = total_possible - total_score
        print(f"  VERDICT: {missing} GAPS remaining")

    # Write JSON for tracking
    out = Path("D:/Groundtruth/artifact_deepswe/parity_results.json")
    with open(out, "w") as f:
        json.dump({"score": total_score, "total": total_possible, "results": results}, f, indent=2, default=str)
    print(f"\n  Results: {out}")

    sys.exit(0 if total_score >= total_possible else 1)


if __name__ == "__main__":
    main()
