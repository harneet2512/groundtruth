"""graph.db schema version contract (FINAL_ARCH_V2 Track-A B-1).

Readers call ``verify_graph_db_schema`` before consuming a graph.db. If the
required ``project_meta.schema_version`` row is missing or older than what
the reader needs, a :class:`SchemaMismatch` is raised and the caller MUST
fail fast — silently returning empty rows from missing columns is the exact
parity failure this module exists to prevent.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

# Required schema columns on the edges table. Mirrors gt-index/internal/store/sqlite.go
# edges table at the version named in REQUIRED_SCHEMA_VERSION below.
REQUIRED_EDGE_COLUMNS: frozenset[str] = frozenset(
    {
        "trust_tier",
        "candidate_count",
        "evidence_type",
        "verification_status",
    }
)

# Minimum schema_version the FINAL_ARCH_V2 router pipeline requires. The Go
# indexer writes ``schema_version`` into ``project_meta`` at build time. If the
# row is absent, the binary that wrote the DB is pre-FINAL_ARCH_V2.
REQUIRED_SCHEMA_VERSION = "v15.2-trust-tier"


class SchemaMismatch(RuntimeError):
    """graph.db's schema is incompatible with the current reader."""


@dataclass(frozen=True)
class SchemaProbe:
    """What we learned about a graph.db's schema. Useful for telemetry."""

    db_path: str
    schema_version: str | None
    indexer_version: str | None
    git_commit: str | None
    build_time_utc: str | None
    has_required_columns: bool
    missing_columns: frozenset[str]


def probe(db_path: str | Path) -> SchemaProbe:
    """Read schema_version / provenance from project_meta + check edge columns.

    Tolerant of missing tables: returns a probe with all-None metadata when
    the DB predates project_meta. The caller decides whether to raise.
    """
    db_path = str(db_path)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        meta: dict[str, str] = {}
        try:
            for k, v in conn.execute("SELECT key, value FROM project_meta"):
                meta[str(k)] = str(v) if v is not None else ""
        except sqlite3.OperationalError:
            pass
        try:
            edge_cols = {row[1] for row in conn.execute("PRAGMA table_info(edges)")}
        except sqlite3.OperationalError:
            edge_cols = set()
        missing = REQUIRED_EDGE_COLUMNS - edge_cols
        return SchemaProbe(
            db_path=db_path,
            schema_version=meta.get("schema_version"),
            indexer_version=meta.get("indexer_version"),
            git_commit=meta.get("git_commit"),
            build_time_utc=meta.get("build_time_utc"),
            has_required_columns=not missing,
            missing_columns=frozenset(missing),
        )
    finally:
        conn.close()


_SCHEMA_VERSION_NUM_RE = re.compile(r"^v(\d+)(?:\.(\d+))?")


def _version_tuple(version: str) -> tuple[int, int] | None:
    """``v15.4-callsite-actuals`` -> ``(15, 4)``; unparseable -> ``None``.

    Producer versions are monotonically increasing ``v<major>[.<minor>]-<tag>``
    stamps — the tag names the change, the numbers order it. Schema changes
    are additive-only (new columns/tables), so a NEWER graph satisfies a
    reader whose required floor is older.
    """
    m = _SCHEMA_VERSION_NUM_RE.match(version)
    if m is None:
        return None
    return (int(m.group(1)), int(m.group(2) or 0))


def verify_graph_db_schema(
    db_path: str | Path,
    *,
    required_schema_version: str = REQUIRED_SCHEMA_VERSION,
    strict: bool = True,
) -> SchemaProbe:
    """Fail fast if the DB is too old for FINAL_ARCH_V2 consumers.

    Raises ``SchemaMismatch`` when:
      - ``schema_version`` is missing from project_meta (pre-stamping binary).
      - ``schema_version`` orders BELOW the required floor (ordered compare —
        additive schema bumps like ``v15.4`` satisfy a ``v15.2`` floor).
      - Any required edges column is absent.

    Set ``strict=False`` to only return the probe without raising — useful for
    diagnostic tools that want to surface the mismatch without crashing.
    """
    p = probe(db_path)
    if not strict:
        return p
    reasons: list[str] = []
    if p.schema_version is None:
        reasons.append(
            "project_meta.schema_version is missing — binary predates "
            "FINAL_ARCH_V2 provenance stamping"
        )
    else:
        have = _version_tuple(p.schema_version)
        want = _version_tuple(required_schema_version)
        if have is None or want is None or have < want:
            reasons.append(
                f"schema_version={p.schema_version!r} below required={required_schema_version!r}"
            )
    if p.missing_columns:
        reasons.append(
            "edges table missing required columns: " + ", ".join(sorted(p.missing_columns))
        )
    if reasons:
        raise SchemaMismatch(
            f"graph.db at {p.db_path!r} is incompatible with FINAL_ARCH_V2 reader: "
            + " | ".join(reasons)
        )
    return p


__all__ = [
    "REQUIRED_EDGE_COLUMNS",
    "REQUIRED_SCHEMA_VERSION",
    "SchemaMismatch",
    "SchemaProbe",
    "probe",
    "verify_graph_db_schema",
]
