#!/usr/bin/env python3
"""GT localization-efficacy extractor (process metrics from a DeepSWE/mini-swe-agent trajectory).

Built ARTIFACT-FIRST (TTD) against real baseline run 28622361642 trajectories
(mini-swe-agent-1.1: agent/trajectory.json → steps[].tool_calls[].arguments.command +
observation). Computes the GT_THREE_PROBLEMS_20260701 process metrics — these are
DIAGNOSTICS, never an acceptance gate; every process win must be PAIRED against
correctness no-regression (resolved / f2p / p2p), which this also emits.

Per task:
  total_actions              # agent bash steps
  first_edit_action          # 1-indexed step of the first file WRITE (fix begins)
  first_source_edit_action   # first write to a NON-test repo file
  search_before_first_edit   # READ/SEARCH actions before the first edit (the localization cost)
  files_read / files_edited  # distinct repo source files read vs written
  files_opened_never_edited  # wrong-branch reads (opened, never edited)
  first_hit_precision        # was the first repo file opened one the agent eventually edited?
  resolved / f2p / p2p       # correctness pairing (no-regression guard)

Usage:
  gt_localization_efficacy.py --traj <path/to/agent/trajectory.json> [--task NAME]
  gt_localization_efficacy.py --artifact-dir <downloaded artifact root>   # auto-finds traj+reward
  gt_localization_efficacy.py --run-dir <dir of many artifact dirs> --out <aggregate.json>
"""
from __future__ import annotations
import argparse, json, os, re, glob, posixpath

SRC_EXT = r"(?:py|go|ts|tsx|js|jsx|mjs|cjs|rs|java|kt|rb|c|cc|cpp|h|hpp|html|vue|svelte)"
_FILE_TOKEN = re.compile(r"[\w./+-]+\.%s\b" % SRC_EXT)
_OPEN_IN_PY = re.compile(r"""open\(\s*['"]([^'"]+)['"]""")
_PY_WRITE = re.compile(r"""open\([^)]*,\s*['"][wa]|\.write(?:lines)?\(|write_text\(|\.writelines\(|Path\([^)]*\)\.write""")


def _strip_cd(cmd: str) -> str:
    return re.sub(r"^\s*cd\s+\S+\s*&&\s*", "", cmd.strip())


def _cd_dir(cmd: str):
    m = re.match(r"^\s*cd\s+(\S+)\s*&&", cmd)
    return m.group(1) if m else None


def _resolve(f: str, cd: str | None) -> str:
    """Resolve a file token to an absolute repo path using the command's cd prefix."""
    if f.startswith("/"):
        return posixpath.normpath(f)
    f = f.lstrip("./")
    if cd:
        return posixpath.normpath(cd.rstrip("/") + "/" + f)
    return f


def _redirect_to_source(cmd: str) -> bool:
    # a real write redirect to a source file: `> foo.py`, `>> foo.ts`; exclude 2>, >/dev/null, >&
    for m in re.finditer(r"(?<!2)>>?\s*([\w./+-]+)", cmd):
        tgt = m.group(1)
        if tgt in ("/dev/null",) or tgt.startswith("&"):
            continue
        if re.search(r"\.%s$" % SRC_EXT, tgt):
            return True
    return False


def classify(cmd: str):
    """Return (kind, files, is_test_target). kind in READ|EDIT|RUN|NAV."""
    c = _strip_cd(cmd)
    verb = (c.split() or ["?"])[0]
    low = c

    # ---- EDIT detection (a repo file is written) ----
    is_edit = False
    if re.search(r"\bsed\s+-i\b", low):
        is_edit = True
    elif re.search(r"\bcat\s*>>?\s*[\w./+-]", low):
        is_edit = True
    elif re.search(r"\btee\b", low):
        is_edit = True
    elif re.search(r"\bapply_patch\b|\bgit\s+apply\b|(?<![\w])patch\s+.*<", low):
        is_edit = True
    elif _redirect_to_source(low):
        is_edit = True
    elif re.search(r"\becho\b.*>>?\s*[\w./+-]+\.%s" % SRC_EXT, low):
        is_edit = True
    elif verb.startswith("python") and _PY_WRITE.search(low):
        is_edit = True

    cd = _cd_dir(cmd)
    files = set()
    # files named literally on the command line
    for m in _FILE_TOKEN.finditer(cmd):
        f = _resolve(m.group(0), cd)
        if not f.startswith(("/usr", "/opt", "/app/.local", ".local", "/tmp")) and "/.local/" not in f and "site-packages" not in f:
            files.add(f)
    # files opened inside a python heredoc/-c
    for m in _OPEN_IN_PY.finditer(cmd):
        f = m.group(1)
        if re.search(r"\.%s$" % SRC_EXT, f):
            files.add(_resolve(f, cd))

    is_test = any(re.search(r"(^|/)test_|_test\.|(^|/)tests?/", f) for f in files) if files else False

    if is_edit:
        return "EDIT", files, is_test

    # ---- RUN / TEST (not an edit) ----
    if re.search(r"\bpytest\b|\b-m\s+pytest\b|\bnpx\s+(?:jest|vitest|mocha)\b|\bcargo\s+test\b|\bgo\s+test\b|\bnpm\s+(?:run\s+)?test\b|\bdeno\s+test\b|\bbun\s+test\b", low):
        return "RUN", files, is_test
    if verb.startswith("python") and re.search(r"<<|-c\b|\.py\b", low) and not _PY_WRITE.search(low):
        return "RUN", files, is_test

    # ---- READ / SEARCH ----
    if re.match(r"(cat|grep|rg|find|ls|head|tail|wc|nl|less|more|awk|tree|diff|file|stat|cut|sort|uniq|sed)\b", c):
        if re.match(r"git\b", c):
            pass
        return "READ", files, is_test
    if re.match(r"git\s+(diff|show|log|status|blame|grep|ls-files|cat-file)", c):
        return "READ", files, is_test

    return "NAV", files, is_test


