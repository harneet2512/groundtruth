"""GT_OBLIGATIONS_V2 — guard for the cert-sidecar mirror (2026-07-08 witness gap).

Run 28975223607 shipped T1 (via brief.txt) but T2/T3 never reached the agent
because emit_brief mirrored ONLY gt_issue_anchors.json into out_dir — the
obligations v2 files were written to /tmp but never crossed the cert-dir
handoff. This pins the enumerated sidecar list + the mirror so a new sidecar
cannot be silently forgotten again."""
from __future__ import annotations

import importlib.util
import os
import tempfile

_PROOF_PATH = os.path.join(
    os.path.dirname(__file__), "..", "scripts", "swebench", "gt_run_proof.py"
)
_spec = importlib.util.spec_from_file_location("gt_run_proof_sidecar_t", _PROOF_PATH)
_mod = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader
_spec.loader.exec_module(_mod)


def test_cert_sidecars_enumeration_includes_obligations_v2():
    # the enumeration is the guard: T2/T3 depend on these two reaching out_dir
    assert "gt_obligations_v2.json" in _mod._CERT_SIDECARS
    assert "gt_obligations.md" in _mod._CERT_SIDECARS
    assert "gt_issue_anchors.json" in _mod._CERT_SIDECARS  # unchanged v1 behavior


def test_mirror_copies_present_obligations_files():
    with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as out:
        for name in ("gt_issue_anchors.json", "gt_obligations_v2.json",
                     "gt_obligations.md"):
            with open(os.path.join(src, name), "w", encoding="utf-8") as f:
                f.write("{}" if name.endswith(".json") else "- [ ] x")
        mirrored = _mod._mirror_cert_sidecars(out, src_dir=src)
        assert set(mirrored) == set(_mod._CERT_SIDECARS)
        for name in _mod._CERT_SIDECARS:
            assert os.path.exists(os.path.join(out, name)), f"{name} not mirrored"


def test_mirror_is_noop_when_absent():
    # flag-off / absent source -> byte-identical no-op (nothing mirrored, no error)
    with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as out:
        assert _mod._mirror_cert_sidecars(out, src_dir=src) == []
        assert os.listdir(out) == []


def test_mirror_partial_only_present():
    # anchors present, obligations absent (flag off) -> only anchors mirrored
    with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as out:
        with open(os.path.join(src, "gt_issue_anchors.json"), "w") as f:
            f.write("{}")
        mirrored = _mod._mirror_cert_sidecars(out, src_dir=src)
        assert mirrored == ["gt_issue_anchors.json"]
        assert not os.path.exists(os.path.join(out, "gt_obligations_v2.json"))
