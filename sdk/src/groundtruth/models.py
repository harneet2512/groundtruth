"""Typed data models returned by ``GroundTruth``."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ResolutionMethod = Literal["import", "fqn", "class_hierarchy", "name_match", "same_file"]
Confidence = Literal["high", "medium", "low"]
Family = Literal["TARGET", "ALSO", "SCOPE"]
Direction = Literal["callers", "callees", "both"]


@dataclass(frozen=True)
class Caller:
    symbol: str
    file: str
    line: int
    resolution_method: ResolutionMethod
    is_deterministic: bool


@dataclass(frozen=True)
class Behavior:
    kind: str
    target: str


@dataclass(frozen=True)
class Rule:
    pattern: str
    prevalence: float
    source_count: int


@dataclass(frozen=True)
class AffectedSymbol:
    symbol: str
    file: str
    relationship: str


@dataclass(frozen=True)
class Briefing:
    symbol: str
    file: str
    callers: list[Caller]
    behaviors: list[Behavior]
    rules: list[Rule]
    evidence_text: str
    confidence: Confidence


@dataclass(frozen=True)
class Impact:
    affected_symbols: list[AffectedSymbol]
    breaking_callers: list[Caller]
    rule_violations: list[Rule]
    summary: str


@dataclass(frozen=True)
class ContextResult:
    matches: list[Caller]
    call_graph: dict[str, list[str]]
    evidence: str
