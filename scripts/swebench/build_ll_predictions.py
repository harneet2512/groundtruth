#!/usr/bin/env python3
"""Assemble ONE predictions.jsonl for the official SWE-bench-Live evaluator from the
per-task trial artifacts (ll-full-<task>/), pinned to the full expected dataset so
empty/absent tasks count as unresolved (never silently dropped from the denominator).

Called by swebench_live_lite_full.yml (summarize job):
    build_ll_predictions.py <artifacts_dir> <out.jsonl> --expected <dataset.jsonl> --model <name>

<artifacts_dir> holds actions/download-artifact output: <artifacts_dir>/ll-full-<task>/...
Each task dir carries pred_<task>.jsonl ({instance_id, model_patch, model_name_or_path});
falls back to agent_patch.diff if the pred file is absent. Output records are the
SWE-bench standard {instance_id, model_name_or_path, model_patch}.
"""
import argparse, glob, json, os, sys


def _load_expected(path):
    ids = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            iid = json.loads(line).get("instance_id")
            if iid:
                ids.append(iid)
    # dedupe, preserve order
    seen, out = set(), []
    for i in ids:
        if i not in seen:
            seen.add(i); out.append(i)
    return out


def _patch_for_task(task_dir):
    """Return the model_patch string for a downloaded ll-full-<task> dir, or ''."""
    # preferred: the pre-built prediction record
    preds = glob.glob(os.path.join(task_dir, "pred_*.jsonl"))
    for p in preds:
        try:
            rec = json.loads(open(p, encoding="utf-8").readline())
            if rec.get("model_patch") is not None:
                return rec["model_patch"]
        except (OSError, ValueError):
            pass
    # fallback: the raw diff
    for name in ("agent_patch.diff", os.path.join("logs", "*", "patch.diff")):
        for d in glob.glob(os.path.join(task_dir, name)):
            try:
                txt = open(d, encoding="utf-8").read()
                if txt.strip():
                    return txt
            except OSError:
                pass
    return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("artifacts_dir")
    ap.add_argument("out")
    ap.add_argument("--expected", required=True, help="dataset jsonl; pins the denominator")
    ap.add_argument("--model", required=True, help="model_name_or_path for every record")
    args = ap.parse_args()

    expected = _load_expected(args.expected)
    if not expected:
        sys.exit(f"FATAL: no instance_ids in --expected {args.expected}")

    # map instance_id -> patch from whatever artifacts are present
    by_id = {}
    for task_dir in sorted(glob.glob(os.path.join(args.artifacts_dir, "ll-full-*"))):
        if not os.path.isdir(task_dir):
            continue
        task = os.path.basename(task_dir).split("ll-full-", 1)[-1]
        by_id[task] = _patch_for_task(task_dir)

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with_patch = empty = absent = 0
    with open(args.out, "w", encoding="utf-8") as out:
        for iid in expected:
            patch = by_id.get(iid, None)
            if iid not in by_id:
                absent += 1; patch = ""
            elif patch:
                with_patch += 1
            else:
                empty += 1
            out.write(json.dumps({
                "instance_id": iid,
                "model_name_or_path": args.model,
                "model_patch": patch or "",
            }) + "\n")

    print(f"predictions.jsonl: {len(expected)} records (pinned to --expected) | "
          f"with_patch={with_patch} empty={empty} absent_artifact={absent} | "
          f"artifacts_seen={len(by_id)} -> {args.out}")


if __name__ == "__main__":
    main()
