import json, os, glob, sys

results_dir = sys.argv[1]

for condition in ["baseline", "gt_v13"]:
    cdir = os.path.join(results_dir, condition)
    trajs = sorted(glob.glob(os.path.join(cdir, "*/*.traj.json")))

    print(f"\n=== {condition} ({len(trajs)} trajs) ===")

    if not trajs:
        continue

    # Check first traj for structure
    d = json.load(open(trajs[0]))
    info = d.get("info", {})
    model_stats = info.get("model_stats", {})
    print(f"model_stats keys: {list(model_stats.keys())[:20]}")
    print(f"model_stats sample: {json.dumps(model_stats, indent=2)[:500]}")

    messages = d.get("messages", [])
    print(f"messages count: {len(messages)}")
    if messages:
        print(f"message[0] keys: {list(messages[0].keys())}")

    # Aggregate model_stats across all trajs
    total_input = 0
    total_output = 0
    total_cost = 0
    total_turns = 0
    count = 0

    for t in trajs:
        try:
            d = json.load(open(t))
            ms = d.get("info", {}).get("model_stats", {})
            total_input += ms.get("instance_cost", 0) or ms.get("total_cost", 0) or ms.get("prompt_tokens", 0) or 0
            total_output += ms.get("completion_tokens", 0) or 0
            total_cost += ms.get("cost", 0) or ms.get("instance_cost", 0) or 0
            total_turns += len(d.get("messages", []))
            count += 1
        except:
            pass

    avg_turns = total_turns / max(count, 1)
    print(f"\nAggregated:")
    print(f"  Tasks: {count}")
    print(f"  Total turns: {total_turns}")
    print(f"  Avg turns/task: {avg_turns:.1f}")
    print(f"  Total cost (from model_stats): ${total_cost:.4f}")
    print(f"  Total input metric: {total_input}")
    print(f"  Total output metric: {total_output}")
