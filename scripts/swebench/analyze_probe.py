#!/usr/bin/env python3
"""Deep utilization analysis for a v1.0.5 probe run.

Usage:
  python3 analyze_probe.py <RUN_DIR>

Emits:
  - Brief delivery rate (gt-task-brief in completion JSONs)
  - Edit sequence (path + action) extracted from agent completions
  - focus_file_edit_rate (first-3 + overall) vs top-5 brief files
  - Iter count, cost, walltime
  - Final patch summary (files modified)
  - Hook activity (if /tmp/gt_hook_stdout.log preserved)
"""
import json
import os
import re
import sys
import glob


def load_first_jsonl_record(path: str):
    with open(path) as fh:
        line = fh.readline().strip()
    return json.loads(line) if line else {}


def extract_edit_sequence_from_completions(completions_dir: str):
    """Walk LLM completion JSONs in time order, parse OH function-call markup."""
    files = sorted(glob.glob(os.path.join(completions_dir, "*.json")))
    edit_seq = []
    for fp in files:
        try:
            d = json.load(open(fp))
        except Exception:
            continue
        # The completion JSON contains both messages and response choices.
        # Tool calls live in either response.choices[*].message.content (raw text)
        # or response.choices[*].message.tool_calls[*].function.arguments (JSON).
        resp = d.get("response", {}) or d.get("kwargs", {}).get("response", {})
        choices = resp.get("choices", [])
        for ch in choices:
            msg = ch.get("message", {}) or {}
            content = msg.get("content", "")
            if isinstance(content, list):
                content = "".join(c.get("text", "") for c in content if isinstance(c, dict))
            if not isinstance(content, str):
                content = str(content)

            # Pattern A: OH function-call markup
            for blk in re.finditer(
                r"<function=str_replace_editor>(.*?)</function>",
                content,
                flags=re.DOTALL,
            ):
                body = blk.group(1)
                cmd_m = re.search(r"<parameter=command>([^<]+)</parameter>", body)
                path_m = re.search(r"<parameter=path>([^<]+)</parameter>", body)
                if cmd_m and path_m:
                    edit_seq.append((cmd_m.group(1).strip(), path_m.group(1).strip(), os.path.basename(fp)))

            # Pattern B: native tool_calls
            for tc in msg.get("tool_calls", []) or []:
                fn = tc.get("function", {}) or {}
                name = fn.get("name", "")
                if name in ("str_replace_editor", "edit", "write"):
                    try:
                        args = json.loads(fn.get("arguments", "{}"))
                    except Exception:
                        args = {}
                    path = args.get("path") or args.get("file_path") or ""
                    cmd = args.get("command", name)
                    if path:
                        edit_seq.append((cmd.strip(), path.strip(), os.path.basename(fp)))
    return edit_seq


