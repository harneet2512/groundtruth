"""G1 (submit-gate hygiene arm) — Stage-1 deterministic tests.

diff_hygiene BLOCKS categorically on a binary blob in the diff (the class that
poisoned the net-negative run: an 11.5 MB `abs` binary staged into the patch),
which the extension-only scripts/swebench/patch_hygiene.classify_file cannot see.
Oversize/lockfile stay ADVISORY (correct-or-quiet; plan §6 invariant ②).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from groundtruth.runtime import submit_gate as sg
from groundtruth.runtime.patch_auditor import diff_hygiene, git_diff_hygiene


def test_binary_in_numstat_blocks():
    numstat = "-\t-\tabs\n12\t3\tsrc/main.py\n"
    v = diff_hygiene(numstat=numstat)
    assert v["blocking"] is True
    assert v["reason"] == "binary_file_in_diff"
    assert "abs" in v["binary_files"]


def test_clean_source_diff_is_not_blocking():
    numstat = "12\t3\tsrc/main.py\n4\t1\tsrc/util.py\n"
    v = diff_hygiene(numstat=numstat)
    assert v["blocking"] is False
    assert v["reason"] == ""


def test_oversize_is_advisory_not_blocking(monkeypatch):
    monkeypatch.setenv("GT_HYGIENE_MAX_ADDED_LINES", "100")
    numstat = "5000\t0\tsrc/bundle.js\n"
    v = diff_hygiene(numstat=numstat)
    assert v["blocking"] is False  # oversize never hard-blocks
    assert v["reason"] == "oversize_addition"
    assert v["oversize_files"] and v["oversize_files"][0]["path"] == "src/bundle.js"


def test_lockfile_is_advisory_not_blocking():
    numstat = "800\t0\tpackage-lock.json\n"
    v = diff_hygiene(numstat=numstat)
    assert v["blocking"] is False
    assert v["reason"] == "lockfile_touched"
    assert "package-lock.json" in v["lockfiles"]


def test_binary_wins_over_oversize_and_lockfile():
    numstat = "-\t-\tblob.bin\n99999\t0\tbig.txt\n1\t0\tCargo.lock\n"
    v = diff_hygiene(numstat=numstat)
    assert v["blocking"] is True
    assert v["reason"] == "binary_file_in_diff"


def test_raw_patch_binary_fallback():
    patch = "diff --git a/x.dat b/x.dat\nBinary files a/x.dat and b/x.dat differ\n"
    v = diff_hygiene(numstat="", patch=patch)
    assert v["blocking"] is True
    assert "x.dat" in v["binary_files"]


def test_git_binary_patch_marker_fallback():
    patch = "diff --git a/x b/x\nGIT binary patch\nliteral 10\n"
    v = diff_hygiene(numstat="", patch=patch)
    assert v["blocking"] is True


def test_gate_blocks_on_binary_hygiene():
    hyg = diff_hygiene(numstat="-\t-\tabs\n")
    v = sg.gate_verdict(covering={"verdict": "pass"}, hygiene=hyg)
    assert v.allow is False
    assert v.reason == "hygiene"


# ---------------------------------------------------------------------------
# REAL git repo — stage a binary blob, prove git_diff_hygiene blocks it.
# ---------------------------------------------------------------------------
def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


@pytest.mark.skipif(subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
                    reason="git not available")
def test_real_git_binary_blob_blocks(tmp_path: Path):
    repo = tmp_path
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    (repo / "README.md").write_text("base\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-qm", "base")
    # a real binary blob (NUL bytes) staged into the diff — the `abs` scenario.
    (repo / "abs").write_bytes(bytes(range(256)) * 64)
    _git(repo, "add", "abs")
    v = git_diff_hygiene(str(repo))
    assert v["blocking"] is True, v
    assert any(b.endswith("abs") for b in v["binary_files"]), v
    # the gate refuses the submit.
    assert sg.gate_verdict(hygiene=v).allow is False
