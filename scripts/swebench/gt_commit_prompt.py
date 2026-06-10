"""TEG-1 commit-prompt hook — fires once per task to compress touch->edit gap.

Spec (frozen, see touch_edit_gap_next_track.md §9):

This script is invoked as a post-tool-use hook by OpenHands inside a SWE-bench
container after every tool call. It maintains per-task state and fires exactly
ONCE per task when all of:

  - reads_seen >= N (default N=4)
  - the agent has read at least one file from the V1R-map brief's gold_set_paths
  - edits_seen == 0 against any path in gold_set_paths
  - has_fired is False

When it fires, it prints a single commit-or-justify message to stdout. OpenHands
will surface this in the next observation the agent sees, so the agent's next
turn reads it before deciding the next action.

Design properties (binding):

  - REPO-AGNOSTIC: no per-repo strings, paths, or vocabulary. Reads gold paths
    from a config file written by run_infer.py at task start.
  - LANGUAGE-AGNOSTIC: classifies edits via `git status --porcelain`, which works
    for any text file. Read detection uses atime (relatime is the default on
    Linux containers, and SWE-bench-Live containers don't override it).
  - ONE-SHOT: state file persists fired flag; subsequent invocations are no-ops.
  - FAILS QUIET: any error is logged but never breaks the agent loop.
  - LOW OVERHEAD: ~50ms typical (one git status + a handful of stats).

Invocation (from hooks.json, post_tool_use, broad matcher):

  python3 /tmp/gt_commit_prompt.py \
      --root=/workspace \
      --gold-paths=/tmp/teg1_gold_paths.txt \
      --state=/tmp/teg1_state.json \
      --log=/tmp/teg1_log.jsonl \
      --threshold=4

Stdout (when fire condition triggers):

  <gt-commit-prompt>
  You have read the candidate files from your task brief but have not edited
  any of them yet. Draft a concrete edit to one of these files now, or state
  explicitly which sibling file is missing context and what one read would
  unblock the edit. Avoid writing throwaway scaffolding or repro tests at the
  repo root before editing the real fix.
  </gt-commit-prompt>

Stdout is empty otherwise — the agent sees nothing on most invocations.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from typing import Any


COMMIT_PROMPT_TEXT = (
    "<gt-commit-prompt>\n"
    "You have read the candidate files from your task brief but have not "
    "edited any of them yet. Draft a concrete edit to one of these files "
    "now, or state explicitly which sibling file is missing context and "
    "what one read would unblock the edit. Avoid writing throwaway "
    "scaffolding or repro tests at the repo root before editing the real "
    "fix.\n"
    "</gt-commit-prompt>"
)


def _read_gold_paths(path: str) -> list[str]:
    """One path per line. Blank lines and lines starting with '#' are ignored."""
    if not os.path.exists(path):
        return []
    out: list[str] = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            out.append(s)
    return out


def _load_state(path: str) -> dict[str, Any]:
    """State schema: see _default_state for shape. Missing keys are filled in."""
    if not os.path.exists(path):
        return _default_state()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return _default_state()
    base = _default_state()
    base.update(data)
    return base


def _save_state(path: str, state: dict[str, Any]) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, sort_keys=True)
    os.replace(tmp, path)


def _default_state() -> dict[str, Any]:
    return {
        "reads_seen": 0,
        "edits_seen": 0,
        "files_modified_seen": [],
        "gold_files_read": [],
        "gold_files_edited": [],
        "has_fired": False,
        "first_invocation_ts": time.time(),
        "last_invocation_ts": 0.0,
        "fire_ts": 0.0,
    }


def _git_modified_files(root: str) -> list[str]:
    """Return list of files reported by `git status --porcelain` (modified/new).

    Returns repo-relative paths, normalized with forward slashes.
    """
    if not os.path.isdir(os.path.join(root, ".git")):
        return []
    env = dict(os.environ)
    env["GIT_CONFIG_COUNT"] = "1"
    env["GIT_CONFIG_KEY_0"] = "safe.directory"
    env["GIT_CONFIG_VALUE_0"] = "*"
    try:
        result = subprocess.run(
            ["git", "-C", root, "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    out: list[str] = []
    for line in result.stdout.splitlines():
        if len(line) < 4:
            continue
        relpath = line[3:].split(" -> ")[-1].strip().strip('"')
        if relpath:
            out.append(relpath.replace("\\", "/"))
    return out


def _is_gold_path(path: str, gold_paths: list[str]) -> bool:
    """Match repo-relative `path` against the gold candidate list.

    Matches when:
      - exact path equality, OR
      - path is contained in a gold path (handles the case where the brief
        listed `babel/dates.py` and the agent edited `babel/dates.py`), OR
      - the path's last 2 segments match a gold path's last 2 segments
        (handles `src/pdm/auth.py` vs `pdm/auth.py` style differences).
    """
    if not gold_paths:
        return False
    p = path.replace("\\", "/").strip("/")
    p_tail = "/".join(p.split("/")[-2:]) if "/" in p else p
    for g in gold_paths:
        g_norm = g.replace("\\", "/").strip("/")
        if p == g_norm:
            return True
        if g_norm.endswith("/" + p) or p.endswith("/" + g_norm):
            return True
        g_tail = "/".join(g_norm.split("/")[-2:]) if "/" in g_norm else g_norm
        if p_tail == g_tail and "/" in p_tail:
            return True
    return False


def _gold_files_recently_read(
    root: str,
    gold_paths: list[str],
    since_ts: float,
) -> list[str]:
    """Return gold paths whose atime is >= since_ts.

    On Linux with `relatime` (the default on most distros and SWE-bench
    containers), atime is updated when atime < mtime/ctime, which is the
    typical case for a freshly-checked-out repo. So if the agent reads a gold
    file, its atime moves forward.

    This is a best-effort signal. If the filesystem mounts noatime, this
    returns []; the caller treats that as "we don't know whether gold was
    read" and skips the strict gate.
    """
    out: list[str] = []
    for g in gold_paths:
        candidate = g.replace("\\", "/").lstrip("/")
        full = os.path.join(root, candidate)
        if not os.path.exists(full):
            continue
        try:
            atime = os.path.getatime(full)
        except OSError:
            continue
        if atime >= since_ts:
            out.append(candidate)
    return out


def _atime_signals_available(root: str, sample_paths: list[str]) -> bool:
    """Sniff whether the filesystem provides usable atime.

    Picks up to 3 sample files, reads them, then checks whether atime moved
    forward. If even one moves, atime is considered available.
    """
    samples = [p for p in sample_paths if os.path.isfile(os.path.join(root, p))][:3]
    if not samples:
        return False
    before: dict[str, float] = {}
    for p in samples:
        full = os.path.join(root, p)
        try:
            before[p] = os.path.getatime(full)
        except OSError:
            pass
    for p in list(before.keys()):
        full = os.path.join(root, p)
        try:
            with open(full, "rb") as f:
                f.read(64)
        except OSError:
            continue
    for p, prev in before.items():
        full = os.path.join(root, p)
        try:
            now = os.path.getatime(full)
            if now > prev:
                return True
        except OSError:
            continue
    return False


def _log(log_path: str, record: dict[str, Any]) -> None:
    record["ts"] = time.time()
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, sort_keys=True) + "\n")
    except OSError:
        pass


def run(
    root: str,
    gold_paths_file: str,
    state_file: str,
    log_file: str,
    threshold: int,
    out_stream: Any = sys.stdout,
) -> int:
    """Single hook invocation. Returns 0 on success, 0 on quiet failure."""
    gold_paths = _read_gold_paths(gold_paths_file)
    state = _load_state(state_file)

    if state["has_fired"]:
        state["last_invocation_ts"] = time.time()
        _save_state(state_file, state)
        return 0

    git_files = _git_modified_files(root)
    new_modifications = [f for f in git_files if f not in state["files_modified_seen"]]
    if new_modifications:
        state["edits_seen"] += len(new_modifications)
        state["files_modified_seen"] = sorted(set(state["files_modified_seen"]) | set(git_files))
        for f in new_modifications:
            if _is_gold_path(f, gold_paths) and f not in state["gold_files_edited"]:
                state["gold_files_edited"].append(f)
    else:
        state["reads_seen"] += 1

    since_ts = state.get("first_invocation_ts", 0.0)
    recent_gold_reads = _gold_files_recently_read(root, gold_paths, since_ts)
    for f in recent_gold_reads:
        if f not in state["gold_files_read"]:
            state["gold_files_read"].append(f)

    state["last_invocation_ts"] = time.time()

    fired = False
    if (
        state["reads_seen"] >= threshold
        and len(state["gold_files_edited"]) == 0
        and (len(state["gold_files_read"]) > 0 or not _atime_signals_available(root, gold_paths))
        and gold_paths
    ):
        fired = True

    if fired:
        out_stream.write(COMMIT_PROMPT_TEXT + "\n")
        out_stream.flush()
        state["has_fired"] = True
        state["fire_ts"] = time.time()
        _log(log_file, {
            "event": "fire",
            "reads_seen": state["reads_seen"],
            "edits_seen": state["edits_seen"],
            "gold_files_read": list(state["gold_files_read"]),
            "gold_files_edited": list(state["gold_files_edited"]),
            "files_modified_seen": list(state["files_modified_seen"]),
            "atime_available": _atime_signals_available(root, gold_paths),
        })
    else:
        _log(log_file, {
            "event": "tick",
            "reads_seen": state["reads_seen"],
            "edits_seen": state["edits_seen"],
            "gold_files_read_count": len(state["gold_files_read"]),
            "gold_files_edited_count": len(state["gold_files_edited"]),
        })

    _save_state(state_file, state)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TEG-1 commit-prompt hook")
    parser.add_argument("--root", default="/workspace")
    parser.add_argument("--gold-paths", default="/tmp/teg1_gold_paths.txt")
    parser.add_argument("--state", default="/tmp/teg1_state.json")
    parser.add_argument("--log", default="/tmp/teg1_log.jsonl")
    parser.add_argument("--threshold", type=int, default=4)
    args = parser.parse_args(argv)
    try:
        return run(
            root=args.root,
            gold_paths_file=args.gold_paths,
            state_file=args.state,
            log_file=args.log,
            threshold=args.threshold,
        )
    except Exception as e:
        try:
            with open(args.log, "a", encoding="utf-8") as f:
                f.write(json.dumps({"event": "error", "error": str(e), "ts": time.time()}) + "\n")
        except OSError:
            pass
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
