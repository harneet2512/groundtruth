"""v1.0.5 pre-submit gate — fires on OH ``finish`` action via PreToolUse hook.

Logic:
  1. Diff `git -C $WORKSPACE diff --name-only HEAD` → list of edited files
  2. Read /tmp/gt_check_log.jsonl → set of files agent ran gt_check on
  3. For each edited file NOT covered: emit
     ``<gt-intervention expected="gt_check <file>" expires="+2">``
  4. Track attempts in /tmp/gt_finish_attempts_<instance_id>.json
  5. Soft-escape: after 3 failed gate attempts, allow finish through and
     log the bypass.

Hook contract: this script's stdout becomes part of the agent's context.
We never block hard — exit 0 always — because OH 0.54's hook block
semantic is implementation-specific. The system_message tells the agent
to treat ``<gt-intervention expected="...">`` as ground truth.

Env consumed:
  GT_INSTANCE_ID — per-task counter key (defaults to "global")
  GT_WORKSPACE   — repo root (defaults to /workspace, falls back to /testbed)

Files read:
  /tmp/gt_check_log.jsonl

Files written:
  /tmp/gt_finish_attempts_<id>.json
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

_MAX_ATTEMPTS = 3


def _instance_id() -> str:
    iid = os.environ.get("GT_INSTANCE_ID", "").strip()
    if iid:
        return iid.replace("/", "_").replace("..", "_")
    return "global"


def _attempts_path() -> str:
    return os.path.join(tempfile.gettempdir(), f"gt_finish_attempts_{_instance_id()}.json")


def _check_log_path() -> str:
    return "/tmp/gt_check_log.jsonl"


def _workspace() -> str:
    ws = os.environ.get("GT_WORKSPACE", "").strip()
    if ws and os.path.isdir(ws):
        return ws
    for cand in ("/workspace", "/testbed"):
        if os.path.isdir(cand):
            return cand
    return os.getcwd()


def _edited_files(workspace: str) -> list[str]:
    try:
        env = dict(os.environ)
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "safe.directory"
        env["GIT_CONFIG_VALUE_0"] = "*"
        result = subprocess.run(
            ["git", "-C", workspace, "diff", "--name-only", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    files = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
    return [f for f in files if not _is_test_file(f)]


def _is_test_file(path: str) -> bool:
    """Delegate to the canonical test predicate (``path_policy.is_test_path``, full
    ``walker.IsTestFile`` parity). The prior thin copy caught only ``test_``/``_test.py``
    + a few dirs, so on a non-Python repo the row-27 gate leg treated ``*_test.go`` /
    ``FooTest.java`` / ``*.test.ts`` / ``*_spec.rb`` / ``conftest.py`` / ``tests.rs`` as
    source (parity gap flagged in GT_MINI_OH_PORT_GUIDE §IV.6)."""
    from groundtruth.delivery.path_policy import is_test_path

    return is_test_path(path)


def _checked_files(instance_id: str) -> set[str]:
    """Files covered by gt_check for the current instance, per /tmp/gt_check_log.jsonl."""
    path = _check_log_path()
    if not os.path.exists(path):
        return set()
    out: set[str] = set()
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("instance_id") != instance_id:
                    continue
                f = rec.get("file")
                if isinstance(f, str) and f:
                    out.add(f)
    except OSError:
        pass
    return out


def _load_attempts() -> int:
    path = _attempts_path()
    if not os.path.exists(path):
        return 0
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return int(data.get("attempts", 0))
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return 0


def _save_attempts(n: int) -> None:
    path = _attempts_path()
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"attempts": n}, fh)
        os.replace(tmp, path)
    except OSError:
        pass


def _normalize(path: str) -> str:
    return path.strip().replace("\\", "/").lstrip("./")


def main() -> int:
    iid = _instance_id()
    ws = _workspace()
    edited = [_normalize(f) for f in _edited_files(ws)]
    if not edited:
        # Nothing edited — finish is fine. Reset attempts so a future task
        # starts clean.
        _save_attempts(0)
        _log_gate(iid, edited=[], checked=[], uncovered=[], attempt=0,
                  decision="allow_no_edits", intervention="")
        return 0

    checked = {_normalize(f) for f in _checked_files(iid)}
    uncovered = [f for f in edited if f not in checked]

    if not uncovered:
        _save_attempts(0)
        _log_gate(iid, edited=edited, checked=sorted(checked), uncovered=[],
                  attempt=0, decision="allow_full_coverage", intervention="")
        return 0

    attempts = _load_attempts() + 1
    _save_attempts(attempts)

    if attempts > _MAX_ATTEMPTS:
        # Soft-escape: allow finish, log bypass.
        intervention = (
            f"<gt-intervention id=\"finish-gate-bypass\" "
            f"attempts=\"{attempts}\" outcome=\"soft-escape\">\n"
            f"Pre-submit gate: {len(uncovered)} edited file(s) without "
            f"gt_check coverage. Soft-escape after {_MAX_ATTEMPTS} attempts; "
            f"finish allowed through but logged as gate bypass."
            f"\n</gt-intervention>"
        )
        print(intervention, flush=True)
        _log_gate(iid, edited=edited, checked=sorted(checked), uncovered=uncovered,
                  attempt=attempts, decision="soft_escape", intervention=intervention)
        return 0

    # Build intervention text — point to the FIRST uncovered file (one focal
    # action per turn keeps the agent on rails).
    target = uncovered[0]
    intervention = (
        f"<gt-intervention id=\"finish-gate-{iid}-{attempts}\" "
        f"expected=\"gt_check {target}\" expires=\"+2\">\n"
        f"Finish blocked (attempt {attempts}/{_MAX_ATTEMPTS}): "
        f"material edit at {target} was not verified. "
        f"Run gt_check {target} before retrying finish. "
        f"Other uncovered files: {len(uncovered) - 1}."
        f"\n</gt-intervention>"
    )
    print(intervention, flush=True)
    _log_gate(iid, edited=edited, checked=sorted(checked), uncovered=uncovered,
              attempt=attempts, decision="block", intervention=intervention)
    return 0


def _log_gate(
    iid: str,
    *,
    edited: list[str],
    checked: list[str],
    uncovered: list[str],
    attempt: int,
    decision: str,
    intervention: str,
) -> None:
    """Best-effort layer5 telemetry. Never raises."""
    try:
        from groundtruth.runtime.v105_telemetry import log_gate

        log_gate(
            instance_id=iid,
            edited_files=edited,
            checked_files=checked,
            uncovered=uncovered,
            attempt=attempt,
            decision=decision,
            intervention=intervention,
        )
    except Exception:
        pass


if __name__ == "__main__":
    sys.exit(main())
