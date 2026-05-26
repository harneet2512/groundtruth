#!/usr/bin/env python3
"""Find the 9 audit repos from the manifest."""
import json
from pathlib import Path

manifest_path = Path(__file__).resolve().parent.parent / "repo_manifest.json"
with open(manifest_path) as f:
    m = json.load(f)

targets = [
    "beevik/etree", "expr-lang/expr", "go-task/task",
    "gvergnaud/ts-pattern", "kysely-org/kysely", "arktypeio/arktype",
    "celery/kombu", "simonw/sqlite-utils", "dateutil/dateutil",
]

print("=== Audit repo candidates found in DeepSWE ===\n")
found = set()
for t in m["tasks"]:
    for name in targets:
        if name in t["repo_url"] and name not in found:
            found.add(name)
            print(f"  {t['language']:12s}  {t['instance_id']:50s}  {t['repo_url']}")
            print(f"  {'':12s}  commit: {t['commit_hash']}")
            print()

missing = set(targets) - found
if missing:
    print(f"NOT FOUND in DeepSWE: {missing}")
    print("\nAlternative repos by language:")
    for lang in ["go", "typescript", "python"]:
        repos = {}
        for t in m["tasks"]:
            if t["language"] == lang and t["repo_url"] not in repos:
                repos[t["repo_url"]] = t
        print(f"\n  {lang} repos ({len(repos)}):")
        for url, t in sorted(repos.items()):
            print(f"    {t['instance_id']:50s}  {url}")
