#!/usr/bin/env python3
"""Build the 1-line predictions.jsonl for the official SWE-bench-Live evaluator, with a
deterministic DEWRAP repair of terminal-wrapped patches.

WHY: the agent patch can reach us wrapped — mini-swe-agent captures command output through an
~80-col PTY, so a long line gets split and the continuation LOSES its leading token. git apply
then rejects the patch and the official evaluator produces no report. This bites BOTH kinds of line:
  - a hunk BODY line   (`+   # ... __str__ -> ` / `stdout -> wait()`), and
  - a file HEADER line (`--- a/very/long/path/…/b` / `atch_computeenvironment.json`) — a long path
    wraps so `git apply` reports "can't find file to patch" and skips every hunk for that file.
Every line of a valid unified diff starts with a KNOWN token: a header keyword (`diff `, `index `,
`--- `, `+++ `, `@@`, `old mode`, `new file`, `rename `, `Binary `, …) or a body prefix (`+`, `-`,
` `, `\\`). A non-empty line that starts with NONE of these is PROVABLY a wrapped continuation, so
we rejoin it (concatenate, no separator — the wrap preserves the trailing content) onto the previous
line, whatever kind it is. One rule repairs header wraps and body wraps alike. Safe by construction:
a well-formed diff has no such lines, so it passes through byte-identical.

Usage: build_pred.py <instance_id> <patch_file> <model_name> [out=/tmp/pred.jsonl]
"""
import json
import sys

# every valid unified-diff line begins with one of these tokens
_VALID_STARTS = (
    "diff ", "index ", "--- ", "+++ ", "@@", "+", "-", " ", "\\",
    "old mode", "new mode", "new file", "deleted ", "similarity ",
    "rename ", "copy ", "dissimilarity ", "Binary ", "GIT binary",
)


def dewrap(patch: str) -> str:
    out = []
    for ln in patch.split("\n"):
        if ln == "" or ln.startswith(_VALID_STARTS):
            out.append(ln)
        elif out and out[-1]:            # unprefixed, non-empty -> a wrapped continuation
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
