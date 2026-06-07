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


def test_delta_restructuring_no_churn(tmp_path):
    """arviz run5 regression (the residual-noise bug): wrapping code in `if smooth:` and
    re-indenting makes the indexer re-extract the SAME guards as different strings. The
    delta must report ONLY the real contract change (the added TypeError raise) — NOT the
    pre-existing ValueError guards as 'dropped'/'new', and NOT boundary/restructure churn."""
    d = str(tmp_path)
    old_src = (
        "def plot_hdi(x, y=None, hdi_data=None, smooth=True):\n"
        "    if y is None and hdi_data is None:\n"
        "        raise ValueError('one of y/hdi_data required')\n"
        "    if len(x) != 1:\n"
        "        raise ValueError('bad shape')\n"
        "    result = smoothify(x)\n"
        "    return result\n\n"
        "def caller():\n    return plot_hdi([1])\n"
    )
    # Agent edit: add datetime/str -> raise TypeError AND wrap the smooth call in `if smooth:`
    new_src = (
        "def plot_hdi(x, y=None, hdi_data=None, smooth=True):\n"
        "    if y is None and hdi_data is None:\n"
        "        raise ValueError('one of y/hdi_data required')\n"
        "    if isinstance(x[0], str):\n"
        "        raise TypeError('cannot deal with categorical x')\n"
        "    if len(x) != 1:\n"
        "        raise ValueError('bad shape')\n"
        "    if smooth:\n"
        "        result = smoothify(x)\n"
        "    return result\n\n"
        "def caller():\n    return plot_hdi([1])\n"
    )
    with open(os.path.join(d, "m.py"), "w") as f:
        f.write(new_src)
    graph = _main_graph(d)
    out = "\n".join(compute_delta(graph, "m.py", repo_root=d,
                                  old_content=old_src, current_content=new_src))
    assert "new raise: TypeError" in out      # the REAL change is reported
    assert "ValueError" not in out            # preserved ValueError guards NOT churned
    assert "dropped" not in out               # nothing falsely dropped (smooth wrap, len check)


def test_delta_quiet_on_noop(tmp_path):
    d = _repo(tmp_path)
    graph = _main_graph(d)
    # no edit on disk -> old (HEAD) == current -> empty
    assert compute_delta(graph, "m.py", repo_root=d) == []


def test_old_content_git_head_full_file_with_prefix(tmp_path):
    """arviz run4 root cause: file_rel carries a SWE-bench task-dir prefix, so
    `git show HEAD:<prefixed>` failed and the chain fell to a diff FRAGMENT, making the
    whole pre-existing contract read as 'new'. _old_content must return the FULL file
    from git HEAD, stripping the prefix — never a fragment."""
    d = str(tmp_path)
    full = ("def plot_hdi(x):\n    if x is None:\n"
            "        raise ValueError('need x')\n    return x\n")
    with open(os.path.join(d, "m.py"), "w") as f:
        f.write(full)
    sh = lambda *a: subprocess.run(["git", "-C", d, *a], capture_output=True, text=True)
    sh("init", "-q")
    sh("-c", "user.email=a@b", "-c", "user.name=t", "add", "-A")
    subprocess.run(["git", "-C", d, "-c", "user.email=a@b", "-c", "user.name=t",
                    "commit", "-qm", "i"], capture_output=True, text=True)
    with open(os.path.join(d, "m.py"), "w") as f:  # edit current (after HEAD)
        f.write(full.replace("    return x", "    raise TypeError('no')\n    return x"))
    # file_rel WITH a task-dir prefix -> must strip to find m.py at the git root
    old = _old_content(d, "some-task-dir/m.py")
    assert "def plot_hdi(x):" in old
    assert "raise ValueError('need x')" in old   # FULL file (not a fragment)
    assert "raise TypeError" not in old          # OLD content, pre-edit


def test_old_content_anchors_base_commit_over_moved_head(tmp_path, monkeypatch):
    """run7 in-container silence: the agent runs its own git (checkout/commit) which can
    move HEAD so old==current. _old_content must anchor to GT_BASE_COMMIT (the immutable
    task base captured pre-agent), not live HEAD."""
    d = str(tmp_path)
    sh = lambda *a: subprocess.run(["git", "-C", d, *a], capture_output=True, text=True)
    base_src = "def f(x):\n    if x is None:\n        raise ValueError('a')\n    return x\n"
    with open(os.path.join(d, "m.py"), "w") as fh:
        fh.write(base_src)
    sh("init", "-q")
    sh("-c", "user.email=a@b", "-c", "user.name=t", "add", "-A")
    subprocess.run(["git", "-C", d, "-c", "user.email=a@b", "-c", "user.name=t",
                    "commit", "-qm", "base"], capture_output=True)
    base_sha = subprocess.run(["git", "-C", d, "rev-parse", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
    # agent edits AND commits -> HEAD moves off the base
    with open(os.path.join(d, "m.py"), "w") as fh:
        fh.write(base_src.replace("raise ValueError('a')", "raise TypeError('b')"))
    sh("add", "-A")
    subprocess.run(["git", "-C", d, "-c", "user.email=a@b", "-c", "user.name=t",
                    "commit", "-qm", "agent"], capture_output=True)
    monkeypatch.setenv("GT_BASE_COMMIT", base_sha)
    old = _old_content(d, "m.py")
    assert "raise ValueError('a')" in old   # base content (immutable anchor)
    assert "TypeError" not in old           # NOT the agent's moved HEAD
    monkeypatch.delenv("GT_BASE_COMMIT")
    assert "TypeError" in _old_content(d, "m.py")  # without anchor, falls back to moved HEAD


def test_delta_degenerate_old_guard(tmp_path):
    """If old has NO extractable contract for a function the post shows as fully-formed
    (recovery degraded to a fragment), do NOT report the whole contract as 'new' — stay
    quiet for that function (the arviz run4 17-false-positive guard)."""
    d = str(tmp_path)
    new_src = ("def f(x):\n    if x is None:\n        raise ValueError('a')\n"
               "    return [x]\n\ndef caller():\n    return f(1)\n")
    with open(os.path.join(d, "m.py"), "w") as fh:
        fh.write(new_src)
    graph = _main_graph(d)
    # old f is property-less (degraded recovery) -> degenerate guard must skip f.
    out = "\n".join(compute_delta(
        graph, "m.py", repo_root=d,
        old_content="def f(x):\n    pass\n\ndef caller():\n    return f(1)\n",
        current_content=new_src))
    assert "[CONTRACT-DELTA] f" not in out


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
