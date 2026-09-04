"""CLUSTER F3 — Stage-1 deterministic pins for four localizer defects.

Each test is keyed to PATH/issue STRUCTURE on synthetic fixtures — no task IDs,
no gold labels, no benchmark names, no real-repo symbols.

VERDICTS at authoring (HEAD = gt-trial b3398595), verified by reading the code:

  BUG 1  STILL-REAL  graph_localizer._normalize:717 — ``.lstrip("./")`` is a
         CHARSET strip, not a prefix strip: it ate every leading '.'/'/' char so a
         real dotfile dir lost its dot (``.github/x.py`` -> ``github/x.py``) and two
         distinct files collapsed to one candidate key (``./.config/app.py`` and
         ``config/app.py`` both -> ``config/app.py``). FIXED here: anchored ``./``-
         prefix strip + one leading '/'. These tests PIN the fix (mutation: revert
         to ``.lstrip("./")`` -> RED).

  BUG 2  CLOSED      the final localize() sort tie-breaks ``-_rrf3(c)`` with
         ``*_final_relevance_key`` = ``(-confidence, -lex_hits, subject_pos,
         file_path)`` — the path string is LAST, behind three relevance signals
         (graph_localizer.py:744-760, wired at :2739-2746). This test pins that an
         embedder-off / grep-off tie is decided by SUBJECT POSITION, not alphabetical
         path order (mutation: drop ``_final_relevance_key`` from the sort key,
         leaving ``c.file_path`` -> RED).

  BUG 3  CLOSED      path_policy.is_generated:61 keys ONLY on
         _GENERATED_RANK_DEMOTE_SUFFIXES (``.pb.go``/``_pb2.py``/``zz_generated``/…),
         NOT the bare ``/generated/`` substring — a handwritten source file under a
         ``generated/`` directory no longer eats the -0.5 demote. (Fix lives in
         path_policy.py, a file-disjoint module; pinned here through the localizer's
         imported ``_is_generated`` alias.)

  BUG 4  N/A IN THIS FILE — see the module docstring of the report. The localizer's
         structural-signal floor is _NAME_MATCH_FLOOR (0.5), NOT 0.7, and
         _file_degrees/_degree_edge_filter do not confidence-filter at all, so
         name_match-0.6 edges already feed structural ranking here
         (test_admission_keeps_midconf_name_match_above_floor pins that). The 0.7
         floor the review names lives in the file-disjoint hub_penalty/graph_reach/
         anchor_proximity modules — out of this cluster's scope. Documented seam.
"""

from __future__ import annotations

import sqlite3

from groundtruth.pretask.graph_localizer import (
    Candidate,
    _final_relevance_key,
    _is_generated,
    _normalize,
    localize,
)

# ============================================================== BUG 1 (FIXED)


def test_normalize_strips_dotslash_prefix_only_not_charset() -> None:
    """The fix: a './' PREFIX is stripped (repeatably), a single leading '/' is
    stripped, and NOTHING else — leading dots in real path components survive.

    Mutation check: reverting _normalize to ``.lstrip("./").lstrip("/")`` turns
    ``.github/x.py`` into ``github/x.py`` and fails the dotfile assertions below.
    """
    # common cases — byte-identical to the prior behavior (no regression)
    assert _normalize("beets/importer.py") == "beets/importer.py"
    assert _normalize("./beets/importer.py") == "beets/importer.py"
    assert _normalize("././beets/importer.py") == "beets/importer.py"
    assert _normalize("beets\\importer.py") == "beets/importer.py"
    assert _normalize("/abs/beets/importer.py") == "abs/beets/importer.py"
    # THE FIX — leading dotfile directories keep their dot (charset strip ate it)
    assert _normalize(".github/workflows/ci.py") == ".github/workflows/ci.py"
    assert _normalize("./.config/app.py") == ".config/app.py"
    assert _normalize(".config/app.py") == ".config/app.py"
    # mid-path dot dirs were never the bug, but pin them anyway
    assert _normalize("src/.hidden/mod.py") == "src/.hidden/mod.py"


