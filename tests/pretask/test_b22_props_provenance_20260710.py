"""B-22 (contract_map property provenance consumer).

The Go indexer now stores per-fact provenance on ``properties``
(``property_id, start_line, end_line, extractor, evidence_method, trust_tier,
verification_status, source_revision``). The consumer must (1) READ those columns
with back-compat (a legacy ``node_id,kind,value,line,confidence`` table still reads —
no ``no such column``), (2) GATE correct-or-quiet on confidence AND trust_tier (a
non-empty tier outside {CERTIFIED,CANDIDATE} is dropped; an empty/legacy tier gates on
confidence alone), and (3) CARRY property_id + span downstream on a shape that allows it
(``read_property_facts`` -> ``PropertyFact``).

RED (pre-fix): ``_read_props`` selected only ``kind,value`` gated on confidence, so a
SPECULATIVE-tier property with confidence 0.6 (>= the 0.5 floor) was DELIVERED as a fact;
``read_property_facts`` did not exist. GREEN: the tier gate drops it; the receipt surface
carries property_id + span.
"""
from __future__ import annotations

import sqlite3

from groundtruth.pretask.contract_map import (
    PropertyFact,
    _read_props,
    read_property_facts,
)

# The current-binary properties schema (parity with gt-index/internal/store/sqlite.go).
_FULL_SCHEMA = (
    "CREATE TABLE properties (id INTEGER PRIMARY KEY AUTOINCREMENT, node_id INTEGER,"
    " kind TEXT, value TEXT, line INTEGER, confidence REAL, property_id TEXT,"
    " start_line INTEGER, end_line INTEGER, extractor TEXT, evidence_method TEXT,"
    " trust_tier TEXT, verification_status TEXT, source_revision TEXT)"
)
_NODES = (
    "CREATE TABLE nodes (id INTEGER PRIMARY KEY, label TEXT, name TEXT,"
    " qualified_name TEXT, file_path TEXT, start_line INTEGER, end_line INTEGER,"
    " is_test INTEGER)"
)


def _full_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(_NODES + ";" + _FULL_SCHEMA)
    conn.execute(
        "INSERT INTO nodes(id,label,name,file_path,start_line,end_line,is_test)"
        " VALUES(1,'Function','get_user','src/u.py',10,20,0)"
    )
    return conn


def _insert(conn, **kw):
    cols = ("node_id", "kind", "value", "line", "confidence", "property_id",
            "start_line", "end_line", "extractor", "evidence_method",
            "trust_tier", "verification_status", "source_revision")
    conn.execute(
        "INSERT INTO properties(" + ",".join(cols) + ") VALUES(" + ",".join("?" * len(cols)) + ")",
        tuple(kw.get(c) for c in cols),
    )


# ------------------------------------------------------------------ B-22 gate
def test_speculative_tier_dropped_even_at_pass_confidence():
    """A non-empty trust_tier outside {CERTIFIED,CANDIDATE} is NOT a contract fact —
    dropped even when its confidence clears the 0.5 floor (correct-or-quiet). This is
    the RED behaviour: the old confidence-only gate delivered the SPECULATIVE row."""
    conn = _full_conn()
    _insert(conn, node_id=1, kind="boundary_condition", value="x > 0", line=12,
            confidence=0.6, property_id="a" * 64, start_line=12, end_line=12,
            extractor="ast", evidence_method="static", trust_tier="CANDIDATE",
            verification_status="unverified", source_revision="rev1")
    _insert(conn, node_id=1, kind="boundary_condition", value="y < 9", line=13,
            confidence=0.6, property_id="b" * 64, start_line=13, end_line=13,
            extractor="ast", evidence_method="static", trust_tier="SPECULATIVE",
            verification_status="unverified", source_revision="rev1")
    conn.commit()
    vals = _read_props(conn, [1]).get("boundary_condition", [])
    assert "x > 0" in vals            # CANDIDATE -> fact
    assert "y < 9" not in vals        # SPECULATIVE -> dropped (was delivered pre-fix)


def test_certified_and_candidate_tiers_kept():
    conn = _full_conn()
    _insert(conn, node_id=1, kind="boundary_condition", value="a >= 1", line=11,
            confidence=1.0, property_id="c" * 64, start_line=11, end_line=11,
            extractor="ast", evidence_method="static", trust_tier="CERTIFIED",
            verification_status="verified", source_revision="rev1")
    conn.commit()
    assert "a >= 1" in _read_props(conn, [1]).get("boundary_condition", [])


# ------------------------------------------------------------ B-22 provenance
def test_read_property_facts_carries_id_span_tier(tmp_path):
    """The receipt surface carries property_id (dedup/receipt key) + span + tier — the
    provenance the {kind:[values]} shape cannot. RED: read_property_facts did not exist."""
    path = str(tmp_path / "graph.db")
    disk = sqlite3.connect(path)
    disk.executescript(_NODES + ";" + _FULL_SCHEMA)
    disk.execute(
        "INSERT INTO nodes(id,label,name,file_path,start_line,end_line,is_test)"
        " VALUES(1,'Function','get_user','src/u.py',10,20,0)"
    )
    _insert(disk, node_id=1, kind="return_shape", value="Optional[User]", line=15,
            confidence=0.95, property_id="d" * 64, start_line=15, end_line=17,
            extractor="lsp", evidence_method="typeflow", trust_tier="CERTIFIED",
            verification_status="verified", source_revision="revX")
    disk.commit()
    disk.close()
    facts = read_property_facts(path, "src/u.py", ["get_user"])
    assert len(facts) == 1
    f = facts[0]
    assert isinstance(f, PropertyFact)
    assert f.property_id == "d" * 64          # the dedup/receipt key
    assert (f.start_line, f.end_line) == (15, 17)   # the span
    assert f.trust_tier == "CERTIFIED"
    assert f.value == "Optional[User]"
    assert f.extractor == "lsp" and f.evidence_method == "typeflow"


# ------------------------------------------------------------- B-22 back-compat
def test_legacy_confidence_only_schema_still_reads():
    """A legacy properties table (confidence but NO B-22 provenance columns) reads
    WITHOUT error via the column-probe (the extended SELECT would otherwise say
    'no such column: start_line')."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE properties(node_id INTEGER, kind TEXT, value TEXT, line INTEGER,"
        " confidence REAL)"
    )
    conn.execute("INSERT INTO properties VALUES(1,'boundary_condition','n > 0',1,0.8)")
    conn.commit()
    assert _read_props(conn, [1]) == {"boundary_condition": ["n > 0"]}


def test_legacy_no_confidence_column_is_quiet():
    """B-25 preserved: no confidence column -> cannot verify -> correct-or-quiet {}."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE properties(node_id INTEGER, kind TEXT, value TEXT, line INTEGER)")
    conn.execute("INSERT INTO properties VALUES(1,'boundary_condition','n > 0',1)")
    conn.commit()
    assert _read_props(conn, [1]) == {}
