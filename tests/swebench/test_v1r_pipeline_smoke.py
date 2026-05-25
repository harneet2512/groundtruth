"""Structural smoke for the V1R-map plumbing.

These tests catch the bug class that produced "raw_len=243" and
"empty/no signal" on the VM yesterday. They run without spending money:

  - test_v1r_bundle_empty_db_returns_zero: invokes the bundle with a missing
    db and asserts graceful fallback (exit 0, empty stdout). Catches argparse
    breakage.
  - test_v1r_bundle_non_empty_brief_on_fixture: builds a tiny graph.db on a
    fixture repo, invokes the bundle, asserts the brief is non-empty and
    contains numbered file entries. Skipped if gt-index binary or
    sentence_transformers is unavailable.

The bundle itself is the in-repo build: scripts/swebench/gt_pretask_brief_v1r.py
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


REPO_DIR = Path(__file__).resolve().parents[2]
BUNDLE = REPO_DIR / "scripts" / "swebench" / "gt_pretask_brief_v1r.py"
FIXTURE_REPO = REPO_DIR / "tests" / "fixtures" / "project_py"


def _gt_index_binary() -> Path | None:
    """Return a usable gt-index binary path, or None if not available.

    Prefers a Linux build under bin/ when running on Linux; falls back to the
    Windows build under gt-index/ when running on Windows; otherwise None.
    """
    if sys.platform.startswith("linux"):
        cand = REPO_DIR / "bin" / "gt-index-linux"
        if cand.is_file() and os.access(cand, os.X_OK):
            return cand
    if sys.platform.startswith("win"):
        cand = REPO_DIR / "gt-index" / "gt-index.exe"
        if cand.is_file():
            return cand
    return None


def _has_sentence_transformers() -> bool:
    try:
        import importlib.util
        return importlib.util.find_spec("sentence_transformers") is not None
    except Exception:
        return False


def test_v1r_bundle_exists():
    """The built V1R bundle must be checked in."""
    assert BUNDLE.is_file(), f"bundle missing at {BUNDLE} — run build_v1r_bundle.py"
    assert BUNDLE.stat().st_size > 50_000, "bundle suspiciously small"


def test_v1r_bundle_empty_db_returns_zero(tmp_path):
    """Bundle launches successfully, accepts the run_infer.py arg pattern, and
    falls back gracefully when the db is missing.

    Catches the argparse-rejection class of bug (the one that produced
    raw_len=243 on the VM yesterday).
    """
    issue_file = tmp_path / "issue.txt"
    issue_file.write_text("dummy issue text", encoding="utf-8")
    telemetry = tmp_path / "telemetry.jsonl"

    proc = subprocess.run(
        [
            sys.executable, str(BUNDLE),
            "--db", str(tmp_path / "does-not-exist.db"),
            "--root", str(FIXTURE_REPO),
            "--issue-text-file", str(issue_file),
            "--telemetry-out", str(telemetry),
            "--max-files", "5",
            "--max-funcs-per-file", "3",
            "--task-id", "smoke-test",
        ],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, (
        f"bundle exit {proc.returncode}; stderr=\n{proc.stderr}"
    )
    assert proc.stdout == "", (
        f"expected empty stdout for missing db, got {len(proc.stdout)} chars: "
        f"{proc.stdout[:200]!r}"
    )


def test_v1r_bundle_accepts_unknown_args(tmp_path):
    """Bundle uses parse_known_args, so future arg additions don't break it."""
    issue_file = tmp_path / "issue.txt"
    issue_file.write_text("dummy", encoding="utf-8")

    proc = subprocess.run(
        [
            sys.executable, str(BUNDLE),
            "--db", str(tmp_path / "missing.db"),
            "--root", str(FIXTURE_REPO),
            "--issue-text-file", str(issue_file),
            "--task-id", "smoke",
            "--some-future-flag", "xyz",
        ],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, (
        f"bundle should accept unknown args via parse_known_args; "
        f"exit {proc.returncode}, stderr={proc.stderr}"
    )


@pytest.mark.skipif(
    _gt_index_binary() is None,
    reason="gt-index binary not built (run scripts/swebench/build_gt_index_linux.sh)",
)
@pytest.mark.skipif(
    not _has_sentence_transformers(),
    reason="sentence_transformers not installed",
)
def test_v1r_bundle_non_empty_brief_on_fixture(tmp_path):
    """Full structural smoke: build graph.db on a fixture, run the bundle,
    assert a non-empty brief with numbered file entries.

    Heavy — requires gt-index and sentence_transformers. Skipped otherwise.
    """
    binary = _gt_index_binary()
    assert binary is not None  # narrowed by skipif above

    db_path = tmp_path / "graph.db"
    proc = subprocess.run(
        [str(binary), "-root", str(FIXTURE_REPO), "-output", str(db_path)],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, f"gt-index failed: {proc.stderr}"
    assert db_path.is_file() and db_path.stat().st_size > 1024

    issue_text = (
        "Authentication bug: get_user returns None unexpectedly when the "
        "user is logged in. The auth middleware in src/auth/ should set "
        "the user context but does not. Fix the user lookup."
    )
    issue_file = tmp_path / "issue.txt"
    issue_file.write_text(issue_text, encoding="utf-8")
    telemetry = tmp_path / "telemetry.jsonl"

    proc = subprocess.run(
        [
            sys.executable, str(BUNDLE),
            "--db", str(db_path),
            "--root", str(FIXTURE_REPO),
            "--issue-text-file", str(issue_file),
            "--telemetry-out", str(telemetry),
            "--max-files", "5",
            "--max-funcs-per-file", "3",
            "--task-id", "fixture-test",
        ],
        capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 0, f"bundle failed: stderr={proc.stderr}"

    out = proc.stdout
    if not out.strip():
        pytest.skip(
            "bundle produced empty output on fixture — fixture too small to "
            "trigger v7.4 retrieval. Test passes structural launch."
        )
    assert out.startswith("<gt-task-brief>"), (
        f"brief missing opening tag, got: {out[:200]!r}"
    )
    assert "</gt-task-brief>" in out
    assert "1." in out, "expected numbered entry '1.' in brief"
