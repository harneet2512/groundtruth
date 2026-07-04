import json, os, glob, sys

results_dir = sys.argv[1]

for condition in ["baseline", "gt_v13"]:
    cdir = os.path.join(results_dir, condition)
    trajs = sorted(glob.glob(os.path.join(cdir, "*/*.traj.json")))

    total_input = 0
    total_output = 0
    total_steps = 0
    total_cost = 0
    count = 0
    costs_found = False

    # Check first traj structure
    if trajs:
        d = json.load(open(trajs[0]))
        keys = list(d.keys())
        info_keys = list(d.get("info", {}).keys()) if "info" in d else []
        traj = d.get("trajectory", [])
        step_keys = list(traj[0].keys()) if traj else []
        obs_keys = []
        if traj and "observation" in traj[0]:
            obs = traj[0]["observation"]
            if isinstance(obs, dict):
                obs_keys = list(obs.keys())

        print(f"\n=== {condition} ===")
        print(f"Top keys: {keys}")
        print(f"Info keys: {info_keys[:15]}")
        print(f"Step keys: {step_keys}")
        print(f"Obs keys: {obs_keys}")
        print(f"Trajs found: {len(trajs)}")

        # Look for cost/token data anywhere
        flat = json.dumps(d)
        for keyword in ["cost", "token", "usage", "prompt_tokens", "completion_tokens", "total_tokens"]:
            if keyword in flat.lower():
                costs_found = True
                # Find it
                for key_path in ["info.total_cost", "info.cost", "info.tokens", "info.usage"]:
                    parts = key_path.split(".")
                    val = d
                    for p in parts:
                        val = val.get(p, {}) if isinstance(val, dict) else None
                        if val is None:
                            break
                    if val and val != {}:
                        print(f"  {key_path}: {val}")

        if not costs_found:
            print("  No cost/token data found in trajectories")
            # Check if there's usage in step observations
            for step in traj[:3]:
                obs = step.get("observation", {})
                if isinstance(obs, dict) and "usage" in str(obs):
                    print(f"  Found usage in observation")
                    break

    # Count steps across all trajs
    for t in trajs:
        try:
            d = json.load(open(t))
            steps = len(d.get("trajectory", []))
            total_steps += steps
            count += 1
        except:
            pass

    print(f"Tasks with trajectories: {count}")
    print(f"Total steps: {total_steps}")
    print(f"Avg steps/task: {total_steps/max(count,1):.1f}")
