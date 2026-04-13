"""RegistryCouplingExtractor -- mines registry/file coupling contracts.

This family is intentionally language-agnostic. It relies on graph edges and
common registry file conventions rather than language-specific AST logic.
"""

from __future__ import annotations

import os
import re

from groundtruth.substrate.types import ContractRecord, tier_from_confidence

_REGISTRY_PATTERNS = ("__init__.py", "urls.py", "routes.*", "registry.*", "plugin.*")


class RegistryCouplingExtractor:
    """Extract contracts for symbols/files registered through registry files."""

    contract_type = "registry_coupling"

    def extract(self, reader, node_id: int) -> list[ContractRecord]:  # noqa: ANN001
        node = reader.get_node_by_id(node_id)
        if not node:
            return []

        target_file = node.get("file_path", "")
        if not target_file:
            return []

        name = node["name"]
        qualified = node.get("qualified_name") or name
        scope_kind = _label_to_scope(node.get("label", "Function"))

        registry_files = self._find_registry_files(reader, target_file)
        if not registry_files:
            return []

        confidence = 0.90 if len(registry_files) >= 2 else 0.80
        support_count = len(registry_files)
        primary_registry = registry_files[0]

        return [
            ContractRecord(
                contract_type=self.contract_type,
                scope_kind=scope_kind,
                scope_ref=qualified,
                predicate=(
                    f"Changes to {name} must preserve registration/coupling in "
                    f"{os.path.basename(primary_registry)}"
                ),
                normalized_form=(
                    f"registry_coupling:preserve:{name}:{target_file}:{primary_registry}"
                ),
                support_sources=tuple(f"{path}:0" for path in registry_files[:5]),
                support_count=support_count,
                confidence=confidence,
                tier=tier_from_confidence(confidence, support_count),
            )
        ]

    def _find_registry_files(self, reader, target_file: str) -> list[str]:  # noqa: ANN001
        matches: list[str] = []
        for file_path in reader.get_file_paths():
            basename = os.path.basename(file_path)
            if not any(
                basename == pattern or re.match(pattern.replace("*", ".*"), basename)
                for pattern in _REGISTRY_PATTERNS
            ):
                continue

            for node in reader.get_nodes_in_file(file_path):
                for callee in reader.get_callees(node.get("id", 0)):
                    if callee.get("target_file_path") == target_file:
                        matches.append(file_path)
                        break
                else:
                    continue
                break

        return list(dict.fromkeys(matches))


def _label_to_scope(label: str) -> str:
    mapping = {
        "Function": "function",
        "Method": "method",
        "Class": "class",
        "Interface": "class",
        "Struct": "class",
    }
    return mapping.get(label, "function")