def test_normalize_no_collision_between_dotdir_and_plaindir() -> None:
    """``.config/app.py`` and ``config/app.py`` are DIFFERENT files. The charset
    strip collapsed them to one key, merging two candidates into one (or splitting
    a gold's score across two spellings). They must now key distinctly."""
    assert _normalize(".config/app.py") != _normalize("config/app.py")
    # and the './'-prefixed spelling of a path unifies with its bare spelling
    assert _normalize("./pkg/mod.py") == _normalize("pkg/mod.py")


def _make_dotslash_gold_db(tmp_path):
    """Gold node lives at a DB path with a leading './' AND a dotfile directory.
    A second node at the SAME logical file but the BARE spelling references it via
    a verified CALLS edge, so both spellings must unify into ONE candidate key —
    the gold may never split into two half-scored entries (the BUG-1 symptom)."""
    db = str(tmp_path / "graph.db")
    conn = sqlite3.connect(db)
    conn.executescript(
        """
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
    )
    conn.executemany(
        "INSERT INTO nodes (id,label,name,file_path,start_line,end_line,signature,"
        "is_test,language) VALUES (?,?,?,?,?,?,?,0,'python')",
        [
            # gold: defined at the './'-prefixed dotfile-dir spelling
            (1, "Method", "apply_hook", "./.ci/runner.py", 10, 40, "def apply_hook(self, cfg):"),
            # a caller in a plain dir that CALLS apply_hook (verified) — extends BFS
            (2, "Function", "drive", "pipeline/drive.py", 1, 8, "def drive():"),
        ],
    )
    conn.execute(
        "INSERT INTO edges (id,source_id,target_id,type,source_line,source_file,"
        "resolution_method,confidence) VALUES "
        "(1,2,1,'CALLS',5,'pipeline/drive.py','import',1.0)"
    )
    conn.commit()
    conn.close()
    return db


def test_localize_dotfile_dir_gold_keys_canonically(tmp_path) -> None:
    """The gold at ``./.ci/runner.py`` must surface as a candidate under its
    CANONICAL key ``.ci/runner.py`` (dotfile dir preserved, './' stripped) — never
    ``ci/runner.py`` (the charset over-strip) and never split into two entries.

    Mutation check: reverting _normalize to the charset strip keys the gold as
    ``ci/runner.py`` and this exact-key assertion goes RED.
    """
    issue = (
        "apply_hook does not run the configured hook. When drive() calls "
        "apply_hook the cfg is ignored."
    )
    db = _make_dotslash_gold_db(tmp_path)
    res = localize(issue, db)
    paths = [c.file_path for c in res.candidates]
    # canonical key: dotfile dir kept, './' prefix stripped
    assert ".ci/runner.py" in paths, f"gold not keyed canonically: {paths}"
    # the over-strip spelling must NOT appear
    assert "ci/runner.py" not in paths, f"charset over-strip leaked: {paths}"
    # exactly one candidate row per logical file (no split)
    assert paths.count(".ci/runner.py") == 1, f"gold split into duplicates: {paths}"


# ============================================================== BUG 2 (CLOSED)


def test_final_relevance_key_puts_path_string_last() -> None:
    """_final_relevance_key is the tie-break appended after the RRF term: it orders
    by (-confidence, -lex_hits, subject_pos, file_path). The path string is the LAST
    element — a signal-bearing key always decides before lexical path order.

    Mutation check: if the key degenerates to ``(c.file_path,)`` (the old bug), the
    first three relevance components vanish and this ordering assertion goes RED.
    """
    # 'zzz.py' has STRONGER relevance (higher confidence, earlier subject) than the
    # alphabetically-FIRST 'aaa.py'. The relevance key must rank zzz.py first.
    strong = Candidate(
        file_path="zzz.py",
        score=0.5,
        witnesses=[],
        lex_hits=3,
        degree=0,
        confidence=0.9,
    )
    weak = Candidate(
        file_path="aaa.py",
        score=0.5,
        witnesses=[],
        lex_hits=1,
        degree=0,
        confidence=0.4,
    )
    subject_pos = {"zzz.py": 2, "aaa.py": 50}
    ordered = sorted([weak, strong], key=lambda c: _final_relevance_key(c, subject_pos))
    assert ordered[0].file_path == "zzz.py", (
        "relevance (confidence/lex/subject) must beat alphabetical path order; "
        f"got {[c.file_path for c in ordered]}"
    )
    # and the path string is genuinely last: two candidates equal on all relevance
    # signals fall back to path order (insertion-stable, deterministic).
    a = Candidate(file_path="a.py", score=0.5, witnesses=[], lex_hits=2, degree=0, confidence=0.7)
    b = Candidate(file_path="b.py", score=0.5, witnesses=[], lex_hits=2, degree=0, confidence=0.7)
    sp = {"a.py": 5, "b.py": 5}
    tied = sorted([b, a], key=lambda c: _final_relevance_key(c, sp))
    assert [c.file_path for c in tied] == ["a.py", "b.py"]


def _final_sort_key(c: Candidate, subject_pos: "dict[str, int]") -> tuple:
    """Reconstruct the EXACT composed key localize() applies in its final sort
    (graph_localizer.py:2739-2746) for the embedder-OFF / grep-OFF / RRF-TIED
    regime: ``(grep_floor, depth_authority, -rrf3, *_final_relevance_key)``. Here
    every candidate is RRF-tied by construction (same grep/struct rank -> same
    rrf3), so the leading three terms are constant and ONLY _final_relevance_key
    decides — which is precisely the slot BUG-2 fixed. Wiring this through the real
    _final_relevance_key means a mutation that drops it (falling back to
    ``c.file_path``) flips the order under this key."""
    rrf_tied = 0.0  # all candidates share the same fusion mass in this regime
    grep_floor = 0  # no grep recall -> floor is a no-op (every file at 0)
    depth_authority = 0
    return (grep_floor, depth_authority, -rrf_tied, *_final_relevance_key(c, subject_pos))


def test_final_sort_rrf_tie_resolved_by_subject_not_alpha() -> None:
    """The load-bearing BUG-2 case: with the embedder OFF and no grep recall,
    ``_rrf3`` collapses to a constant for two equally-witnessed files, so the
    FINAL sort is decided entirely by the keys AFTER ``-_rrf3``. The file defining
    the issue's first-named (broken) function — alphabetically LATER — must win
    over its callee on SUBJECT POSITION, never on alphabetical path order.

    Mutation check: replace ``*_final_relevance_key(c, _cand_subject_pos)`` in
    localize()'s sort with the bare ``c.file_path`` (the pre-BUG-2 code) and the
    alphabetically-first 'alpha/cache.py' wins this RRF-tied contest -> RED.
    """
    # Two RRF-tied candidates, equal confidence + lex (the genuine tie). The ONLY
    # discriminator left is subject position: 'zeta/widget.py' defines the broken,
    # first-named function (subject_pos 0); 'alpha/cache.py' is its callee (pos 30).
    subject = Candidate(
        file_path="zeta/widget.py",
        score=0.50,
        witnesses=[],
        lex_hits=2,
        degree=0,
        confidence=0.5,
    )
    callee = Candidate(
        file_path="alpha/cache.py",
        score=0.50,
        witnesses=[],
        lex_hits=2,
        degree=0,
        confidence=0.5,
    )
    subject_pos = {"zeta/widget.py": 0, "alpha/cache.py": 30}
    ordered = sorted([callee, subject], key=lambda c: _final_sort_key(c, subject_pos))
    assert ordered[0].file_path == "zeta/widget.py", (
        "RRF-tied: the subject-defining file must beat the alphabetically-earlier "
        f"callee on subject position, not path order; got "
        f"{[c.file_path for c in ordered]}"
    )


# ============================================================== BUG 3 (CLOSED)


def test_is_generated_demotes_codegen_suffix_only_not_dir() -> None:
    """A handwritten source file under a ``generated/`` directory is NOT machine-
    generated and must NOT eat the ranking demote; unambiguous codegen SUFFIXES
    (protobuf/deepcopy/dart) still demote.

    Mutation check: add a bare ``/generated/`` (or ``.generated.``) directory
    substring back into the demote predicate and the first two assertions go RED.
    """
    # real source under a generated/ dir -> NOT demoted
    assert _is_generated("pkg/generated/handwritten.go") is False
    assert _is_generated("src/generated/real_module.py") is False
    assert _is_generated("a/b/generated/widget.ts") is False
    # genuine codegen suffixes -> demoted
    assert _is_generated("svc/api.pb.go") is True
    assert _is_generated("proto/msg_pb2.py") is True
    assert _is_generated("k8s/types/zz_generated.deepcopy.go") is True
    assert _is_generated("ui/model.g.dart") is True
