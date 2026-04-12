"""Protocol interfaces for the GT substrate boundary.

These protocols define the contract between semantic logic (contracts,
verification, procedures) and the underlying data layer (graph.db via
GraphStore). All new semantic code depends on these protocols — never
on raw sqlite3 connections directly.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from groundtruth.substrate.types import ContractRecord, EvidenceItem


# ---------------------------------------------------------------------------
# Graph access protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class GraphReader(Protocol):
    """Read-only access to the code graph (graph.db).

    Implementations wrap GraphStore or raw sqlite3 connections.
    Methods return plain dicts to avoid coupling semantic logic to
    specific ORM types.
    """

    def get_node_by_name(
        self, name: str, file_path: str | None = None
    ) -> dict | None:
        """Find a node by symbol name, optionally scoped to a file.

        Returns dict with keys: id, label, name, qualified_name,
        file_path, start_line, end_line, signature, return_type,
        is_exported, is_test, language, parent_id.
        """
        ...

    def get_node_by_id(self, node_id: int) -> dict | None:
        """Get a single node by its primary key."""
        ...

    def get_callers(self, node_id: int) -> list[dict]:
        """Get all nodes that call this node (incoming CALLS edges).

        Each dict includes: source_id, source_name, source_file,
        source_line, resolution_method, confidence, edge_type.
        """
        ...

    def get_callees(self, node_id: int) -> list[dict]:
        """Get all nodes called by this node (outgoing CALLS edges)."""
        ...

    def get_siblings(self, node_id: int) -> list[dict]:
        """Get sibling methods/functions (same parent class or file).

        Each dict includes full node fields plus properties if available.
        """
        ...

    def get_tests_for(self, node_id: int) -> list[dict]:
        """Get test nodes that exercise this symbol.

        Resolved via: direct test edges, name-matching test_<name>,
        or assertion target_node_id references.
        """
        ...

    def get_properties(
        self, node_id: int, kind: str | None = None
    ) -> list[dict]:
        """Get structural properties for a node.

        Optional kind filter: 'guard_clause', 'return_shape',
        'exception_type', 'raise_type', 'framework_call', 'docstring'.

        Each dict: id, node_id, kind, value, line, confidence.
        """
        ...

    def get_assertions(self, test_node_id: int) -> list[dict]:
        """Get assertions belonging to a specific test function.

        Each dict: id, test_node_id, target_node_id, kind,
        expression, expected, line.
        """
        ...

    def get_assertions_for_target(self, target_name: str) -> list[dict]:
        """Get all assertions that reference a target symbol by name.

        Used by contract extractors to find behavioral specs.
        """
        ...

    def get_nodes_in_file(self, file_path: str) -> list[dict]:
        """Get all nodes defined in a file."""
        ...

    def get_file_paths(self) -> list[str]:
        """Get all indexed file paths."""
        ...


# ---------------------------------------------------------------------------
# Contract extraction protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class ContractExtractor(Protocol):
    """Extracts contracts of a specific type from the code graph.

    Each extractor is responsible for one contract_type. The engine
    composes multiple extractors and gates results by confidence.
    """

    contract_type: str
    """The contract type this extractor produces (e.g. 'exception_message')."""

    def extract(
        self, reader: GraphReader, node_id: int
    ) -> list[ContractRecord]:
        """Extract contracts for a given symbol node.

        Returns all candidates including low-confidence ones — the
        engine handles gating.
        """
        ...


# ---------------------------------------------------------------------------
# Evidence production protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class EvidenceProducer(Protocol):
    """Produces evidence items for a target symbol.

    Each producer is responsible for one evidence family. The service
    composes multiple producers and ranks results.
    """

    family: str
    """Evidence family this producer generates (e.g. 'CALLER', 'TEST')."""

    def produce(
        self, reader: GraphReader, target_node_id: int, root: str
    ) -> list[EvidenceItem]:
        """Produce evidence items for a target node.

        Args:
            reader: Graph access.
            target_node_id: The node to produce evidence for.
            root: Repository root path (for relative path computation).

        Returns unsorted candidates — the service handles ranking.
        """
        ...


# ---------------------------------------------------------------------------
# Patch verification protocol (Phase 2)
# ---------------------------------------------------------------------------

@runtime_checkable
class PatchVerifier(Protocol):
    """Scores a candidate patch against contracts and tests."""

    def verify(
        self,
        diff: str,
        contracts: list[ContractRecord],
        changed_symbols: list[str],
    ) -> dict:
        """Score a diff against applicable contracts.

        Returns dict with: contract_score, violations, recommended_tests.
        """
        ...
