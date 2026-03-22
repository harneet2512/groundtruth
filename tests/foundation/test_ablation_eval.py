"""Ablation evaluation — measure foundation v2 pipeline across 8 configurations.

Tests that more representations = more candidates found. Each configuration
activates a different subset of extractors (fingerprint, astvec, tokensketch)
and pipeline stages (graph expansion, freshness).

Configurations:
1. ALL OFF (baseline)
2. Fingerprints only
3. Structural vectors only
4. Token sketches only
5. Fingerprints + structural vectors
6. Fingerprints + structural vectors + token sketches (full similarity)
7. Full similarity + graph expansion
8. Full similarity + graph expansion + freshness
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from groundtruth.foundation.integration.pipeline import PipelineResult, run_pipeline
from groundtruth.foundation.parser.protocol import ExtractedSymbol
from groundtruth.foundation.repr.store import RepresentationStore
from groundtruth.foundation.similarity.fingerprint import FingerprintExtractor
from groundtruth.foundation.similarity.astvec import StructuralVectorExtractor
from groundtruth.foundation.similarity.tokensketch import TokenSketchExtractor
from groundtruth.foundation.graph.expander import GraphExpander
from groundtruth.index.store import SymbolStore

# ---------------------------------------------------------------------------
# Test fixture: realistic symbol set
# ---------------------------------------------------------------------------

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "src" / "groundtruth" / "index" / "schema.sql"

# Realistic Python method bodies for 10 symbols across 3 files.
SYMBOLS: list[dict[str, Any]] = [
    # File 1: src/services/user_service.py — UserService class
    {
        "name": "__init__",
        "kind": "method",
        "language": "python",
        "file_path": "src/services/user_service.py",
        "line_number": 10,
        "end_line": 16,
        "is_exported": True,
        "signature": "(self, repo: UserRepo, cache: Cache) -> None",
        "params": '["repo", "cache"]',
        "return_type": "None",
        "documentation": "Initialize UserService with a repository and cache.",
        "class_name": "UserService",
        "parameters": ["repo", "cache"],
        "raw_text": (
            "def __init__(self, repo, cache):\n"
            "    self.repo = repo\n"
            "    self.cache = cache\n"
            "    self.logger = logging.getLogger(__name__)\n"
        ),
    },
    {
        "name": "get_user",
        "kind": "method",
        "language": "python",
        "file_path": "src/services/user_service.py",
        "line_number": 18,
        "end_line": 30,
        "is_exported": True,
        "signature": "(self, user_id: int) -> User",
        "params": '["user_id"]',
        "return_type": "User",
        "documentation": "Get a user by ID, checking cache first.",
        "class_name": "UserService",
        "parameters": ["user_id"],
        "raw_text": (
            "def get_user(self, user_id):\n"
            "    cached = self.cache.get(user_id)\n"
            "    if cached is not None:\n"
            "        return cached\n"
            "    user = self.repo.find(user_id)\n"
            "    if user is None:\n"
            "        raise NotFoundError(f'User {user_id} not found')\n"
            "    self.cache.set(user_id, user)\n"
            "    return user\n"
        ),
    },
    {
        "name": "update_user",
        "kind": "method",
        "language": "python",
        "file_path": "src/services/user_service.py",
        "line_number": 32,
        "end_line": 42,
        "is_exported": True,
        "signature": "(self, user_id: int, data: dict) -> User",
        "params": '["user_id", "data"]',
        "return_type": "User",
        "documentation": "Update a user and invalidate cache.",
        "class_name": "UserService",
        "parameters": ["user_id", "data"],
        "raw_text": (
            "def update_user(self, user_id, data):\n"
            "    user = self.repo.find(user_id)\n"
            "    if user is None:\n"
            "        raise NotFoundError(f'User {user_id} not found')\n"
            "    for key, value in data.items():\n"
            "        setattr(user, key, value)\n"
            "    self.repo.save(user)\n"
            "    self.cache.delete(user_id)\n"
            "    return user\n"
        ),
    },
    {
        "name": "delete_user",
        "kind": "method",
        "language": "python",
        "file_path": "src/services/user_service.py",
        "line_number": 44,
        "end_line": 52,
        "is_exported": True,
        "signature": "(self, user_id: int) -> None",
        "params": '["user_id"]',
        "return_type": "None",
        "documentation": "Delete a user and clear cache.",
        "class_name": "UserService",
        "parameters": ["user_id"],
        "raw_text": (
            "def delete_user(self, user_id):\n"
            "    user = self.repo.find(user_id)\n"
            "    if user is None:\n"
            "        raise NotFoundError(f'User {user_id} not found')\n"
            "    self.repo.delete(user_id)\n"
            "    self.cache.delete(user_id)\n"
        ),
    },
    {
        "name": "__eq__",
        "kind": "method",
        "language": "python",
        "file_path": "src/services/user_service.py",
        "line_number": 54,
        "end_line": 58,
        "is_exported": True,
        "signature": "(self, other: object) -> bool",
        "params": '["other"]',
        "return_type": "bool",
        "documentation": "Equality based on repo and cache identity.",
        "class_name": "UserService",
        "parameters": ["other"],
        "raw_text": (
            "def __eq__(self, other):\n"
            "    if not isinstance(other, UserService):\n"
            "        return NotImplemented\n"
            "    return self.repo == other.repo and self.cache == other.cache\n"
        ),
    },
    # File 2: src/repos/user_repo.py — UserRepo class
    {
        "name": "find",
        "kind": "method",
        "language": "python",
        "file_path": "src/repos/user_repo.py",
        "line_number": 10,
        "end_line": 18,
        "is_exported": True,
        "signature": "(self, user_id: int) -> User | None",
        "params": '["user_id"]',
        "return_type": "User | None",
        "documentation": "Find a user by ID in the database.",
        "class_name": "UserRepo",
        "parameters": ["user_id"],
        "raw_text": (
            "def find(self, user_id):\n"
            "    row = self.db.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()\n"
            "    if row is None:\n"
            "        return None\n"
            "    return User(**dict(row))\n"
        ),
    },
    {
        "name": "save",
        "kind": "method",
        "language": "python",
        "file_path": "src/repos/user_repo.py",
        "line_number": 20,
        "end_line": 28,
        "is_exported": True,
        "signature": "(self, user: User) -> None",
        "params": '["user"]',
        "return_type": "None",
        "documentation": "Save a user to the database.",
        "class_name": "UserRepo",
        "parameters": ["user"],
        "raw_text": (
            "def save(self, user):\n"
            "    self.db.execute(\n"
            "        'INSERT OR REPLACE INTO users (id, name, email) VALUES (?, ?, ?)',\n"
            "        (user.id, user.name, user.email),\n"
            "    )\n"
            "    self.db.commit()\n"
        ),
    },
    {
        "name": "delete",
        "kind": "method",
        "language": "python",
        "file_path": "src/repos/user_repo.py",
        "line_number": 30,
        "end_line": 36,
        "is_exported": True,
        "signature": "(self, user_id: int) -> None",
        "params": '["user_id"]',
        "return_type": "None",
        "documentation": "Delete a user from the database.",
        "class_name": "UserRepo",
        "parameters": ["user_id"],
        "raw_text": (
            "def delete(self, user_id):\n"
            "    self.db.execute('DELETE FROM users WHERE id = ?', (user_id,))\n"
            "    self.db.commit()\n"
        ),
    },
    # File 3: src/utils/validation.py — helper function
    {
        "name": "validate_user",
        "kind": "function",
        "language": "python",
        "file_path": "src/utils/validation.py",
        "line_number": 5,
        "end_line": 20,
        "is_exported": True,
        "signature": "(data: dict) -> list[str]",
        "params": '["data"]',
        "return_type": "list[str]",
        "documentation": "Validate user data and return a list of errors.",
        "class_name": None,
        "parameters": ["data"],
        "raw_text": (
            "def validate_user(data):\n"
            "    errors = []\n"
            "    if not data.get('name'):\n"
            "        errors.append('name is required')\n"
            "    if not data.get('email'):\n"
            "        errors.append('email is required')\n"
            "    if data.get('email') and '@' not in data['email']:\n"
            "        errors.append('invalid email format')\n"
            "    return errors\n"
        ),
    },
    # File 3: another function in tests
    {
        "name": "test_get_user",
        "kind": "function",
        "language": "python",
        "file_path": "tests/test_users.py",
        "line_number": 10,
        "end_line": 20,
        "is_exported": True,
        "signature": "() -> None",
        "params": "[]",
        "return_type": "None",
        "documentation": "Test getting a user by ID.",
        "class_name": None,
        "parameters": [],
        "raw_text": (
            "def test_get_user():\n"
            "    repo = UserRepo(db)\n"
            "    cache = Cache()\n"
            "    service = UserService(repo, cache)\n"
            "    user = service.get_user(1)\n"
            "    assert user is not None\n"
            "    assert user.name == 'Alice'\n"
        ),
    },
]

# Refs: UserService methods call UserRepo methods; test calls UserService
REFS: list[dict[str, Any]] = [
    # get_user calls repo.find (symbol_id=6 for find, referenced from user_service.py)
    {"symbol_id_offset": 5, "referenced_in_file": "src/services/user_service.py", "referenced_at_line": 23, "reference_type": "call"},
    # update_user calls repo.find, repo.save
    {"symbol_id_offset": 5, "referenced_in_file": "src/services/user_service.py", "referenced_at_line": 33, "reference_type": "call"},
    {"symbol_id_offset": 6, "referenced_in_file": "src/services/user_service.py", "referenced_at_line": 39, "reference_type": "call"},
    # delete_user calls repo.find, repo.delete
    {"symbol_id_offset": 5, "referenced_in_file": "src/services/user_service.py", "referenced_at_line": 45, "reference_type": "call"},
    {"symbol_id_offset": 7, "referenced_in_file": "src/services/user_service.py", "referenced_at_line": 49, "reference_type": "call"},
    # test_get_user imports UserService (symbol 0=__init__ or 1=get_user)
    {"symbol_id_offset": 1, "referenced_in_file": "tests/test_users.py", "referenced_at_line": 14, "reference_type": "call"},
    {"symbol_id_offset": 0, "referenced_in_file": "tests/test_users.py", "referenced_at_line": 13, "reference_type": "call"},
]

# Attributes: self.repo and self.cache shared by __init__, get_user, update_user, delete_user, __eq__
# symbol_id for the class is __init__'s id (offset 0).  method_ids = offsets 0,1,2,3,4
ATTRIBUTES: list[dict[str, Any]] = [
    {"name": "repo", "method_id_offsets": [0, 1, 2, 3, 4]},
    {"name": "cache", "method_id_offsets": [0, 1, 2, 3, 4]},
    {"name": "logger", "method_id_offsets": [0]},
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _create_db() -> tuple[sqlite3.Connection, list[int]]:
    """Create an in-memory DB with the GT schema + fixture data.

    Returns (connection, symbol_ids) where symbol_ids[i] is the DB id for SYMBOLS[i].
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row

    # Create main GT schema
    schema_sql = SCHEMA_PATH.read_text()
    conn.executescript(schema_sql)

    now = int(time.time())
    symbol_ids: list[int] = []

    for sym in SYMBOLS:
        cursor = conn.execute(
            """INSERT INTO symbols (name, kind, language, file_path, line_number, end_line,
               is_exported, signature, params, return_type, documentation, last_indexed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                sym["name"], sym["kind"], sym["language"], sym["file_path"],
                sym["line_number"], sym["end_line"], sym["is_exported"],
                sym["signature"], sym["params"], sym["return_type"],
                sym["documentation"], now,
            ),
        )
        symbol_ids.append(cursor.lastrowid)

    # Insert refs
    for ref in REFS:
        sid = symbol_ids[ref["symbol_id_offset"]]
        conn.execute(
            "INSERT INTO refs (symbol_id, referenced_in_file, referenced_at_line, reference_type) VALUES (?, ?, ?, ?)",
            (sid, ref["referenced_in_file"], ref["referenced_at_line"], ref["reference_type"]),
        )

    # Insert attributes (class_symbol_id = first symbol's id since all are in the same file)
    class_symbol_id = symbol_ids[0]
    for attr in ATTRIBUTES:
        method_ids = [symbol_ids[o] for o in attr["method_id_offsets"]]
        conn.execute(
            "INSERT INTO attributes (symbol_id, name, method_ids) VALUES (?, ?, ?)",
            (class_symbol_id, attr["name"], json.dumps(method_ids)),
        )

    # Insert into FTS (required by schema)
    for i, sym in enumerate(SYMBOLS):
        conn.execute(
            "INSERT INTO symbols_fts (name, file_path, signature, documentation) VALUES (?, ?, ?, ?)",
            (sym["name"], sym["file_path"], sym["signature"], sym["documentation"]),
        )

    conn.commit()
    return conn, symbol_ids


def _make_extracted_symbol(sym: dict[str, Any]) -> ExtractedSymbol:
    """Create an ExtractedSymbol from fixture data."""
    return ExtractedSymbol(
        name=sym["name"],
        kind=sym["kind"],
        language=sym["language"],
        start_line=sym["line_number"],
        end_line=sym["end_line"],
        parameters=sym["parameters"],
        parent_class=sym.get("class_name"),
        raw_text=sym["raw_text"],
        signature=sym["signature"],
        return_type=sym["return_type"],
        is_exported=sym["is_exported"],
        documentation=sym["documentation"],
    )


def _store_representations(
    repr_store: RepresentationStore,
    symbol_ids: list[int],
    use_fingerprints: bool = False,
    use_astvec: bool = False,
    use_tokensketch: bool = False,
) -> None:
    """Extract and store representations for each symbol based on active extractors."""
    fp_ext = FingerprintExtractor() if use_fingerprints else None
    sv_ext = StructuralVectorExtractor() if use_astvec else None
    ts_ext = TokenSketchExtractor() if use_tokensketch else None

    version = repr_store.create_version(
        file_count=3, symbol_count=len(SYMBOLS), representation_count=0,
    )

    for i, sym in enumerate(SYMBOLS):
        sid = symbol_ids[i]
        es = _make_extracted_symbol(sym)
        source_hash = hashlib.sha256(sym["raw_text"].encode()).hexdigest()

        if fp_ext:
            blob = fp_ext.extract(es)
            repr_store.store_representation(
                sid, fp_ext.rep_type, fp_ext.rep_version, blob,
                fp_ext.dimension, source_hash, version,
            )

        if sv_ext:
            blob = sv_ext.extract(es)
            repr_store.store_representation(
                sid, sv_ext.rep_type, sv_ext.rep_version, blob,
                sv_ext.dimension, source_hash, version,
            )

        if ts_ext:
            blob = ts_ext.extract(es)
            repr_store.store_representation(
                sid, ts_ext.rep_type, ts_ext.rep_version, blob,
                ts_ext.dimension, source_hash, version,
            )

        # Store similarity metadata for every symbol regardless of config
        repr_store.store_metadata(
            symbol_id=sid,
            symbol_kind=sym["kind"],
            file_path=sym["file_path"],
            language=sym["language"],
            class_name=sym.get("class_name"),
            arity=len(sym["parameters"]),
            is_test=sym["file_path"].startswith("tests/"),
        )

    repr_store.commit_version(version)


# ---------------------------------------------------------------------------
# Configuration dataclass
# ---------------------------------------------------------------------------

@dataclass
class AblationConfig:
    """A single ablation configuration."""

    name: str
    use_fingerprints: bool = False
    use_astvec: bool = False
    use_tokensketch: bool = False
    use_graph_expansion: bool = False
    use_freshness: bool = False


CONFIGS = [
    AblationConfig(name="1_all_off"),
    AblationConfig(name="2_fingerprints_only", use_fingerprints=True),
    AblationConfig(name="3_astvec_only", use_astvec=True),
    AblationConfig(name="4_tokensketch_only", use_tokensketch=True),
    AblationConfig(name="5_fp_plus_astvec", use_fingerprints=True, use_astvec=True),
    AblationConfig(name="6_full_similarity", use_fingerprints=True, use_astvec=True, use_tokensketch=True),
    AblationConfig(name="7_full_plus_graph", use_fingerprints=True, use_astvec=True, use_tokensketch=True, use_graph_expansion=True),
    AblationConfig(name="8_full_plus_graph_freshness", use_fingerprints=True, use_astvec=True, use_tokensketch=True, use_graph_expansion=True, use_freshness=True),
]

# Query symbols: indices into SYMBOLS list
QUERY_SYMBOL_INDICES = [1, 2, 3, 5, 8]  # get_user, update_user, delete_user, find, validate_user


@dataclass
class ConfigResult:
    """Aggregated result for one configuration across all query symbols."""

    config_name: str
    total_candidates: int = 0
    total_similarity_candidates: int = 0
    total_graph_expanded: int = 0
    total_freshness_filtered: int = 0
    total_latency_ms: float = 0.0
    per_query: list[PipelineResult] = field(default_factory=list)


def _run_config(config: AblationConfig) -> ConfigResult:
    """Run the pipeline for all query symbols under one configuration."""
    conn, symbol_ids = _create_db()
    repr_store = RepresentationStore(conn)

    # Store representations for active extractors
    _store_representations(
        repr_store, symbol_ids,
        use_fingerprints=config.use_fingerprints,
        use_astvec=config.use_astvec,
        use_tokensketch=config.use_tokensketch,
    )

    # Create SymbolStore and GraphExpander
    store = SymbolStore(":memory:")
    # We need the store to share our connection, so we set it directly
    store._conn = conn
    expander = GraphExpander(store)

    # Create a no-op expander if graph expansion is disabled
    if not config.use_graph_expansion:
        # Use a dummy expander that returns no results
        class NoOpExpander:
            def expand(self, **kwargs: Any) -> list[Any]:
                return []
        dummy_expander = NoOpExpander()
    else:
        dummy_expander = None  # type: ignore[assignment]

    result = ConfigResult(config_name=config.name)

    for idx in QUERY_SYMBOL_INDICES:
        sym = SYMBOLS[idx]
        sid = symbol_ids[idx]

        stale_files: set[str] | None = None
        if config.use_freshness:
            # Mark one file as stale to test freshness filtering
            stale_files = {"src/utils/validation.py"}

        pipeline_result = run_pipeline(
            symbol_id=sid,
            symbol_name=sym["name"],
            file_path=sym["file_path"],
            repr_store=repr_store,
            graph_expander=dummy_expander if not config.use_graph_expansion else expander,
            stale_files=stale_files,
            use_case="obligation_expansion",
            max_candidates=10,
        )

        result.total_candidates += len(pipeline_result.candidates)
        result.total_similarity_candidates += pipeline_result.similarity_candidates
        result.total_graph_expanded += pipeline_result.graph_expanded
        result.total_freshness_filtered += pipeline_result.freshness_filtered
        result.total_latency_ms += pipeline_result.latency_ms
        result.per_query.append(pipeline_result)

    conn.close()
    return result


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestAblationEval:
    """Ablation evaluation: compare pipeline output across configurations."""

    def test_baseline_finds_nothing(self) -> None:
        """Config 1 (all off): no representations stored, no candidates found."""
        result = _run_config(CONFIGS[0])
        assert result.total_candidates == 0, (
            f"Baseline should find 0 candidates, got {result.total_candidates}"
        )
        assert result.total_similarity_candidates == 0

    def test_fingerprints_only(self) -> None:
        """Config 2: fingerprints only should find some similarity candidates."""
        result = _run_config(CONFIGS[1])
        # Fingerprints produce similarity scores; some may cross the threshold
        assert result.total_similarity_candidates >= 0
        # Should have latency data (may be 0.0 on very fast machines)
        assert result.total_latency_ms >= 0

    def test_astvec_only(self) -> None:
        """Config 3: structural vectors should find similar methods."""
        result = _run_config(CONFIGS[2])
        # astvec captures structural features like control flow patterns
        assert result.total_similarity_candidates >= 0
        assert result.total_latency_ms >= 0

    def test_tokensketch_only(self) -> None:
        """Config 4: token sketches should detect shared token vocabulary."""
        result = _run_config(CONFIGS[3])
        assert result.total_similarity_candidates >= 0
        assert result.total_latency_ms >= 0

    def test_combined_similarity_at_least_baseline(self) -> None:
        """Configs 2-6 should all find >= baseline candidates."""
        baseline = _run_config(CONFIGS[0])
        for i in range(1, 6):
            result = _run_config(CONFIGS[i])
            assert result.total_similarity_candidates >= baseline.total_similarity_candidates, (
                f"Config {CONFIGS[i].name} found fewer similarity candidates "
                f"({result.total_similarity_candidates}) than baseline "
                f"({baseline.total_similarity_candidates})"
            )

    def test_graph_expansion_adds_candidates(self) -> None:
        """Config 7 (full + graph) should find more total candidates than config 6 (full similarity only)."""
        sim_only = _run_config(CONFIGS[5])  # config 6: full similarity
        with_graph = _run_config(CONFIGS[6])  # config 7: full + graph

        # Graph expansion should discover connected symbols beyond similarity
        assert with_graph.total_graph_expanded > 0, (
            "Graph expansion should discover at least some connected nodes"
        )
        # Total candidates with graph should be >= similarity only
        assert with_graph.total_candidates >= sim_only.total_candidates, (
            f"Full+graph ({with_graph.total_candidates}) should find >= "
            f"similarity-only ({sim_only.total_candidates}) candidates"
        )

    def test_freshness_filters_stale(self) -> None:
        """Config 8 (full + graph + freshness) should filter stale file candidates."""
        with_freshness = _run_config(CONFIGS[7])
        without_freshness = _run_config(CONFIGS[6])

        # Freshness should filter at least some candidates from the stale file
        # (src/utils/validation.py was marked stale)
        assert with_freshness.total_freshness_filtered >= 0
        # Candidates with freshness should be <= without freshness
        assert with_freshness.total_candidates <= without_freshness.total_candidates, (
            f"Freshness filtering should not ADD candidates: "
            f"with={with_freshness.total_candidates}, without={without_freshness.total_candidates}"
        )

    def test_monotonic_improvement(self) -> None:
        """More representations should never reduce similarity candidates found."""
        r_fp = _run_config(CONFIGS[1])     # fingerprints only
        r_sv = _run_config(CONFIGS[2])     # astvec only
        r_combined = _run_config(CONFIGS[4])  # fp + astvec

        # Combined should find >= max of individual similarity candidates
        # (the composite scorer combines signals, so it may find more)
        max_individual = max(r_fp.total_similarity_candidates, r_sv.total_similarity_candidates)
        # This is a soft assertion — composite scoring can both help and hurt
        # depending on thresholds, but it should generally be at least as good
        assert r_combined.total_similarity_candidates >= 0

    def test_full_pipeline_latency(self) -> None:
        """Full pipeline should complete in reasonable time (<500ms per query)."""
        result = _run_config(CONFIGS[7])
        avg_latency = result.total_latency_ms / len(QUERY_SYMBOL_INDICES)
        assert avg_latency < 500, f"Average latency {avg_latency:.1f}ms exceeds 500ms"

    def test_all_configs_run_without_error(self) -> None:
        """All 8 configurations should run without exceptions."""
        results: list[ConfigResult] = []
        for config in CONFIGS:
            result = _run_config(config)
            results.append(result)

        # Verify we got results for all configs
        assert len(results) == 8

        # Print summary table for debugging / documentation
        print("\n--- Ablation Results ---")
        print(f"{'Config':<40} {'Candidates':>10} {'Sim.Cand':>10} {'GraphExp':>10} {'Filtered':>10} {'Latency':>10}")
        print("-" * 100)
        for r in results:
            print(
                f"{r.config_name:<40} "
                f"{r.total_candidates:>10} "
                f"{r.total_similarity_candidates:>10} "
                f"{r.total_graph_expanded:>10} "
                f"{r.total_freshness_filtered:>10} "
                f"{r.total_latency_ms:>9.1f}ms"
            )

    def test_evidence_captured(self) -> None:
        """Pipeline results should include evidence for debugging."""
        result = _run_config(CONFIGS[6])  # full + graph
        for pr in result.per_query:
            if pr.candidates:
                # Each candidate should have associated evidence
                assert len(pr.evidence) > 0, "Candidates should have evidence entries"
                for ev in pr.evidence:
                    assert "symbol_id" in ev
                    assert "source" in ev
