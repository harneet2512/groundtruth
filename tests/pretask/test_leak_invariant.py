"""Leak invariant — item D of the cooperative-localization build (offline gate).

WHAT THIS GUARDS
----------------
GroundTruth is handed the checked-out tree (``repo_root``) at brief time. On the
eval harness that tree is the GOLD-patched + test-patched checkout — it literally
contains the answer. The invariant: **GT's emitted bytes must be a function of the
INDEXED graph + issue ONLY, never of the gold/test content that happens to sit on
disk.** Two independent leak vectors, two guards:

  1. BYTE-IDENTITY across repo_root.  The SAME graph.db, rendered against
     (base checkout) vs (base + gold_patch + test_patch), must be byte-for-byte
     identical. If any on-disk read (grep-spine recall, caller call-site line,
     B2 body terms) let the fixed body / new test file change the output, the
     bytes diverge → GT is reading the answer.

  2. NO TEST / GOLD ARTIFACT in the bytes.  The delivered brief + localize output
     must contain no test file path, no test function name, and no gold/test
     sentinel token. Surfacing the FAIL_TO_PASS test is handing over the oracle.

MUTATION COMPANION
------------------
The load-bearing exclusion is the ``is_test`` graph flag (the seed SQL filters
``is_test = 0``; a correctly-flagged test node never reaches a delivered surface).
The mutation FLIPS that exclusion by mislabelling the test node ``is_test = 0``
(the real "walker missed the test flag" failure) so the test node IS allowed into
the delivered surface. Under the mutation guard #2 MUST go red — the test artifact
leaks. ``test_mutation_*`` proves it (and that the green assertion would raise),
so the invariant is not vacuous.

HERMETIC
--------
Synthetic graph.db + tmp checkouts, ≥2 languages (python, go). The embedder is
forced OFF (deterministic ``{}`` semantic) so the test needs no ONNX / torch and
is byte-stable: we are testing leakage, not ranking quality.
"""
from __future__ import annotations

import os
import sqlite3

import pytest

import groundtruth.pretask.graph_localizer as GL
import groundtruth.pretask.v7_4_brief as V74
from groundtruth.pretask.graph_localizer import localize
from groundtruth.pretask.v1r_brief import generate_v1r_brief


# --- hermetic: force semantic OFF everywhere (deterministic, no ONNX/torch) ----
@pytest.fixture(autouse=True)
def _semantic_off(monkeypatch):
    """Neutralise both embedder halves so the pipeline is deterministic and
    env-independent. localize's ``_semantic_score_by_file`` returns ``{}`` when
    the embedder is None; run_v74 falls to the zero-embedding model (W_SEM→0)."""
    monkeypatch.setattr(GL, "_get_embedder", lambda: None)

    def _zero_model():
        try:
            return V74._ZeroEmbeddingModel()
        except Exception:  # pragma: no cover - trivial inline fallback
            class _Z:
                dim = 8

                def encode(self, texts, **_):
                    return [[0.0] * self.dim for _ in texts]

            return _Z()

    monkeypatch.setattr(V74, "_get_model", _zero_model)


# --- per-language fixture spec -------------------------------------------------
# Each spec: the gold (product) symbol, the two product files, and the TEST node
# whose name/path must never leak. ``sentinel`` tokens are injected ONLY into the
# gold checkout — if either appears in the emitted bytes, disk content leaked.
_SPECS = {
    "python": {
        "lang": "python",
        "gold_sym": "apply_defaults",
        "gold_file": "pkg/config.py",
        "caller_sym": "load_config",
        "caller_file": "pkg/loader.py",
        "test_sym": "test_apply_defaults",
        "test_file": "tests/test_config.py",
        # The gold/test patches differ ONLY by a non-issue-token sentinel comment.
        # A grep-recall change from a genuinely issue-relevant token is by design
        # (grep reflects content); the LEAK we isolate is GT rendering the gold/test
        # disk content itself — so the on-disk delta carries no issue vocabulary.
        "gold_base": "def apply_defaults(cfg):\n    return cfg\n",
        "gold_patched": "def apply_defaults(cfg):\n    return cfg  # GOLDPATCHSENTINEL\n",
        "caller_body": "def load_config():\n    return apply_defaults({})\n",
        "test_base": "def test_apply_defaults():\n    assert apply_defaults({}) is not None\n",
        "test_patched": "def test_apply_defaults():\n    assert apply_defaults({}) is not None  # F2PSENTINEL\n",
        "issue": "apply_defaults mutates the caller mapping; correct apply_defaults in config",
    },
    "go": {
        "lang": "go",
        "gold_sym": "ApplyDefaults",
        "gold_file": "pkg/config.go",
        "caller_sym": "LoadConfig",
        "caller_file": "pkg/loader.go",
        "test_sym": "TestApplyDefaults",
        "test_file": "pkg/config_test.go",
        "gold_base": "package pkg\nfunc ApplyDefaults(c Cfg) Cfg {\n    return c\n}\n",
        "gold_patched": "package pkg\nfunc ApplyDefaults(c Cfg) Cfg {\n    return c // GOLDPATCHSENTINEL\n}\n",
        "caller_body": "package pkg\nfunc LoadConfig() Cfg {\n    return ApplyDefaults(Cfg{})\n}\n",
        "test_base": "package pkg\nfunc TestApplyDefaults(t *testing.T) {\n    ApplyDefaults(Cfg{})\n}\n",
        "test_patched": "package pkg\nfunc TestApplyDefaults(t *testing.T) {\n    ApplyDefaults(Cfg{}) // F2PSENTINEL\n}\n",
        "issue": "ApplyDefaults mutates the caller mapping; correct ApplyDefaults in config",
    },
}

