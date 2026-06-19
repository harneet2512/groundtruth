"""L1 brief generation tests.

Covers the pre-task family brief produced by:

    benchmarks/swebench/gt_intel.py
        - extract_identifiers_from_issue(issue_text) -> list[str]
        - generate_enhanced_briefing(conn, root, ids, max_lines)
        - TAXONOMY_LABELS  (CALLER-BLIND-EDIT, HALLUCINATED-IMPORT, ...)
        - MIN_CONFIDENCE   (0.7 — edge-confidence floor)

and the L1 -> L2 fallback chain in:

    scripts/swebench/gt_track4_pre_run.py:compute_brief

The tests run against a synthetic 5-file graph.db built in-process, with the
canonical schema from CLAUDE.md (nodes + edges with confidence + resolution_method).
No Live-Lite-specific data, no real repo, no LLM, no network — the suite is
repo-agnostic and language-agnostic by construction.
"""
from __future__ import annotations

import importlib
import sqlite3
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# sys.path bootstrap: gt_intel.py and gt_track4_pre_run.py are both scripts
# living outside src/, so we add their dirs explicitly. Mirrors what
# gt_track4_pre_run does for its SWE-agent subprocess.
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[2]
for _sub in ("src", "scripts/swebench", "benchmarks/swebench"):
    _p = _REPO_ROOT / _sub
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# Imported after sys.path bootstrap.
import gt_intel  # type: ignore[import-not-found]  # noqa: E402
import gt_track4_pre_run  # type: ignore[import-not-found]  # noqa: E402


