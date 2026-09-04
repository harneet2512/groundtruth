"""Fix-campaign (2026-06-15) deterministic Stage-1 pins for the localization +
embedder defects in the §4/§4.2 rerank stack and the ONNX embedder.

Every test keys on issue/graph STRUCTURE (path-separator shape, witness
direction, subject position, dense dispersion, tokenizer windows) on synthetic
fixtures — no task IDs, no gold labels, no benchmark names. Red-before-green:
each test FAILS on the pre-fix code and passes after the owned-file fix.

Bug map (worst-first):
  2  RRF/struct tie-break degeneracy — the subject-defining file must out-rank
     its callee under equal-strength verified witnesses (alphabetical path is
     the LAST resort, never a relevance decider).
  3  is_generated unanchored substring — a handwritten file under a
     ``generated/`` dir must NOT eat the -0.5 ranking demote.
  4  witness_tier collapses hop-0 DEFINES with hop>=2 verified structural — a
     real distant edge out-ranks a name-equality DEFINES.
  5  0.7 confidence floor drops name_match (0.6)+NULL edges — hub/prox/reach
     floors lowered to the 0.5 name_match floor so a 0.6 name_match hub is
     penalized and name_match reach is not blanked.
  6  dense-dispersion gate measures coverage not discrimination — MAD over the
     NONZERO sem values; few-but-confident dense is not treated as flat.
  7  embedder 128-token truncation drops the issue tail — the issue QUERY
     tokenizes at a larger window than per-symbol passages.
  8  e5 query/passage role threaded explicitly + folded into the passage cache
     key (a query-prefixed vector never collides with a passage entry).
 10  EmbeddingModel.dim re-derived from the ONNX output width after load.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("tokenizers", reason="tokenizers is not installed")


@pytest.fixture(autouse=True)
def _clear_shared_embed_caches():
    """Clear BOTH semantic caches before AND after each test so a real-model load in
    this file never poisons the shared bounded-LRU passage-vector cache that
    test_semantic_encode_budget asserts on (cross-module pollution)."""

    def _clear():
        try:
            from groundtruth.pretask import anchor_select as _as

            _as._EMBED_CACHE.clear()
            _as._SYMVEC_CACHE.clear()
        except Exception:
            pass
        try:
            from groundtruth.memory.enrich import embed as _e

            _e._PASSAGE_VEC_CACHE.clear()
        except Exception:
            pass

    _clear()
    yield
    _clear()


# ===========================================================================
# Bug 1 — candidate-set path-key mismatch fragments the gold
# ===========================================================================


def _winpath_db_and_repo(tmp_path: Path):
    """A graph whose DB paths use BACKSLASHES + a ./-prefix (the Windows-indexed
    shape) so the RAW reach/lex keys differ from anchor_select's normalized keys.
    The repo files exist at the canonical posix locations so lexical search reads
    them. a CALLS b, both name issue tokens."""
    db = tmp_path / "graph.db"
    repo = tmp_path / "repo"
    (repo / "pkg" / "mod").mkdir(parents=True)
    (repo / "pkg" / "mod" / "alpha.py").write_text(
        "def parse_amount(line):\n    return normalize_amount(line)\n", encoding="utf-8"
    )
    (repo / "pkg" / "mod" / "beta.py").write_text(
        "def normalize_amount(line):\n    return line.strip()\n", encoding="utf-8"
    )
    conn = sqlite3.connect(str(db))
    conn.executescript(_SCHEMA)
    conn.executemany(
        "INSERT INTO nodes (id,label,name,file_path,start_line,end_line,signature,"
        "is_test,language) VALUES (?,?,?,?,?,?,?,0,'python')",
        [
            # backslash + ./ prefix — the un-normalized DB spelling
            (1, "Function", "parse_amount", "pkg\\mod\\alpha.py", 1, 2, "x"),
            (2, "Function", "normalize_amount", "./pkg/mod/beta.py", 1, 2, "x"),
        ],
    )
    conn.execute(
        "INSERT INTO edges (id,source_id,target_id,type,source_line,source_file,"
        "resolution_method,confidence) VALUES "
        "(1,1,2,'CALLS',2,'pkg/mod/alpha.py','import',1.0)"
    )
    conn.commit()
    conn.close()
    return str(repo), str(db)


def test_candidate_set_has_no_path_separator_duplicates(tmp_path, monkeypatch):
    """RED (pre-fix): the same physical file enters candidate_set once as the
    normalized sem key and once as the RAW reach/lex key, so ranked_full carries
    TWO rows that normalize to one path. GREEN: each file is one candidate."""
    from groundtruth.pretask import v7_4_brief as b
    from groundtruth.pretask.v7_4_brief import _norm_path, run_v74

    # Semantic off: isolate the structural/lexical path-key plumbing.
    monkeypatch.setattr(b, "_SEMANTIC_AVAILABLE", False)
    repo, db = _winpath_db_and_repo(tmp_path)
    res = run_v74(
        issue_text="parse_amount fails to normalize_amount the parsed line value",
        repo_root=repo,
        graph_db=db,
        ablation="C",
    )
    paths = [r["path"] for r in res.ranked_full]
    norm = [_norm_path(p) for p in paths]
    assert len(set(norm)) == len(norm), (
        f"a file fragmented into >1 candidate by path-separator spelling: {paths}"
    )


def test_callee_carries_both_lex_and_reach_in_one_row(tmp_path, monkeypatch):
    """The callee (beta.py) is reached via the CALLS edge (reach signal) AND names
    an issue token (lex signal). PRE-FIX those signals split across the raw and
    normalized keys; POST-FIX one row carries BOTH."""
    from groundtruth.pretask import v7_4_brief as b
    from groundtruth.pretask.v7_4_brief import _norm_path, run_v74

    monkeypatch.setattr(b, "_SEMANTIC_AVAILABLE", False)
    repo, db = _winpath_db_and_repo(tmp_path)
    res = run_v74(
        issue_text="parse_amount fails to normalize_amount the parsed line value",
        repo_root=repo,
        graph_db=db,
        ablation="C",
    )
    rows = {_norm_path(r["path"]): r["components"] for r in res.ranked_full}
    beta = rows.get("pkg/mod/beta.py")
    assert beta is not None, f"callee absent from ranked set: {list(rows)}"
    # The callee must carry its REACH signal (it is graph-reachable from the anchor)
    # in the SAME row that carries its lexical signal — not split across two keys.
    assert beta.get("reach", 0.0) > 0.0, (
        f"callee row lost its reach signal to a path-key split: {beta}"
    )


# ===========================================================================
# Bug 2 — struct/RRF tie-break: subject-defining file out-ranks its callee
# ===========================================================================

_SCHEMA = """
    CREATE TABLE nodes (
        id INTEGER PRIMARY KEY, label TEXT, name TEXT, qualified_name TEXT,
        file_path TEXT, start_line INTEGER, end_line INTEGER, signature TEXT,
        return_type TEXT, is_exported INTEGER, is_test INTEGER, language TEXT,
        parent_id INTEGER
    );
    CREATE TABLE edges (
        id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER, type TEXT,
        source_line INTEGER, source_file TEXT, resolution_method TEXT,
        confidence REAL, metadata TEXT
    );
