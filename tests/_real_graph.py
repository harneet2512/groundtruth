"""Build real graph.db files with the gt-index producer from this checkout.

Typed-query tests must exercise graphs the producer actually writes (label
taxonomy, analysis-layer Callsite nodes, framework edges, file hashes), not
only hand-inserted rows.  This helper compiles ``gt-index`` from the
repository's own ``gt-index/`` module once per test session and indexes small
fixture repositories written by the tests.

Override the binary with ``GT_TEST_GT_INDEX=<path>``.  Without Go on PATH (and
no override) the real-graph tests skip rather than fail.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
GT_INDEX_MODULE = REPO_ROOT / "gt-index"

# A complete producer identity: an unstamped build rolls the analysis phase
# back (``incomplete graph completion identity``) and the graph would lack the
# analysis-layer nodes these tests must see.
_LDFLAGS = (
    "-X main.commitSHA=test-commit "
    "-X main.buildTimeUTC=2026-08-29T00:00:00Z "
    "-X main.sourceFingerprint=test-source "
    "-X main.compiledBuildTags=sqlite_fts5 "
    "-X main.goToolchain=test-toolchain"
)


def build_gt_index(out_dir: Path) -> Path | None:
    """Return a usable gt-index binary, or None when none can be produced."""
    override = os.environ.get("GT_TEST_GT_INDEX", "").strip()
    if override:
        path = Path(override)
        return path if path.is_file() else None
    go = shutil.which("go")
    if go is None or not (GT_INDEX_MODULE / "cmd" / "gt-index").is_dir():
        return None
    exe = out_dir / ("gt-index.exe" if os.name == "nt" else "gt-index")
    proc = subprocess.run(
        [go, "build", "-tags", "sqlite_fts5", "-ldflags", _LDFLAGS, "-o", str(exe), "./cmd/gt-index"],
        cwd=str(GT_INDEX_MODULE),
        capture_output=True,
        text=True,
        timeout=900,
    )
    if proc.returncode != 0 or not exe.is_file():
        raise RuntimeError(f"gt-index build failed:\n{proc.stdout}\n{proc.stderr}")
    return exe


def write_repo(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
    return root


def index_repo(
    binary: Path,
    root: Path,
    db: Path,
    *,
    source_revision: str | None = None,
) -> Path:
    """Index ``root`` into ``db``.  ``source_revision`` stamps
    ``project_meta.source_revision`` the way the HAR-90 producer contract
    (``-source-revision``) does, so graph-revision binding can be exercised
    before every producer build carries the flag."""
    proc = subprocess.run(
        [str(binary), "-root", str(root), "-output", str(db)],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if proc.returncode != 0 or not db.is_file():
        raise RuntimeError(f"gt-index failed:\n{proc.stdout}\n{proc.stderr}")
    if source_revision is not None:
        conn = sqlite3.connect(str(db))
        try:
            conn.execute(
                "INSERT OR REPLACE INTO project_meta (key, value) VALUES ('source_revision', ?)",
                (source_revision,),
            )
            conn.commit()
        finally:
            conn.close()
    return db