# ---------------------------------------------------------------------------
# Synthetic 5-file graph.db fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def synthetic_graph_db(tmp_path: Path) -> str:
    """Build a 5-file synthetic graph.db with the canonical Go-indexer schema.

    Layout:
        src/urls.py         parse_url, normalize_host
        src/utils.py        validate_input, format_value
        src/net.py          Network class, Network.add (Method)
        src/render.py       render, build_page
        tests/test_urls.py  test_parse_url   (is_test=1)

    Cross-file edges (CALLS, all admissible: import / same_file):
        render            -> parse_url        confidence 1.00 (import)
        render            -> validate_input   confidence 1.00 (import)
        Network.add       -> parse_url        confidence 0.90 (import)
        Network.add       -> normalize_host   confidence 1.00 (same_file? cross-file impossible -- use import)
        test_parse_url    -> parse_url        confidence 0.95 (import)
        build_page        -> render           confidence 1.00 (same_file)
        format_value      -> validate_input   confidence 0.20 (name_match -- BELOW MIN_CONFIDENCE=0.7)

    The last edge is a deliberate poison pill: it must be filtered out by
    the MIN_CONFIDENCE gate when callers/tests/sibling queries run.
    """
    db_path = tmp_path / "synthetic_graph.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL,
            name TEXT NOT NULL,
            qualified_name TEXT,
            file_path TEXT NOT NULL,
            start_line INTEGER,
            end_line INTEGER,
            signature TEXT,
            return_type TEXT,
            is_exported BOOLEAN DEFAULT 0,
            is_test BOOLEAN DEFAULT 0,
            language TEXT NOT NULL,
            parent_id INTEGER REFERENCES nodes(id)
        );
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id INTEGER NOT NULL,
            target_id INTEGER NOT NULL,
            type TEXT NOT NULL,
            source_line INTEGER,
            source_file TEXT,
            resolution_method TEXT,
            confidence REAL DEFAULT 0.0,
            metadata TEXT
        );
        """
    )
    nodes = [
        # id, label,   name,             qualified_name,            file,                start,end, sig,                          ret,    exp,test,lang,    parent
        (1, "Function", "parse_url",      "urls.parse_url",          "src/urls.py",       10, 30,  "def parse_url(s)",            "str",   1, 0, "python", None),
        (2, "Function", "normalize_host", "urls.normalize_host",     "src/urls.py",       40, 60,  "def normalize_host(h)",       "str",   1, 0, "python", None),
        (3, "Function", "validate_input", "utils.validate_input",    "src/utils.py",       5, 20,  "def validate_input(x)",       "bool",  1, 0, "python", None),
        (4, "Function", "format_value",   "utils.format_value",      "src/utils.py",      30, 50,  "def format_value(x)",         "str",   1, 0, "python", None),
        (5, "Class",    "Network",        "net.Network",             "src/net.py",         1, 100, None,                          None,    1, 0, "python", None),
        (6, "Method",   "add",            "net.Network.add",         "src/net.py",        50, 70,  "def add(self, host)",         "None",  1, 0, "python", 5),
        (7, "Function", "render",         "render.render",           "src/render.py",      1, 30,  "def render(t)",               "str",   1, 0, "python", None),
        (8, "Function", "build_page",     "render.build_page",       "src/render.py",     35, 70,  "def build_page(items)",       "str",   1, 0, "python", None),
        (9, "Function", "test_parse_url", "tests.test_parse_url",    "tests/test_urls.py", 1, 15,  None,                          None,    0, 1, "python", None),
    ]
    conn.executemany(
        "INSERT INTO nodes (id, label, name, qualified_name, file_path, "
        "start_line, end_line, signature, return_type, is_exported, "
        "is_test, language, parent_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        nodes,
    )
    # (source_id, target_id, source_line, source_file, resolution_method, confidence)
    edges = [
        (7, 1, 12, "src/render.py",       "import",     1.00),  # render -> parse_url
        (7, 3, 14, "src/render.py",       "import",     1.00),  # render -> validate_input
        (6, 1, 55, "src/net.py",          "import",     0.90),  # Network.add -> parse_url
        (6, 2, 60, "src/net.py",          "import",     1.00),  # Network.add -> normalize_host
        (9, 1,  5, "tests/test_urls.py",  "import",     0.95),  # test_parse_url -> parse_url
        (8, 7, 40, "src/render.py",       "same_file",  1.00),  # build_page -> render
        # Poison pill: BELOW MIN_CONFIDENCE — must be filtered in caller/test queries.
        (4, 3, 35, "src/utils.py",        "name_match", 0.20),  # format_value -> validate_input
    ]
    conn.executemany(
        "INSERT INTO edges (source_id, target_id, type, source_line, "
        "source_file, resolution_method, confidence) VALUES "
        "(?, ?, 'CALLS', ?, ?, ?, ?)",
        edges,
    )
    conn.commit()
    conn.close()
    return str(db_path)


# ---------------------------------------------------------------------------
# extract_identifiers_from_issue
# ---------------------------------------------------------------------------

class TestExtractIdentifiers:
    def test_extract_zero_ids(self) -> None:
        """Pure prose with no code-shaped tokens -> 0 identifiers."""
        ids = gt_intel.extract_identifiers_from_issue(
            "the layout breaks on small screens"
        )
        assert ids == [], (
            f"expected zero IDs from pure prose, got {ids!r}"
        )

    def test_extract_one_id(self) -> None:
        """A single snake_case identifier in plain text -> >=1 ID containing it."""
        ids = gt_intel.extract_identifiers_from_issue(
            "Bug in set_snapshots when called twice."
        )
        assert len(ids) >= 1, f"expected >=1 ID, got {ids!r}"
        assert "set_snapshots" in ids, f"missing set_snapshots in {ids!r}"

    def test_extract_three_ids(self) -> None:
        """Three identifiers (snake_case + dotted + snake_case) -> all three present."""
        ids = gt_intel.extract_identifiers_from_issue(
            "parse_url crashes; Network.add ignores port; validate_input rejects valid input."
        )
        assert len(ids) >= 3, f"expected >=3 IDs, got {ids!r}"
        for needle in ("parse_url", "Network.add", "validate_input"):
            assert needle in ids, f"missing {needle!r} in {ids!r}"

    def test_extract_multilang(self) -> None:
        """Multi-language identifiers: Go-style CamelCase, Rust mod::path, Python snake_case.

        gt_intel's extractor relies on regex shape, not language. Required
        invariants:
          - Go-style 2-hump CamelCase (HandleRequest) is extracted.
          - Rust mod::parse_url surfaces parse_url (the snake_case tail).
          - Python snake_case (do_thing) is extracted.
          - Case is preserved (CamelCase stays CamelCase).
        """
        ids = gt_intel.extract_identifiers_from_issue(
            "Go HandleRequest panics; Rust mod::parse_url returns Err; "
            "Python do_thing raises."
        )
        assert "HandleRequest" in ids, f"Go CamelCase missing in {ids!r}"
        assert "parse_url" in ids, f"Rust snake_case tail missing in {ids!r}"
        assert "do_thing" in ids, f"Python snake_case missing in {ids!r}"
        # Case preservation:
        assert "handlerequest" not in ids, (
            f"case was lowered for Go ident in {ids!r}"
        )


# ---------------------------------------------------------------------------
# generate_enhanced_briefing  (against the synthetic graph.db)
# ---------------------------------------------------------------------------

class TestEnhancedBriefing:
    def test_brief_structure(self, synthetic_graph_db: str) -> None:
        """3-ID issue + synthetic graph -> non-empty brief tagged with >=1 family label.

        Validates the family-tagged structure produced by
        generate_enhanced_briefing: tier framing ([VERIFIED]/[LIKELY]/[POSSIBLE])
        plus per-family TAXONOMY_LABELS.
        """
        ids = gt_intel.extract_identifiers_from_issue(
            "`parse_url` is broken; `Network.add` ignores port; "
            "`validate_input` returns wrong bool."
        )
        assert len(ids) >= 3, f"setup precondition failed: ids={ids!r}"

        conn = sqlite3.connect(synthetic_graph_db)
        try:
            out = gt_intel.generate_enhanced_briefing(
                conn, str(_REPO_ROOT), ids, max_lines=8,
            )
        finally:
            conn.close()

        assert out, "brief was empty"
        assert "<gt-evidence>" in out, (
            f"missing <gt-evidence> wrapper in:\n{out}"
        )
        labels = list(gt_intel.TAXONOMY_LABELS.values())
        present = [lbl for lbl in labels if lbl in out]
        assert present, (
            f"no TAXONOMY_LABELS tag found.\n"
            f"expected one of {labels}\n"
            f"got brief:\n{out}"
        )

    def test_brief_zero_id_falls_back_to_orientation(self, synthetic_graph_db: str) -> None:
        """0-ID issue -> always-fire codebase orientation, not empty/[OK].

        Current contract (gt_intel.generate_pretask_briefing v14 fallback 2,
        gt_intel.py:1142-1167): with no identifiers, the brief falls through to
        the top-entry-points fallback and emits a "CODEBASE CONTEXT:" orientation
        with ENTRY POINT bullets. It still carries NO per-task taxonomy family
        labels (it is orientation, not symbol-specific evidence). The bare
        "[OK] No symbols matched" line only fires when the graph has zero usable
        CALLS edges; with a connected graph the orientation always fires.
        """
        conn = sqlite3.connect(synthetic_graph_db)
        try:
            out = gt_intel.generate_enhanced_briefing(
                conn, str(_REPO_ROOT), [], max_lines=8,
            )
        finally:
            conn.close()

        assert "<gt-evidence>" in out, f"missing wrapper:\n{out}"
        labels_present = [
            lbl for lbl in gt_intel.TAXONOMY_LABELS.values() if lbl in out
        ]
        assert not labels_present, (
            f"zero-ID brief should not contain per-task family labels, found: "
            f"{labels_present}\nbrief:\n{out}"
        )
        # Always-fire orientation: codebase-context entry points, not empty/[OK].
        assert "CODEBASE CONTEXT" in out and "ENTRY POINT" in out, (
            f"zero-ID brief should fall back to entry-point orientation:\n{out}"
        )
        # Negative control: it must NOT claim a specific FIX HERE symbol target,
        # since no identifier was supplied to localize to.
        assert "FIX HERE" not in out, (
            f"zero-ID orientation must not assert a localized fix target:\n{out}"
        )


# ---------------------------------------------------------------------------
# MIN_CONFIDENCE filtering
# ---------------------------------------------------------------------------

class TestConfidenceFilter:
    def test_min_confidence_constant(self) -> None:
        """Pin the documented threshold so a silent change is caught."""
        assert gt_intel.MIN_CONFIDENCE == 0.7, (
            f"MIN_CONFIDENCE drifted from 0.7 to {gt_intel.MIN_CONFIDENCE}"
        )

    def test_low_confidence_edge_filtered(self, synthetic_graph_db: str) -> None:
        """The 0.20-confidence (format_value -> validate_input) edge must be
        excluded from cross-file caller queries."""
        conn = sqlite3.connect(synthetic_graph_db)
        try:
            # validate_input is node id=3
            callers = gt_intel.get_callers(conn, target_id=3, target_file="src/utils.py")
        finally:
            conn.close()

        caller_names = [c[0].name for c in callers]
        # render (cross-file, conf=1.0) must be present.
        assert "render" in caller_names, (
            f"high-confidence cross-file caller missing: {caller_names!r}"
        )
        # format_value (same-file caller, conf=0.20) must NOT be present:
        # filtered out by MIN_CONFIDENCE >= 0.7.
        # NOTE: get_callers also filters on source_file != target_file, so
        # this is a belt-and-suspenders check on the confidence gate.
        assert "format_value" not in caller_names, (
            f"low-confidence (0.20) edge leaked past MIN_CONFIDENCE filter: "
            f"{caller_names!r}"
        )


# ---------------------------------------------------------------------------
# compute_brief routing (L1 -> L2)
# ---------------------------------------------------------------------------

class TestComputeBriefRouting:
    """Verify which layer fires given an issue's identifier extraction outcome.

    compute_brief is the dispatcher; it calls L1 only when ids are non-empty
    AND L1 produced non-empty output, else L2. We patch the lazy importers
    so we control both branches without touching real fixtures.
    """

    def test_zero_id_routes_to_l2(self, monkeypatch, tmp_path: Path) -> None:
        """0-ID issue -> L1 status is 'empty', L2 fires."""
        l1_call_count = {"n": 0}
        l2_call_count = {"n": 0}

        def fake_extract(_text: str) -> list[str]:
            return []  # zero ids — must short-circuit before L1 brief call.

        def fake_briefing(_conn, _root, _ids, max_lines: int = 8) -> str:
            l1_call_count["n"] += 1
            return "[VERIFIED] FIX HERE: should-not-fire"

        def fake_l2(_issue: str, _repo: str, _db: str) -> str:
            l2_call_count["n"] += 1
            return "<gt-task-brief>\nL2 STUB\n</gt-task-brief>"

        monkeypatch.setattr(
            gt_track4_pre_run, "_import_gt_intel",
            lambda: (fake_extract, fake_briefing),
        )
        monkeypatch.setattr(
            gt_track4_pre_run, "_import_l2_fallback",
            lambda: fake_l2,
        )

        # graph_db_path doesn't need to exist for the zero-ID branch
        # (L1 short-circuits on empty ids before opening the DB).
        brief, l1_status, l2_status = gt_track4_pre_run.compute_brief(
            issue_text="the layout breaks on small screens",
            repo_path=str(tmp_path),
            graph_db_path=str(tmp_path / "missing.db"),
        )

        assert l1_call_count["n"] == 0, (
            "L1 briefing was invoked despite zero IDs"
        )
        assert l2_call_count["n"] == 1, (
            f"L2 fallback should fire exactly once, got {l2_call_count['n']}"
        )
        assert l1_status == "empty", f"l1_status={l1_status!r}"
        assert l2_status == "fired", f"l2_status={l2_status!r}"
        assert "L2 STUB" in brief, f"brief missing L2 content: {brief!r}"

    def test_nonzero_id_with_l1_hit_skips_l2(
        self, monkeypatch, synthetic_graph_db: str, tmp_path: Path,
    ) -> None:
        """Non-empty IDs + L1 produces output -> L2 is a no-op."""
        l1_call_count = {"n": 0}
        l2_call_count = {"n": 0}

        def fake_extract(_text: str) -> list[str]:
            return ["parse_url", "validate_input"]

        def fake_briefing(_conn, _root, ids, max_lines: int = 8) -> str:
            l1_call_count["n"] += 1
            assert ids, "compute_brief must pass non-empty ids to L1"
            return "<gt-evidence>\n[VERIFIED] FIX HERE: parse_url\n</gt-evidence>"

        def fake_l2(_issue: str, _repo: str, _db: str) -> str:
            l2_call_count["n"] += 1
            return "<gt-task-brief>\nshould-not-fire\n</gt-task-brief>"

        monkeypatch.setattr(
            gt_track4_pre_run, "_import_gt_intel",
            lambda: (fake_extract, fake_briefing),
        )
        monkeypatch.setattr(
            gt_track4_pre_run, "_import_l2_fallback",
            lambda: fake_l2,
        )

        brief, l1_status, l2_status = gt_track4_pre_run.compute_brief(
            issue_text="parse_url is broken; validate_input rejects.",
            repo_path=str(tmp_path),
            graph_db_path=synthetic_graph_db,
        )

        assert l1_call_count["n"] == 1, (
            f"L1 must fire exactly once when ids present, got {l1_call_count['n']}"
        )
        assert l2_call_count["n"] == 0, (
            f"L2 must NOT fire when L1 already produced output, "
            f"got {l2_call_count['n']}"
        )
        assert l1_status == "fired", f"l1_status={l1_status!r}"
        assert l2_status == "noop", f"l2_status={l2_status!r}"
        assert "FIX HERE: parse_url" in brief