_SENTINELS = ("GOLDPATCHSENTINEL", "F2PSENTINEL")


def _build_graph(db: str, spec: dict, *, is_test_flag: int) -> None:
    """Graph: gold symbol + product caller + a TEST node calling gold.

    ``is_test_flag`` is the mutation knob: 1 = correctly flagged (excluded);
    0 = the walker mislabelled the test (exclusion flipped OFF → allowed in)."""
    con = sqlite3.connect(db)
    con.executescript(
        """CREATE TABLE nodes(id INTEGER PRIMARY KEY, label TEXT, name TEXT,
             qualified_name TEXT, file_path TEXT, start_line INTEGER, end_line INTEGER,
             signature TEXT, return_type TEXT, is_exported INTEGER, is_test INTEGER,
             language TEXT, parent_id INTEGER);
           CREATE TABLE edges(id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,
             type TEXT, source_line INTEGER, source_file TEXT, resolution_method TEXT,
             confidence REAL, metadata TEXT);"""
    )
    lang = spec["lang"]
    con.execute(
        "INSERT INTO nodes(id,label,name,file_path,start_line,end_line,signature,is_test,language)"
        " VALUES(1,'Function',?,?,1,3,?,0,?)",
        (spec["gold_sym"], spec["gold_file"], f"{spec['gold_sym']}(cfg)", lang),
    )
    con.execute(
        "INSERT INTO nodes(id,label,name,file_path,start_line,end_line,is_test,language)"
        " VALUES(2,'Function',?,?,1,3,0,?)",
        (spec["caller_sym"], spec["caller_file"], lang),
    )
    con.execute(
        "INSERT INTO edges(id,source_id,target_id,type,source_line,resolution_method,confidence)"
        " VALUES(1,2,1,'CALLS',2,'import',1.0)")
    con.execute(
        "INSERT INTO nodes(id,label,name,file_path,start_line,end_line,is_test,language)"
        " VALUES(3,'Function',?,?,1,3,?,?)",
        (spec["test_sym"], spec["test_file"], is_test_flag, lang),
    )
    con.execute(
        "INSERT INTO edges(id,source_id,target_id,type,source_line,resolution_method,confidence)"
        " VALUES(2,3,1,'CALLS',2,'import',1.0)")
    con.commit()
    con.close()


def _write_tree(root: str, spec: dict, variant: str) -> None:
    """``base`` = original bodies; ``gold`` = gold_patch + test_patch on disk."""
    for rel, key_base, key_gold in (
        (spec["gold_file"], "gold_base", "gold_patched"),
        (spec["test_file"], "test_base", "test_patched"),
    ):
        body = spec[key_gold] if variant == "gold" else spec[key_base]
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(body)
    cp = os.path.join(root, spec["caller_file"])
    os.makedirs(os.path.dirname(cp), exist_ok=True)
    with open(cp, "w", encoding="utf-8") as fh:
        fh.write(spec["caller_body"])


def _surface_bytes(spec: dict, root: str, db: str, *, sem_body: bool = False) -> str:
    """The full agent-visible surface: the brief text + a deterministic
    serialization of the localize candidate list (path, score, witness)."""
    prev = os.environ.get("GT_SEM_BODY")
    if sem_body:
        os.environ["GT_SEM_BODY"] = "1"
    else:
        os.environ.pop("GT_SEM_BODY", None)
    try:
        brief = generate_v1r_brief(spec["issue"], root, db).brief_text
        loc = localize(spec["issue"], db, repo_root=root)
        cand_lines = [
            f"{GL._normalize(c.file_path)}\t{c.score:.6f}\t{c.render_witness()}"
            for c in (loc.candidates or [])
        ]
    finally:
        if prev is None:
            os.environ.pop("GT_SEM_BODY", None)
        else:
            os.environ["GT_SEM_BODY"] = prev
    return brief + "\n===LOCALIZE===\n" + "\n".join(cand_lines)


