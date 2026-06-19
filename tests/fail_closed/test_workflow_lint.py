#!/usr/bin/env python3
"""test_workflow_lint.py — self-test for the pre-matrix workflow lint.

Exercises each of the 4 detector classes with synthetic mini-YAML strings:
every "bad" case must be CAUGHT, every "good" case must be CLEAN. Also runs the
lint against the REAL official workflow files and asserts they pass (so a future
edit that reintroduces a bug class fails CI here, before the paid matrix).
"""

from __future__ import annotations

import os
import sys
import textwrap

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))
_VERIFY = os.path.join(_REPO, "scripts", "verify")
if _VERIFY not in sys.path:
    sys.path.insert(0, _VERIFY)

import workflow_lint as wl  # noqa: E402


def _write(tmp_path, text: str, name: str = "wf.yml") -> str:
    p = tmp_path / name
    p.write_text(textwrap.dedent(text), encoding="utf-8")
    return str(p)


def _classes(violations):
    """Return the set of CLASS tokens present in a violations list."""
    out = set()
    for v in violations:
        for tok in ("CLASS1", "CLASS2", "CLASS3", "CLASS4"):
            if tok in v:
                out.add(tok)
    return out


# ── CLASS 1: bash-ism under sh ───────────────────────────────────────────────


def test_class1_declare_in_container_step_no_shell_bash_caught(tmp_path):
    wf = _write(
        tmp_path,
        """
        name: t
        jobs:
          agent:
            runs-on: ubuntu-latest
            container:
              image: ghcr.io/x/y:latest
            steps:
              - name: bashism
                run: |
                  declare -A MAP
                  echo "${MAP[a]}"
        """,
    )
    v = wl.lint_file(wf)
    assert "CLASS1" in _classes(v), v


def test_class1_pipestatus_in_container_step_caught(tmp_path):
    wf = _write(
        tmp_path,
        """
        name: t
        jobs:
          agent:
            runs-on: ubuntu-latest
            container:
              image: ghcr.io/x/y:latest
            steps:
              - name: pipestatus
                run: |
                  foo | tee log
                  RC=${PIPESTATUS[0]}
        """,
    )
    assert "CLASS1" in _classes(wl.lint_file(wf))


def test_class1_with_job_defaults_shell_bash_clean(tmp_path):
    wf = _write(
        tmp_path,
        """
        name: t
        jobs:
          agent:
            runs-on: ubuntu-latest
            defaults:
              run:
                shell: bash
            container:
              image: ghcr.io/x/y:latest
            steps:
              - name: bashism-but-pinned
                run: |
                  declare -A MAP
                  RC=${PIPESTATUS[0]}
        """,
    )
    assert "CLASS1" not in _classes(wl.lint_file(wf))


def test_class1_with_step_shell_bash_clean(tmp_path):
    wf = _write(
        tmp_path,
        """
        name: t
        jobs:
          agent:
            runs-on: ubuntu-latest
            container:
              image: ghcr.io/x/y:latest
            steps:
              - name: bashism-step-pinned
                shell: bash
                run: |
                  declare -A MAP
                  RC=${PIPESTATUS[0]}
        """,
    )
    assert "CLASS1" not in _classes(wl.lint_file(wf))


def test_class1_non_container_job_bash_default_clean(tmp_path):
    # A non-container ubuntu job defaults to bash, so readarray is fine unpinned.
    wf = _write(
        tmp_path,
        """
        name: t
        jobs:
          prepare:
            runs-on: ubuntu-latest
            steps:
              - name: readarray-on-host
                run: |
                  readarray -t TASKS < /tmp/ids.txt
                  echo "${#TASKS[@]}"
        """,
    )
    assert "CLASS1" not in _classes(wl.lint_file(wf))


# ── CLASS 2: bare pip ────────────────────────────────────────────────────────


def test_class2_bare_pip_caught(tmp_path):
    wf = _write(
        tmp_path,
        """
        name: t
        jobs:
          prepare:
            runs-on: ubuntu-latest
            steps:
              - name: bare-pip
                run: |
                  pip install requests
        """,
    )
    assert "CLASS2" in _classes(wl.lint_file(wf))


def test_class2_python_m_pip_clean(tmp_path):
    wf = _write(
        tmp_path,
        """
        name: t
        jobs:
          prepare:
            runs-on: ubuntu-latest
            steps:
              - name: module-pip
                run: |
                  python -m pip install requests
        """,
    )
    assert "CLASS2" not in _classes(wl.lint_file(wf))


def test_class2_bare_pip_after_or_caught(tmp_path):
    # `import datasets || pip install datasets` — bare pip after `||`.
    wf = _write(
        tmp_path,
        """
        name: t
        jobs:
          prepare:
            runs-on: ubuntu-latest
            steps:
              - name: guard-bare-pip
                run: |
                  python3 -c "import datasets" || pip install datasets
        """,
    )
    assert "CLASS2" in _classes(wl.lint_file(wf))


# ── CLASS 3: dataset fetch unguarded ─────────────────────────────────────────


def test_class3_load_dataset_unguarded_caught(tmp_path):
    wf = _write(
        tmp_path,
        """
        name: t
        jobs:
          prepare:
            runs-on: ubuntu-latest
            steps:
              - name: fetch
                run: |
                  python3 - << 'EOF'
                  from datasets import load_dataset
                  ds = load_dataset("x/y", split="lite")
                  EOF
        """,
    )
    assert "CLASS3" in _classes(wl.lint_file(wf))


