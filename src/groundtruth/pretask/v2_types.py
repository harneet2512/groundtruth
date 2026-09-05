"""Locked dataclass schemas shared by Track A (preprocessor) and Track B (ranker).

Both modules import from this single source so neither can drift the contract.
Track D (eval harness) consumes the same types via Track B's return value.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

TokenSource = Literal[
    "stack_trace",
    "backtick",
    "snake_case",
    "camel_case",
    "error_class",
    "title",
]


@dataclass(frozen=True)
class HighSignalToken:
    token: str
    weight: float
    source: TokenSource


@dataclass
class QueryObject:
    file_hints: list[str] = field(default_factory=list)
    function_hints: list[str] = field(default_factory=list)
    class_hints: list[str] = field(default_factory=list)
    high_signal_tokens: list[HighSignalToken] = field(default_factory=list)
    code_blocks: list[str] = field(default_factory=list)
    raw_text: str = ""


@dataclass
class RankedFile:
    file: str
    score: float


@dataclass
class RankedFunction:
    file: str
    function: str
    score: float
    components: dict[str, float] = field(default_factory=dict)


@dataclass
class RankedResults:
    files: list[RankedFile] = field(default_factory=list)
    functions: list[RankedFunction] = field(default_factory=list)
