#!/usr/bin/env python3
"""Repair terminal-wrapped unified diff patches extracted from trial_output.log.

Terminal PTY wrapping inserts \\r\\n at variable column widths, splitting diff
lines mid-content.  This script detects continuation lines (lines that don't
start with a valid unified-diff prefix) and joins them back to their
predecessor.

Usage:
  Single file:  python unwrap_patch.py <input.diff> <output.diff>
  Batch mode:   python unwrap_patch.py --batch <artifacts_dir> <output_dir>

Batch mode walks artifacts_dir for trial_output.log files, extracts patches
(same logic as extract_patch_from_log.py), unwraps them, and writes results
to output_dir preserving the directory structure.
"""

import os
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Valid unified-diff line prefixes.  A line that does NOT match any of these
# is a wrapped continuation of the previous line.
# ---------------------------------------------------------------------------
_DIFF_PREFIX_RE = re.compile(
    r'^('
    r'diff '         # diff --git header
    r'|index '       # index line
    r'|--- '         # old-file header
    r'|\+\+\+ '     # new-file header
    r'|@@ '          # hunk header
    r'|\+'           # added line (including bare "+")
    r'|-'            # removed line (including bare "-")
    r'| '            # context line (starts with space)
    r'|\\'           # "\ No newline at end of file"
    r'|new '         # new file mode
    r'|old '         # old file mode
    r'|deleted '     # deleted file mode
    r'|rename'       # rename from/to
    r'|similarity'   # similarity index
    r'|Binary'       # Binary files differ
    r'|copy '        # copy from/to
    r')'
)


def _is_valid_diff_line(line: str) -> bool:
    """Return True if *line* starts with a recognised unified-diff prefix."""
    if line == '':
        return True  # blank lines are valid separators
    return _DIFF_PREFIX_RE.match(line) is not None


def unwrap_patch(raw: str) -> tuple[str, dict]:
    """Unwrap a terminal-corrupted unified diff.

    Returns (unwrapped_text, stats) where stats contains:
      original_lines  - line count before processing
      joined_lines    - number of continuation joins performed
      output_lines    - line count after processing
    """
    # Step 1: strip all \\r characters
    text = raw.replace('\r', '')

    lines = text.split('\n')
    original_count = len(lines)

    # Step 2: join continuation lines
    out: list[str] = []
    joined = 0

    for line in lines:
        if not out:
            # First line -- nothing to join to
            out.append(line)
            continue

        prev = out[-1]

        # Special case 3: "b/..." right after "diff --git a/..." header
        if line.startswith('b/') and prev.startswith('diff --git a/'):
            # Avoid double space if prev already ends with one (trailing
            # space before the \r\n wrap point).
            sep = '' if prev.endswith(' ') else ' '
            out[-1] = prev + sep + line
            joined += 1
            continue

        # Special case 4a: "a/..." right after "--- " header
        if line.startswith('a/') and prev.startswith('--- '):
            out[-1] = prev + line
            joined += 1
            continue

        # Special case 4b: "b/..." right after "+++ " header
        if line.startswith('b/') and prev.startswith('+++ '):
            out[-1] = prev + line
            joined += 1
            continue

        # General case: not a valid diff prefix => continuation of prev line
        if not _is_valid_diff_line(line):
            out[-1] = prev + line
            joined += 1
            continue

        out.append(line)

    result = '\n'.join(out)
    stats = {
        'original_lines': original_count,
        'joined_lines': joined,
        'output_lines': len(out),
    }
    return result, stats


# ---------------------------------------------------------------------------
# Patch extraction from trial_output.log  (mirrors extract_patch_from_log.py)
# ---------------------------------------------------------------------------

_TERMINAL_MARKERS = [
    "\nSaved trajectory",
    "\nAgent wants to finish",
    "\n=== Pro Trial",
    "\nEVAL_DEFERRED:",
    "\nPRO_EVAL_",
    "\nGT_RUN_PROOF",
    "\n[MEMLOG ",
    "\n[GT_DEEP]",
    "\n[RESMON]",
    "\nbash: line",
    "\nSubmit message:",
]