"""


def _subject_callee_db(tmp_path: Path) -> str:
    """A defines the issue's FIRST-named anchor (the broken fn) and CALLS B,
    which defines the second-named anchor. Both carry a verified hop-0 witness;
    B has in-degree 1 (it is the callee) so a degree/composite prior tips the
    raw score to B unless a relevance key (subject position) decides.

    Path stems are chosen so the WRONG answer (B) sorts ALPHABETICALLY FIRST:
    ``a_app/caller.py`` (subject) vs ``a_app/aaa_callee.py`` (callee) — the
    callee's basename is alphabetically earlier, so a path-string tie-break
    would pick the callee. Only a subject-relevance key flips it back."""
    db = tmp_path / "graph.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(_SCHEMA)
    conn.executemany(
        "INSERT INTO nodes (id,label,name,file_path,start_line,end_line,"
        "signature,is_test,language) VALUES (?,?,?,?,?,?,?,0,'python')",
        [
            (1, "Method", "apply_discount", "a_app/caller.py", 1, 3, "x"),
            (2, "Method", "round_cents", "a_app/aaa_callee.py", 1, 2, "x"),
        ],
    )
    conn.execute(
        "INSERT INTO edges (id,source_id,target_id,type,source_line,source_file,"
        "resolution_method,confidence) VALUES "
        "(1,1,2,'CALLS',2,'a_app/caller.py','import',1.0)"
    )
    conn.commit()
    conn.close()
    return str(db)


def test_subject_defining_file_outranks_callee_under_equal_witness(tmp_path):
    """RED (pre-fix): the callee out-ranks the subject-defining file because the
    composite score / struct rank rewards the callee's in-degree, and the
    alphabetical path string seals it. GREEN: the file defining the issue's
    FIRST-named anchor (the broken function) is the top candidate."""
    from groundtruth.pretask.graph_localizer import localize

    issue = (
        "apply_discount computes the wrong total. When apply_discount runs it "
        "fails to round the cents via round_cents, so round_cents never floors "
        "the value. Expected apply_discount to call round_cents correctly."
    )
    res = localize(issue, _subject_callee_db(tmp_path))
    order = [c.file_path for c in res.candidates]
    assert order, "no candidates on the witnessed db"
    assert order[0] == "a_app/caller.py", f"subject-defining file did not win the cap slot: {order}"


def test_exact_tie_falls_to_relevance_key_not_path(tmp_path):
    """Two candidates that tie on EVERY structural signal except subject
    position must order by subject position, NOT by the alphabetical path
    string (path is the last resort)."""
    from groundtruth.pretask.graph_localizer import (
        Candidate,
        Witness,
        _final_relevance_key,
    )

    # Identical confidence + lex_hits; differ only in subject position.
    w = Witness(
        file_path="z.py",
        anchor="a",
        edge_type="CALLS",
        direction="calls_anchor",
        verified=True,
        confidence=1.0,
        hop=0,
        src_symbol="a",
        dst_symbol="b",
    )
    early = Candidate("z_subject.py", 0.5, [w], lex_hits=2, degree=0, confidence=0.5)
    late = Candidate("a_other.py", 0.5, [w], lex_hits=2, degree=0, confidence=0.5)
    subject_pos = {"z_subject.py": 1, "a_other.py": 99}
    # The earlier-subject file must sort first despite the later, alphabetically
    # EARLIER path of the other.
    ranked = sorted([late, early], key=lambda c: _final_relevance_key(c, subject_pos))
    assert ranked[0].file_path == "z_subject.py", (
        f"relevance key did not beat the path string: {[c.file_path for c in ranked]}"
    )


# ===========================================================================
# Bug 3 — is_generated must not fire on a handwritten file in a generated/ dir
# ===========================================================================


def test_is_generated_ignores_bare_generated_dir():
    """A handwritten source file living under a ``generated/`` directory is NOT
    machine-generated — the ranking demote must key on unambiguous file-suffix
    forms only, never the bare dir substring."""
    from groundtruth.delivery.path_policy import is_generated

    assert is_generated("src/generated/handwritten_logic.py") is False, (
        "bare /generated/ dir substring still demotes a handwritten file"
    )
    assert is_generated("pkg/api.generated.helper.py") is False, (
        ".generated. dir/segment substring still demotes a handwritten file"
    )


def test_is_generated_still_fires_on_real_codegen_suffixes():
    """No over-correction: the unambiguous codegen suffix forms still demote."""
    from groundtruth.delivery.path_policy import is_generated

    for p in (
        "api/service.pb.go",
        "proto/messages_pb2.py",
        "zz_generated.deepcopy.go",
        "lib/model.g.dart",
        "lib/model.freezed.dart",
    ):
        assert is_generated(p) is True, f"real codegen file no longer demoted: {p}"


def test_generated_demote_not_applied_to_handwritten_generated_dir(tmp_path):
    """End-to-end on the localizer ranking: a witnessed handwritten file under a
    ``generated/`` dir must NOT be pushed down by the -0.5 generated demote."""
    from groundtruth.pretask.graph_localizer import localize

    db = tmp_path / "graph.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(_SCHEMA)
    conn.executemany(
        "INSERT INTO nodes (id,label,name,file_path,start_line,end_line,"
        "signature,is_test,language) VALUES (?,?,?,?,?,?,?,0,'python')",
        [
            (1, "Function", "compute_levy", "app/generated/levy.py", 1, 3, "x"),
            (2, "Function", "round_levy", "app/util.py", 1, 2, "x"),
        ],
    )
    conn.execute(
        "INSERT INTO edges (id,source_id,target_id,type,source_line,source_file,"
        "resolution_method,confidence) VALUES "
        "(1,1,2,'CALLS',2,'app/generated/levy.py','import',1.0)"
    )
    conn.commit()
    conn.close()
    issue = (
        "compute_levy returns the wrong amount. compute_levy should call "
        "round_levy to floor the result; instead round_levy is skipped."
    )
    res = localize(issue, str(db))
    by_file = {c.file_path: c.score for c in res.candidates}
    assert "app/generated/levy.py" in by_file, "handwritten generated/ file dropped"
    # Its score must be in the normal [0,1]-ish band, not crushed by -0.5.
    assert by_file["app/generated/levy.py"] > 0.0, (
        f"handwritten generated/ file ate the -0.5 demote: {by_file}"
    )


# ===========================================================================
# Bug 4 — witness_tier: verified DISTANT structural (hop>=2) ABOVE name-equality DEFINES
# ===========================================================================


def test_witness_tier_distant_structural_above_bare_defines():
    """A file whose only witness is a verified hop-2 CALLS edge (a real, if
    distant, structural fact) must sort STRICTLY ABOVE a file whose only witness
    is a hop-0 name-equality DEFINES (the file merely defines a same-named
    symbol). RED (pre-fix): both collapse to tier 1 — the DEFINES ties the real
    distant edge."""
    from groundtruth.pretask.graph_localizer import (
        Candidate,
        Witness,
        _struct_witness_tier,
    )

    distant = Candidate(
        "distant.py",
        0.5,
        [Witness("distant.py", "a", "CALLS", "calls_anchor", True, 1.0, 2, "x", "a")],
        lex_hits=1,
        degree=0,
        confidence=0.5,
    )
    defines = Candidate(
        "defines.py",
        0.5,
        [Witness("defines.py", "b", "DEFINES", "defines_anchor", True, 1.0, 0, "b", "b")],
        lex_hits=1,
        degree=0,
        confidence=0.5,
    )
    assert _struct_witness_tier(distant) < _struct_witness_tier(defines), (
        "verified distant structural edge did not out-tier a bare name-equality DEFINES"
    )


def test_witness_tier_close_structural_still_top():
    """No regression: a verified hop-0/1 CALLS edge is still the top tier (above
    both distant-structural and DEFINES)."""
    from groundtruth.pretask.graph_localizer import (
        Candidate,
        Witness,
        _struct_witness_tier,
    )

    close = Candidate(
        "close.py",
        0.5,
        [Witness("close.py", "a", "CALLS", "calls_anchor", True, 1.0, 0, "x", "a")],
        lex_hits=1,
        degree=0,
        confidence=0.5,
    )
    distant = Candidate(
        "distant.py",
        0.5,
        [Witness("distant.py", "a", "CALLS", "calls_anchor", True, 1.0, 2, "x", "a")],
        lex_hits=1,
        degree=0,
        confidence=0.5,
    )
    defines = Candidate(
        "defines.py",
        0.5,
        [Witness("defines.py", "b", "DEFINES", "defines_anchor", True, 1.0, 0, "b", "b")],
        lex_hits=1,
        degree=0,
        confidence=0.5,
    )
    assert _struct_witness_tier(close) < _struct_witness_tier(distant)
    assert _struct_witness_tier(distant) < _struct_witness_tier(defines)


# ===========================================================================
# Bug 5 — name_match (0.6) + NULL-confidence edges survive the floor
# ===========================================================================


def test_hub_penalty_counts_name_match_hub_at_floor(tmp_path):
    """A 0.6 name_match hub (in-degree above HUB_SCALE-relevant) must accrue a
    hub penalty — the floor is the 0.5 name_match floor, not 0.7."""
    from groundtruth.pretask.hub_penalty import compute_hub_penalties

    db = tmp_path / "graph.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(_SCHEMA)
    # one hub target + many callers, ALL via 0.6 name_match edges
    conn.execute(
        "INSERT INTO nodes (id,label,name,file_path,is_test,language) "
        "VALUES (1,'Function','hub','hub.py',0,'python')"
    )
    for i in range(2, 40):
        conn.execute(
            "INSERT INTO nodes (id,label,name,file_path,is_test,language) "
            "VALUES (?,?,?,?,0,'python')",
            (i, "Function", f"c{i}", f"caller{i}.py"),
        )
        conn.execute(
            "INSERT INTO edges (source_id,target_id,type,resolution_method,confidence) "
            "VALUES (?,1,'CALLS','name_match',0.6)",
            (i,),
        )
    conn.commit()
    conn.close()
    pens = compute_hub_penalties(str(db))
    assert pens.get("hub.py", 0.0) > 0.0, "0.6 name_match hub escaped the penalty (floor still 0.7)"


def test_reach_admits_name_match_edges_at_floor(tmp_path):
    """compute_reach (min_confidence=0.5) must traverse a 0.6 name_match edge —
    on name_match-heavy graphs the reach term goes blank under a 0.7 floor."""
    from groundtruth.pretask.graph_reach import compute_reach

    db = tmp_path / "graph.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(_SCHEMA)
    conn.executemany(
        "INSERT INTO nodes (id,label,name,file_path,is_test,language) VALUES (?,?,?,?,0,'python')",
        [(1, "Function", "a", "a.py"), (2, "Function", "b", "b.py")],
    )
    conn.execute(
        "INSERT INTO edges (source_id,target_id,type,resolution_method,confidence) "
        "VALUES (1,2,'CALLS','name_match',0.6)"
    )
    conn.commit()
    conn.close()
    reach = compute_reach(["a.py"], str(db), max_depth=3, min_confidence=0.5)
    assert "b.py" in reach and reach["b.py"].reach_score > 0.0, (
        "0.6 name_match edge blanked from reach at the 0.5 floor"
    )


def test_anchor_proximity_admits_name_match_edges_at_floor(tmp_path):
    """compute_anchor_proximity must count a 0.6 name_match 1-hop neighbor."""
    from groundtruth.pretask.anchor_proximity import compute_anchor_proximity

    db = tmp_path / "graph.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(_SCHEMA)
    conn.executemany(
        "INSERT INTO nodes (id,label,name,file_path,is_test,language) VALUES (?,?,?,?,0,'python')",
        [(1, "Function", "a", "a.py"), (2, "Function", "b", "b.py")],
    )
    conn.execute(
        "INSERT INTO edges (source_id,target_id,type,resolution_method,confidence) "
        "VALUES (1,2,'CALLS','name_match',0.6)"
    )
    conn.commit()
    conn.close()
    prox = compute_anchor_proximity(["a.py"], str(db))
    assert prox.get("b.py", 0.0) > 0.0, (
        "0.6 name_match neighbor not counted in proximity at the 0.5 floor"
    )


# ===========================================================================
# Bug 6 — dense-dispersion gate measures discrimination, not coverage
# ===========================================================================


def test_dispersion_gate_does_not_fire_on_few_but_confident_dense():
    """A dense signal that covers FEW files but discriminates SHARPLY among
    them (high max, clear spread over the covered set) is NOT flat — the gate
    must measure dispersion over the NONZERO sem values, not the zero-padded
    full candidate vector."""
    from groundtruth.pretask.v7_4_brief import (
        DEFAULT_WEIGHTS,
        _apply_dense_dispersion_gate,
    )

    files = [f"f{i}.py" for i in range(20)]
    # 3 of 20 covered, but SHARPLY separated (0.90 / 0.55 / 0.20).
    sem = {"f0.py": 0.90, "f1.py": 0.55, "f2.py": 0.20}
    _, fired, _ = _apply_dense_dispersion_gate(dict(DEFAULT_WEIGHTS), sem, files)
    assert fired is False, (
        "gate fired on a sharp few-but-confident dense signal (measured coverage, "
        "not discrimination)"
    )


def test_dispersion_gate_still_fires_on_truly_flat_covered_set():
    """No over-correction: when the COVERED set is itself flat (all-equal among
    the few covered files), the gate still fires."""
    from groundtruth.pretask.v7_4_brief import (
        DEFAULT_WEIGHTS,
        _apply_dense_dispersion_gate,
    )

    files = [f"f{i}.py" for i in range(20)]
    sem = {"f0.py": 0.83, "f1.py": 0.83, "f2.py": 0.83}
    _, fired, _ = _apply_dense_dispersion_gate(dict(DEFAULT_WEIGHTS), sem, files)
    assert fired is True, "gate missed an all-equal covered set"


# ===========================================================================
# Bug 7 — issue query tokenizes at a larger window than symbol passages
# ===========================================================================


def test_issue_query_window_larger_than_passage_window():
    """The issue QUERY must tokenize at a window strictly larger than the
    ~128-token per-symbol passage window, so the file/symbol hints in the issue
    tail are not discarded."""
    from groundtruth.memory.enrich.embed import (
        EmbeddingModel,
        DEFAULT_EMBED_MODEL,
        DEFAULT_EMBED_DIM,
        E5_MODEL,
        E5_DIM,
        _query_token_window,
        _passage_token_window,
    )

    gte = EmbeddingModel(DEFAULT_EMBED_MODEL, DEFAULT_EMBED_DIM)
    e5 = EmbeddingModel(E5_MODEL, E5_DIM)
    assert _passage_token_window(gte) <= 128
    assert _query_token_window(gte) > _passage_token_window(gte)
    assert _query_token_window(gte) >= 1024  # gte supports 8192
    assert _query_token_window(e5) >= 512  # e5 supports 512


def test_long_issue_query_tail_survives_tokenization():
    """A query whose discriminating token sits PAST the 128-token mark is still
    encoded distinctly (proves the larger query window is actually applied).

    Uses a deterministic fake session that records the input length so the test
    runs without a baked ONNX model."""
    from groundtruth.memory.enrich.embed import (
        EmbeddingModel,
        DEFAULT_EMBED_MODEL,
        DEFAULT_EMBED_DIM,
    )

    m = EmbeddingModel(DEFAULT_EMBED_MODEL, DEFAULT_EMBED_DIM)

    class _RecTok:
        def __init__(self):
            self.last_len = None

        def enable_padding(self, **kw):
            pass

        def enable_truncation(self, max_length=128, **kw):
            self.max_length = max_length

        def encode_batch(self, texts):
            # honor the configured truncation length
            class E:
                def __init__(self, n):
                    self.ids = list(range(n))
                    self.attention_mask = [1] * n

            out = []
            for t in texts:
                n = min(len(t.split()), getattr(self, "max_length", 128))
                self.last_len = n
                out.append(E(n))
            return out

    class _RecSess:
        def get_inputs(self):
            class I:
                name = "input_ids"

            class J:
                name = "attention_mask"

            return [I(), J()]

        def run(self, _o, feed):
            b, s = feed["input_ids"].shape
            return [np.ones((b, s, 768), dtype=np.float32)]

    tok = _RecTok()
    m._session = _RecSess()
    m._tokenizer = tok
    m._input_names = ["input_ids", "attention_mask"]

    long_query = " ".join(f"tok{i}" for i in range(400))
    m.embed(long_query, is_query=True)
    assert tok.last_len > 128, (
        f"issue query truncated to {tok.last_len} tokens — the tail was dropped"
    )


# ===========================================================================
# Bug 8 — explicit is_query threaded; query/passage role folded into the cache key
# ===========================================================================


def test_passage_hash_distinguishes_query_from_passage_role():
    """The SAME text embedded as a QUERY vs a PASSAGE must hash to DIFFERENT
    cache keys, so a query-prefixed vector can never poison a passage entry."""
    from groundtruth.memory.enrich.embed import passage_hash

    txt = "load configuration from disk"
    h_passage = passage_hash(txt, "intfloat/e5-small-v2", 384, "v", is_query=False)
    h_query = passage_hash(txt, "intfloat/e5-small-v2", 384, "v", is_query=True)
    assert h_passage != h_query, "query and passage roles collide in the cache key"


def test_embed_threads_is_query_explicitly_not_by_length():
    """An EmbeddingModel.embed_batch with a SINGLE passage and is_query=False
    must use the PASSAGE prefix — role comes from the explicit flag, never from
    len(texts)==1."""
    from groundtruth.memory.enrich.embed import (
        EmbeddingModel,
        E5_MODEL,
        E5_DIM,
    )

    m = EmbeddingModel(E5_MODEL, E5_DIM)
    captured = {}

    def _fake_prefixed(texts, *, is_query=False):
        captured["texts"] = list(texts)
        captured["is_query"] = is_query
        return [[0.0] * E5_DIM for _ in texts]

    m._embed_prefixed = _fake_prefixed  # type: ignore[assignment]
    m.embed_batch(["only one passage"], is_query=False)
    assert captured["texts"] == ["passage: only one passage"], (
        f"single passage got the wrong (query?) prefix: {captured['texts']}"
    )
    m.embed_batch(["only one query"], is_query=True)
    assert captured["texts"] == ["query: only one query"]


# ===========================================================================
# Bug 10 — EmbeddingModel.dim re-derived from the ONNX output width after load
# ===========================================================================


def test_dim_corrected_from_onnx_output_width(monkeypatch):
    """If the declared dim disagrees with the ONNX output width, the model
    corrects self.dim from the graph after load (the metadata must not lie to
    the cache-staleness check)."""
    from groundtruth.memory.enrich.embed import EmbeddingModel, DEFAULT_EMBED_MODEL

    # Declare a WRONG dim (999); the fake ONNX emits 768-wide token embeddings.
    m = EmbeddingModel(DEFAULT_EMBED_MODEL, 999)

    class _Out:
        def __init__(self, shape):
            self.shape = shape

    class _Sess:
        def get_inputs(self):
            class I:
                name = "input_ids"

            class J:
                name = "attention_mask"

            return [I(), J()]

        def get_outputs(self):
            # (batch, seq, hidden=768) — the true width
            return [_Out([None, None, 768])]

        def run(self, _o, feed):
            b, s = feed["input_ids"].shape
            return [np.ones((b, s, 768), dtype=np.float32)]

    import onnxruntime as _ort  # noqa: F401  (import guard parity)

    monkeypatch.setattr("onnxruntime.InferenceSession", lambda *a, **k: _Sess(), raising=False)

    # Point the tokenizer/onnx resolution at a temp dir with stub files so
    # _ensure_loaded reaches the session build (we stub the heavy bits).
    class _Tok:
        def enable_padding(self, **kw):
            pass

        def enable_truncation(self, **kw):
            pass

        def encode_batch(self, texts):
            class E:
                ids = [1, 2, 3]
                attention_mask = [1, 1, 1]

            return [E() for _ in texts]

    monkeypatch.setattr("tokenizers.Tokenizer.from_file", lambda *a, **k: _Tok(), raising=False)
    monkeypatch.setattr(EmbeddingModel, "_resolve_onnx_path", lambda self: Path("model.onnx"))
    monkeypatch.setattr(Path, "exists", lambda self: True)

    m._ensure_loaded()
    assert m.dim == 768, f"dim not corrected from ONNX output width: {m.dim}"
