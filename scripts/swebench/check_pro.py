#!/usr/bin/env python3
"""Check SWE-bench Pro dataset availability and structure."""
from datasets import load_dataset

ds = load_dataset("ScaleAI/SWE-bench_Pro", split="test")
print(f"Tasks: {len(ds)}")
print("Columns:", ds.column_names)

for i in range(5):
    d = ds[i]
    print(f"  {d['instance_id']} | repo={d['repo']} | tag={d.get('dockerhub_tag', '?')}")

# Count by repo
langs = {}
for d in ds:
    repo = d["repo"]
    langs[repo] = langs.get(repo, 0) + 1

print("\nTasks by repo:")
for repo, count in sorted(langs.items(), key=lambda x: -x[1]):
    print(f"  {repo}: {count} tasks")

# Check which have Python files (for GT compatibility)
python_repos = set()
for d in ds:
    ps = d.get("problem_statement", "")
    if ".py" in ps or "python" in ps.lower():
        python_repos.add(d["repo"])
print(f"\nRepos mentioning Python: {python_repos}")