def analyze_traj(traj_path: str):
    tj = json.load(open(traj_path, encoding="utf-8"))
    steps = tj.get("steps") if isinstance(tj, dict) else None
    if steps is None:
        raise ValueError("not a mini-swe-agent trajectory.json (no 'steps')")
    actions = []
    for s in steps:
        if s.get("source") != "agent":
            continue
        tcs = s.get("tool_calls") or []
        if not tcs:
            continue
        cmd = (tcs[0].get("arguments") or {}).get("command", "")
        kind, files, is_test = classify(cmd)
        actions.append({"kind": kind, "files": files, "is_test": is_test})

    total = len(actions)
    files_read, files_edited, files_edited_src = set(), set(), set()
    first_edit = None
    first_source_edit = None
    reads_before_first_edit = 0
    first_file_opened = None
    for i, a in enumerate(actions, 1):
        if a["kind"] == "READ":
            for f in a["files"]:
                files_read.add(f)
            if first_file_opened is None:
                srcs = [f for f in a["files"] if not a["is_test"]]
                if srcs:
                    first_file_opened = sorted(srcs)[0]
        if a["kind"] == "EDIT":
            if first_edit is None:
                first_edit = i
            for f in a["files"]:
                files_edited.add(f)
                if not a["is_test"]:
                    files_edited_src.add(f)
            if first_source_edit is None and not a["is_test"] and a["files"]:
                first_source_edit = i
        if first_edit is None and a["kind"] in ("READ", "RUN"):
            reads_before_first_edit += 1

    opened_never_edited = sorted(files_read - files_edited)
    first_hit = (first_file_opened in files_edited) if first_file_opened else None
    fm = tj.get("final_metrics", {}) or {}
    return {
        "total_actions": total,
        "first_edit_action": first_edit,
        "first_source_edit_action": first_source_edit,
        "search_before_first_edit": reads_before_first_edit,
        "n_files_read": len(files_read),
        "n_files_edited": len(files_edited),
        "n_files_edited_source": len(files_edited_src),
        "files_opened_never_edited": opened_never_edited,
        "n_files_opened_never_edited": len(opened_never_edited),
        "first_file_opened": first_file_opened,
        "first_hit_precision": first_hit,
        "total_cost_usd": fm.get("total_cost_usd"),
        "total_steps_reported": fm.get("total_steps"),
    }


def find_correctness(artifact_dir: str):
    out = {"resolved": None, "f2p": None, "p2p": None}
    for rp in glob.glob(os.path.join(artifact_dir, "**", "reward.json"), recursive=True):
        try:
            r = json.load(open(rp, encoding="utf-8"))
            out["resolved"] = r.get("resolved", r.get("reward", r.get("success")))
            out["f2p"] = r.get("fail_to_pass", r.get("f2p"))
            out["p2p"] = r.get("pass_to_pass", r.get("p2p"))
            break
        except Exception:
            continue
    if out["resolved"] is None:
        for op in glob.glob(os.path.join(artifact_dir, "**", "outcome.json"), recursive=True):
            try:
                o = json.load(open(op, encoding="utf-8"))
                out["resolved"] = o.get("resolved", o.get("success"))
                break
            except Exception:
                continue
    return out


def analyze_artifact_dir(artifact_dir: str, task: str | None = None):
    trajs = glob.glob(os.path.join(artifact_dir, "**", "agent", "trajectory.json"), recursive=True)
    if not trajs:
        raise FileNotFoundError(f"no agent/trajectory.json under {artifact_dir}")
    res = analyze_traj(trajs[0])
    res.update(find_correctness(artifact_dir))
    res["task"] = task or _task_from_path(trajs[0])
    return res


def _task_from_path(p: str):
    m = re.search(r"deepswe-full-([^/\\]+)", p)
    if m:
        return m.group(1)
    # the run job dir is <slug>__<hash>; the slug starts with a letter (skip 2026-07-02 date dirs)
    for m in re.finditer(r"([a-z][a-z0-9-]+)__[A-Za-z0-9]+", p):
        return m.group(1)
    return os.path.basename(os.path.dirname(os.path.dirname(p)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--traj")
    ap.add_argument("--artifact-dir")
    ap.add_argument("--run-dir")
    ap.add_argument("--task")
    ap.add_argument("--out")
    a = ap.parse_args()
    if a.traj:
        print(json.dumps(analyze_traj(a.traj), indent=2, default=list))
    elif a.artifact_dir:
        print(json.dumps(analyze_artifact_dir(a.artifact_dir, a.task), indent=2, default=list))
    elif a.run_dir:
        rows = []
        for d in sorted(glob.glob(os.path.join(a.run_dir, "*"))):
            if not os.path.isdir(d):
                continue
            try:
                rows.append(analyze_artifact_dir(d, task=os.path.basename(d)))
            except Exception as e:
                rows.append({"artifact": os.path.basename(d), "error": str(e)})
        blob = {"rows": rows}
        print(json.dumps(blob, indent=2, default=list))
        if a.out:
            json.dump(blob, open(a.out, "w"), indent=2, default=list)
    else:
        ap.error("one of --traj / --artifact-dir / --run-dir required")


if __name__ == "__main__":
    main()
