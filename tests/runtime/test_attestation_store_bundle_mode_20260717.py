"""W1a-PERM — a published attestation bundle must be traversable across uid boundaries.

Live witness (run 29594276655, 15/15 tasks): the container persists bundles as root;
``tempfile.mkdtemp`` creates the staging dir 0o700 by POSIX security contract and
``os.replace`` preserves that mode onto the published ``index/<key>/`` dir, so the
host-side non-root harvester's ``find``/``cp`` got a silent EACCES and reported
"harvested 0 bundle(s)" while every bundle existed. Contract: a published bundle dir
is world-traversable/readable so a different-uid auditor can read entry.json.

The mode assertion is meaningful only on POSIX (Windows has no POSIX dir modes);
CI (GHA ubuntu) and the in-container runtime are exactly the platforms that bit.
"""
from __future__ import annotations

import hashlib
import os
import stat
import sys
from pathlib import Path

import pytest

from groundtruth.runtime.lane_attestation import lane_delivery_candidate_id
from groundtruth.runtime.syntax_observation import build_syntax_observation

ARTIFACT_DIR = str(Path(__file__).resolve().parents[2] / "artifact_deepswe")
if ARTIFACT_DIR not in sys.path:
    sys.path.insert(0, ARTIFACT_DIR)
import gt_mini_patch as gmp  # noqa: E402


def _persist_one_bundle(tmp_path, monkeypatch) -> Path:
    source_path = tmp_path / "src/mod.py"
    source_path.parent.mkdir(parents=True)
    source = b"def f(\n"
    source_path.write_bytes(source)
    block = 'File "src/mod.py", line 1\n    def f(\n         ^\nSyntaxError: never closed'
    shipped = "\n" + block
    observation = build_syntax_observation(
        file_path="src/mod.py",
        source_bytes=source,
        check_result={
            "verdict": "syntax_error", "diagnostic": block, "language": ".py",
            "reason": "parse_error", "checker": ["ast.parse"],
        },
        actual_event="edit_result",
        rendered_block=block,
    )
    monkeypatch.setenv("GT_C_OUT", str(tmp_path))
    monkeypatch.setattr(gmp, "_root", lambda: str(tmp_path))
    monkeypatch.setattr(gmp, "_last_edit_syntax_observation", observation)
    candidate_id = lane_delivery_candidate_id("edit.syntax", "src/mod.py", shipped)
    seal = hashlib.sha256(shipped.encode()).hexdigest()[:16]
    gmp._persist_lane_producer_attestation(
        "edit.syntax", "src/mod.py", block, shipped, candidate_id, seal
    )
    bundles = list((tmp_path / "producer_attestations" / "index").iterdir())
    assert len(bundles) == 1
    return bundles[0]


@pytest.mark.skipif(os.name == "nt", reason="POSIX dir modes only")
def test_published_bundle_dir_is_world_traversable(tmp_path, monkeypatch) -> None:
    bundle = _persist_one_bundle(tmp_path, monkeypatch)
    mode = stat.S_IMODE(os.stat(bundle).st_mode)
    # RED before the fix: mkdtemp's 0o700 survives os.replace -> group/other locked out.
    assert mode & 0o055 == 0o055, (
        f"published bundle dir mode {oct(mode)} is not group/other traversable; "
        "a cross-uid harvester cannot read entry.json"
    )


def test_persist_still_publishes_complete_bundle(tmp_path, monkeypatch) -> None:
    bundle = _persist_one_bundle(tmp_path, monkeypatch)
    assert (bundle / "entry.json").is_file()
    assert (bundle / "attestation.json").is_file()
    assert (bundle / "artifacts").is_dir()
