"""Deep message-by-message analysis of Inspect eval logs."""
import glob
import os
import sys

from inspect_ai.log import read_eval_log


def analyze(logdir: str, label: str) -> None:
    evals = sorted(glob.glob(os.path.join(logdir, "*.eval")))
    if not evals:
        print(f"{label}: NO EVAL LOG")
        return
    log = read_eval_log(evals[-1])
    s = log.samples[0]
    sep = "=" * 70
    print(sep)
    print(f"{label} -- {s.id} -- {len(s.messages)} messages")
    print(sep)

    gt_tool_calls = 0
    gt_injections = 0
    edits = 0
    files_edited = set()
    files_viewed = set()

    for i, m in enumerate(s.messages):
        role = getattr(m, "role", "?")
        content = str(getattr(m, "content", ""))
        fn = getattr(m, "function", "")

        if role == "assistant" and hasattr(m, "tool_calls") and m.tool_calls:
            for tc in m.tool_calls:
                tcfn = tc.function if isinstance(tc.function, str) else str(tc.function)
                args = getattr(tc, "arguments", {}) or {}
                args_str = str(args)[:200]
                gt = " ***GT***" if "groundtruth" in tcfn else ""
                if gt:
                    gt_tool_calls += 1
                print(f"  [{i}] CALL: {tcfn}{gt} | {args_str}")

        elif role == "tool":
            tool_name = fn or "?"
            gt = " ***GT***" if "groundtruth" in str(tool_name) else ""
            if gt:
                print(f"  [{i}] GT_RESULT: {tool_name} | {content[:400]}")
            elif "has been edited" in content or "created file" in content:
                edits += 1
                # try to extract path
                for line in content.split("\n")[:3]:
                    if "/testbed/" in line:
                        fp = line.split("/testbed/")[-1].split("`")[0].split("'")[0].strip()
                        files_edited.add(fp)
                print(f"  [{i}] EDIT: {tool_name} | {content[:150]}")
            elif "view" in str(tool_name).lower() or "cat " in content[:50]:
                pass  # skip verbose view output

        elif role == "user":
            if "[GT" in content:
                gt_injections += 1
                print(f"  [{i}] GT_INJECTION: {content[:300]}")
            elif i > 1 and len(content) > 20 and "Please solve" not in content[:20]:
                print(f"  [{i}] USER: {content[:100]}")

    # Score and patch
    print()
    for name, sc in (s.scores or {}).items():
        meta = sc.metadata or {}
        diff_stat = meta.get("diff_stat", sc.explanation or "")
        full_diff = meta.get("full_diff", "")[:600]
        print(f"SCORE: {sc.value} | {diff_stat}")
        if full_diff:
            print(f"PATCH:\n{full_diff}")

    # Summary
    print()
    print(f"SUMMARY:")
    print(f"  Messages: {len(s.messages)}")
    print(f"  GT tool calls: {gt_tool_calls}")
    print(f"  GT on_continue injections: {gt_injections}")
    print(f"  Edits: {edits}")
    print(f"  Files edited: {files_edited or 'unknown'}")

    if hasattr(log, "stats") and log.stats and log.stats.model_usage:
        for model, u in log.stats.model_usage.items():
            print(f"  Tokens: in={u.input_tokens} cache={u.input_tokens_cache_read} out={u.output_tokens} total={u.total_tokens}")
    print()


if __name__ == "__main__":
    analyze("/tmp/inspect_t2_baseline", "BASELINE")
    analyze("/tmp/inspect_t2_gt", "GT (on_continue + pull tools)")
