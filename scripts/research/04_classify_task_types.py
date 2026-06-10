"""G6 Validation: Does task type predict which evidence GT should provide?

Classifies bugs from issue text into categories and checks whether gold file
properties differ systematically across categories. If they do, GT should
route different evidence types to different task types.

Uses both holdout_v1 (60 bugs, has graph.db) and mined_prs (100 bugs, no graph).
"""

import json
import re
from pathlib import Path
from collections import Counter, defaultdict

HOLDOUT_PATH = Path(__file__).resolve().parents[2] / "holdout_v1.jsonl"
MINED_PATH = Path(__file__).resolve().parents[2] / "research" / "big_repo_routing" / "mined_prs.jsonl"

# Task type keywords (from issue/PR text)
TYPE_PATTERNS = {
    "crash": [
        r'\b(?:crash|panic|segfault|abort|fatal|core dump|sigsegv)\b',
        r'\b(?:nil pointer|null pointer|nullptr|npe|nullref)\b',
        r'\b(?:index out of|out of bounds|overflow|underflow)\b',
    ],
    "wrong_output": [
        r'\b(?:wrong|incorrect|unexpected|bad) (?:result|output|value|behavior)\b',
        r'\b(?:should return|should be|expected .+ but got|returns? .+ instead)\b',
        r'\b(?:produces? wrong|gives? wrong|yields? incorrect)\b',
    ],
    "error_handling": [
        r'\b(?:error|exception|throw|raise|catch|handle|unhandled)\b',
        r'\b(?:error message|error code|stack trace|traceback)\b',
        r'\b(?:try/except|try/catch|error handling|fail gracefully)\b',
    ],
    "regression": [
        r'\b(?:regression|used to work|worked before|broke|broken after|since .+commit)\b',
        r'\b(?:no longer works|stopped working|was working|now fails)\b',
    ],
    "type_mismatch": [
        r'\b(?:type error|type mismatch|wrong type|incompatible type)\b',
        r'\b(?:cannot convert|cannot assign|type assertion|cast)\b',
        r'\b(?:generic|template|trait bound|impl|interface)\b.*\b(?:error|mismatch|fail)\b',
    ],
    "logic_error": [
        r'\b(?:logic|logical|off-by-one|off by one|fence ?post|boundary)\b',
        r'\b(?:condition|conditional|branch|case) .{0,20}(?:wrong|missing|incorrect)\b',
        r'\b(?:always|never) .{0,20}(?:true|false|executed|reached)\b',
    ],
    "missing_feature": [
        r'\b(?:add support|implement|missing|not supported|unimplemented)\b',
        r'\b(?:does not support|cannot|unable to)\b',
        r'\b(?:feature|enhancement|improvement)\b',
    ],
    "perf_issue": [
        r'\b(?:slow|performance|memory leak|timeout|hang|deadlock)\b',
        r'\b(?:oom|out of memory|high cpu|high memory|latency)\b',
    ],
}


def classify_task_type(text):
    """Classify a bug from its issue/PR text. Returns all matching types."""
    if not text:
        return ["unknown"]

    text_lower = text.lower()
    matches = []

    for task_type, patterns in TYPE_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, text_lower):
                matches.append(task_type)
                break

    return matches if matches else ["unclassified"]


def classify_gold_file_role(path):
    """Classify what kind of file the gold file is."""
    path_lower = path.lower()

    if any(ind in path_lower for ind in ["test", "spec", "_test", "test_"]):
        return "test"
    if any(ind in path_lower for ind in ["example", "sample", "demo", "tutorial", "docs_snippet"]):
        return "example"
    if any(ind in path_lower for ind in ["config", "settings", "conf.py", "setup.py", "toml"]):
        return "config"
    if any(ind in path_lower for ind in ["handler", "controller", "endpoint", "api", "route", "view"]):
        return "handler"
    if any(ind in path_lower for ind in ["util", "helper", "common", "misc", "tools"]):
        return "utility"
    if any(ind in path_lower for ind in ["model", "schema", "types", "struct", "entity"]):
        return "model"
    if any(ind in path_lower for ind in ["core", "engine", "runtime", "internal"]):
        return "core"
    if any(ind in path_lower for ind in ["cmd", "cli", "main", "entry"]):
        return "entrypoint"

    return "source"


