#!/usr/bin/env python3
"""Parse all DeepSWE task.toml files into a single repo_manifest.json."""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore[no-redef]

TASKS_DIR = Path(__file__).resolve().parent.parent.parent / "deepswe-bench" / "tasks"


def main() -> None:
    if not TASKS_DIR.is_dir():
        print(f"ERROR: tasks dir not found at {TASKS_DIR}", file=sys.stderr)
        sys.exit(1)

    tasks: list[dict] = []
    lang_counter: Counter[str] = Counter()
    repos: set[str] = set()

    for task_dir in sorted(TASKS_DIR.iterdir()):
        toml_path = task_dir / "task.toml"
        if not toml_path.exists():
            continue

        with open(toml_path, "rb") as f:
            data = tomllib.load(f)

        meta = data.get("metadata", {})
        env = data.get("environment", {})
        agent = data.get("agent", {})

        task_id = meta.get("task_id", task_dir.name)
        language = meta.get("language", "unknown")
        repo_url = meta.get("repository_url", "")
        commit = meta.get("base_commit_hash", "")
        docker_image = env.get("docker_image", "")
        category = meta.get("category", "")
        title = meta.get("display_title", "")
        timeout = agent.get("timeout_sec", 5400)

        instruction_path = task_dir / "instruction.md"
        instruction_chars = instruction_path.stat().st_size if instruction_path.exists() else 0

        tasks.append({
            "instance_id": task_id,
            "repo_url": repo_url,
            "language": language,
            "commit_hash": commit,
            "docker_image": docker_image,
            "category": category,
            "display_title": title,
            "agent_timeout_sec": timeout,
            "instruction_chars": instruction_chars,
        })

        lang_counter[language] += 1
        if repo_url:
            repos.add(repo_url)

    manifest = {
        "benchmark": "DeepSWE",
        "source": "https://github.com/datacurve-ai/deep-swe",
        "total_tasks": len(tasks),
        "total_repos": len(repos),
        "language_distribution": dict(lang_counter.most_common()),
        "tasks": tasks,
    }

    out_path = Path(__file__).resolve().parent.parent / "repo_manifest.json"
    with open(out_path, "w") as f:
        json.dump(manifest, f, indent=2)

    print(f"Wrote {len(tasks)} tasks from {len(repos)} repos to {out_path}")
    print(f"Languages: {dict(lang_counter.most_common())}")


if __name__ == "__main__":
    main()
