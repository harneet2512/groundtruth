"""Tests for the observability layer."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from groundtruth.observability.schema import (
    ComponentStatus,
    ComponentTrace,
    EndpointTrace,
    RequestLayer,
    ResponseLayer,
    SynthesisLayer,
)
from groundtruth.observability.tracer import EndpointTracer
from groundtruth.observability.writer import TraceWriter
from groundtruth.observability.analyzer import analyze, load_traces


# --- Schema tests ---


class TestSchema:
    def test_request_layer_defaults(self) -> None:
        req = RequestLayer(endpoint="groundtruth_impact")
        assert req.endpoint == "groundtruth_impact"
        assert len(req.trace_id) == 12
        assert req.timestamp > 0
        assert req.input_summary == ""

    def test_component_status_values(self) -> None:
        assert ComponentStatus.USED.value == "used"
        assert ComponentStatus.SUPPRESSED.value == "suppressed"
        assert ComponentStatus.ABSTAINED.value == "abstained"
        assert ComponentStatus.FAILED.value == "failed"
        assert ComponentStatus.SKIPPED.value == "skipped"

    def test_component_trace_creation(self) -> None:
        ct = ComponentTrace(
            component="obligations",
            status=ComponentStatus.ABSTAINED,
            reason="index stale",
            confidence=0.3,
        )
        assert ct.component == "obligations"
        assert ct.status == ComponentStatus.ABSTAINED
        assert ct.reason == "index stale"

    def test_endpoint_trace_to_dict(self) -> None:
        trace = EndpointTrace(
            request=RequestLayer(endpoint="groundtruth_check", input_summary="3 files modified"),
            components=[
                ComponentTrace(
                    component="autocorrect",
                    status=ComponentStatus.USED,
                    output_summary="2 corrections",
                    item_count=2,
                    confidence=0.9,
                    duration_ms=12.5,
                ),
                ComponentTrace(
                    component="obligations",
                    status=ComponentStatus.SUPPRESSED,
                    reason="below confidence threshold",
                ),
            ],
            synthesis=SynthesisLayer(
                included=["autocorrect"],
                excluded=["obligations"],
                exclusion_reasons={"obligations": "below confidence threshold"},
                verdict="NEEDS_FIXES",
            ),
            response=ResponseLayer(
                response_type="patch_check",
                item_count=2,
                verdict="NEEDS_FIXES",
                total_duration_ms=15.3,
            ),
        )
        d = trace.to_dict()

        assert d["request"]["endpoint"] == "groundtruth_check"
        assert len(d["components"]) == 2
        assert d["components"][0]["status"] == "used"
        assert d["components"][0]["confidence"] == 0.9
        assert d["components"][1]["status"] == "suppressed"
        assert d["components"][1]["reason"] == "below confidence threshold"
        assert d["synthesis"]["included"] == ["autocorrect"]
        assert d["synthesis"]["excluded"] == ["obligations"]
        assert d["response"]["verdict"] == "NEEDS_FIXES"

    def test_to_dict_omits_empty_optionals(self) -> None:
        trace = EndpointTrace(
            request=RequestLayer(endpoint="groundtruth_references"),
            components=[
                ComponentTrace(component="graph", status=ComponentStatus.USED),
            ],
        )
        d = trace.to_dict()
        # No confidence, trust_tier, reason, extra should be absent
        comp = d["components"][0]
        assert "confidence" not in comp
        assert "trust_tier" not in comp
        assert "reason" not in comp
        assert "extra" not in comp

    def test_to_dict_is_json_serializable(self) -> None:
        trace = EndpointTrace(
            request=RequestLayer(endpoint="groundtruth_impact", symbol="getUserById"),
            components=[
                ComponentTrace(
                    component="callers",
                    status=ComponentStatus.USED,
                    item_count=5,
                    duration_ms=3.2,
                ),
            ],
            synthesis=SynthesisLayer(included=["callers"], verdict="HIGH"),
            response=ResponseLayer(verdict="HIGH", item_count=5, total_duration_ms=4.1),
        )
        s = json.dumps(trace.to_dict())
        parsed = json.loads(s)
        assert parsed["request"]["symbol"] == "getUserById"


# --- Writer tests ---


class TestWriter:
    def test_write_creates_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            writer = TraceWriter(tmpdir)
            trace = EndpointTrace(
                request=RequestLayer(endpoint="groundtruth_impact"),
            )
            writer.write(trace)
            writer.close()

            path = Path(tmpdir) / "gt_traces.jsonl"
            assert path.exists()
            lines = path.read_text(encoding="utf-8").strip().split("\n")
            assert len(lines) == 1
            parsed = json.loads(lines[0])
            assert parsed["request"]["endpoint"] == "groundtruth_impact"

    def test_write_appends(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            writer = TraceWriter(tmpdir)
            for i in range(3):
                trace = EndpointTrace(
                    request=RequestLayer(endpoint=f"ep_{i}"),
                )
                writer.write(trace)
            writer.close()

            path = Path(tmpdir) / "gt_traces.jsonl"
            lines = path.read_text(encoding="utf-8").strip().split("\n")
            assert len(lines) == 3
            assert writer.trace_count == 3

    def test_disabled_writer_does_not_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            writer = TraceWriter(tmpdir, enabled=False)
            trace = EndpointTrace(
                request=RequestLayer(endpoint="groundtruth_check"),
            )
            writer.write(trace)
            writer.close()

            path = Path(tmpdir) / "gt_traces.jsonl"
            assert not path.exists()
            assert writer.trace_count == 0

    def test_context_manager(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with TraceWriter(tmpdir) as writer:
                writer.write(EndpointTrace(request=RequestLayer(endpoint="test")))
            path = Path(tmpdir) / "gt_traces.jsonl"
            assert path.exists()


# --- Tracer tests ---


class TestTracer:
    def test_trace_context_builds_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            writer = TraceWriter(tmpdir)
            tracer = EndpointTracer(writer)

            with tracer.trace(
                "groundtruth_impact",
                symbol="getUserById",
                input_summary="check impact of getUserById",
            ) as t:
                t.log_component(
                    "graph_callers",
                    ComponentStatus.USED,
                    output_summary="5 callers",
                    item_count=5,
                )
                t.log_component(
                    "obligations",
                    ComponentStatus.ABSTAINED,
                    reason="index stale",
                )
                t.synthesize(
                    included=["graph_callers"],
                    excluded=["obligations"],
                    exclusion_reasons={"obligations": "index stale"},
                    verdict="5 callers at risk",
                )
                t.respond(
                    response_type="impact_analysis",
                    item_count=5,
                    verdict="HIGH",
                )

            writer.close()

            path = Path(tmpdir) / "gt_traces.jsonl"
            parsed = json.loads(path.read_text(encoding="utf-8").strip())
            assert parsed["request"]["endpoint"] == "groundtruth_impact"
            assert parsed["request"]["symbol"] == "getUserById"
            assert len(parsed["components"]) == 2
            assert parsed["components"][0]["status"] == "used"
            assert parsed["components"][1]["status"] == "abstained"
            assert parsed["synthesis"]["verdict"] == "5 callers at risk"
            assert parsed["response"]["verdict"] == "HIGH"
            assert parsed["response"]["total_duration_ms"] >= 0

    def test_trace_without_writer(self) -> None:
        """Tracer with no writer should not crash."""
        tracer = EndpointTracer(writer=None)
        with tracer.trace("groundtruth_check") as t:
            t.log_component("autocorrect", ComponentStatus.USED)
            t.respond(verdict="CLEAN")
        # No exception = pass

    def test_trace_records_duration(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            writer = TraceWriter(tmpdir)
            tracer = EndpointTracer(writer)
            with tracer.trace("groundtruth_references", symbol="Foo") as t:
                t.respond(verdict="FOUND")
            writer.close()

            parsed = json.loads(Path(tmpdir, "gt_traces.jsonl").read_text(encoding="utf-8").strip())
            assert parsed["response"]["total_duration_ms"] >= 0


# --- Analyzer tests ---


class TestAnalyzer:
    def _write_sample_traces(self, path: Path) -> None:
        """Write sample traces for analysis."""
        traces = [
            EndpointTrace(
                request=RequestLayer(endpoint="groundtruth_impact"),
                components=[
                    ComponentTrace(component="callers", status=ComponentStatus.USED, item_count=3),
                    ComponentTrace(
                        component="obligations",
                        status=ComponentStatus.ABSTAINED,
                        reason="stale",
                    ),
                ],
                synthesis=SynthesisLayer(
                    included=["callers"], excluded=["obligations"], verdict="MODERATE"
                ),
                response=ResponseLayer(verdict="MODERATE", total_duration_ms=10.0),
            ),
            EndpointTrace(
                request=RequestLayer(endpoint="groundtruth_check"),
                components=[
                    ComponentTrace(component="autocorrect", status=ComponentStatus.USED),
                    ComponentTrace(
                        component="obligations", status=ComponentStatus.USED, item_count=2
                    ),
                    ComponentTrace(
                        component="contradictions",
                        status=ComponentStatus.SUPPRESSED,
                        reason="low confidence",
                    ),
                ],
                synthesis=SynthesisLayer(
                    included=["autocorrect", "obligations"],
                    excluded=["contradictions"],
                    verdict="INCOMPLETE",
                ),
                response=ResponseLayer(verdict="INCOMPLETE", total_duration_ms=20.0),
            ),
            EndpointTrace(
                request=RequestLayer(endpoint="groundtruth_impact"),
                components=[
                    ComponentTrace(component="callers", status=ComponentStatus.USED),
                    ComponentTrace(component="obligations", status=ComponentStatus.USED),
                ],
                response=ResponseLayer(verdict="HIGH", total_duration_ms=8.0),
            ),
        ]
        with open(path, "w", encoding="utf-8") as f:
            for t in traces:
                f.write(json.dumps(t.to_dict(), separators=(",", ":")) + "\n")

    def test_load_traces(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "test.jsonl"
            self._write_sample_traces(path)
            traces = load_traces(path)
            assert len(traces) == 3

    def test_analyze_endpoint_calls(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "test.jsonl"
            self._write_sample_traces(path)
            result = analyze(load_traces(path))
            assert result["total_traces"] == 3
            assert result["endpoint_calls"]["groundtruth_impact"] == 2
            assert result["endpoint_calls"]["groundtruth_check"] == 1

    def test_analyze_component_stats(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "test.jsonl"
            self._write_sample_traces(path)
            result = analyze(load_traces(path))
            # callers: used in trace 0 and trace 2 = 2
            assert result["component_used"]["callers"] == 2
            # obligations: used in trace 1 and trace 2 = 2
            assert result["component_used"]["obligations"] == 2
            assert result["component_abstained"]["obligations"] == 1
            assert result["component_suppressed"]["contradictions"] == 1

    def test_analyze_abstention_rate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "test.jsonl"
            self._write_sample_traces(path)
            result = analyze(load_traces(path))
            # 1 abstained / (5 used + 1 abstained) = 16.7%
            assert result["abstention_rate"] == pytest.approx(16.7, abs=0.1)

    def test_analyze_verdicts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "test.jsonl"
            self._write_sample_traces(path)
            result = analyze(load_traces(path))
            assert result["endpoint_verdicts"]["groundtruth_impact"]["MODERATE"] == 1
            assert result["endpoint_verdicts"]["groundtruth_impact"]["HIGH"] == 1
            assert result["endpoint_verdicts"]["groundtruth_check"]["INCOMPLETE"] == 1