def extract_patch_from_log(log_text: str) -> str | None:
    """Extract the raw patch from a trial_output.log, or None if not found."""
    marker = "COMPLETE_TASK_AND_SUBMIT"
    marker_pos = log_text.rfind(marker)
    if marker_pos < 0:
        return None

    region = log_text[marker_pos:]

    for em in ["\nExit:\n", "\nExit:\r\n"]:
        p = region.find(em)
        if p >= 0:
            region = region[p + len(em):]
            break

    diff_start = region.find("diff --git")
    if diff_start < 0:
        return None

    raw = region[diff_start:]

    for tm in _TERMINAL_MARKERS:
        pos = raw.find(tm)
        if pos > 0:
            raw = raw[:pos]

    # Strip trailing non-diff lines
    lines = raw.split('\n')
    while lines:
        last = lines[-1]
        if last.strip() == '':
            lines.pop()
            continue
        if _is_valid_diff_line(last):
            break
        lines.pop()

    patch = '\n'.join(lines)
    if len(patch) < 10:
        return None
    return patch


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _single_file(input_path: str, output_path: str) -> None:
    raw = Path(input_path).read_text(encoding='utf-8', errors='replace')
    result, stats = unwrap_patch(raw)
    Path(output_path).write_text(result, encoding='utf-8')
    print(
        f"original_lines={stats['original_lines']}  "
        f"joined_lines={stats['joined_lines']}  "
        f"output_lines={stats['output_lines']}"
    )


def _batch(artifacts_dir: str, output_dir: str) -> None:
    artifacts = Path(artifacts_dir)
    out_root = Path(output_dir)
    out_root.mkdir(parents=True, exist_ok=True)

    total_files = 0
    total_extracted = 0
    total_joined = 0
    total_failed_extract = 0
    total_empty = 0

    for log_path in sorted(artifacts.rglob('trial_output.log')):
        total_files += 1
        rel = log_path.relative_to(artifacts)
        # Use parent directory name (typically the task id) as output filename
        task_name = rel.parent.name if rel.parent != Path('.') else rel.stem
        out_path = out_root / f"{task_name}.diff"

        log_text = log_path.read_text(encoding='utf-8', errors='replace')
        raw_patch = extract_patch_from_log(log_text)
        if raw_patch is None:
            total_failed_extract += 1
            continue

        result, stats = unwrap_patch(raw_patch)
        if len(result.strip()) < 10:
            total_empty += 1
            continue

        out_path.write_text(result, encoding='utf-8')
        total_extracted += 1
        total_joined += stats['joined_lines']

    print(f"batch complete:")
    print(f"  logs scanned:      {total_files}")
    print(f"  patches extracted: {total_extracted}")
    print(f"  lines joined:      {total_joined}")
    print(f"  extract failures:  {total_failed_extract}")
    print(f"  empty patches:     {total_empty}")


def _from_log(log_path: str, output_path: str) -> None:
    """Extract the patch from a trial_output.log AND unwrap it, in one call.

    This is the path the SWE-bench Pro re-scorer uses: it repairs the terminal
    wrapping that the plain extract_patch_from_log.py leaves in place (the
    corruption behind the 523 APPLY_FAIL). Exits 1 if no patch is recoverable.
    """
    log_text = Path(log_path).read_text(encoding='utf-8', errors='replace')
    raw = extract_patch_from_log(log_text)
    if raw is None:
        sys.exit(1)
    result, stats = unwrap_patch(raw)
    if len(result.strip()) < 10:
        sys.exit(1)
    Path(output_path).write_text(result, encoding='utf-8')
    print(
        f"from-log: joined={stats['joined_lines']} "
        f"output_lines={stats['output_lines']}"
    )


def main() -> None:
    if len(sys.argv) < 3:
        print(
            "usage:\n"
            "  unwrap_patch.py <input.diff> <output.diff>\n"
            "  unwrap_patch.py --from-log <trial_output.log> <output.diff>\n"
            "  unwrap_patch.py --batch <artifacts_dir> <output_dir>",
            file=sys.stderr,
        )
        sys.exit(1)

    if sys.argv[1] == '--batch':
        if len(sys.argv) < 4:
            print(
                "usage: unwrap_patch.py --batch <artifacts_dir> <output_dir>",
                file=sys.stderr,
            )
            sys.exit(1)
        _batch(sys.argv[2], sys.argv[3])
    elif sys.argv[1] == '--from-log':
        if len(sys.argv) < 4:
            print(
                "usage: unwrap_patch.py --from-log <trial_output.log> <output.diff>",
                file=sys.stderr,
            )
            sys.exit(1)
        _from_log(sys.argv[2], sys.argv[3])
    else:
        _single_file(sys.argv[1], sys.argv[2])


if __name__ == '__main__':
    main()
