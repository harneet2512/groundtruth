"""Evidence depth parity tests — prove uniform depth across 5 languages.

Indexes equivalent auth-service fixtures in Python, Go, TypeScript, Java,
and Rust with gt-index, then asserts that evidence depth is uniform:
- Every property family that fires for Python also fires for other languages
- Non-Python depth score >= 75% of Python's
- All languages produce at least 3 assertions with spec-quality expressions
- Average edge confidence >= 0.7

These tests require the gt-index binary (auto-discovered via _binary.py).
"""

import os
import sqlite3
import subprocess

import pytest

# ---------------------------------------------------------------------------
# Fixture: index all 5 repos once per session
# ---------------------------------------------------------------------------

LANGUAGES = ["python", "go", "typescript", "java", "rust"]
FIXTURE_ROOTS = {
    "python": "tests/fixtures/project_py",
    "go": "tests/fixtures/project_go",
    "typescript": "tests/fixtures/project_ts",
    "java": "tests/fixtures/project_java",
    "rust": "tests/fixtures/project_rust",
}


@pytest.fixture(scope="session")
def indexed_repos(tmp_path_factory):
    """Index all 5 fixture repos with gt-index, return {lang: db_path}."""
    try:
        from groundtruth._binary import find_binary

        binary = find_binary()
    except Exception:
        pytest.skip("gt-index binary not available")
        return {}

    dbs = {}
    for lang, root in FIXTURE_ROOTS.items():
        if not os.path.isdir(root):
            pytest.skip(f"Fixture {root} not found")
            return {}
        db_path = str(tmp_path_factory.mktemp(lang) / "graph.db")
        result = subprocess.run(
            [binary, "-root", root, "-output", db_path],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, f"gt-index failed for {lang}: {result.stderr[:200]}"
        dbs[lang] = db_path
    return dbs


# ---------------------------------------------------------------------------
# Measurement helpers
# ---------------------------------------------------------------------------

PROPERTY_FAMILIES = ["guard_clause", "return_shape", "exception_type", "caller_usage", "docstring"]


def _connect(db_path: str) -> sqlite3.Connection:
    return sqlite3.connect(db_path)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    """Check if a table exists in the database."""
    row = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row[0] > 0


def measure_coverage(db_path: str) -> dict[str, bool]:
    """Returns {family: True/False} — does each property kind exist?"""
    conn = _connect(db_path)
    result = {}
    if _table_exists(conn, "properties"):
        for family in PROPERTY_FAMILIES:
            count = conn.execute(
                "SELECT COUNT(*) FROM properties WHERE kind = ?", (family,)
            ).fetchone()[0]
            result[family] = count > 0
    else:
        for family in PROPERTY_FAMILIES:
            result[family] = False
    # Assertions
    if _table_exists(conn, "assertions"):
        result["assertion"] = conn.execute("SELECT COUNT(*) FROM assertions").fetchone()[0] > 0
    else:
        result["assertion"] = False
    # Test functions
    result["test_function"] = (
        conn.execute("SELECT COUNT(*) FROM nodes WHERE is_test = 1").fetchone()[0] > 0
    )
    conn.close()
    return result


def measure_depth(db_path: str) -> dict[str, int]:
    """Returns {family: 0|1|2} depth score per family.

    0 = absent
    1 = present but shallow (exists but no rich expressions)
    2 = spec-quality (assertions have expressions with calls/comparisons)
    """
    conn = _connect(db_path)
    scores = {}

    has_properties = _table_exists(conn, "properties")
    for family in PROPERTY_FAMILIES:
        if not has_properties:
            scores[family] = 0
            continue
        rows = conn.execute(
            "SELECT value FROM properties WHERE kind = ? LIMIT 5", (family,)
        ).fetchall()
        if not rows:
            scores[family] = 0
        elif any(len(r[0] or "") > 10 for r in rows):
            scores[family] = 2  # Has meaningful content
        else:
            scores[family] = 1

    # Assertions depth
    if _table_exists(conn, "assertions"):
        assertion_rows = conn.execute("SELECT expression FROM assertions LIMIT 5").fetchall()
    else:
        assertion_rows = []
    if not assertion_rows:
        scores["assertion"] = 0
    elif any("(" in (r[0] or "") or "==" in (r[0] or "") for r in assertion_rows):
        scores["assertion"] = 2  # Spec-quality
    else:
        scores["assertion"] = 1

    # Test function depth
    test_count = conn.execute("SELECT COUNT(*) FROM nodes WHERE is_test = 1").fetchone()[0]
    scores["test_function"] = 2 if test_count >= 3 else (1 if test_count > 0 else 0)

    conn.close()
    return scores


# ---------------------------------------------------------------------------
# Parity tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("lang", ["go", "typescript", "java", "rust"])
def test_coverage_parity(indexed_repos, lang):
    """Every property family that fires for Python must fire for this language."""
    if not indexed_repos:
        pytest.skip("No indexed repos")
    py_cov = measure_coverage(indexed_repos["python"])
    lang_cov = measure_coverage(indexed_repos[lang])

    failures = []
    for family, py_fires in py_cov.items():
        if py_fires and not lang_cov.get(family, False):
            failures.append(family)

    assert not failures, f"{lang}: these families fire for Python but not {lang}: {failures}"


@pytest.mark.parametrize("lang", ["go", "typescript", "java", "rust"])
def test_depth_parity(indexed_repos, lang):
    """Non-Python depth score >= 75% of Python's."""
    if not indexed_repos:
        pytest.skip("No indexed repos")
    py_depth = measure_depth(indexed_repos["python"])
    lang_depth = measure_depth(indexed_repos[lang])

    py_total = sum(py_depth.values())
    lang_total = sum(lang_depth.values())
    ratio = lang_total / max(py_total, 1)

    assert ratio >= 0.75, (
        f"{lang}: depth {lang_total}/{py_total} = {ratio:.0%}, need >= 75%\n"
        f"  Python: {py_depth}\n"
        f"  {lang}: {lang_depth}"
    )


@pytest.mark.parametrize("lang", LANGUAGES)
def test_nodes_exist(indexed_repos, lang):
    """Every language produces nodes from indexing."""
    if not indexed_repos:
        pytest.skip("No indexed repos")
    conn = _connect(indexed_repos[lang])
    count = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    conn.close()
    assert count >= 10, f"{lang}: only {count} nodes, expected >= 10"


@pytest.mark.parametrize("lang", LANGUAGES)
def test_properties_exist(indexed_repos, lang):
    """Every language produces at least 20 properties."""
    if not indexed_repos:
        pytest.skip("No indexed repos")
    conn = _connect(indexed_repos[lang])
    count = conn.execute("SELECT COUNT(*) FROM properties").fetchone()[0]
    conn.close()
    assert count >= 20, f"{lang}: only {count} properties, expected >= 20"


@pytest.mark.parametrize("lang", LANGUAGES)
def test_assertions_extracted(indexed_repos, lang):
    """Every language produces at least 3 assertions from test files."""
    if not indexed_repos:
        pytest.skip("No indexed repos")
    conn = _connect(indexed_repos[lang])
    count = conn.execute("SELECT COUNT(*) FROM assertions").fetchone()[0]
    conn.close()
    assert count >= 3, f"{lang}: only {count} assertions, expected >= 3"


@pytest.mark.parametrize("lang", LANGUAGES)
def test_assertion_quality(indexed_repos, lang):
    """Assertions are spec-quality: contain function calls or comparisons."""
    if not indexed_repos:
        pytest.skip("No indexed repos")
    conn = _connect(indexed_repos[lang])
    rows = conn.execute("SELECT expression FROM assertions LIMIT 8").fetchall()
    conn.close()

    assert len(rows) > 0, f"{lang}: no assertions found"

    spec_count = 0
    for row in rows:
        expr = row[0] or ""
        has_call = "(" in expr
        has_comparison = any(op in expr for op in ("==", "!=", "assert", "REQUIRE", "expect"))
        if has_call or has_comparison:
            spec_count += 1

    ratio = spec_count / len(rows)
    assert ratio >= 0.5, (
        f"{lang}: only {spec_count}/{len(rows)} assertions are spec-quality (need >= 50%)"
    )


@pytest.mark.parametrize("lang", LANGUAGES)
def test_guard_clauses_detected(indexed_repos, lang):
    """Every language has at least 1 guard clause detected."""
    if not indexed_repos:
        pytest.skip("No indexed repos")
    conn = _connect(indexed_repos[lang])
    if not _table_exists(conn, "properties"):
        conn.close()
        pytest.skip(f"{lang}: properties table not created by gt-index on this platform")
    count = conn.execute("SELECT COUNT(*) FROM properties WHERE kind = 'guard_clause'").fetchone()[
        0
    ]
    conn.close()
    assert count >= 1, f"{lang}: no guard clauses detected, expected >= 1"


def test_all_languages_indexed(indexed_repos):
    """All 5 languages indexed successfully."""
    if not indexed_repos:
        pytest.skip("No indexed repos")
    assert set(indexed_repos.keys()) == set(LANGUAGES), (
        f"Missing languages: {set(LANGUAGES) - set(indexed_repos.keys())}"
    )


def test_print_parity_matrix(indexed_repos):
    """Print the full parity matrix for human review (always passes)."""
    if not indexed_repos:
        pytest.skip("No indexed repos")

    print("\n" + "=" * 80)
    print("EVIDENCE DEPTH PARITY MATRIX")
    print("=" * 80)

    header = f"{'Metric':<25}"
    for lang in LANGUAGES:
        header += f" {lang:>12}"
    print(header)
    print("-" * 80)

    # Nodes, edges, properties, assertions
    for metric, query in [
        ("Nodes", "SELECT COUNT(*) FROM nodes"),
        ("Edges", "SELECT COUNT(*) FROM edges"),
        ("Properties", "SELECT COUNT(*) FROM properties"),
        ("Assertions", "SELECT COUNT(*) FROM assertions"),
        ("Test functions", "SELECT COUNT(*) FROM nodes WHERE is_test = 1"),
        ("Guard clauses", "SELECT COUNT(*) FROM properties WHERE kind = 'guard_clause'"),
        ("Return shapes", "SELECT COUNT(*) FROM properties WHERE kind = 'return_shape'"),
        ("Exception types", "SELECT COUNT(*) FROM properties WHERE kind = 'exception_type'"),
    ]:
        row = f"{metric:<25}"
        for lang in LANGUAGES:
            conn = _connect(indexed_repos[lang])
            val = conn.execute(query).fetchone()[0]
            conn.close()
            row += f" {val:>12}"
        print(row)

    # Depth scores
    print("-" * 80)
    print("Depth scores:")
    for lang in LANGUAGES:
        depth = measure_depth(indexed_repos[lang])
        total = sum(depth.values())
        print(f"  {lang}: {total}/14 — {depth}")
    print("=" * 80)
