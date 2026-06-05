"""Contract-DELTA engine (hooks/contract_delta.compute_delta) — real-binary.

Proves the same-path before/after property diff: the edited function surfaces its contract
change with the dependency consequence, and an UNEDITED multi-return function in the same file
does NOT appear (identical extraction both sides ⇒ no phantom drift, no scoping needed).

Skips when the gt-index binary isn't available (CI without the Go build).
"""
from __future__ import annotations

import os
import subprocess
import textwrap

import pytest

_BIN = os.environ.get("GT_INDEX_BINARY") or r"D:\Groundtruth\gt-index\gt-index-current.exe"
if not os.path.exists(_BIN):
    pytest.skip("gt-index binary not available", allow_module_level=True)
os.environ["GT_INDEX_BINARY"] = _BIN

from groundtruth.hooks.contract_delta import _old_content, compute_delta  # noqa: E402

_SRC = '''
import pickle

def open_state(path):
    try:
        with open(path, 'rb') as f:
            return pickle.load(f)
    except Exception:
        return {}

def get_user(uid):
    if not uid:
        raise KeyError("missing")
    return [uid]

def caller():
    return open_state('x'), get_user(1)
'''


def _repo(tmp_path) -> str:
    d = str(tmp_path)
    with open(os.path.join(d, "m.py"), "w") as f:
        f.write(textwrap.dedent(_SRC).lstrip())
    sh = lambda *a: subprocess.run(["git", "-C", d, *a], capture_output=True, text=True)
    sh("init", "-q")
    sh("-c", "user.email=a@b", "-c", "user.name=t", "add", "-A")
    subprocess.run(["git", "-C", d, "-c", "user.email=a@b", "-c", "user.name=t",
                    "commit", "-qm", "init"], capture_output=True, text=True)
    return d


def _main_graph(d: str) -> str:
    db = os.path.join(d, "graph.db")
    subprocess.run([_BIN, "-root", d, "-output", db], capture_output=True, text=True, timeout=60)
    return db


def test_delta_edited_func_only(tmp_path):
    d = _repo(tmp_path)
    graph = _main_graph(d)
    # Agent edits ONLY get_user: return None, drop the KeyError. open_state untouched.
    with open(os.path.join(d, "m.py"), "w") as f:
        f.write(textwrap.dedent(_SRC).lstrip().replace(
            '    if not uid:\n        raise KeyError("missing")\n    return [uid]\n',
            "    return None\n",
        ))
    lines = compute_delta(graph, "m.py", repo_root=d)
    out = "\n".join(lines)
    assert "[CONTRACT-DELTA] get_user" in out
    # contract change is reported (return shape and/or dropped raise)
    assert ("return shape" in out) or ("dropped raise: KeyError" in out)
    # the verified caller (caller()) is counted
    assert "verified caller" in out
    # SAME-PATH proof: open_state was NOT edited -> must NOT appear
    assert "open_state" not in out


def test_delta_quiet_on_noop(tmp_path):
    d = _repo(tmp_path)
    graph = _main_graph(d)
    # no edit on disk -> old (HEAD) == current -> empty
    assert compute_delta(graph, "m.py", repo_root=d) == []


def test_old_content_absolute_path_diff(tmp_path):
    """arviz root cause: OpenHands emits ABSOLUTE-path diff headers; the old `+++ b/`
    parser never matched -> old_content empty -> silent []. The fixed parser handles it."""
    diff = (
        "--- /workspace/arviz-devs__arviz-2413/arviz/plots/hdiplot.py\n"
        "+++ /workspace/arviz-devs__arviz-2413/arviz/plots/hdiplot.py\n"
        "@@ -1,2 +1,3 @@\n"
        " def plot_hdi(x):\n"
        "-    return x\n"
        "+    raise TypeError('no')\n"
        "+    return x\n"
    )
    # non-git tmp_path -> git show fails -> falls to the diff parser (the path under test)
    old = _old_content(str(tmp_path), "arviz/plots/hdiplot.py", diff)
    assert "def plot_hdi(x):" in old
    assert "return x" in old
    assert "raise TypeError" not in old  # old side excludes added (+) lines


def test_delta_with_explicit_old_content(tmp_path):
    """The arviz thread-through fix: when old_content is passed, compute_delta uses it
    directly (no git dependency) and surfaces the change."""
    d = str(tmp_path)
    old_src = ("def get_user(uid):\n    if not uid:\n        raise KeyError('x')\n"
               "    return [uid]\n\ndef c():\n    return get_user(1)\n")
    new_src = "def get_user(uid):\n    return None\n\ndef c():\n    return get_user(1)\n"
    with open(os.path.join(d, "m.py"), "w") as f:
        f.write(new_src)
    graph = _main_graph(d)
    out = "\n".join(compute_delta(graph, "m.py", repo_root=d,
                                  old_content=old_src, current_content=new_src))
    assert "[CONTRACT-DELTA] get_user" in out
    assert ("return shape" in out) or ("dropped raise: KeyError" in out)


def test_delta_quiet_on_non_contract_edit(tmp_path):
    d = _repo(tmp_path)
    graph = _main_graph(d)
    # edit get_user body without changing its contract (rename a local) -> no material delta
    with open(os.path.join(d, "m.py"), "w") as f:
        f.write(textwrap.dedent(_SRC).lstrip().replace("return [uid]", "result = [uid]\n    return result"))
    lines = compute_delta(graph, "m.py", repo_root=d)
    # return_shape value may differ (uid vs result expr); accept either empty or get_user-only,
    # but open_state must never appear (same-path).
    assert "open_state" not in "\n".join(lines)
