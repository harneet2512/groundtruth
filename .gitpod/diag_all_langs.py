#!/usr/bin/env python3
"""Deep 5-language diagnostic: trace import resolution pipeline per language.

For each language, reports:
1. Import prefix breakdown (internal vs external vs unresolvable)
2. Edge resolution method breakdown with confidence
3. The gap: imports that SHOULD resolve but DON'T
4. Sample unresolved internal imports (the smoking gun)
"""
import os
import re
import sqlite3
import subprocess
import sys

DBS = {
    "python": ("/tmp/gt_5lang/beets.db", "/tmp/gt_5lang/beets"),
    "go": ("/tmp/gt_5lang/crossplane.db", "/tmp/gt_5lang/crossplane"),
    "typescript": ("/tmp/gt_5lang/hono.db", "/tmp/gt_5lang/hono"),
    "rust": ("/tmp/gt_5lang/axum.db", "/tmp/gt_5lang/axum"),
    "javascript": ("/tmp/gt_5lang/marimo.db", "/tmp/gt_5lang/marimo"),
}

# Known external/stdlib prefixes per language (can never resolve)
EXTERNAL = {
    "python": {"os", "sys", "re", "json", "collections", "typing", "pathlib",
               "functools", "itertools", "abc", "io", "datetime", "copy",
               "unittest", "logging", "hashlib", "importlib", "contextlib",
               "dataclasses", "enum", "math", "string", "textwrap", "shutil",
               "tempfile", "warnings", "weakref", "operator", "struct",
               "traceback", "inspect", "platform", "pprint", "time",
               "concurrent", "threading", "subprocess", "sqlite3", "pickle",
               "configparser", "argparse", "optparse", "glob", "fnmatch"},
    "go": {"fmt", "os", "io", "net", "http", "context", "sync", "time",
            "strings", "bytes", "errors", "log", "path", "sort", "testing",
            "reflect", "encoding", "crypto", "regexp", "bufio", "flag",
            "strconv", "math", "runtime", "debug", "syscall", "unsafe"},
    "typescript": {},
    "rust": {"std", "core", "alloc"},
    "javascript": {},
}


def get_import_prefixes(repo_root, lang):
    """Extract import prefixes from source files."""
    if lang == "python":
        cmd = ["grep", "-rh", "^from \\|^import ", repo_root, "--include=*.py"]
    elif lang == "go":
        cmd = ["grep", "-rh", '^\t"', repo_root, "--include=*.go"]
    elif lang in ("typescript", "javascript"):
        cmd = ["grep", "-rh", "^import \\|from ['\"]", repo_root,
               "--include=*.ts", "--include=*.tsx", "--include=*.js", "--include=*.jsx"]
    elif lang == "rust":
        cmd = ["grep", "-rh", "^use ", repo_root, "--include=*.rs"]
    else:
        return {}

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        lines = (r.stdout or "").strip().split("\n")
    except Exception:
        return {}

    prefixes = {}
    for line in lines:
        line = line.strip()
        if not line:
            continue
        prefix = ""
        if lang == "python":
            m = re.match(r"(?:from|import)\s+([\w.]+)", line)
            if m:
                prefix = m.group(1).split(".")[0]
        elif lang == "go":
            m = re.search(r'"([^"]+)"', line)
            if m:
                parts = m.group(1).split("/")
                prefix = parts[0] if not "." in parts[0] else "/".join(parts[:3])
        elif lang in ("typescript", "javascript"):
            m = re.search(r"""from\s+['"]([^'"]+)['"]""", line)
            if m:
                path = m.group(1)
                if path.startswith("."):
                    prefix = "<relative>"
                elif path.startswith("@"):
                    prefix = path.split("/")[0] + "/" + path.split("/")[1] if "/" in path else path
                else:
                    prefix = path.split("/")[0]
        elif lang == "rust":
            m = re.match(r"use\s+(\w+)", line)
            if m:
                prefix = m.group(1)
        if prefix:
            prefixes[prefix] = prefixes.get(prefix, 0) + 1

    return prefixes


def classify_prefixes(prefixes, lang, repo_root):
    """Classify prefixes as internal, external, or stdlib."""
    internal = {}
    external = {}
    stdlib = {}
    for prefix, count in sorted(prefixes.items(), key=lambda x: -x[1]):
        if prefix in EXTERNAL.get(lang, set()):
            stdlib[prefix] = count
        elif lang == "python":
            # Check if prefix is a directory/package in repo
            if (os.path.isdir(os.path.join(repo_root, prefix)) or
                os.path.isfile(os.path.join(repo_root, prefix + ".py")) or
                os.path.isfile(os.path.join(repo_root, "src", prefix + ".py")) or
                os.path.isdir(os.path.join(repo_root, "src", prefix))):
                internal[prefix] = count
            else:
                external[prefix] = count
        elif lang == "go":
            # Internal if import path contains the repo module name
            if "/" in prefix and any(d in prefix for d in ["crossplane", "internal", "apis"]):
                internal[prefix] = count
            elif "/" not in prefix:
                stdlib[prefix] = count
            else:
                external[prefix] = count
        elif lang in ("typescript", "javascript"):
            if prefix == "<relative>" or prefix.startswith("."):
                internal[prefix] = count
            elif prefix.startswith("@") or prefix in ("react", "next", "node"):
                external[prefix] = count
            else:
                # Check if it's a local workspace package
                if os.path.isdir(os.path.join(repo_root, "packages", prefix)):
                    internal[prefix] = count
                else:
                    external[prefix] = count
        elif lang == "rust":
            if prefix in ("crate", "super", "self"):
                internal[prefix] = count
            elif prefix in EXTERNAL.get(lang, set()):
                stdlib[prefix] = count
            elif (os.path.isdir(os.path.join(repo_root, prefix)) or
                  os.path.isdir(os.path.join(repo_root, prefix.replace("_", "-")))):
                internal[prefix] = count
            else:
                external[prefix] = count

    return internal, external, stdlib


