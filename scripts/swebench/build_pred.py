#!/usr/bin/env python3
"""Build the 1-line predictions.jsonl for the official SWE-bench-Live evaluator, with a
deterministic DEWRAP repair of terminal-wrapped patches.

WHY: the agent patch can reach us wrapped — mini-swe-agent captures command output through an
~80-col PTY, so a long diff line gets split and the continuation LOSES its +/-/space prefix
(e.g. `+   # ... __str__ -> ` / `stdout -> wait()`). git apply then rejects the whole patch as
malformed and the official evaluator produces no report. In a unified diff EVERY in-hunk line
MUST start with '+', '-', ' ' (or '\\' for the no-newline marker); a line that does not — and is
not a diff/hunk header — is PROVABLY a wrapped continuation, so we rejoin it (concatenate, no
separator: the wrap preserves the trailing content) to the previous +/- line. Safe by construction.

Usage: build_pred.py <instance_id> <patch_file> <model_name> [out=/tmp/pred.jsonl]
"""
import json
import sys

_HEADERS = ("diff ", "index ", "--- ", "+++ ", "new file", "deleted", "rename",
            "similarity", "Binary ", "@@ ", "@@")


def dewrap(patch: str) -> str:
    out = []
    in_hunk = False
    for ln in patch.split("\n"):
        if ln.startswith("@@"):
            in_hunk = True
            out.append(ln)
            continue
        if ln.startswith(_HEADERS):
            in_hunk = ln.startswith("@@")
            out.append(ln)
            continue
        # inside a hunk, a line with no +/-/space/\ prefix is a wrapped continuation
        if (in_hunk and ln and ln[0] not in "+- \\"
                and out and out[-1] and out[-1][0] in "+- \\"):
            out[-1] = out[-1] + ln
        else:
            out.append(ln)
    return "\n".join(out)


def main():
    iid, pf, model = sys.argv[1], sys.argv[2], sys.argv[3]
    out = sys.argv[4] if len(sys.argv) > 4 else "/tmp/pred.jsonl"
    patch = open(pf, encoding="utf-8").read()
    fixed = dewrap(patch)
    with open(out, "w", encoding="utf-8") as f:
        f.write(json.dumps({"instance_id": iid, "model_patch": fixed,
                            "model_name_or_path": model}))


if __name__ == "__main__":
    main()