def test_class3_load_dataset_guarded_clean(tmp_path):
    wf = _write(
        tmp_path,
        """
        name: t
        jobs:
          prepare:
            runs-on: ubuntu-latest
            steps:
              - name: fetch-guarded
                env:
                  HF_DATASETS_OFFLINE: "0"
                run: |
                  python3 -c "import datasets" || python -m pip install datasets
                  python3 - << 'EOF'
                  from datasets import load_dataset
                  ds = load_dataset("x/y", split="lite")
                  EOF
        """,
    )
    assert "CLASS3" not in _classes(wl.lint_file(wf))


def test_class3_offline_via_export_clean(tmp_path):
    wf = _write(
        tmp_path,
        """
        name: t
        jobs:
          prepare:
            runs-on: ubuntu-latest
            steps:
              - name: fetch-export-offline
                run: |
                  export HF_DATASETS_OFFLINE=0
                  python3 -c "import datasets" || pip install datasets
                  python3 - << 'EOF'
                  from datasets import load_dataset
                  ds = load_dataset("x/y", split="lite")
                  EOF
        """,
    )
    # offline override present (export) AND install guard present -> clean for CLASS3.
    assert "CLASS3" not in _classes(wl.lint_file(wf))


def test_class3_install_only_no_offline_caught(tmp_path):
    wf = _write(
        tmp_path,
        """
        name: t
        jobs:
          prepare:
            runs-on: ubuntu-latest
            steps:
              - name: fetch-install-only
                run: |
                  python3 -c "import datasets" || python -m pip install datasets
                  python3 - << 'EOF'
                  from datasets import load_dataset
                  ds = load_dataset("x/y", split="lite")
                  EOF
        """,
    )
    assert "CLASS3" in _classes(wl.lint_file(wf))


# ── CLASS 4: swallow on required op ──────────────────────────────────────────


def test_class4_gtindex_swallow_echo_caught(tmp_path):
    wf = _write(
        tmp_path,
        """
        name: t
        jobs:
          agent:
            runs-on: ubuntu-latest
            steps:
              - name: build-swallow
                shell: bash
                run: |
                  (cd gt-index && go build -tags sqlite_fts5 -o gt-index ./cmd/gt-index/) || echo WARN
        """,
    )
    assert "CLASS4" in _classes(wl.lint_file(wf))


def test_class4_gtindex_root_swallow_true_caught(tmp_path):
    wf = _write(
        tmp_path,
        """
        name: t
        jobs:
          agent:
            runs-on: ubuntu-latest
            steps:
              - name: index-swallow
                shell: bash
                run: |
                  gt-index -root /repo -output /tmp/g.db || true
        """,
    )
    assert "CLASS4" in _classes(wl.lint_file(wf))


def test_class4_resolve_swallow_caught(tmp_path):
    wf = _write(
        tmp_path,
        """
        name: t
        jobs:
          agent:
            runs-on: ubuntu-latest
            steps:
              - name: resolve-swallow
                shell: bash
                run: |
                  python -m groundtruth.resolve --db /tmp/g.db --resolve || echo WARN
        """,
    )
    assert "CLASS4" in _classes(wl.lint_file(wf))


def test_class4_benign_cp_swallow_clean(tmp_path):
    wf = _write(
        tmp_path,
        """
        name: t
        jobs:
          agent:
            runs-on: ubuntu-latest
            steps:
              - name: benign-copy
                shell: bash
                run: |
                  cp /tmp/foo.json /tmp/bar.json || true
                  cp /tmp/x.log out/ || echo "no log"
        """,
    )
    assert "CLASS4" not in _classes(wl.lint_file(wf))


def test_class4_required_op_no_swallow_clean(tmp_path):
    wf = _write(
        tmp_path,
        """
        name: t
        jobs:
          agent:
            runs-on: ubuntu-latest
            steps:
              - name: fail-fast-build
                shell: bash
                run: |
                  (cd gt-index && go build -tags sqlite_fts5 -o gt-index ./cmd/gt-index/) || { echo FATAL; exit 1; }
        """,
    )
    assert "CLASS4" not in _classes(wl.lint_file(wf))


# ── Exit-code contract ───────────────────────────────────────────────────────


def test_main_returns_1_on_violation(tmp_path):
    wf = _write(
        tmp_path,
        """
        name: t
        jobs:
          prepare:
            runs-on: ubuntu-latest
            steps:
              - name: bad
                run: |
                  pip install x
        """,
    )
    assert wl.main([wf]) == 1


def test_main_returns_0_on_clean(tmp_path):
    wf = _write(
        tmp_path,
        """
        name: t
        jobs:
          prepare:
            runs-on: ubuntu-latest
            steps:
              - name: ok
                run: |
                  python -m pip install x
        """,
    )
    assert wl.main([wf, "--quiet"]) == 0


# ── Real official workflow files must lint clean ─────────────────────────────


@pytest.mark.parametrize(
    "rel",
    [
        os.path.join(".github", "workflows", "swebench_300task.yml"),
        os.path.join(".github", "workflows", "deepswe_full.yml"),
        os.path.join(".github", "actions", "setup-eval", "action.yml"),
    ],
)
def test_real_official_files_clean(rel):
    path = os.path.join(_REPO, rel)
    assert os.path.exists(path), f"missing {path}"
    v = wl.lint_file(path)
    assert v == [], "real workflow has lint violations:\n" + "\n".join(v)