def _leaked_tokens(surface: str, spec: dict) -> list[str]:
    """Every test/gold artifact that appears in the emitted bytes (should be [])."""
    needles = [
        spec["test_sym"],                       # the FAIL_TO_PASS test name
        os.path.basename(spec["test_file"]),    # test basename
        spec["test_file"].split("/")[0] + "/",  # the tests/ (or pkg/) prefix ONLY if test-dir
        *(_SENTINELS),                          # gold/test disk-only content
    ]
    # tests/ dir prefix only meaningful when the test file lives in a tests dir.
    if not spec["test_file"].startswith("tests/"):
        needles = needles[:2] + list(_SENTINELS)
    return sorted({n for n in needles if n and n in surface})


def _assert_leak_free(surface: str, spec: dict) -> None:
    leaked = _leaked_tokens(surface, spec)
    assert not leaked, f"LEAK: test/gold artifact surfaced in the brief: {leaked}"


# ============================ INVARIANT (green) ================================
@pytest.mark.parametrize("lang", ["python", "go"])
def test_brief_localize_byte_identical_across_repo_root(tmp_path, lang):
    """Vector 1: the SAME graph rendered on (base) vs (base+gold+test) is
    byte-identical — no on-disk read let the answer change the output."""
    spec = _SPECS[lang]
    db = str(tmp_path / "g.db")
    _build_graph(db, spec, is_test_flag=1)
    base = str(tmp_path / "base")
    gold = str(tmp_path / "gold")
    _write_tree(base, spec, "base")
    _write_tree(gold, spec, "gold")

    b_base = _surface_bytes(spec, base, db)
    b_gold = _surface_bytes(spec, gold, db)
    assert b_base == b_gold, (
        "repo_root leak: base vs gold+test checkout produced different bytes\n"
        f"--- base ---\n{b_base}\n--- gold ---\n{b_gold}"
    )


@pytest.mark.parametrize("lang", ["python", "go"])
def test_no_test_or_gold_artifact_leaked(tmp_path, lang):
    """Vector 2: no test name / test path / gold sentinel in the emitted bytes."""
    spec = _SPECS[lang]
    db = str(tmp_path / "g.db")
    _build_graph(db, spec, is_test_flag=1)
    for variant in ("base", "gold"):
        root = str(tmp_path / variant)
        _write_tree(root, spec, variant)
        _assert_leak_free(_surface_bytes(spec, root, db), spec)


def test_producer_deterministic_same_input(tmp_path):
    """Determinism: two runs on identical (issue, repo_root, graph) → same bytes."""
    spec = _SPECS["python"]
    db = str(tmp_path / "g.db")
    _build_graph(db, spec, is_test_flag=1)
    base = str(tmp_path / "base")
    _write_tree(base, spec, "base")
    assert _surface_bytes(spec, base, db) == _surface_bytes(spec, base, db)


def test_b2_flag_on_off_byte_identical(tmp_path):
    """B2 (GT_SEM_BODY) reads function BODIES from disk. With the embedder off it
    must be render-neutral: flag ON vs OFF is byte-identical AND leaks no body
    content — the body vocabulary feeds ranking only, never the emitted bytes."""
    spec = _SPECS["python"]
    db = str(tmp_path / "g.db")
    _build_graph(db, spec, is_test_flag=1)
    gold = str(tmp_path / "gold")
    _write_tree(gold, spec, "gold")   # gold body carries GOLDPATCHSENTINEL on disk
    off = _surface_bytes(spec, gold, db, sem_body=False)
    on = _surface_bytes(spec, gold, db, sem_body=True)
    assert off == on, "B2 changed the emitted bytes with a zero embedder (render leak)"
    _assert_leak_free(on, spec)


def _fts5_available() -> bool:
    try:
        c = sqlite3.connect(":memory:")
        c.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        c.close()
        return True
    except sqlite3.OperationalError:
        return False


