#!/usr/bin/env python3
"""Strip DEEPSEEK-ONLY request params from the pier agent config for a non-DeepSeek model.

WHY (2026-07-24): ``deepswe_gt_pier.yaml`` ships ``model.model_kwargs.extra_body.thinking.type:
enabled`` — a DeepSeek-specific parameter. litellm's ``drop_params`` drops unsupported *top-level*
params but forwards ``extra_body`` RAW, so another OpenAI-compatible backend (e.g. GLM on vLLM via
TokenRouter) can reject the unknown field with a 400. On a FREE tier that counts failed attempts
against the rate limit, a systematic 400 does not merely degrade the run — it burns the quota and
kills it. So the block must be removed BEFORE the run for any non-DeepSeek model.

Correct-or-quiet: a missing/unreadable/already-stripped config is a no-op (exit 0). DeepSeek models
are never touched (the caller skips this script), so DeepSeek runs stay byte-identical.

Usage:  python3 scripts/ci/strip_model_specific_params.py <config.yaml> <model-string>
"""
from __future__ import annotations

import io
import sys

# Blocks that are valid ONLY for a DeepSeek backend. Keyed by the exact YAML key line prefix.
_DEEPSEEK_ONLY_BLOCKS = ("extra_body:",)


def strip_blocks(src: str, blocks: "tuple[str, ...]" = _DEEPSEEK_ONLY_BLOCKS) -> "tuple[str, int]":
    """Remove each named mapping block (its key line + every deeper-indented line).

    Indentation-based, so it needs no YAML round-trip (which would reformat the whole file and
    destroy the comments the config depends on for its audit trail). Returns (text, n_stripped).
    """
    out: list[str] = []
    skip = False
    base = 0
    n = 0
    for line in src.splitlines(True):
        stripped = line.strip()
        indent = len(line) - len(line.lstrip(" "))
        if not skip and any(stripped.startswith(b) for b in blocks):
            skip, base, n = True, indent, n + 1
            out.append(" " * indent + "# [ci] deepseek-only block stripped for a non-deepseek model\n")
            continue
        if skip:
            # Stay inside the block while indented deeper than its key; blank lines belong to it.
            if stripped == "" or indent > base:
                continue
            skip = False
        out.append(line)
    return "".join(out), n


def main(argv: "list[str]") -> int:
    if len(argv) < 3:
        print("usage: strip_model_specific_params.py <config.yaml> <model>", file=sys.stderr)
        return 2
    path, model = argv[1], argv[2]
    if "deepseek" in model.lower():
        print(f"[MODEL-CFG] {model} is DeepSeek — config untouched (byte-identical)")
        return 0
    try:
        src = io.open(path, encoding="utf-8").read()
    except OSError as exc:  # correct-or-quiet: never fail the run over a config tweak
        print(f"[MODEL-CFG] config unreadable ({exc}) — no-op")
        return 0
    new, n = strip_blocks(src)
    if n:
        io.open(path, "w", encoding="utf-8").write(new)
    print(f"[MODEL-CFG] {model}: stripped {n} deepseek-only block(s) from {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
