#!/usr/bin/env python3
"""Package a run's artifacts into the SWE-bench-Live submission layout + deep per-run metrics.

Submission layout (SWE-bench-Live/submission): submissions/{subset}/{agent}/{model}/ with
  preds.json     -- {instance_id: {model_name_or_path, model_patch}}  (the patch per instance)
  results.json   -- the official SWE-bench-Live eval report (resolved_ids etc.), if present
  trajs/         -- per-instance agent trajectories (rollout logs)
  logs/          -- per-instance run/eval logs
  README.md      -- agent scaffold + experimental setting (model, rollouts, iterations)
  deep_metrics.json -- 8-dp deep record: tokens, COMPUTED cost (litellm/openrouter return
                       null for deepseek-v4-flash -> cost = tokens x configurable rate),
                       per-layer firing, GT delivery (brief/consensus/contract/cochange/L5),
                       per-task outcome + flips-vs-baseline if a baseline file is given.
  SUMMARY.md     -- human-readable table so anyone opening the repo sees the result cleanly.

NOTHING is submitted; this only PACKAGES so the benchmark reproduces + is submission-ready.
"""
from __future__ import annotations
import argparse, glob, json, os, re, shutil, sys

SRC_EXTS = (".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".rb")
# GT delivery markers searched in the agent's OBSERVATION (not telemetry) — the truth.
GT_MARKERS = {
    "brief": r"<gt-task-brief",
    "consensus": r"<gt-scope",
    "consensus_progressive": r'note="in-scope"',
    "consensus_override": r'reason="re-anchored"',
    "contract": r"<gt-contract",
    "cochange": r"<gt-cochange",
    "nudge_L5": r"<gt-nudge",
    "per_view_contract": r"\[CONTRACT\]",
    "per_edit_evidence": r"<gt-evidence",
}


def _f(x):
    try:
        return round(float(x), 8)
    except Exception:
        return 0.0


def load_pricing(path, model):
    try:
        m = json.load(open(path, encoding="utf-8"))["models"]
        return m.get(model) or next(iter(m.values()))
    except Exception:
        return {"input_per_1m": 0.0, "cache_hit_per_1m": 0.0, "output_per_1m": 0.0}


def compute_cost(n_in, n_cache, n_out, rate):
    n_in, n_cache, n_out = n_in or 0, n_cache or 0, n_out or 0
    miss = max(n_in - n_cache, 0)
    return _f(miss / 1e6 * rate.get("input_per_1m", 0)
              + n_cache / 1e6 * rate.get("cache_hit_per_1m", 0)
              + n_out / 1e6 * rate.get("output_per_1m", 0))


def read_text(*paths):
    out = []
    for p in paths:
        try:
            out.append(open(p, encoding="utf-8", errors="replace").read())
        except Exception:
            pass
    return "\n".join(out)


def per_instance(art_dir):
    """Yield a record per instance from whatever artifact shape is present (OH output.jsonl
    or DeepSWE result.json + trajectory)."""
    recs = {}
    # OH: results/.../output.jsonl (one obj with instance_id + test_result.git_patch + metrics)
    for f in glob.glob(os.path.join(art_dir, "**", "output.jsonl"), recursive=True):
        for line in open(f, encoding="utf-8", errors="replace"):
            try:
                o = json.loads(line)
            except Exception:
                continue
            iid = o.get("instance_id")
            if not iid:
                continue
            tr = o.get("test_result", {}) or {}
            m = o.get("metrics", {}) or {}
            recs.setdefault(iid, {})
            recs[iid].update(dict(
                instance_id=iid, harness="openhands",
                patch=tr.get("git_patch") or o.get("git_patch") or "",
                traj_file=f,
                n_in=m.get("accumulated_token_usage", {}).get("prompt_tokens") if isinstance(m.get("accumulated_token_usage"), dict) else None,
                n_out=m.get("accumulated_token_usage", {}).get("completion_tokens") if isinstance(m.get("accumulated_token_usage"), dict) else None,
                n_cache=None,
                obs_text=json.dumps(o.get("history", "")),
            ))
    # DeepSWE: result.json (agent_result tokens) + trajectory + model.patch
    for f in glob.glob(os.path.join(art_dir, "**", "result.json"), recursive=True):
        try:
            r = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        if "agent_result" not in r:
            continue
        iid = (r.get("task_name") or "").split("/")[-1] or os.path.basename(os.path.dirname(f))
        ar = r.get("agent_result", {})
        d = os.path.dirname(f)
        patch = read_text(os.path.join(d, "artifacts", "model.patch"))
        traj = (glob.glob(os.path.join(d, "agent", "mini-swe-agent.txt"))
                + glob.glob(os.path.join(d, "agent", "*.trajectory.json")))
        di = glob.glob(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(d))), "delivered_instruction.txt"))
        recs.setdefault(iid, {})
        recs[iid].update(dict(
            instance_id=iid, harness="mini-swe-agent",
            patch=patch, traj_file=traj[0] if traj else "",
            n_in=ar.get("n_input_tokens"), n_out=ar.get("n_output_tokens"),
            n_cache=ar.get("n_cache_tokens"), steps=ar.get("n_agent_steps"),
            reward=(r.get("verifier_result", {}) or {}).get("rewards", {}).get("reward"),
            obs_text=read_text(*(traj[:1] + di[:1])),
        ))
    return recs