@pytest.mark.skipif(not _fts5_available(), reason="Python sqlite3 built without FTS5")
def test_fts5_name_leg_excludes_test_rows_from_stale_index(tmp_path):
    """Vector 1b (Fable S1/L2, reproduced live): a graph.db whose Go-built EXTERNAL-content
    nodes_fts was populated BEFORE the is_test-at-INSERT fix (store/sqlite.go) still carries
    test rows. The Python name leg (``_fts5_candidates``, direct-graph.db path) MUST exclude
    them via the defense-in-depth is_test JOIN, so a test symbol (Go ``TestX``, Mocha
    ``it: …``) can never become an FTS5 seed / BFS root / ``fts5 match: …`` render.
    RED before the join (the test node is returned); GREEN after."""
    db = str(tmp_path / "g.db")
    con = sqlite3.connect(db)
    con.executescript(
        """CREATE TABLE nodes(id INTEGER PRIMARY KEY, label TEXT, name TEXT,
             qualified_name TEXT, file_path TEXT, start_line INTEGER, end_line INTEGER,
             signature TEXT, return_type TEXT, is_exported INTEGER, is_test INTEGER,
             language TEXT, parent_id INTEGER);
           CREATE VIRTUAL TABLE nodes_fts USING fts5(
             name, qualified_name, signature, file_path,
             content='nodes', content_rowid='id');"""
    )
    # id=1 production, id=2 TEST — both carry the standalone token 'redis'.
    con.execute("INSERT INTO nodes(id,label,name,qualified_name,file_path,signature,is_test,language)"
                " VALUES(1,'Function','handle_redis','','svc/net.py','handle_redis()',0,'python')")
    con.execute("INSERT INTO nodes(id,label,name,qualified_name,file_path,signature,is_test,language)"
                " VALUES(2,'Function','test_redis','','tests/test_net.py','test_redis()',1,'python')")
    # Populate nodes_fts the OLD (pre-fix) way: ALL rows, NO is_test filter = the stale index
    # a graph.db baked before the store/sqlite.go fix would carry.
    con.execute("INSERT INTO nodes_fts(rowid,name,qualified_name,signature,file_path)"
                " SELECT id,name,COALESCE(qualified_name,''),COALESCE(signature,''),file_path FROM nodes")
    con.commit()
    try:
        got = GL._fts5_candidates(con, {"redis"})
    finally:
        con.close()
    ids = [r[0] for r in got]
    assert 2 not in ids, f"LEAK: test node surfaced from a stale nodes_fts index: {got}"
    assert 1 in ids, f"regression: production node must still be retrieved: {got}"


def test_render_witness_drops_test_block_anchor(tmp_path):
    """Fable L8 (defense-in-depth): the hop-2 render prints ``w.anchor`` verbatim
    (``{anchor} -> ... -> {far}``), but the old edge-witness filter guarded only
    src/dst_symbol — NEVER the anchor. A witness whose ANCHOR is a test-block name
    (Mocha ``it: …``) but whose endpoints are product symbols would leak the test
    description. The guard now drops any witness whose anchor OR endpoint is a
    test-block name. RED before the anchor check; GREEN after."""
    from types import SimpleNamespace

    from groundtruth.pretask.graph_localizer import Candidate, Witness

    # hop-2 witness: ANCHOR is a Mocha test description, endpoints are product symbols.
    w_bad = Witness(file_path="svc/net.py", anchor="it: should reject bad input",
                    edge_type="CALLS", direction="calls_anchor", verified=True,
                    confidence=1.0, hop=2, src_symbol="handler", dst_symbol="validate")
    out = Candidate.render_witness(SimpleNamespace(witnesses=[w_bad]))
    assert "it:" not in out and out == "", f"L8 LEAK: test anchor rendered: {out!r}"

    # control: a product-symbol anchor still renders its structural fact.
    w_ok = Witness(file_path="svc/net.py", anchor="validate", edge_type="CALLS",
                   direction="calls_anchor", verified=True, confidence=1.0, hop=2,
                   src_symbol="handler", dst_symbol="validate")
    out2 = Candidate.render_witness(SimpleNamespace(witnesses=[w_ok]))
    assert "validate" in out2, f"control: product witness must still render: {out2!r}"


# ============================ MUTATION (red) ==================================
@pytest.mark.parametrize("lang", ["python", "go"])
def test_mutation_flip_test_exclusion_reddens_leak(tmp_path, lang):
    """Flip the exclusion: the test node is mislabelled ``is_test=0`` so the seed
    filter no longer excludes it. Guard #2 MUST go red — the test artifact leaks.
    A dead mutation (the test node still excluded) would make this assertion fail,
    proving the ``is_test`` exclusion is load-bearing for the leak invariant."""
    spec = _SPECS[lang]
    db = str(tmp_path / "g.db")
    _build_graph(db, spec, is_test_flag=0)          # EXCLUSION FLIPPED OFF
    base = str(tmp_path / "base")
    _write_tree(base, spec, "base")
    surface = _surface_bytes(spec, base, db)

    leaked = _leaked_tokens(surface, spec)
    assert leaked, (
        "MUTATION DEAD: flipping the is_test exclusion leaked nothing — the leak "
        "invariant is not actually guarding the delivered surface."
    )
    # the exact green assertion the invariant tests use now RAISES here (red).
    with pytest.raises(AssertionError):
        _assert_leak_free(surface, spec)
