"""P0-02 — proof_progress.json / proof_failure.json contract."""

from __future__ import annotations

import importlib.util
import json
import os
import tempfile

_PROOF_PATH = os.path.join(
    os.path.dirname(__file__), "..", "scripts", "swebench", "gt_run_proof.py"
)
_spec = importlib.util.spec_from_file_location("gt_run_proof_t", _PROOF_PATH)
_mod = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader
_spec.loader.exec_module(_mod)
_ProofTracker = _mod._ProofTracker


def test_proof_tracker_writes_progress_and_failure():
    with tempfile.TemporaryDirectory() as td:
        tracker = _ProofTracker(td)
        tracker.complete("env_validation")
        tracker.complete("dep_store", manifest="absent")
        tracker.complete("workspace_metadata", skipped_reason="language_not_metadata_bound")
        rc = tracker.fail("lsp_pass", "LSP_FAIL_NOT_READY", "gopls not ready", edges=73)
        assert rc == 2

        prog = json.load(open(os.path.join(td, "proof_progress.json"), encoding="utf-8"))
        assert prog["schema"] == "gt.proof_progress.v1"
        assert len(prog["stages"]) == 4
        assert prog["stages"][-1]["code"] == "LSP_FAIL_NOT_READY"

        fail = json.load(open(os.path.join(td, "proof_failure.json"), encoding="utf-8"))
        assert fail["schema"] == "gt.proof_failure.v1"
        assert fail["stage"] == "lsp_pass"
        assert fail["edges"] == 73
