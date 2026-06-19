"""Stage-1 deterministic tests for the 2026-06-10 rerank/localization fixes.

Three GENERALIZED defects in the §4/§4.2 rerank stack (PATH B Tier-3b audit,
run 27260307167 — all three root-caused RERANK_LOGIC with the substrate green):

  FIX 1 — anchor extraction dropped dotted/backtick code symbols
          (``Class.method`` never survives the bare-name cross-check; its
          components die as homonyms; W_CODE_DEF never engages).
  FIX 2 — candidate-union recall guarantee excluded class-like definitions and
          shape-skipped reporter-confirmed short names (an issue whose TITLE
          names the defective class verbatim never earned the guarantee).
  FIX 3 — fusion mis-ordered under a FLAT dense signal (sem MAD ~ 0 at the
          fusion input: all-equal cosines or 1-of-N coverage arbitrarily boosts
          whichever file carries coverage; the gate floors W_SEM and leans on
          content/anchor-structural signals).

Every test is keyed to issue STRUCTURE (backtick/dotted symbols,
exact-anchor-defines-file, flat dispersion) on synthetic fixtures — no task
IDs, no gold labels, no benchmark names, no real-repo symbols.
"""
from __future__ import annotations

import sqlite3
import textwrap
from pathlib import Path

import numpy as np
import pytest


# ------------------------------------------------------------------ fixtures