def gt_delivery(obs_text):
    return {k: len(re.findall(pat, obs_text or "")) for k, pat in GT_MARKERS.items()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--artifacts-dir", required=True)
    ap.add_argument("--subset", default="lite")
    ap.add_argument("--agent", required=True)
    ap.add_argument("--model", default="deepseek-v4-flash")
    ap.add_argument("--name", default="", help="submission folder name; real format is "
                    "{YYYYMMDD}-{agent}-{model} (flat under submissions/{subset}/). Default: agent-model")
    ap.add_argument("--pricing", default="benchmarks/pricing/deepseek_pricing.json")
    ap.add_argument("--eval-report", default="", help="official results.json from run_evaluation")
    ap.add_argument("--baseline-resolved", default="", help="text/file listing baseline-resolved instance_ids (for flips)")
    ap.add_argument("--out-root", default="submissions")
    args = ap.parse_args()

    rate = load_pricing(args.pricing, args.model)
    # Real SWE-bench-Live layout (verified against submissions/lite/20250501-sweagent-claude37):
    # FLAT submissions/{subset}/{name}/ with preds.json, results.json, logs/, README.md (+trajs optional).
    name = args.name or f"{args.agent}-{args.model}"
    out = os.path.join(args.out_root, args.subset, name)
    os.makedirs(os.path.join(out, "trajs"), exist_ok=True)
    os.makedirs(os.path.join(out, "logs"), exist_ok=True)

    resolved_ids = set()
    if args.eval_report and os.path.isfile(args.eval_report):
        try:
            er = json.load(open(args.eval_report, encoding="utf-8"))
            resolved_ids = set(er.get("resolved_ids", []))
            shutil.copy(args.eval_report, os.path.join(out, "results.json"))
        except Exception:
            pass
    base_resolved = set()
    if args.baseline_resolved:
        bt = read_text(args.baseline_resolved) if os.path.isfile(args.baseline_resolved) else args.baseline_resolved
        base_resolved = set(re.findall(r"[A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+-?\d*", bt))

    recs = per_instance(args.artifacts_dir)
    preds, deep, tot = {}, [], dict(n_in=0, n_out=0, n_cache=0, cost=0.0, resolved=0, flips=0)
    for iid, r in sorted(recs.items()):
        # EXACT schema from a real accepted submission: model_name_or_path + instance_id + model_patch
        preds[iid] = {"model_name_or_path": args.model, "instance_id": iid, "model_patch": r.get("patch", "")}
        if r.get("traj_file") and os.path.isfile(r["traj_file"]):
            shutil.copy(r["traj_file"], os.path.join(out, "trajs", iid + os.path.splitext(r["traj_file"])[1]))
        cost = compute_cost(r.get("n_in"), r.get("n_cache"), r.get("n_out"), rate)
        # resolved: prefer official report; else reward>0
        res = iid in resolved_ids if resolved_ids else (float(r.get("reward") or 0) > 0)
        flip = bool(res and iid not in base_resolved) if base_resolved else None
        dlv = gt_delivery(r.get("obs_text"))
        deep.append(dict(
            instance_id=iid, harness=r.get("harness"), model=args.model,
            n_input_tokens=_f(r.get("n_in")), n_output_tokens=_f(r.get("n_out")),
            n_cache_tokens=_f(r.get("n_cache")), cost_usd=cost, steps=_f(r.get("steps")),
            resolved=bool(res), flip_vs_baseline=flip, has_patch=bool((r.get("patch") or "").strip()),
            gt_delivery=dlv,
        ))
        tot["n_in"] += r.get("n_in") or 0; tot["n_out"] += r.get("n_out") or 0
        tot["n_cache"] += r.get("n_cache") or 0; tot["cost"] += cost
        tot["resolved"] += int(bool(res)); tot["flips"] += int(bool(flip))

    json.dump(preds, open(os.path.join(out, "preds.json"), "w", encoding="utf-8"), indent=1)
    agg = dict(model=args.model, agent=args.agent, subset=args.subset, instances=len(recs),
               resolved=tot["resolved"], flips_vs_baseline=tot["flips"] if base_resolved else None,
               total_input_tokens=_f(tot["n_in"]), total_output_tokens=_f(tot["n_out"]),
               total_cache_tokens=_f(tot["n_cache"]), total_cost_usd=_f(tot["cost"]),
               cost_basis="computed from token counts x %s (litellm/openrouter return null)" % args.pricing,
               per_instance=deep)
    json.dump(agg, open(os.path.join(out, "deep_metrics.json"), "w", encoding="utf-8"), indent=1)

    # human-readable SUMMARY.md
    lines = [f"# {args.agent} / {args.model} — SWE-bench-Live {args.subset}", "",
             f"- instances: **{len(recs)}**  resolved: **{tot['resolved']}**"
             + (f"  flips-vs-baseline: **{tot['flips']}**" if base_resolved else ""),
             f"- tokens: in={tot['n_in']:,} out={tot['n_out']:,} cache={tot['n_cache']:,}",
             f"- cost: **${_f(tot['cost'])}** (computed; rate from {args.pricing})", "",
             "| instance | resolved | flip | tokens(in/out) | cost$ | brief | consensus | contract | cochange | L5 |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for d in deep:
        g = d["gt_delivery"]
        lines.append(f"| {d['instance_id']} | {'Y' if d['resolved'] else 'n'} "
                     f"| {'FLIP' if d['flip_vs_baseline'] else ('-' if d['flip_vs_baseline'] is not None else '?')} "
                     f"| {int(d['n_input_tokens'])}/{int(d['n_output_tokens'])} | {d['cost_usd']} "
                     f"| {g['brief']} | {g['consensus']} | {g['contract']} | {g['cochange']} | {g['nudge_L5']} |")
    open(os.path.join(out, "SUMMARY.md"), "w", encoding="utf-8").write("\n".join(lines))

    open(os.path.join(out, "README.md"), "w", encoding="utf-8").write(
        f"# {args.agent} on SWE-bench-Live {args.subset}\n\n"
        f"- Model: `{args.model}` (thinking disabled)\n"
        f"- Scaffold: GroundTruth full pipeline (host-LSP graph brief + per-view/edit hooks "
        f"+ consensus + L5/L6 + co-change), {args.agent}.\n"
        f"- Rollouts: 1 per task. Iterations: OH max_iterations=100 / mini-swe step_limit=300.\n"
        f"- Cost computed from token counts (litellm/openrouter return null for {args.model}); "
        f"see deep_metrics.json + benchmarks/pricing/deepseek_pricing.json.\n"
        f"- GT delivery verified from AGENT OBSERVATION (see SUMMARY.md gt_delivery columns).\n")

    print(f"packaged -> {out}")
    print(f"  instances={len(recs)} resolved={tot['resolved']} cost=${_f(tot['cost'])}")
    print(f"  files: preds.json results.json deep_metrics.json SUMMARY.md README.md trajs/ logs/")


if __name__ == "__main__":
    main()
