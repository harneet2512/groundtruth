"""IncubatorRuntime — single facade for all Phase 5 enrichments.

Byte-parity contract:
    When no enrichment flags are on, enrich() returns the SAME dict object.
    No copy, no mutation, no new keys. Tests can assert `out is inp`.

    When enrichment flags ARE on, enrich() returns a shallow copy with
    new keys prefixed `_incubator_*`. Existing keys are never modified.
"""

from __future__ import annotations

from typing import Any

from groundtruth.core import flags
from groundtruth.index.store import SymbolStore


class IncubatorRuntime:
    """Facade for all incubator enrichments.

    Constructed only when at least one Phase 5 flag is on.
    When constructed, subsystems are initialized lazily on first use.
    """

    def __init__(self, store: SymbolStore, root_path: str) -> None:
        self._store = store
        self._root_path = root_path
        # Subsystems initialized based on flags
        self._intel_logger: Any = None
        self._intel_reader: Any = None  # Step 9: RepoIntelReader
        self._foundation: Any = None    # Step 10: foundation pipeline

        # Construct intel logger when logging flag is on
        if flags.repo_intel_logging_enabled():
            from groundtruth.incubator.intel_logger import RepoIntelLogger
            self._intel_logger = RepoIntelLogger(store.connection)

        # Construct intel reader when decisions flag is on (requires logging)
        if flags.repo_intel_decisions_enabled():
            from groundtruth.incubator.intel_reader import RepoIntelReader
            self._intel_reader = RepoIntelReader(store.connection)

    def enrich(self, tool_name: str, result: dict[str, Any]) -> dict[str, Any]:
        """Add incubator data to a tool result.

        Returns the SAME dict object when no enrichment flags are active.
        Returns a shallow copy with `_incubator_*` keys when enriching.
        Never modifies existing keys in the result.
        """
        if not self._any_enrichment_on():
            return result  # same object — zero overhead
        # Shallow copy only when we know we'll add data
        enriched = dict(result)

        # Convention fingerprints for classes in obligations
        if flags.convention_fingerprint_enabled():
            conv_data = self._enrich_conventions(enriched)
            if conv_data:
                enriched["_incubator_conventions"] = conv_data

        # State flow graphs for shared_state obligations
        if flags.state_flow_enabled():
            flow_data = self._enrich_state_flow(enriched)
            if flow_data:
                enriched["_incubator_state_flow"] = flow_data

        # Historical obligation patterns (decision-time)
        if self._intel_reader is not None:
            history = self._enrich_from_history(enriched)
            if history:
                enriched["_incubator_obligation_history"] = history

        return enriched

    def log_interaction(self, tool_name: str, result: dict[str, Any]) -> None:
        """Fire-and-forget logging after successful tool completion.

        Called AFTER token tracking, so the logged shape matches
        what the agent sees (including _token_footprint).
        """
        if self._intel_logger is not None:
            self._intel_logger.record(tool_name, result)

    def _enrich_from_history(self, result: dict[str, Any]) -> list[dict[str, Any]]:
        """Add historical obligation data from summary tables."""
        if self._intel_reader is None:
            return []
        subjects = [
            obl.get("target", "") for obl in result.get("obligations", [])
            if obl.get("target")
        ]
        if not subjects:
            return []
        return self._intel_reader.get_obligation_history(subjects)

    def _enrich_conventions(self, result: dict[str, Any]) -> list[dict[str, Any]]:
        """Add convention fingerprints for classes mentioned in obligations."""
        from groundtruth.analysis.conventions import fingerprint_class

        conventions: list[dict[str, Any]] = []
        seen_classes: set[str] = set()

        for obl in result.get("obligations", []):
            target_file = obl.get("file", "")
            target = obl.get("target", "")
            # Extract class name from "ClassName.method" pattern
            class_name = target.split(".")[0] if "." in target else ""
            if not class_name or not target_file or class_name in seen_classes:
                continue
            seen_classes.add(class_name)

            try:
                source = self._read_source(target_file)
                if source:
                    fp = fingerprint_class(source, class_name)
                    conventions.append({
                        "class": class_name,
                        "file": target_file,
                        "guard_clause_freq": fp.guard_clause_freq,
                        "error_type": fp.error_type,
                        "return_shape": fp.return_shape,
                    })
            except Exception:
                pass

        return conventions

    def _enrich_state_flow(self, result: dict[str, Any]) -> list[dict[str, Any]]:
        """Add state flow graphs for shared_state obligations."""
        from groundtruth.analysis.pattern_roles import build_state_flow

        flows: list[dict[str, Any]] = []
        seen_classes: set[str] = set()

        for obl in result.get("obligations", []):
            if obl.get("kind") != "shared_state":
                continue
            target_file = obl.get("file", "")
            target = obl.get("target", "")
            class_name = target.split(".")[0] if "." in target else ""
            if not class_name or not target_file or class_name in seen_classes:
                continue
            seen_classes.add(class_name)

            try:
                source = self._read_source(target_file)
                if source:
                    graph = build_state_flow(source, class_name)
                    if graph.attr_to_methods:
                        flows.append({
                            "class": class_name,
                            "file": target_file,
                            "attr_to_methods": {
                                attr: dict(methods)
                                for attr, methods in graph.attr_to_methods.items()
                            },
                        })
            except Exception:
                pass

        return flows

    def _read_source(self, file_path: str) -> str | None:
        """Read source file from disk. Returns None on failure."""
        import os
        full = os.path.join(self._root_path, file_path)
        try:
            with open(full, encoding="utf-8", errors="replace") as f:
                return f.read()
        except OSError:
            return None

    def _any_enrichment_on(self) -> bool:
        """True if any flag that adds data to tool responses is active."""
        return (
            flags.repo_intel_decisions_enabled()
            or flags.foundation_enabled()
            or flags.state_flow_enabled()
            or flags.convention_fingerprint_enabled()
        )


def any_phase5_flag_on() -> bool:
    """True if any Phase 5 flag is active (including logging-only).

    Used to decide whether to construct IncubatorRuntime at all.
    """
    return (
        flags.repo_intel_logging_enabled()
        or flags.repo_intel_decisions_enabled()
        or flags.response_state_machine_enabled()
        or flags.foundation_enabled()
        or flags.hnsw_enabled()
        or flags.state_flow_enabled()
        or flags.convention_fingerprint_enabled()
    )
