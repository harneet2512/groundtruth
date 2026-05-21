"""Tests for shared evidence marker contract."""
import pytest
from groundtruth.config.evidence_markers import (
    L3B_MARKERS,
    L3_MARKERS,
    RESCUE_MARKERS,
    has_gt_evidence,
)


class TestHasGtEvidence:
    """has_gt_evidence must recognize all structural markers and reject garbage."""

    # --- L3b layer: all structural markers recognized ---

    def test_l3b_contract(self):
        assert has_gt_evidence("[CONTRACT] 9 callers depend on trace()", "l3b")

    def test_l3b_contract_unverified(self):
        assert has_gt_evidence("[CONTRACT ~] possible callers of foo()", "l3b")

    def test_l3b_peer(self):
        assert has_gt_evidence("[PEER] tracer.py::trace() (your earlier edit):\ndef trace(", "l3b")

    def test_l3b_pattern(self):
        assert has_gt_evidence("[PATTERN] sibling set_tag() does:\n\"\"\"", "l3b")

    def test_l3b_signature(self):
        assert has_gt_evidence("[SIGNATURE] def trace() -> Iterator[Span]", "l3b")

    def test_l3b_test(self):
        assert has_gt_evidence("[TEST] test_tracer.py: assert len(traces) == 1", "l3b")

    def test_l3b_verify(self):
        assert has_gt_evidence("[GT_VERIFY medium] Run: pytest test/tracing/", "l3b")

    def test_l3b_propagate(self):
        assert has_gt_evidence("[PROPAGATE] 3 call sites may need updating", "l3b")

    def test_l3b_cochange(self):
        assert has_gt_evidence("[CO-CHANGE] graph.py changed with this file in 7/10 commits", "l3b")

    def test_l3b_scope(self):
        assert has_gt_evidence("[SCOPE] commits to this file typically touch 3.2 files", "l3b")

    def test_l3b_behavioral_contract(self):
        assert has_gt_evidence("[BEHAVIORAL CONTRACT]\n  GUARD: if x -> return", "l3b")

    def test_l3b_recall(self):
        assert has_gt_evidence("[RECALL] from earlier: Called by: test.py:42", "l3b")

    def test_l3b_called_by(self):
        assert has_gt_evidence("Called by: installer.py:205 `install_graph = InstallGraph(graph)`", "l3b")

    def test_l3b_calls_into(self):
        assert has_gt_evidence("Calls into: graph.py::analyze_binaries", "l3b")

    def test_l3b_imported_by(self):
        assert has_gt_evidence("Imported by: test_tracer.py", "l3b")

    def test_l3b_gt_status_success_only_rejected(self):
        assert not has_gt_evidence("[GT_STATUS] success:3_items", "l3b")

    # --- P0-5: [GT_STATUS] no_evidence must NOT pass ---

    def test_l3b_gt_status_no_evidence_rejected(self):
        """P0-5 proof: no-evidence status must not pass delivery gate."""
        assert not has_gt_evidence("[GT_STATUS] no_evidence:no_graph_edges", "l3b")

    def test_l3b_gt_status_error_rejected(self):
        """P0-5 proof: error status must not pass delivery gate."""
        assert not has_gt_evidence("[GT_STATUS] error:sqlite_fail", "l3b")

    def test_l3b_gt_status_skipped_rejected(self):
        """P0-5 proof: skipped status must not pass delivery gate."""
        assert not has_gt_evidence("[GT_STATUS] skipped:test_file", "l3b")

    def test_l3b_bare_gt_without_space_rejected(self):
        """P0-5 proof: bare [GT] without trailing space must not match."""
        assert not has_gt_evidence("[GT]\n", "l3b")

    def test_l3b_gt_with_content_accepted(self):
        """P0-5 proof: [GT] with real content (trailing space) passes."""
        assert has_gt_evidence("[GT] graph:\n→ Next: read tests/test_foo.py", "l3b")

    def test_l3b_new_module_markers(self):
        """P0-5 proof: new evidence module markers pass."""
        assert has_gt_evidence("[GT_AUTO] Key symbols in base.py:", "l3b")
        assert has_gt_evidence("[MISMATCH] You removed old_url", "l3b")
        assert has_gt_evidence("[FORMAT] Callers access keys: name", "l3b")
        assert has_gt_evidence("[GT_CONTRACT high] Issue says to omit old_url", "l3b")

    # --- L3b rejects garbage ---

    def test_l3b_rejects_empty(self):
        assert not has_gt_evidence("", "l3b")

    def test_l3b_rejects_garbage(self):
        assert not has_gt_evidence("random garbage text with no markers", "l3b")

    def test_l3b_rejects_partial_marker(self):
        assert not has_gt_evidence("CONTRACT without brackets", "l3b")

    def test_l3b_rejects_file_content(self):
        assert not has_gt_evidence("def foo():\n    return bar\n", "l3b")

    # --- L3 layer: superset of L3b + legacy ---

    def test_l3_includes_l3b_markers(self):
        assert has_gt_evidence("[CONTRACT] callers depend on foo()", "l3")

    def test_l3_twins(self):
        assert has_gt_evidence("[TWINS] L86: `if args.order_by is None:`", "l3")

    def test_l3_legacy_signature(self):
        assert has_gt_evidence("SIGNATURE: def install_build_order(self):", "l3")

    def test_l3_legacy_sibling(self):
        assert has_gt_evidence("SIBLING: pref uses: return PkgReference()", "l3")

    def test_l3_legacy_callers(self):
        assert has_gt_evidence("CALLERS: installer.py:205", "l3")

    def test_l3_rejects_garbage(self):
        assert not has_gt_evidence("no markers here at all", "l3")

    # --- Rescue layer ---

    def test_rescue_gt_marker(self):
        assert has_gt_evidence("[GT] You confirmed graph.py earlier.", "rescue")

    def test_rescue_rejects_no_gt(self):
        assert not has_gt_evidence("You confirmed graph.py earlier.", "rescue")

    # --- Default layer ---

    def test_default_uses_l3b(self):
        assert has_gt_evidence("[CONTRACT] callers", "unknown_layer")
        assert not has_gt_evidence("random garbage", "unknown_layer")


class TestMarkerCompleteness:
    """L3 markers must be a superset of L3b markers."""

    def test_l3_superset_of_l3b(self):
        for marker in L3B_MARKERS:
            assert marker in L3_MARKERS, f"L3b marker {marker!r} missing from L3_MARKERS"

    def test_rescue_minimal(self):
        assert len(RESCUE_MARKERS) <= 3, "Rescue markers should be minimal"