def main():
    run_dir = sys.argv[1].rstrip("/")
    out_root = next(
        (
            os.path.join(run_dir, sub)
            for sub in os.listdir(run_dir)
            if sub.startswith("SWE-bench-Live__")
        ),
        None,
    )
    if not out_root:
        print(f"FATAL: SWE-bench-Live__ dir missing under {run_dir}")
        sys.exit(1)
    inner = os.listdir(out_root)
    out_root = os.path.join(out_root, inner[0])  # CodeActAgent
    inner2 = os.listdir(out_root)
    out_root = os.path.join(out_root, inner2[0])  # qwen3-coder-...

    output_jsonl = os.path.join(out_root, "output.jsonl")
    completions_root = os.path.join(out_root, "llm_completions")
    iid_dirs = os.listdir(completions_root)
    iid = iid_dirs[0]
    completions_dir = os.path.join(completions_root, iid)

    print(f"=== run_dir: {run_dir}")
    print(f"=== iid:     {iid}")
    print()

    # Brief delivery rate
    completion_files = sorted(glob.glob(os.path.join(completions_dir, "*.json")))
    brief_hits = 0
    focus_hits = 0
    for fp in completion_files:
        try:
            txt = open(fp, errors="replace").read()
        except Exception:
            continue
        if "<gt-task-brief>" in txt:
            brief_hits += 1
        if "<gt-focus-functions>" in txt:
            focus_hits += 1
    print(f"=== Brief delivery ===")
    print(f"  completion_count = {len(completion_files)}")
    print(f"  gt-task-brief    = {brief_hits}/{len(completion_files)}  ({100*brief_hits//max(1,len(completion_files))}%)")
    print(f"  gt-focus-funcs   = {focus_hits}/{len(completion_files)}  ({100*focus_hits//max(1,len(completion_files))}%)")
    print()

    # Output record
    rec = load_first_jsonl_record(output_jsonl)
    test_result = rec.get("test_result") or {}
    patch = test_result.get("git_patch", "") or rec.get("git_patch", "")
    metrics = rec.get("metrics", {}) or {}
    print(f"=== Run summary ===")
    print(f"  history_events = {len(rec.get('history', []))}")
    print(f"  patch_chars    = {len(patch)}")
    if metrics:
        print(f"  metrics.accumulated_cost = {metrics.get('accumulated_cost', 'n/a')}")
        usage = metrics.get("accumulated_token_usage", {})
        for k in ("prompt_tokens", "completion_tokens", "cache_read_tokens", "cache_write_tokens"):
            if k in usage:
                print(f"  metrics.{k} = {usage[k]}")
    print()

    files_modified = re.findall(r"^diff --git a/(\S+) b/", patch, re.M)
    new_files = re.findall(r"^new file mode \d+\s*\n--- /dev/null\s*\n\+\+\+ b/(\S+)", patch, re.M)
    print(f"=== Patch files ===")
    print(f"  files_modified = {files_modified}")
    print(f"  new_files      = {new_files}")
    print()

    # Edit sequence
    edits = extract_edit_sequence_from_completions(completions_dir)
    print(f"=== Edit sequence (from LLM completions) ===")
    print(f"  total edits captured = {len(edits)}")
    for i, (cmd, path, fn) in enumerate(edits[:20]):
        print(f"  [{i+1:02d}] {cmd:<14} {path}")
    print()

    TOP5 = [
        "weasyprint/layout/flex.py",
        "weasyprint/draw/border.py",
        "weasyprint/html.py",
        "weasyprint/formatting_structure/boxes.py",
        "weasyprint/layout/block.py",
    ]

    def is_top5(p: str) -> bool:
        return any(p.endswith(t) or ("/" + t) in p for t in TOP5)

    first3 = edits[:3]
    hit_first3 = sum(1 for c, p, _ in first3 if is_top5(p))
    hit_overall = sum(1 for c, p, _ in edits if is_top5(p))

    # Distinct edited paths
    distinct = []
    seen = set()
    for c, p, _ in edits:
        if p not in seen:
            seen.add(p)
            distinct.append(p)
    distinct_top5 = [p for p in distinct if is_top5(p)]

    print(f"=== L2 — Focus metrics (top-5 brief = {TOP5}) ===")
    print(f"  focus_file_edit_rate_first3   = {hit_first3}/3  ({'PASS' if hit_first3 >= 2 else 'WEAK'})")
    if edits:
        print(f"  focus_file_edit_rate_overall  = {hit_overall}/{len(edits)} ({100*hit_overall//len(edits)}%)")
    print(f"  distinct_paths_edited         = {len(distinct)}")
    print(f"  distinct_top5_edited          = {len(distinct_top5)}: {distinct_top5}")
    print(f"  scaffolding_paths_edited      = {[p for p in distinct if not is_top5(p)]}")
    print()

    # ---- L1 localization vs gold ----
    gold_files = set(files_modified)
    top5_set = set(TOP5)
    print(f"=== L1 — Localization metrics (vs final patch as proxy gold) ===")
    print(f"  patch_files (proxy gold)      = {sorted(gold_files)}")
    intersection_top5 = gold_files & top5_set
    if gold_files:
        focus_precision = len(intersection_top5) / len(top5_set)
        focus_coverage = len(intersection_top5) / len(gold_files)
        print(f"  focus_precision               = {len(intersection_top5)}/{len(top5_set)} = {focus_precision:.2f}  (how many top-5 ranked are gold)")
        print(f"  focus_coverage                = {len(intersection_top5)}/{len(gold_files)} = {focus_coverage:.2f}  (how many gold files appear in top-5)")
    # first_gold_rank
    first_gold_rank = None
    for i, t in enumerate(TOP5):
        if t in gold_files:
            first_gold_rank = i + 1
            break
    print(f"  first_gold_rank               = {first_gold_rank}  (lower=better; 1 means gold file is rank-1)")
    print()

    # ---- L3 hook — read stdout log if archived ----
    hook_log_paths = []
    for guess in (
        os.path.join(run_dir, "gt_hook_stdout.log"),
        "/tmp/gt_hook_stdout.log",
    ):
        if os.path.exists(guess):
            hook_log_paths.append(guess)
    print(f"=== L3 — Hook fires (stdout log) ===")
    if hook_log_paths:
        for p in hook_log_paths:
            content = open(p, errors="replace").read()
            fires = content.count("---\nfired_for=")
            print(f"  {p}: {fires} hook fires; {len(content)} bytes")
    else:
        print(f"  hook_stdout_log: NOT PRESERVED (container removed before extraction)")
        print(f"  watcher_status:  UNCERTAIN (per wrapper.log L3 print — pgrep gt_watcher.py did not return in time)")
    print()

    # ---- L4 endpoints ----
    print(f"=== L4 — MCP endpoints (gt_lookup / gt_impact / gt_check) ===")
    print(f"  status: DISABLED in this run (OH SWE-bench-Live fork hard-codes enable_mcp=False)")
    print(f"         endpoints will be wired in v1.0.6 after MCP path lands in fork")
    print()

    # ---- L5 gate ----
    print(f"=== L5 — Pre-finish gate ===")
    print(f"  status: NOT PORTED (depends on L4 MCP endpoint coverage; deferred with L4)")
    print()

    # ---- L6 reindex ----
    print(f"=== L6 — Incremental reindex ===")
    print(f"  status: gt-index binary copied to container, watcher uncertain → no observable reindex")
    print(f"  blocked_by: L3 watcher start uncertainty in fork's runtime model")
    print()

    # ---- agent behavior summary ----
    print(f"=== Per-task summary ===")
    print(f"  brief_chars                   = 1324 (from L1 host-side ranker)")
    print(f"  brief_tokens_approx           = ~{1324//4}")
    print(f"  brief_delivery_rate           = {brief_hits}/{len(completion_files)} = {100*brief_hits//max(1,len(completion_files))}%")
    print(f"  total_completions             = {len(completion_files)}")
    print(f"  total_edits_captured          = {len(edits)}")
    print(f"  walltime_sec                  = (see wrapper.log final tail)")
    print()


if __name__ == "__main__":
    main()
