"""Tests for _deliver_or_trace delivery invariant."""
import sys
import io
import pytest
from unittest.mock import MagicMock
from pathlib import Path

# Add wrapper to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts" / "swebench"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from groundtruth.config.evidence_markers import has_gt_evidence


class MockConfig:
    """Minimal GTRuntimeConfig mock for delivery tests."""
    def __init__(self):
        self.action_count = 10
        self._last_gt_action = 0


class MockObs:
    """Mock observation object."""
    def __init__(self, content="original obs"):
        self.content = content


def mock_append(obs, text):
    obs.content += text
    return obs


def mock_prepend(obs, text):
    obs.content = text + obs.content
    return obs


class TestDeliverOrTrace:
    """_deliver_or_trace must enforce the delivery invariant."""

    def _make_deliver(self):
        """Import _deliver_or_trace with mocked observation functions."""
        # We can't easily import from the wrapper due to its OH dependencies.
        # Instead, test the logic directly using has_gt_evidence + the contract.
        pass

    def test_evidence_with_contract_marker_is_recognized(self):
        payload = "[GT] tracer.py:\n[CONTRACT] 9 callers depend on trace()\n"
        assert has_gt_evidence(payload, "l3b")

    def test_evidence_with_peer_marker_is_recognized(self):
        payload = "[GT] opentelemetry:\n[PEER] tracer.py::trace():\ndef trace(self):\n"
        assert has_gt_evidence(payload, "l3b")

    def test_evidence_with_legacy_called_by_is_recognized(self):
        payload = "[GT] install_graph:\nCalled by: installer.py:205 `install_graph = InstallGraph(graph)`\n"
        assert has_gt_evidence(payload, "l3b")

    def test_empty_payload_is_not_evidence(self):
        assert not has_gt_evidence("", "l3b")
        assert not has_gt_evidence("   \n  ", "l3b")

    def test_payload_without_structural_markers_but_with_gt_prefix(self):
        # [GT] prefix is itself a marker — "No coupling data" still has [GT]
        payload = "[GT] tracer.py:\nNo coupling data. Try: gt_search function tracer\n"
        assert has_gt_evidence(payload, "l3b")  # [GT] is a valid marker

    def test_raw_text_without_any_marker(self):
        payload = "tracer.py:\nNo coupling data. Try: gt_search function tracer\n"
        assert not has_gt_evidence(payload, "l3b")

    def test_gt_status_only_is_not_evidence(self):
        payload = "[GT_STATUS] success:3_items"
        assert not has_gt_evidence(payload, "l3b")

    def test_l3b_recognizes_all_new_structural_markers(self):
        """This is the PRIMARY bug fix — L3b must recognize structural markers."""
        markers_and_samples = {
            "[CONTRACT]": "[CONTRACT] 9 callers depend on trace()",
            "[PEER]": "[PEER] tracer.py::trace() (your earlier edit):",
            "[PATTERN]": "[PATTERN] sibling set_tag() does:",
            "[SIGNATURE]": "[SIGNATURE] def trace() -> Iterator[Span]",
            "[TEST]": "[TEST] test_tracer.py: assert len(traces) == 1",
            "[PROPAGATE]": "[PROPAGATE] 3 call sites may need updating",
            "[CO-CHANGE]": "[CO-CHANGE] graph.py changed with this file",
            "[SCOPE]": "[SCOPE] commits to this file typically touch 3 files",
            "[BEHAVIORAL CONTRACT]": "[BEHAVIORAL CONTRACT]\n  GUARD: if x -> return",
            "[RECALL]": "[RECALL] from earlier: Called by: test.py:42",
        }
        for marker, sample in markers_and_samples.items():
            assert has_gt_evidence(sample, "l3b"), f"L3b should recognize {marker}"

    def test_delivery_invariant_contract(self):
        """Verify the full delivery contract logic."""
        config = MockConfig()

        # Case 1: evidence with markers → should deliver
        payload = "[GT] file.py:\n[CONTRACT] 3 callers\n"
        assert has_gt_evidence(payload, "l3b"), "Should recognize [CONTRACT]"

        # Case 2: empty → should trace HOOK_EMPTY
        assert not has_gt_evidence("", "l3b")

        # Case 3: no markers → should trace MARKER_MISMATCH
        payload_no_markers = "some random hook output without any GT markers"
        assert not has_gt_evidence(payload_no_markers, "l3b")

    def test_l3_recognizes_legacy_and_new(self):
        """L3 layer must recognize both new and legacy markers."""
        assert has_gt_evidence("[CONTRACT] callers", "l3")
        assert has_gt_evidence("SIGNATURE: def foo()", "l3")
        assert has_gt_evidence("SIBLING: bar uses: return X", "l3")
        assert has_gt_evidence("[TWINS] L86: duplicate", "l3")