def main():
    # Load both datasets
    holdout = []
    with open(HOLDOUT_PATH) as f:
        for line in f:
            r = json.loads(line)
            text = (r.get("issue_title", "") or "") + "\n" + (r.get("issue_body", "") or "")
            holdout.append({
                "bug_id": r.get("bug_id", f"{r['repo']}-{r.get('pr_number','')}"),
                "repo": r["repo"],
                "text": text,
                "gold_files": r.get("gold_files", []),
                "source": "holdout",
            })

    mined = []
    try:
        with open(MINED_PATH, encoding="utf-8") as f:
            for line in f:
                r = json.loads(line)
                text = (r.get("issue_title", "") or "") + "\n" + (r.get("issue_body", "") or "")
                mined.append({
                    "bug_id": f"{r['repo']}-PR{r['pr_number']}",
                    "repo": r["repo"],
                    "text": text,
                    "gold_files": r.get("gold_files", []),
                    "source": "mined",
                })
    except FileNotFoundError:
        pass

    all_bugs = holdout + mined

    print("=" * 70)
    print("G6 VALIDATION: Task Type Classification")
    print("=" * 70)
    print(f"Total bugs: {len(all_bugs)} (holdout={len(holdout)}, mined={len(mined)})")
    print()

    # Classify all bugs
    results = []
    for bug in all_bugs:
        types = classify_task_type(bug["text"])
        gold_roles = [classify_gold_file_role(gf) for gf in bug["gold_files"]]
        results.append({
            "bug_id": bug["bug_id"],
            "repo": bug["repo"],
            "source": bug["source"],
            "types": types,
            "primary_type": types[0],
            "gold_count": len(bug["gold_files"]),
            "gold_roles": gold_roles,
            "primary_role": Counter(gold_roles).most_common(1)[0][0] if gold_roles else "none",
        })

    # Type distribution
    total = len(results)
    type_counts = Counter()
    for r in results:
        for t in r["types"]:
            type_counts[t] += 1

    print("TASK TYPE DISTRIBUTION")
    print("-" * 70)
    for t, n in type_counts.most_common():
        print(f"  {t:<20}: {n:>3}/{total} ({n/total:.0%})")

    # Gold file role distribution
    role_counts = Counter(r["primary_role"] for r in results)
    print(f"\nGOLD FILE ROLE DISTRIBUTION")
    print("-" * 70)
    for role, n in role_counts.most_common():
        print(f"  {role:<20}: {n:>3}/{total} ({n/total:.0%})")

    # Cross-tabulation: task type x gold file role
    print(f"\nCROSS-TABULATION: Task Type -> Gold File Role")
    print("=" * 70)

    # Build contingency
    type_role_matrix = defaultdict(lambda: Counter())
    for r in results:
        for t in r["types"]:
            type_role_matrix[t][r["primary_role"]] += 1

    all_roles = sorted(role_counts.keys())
    header = f"{'Type':<20}" + "".join(f"{role:>10}" for role in all_roles)
    print(header)
    print("-" * len(header))

    for t, _ in type_counts.most_common():
        row = f"{t:<20}"
        total_t = sum(type_role_matrix[t].values())
        for role in all_roles:
            n = type_role_matrix[t][role]
            pct = n / total_t if total_t > 0 else 0
            row += f"{n:>5}({pct:.0%}) "
            # Keep 10 chars per column
        print(row)

    # Gold file count by type
    print(f"\nGOLD FILE COUNT BY TASK TYPE")
    print("-" * 70)
    for t, _ in type_counts.most_common():
        subset = [r for r in results if t in r["types"]]
        avg = sum(r["gold_count"] for r in subset) / len(subset)
        single = sum(1 for r in subset if r["gold_count"] == 1)
        multi = sum(1 for r in subset if r["gold_count"] > 1)
        print(f"  {t:<20}: avg={avg:.1f}, single={single}, multi={multi} ({multi/len(subset):.0%} multi-file)")

    # Per-dataset comparison
    print(f"\n{'='*70}")
    print("HOLDOUT vs MINED COMPARISON")
    print("=" * 70)

    for source_label, source_data in [("holdout", holdout), ("mined", mined)]:
        subset = [r for r in results if r["source"] == source_label]
        if not subset:
            continue
        n = len(subset)
        tc = Counter()
        for r in subset:
            for t in r["types"]:
                tc[t] += 1
        print(f"\n{source_label} (n={n}):")
        for t, count in tc.most_common():
            print(f"  {t:<20}: {count}/{n} ({count/n:.0%})")

    # Evidence routing recommendation
    print(f"\n{'='*70}")
    print("EVIDENCE ROUTING ANALYSIS")
    print("=" * 70)

    # For each task type, what would be the most useful evidence?
    type_evidence_map = {
        "crash": "Stack trace location -> callers + error handling patterns in siblings",
        "wrong_output": "Expected vs actual -> contract (return type) + test assertions (bonus)",
        "error_handling": "Error propagation -> callers that catch/handle + sibling error patterns",
        "regression": "What changed -> git precedent + callers affected by the change",
        "type_mismatch": "Type constraint -> contract (signature) + callers with type expectations",
        "logic_error": "Control flow -> sibling conditional patterns + caller expectations",
        "missing_feature": "Scope -> sibling implementations + callers to understand API surface",
        "perf_issue": "Hotspot -> callers (frequency) + sibling resource patterns",
        "unclassified": "General -> all evidence families equally weighted",
    }

    for task_type, _ in type_counts.most_common():
        subset = [r for r in results if task_type in r["types"]]
        n = len(subset)
        roles = Counter(r["primary_role"] for r in subset)
        top_role = roles.most_common(1)[0] if roles else ("none", 0)
        evidence = type_evidence_map.get(task_type, "general")

        print(f"\n  {task_type} (n={n}):")
        print(f"    Primary gold role: {top_role[0]} ({top_role[1]}/{n})")
        print(f"    Suggested evidence: {evidence}")

    # Validation verdict
    print(f"\n{'='*70}")
    print("VALIDATION VERDICT")
    print("=" * 70)

    # Check: do types actually predict different gold file properties?
    # If all types have the same gold role distribution, type routing is useless
    type_role_entropy = {}
    for t, _ in type_counts.most_common():
        if type_counts[t] < 5:
            continue
        subset = [r for r in results if t in r["types"]]
        roles = Counter(r["primary_role"] for r in subset)
        n = len(subset)
        # Compute concentration: fraction of bugs with the dominant role
        if roles:
            top_frac = roles.most_common(1)[0][1] / n
            type_role_entropy[t] = top_frac

    if type_role_entropy:
        avg_concentration = sum(type_role_entropy.values()) / len(type_role_entropy)
        baseline_concentration = role_counts.most_common(1)[0][1] / total

        print(f"  Baseline role concentration: {baseline_concentration:.0%} (most common role = {role_counts.most_common(1)[0][0]})")
        print(f"  Avg within-type concentration: {avg_concentration:.0%}")
        print(f"  Lift: {avg_concentration - baseline_concentration:+.0%}")

        if avg_concentration - baseline_concentration > 0.15:
            print(f"\n  G6 VALIDATED: Task types predict gold file roles (+{avg_concentration - baseline_concentration:.0%} lift)")
            print(f"  Evidence routing by task type is justified.")
        elif avg_concentration - baseline_concentration > 0.05:
            print(f"\n  G6 WEAK: Small lift (+{avg_concentration - baseline_concentration:.0%})")
            print(f"  Task types predict gold roles slightly better than baseline.")
            print(f"  Evidence routing may help but is not strongly justified.")
        else:
            print(f"\n  G6 NOT VALIDATED: No meaningful lift ({avg_concentration - baseline_concentration:+.0%})")
            print(f"  Task types do not predict gold file roles better than baseline.")
            print(f"  Uniform evidence strategy is correct.")
    else:
        print(f"  Insufficient data for validation (no type with >=5 bugs)")


if __name__ == "__main__":
    main()
