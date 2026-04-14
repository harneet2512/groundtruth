"""ContractEngine — composes extractors, manages confidence gating, persists to DB.

This is the main entry point for contract extraction. It runs all
registered extractors, applies confidence gating, and optionally
persists contracts to the database for later retrieval.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

from groundtruth.contracts.extractors.behavioral_assertion_extractor import BehavioralAssertionExtractor
from groundtruth.contracts.extractors.constructor_invariant_extractor import ConstructorInvariantExtractor
from groundtruth.contracts.extractors.constructor_postcondition_extractor import ConstructorPostconditionExtractor
from groundtruth.contracts.extractors.dispatch_registration_extractor import DispatchRegistrationExtractor
from groundtruth.contracts.extractors.exact_render_string_extractor import ExactRenderStringExtractor
from groundtruth.contracts.extractors.exception_extractor import ExceptionExtractor
from groundtruth.contracts.extractors.negative_extractor import NegativeExtractor
from groundtruth.contracts.extractors.obligation_extractor import ObligationExtractor
from groundtruth.contracts.extractors.output_extractor import OutputExtractor
from groundtruth.contracts.extractors.paired_behavior_extractor import PairedBehaviorExtractor
from groundtruth.contracts.extractors.protocol_invariant_extractor import ProtocolInvariantExtractor
from groundtruth.contracts.extractors.protocol_usage_extractor import ProtocolUsageExtractor
from groundtruth.contracts.extractors.registry_coupling_extractor import RegistryCouplingExtractor
from groundtruth.contracts.extractors.roundtrip_extractor import RoundtripExtractor
from groundtruth.contracts.extractors.type_shape_extractor import TypeShapeExtractor
from groundtruth.substrate.protocols import ContractExtractor, GraphReader
from groundtruth.substrate.types import ContractRecord

logger = logging.getLogger(__name__)

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


class ContractEngine:
    """Orchestrates contract extraction with confidence gating.

    Usage:
        engine = ContractEngine(reader, conn)
        contracts = engine.extract_all(node_id)
        # or persist:
        contracts = engine.extract_and_persist(node_id)
    """

    def __init__(
        self,
        reader: GraphReader,
        db_conn: sqlite3.Connection | None = None,
    ) -> None:
        self._reader = reader
        self._conn = db_conn
        self._extractors: list[ContractExtractor] = [
            ExceptionExtractor(),
            OutputExtractor(),
            ExactRenderStringExtractor(),
            RoundtripExtractor(),
            ObligationExtractor(),
            TypeShapeExtractor(),
            NegativeExtractor(),
            RegistryCouplingExtractor(),
            ProtocolInvariantExtractor(),
            ProtocolUsageExtractor(),
            ConstructorInvariantExtractor(),
            # Phase 2: high-value general semantic families
            BehavioralAssertionExtractor(),
            PairedBehaviorExtractor(),
            DispatchRegistrationExtractor(),
            ConstructorPostconditionExtractor(),
        ]
        self._schema_initialized = False

    def register_extractor(self, extractor: ContractExtractor) -> None:
        """Register an additional contract extractor."""
        self._extractors.append(extractor)

    def extract_all(self, node_id: int) -> list[ContractRecord]:
        """Run all extractors, gate by confidence.

        Returns only 'verified' and 'likely' contracts.
        'possible' tier is suppressed from runtime output.
        """
        results: list[ContractRecord] = []
        for extractor in self._extractors:
            try:
                contracts = extractor.extract(self._reader, node_id)
                # Gate: suppress 'possible' tier
                results.extend(c for c in contracts if c.tier != "possible")
            except Exception as exc:
                logger.debug(
                    "Extractor %s failed for node %d: %s",
                    getattr(extractor, "contract_type", "unknown"),
                    node_id,
                    exc,
                )
                continue
        return results

    def extract_and_persist(self, node_id: int) -> list[ContractRecord]:
        """Extract contracts and write to database.

        Creates the contracts/contract_evidence tables if they don't exist.
        Uses UPSERT semantics (INSERT OR REPLACE) based on the UNIQUE constraint.
        """
        contracts = self.extract_all(node_id)

        if self._conn is not None and contracts:
            self._ensure_schema()
            self._persist(contracts, node_id)

        return contracts

    def query_contracts(
        self,
        scope_ref: str | None = None,
        contract_type: str | None = None,
        tier: str | None = None,
    ) -> list[ContractRecord]:
        """Query persisted contracts from the database.

        Args:
            scope_ref: Filter by qualified name.
            contract_type: Filter by contract type.
            tier: Filter by confidence tier.

        Returns matching ContractRecords.
        """
        if self._conn is None:
            return []

        self._ensure_schema()

        query = "SELECT * FROM contracts WHERE 1=1"
        params: list = []

        if scope_ref:
            query += " AND scope_ref = ?"
            params.append(scope_ref)
        if contract_type:
            query += " AND contract_type = ?"
            params.append(contract_type)
        if tier:
            query += " AND tier = ?"
            params.append(tier)

        try:
            cursor = self._conn.execute(query, params)
            rows = cursor.fetchall()
        except sqlite3.Error as exc:
            logger.debug("Failed to query contracts: %s", exc)
            return []

        results: list[ContractRecord] = []
        for row in rows:
            # Fetch evidence sources for this contract
            try:
                ev_cursor = self._conn.execute(
                    "SELECT source_file, source_line FROM contract_evidence WHERE contract_id = ?",
                    (row[0],),  # id is first column
                )
                sources = tuple(
                    f"{r[0]}:{r[1]}" for r in ev_cursor.fetchall()
                )
            except sqlite3.Error:
                sources = ()

            results.append(ContractRecord(
                contract_type=row[1],
                scope_kind=row[2],
                scope_ref=row[3],
                predicate=row[5],
                normalized_form=row[6],
                support_sources=sources,
                support_count=row[7],
                confidence=row[8],
                tier=row[9],
            ))

        return results

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _ensure_schema(self) -> None:
        """Create contracts tables if they don't exist."""
        if self._schema_initialized or self._conn is None:
            return

        try:
            schema_sql = _SCHEMA_PATH.read_text()
            self._conn.executescript(schema_sql)
            self._schema_initialized = True
        except (sqlite3.Error, OSError) as exc:
            logger.debug("Failed to initialize contract schema: %s", exc)

    def _persist(self, contracts: list[ContractRecord], node_id: int) -> None:
        """Write contracts and evidence to database."""
        if self._conn is None:
            return

        now = int(time.time())

        for contract in contracts:
            try:
                # Stable upsert: preserve row ID on conflict (P1.1 fix)
                cursor = self._conn.execute(
                    """INSERT INTO contracts
                       (contract_type, scope_kind, scope_ref, node_id,
                        predicate, normalized_form, support_count,
                        confidence, tier, extracted_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(contract_type, scope_ref, normalized_form)
                       DO UPDATE SET
                        support_count = excluded.support_count,
                        confidence = excluded.confidence,
                        tier = excluded.tier,
                        extracted_at = excluded.extracted_at""",
                    (
                        contract.contract_type,
                        contract.scope_kind,
                        contract.scope_ref,
                        node_id,
                        contract.predicate,
                        contract.normalized_form,
                        contract.support_count,
                        contract.confidence,
                        contract.tier,
                        now,
                    ),
                )
                contract_id = cursor.lastrowid

                # Insert evidence rows
                if contract_id and contract.support_sources:
                    # Clear old evidence for this contract
                    self._conn.execute(
                        "DELETE FROM contract_evidence WHERE contract_id = ?",
                        (contract_id,),
                    )
                    for source in contract.support_sources:
                        parts = source.rsplit(":", 1)
                        source_file = parts[0] if parts else source
                        source_line = int(parts[1]) if len(parts) > 1 else 0
                        self._conn.execute(
                            """INSERT INTO contract_evidence
                               (contract_id, source_file, source_line,
                                source_kind, detail, confidence)
                               VALUES (?, ?, ?, ?, ?, ?)""",
                            (
                                contract_id,
                                source_file,
                                source_line,
                                contract.contract_type,
                                contract.predicate,
                                contract.confidence,
                            ),
                        )

            except sqlite3.Error as exc:
                logger.debug("Failed to persist contract: %s", exc)
                continue

        try:
            self._conn.commit()
        except sqlite3.Error:
            pass
