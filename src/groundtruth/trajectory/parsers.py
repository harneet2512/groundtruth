"""Failure parser registry — extracts structured failure records from test output."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class FailureRecord:
    command_kind: str = ""
    failure_kind: str = ""
    failing_unit: str = ""
    file: str = ""
    line: int = 0
    assertion_or_error: str = ""
    expected: str = ""
    actual: str = ""
    exception_type: str = ""
    exception_message: str = ""
    top_project_frame: str = ""
    raw_excerpt: str = ""
    parser_name: str = ""
    parser_confidence: float = 0.0
    signature_hash: str = ""

    def compute_hash(self) -> str:
        key = f"{self.failing_unit}:{self.assertion_or_error}:{self.expected}"
        self.signature_hash = hashlib.md5(key.encode()).hexdigest()[:12]
        return self.signature_hash

    def render_compact(self, max_chars: int = 300) -> str:
        parts = []
        if self.failing_unit:
            parts.append(f"FAILED: {self.failing_unit}")
        if self.assertion_or_error:
            parts.append(f"  {self.assertion_or_error}")
        if self.expected and self.actual:
            parts.append(f"  expected: {self.expected[:80]}")
            parts.append(f"  actual:   {self.actual[:80]}")
        elif self.exception_type:
            msg = self.exception_message[:100] if self.exception_message else ""
            parts.append(f"  {self.exception_type}: {msg}")
        if self.top_project_frame:
            parts.append(f"  at: {self.top_project_frame}")
        text = "\n".join(parts)
        return text[:max_chars]


class PytestParser:
    name = "pytest"

    _FAILED_RE = re.compile(r"FAILED\s+(\S+?)(?:\s+-|$)")
    _ASSERT_RE = re.compile(r"(?:AssertionError|assert)\s*(.*)", re.IGNORECASE)
    _EXPECTED_RE = re.compile(r"expected\s*[:=]\s*(.+)", re.IGNORECASE)
    _ACTUAL_RE = re.compile(r"(?:actual|got)\s*[:=]\s*(.+)", re.IGNORECASE)
    _EQ_RE = re.compile(r"assert\s+(.+?)\s*==\s*(.+)")
    _SHORT_SUMMARY_RE = re.compile(r"=+ short test summary info =+")
    _FRAME_RE = re.compile(r"(\S+\.py):(\d+):\s*(.*)")
    _ERROR_RE = re.compile(r"E\s+(\w+Error|Exception)\s*:\s*(.*)")

    def parse(self, output: str) -> list[FailureRecord]:
        records: list[FailureRecord] = []
        lines = output.split("\n")

        failed_tests: list[str] = []
        for line in lines:
            m = self._FAILED_RE.search(line)
            if m:
                failed_tests.append(m.group(1))

        if not failed_tests:
            return records

        for test_id in failed_tests[:5]:
            rec = FailureRecord(
                command_kind="test",
                failure_kind="assertion",
                failing_unit=test_id,
                parser_name=self.name,
                parser_confidence=0.8,
            )

            test_section = self._find_test_section(test_id, lines)
            if test_section:
                for sline in test_section:
                    sline_stripped = sline.strip()

                    em = self._ERROR_RE.match(sline_stripped)
                    if em:
                        rec.exception_type = em.group(1)
                        rec.exception_message = em.group(2).strip()
                        if "AssertionError" not in em.group(1):
                            rec.failure_kind = "exception"

                    am = self._ASSERT_RE.search(sline_stripped)
                    if am:
                        rec.assertion_or_error = am.group(1).strip()[:200]

                    eqm = self._EQ_RE.search(sline_stripped)
                    if eqm:
                        rec.expected = eqm.group(2).strip()[:100]
                        rec.actual = eqm.group(1).strip()[:100]

                    exm = self._EXPECTED_RE.search(sline_stripped)
                    if exm and not rec.expected:
                        rec.expected = exm.group(1).strip()[:100]

                    acm = self._ACTUAL_RE.search(sline_stripped)
                    if acm and not rec.actual:
                        rec.actual = acm.group(1).strip()[:100]

                    fm = self._FRAME_RE.match(sline_stripped)
                    if fm and not fm.group(1).startswith("_pytest"):
                        rec.file = fm.group(1)
                        rec.line = int(fm.group(2))
                        rec.top_project_frame = f"{fm.group(1)}:{fm.group(2)}: {fm.group(3)}"

                rec.raw_excerpt = "\n".join(test_section[:10])[:500]

            rec.compute_hash()
            records.append(rec)

        return records

    def _find_test_section(self, test_id: str, lines: list[str]) -> list[str]:
        short_name = test_id.split("::")[-1] if "::" in test_id else test_id
        section: list[str] = []
        capturing = False
        for line in lines:
            if short_name in line and ("FAILED" in line or "___" in line or "---" in line):
                capturing = True
                section = []
                continue
            if capturing:
                if line.startswith("=") and len(line) > 3:
                    break
                if line.startswith("___") and len(line) > 3 and section:
                    break
                section.append(line)
        return section[:20]


class GenericTracebackParser:
    name = "generic_traceback"

    _TB_RE = re.compile(r'File "([^"]+)", line (\d+)')
    _ERROR_LINE_RE = re.compile(r"^(\w+(?:Error|Exception|Warning))\s*:\s*(.*)", re.MULTILINE)

    def parse(self, output: str) -> list[FailureRecord]:
        records: list[FailureRecord] = []
        m = self._ERROR_LINE_RE.search(output[-2000:])
        if not m:
            return records

        rec = FailureRecord(
            command_kind="run",
            failure_kind="exception",
            exception_type=m.group(1),
            exception_message=m.group(2).strip()[:200],
            parser_name=self.name,
            parser_confidence=0.5,
        )

        frames = list(self._TB_RE.finditer(output[-3000:]))
        project_frames = [f for f in frames if not any(
            skip in f.group(1) for skip in ("/site-packages/", "/lib/python", "/_pytest/", "/unittest/")
        )]
        if project_frames:
            last = project_frames[-1]
            rec.file = last.group(1)
            rec.line = int(last.group(2))
            rec.top_project_frame = f"{last.group(1)}:{last.group(2)}"

        rec.assertion_or_error = f"{rec.exception_type}: {rec.exception_message}"[:200]
        rec.raw_excerpt = output[-500:]
        rec.compute_hash()
        records.append(rec)
        return records


class GenericExpectedActualParser:
    name = "generic_expected_actual"

    _PATTERNS = [
        re.compile(r"expected\s*[:=]\s*(.+?)(?:\n|\s+but\s+)", re.IGNORECASE),
        re.compile(r"(?:got|actual|received)\s*[:=]\s*(.+?)(?:\n|$)", re.IGNORECASE),
    ]

    def parse(self, output: str) -> list[FailureRecord]:
        text = output[-2000:]
        expected = ""
        actual = ""
        for p in self._PATTERNS:
            m = p.search(text)
            if m:
                val = m.group(1).strip()[:100]
                if not expected:
                    expected = val
                elif not actual:
                    actual = val

        if not expected and not actual:
            return []

        rec = FailureRecord(
            command_kind="test",
            failure_kind="assertion",
            expected=expected,
            actual=actual,
            assertion_or_error=f"expected {expected}, got {actual}",
            parser_name=self.name,
            parser_confidence=0.3,
            raw_excerpt=text[-300:],
        )
        rec.compute_hash()
        return [rec]


class TscParser:
    name = "tsc"

    _TS_ERR_RE = re.compile(r"(\S+\.tsx?)\((\d+),(\d+)\):\s+error\s+(TS\d+):\s+(.*)")

    def parse(self, output: str) -> list[FailureRecord]:
        records: list[FailureRecord] = []
        for m in self._TS_ERR_RE.finditer(output[:5000]):
            rec = FailureRecord(
                command_kind="typecheck",
                failure_kind="type_error",
                file=m.group(1),
                line=int(m.group(2)),
                assertion_or_error=f"{m.group(4)}: {m.group(5)}",
                parser_name=self.name,
                parser_confidence=0.9,
            )
            rec.compute_hash()
            records.append(rec)
            if len(records) >= 5:
                break
        return records


class MypyParser:
    name = "mypy"

    _MYPY_ERR_RE = re.compile(r"(\S+\.py):(\d+):\s+error:\s+(.*?)(?:\s+\[(\w+)\])?$", re.MULTILINE)

    def parse(self, output: str) -> list[FailureRecord]:
        records: list[FailureRecord] = []
        for m in self._MYPY_ERR_RE.finditer(output[:5000]):
            rec = FailureRecord(
                command_kind="typecheck",
                failure_kind="type_error",
                file=m.group(1),
                line=int(m.group(2)),
                assertion_or_error=m.group(3).strip(),
                parser_name=self.name,
                parser_confidence=0.9,
            )
            rec.compute_hash()
            records.append(rec)
            if len(records) >= 5:
                break
        return records


_PARSER_REGISTRY: list[Any] = [
    PytestParser(),
    TscParser(),
    MypyParser(),
    GenericTracebackParser(),
    GenericExpectedActualParser(),
]


def parse_failures(command: str, output: str) -> list[FailureRecord]:
    from .classifier import classify_command, CommandKind

    cmd_kind = classify_command(command)

    if cmd_kind == CommandKind.TEST:
        preferred = [PytestParser.name, GenericTracebackParser.name, GenericExpectedActualParser.name]
    elif cmd_kind == CommandKind.TYPECHECK:
        preferred = [TscParser.name, MypyParser.name, GenericTracebackParser.name]
    else:
        preferred = [p.name for p in _PARSER_REGISTRY]

    for parser_name in preferred:
        for parser in _PARSER_REGISTRY:
            if parser.name == parser_name:
                results = parser.parse(output)
                if results:
                    for r in results:
                        r.command_kind = cmd_kind
                    return results

    return []
