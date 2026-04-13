#!/usr/bin/env python3
"""Compare smoke test results against full v1.0.4 history."""
import json
import glob
import sys

# Historical data from GT_V104_COMPLETE_RUN_ANALYSIS.md (Qwen3-Coder + GPT-5-nano)
HISTORY = {
    "BL":  {12907:1,13033:0,13236:0,13398:0,13453:1,13579:1,13977:0,14096:1,14182:0,14309:1},
    "v1":  {12907:1,13033:0,13236:0,13398:0,13453:1,13579:1,13977:0,14096:1,14182:0,14309:1},
    "v2":  {12907:1,13033:0,13236:1,13398:0,13453:1,13579:1,13977:0,14096:1,14182:0,14309:1},
    "v3":  {12907:1,13033:0,13236:0,13398:0,13453:1,13579:0,13977:0,14096:0,14182:0,14309:1},
    "v4":  {12907:1,13033:0,13236:0,13398:0,13453:1,13579:1,13977:0,14096:0,14182:0,14309:1},
    "v5":  {12907:1,13033:0,13236:1,13398:0,13453:1,13579:1,13977:0,14096:0,14182:0,14309:1},
    "v6":  {12907:1,13033:0,13236:0,13398:0,13453:1,13579:1,13977:0,14096:1,14182:0,14309:1},
}

TASKS = [12907, 13033, 13236, 13398, 13453, 13579, 13977, 14096, 14182, 14309]

TASK_PATTERNS = {
    12907: "always-resolved",
    13033: "never-resolved",
    13236: "GT-flip (stochastic)",
    13398: "never-resolved",
    13453: "always-resolved",
    13579: "v3-regression-indicator",
    13977: "never-resolved",
    14096: "infra-dependent",
    14182: "never-resolved",
    14309: "always-resolved",
}


def load_eval_results(base_dir):
    """Load eval results from swebench report.json files."""
    results = {}
    for f in glob.glob(f"{base_dir}/*/report.json"):
        try:
            d = json.load(open(f))
            for instance_id, report in d.items():
                task_num = int(instance_id.split("-")[-1])
                results[task_num] = 1 if report.get("resolved", False) else 0
        except Exception:
            pass
    return results


def main():
    base = sys.argv[1] if len(sys.argv) > 1 else "."

    # Load new results
    for cond, label in [
        ("tools_only", "DS-tools"),
        ("hybrid_v1", "DS-v1"),
        ("hybrid_v2", "DS-v2"),
    ]:
        path = f"logs/run_evaluation/{cond}/openai__deepseek-v3"
        results = load_eval_results(path)
        if results:
            HISTORY[label] = results

    versions = list(HISTORY.keys())

    # Print matrix
    print("\n=== FULL REGRESSION MATRIX ===\n")
    hdr = f"{'Task':>7} | " + " | ".join(f"{v:>8}" for v in versions) + " | Pattern"
    print(hdr)
    print("-" * len(hdr))

    for t in TASKS:
        row = f"{t:>7} | "
        bl_val = HISTORY["BL"].get(t, 0)
        for v in versions:
            val = HISTORY[v].get(t, -1)
            if val == -1:
                marker = "?"
            elif val:
                marker = "Y" if val == bl_val else "+Y+"  # gained vs BL
            else:
                marker = "N" if val == bl_val else "-N-"  # lost vs BL
            row += f"{marker:>8} | "
        row += TASK_PATTERNS.get(t, "")
        print(row)

    print("-" * len(hdr))
    totals = f"{'TOTAL':>7} | "
    for v in versions:
        total = sum(HISTORY[v].get(t, 0) for t in TASKS)
        totals += f"{total:>8} | "
    print(totals)

    # Key differences
    print("\n=== CHANGES FROM BASELINE ===\n")
    for v in versions:
        if v == "BL":
            continue
        gained = [t for t in TASKS if HISTORY[v].get(t, 0) and not HISTORY["BL"].get(t, 0)]
        lost = [t for t in TASKS if not HISTORY[v].get(t, 0) and HISTORY["BL"].get(t, 0)]
        total = sum(HISTORY[v].get(t, 0) for t in TASKS)
        delta = total - 5
        sign = "+" if delta > 0 else ""
        print(f"  {v:>10}: {total}/10 ({sign}{delta}) ", end="")
        if gained:
            print(f"GAINED {gained} ", end="")
        if lost:
            print(f"LOST {lost} ", end="")
        if not gained and not lost:
            print("identical to BL", end="")
        print()

    # Regression check
    print("\n=== REGRESSION CHECK ===\n")
    new_conds = [v for v in versions if v.startswith("DS-")]
    for v in new_conds:
        print(f"  {v}:")
        issues = []
        for t in TASKS:
            bl_val = HISTORY["BL"].get(t, 0)
            new_val = HISTORY[v].get(t, -1)
            if new_val == -1:
                continue
            pattern = TASK_PATTERNS.get(t, "")
            if bl_val and not new_val:
                issues.append(f"    *** REGRESSION: {t} ({pattern}) was resolved in BL, now FAILED ***")
            elif not bl_val and new_val:
                issues.append(f"    +++ NEW RESOLVE: {t} ({pattern}) was failed in BL, now RESOLVED +++")
        if issues:
            for i in issues:
                print(i)
        else:
            print("    No regressions, no new resolves vs BL")


if __name__ == "__main__":
    main()
