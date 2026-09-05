"""Validated loader for the live-registry language-operation certification."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Final


SCHEMA: Final = "gt.language_operation_compatibility.v1"
OPERATIONS: Final = (
    "callers",
    "definition",
    "exact_literal_search",
    "patch_impact",
    "references",
    "syntax",
    "verification_status",
)
TERMINAL_SEMANTICS: Final = frozenset(
    {"exact", "sound_overapprox", "execution_specific", "not_applicable", "removed"}
)
_ARTIFACT = Path(__file__).with_name("generated_language_operation_compatibility.json")


@dataclass(frozen=True, order=True)
class LanguageOperationRow:
    registry_identity: str
    operation: str
    terminal_semantics: str


@dataclass(frozen=True)
class LanguageOperationCompatibility:
    schema: str
    source_manifest_sha256: str
    rows: tuple[LanguageOperationRow, ...]

    def semantics_for(self, registry_identity: str, operation: str) -> str:
        for row in self.rows:
            if row.registry_identity == registry_identity and row.operation == operation:
                return row.terminal_semantics
        return "removed"

    def certified_operations(self, registry_identity: str) -> tuple[str, ...]:
        return tuple(
            row.operation
            for row in self.rows
            if row.registry_identity == registry_identity and row.terminal_semantics != "removed"
        )


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def load_language_operation_compatibility(
    path: str | Path = _ARTIFACT,
) -> LanguageOperationCompatibility:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("schema") != SCHEMA:
        raise ValueError("unsupported language-operation compatibility schema")
    if not _is_sha256(raw.get("source_manifest_sha256")):
        raise ValueError("invalid source language manifest hash")
    raw_rows = raw.get("rows")
    if not isinstance(raw_rows, list) or len(raw_rows) != 210:
        raise ValueError("compatibility manifest must contain exactly 210 rows")
    rows: list[LanguageOperationRow] = []
    for item in raw_rows:
        if not isinstance(item, dict):
            raise ValueError("compatibility row must be an object")
        row = LanguageOperationRow(
            registry_identity=str(item.get("registry_identity") or ""),
            operation=str(item.get("operation") or ""),
            terminal_semantics=str(item.get("terminal_semantics") or ""),
        )
        if (
            not row.registry_identity
            or row.operation not in OPERATIONS
            or row.terminal_semantics not in TERMINAL_SEMANTICS
        ):
            raise ValueError(f"invalid compatibility row: {row!r}")
        rows.append(row)
    identities = {row.registry_identity for row in rows}
    pairs = {(row.registry_identity, row.operation) for row in rows}
    if len(identities) != 30 or len(pairs) != 210:
        raise ValueError("compatibility manifest is missing or duplicates registry pairs")
    expected_pairs = {(identity, operation) for identity in identities for operation in OPERATIONS}
    if pairs != expected_pairs:
        raise ValueError("compatibility manifest is not a complete language-operation product")
    if rows != sorted(rows):
        raise ValueError("compatibility rows are not canonical")
    return LanguageOperationCompatibility(
        schema=SCHEMA,
        source_manifest_sha256=raw["source_manifest_sha256"],
        rows=tuple(rows),
    )


COMPATIBILITY: Final = load_language_operation_compatibility()


__all__ = [
    "COMPATIBILITY",
    "LanguageOperationCompatibility",
    "LanguageOperationRow",
    "OPERATIONS",
    "SCHEMA",
    "TERMINAL_SEMANTICS",
    "load_language_operation_compatibility",
]
