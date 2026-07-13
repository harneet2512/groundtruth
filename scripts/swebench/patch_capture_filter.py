#!/usr/bin/env python3
"""Patch-capture hygiene — strip binary/untracked-artifact contamination from a
captured diff before the official eval (SS-3 defect-3).

Why (run 29236533134 / keras-team__keras-20396): the harness captures the agent's
change as ``git diff HEAD`` after ``git add -A -N``. When the agent's own reproduction
script writes binary artifacts (``model.keras``, ``model.weights.h5``, …), those get
swept into the diff as ``Binary files … differ`` blocks. ``git apply`` cannot apply a
binary block that carries no ``GIT binary patch`` payload, so the WHOLE patch is
rejected → no ``report.json`` → the task false-fails eval even though the real source
fix is present.

The fix is file-type GENERAL — it keys on git's OWN binary marker, never on an
extension allow/deny list:

  * ``filter_binary_file_blocks`` drops any ``diff --git`` file-block whose body is a
    ``Binary files … differ`` / ``GIT binary patch`` block, keeping every text hunk.
  * ``choose_patch`` implements the capture priority: the agent's curated patch first
    (when present, non-empty, and structurally applyable after filtering), otherwise
    the binary-filtered ``git diff HEAD``.

Pure/stdlib; safe to import from tests and to invoke as a CLI in the workflow.
"""
from __future__ import annotations

import re
import sys

#: git's own binary markers inside a file-block (no extension hardcoding).
_BINARY_MARKERS = ("Binary files ", "GIT binary patch")
_DIFF_HEADER = "diff --git "


def _split_file_blocks(diff_text: str) -> tuple[str, list[str]]:
    """Split a unified diff into (preamble, [file_block, ...]).

    A file-block starts at a ``diff --git `` line and runs until the next one. The
    preamble is any text before the first ``diff --git`` (normally empty for
    ``git diff``); it is preserved verbatim.
    """
    if not diff_text:
        return "", []
    lines = diff_text.splitlines(keepends=True)
    preamble: list[str] = []
    blocks: list[list[str]] = []
    cur: list[str] | None = None
    for ln in lines:
        if ln.startswith(_DIFF_HEADER):
            if cur is not None:
                blocks.append(cur)
            cur = [ln]
        elif cur is None:
            preamble.append(ln)
        else:
            cur.append(ln)
    if cur is not None:
        blocks.append(cur)
    return "".join(preamble), ["".join(b) for b in blocks]


def _block_is_binary(block: str) -> bool:
    """A file-block is binary when any body line is one of git's binary markers.

    Matched at line start (after optional leading whitespace) so the literal words
    ``Binary files`` appearing INSIDE a normal +/- text hunk cannot false-trigger.
    """
    for ln in block.splitlines():
        s = ln.lstrip()
        if s.startswith("Binary files ") and s.rstrip().endswith(" differ"):
            return True
        if s.startswith("GIT binary patch"):
            return True
    return False


def filter_binary_file_blocks(diff_text: str) -> str:
    """Return ``diff_text`` with every binary file-block removed; text hunks kept."""
    preamble, blocks = _split_file_blocks(diff_text)
    kept = [b for b in blocks if not _block_is_binary(b)]
    return preamble + "".join(kept)


def has_text_hunk(diff_text: str) -> bool:
    """True when the (already-filtered) diff carries at least one applyable text hunk."""
    return bool(re.search(r"^@@ .*@@", diff_text, re.M)) and _DIFF_HEADER in diff_text


def looks_like_valid_patch(diff_text: str) -> bool:
    """Structural applyability proxy (no repo needed): a non-empty diff that has a
    ``diff --git`` header and a text hunk, and carries NO residual binary block."""
    if not diff_text or not diff_text.strip():
        return False
    if _DIFF_HEADER not in diff_text:
        return False
    if any(_block_is_binary(b) for b in _split_file_blocks(diff_text)[1]):
        return False
    return has_text_hunk(diff_text)


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def choose_patch(candidate_paths: list[str]) -> tuple[str, str | None]:
    """Pick the first candidate that yields a non-empty, binary-free, applyable patch.

    Candidates are given in PRIORITY order (e.g. the agent's curated /tmp/patch.txt
    first, then the git-diff-HEAD capture). Each candidate is binary-filtered before
    the applyability check. Returns (filtered_patch_text, chosen_path) — or ("", None)
    when no candidate is usable.
    """
    for path in candidate_paths:
        raw = _read(path)
        if not raw.strip():
            continue
        filtered = filter_binary_file_blocks(raw)
        if looks_like_valid_patch(filtered):
            return filtered, path
    # Fallback: return the binary-filtered form of the first non-empty candidate even
    # if the applyability proxy is unsure (better a text-only diff than a binary one).
    for path in candidate_paths:
        raw = _read(path)
        if raw.strip():
            return filter_binary_file_blocks(raw), path
    return "", None


def main(argv: list[str] | None = None) -> int:
    """CLI. Two modes:

      patch_capture_filter.py IN OUT
          Filter IN → OUT (drop binary file-blocks). Exit 0 if OUT is non-empty.

      patch_capture_filter.py --choose OUT CAND1 [CAND2 ...]
          Choose the first applyable candidate (curated patch first), binary-filter it,
          write to OUT. Prints the chosen source to stderr. Exit 0 if OUT is non-empty,
          else 1 (so the caller can fall back).
    """
    args = list(argv if argv is not None else sys.argv[1:])
    if args and args[0] == "--choose":
        if len(args) < 3:
            print("usage: patch_capture_filter.py --choose OUT CAND...", file=sys.stderr)
            return 2
        out_path, cands = args[1], args[2:]
        text, chosen = choose_patch(cands)
        print(f"patch_capture_filter: chose {chosen!r}", file=sys.stderr)
    else:
        if len(args) < 2:
            print("usage: patch_capture_filter.py IN OUT", file=sys.stderr)
            return 2
        in_path, out_path = args[0], args[1]
        text = filter_binary_file_blocks(_read(in_path))
    try:
        with open(out_path, "w", encoding="utf-8", errors="replace") as fh:
            fh.write(text)
    except OSError as e:
        print(f"patch_capture_filter: write failed: {e}", file=sys.stderr)
        return 2
    return 0 if text.strip() else 1


if __name__ == "__main__":
    raise SystemExit(main())
