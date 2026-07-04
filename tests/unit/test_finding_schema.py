"""Tests for the unified Finding schema."""

from __future__ import annotations

import pytest

from groundtruth.schema.finding import (
    AgentAction,
    Finding,
    FindingKind,
    Location,
    Severity,
    WhyNow,
    format_findings,
)


class TestLocation:
    def test_minimal(self) -> None:
        loc = Location(file="src/model.py")
        assert loc.file == "src/model.py"
        assert loc.line is None
        assert loc.symbol is None

    def test_full(self) -> None:
        loc = Location(file="src/model.py", line=42, symbol="encrypt")
        assert loc.line == 42
        assert loc.symbol == "encrypt"


class TestFinding:
    def test_minimal_construction(self) -> None:
        f = Finding(
            kind=FindingKind.GUARD_REMOVED,
            severity=Severity.WARNING,
            confidence=0.8,
            location=Location(file="src/model.py", line=42),
            message="guard removed",
        )
        assert f.kind == FindingKind.GUARD_REMOVED
        assert f.severity == Severity.WARNING
        assert f.confidence == 0.8
        assert f.novelty is True
        assert f.agent_action == AgentAction.VERIFY
        assert f.why_now == WhyNow.ALWAYS

    def test_confidence_bounds(self) -> None:
        with pytest.raises(Exception):
            Finding(
                kind=FindingKind.GUARD_REMOVED,
                severity=Severity.WARNING,
                confidence=1.5,
                location=Location(file="x.py"),
                message="bad",
            )
        with pytest.raises(Exception):
            Finding(
                kind=FindingKind.GUARD_REMOVED,
                severity=Severity.WARNING,
                confidence=-0.1,
                location=Location(file="x.py"),
                message="bad",
            )

    def test_confidence_edge_values(self) -> None:
        f0 = Finding(
            kind=FindingKind.GUARD_REMOVED,
            severity=Severity.NOTE,
            confidence=0.0,
            location=Location(file="x.py"),
            message="zero",
        )
        assert f0.confidence == 0.0
        f1 = Finding(
            kind=FindingKind.GUARD_REMOVED,
            severity=Severity.ERROR,
            confidence=1.0,
            location=Location(file="x.py"),
            message="one",
        )
        assert f1.confidence == 1.0


class TestToTextLine:
    def _make(
        self,
        confidence: float,
        agent_action: AgentAction = AgentAction.VERIFY,
        location: Location | None = None,
    ) -> Finding:
        return Finding(
            kind=FindingKind.CALLER_CONTRACT,
            severity=Severity.WARNING,
            confidence=confidence,
            location=location or Location(file="src/model.py", line=42, symbol="encrypt"),
            message="3 callers depend on return type",
            agent_action=agent_action,
        )

    def test_verified_tier(self) -> None:
        line = self._make(0.95).to_text_line()
        assert line.startswith("[VERIFIED]")
        assert "[caller_contract]" in line
        assert "@ src/model.py:42" in line
        assert "(0.95)" in line
        assert "VERIFY" in line

    def test_warning_tier(self) -> None:
        line = self._make(0.7).to_text_line()
        assert line.startswith("[WARNING]")

    def test_info_tier(self) -> None:
        line = self._make(0.4).to_text_line()
        assert line.startswith("[INFO]")

    def test_tier_boundary_085(self) -> None:
        assert self._make(0.85).to_text_line().startswith("[VERIFIED]")
        assert self._make(0.84).to_text_line().startswith("[WARNING]")

    def test_tier_boundary_070(self) -> None:
        assert self._make(0.7).to_text_line().startswith("[WARNING]")
        assert self._make(0.69).to_text_line().startswith("[INFO]")

    def test_fix_required_action(self) -> None:
        line = self._make(
            0.9, agent_action=AgentAction.FIX_REQUIRED
        ).to_text_line()
        assert "FIX REQUIRED" in line

    def test_no_line_number(self) -> None:
        f = self._make(
            0.9, location=Location(file="src/model.py")
        )
        line = f.to_text_line()
        assert "@ src/model.py " in line
        assert ":None" not in line


class TestFormatFindings:
    def _make_finding(self, kind: FindingKind, confidence: float = 0.9) -> Finding:
        return Finding(
            kind=kind,
            severity=Severity.WARNING,
            confidence=confidence,
            location=Location(file="x.py", line=1),
            message="test message",
        )

    def test_empty_returns_empty(self) -> None:
        assert format_findings([], "task_map") == ""

    def test_wraps_in_gt_evidence(self) -> None:
        result = format_findings(
            [self._make_finding(FindingKind.GUARD_REMOVED)],
            "event_brief",
        )
        assert result.startswith('<gt-evidence surface="event_brief">')
        assert result.endswith("</gt-evidence>")

    def test_binding_footer(self) -> None:
        f = self._make_finding(FindingKind.OVERRIDE_VIOLATION, 0.95)
        f = f.model_copy(update={"agent_action": AgentAction.FIX_REQUIRED})
        result = format_findings([f], "review_patch", include_binding=True)
        assert "BINDING:" in result
        assert "1 finding(s)" in result

    def test_no_binding_when_no_fix_required(self) -> None:
        f = self._make_finding(FindingKind.GUARD_REMOVED)
        result = format_findings([f], "review_patch", include_binding=True)
        assert "BINDING:" not in result

    def test_multiple_findings(self) -> None:
        findings = [
            self._make_finding(FindingKind.GUARD_REMOVED),
            self._make_finding(FindingKind.CALLER_CONTRACT),
        ]
        result = format_findings(findings, "task_map")
        lines = result.strip().split("\n")
        assert len(lines) == 4  # header + 2 findings + footer


class TestEnumValues:
    def test_all_finding_kinds(self) -> None:
        assert len(FindingKind) == 20

    def test_all_severities(self) -> None:
        assert set(Severity) == {Severity.ERROR, Severity.WARNING, Severity.NOTE}

    def test_all_why_now(self) -> None:
        assert len(WhyNow) == 4

    def test_all_agent_actions(self) -> None:
        assert len(AgentAction) == 4

    def test_string_serialization(self) -> None:
        assert FindingKind.GUARD_REMOVED.value == "guard_removed"
        assert Severity.ERROR.value == "error"
        assert WhyNow.FILE_CHANGED.value == "file_changed"
        assert AgentAction.FIX_REQUIRED.value == "fix_required"