for lang, (db, root) in DBS.items():
    if not os.path.exists(db):
        print(f"\n{'='*70}\n=== {lang.upper()}: DB MISSING\n{'='*70}")
        continue

    c = sqlite3.connect(db)
    nc = c.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    ec = c.execute("SELECT COUNT(*) FROM edges").fetchone()[0]

    methods = c.execute(
        "SELECT resolution_method, COUNT(*), ROUND(AVG(confidence),2) "
        "FROM edges GROUP BY resolution_method ORDER BY COUNT(*) DESC"
    ).fetchall()

    high = c.execute("SELECT COUNT(*) FROM edges WHERE confidence >= 0.9").fetchone()[0]
    med = c.execute("SELECT COUNT(*) FROM edges WHERE confidence >= 0.5 AND confidence < 0.9").fetchone()[0]
    low = c.execute("SELECT COUNT(*) FROM edges WHERE confidence < 0.5").fetchone()[0]

    import_edges = sum(cnt for m, cnt, _ in methods if m == "import")
    name_match = sum(cnt for m, cnt, _ in methods if m == "name_match")
    nm_low = c.execute(
        "SELECT COUNT(*) FROM edges WHERE resolution_method='name_match' AND confidence < 0.5"
    ).fetchone()[0]

    c.close()

    prefixes = get_import_prefixes(root, lang)
    internal, external, stdlib = classify_prefixes(prefixes, lang, root)

    total_imports = sum(prefixes.values())
    internal_count = sum(internal.values())
    external_count = sum(external.values())
    stdlib_count = sum(stdlib.values())

    usable = high + med
    usable_pct = usable * 100 // ec if ec else 0
    import_rate = import_edges * 100 // internal_count if internal_count else 0

    print(f"\n{'='*70}")
    print(f"=== {lang.upper()} === nodes={nc} edges={ec} usable={usable_pct}%")
    print(f"{'='*70}")

    print(f"\n  EDGE RESOLUTION:")
    for m, cnt, conf in methods:
        pct = cnt * 100 // ec
        marker = " <<<" if m == "name_match" and conf < 0.5 else ""
        print(f"    {m:35s} {cnt:6d} ({pct:2d}%)  avg_conf={conf}{marker}")
    print(f"  Confidence: high={high} med={med} low={low}")
    print(f"  name_match below 0.5: {nm_low} ({nm_low*100//name_match if name_match else 0}% of name_match)")

    print(f"\n  IMPORT ANALYSIS ({total_imports} total source-level imports):")
    print(f"    Internal (resolvable):  {internal_count:4d} ({internal_count*100//total_imports if total_imports else 0}%)")
    print(f"    External (never):       {external_count:4d} ({external_count*100//total_imports if total_imports else 0}%)")
    print(f"    Stdlib (never):         {stdlib_count:4d} ({stdlib_count*100//total_imports if total_imports else 0}%)")
    print(f"    Import-verified edges:  {import_edges:4d}")
    print(f"    Resolution rate:        {import_rate}% of internal imports")

    if internal:
        print(f"    Top internal: {list(internal.items())[:8]}")
    if external:
        print(f"    Top external: {list(external.items())[:8]}")

    # Gap analysis
    gap = internal_count - import_edges
    if gap > 10:
        print(f"\n  GAP: {gap} internal imports NOT producing import-verified edges")
        if lang == "rust":
            print(f"    Likely cause: RegisterRustCratePaths drops glob workspace members")
            print(f"    Cross-crate imports (axum_core::, axum_extra::, etc.) not in file map")
        elif lang == "go":
            print(f"    Check: are Go module paths registered correctly in BuildFileMap?")
            print(f"    Check: does Strategy 3 split Go import paths correctly?")
        elif lang == "python":
            print(f"    Check: relative imports, namespace packages, __init__.py re-exports")
    elif gap <= 10:
        print(f"\n  GAP: ~{gap} — GOOD, import resolution is near ceiling")

    print(f"\n  VERDICT: ", end="")
    if usable_pct >= 80 and import_rate >= 25:
        print("GOOD — on par with baseline")
    elif usable_pct >= 70 and import_rate >= 15:
        print(f"FAIR — import resolution at {import_rate}%, name_match quality needs work")
    else:
        print(f"POOR — import resolution at {import_rate}%, major plumbing gap")