_SCHEMA = """
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


@pytest.fixture
def qualified_db(tmp_path: Path) -> str:
    """Graph with a class + parented method (the qualified-anchor shape) and a
    same-named decoy method in another file (the ambiguity the qualification
    must resolve)."""
    db = tmp_path / "graph.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(_SCHEMA)
    nodes = [
        # id, label, name, qualified_name, file, parent_id
        (1, "Class", "PaintCanvas", "gui.canvas.PaintCanvas", "gui/canvas.py", None),
        (2, "Method", "_redraw_axes", "gui.canvas.PaintCanvas._redraw_axes", "gui/canvas.py", 1),
        # decoy: same bare method name, different class/file — the 1/n dilution trap
        (3, "Class", "PlotPanel", "gui.panel.PlotPanel", "gui/panel.py", None),
        (4, "Method", "_redraw_axes", "gui.panel.PlotPanel._redraw_axes", "gui/panel.py", 3),
        (5, "Function", "render_helper", None, "gui/helpers.py", None),
    ]
    conn.executemany(
        "INSERT INTO nodes (id, label, name, qualified_name, file_path, "
        "start_line, end_line, signature, return_type, is_exported, is_test, "
        "language, parent_id) VALUES (?,?,?,?,?,1,10,NULL,NULL,1,0,'python',?)",
        nodes,
    )
    conn.executemany(
        "INSERT INTO edges (source_id, target_id, type, source_line, source_file, "
        "resolution_method, confidence) VALUES (?,?,?,?,?,?,?)",
        [(5, 2, "CALLS", 3, "gui/helpers.py", "import", 1.0)],
    )
    conn.commit()
    conn.close()
    return str(db)


@pytest.fixture
def class_anchor_db(tmp_path: Path) -> str:
    """Graph where the issue-titled symbol is a CLASS defined in exactly one
    file, plus keyword-heavy decoy files (the recall-guarantee shape)."""
    db = tmp_path / "graph.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(_SCHEMA)
    nodes = [
        (1, "Class", "MetricGrid", "io.grid.MetricGrid", "io/grid.py", None),
        (2, "Method", "write", "io.grid.MetricGrid.write", "io/grid.py", 1),
        (3, "Class", "Grid", "io.tiny.Grid", "io/tiny.py", None),  # short class name
        (4, "Function", "format_table", None, "core/table.py", None),
        (5, "Function", "format_output", None, "core/output.py", None),
        (6, "Function", "decimal_repr", None, "core/repr.py", None),
        # an ambiguous name spread across >3 files must NOT earn the guarantee
        (7, "Function", "spread_sym", None, "a/one.py", None),
        (8, "Function", "spread_sym", None, "a/two.py", None),
        (9, "Function", "spread_sym", None, "a/three.py", None),
        (10, "Function", "spread_sym", None, "a/four.py", None),
    ]
    conn.executemany(
        "INSERT INTO nodes (id, label, name, qualified_name, file_path, "
        "start_line, end_line, signature, return_type, is_exported, is_test, "
        "language, parent_id) VALUES (?,?,?,?,?,1,10,NULL,NULL,1,0,'python',?)",
        nodes,
    )
    conn.commit()
    conn.close()
    return str(db)


@pytest.fixture
def class_anchor_repo(tmp_path: Path) -> str:
    """Real files matching class_anchor_db; decoys carry the issue's prose
    keywords so lexical retrieval favors them (the burying shape)."""
    (tmp_path / "io").mkdir()
    (tmp_path / "core").mkdir()
    (tmp_path / "a").mkdir()
    (tmp_path / "io" / "grid.py").write_text(
        textwrap.dedent(
            """
            class MetricGrid:
                def write(self, rows):
                    return rows
            """
        )
    )
    (tmp_path / "io" / "tiny.py").write_text("class Grid:\n    pass\n")
    (tmp_path / "core" / "table.py").write_text(
        "# format table output decimal places supported formats output\n"
        "def format_table(rows):\n    return rows\n"
    )
    (tmp_path / "core" / "output.py").write_text(
        "# output formats decimal supported output formats decimal\n"
        "def format_output(rows):\n    return rows\n"
    )
    (tmp_path / "core" / "repr.py").write_text(
        "# decimal repr output formats decimal output\n"
        "def decimal_repr(x):\n    return x\n"
    )
    for n in ("one", "two", "three", "four"):
        (tmp_path / "a" / f"{n}.py").write_text("def spread_sym():\n    pass\n")
    return str(tmp_path)


# ============================================================== FIX 1 tests


def test_dotted_backtick_anchor_survives_extraction(qualified_db: str) -> None:
    """RED (pre-fix): the dotted token dies at the bare-name cross-check
    (nodes store bare names) — `PaintCanvas._redraw_axes` absent from symbols.
    GREEN: the graph confirms the qualified pair (parent-child), the dotted
    token survives into symbols AND code_symbols (it is backtick-wrapped)."""
    from groundtruth.pretask.anchors import extract_issue_anchors

    issue = (
        "Axes redrawn twice on resize\n\n"
        "The defective code is in `PaintCanvas._redraw_axes`. Resizing the\n"
        "window repaints every axis twice instead of once.\n"
    )
    anchors = extract_issue_anchors(issue, qualified_db)
    assert "PaintCanvas._redraw_axes" in anchors.symbols, (
        f"qualified dotted anchor dropped: {sorted(anchors.symbols)}"
    )
    assert "PaintCanvas._redraw_axes" in anchors.code_symbols


def test_unconfirmed_dotted_anchor_stays_dropped(qualified_db: str) -> None:
    """Correct-or-quiet: a dotted token the graph CANNOT confirm (no such
    class/method pair) is NOT minted from string shape alone."""
    from groundtruth.pretask.anchors import extract_issue_anchors

    issue = "Crash inside `GhostWidget.never_exists` on startup.\n"
    anchors = extract_issue_anchors(issue, qualified_db)
    assert "GhostWidget.never_exists" not in anchors.symbols


def test_dotted_symbol_resolves_to_defining_file_full_confidence(
    qualified_db: str,
) -> None:
    """RED (pre-fix): `_compute_code_symbol_scores` looked up the bare tail →
    1/n split across BOTH files defining `_redraw_axes` (0.5 / 0.5).
    GREEN: the qualified pair resolves to the ONE file whose class the
    reporter named → that file scores 1.0 and the decoy gets nothing."""
    from groundtruth.pretask.anchors import IssueAnchors
    from groundtruth.pretask.v7_4_brief import _compute_code_symbol_scores

    ia = IssueAnchors(code_symbols={"PaintCanvas._redraw_axes"})
    scores = _compute_code_symbol_scores(ia, qualified_db)
    assert scores.get("gui/canvas.py") == 1.0, f"got {scores}"
    assert "gui/panel.py" not in scores, (
        f"qualified resolution must not dilute across same-named methods: {scores}"
    )


def test_dotted_symbol_containing_class_fallback(qualified_db: str) -> None:
    """A dotted tail with NO own node (inherited/dynamic attr) resolves to the
    CONTAINING class's defining file (the §4 prescription: ``Foo.bar`` -> the
    file defining ``Foo``)."""
    from groundtruth.pretask.anchors import IssueAnchors
    from groundtruth.pretask.v7_4_brief import _compute_code_symbol_scores

    ia = IssueAnchors(code_symbols={"PaintCanvas.background_color"})
    scores = _compute_code_symbol_scores(ia, qualified_db)
    assert scores.get("gui/canvas.py") == 1.0, f"got {scores}"


def test_localizer_seed_rows_resolve_qualified_anchor(qualified_db: str) -> None:
    """RED (pre-fix): a dotted anchor seeds NOTHING in the localizer (bare-name
    IN(...) never matches). GREEN: it seeds the real method node."""
    from groundtruth.pretask.graph_localizer import _seed_node_rows

    conn = sqlite3.connect(qualified_db)
    try:
        rows = _seed_node_rows(conn, {"PaintCanvas._redraw_axes"})
    finally:
        conn.close()
    assert any(fp == "gui/canvas.py" and name == "_redraw_axes" for _, name, fp in rows), (
        f"qualified anchor did not seed its definition node: {rows}"
    )
    # and it must NOT seed the same-named decoy in the other class
    assert not any(fp == "gui/panel.py" for _, _, fp in rows)


# ============================================================== FIX 2 tests


def test_exact_issue_named_files_includes_class_definitions(
    class_anchor_db: str,
) -> None:
    """RED (pre-fix): label filter was Function/Method only — a class named
    verbatim in the issue never earned the guarantee. GREEN: the class's
    defining file is returned."""
    from groundtruth.pretask.v1r_brief import _exact_issue_named_files

    issue = "MetricGrid drops decimal places in supported formats output\n"
    named = _exact_issue_named_files(issue, class_anchor_db)
    assert "io/grid.py" in named, f"class definition missing from guarantee: {named}"
    assert "MetricGrid" in named["io/grid.py"]


def test_exact_issue_named_files_short_name_needs_provenance(
    class_anchor_db: str,
) -> None:
    """The short-name shape skip is bypassed ONLY by reporter-confirmed
    provenance (title/backtick) — never unconditionally."""
    from groundtruth.pretask.anchors import IssueAnchors
    from groundtruth.pretask.v1r_brief import _exact_issue_named_files

    issue = "Grid output has wrong decimal formats\n"
    # without provenance: short name (len<5, no underscore) stays skipped
    named_no_prov = _exact_issue_named_files(issue, class_anchor_db)
    assert "io/tiny.py" not in named_no_prov
    # with title provenance: admitted (still unique-definition + non-generic)
    ia = IssueAnchors(title_symbols={"Grid"}, code_symbols={"Grid"})
    named_prov = _exact_issue_named_files(issue, class_anchor_db, issue_anchors=ia)
    assert "io/tiny.py" in named_prov, f"provenance-confirmed short class missing: {named_prov}"


def test_exact_issue_named_files_ambiguity_gate_retained(
    class_anchor_db: str,
) -> None:
    """A name defined in MORE than 3 files is generic — still NO guarantee
    (the confidence gate the fix must not loosen)."""
    from groundtruth.pretask.v1r_brief import _exact_issue_named_files

    issue = "spread_sym misbehaves in MetricGrid formats\n"
    named = _exact_issue_named_files(issue, class_anchor_db)
    spread_files = [f for f, syms in named.items() if "spread_sym" in syms]
    assert spread_files == [], f"ambiguous name earned the guarantee: {named}"


def test_generate_v1r_brief_renders_titled_class_file(
    class_anchor_db: str, class_anchor_repo: str, monkeypatch
) -> None:
    """End-to-end on the LIVE surface (generate_v1r_brief, per BRIEFING §0 —
    never localize() in isolation): an issue whose TITLE names a class defined
    in exactly one file MUST render that file among .files, even when lexical
    retrieval favors keyword-heavy decoys. RED (pre-fix): the class label was
    outside the guarantee and the file could miss every slot."""
    from groundtruth.pretask import v1r_brief as _v1r

    result = _v1r.generate_v1r_brief(
        "MetricGrid drops decimal places in supported formats output\n\n"
        "Writing rows produces wrong decimal output formats; the supported\n"
        "formats table shows decimal output truncated.",
        class_anchor_repo,
        class_anchor_db,
        bug_id="unit-fix2",
        repo="unit",
    )
    norm = [
        str(getattr(f, "path", f)).replace("\\", "/").lstrip("./")
        for f in result.files
    ]
    assert any(f.endswith("io/grid.py") for f in norm), (
        f"titled-class defining file absent from the rendered brief: {norm}"
    )


# ============================================================== FIX 3 tests


def test_dispersion_gate_fires_on_one_of_n_coverage() -> None:
    """1-of-N dense coverage (the live shape: sem_mad=0.00000000 with
    pred_2_coverage=False) → MAD 0 → gate fires: W_SEM led to the floor,
    content/anchor signals led up, NOTHING lowered except W_SEM."""
    from groundtruth.pretask.v7_4_brief import (
        DEFAULT_WEIGHTS,
        _apply_dense_dispersion_gate,
        _w_sem_floor,
    )

    files = ["a.py", "b.py", "c.py", "d.py", "e.py"]
    sem = {"a.py": 0.81234567}  # only one candidate carries any cosine
    w, fired, mad = _apply_dense_dispersion_gate(dict(DEFAULT_WEIGHTS), sem, files)
    assert fired is True
    assert mad == 0.0
    assert w["W_SEM"] == _w_sem_floor()
    assert w["W_SEM"] > 0.0  # §11.6: floored, never zeroed
    assert w["W_CODE_DEF"] >= 0.70
    assert w["W_FRAME"] >= 0.60
    assert w["W_LEX"] >= 0.55
    assert w["W_PATH"] >= 0.50
    # max-compose: no non-dense weight may be LOWERED by the gate
    for k, v in DEFAULT_WEIGHTS.items():
        if k != "W_SEM":
            assert w[k] >= v, f"{k} lowered by the gate: {v} -> {w[k]}"
    # reach is deliberately NOT raised (reach over-promotes hubs — BRIEFING §3)
    assert w["W_REACH"] == DEFAULT_WEIGHTS["W_REACH"]


def test_dispersion_gate_fires_on_all_equal_sem() -> None:
    """All-equal cosines (the sibling-vocabulary collapse) → MAD 0 → fires."""
    from groundtruth.pretask.v7_4_brief import (
        DEFAULT_WEIGHTS,
        _apply_dense_dispersion_gate,
    )

    files = ["a.py", "b.py", "c.py", "d.py"]
    sem = {fp: 0.83886000 for fp in files}
    _, fired, _ = _apply_dense_dispersion_gate(dict(DEFAULT_WEIGHTS), sem, files)
    assert fired is True


def test_dispersion_gate_noop_on_healthy_dispersion() -> None:
    """Healthy spread (relative MAD above the scale-free floor) → weights
    byte-identical (the exact no-regression property)."""
    from groundtruth.pretask.v7_4_brief import (
        DEFAULT_WEIGHTS,
        _apply_dense_dispersion_gate,
    )

    files = ["a.py", "b.py", "c.py", "d.py", "e.py"]
    sem = {"a.py": 0.91, "b.py": 0.62, "c.py": 0.44, "d.py": 0.21, "e.py": 0.08}
    base = dict(DEFAULT_WEIGHTS)
    w, fired, mad = _apply_dense_dispersion_gate(base, sem, files)
    assert fired is False
    assert mad > 0.0
    assert w == DEFAULT_WEIGHTS


def test_dispersion_gate_orders_anchor_file_above_arbitrary_sem_file() -> None:
    """The ordering consequence: under a flat-sparse dense signal, a candidate
    whose only edge is an arbitrary dense boost must NOT out-rank the candidate
    the issue's anchor DEFINES. RED (pre-fix, no gate): W_SEM=0.40 * 0.8 beats
    the anchor file. GREEN: gated weights put the anchor-defined file first."""
    from groundtruth.pretask.v7_4_brief import (
        DEFAULT_WEIGHTS,
        _apply_dense_dispersion_gate,
        _total_score,
    )

    files = ["anchor_defined.py", "sem_lucky.py", "x.py", "y.py", "z.py"]
    sem = {"sem_lucky.py": 0.80}  # 1-of-N coverage — flat at the fusion
    anchor_comps = {"sem": 0.0, "lex": 0.30, "code_def": 1.0}
    lucky_comps = {"sem": 0.80, "lex": 0.30, "code_def": 0.0}

    # Dim-1 engages W_CODE_DEF when code-def signal exists — mirror that here so
    # the test isolates the GATE's contribution, not Dim-1's.
    pre = dict(DEFAULT_WEIGHTS)
    pre["W_CODE_DEF"] = 0.70

    gated, fired, _ = _apply_dense_dispersion_gate(dict(pre), sem, files)
    assert fired is True
    assert _total_score(anchor_comps, gated) > _total_score(lucky_comps, gated), (
        "anchor-defined file must out-rank the arbitrarily-dense-boosted file "
        "under a flat dense signal"
    )


class _FlatEmbedModel:
    """Embedder returning the SAME unit vector for every text → every cosine
    identical (the flat-at-fusion shape) regardless of file content."""

    def encode(self, texts, **kw):
        texts = list(texts)
        v = np.zeros((len(texts), 384), dtype=np.float32)
        v[:, 0] = 1.0
        return v


def test_run_v74_flat_dense_fires_gate_and_records_telemetry(
    class_anchor_db: str, class_anchor_repo: str, monkeypatch
) -> None:
    """Integration (run_v74, live C path): with a real-but-flat embedder the
    gate fires, W_SEM lands on the floor in the APPLIED weights, and the
    telemetry fields record it (8-dp deep-log rule)."""
    from groundtruth.pretask import anchor_select as _as
    from groundtruth.pretask import v7_4_brief as _b

    # Clear BOTH semantic caches: the per-graph matrix cache AND the content-
    # addressed per-symbol vector cache (it survives across graphs/runs and would
    # shadow the fake flat model with a prior test's real-model vectors).
    _as._EMBED_CACHE.clear()
    try:
        _as._SYMVEC_CACHE.clear()
    except Exception:
        pass
    monkeypatch.setattr(_b, "_get_model", lambda: _FlatEmbedModel())
    monkeypatch.setattr(_b, "_SEMANTIC_AVAILABLE", True)

    result = _b.run_v74(
        issue_text=(
            "MetricGrid drops decimal places in supported formats output. "
            "Writing rows produces wrong decimal output formats."
        ),
        repo_root=class_anchor_repo,
        graph_db=class_anchor_db,
        ablation="C",
    )
    assert result.sem_flat_gate_fired is True
    assert result.sem_dispersion_mad == 0.0
    assert result.hyperparameters["W_SEM"] == _b._w_sem_floor()
    assert result.effective_w_sem == _b._w_sem_floor()  # floored, never zeroed
    _as._EMBED_CACHE.clear()
