"""W6 — transactional edit overlay: Stage-1 deterministic proofs.

The invariant under test: after an edit, downstream queries must NEVER see a stale
pre-edit graph presented as CURRENT truth. Pipeline: snapshot -> apply -> incremental
reindex (executor-routed gt-index) -> DIFFERENTIAL diagnostics (only NEW errors
attributed) -> contract delta -> commit | byte-exact rollback.

Laws pinned here:
  - DIFFERENTIAL law (B1/E1): a pre-existing baseline syntax error is NEVER blamed on
    the edit (mutation M1 target).
  - Rollback completeness: every touched file + graph.db byte-compare to the snapshot
    (mutation M2 target); post_revision == pre_revision after rollback.
  - Revision honesty: a committed edit ALWAYS advances post_revision — even when the
    reindex is unavailable (stale graph FLAGGED, edit signature folded in).
  - Executor routing: with an injected executor NO subprocess is spawned for reindex;
    commands follow the frozen CLI contract `gt-index -root R -output G.db [-file F]`
    and the frozen (cmd, cwd, timeout) triple.
  - Capability honesty: jsx/rs/java syntax = unsupported (never guessed clean);
    ts/tsx = supported via esbuild --write=false (toolchain_missing if absent);
    a supported language with its tool absent = toolchain_missing, never "ok".
  - Determinism: identical inputs => identical OverlayResult.to_dict() (wallclock-free).
  - Flag gate: GT_EDIT_OVERLAY off => apply_edit_transaction touches NOTHING.

Fixture strategy (probed, honest): the REAL gt-index binary at <repo>/gt-index/ is
used for the py/go end-to-end pipeline tests when present (it is, on this box);
executor-STUB tests (recorded outputs) cover routing/determinism/failure paths and
run everywhere.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import stat
import subprocess
import sys

import pytest

from groundtruth.runtime import edit_overlay as eo
from groundtruth.runtime.edit_overlay import (
    CONTRACT_OK,
    CONTRACT_STALE_GRAPH,
    EditOverlay,
    GRAPH_FRESH,
    GRAPH_STALE,
    REINDEX_FULL,
    REINDEX_INCREMENTAL,
    REINDEX_UNAVAILABLE,
    SYNTAX_AVAILABLE,
    SYNTAX_TOOLCHAIN_MISSING,
    SYNTAX_UNSUPPORTED,
    apply_edit_transaction,
    capability_matrix,
    mark_stale_envelopes,
)

# --------------------------------------------------------------------------
# fixtures / helpers
# --------------------------------------------------------------------------
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _find_gt_index() -> str | None:
    """The real gt-index binary: PATH first, then the checked-out gt-index/ dir."""
    found = shutil.which("gt-index")
    if found:
        return found
    for name in ("gt-index.exe", "gt-index"):
        cand = os.path.join(_REPO_ROOT, "gt-index", name)
        if os.path.isfile(cand):
            return cand
    return None


_GT_INDEX = _find_gt_index()
_needs_binary = pytest.mark.skipif(
    _GT_INDEX is None, reason="real gt-index binary not found (PATH / <repo>/gt-index/)"
)

_PY_MAIN = "def alpha():\n    return beta()\n\n\ndef beta():\n    return 1\n"
_PY_MAIN_V2 = (
    "def alpha():\n    return beta()\n\n\ndef beta():\n    return 1\n\n\n"
    "def gamma_new_symbol():\n    return alpha()\n"
)
_PY_BROKEN = "def broken(:\n    return 1\n"
_GO_MAIN = (
    "package main\n\nfunc Alpha() int {\n\treturn Beta()\n}\n\n"
    "func Beta() int {\n\treturn 1\n}\n"
)
_GO_MAIN_V2 = (
    "package main\n\nfunc Alpha() int {\n\treturn Beta()\n}\n\n"
    "func Beta() int {\n\treturn 1\n}\n\nfunc GammaNew() int {\n\treturn Alpha()\n}\n"
)


def _write(root: str, rel: str, content: str) -> None:
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p) or root, exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)


def _read_b(root: str, rel: str) -> bytes | None:
    p = os.path.join(root, rel)
    if not os.path.isfile(p):
        return None
    with open(p, "rb") as fh:
        return fh.read()


def _sha(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def _index_full(root: str, db: str) -> None:
    assert _GT_INDEX is not None
    proc = subprocess.run(
        [_GT_INDEX, "-root", root, "-output", db],
        capture_output=True, text=True, timeout=300, check=False,
    )
    assert proc.returncode == 0, f"seed index failed: {proc.stderr[-500:]}"


def _node_names(db: str) -> set[str]:
    con = sqlite3.connect(db)
    try:
        return {r[0] for r in con.execute("SELECT name FROM nodes").fetchall()}
    finally:
        con.close()


def _ok_executor_factory(record: list):
    """An executor stub obeying the frozen triple contract. Returns rc=0 for every
    command (gt-index AND edit_check's `python -c ast.parse` probe) and records the
    calls. It writes NOTHING — the db stays byte-identical (that is the point for
    the determinism tests)."""

    def executor(cmd, cwd, timeout):
        record.append((list(cmd), cwd, int(timeout)))
        return (0, "", "")

    return executor


# --------------------------------------------------------------------------
# (1) end-to-end pipeline on the REAL binary — python
# --------------------------------------------------------------------------
@_needs_binary
def test_pipeline_py_real_binary_commit_fresh_graph(tmp_path):
    """The headline invariant: after commit the graph CONTAINS the edit's new symbol
    and post_revision advanced — no stale pre-edit graph presented as current."""
    root, db = str(tmp_path / "repo"), str(tmp_path / "graph.db")
    _write(root, "m.py", _PY_MAIN)
    _index_full(root, db)
    assert "gamma_new_symbol" not in _node_names(db)  # pre-edit graph lacks the symbol
    pre_sha = _sha(db)

    ov = EditOverlay(root, db, gt_index_bin=_GT_INDEX)
    ov.snapshot({"m.py": _PY_MAIN_V2})
    ov.apply()
    reindex_state, graph_state = ov.reindex()
    diags = ov.diagnostics_delta()
    contract = ov.contract_delta()
    ov.commit()
    res = ov.result(diags, contract)

    assert reindex_state == REINDEX_INCREMENTAL     # -file path, db pre-existing
    assert graph_state == GRAPH_FRESH
    assert res.committed and not res.rolled_back
    assert res.pre_revision == pre_sha
    assert res.post_revision == _sha(db)            # post rev IS the new db content-hash
    assert res.post_revision != res.pre_revision    # revision advanced
    assert not res.graph_stale
    assert diags == []                              # clean edit -> no new diagnostics
    # the refreshed graph now carries the edit — the anti-staleness proof.
    assert "gamma_new_symbol" in _node_names(db)
    assert res.capability_states["syntax"][".py"] == SYNTAX_AVAILABLE
    assert res.capability_states["contract"] == CONTRACT_OK


@_needs_binary
def test_pipeline_full_fallback_on_delete(tmp_path):
    """A deletion cannot ride -file (file-keyed replace) -> full -root fallback."""
    root, db = str(tmp_path / "repo"), str(tmp_path / "graph.db")
    _write(root, "m.py", _PY_MAIN)
    _write(root, "extra.py", "def extra_sym():\n    return 2\n")
    _index_full(root, db)
    assert "extra_sym" in _node_names(db)

    ov = EditOverlay(root, db, gt_index_bin=_GT_INDEX)
    ov.snapshot({"extra.py": None})
    ov.apply()
    reindex_state, graph_state = ov.reindex()
    ov.commit()

    assert reindex_state == REINDEX_FULL
    assert graph_state == GRAPH_FRESH
    assert not os.path.isfile(os.path.join(root, "extra.py"))
    assert "extra_sym" not in _node_names(db)       # graph reflects the deletion


# --------------------------------------------------------------------------
# (2) end-to-end on the REAL binary — go (reindex works; syntax honesty probed)
# --------------------------------------------------------------------------
@_needs_binary
def test_pipeline_go_real_binary(tmp_path):
    root, db = str(tmp_path / "repo"), str(tmp_path / "graph.db")
    _write(root, "main.go", _GO_MAIN)
    _index_full(root, db)
    assert "GammaNew" not in _node_names(db)

    ov = EditOverlay(root, db, gt_index_bin=_GT_INDEX)
    ov.snapshot({"main.go": _GO_MAIN_V2})
    ov.apply()
    reindex_state, graph_state = ov.reindex()
    diags = ov.diagnostics_delta()
    ov.commit()
    res = ov.result(diags)

    assert reindex_state == REINDEX_INCREMENTAL
    assert graph_state == GRAPH_FRESH
    assert "GammaNew" in _node_names(db)
    # go syntax capability is HONEST about the host toolchain: gofmt present ->
    # a real verdict; absent -> toolchain_missing (NEVER guessed clean).
    expected = (
        SYNTAX_AVAILABLE if shutil.which("gofmt") else SYNTAX_TOOLCHAIN_MISSING
    )
    assert res.capability_states["syntax"][".go"] == expected
    assert diags == []  # either a real "ok" or an unattributable "unavailable"


# --------------------------------------------------------------------------
# (3) DIFFERENTIAL law — only NEW, edit-attributed diagnostics (M1 target)
# --------------------------------------------------------------------------
def test_differential_never_blames_baseline(tmp_path):
    """Three files: A pre-broken and edited (still broken) -> NOT attributed;
    B edited clean -> not in delta; C broken BY the edit -> attributed. The delta
    must contain EXACTLY C."""
    root, db = str(tmp_path / "repo"), str(tmp_path / "graph.db")
    _write(root, "a_prebroken.py", _PY_BROKEN)          # baseline failure
    _write(root, "b_clean.py", "def b():\n    return 1\n")
    _write(root, "c_victim.py", "def c():\n    return 2\n")

    ov = EditOverlay(root, db, gt_index_bin="gt-index-definitely-not-installed")
    ov.snapshot({
        "a_prebroken.py": _PY_BROKEN + "\n# still broken, edit did not cause it\n",
        "b_clean.py": "def b():\n    return 10\n",
        "c_victim.py": "def c(:\n    return 2\n",      # NEW syntax error
    })
    ov.apply()
    ov.reindex()
    diags = ov.diagnostics_delta()

    assert [d.file for d in diags] == ["c_victim.py"]   # ONLY the edit-caused error
    d = diags[0]
    assert d.verdict == "syntax_error"
    assert d.diagnostic  # the toolchain's own error text is carried
    assert "<gt-" not in d.diagnostic                   # leak-scrub invariant
    # the documented differential-blocking class: eligible, but UNMEASURED => advisory.
    assert d.blocking_eligible is True
    assert d.measured is False


def test_differential_new_file_with_error_is_attributed(tmp_path):
    """A brand-new broken file IS the edit's fault (baseline 'absent' is clean)."""
    root, db = str(tmp_path / "repo"), str(tmp_path / "graph.db")
    ov = EditOverlay(root, db, gt_index_bin="gt-index-definitely-not-installed")
    ov.snapshot({"new_broken.py": _PY_BROKEN})
    ov.apply()
    ov.reindex()
    diags = ov.diagnostics_delta()
    assert [d.file for d in diags] == ["new_broken.py"]


def test_differential_unprovable_baseline_never_attributed(tmp_path):
    """BEFORE verdict 'unavailable' (unprovable baseline) -> an after-edit error is
    NOT attributed (correct-or-quiet: we cannot prove the edit introduced it)."""
    root, db = str(tmp_path / "repo"), str(tmp_path / "graph.db")
    _write(root, "x.ts", "let a = 1;")                  # ts: edit_check unavailable
    ov = EditOverlay(root, db, gt_index_bin="gt-index-definitely-not-installed")
    ov.snapshot({"x.ts": "let a = ((((;"})
    ov.apply()
    ov.reindex()
    diags = ov.diagnostics_delta()
    assert diags == []                                  # never a guessed verdict
    res = ov.result(diags)
    assert res.capability_states["syntax"][".ts"] == SYNTAX_UNSUPPORTED


# --------------------------------------------------------------------------
# (4) rollback completeness — byte-exact, files + db (M2 target)
# --------------------------------------------------------------------------
@_needs_binary
def test_rollback_restores_every_file_and_db_byte_exact(tmp_path):
    """apply (modify + create + delete), reindex (db mutates), rollback ->
    byte-compare EVERY touched file + the db against the snapshot."""
    root, db = str(tmp_path / "repo"), str(tmp_path / "graph.db")
    _write(root, "m.py", _PY_MAIN)
    _write(root, "gone.py", "def gone_sym():\n    return 3\n")
    _index_full(root, db)

    orig = {
        "m.py": _read_b(root, "m.py"),
        "gone.py": _read_b(root, "gone.py"),
        "created.py": None,
    }
    db_orig = _read_b(str(tmp_path), "graph.db")
    pre_sha = _sha(db)

    ov = EditOverlay(root, db, gt_index_bin=_GT_INDEX)
    ov.snapshot({
        "m.py": _PY_MAIN_V2,                 # modified
        "created.py": "def made():\n    return 4\n",  # created
        "gone.py": None,                     # deleted
    })
    ov.apply()
    ov.reindex()                             # db bytes change here (real reindex)
    assert _sha(db) != pre_sha               # precondition: rollback has real work
    ov.rollback()
    res = ov.result()

    # BYTE-EXACT restoration of every touched file (None == must not exist).
    for rel, want in orig.items():
        got = _read_b(root, rel)
        assert got == want, f"rollback incomplete for {rel!r}"
    # ... and of the db.
    assert _read_b(str(tmp_path), "graph.db") == db_orig
    assert _sha(db) == pre_sha
    # the world is unchanged -> the revision must NOT advance.
    assert res.rolled_back and not res.committed
    assert res.post_revision == res.pre_revision == pre_sha
    # a COMPLETE rollback attests itself as complete (F1 counterpart).
    assert res.capability_states["rollback"] == "complete"
    assert res.capability_states["rollback_failed"] == []
    # backup temp file is cleaned up.
    assert not [f for f in os.listdir(str(tmp_path)) if ".gtoverlay." in f]


def test_rollback_failure_is_flagged_not_silent(tmp_path):
    """F1 (Fable review): a file made READ-ONLY mid-transaction -> the restore write
    fails. rollback() must NOT crash, must NOT claim the world was restored: the
    result surfaces capability_states['rollback']='incomplete' + the failed path,
    and post_revision must ADVANCE (the disk still holds the edit — stale facts
    must invalidate; pre_revision would be a false attestation)."""
    root, db = str(tmp_path / "repo"), str(tmp_path / "graph.db")
    _write(root, "locked.py", _PY_MAIN)
    with open(db, "wb") as fh:
        fh.write(b"seed-db")

    ov = EditOverlay(root, db, gt_index_bin="gt-index-definitely-not-installed")
    ov.snapshot({"locked.py": _PY_MAIN_V2})
    ov.apply()
    ov.reindex()

    tgt = os.path.join(root, "locked.py")
    os.chmod(tgt, stat.S_IREAD)               # hostile: read-only mid-transaction
    # platform guard: if this platform/user can still write (e.g. root), skip.
    try:
        with open(tgt, "ab"):
            pass
        os.chmod(tgt, stat.S_IREAD | stat.S_IWRITE)
        pytest.skip("platform does not enforce read-only files for this user")
    except PermissionError:
        pass
    try:
        ov.rollback()                          # must not raise
        res = ov.result()
    finally:
        os.chmod(tgt, stat.S_IREAD | stat.S_IWRITE)

    assert res.rolled_back                     # the attempt happened...
    assert res.reason == "rollback_incomplete"  # ...but it is NOT claimed complete
    assert res.capability_states["rollback"] == "incomplete"
    assert "locked.py" in res.capability_states["rollback_failed"]
    # disk still holds the EDIT -> the revision MUST advance (never a false
    # "world restored" attestation).
    assert _read_b(root, "locked.py") == _PY_MAIN_V2.encode("utf-8")
    assert res.post_revision != res.pre_revision


def test_rollback_prunes_created_directories(tmp_path):
    """F2 (Fable review): apply() created newpkg/sub/ for a new file; rollback must
    remove the empty created directories too — an empty leftover dir is an
    importable namespace package (a behavioral difference)."""
    root, db = str(tmp_path / "repo"), str(tmp_path / "graph.db")
    _write(root, "keep.py", "def keep():\n    return 0\n")
    with open(db, "wb") as fh:
        fh.write(b"seed-db")

    ov = EditOverlay(root, db, gt_index_bin="gt-index-definitely-not-installed")
    ov.snapshot({"newpkg/sub/newfile.py": _PY_MAIN})
    ov.apply()
    assert os.path.isfile(os.path.join(root, "newpkg", "sub", "newfile.py"))
    ov.reindex()
    ov.rollback()
    res = ov.result()

    assert not os.path.exists(os.path.join(root, "newpkg"))   # no residue at all
    assert os.path.isfile(os.path.join(root, "keep.py"))      # neighbors untouched
    assert res.capability_states["rollback"] == "complete"
    assert res.post_revision == res.pre_revision


def test_rollback_keeps_created_dir_with_foreign_content(tmp_path):
    """F2 guard-rail: a created dir that gained a FOREIGN file mid-transaction is
    NOT deleted (never destroy content the overlay did not write); the empty
    created SUBdir is still pruned, and the rollback is still complete."""
    root, db = str(tmp_path / "repo"), str(tmp_path / "graph.db")
    with open(db, "wb") as fh:
        fh.write(b"seed-db")

    ov = EditOverlay(root, db, gt_index_bin="gt-index-definitely-not-installed")
    ov.snapshot({"newpkg/sub/newfile.py": _PY_MAIN})
    ov.apply()
    _write(root, "newpkg/foreign.txt", "someone else's file\n")  # foreign content
    ov.reindex()
    ov.rollback()
    res = ov.result()

    assert not os.path.exists(os.path.join(root, "newpkg", "sub"))      # empty -> pruned
    assert os.path.isfile(os.path.join(root, "newpkg", "foreign.txt"))  # preserved
    assert res.capability_states["rollback"] == "complete"


def test_edit_signature_is_length_framed_no_delimiter_injection():
    """F3 (Fable review): the old '\\n'-joined 'path:digest' rows collided
    {'a': None, 'b': X} with {'a:deleted\\nb': X}. Length-framed fields (mirror of
    gt-index revEncodeRecord) make the framing injection-proof."""
    sig_two_rows = eo._edit_signature({"a": None, "b": b"X"})
    sig_one_row = eo._edit_signature({"a:deleted\nb": b"X"})
    assert sig_two_rows != sig_one_row                        # the exact reviewer pair
    # ':' alone in a path must not collide either.
    assert eo._edit_signature({"a:b": b"X"}) != eo._edit_signature({"a": b"X"})
    # sanctioned identity: same content -> same signature (deterministic).
    assert eo._edit_signature({"m.py": b"abc"}) == eo._edit_signature({"m.py": b"abc"})
    # deletion vs content row stays distinct.
    assert eo._edit_signature({"a": None}) != eo._edit_signature({"a": b"deleted"})


def test_rollback_deletes_db_created_during_transaction(tmp_path):
    """No db at snapshot + a (stub) transaction that creates one -> rollback removes it."""
    root, db = str(tmp_path / "repo"), str(tmp_path / "graph.db")
    _write(root, "m.py", _PY_MAIN)

    def creating_executor(cmd, cwd, timeout):
        if os.path.basename(str(cmd[0])).startswith("gt-index"):
            with open(db, "wb") as fh:      # simulates gt-index creating -output
                fh.write(b"fake-db-bytes")
        return (0, "", "")

    ov = EditOverlay(root, db, executor=creating_executor)
    ov.snapshot({"m.py": _PY_MAIN_V2})
    ov.apply()
    ov.reindex()
    assert os.path.isfile(db)
    ov.rollback()
    assert not os.path.isfile(db)            # db did not exist at snapshot -> removed
    assert _read_b(root, "m.py") == _PY_MAIN.encode("utf-8")


# --------------------------------------------------------------------------
# (5) missing binary => UNKNOWN capability, stale graph FLAGGED, overlay functions
# --------------------------------------------------------------------------
def test_missing_binary_flags_stale_but_overlay_functions(tmp_path, monkeypatch):
    root, db = str(tmp_path / "repo"), str(tmp_path / "graph.db")
    _write(root, "m.py", _PY_MAIN)
    with open(db, "wb") as fh:
        fh.write(b"pre-edit-db")
    pre_sha = _sha(db)

    # force TOTAL binary absence (no PATH hit, no <cwd>/gt-index probe, no env).
    monkeypatch.delenv("GT_INDEX_BIN", raising=False)
    monkeypatch.setattr(eo.shutil, "which", lambda _n: None)
    monkeypatch.chdir(tmp_path)

    ov = EditOverlay(root, db)               # no executor, no explicit binary
    ov.snapshot({"m.py": _PY_MAIN_V2})
    ov.apply()
    reindex_state, graph_state = ov.reindex()
    diags = ov.diagnostics_delta()
    contract = ov.contract_delta()
    ov.commit()
    res = ov.result(diags, contract)

    assert reindex_state == REINDEX_UNAVAILABLE
    assert graph_state == GRAPH_STALE
    assert res.graph_stale                   # the stale graph is FLAGGED, not hidden
    assert res.capability_states["contract"] == CONTRACT_STALE_GRAPH
    # the overlay still functioned: diagnostics ran, edit retained.
    assert diags == []
    assert _read_b(root, "m.py") == _PY_MAIN_V2.encode("utf-8")
    # REVISION HONESTY: the db bytes are frozen, but post_revision MUST advance
    # (edit signature folded in) so old graph facts are invalidated as stale.
    assert _sha(db) == pre_sha               # db untouched...
    assert res.post_revision != res.pre_revision  # ...yet the revision moved


# --------------------------------------------------------------------------
# (6) executor routing — frozen triple + CLI contract; no subprocess spawned
# --------------------------------------------------------------------------
def test_reindex_routes_through_executor_and_never_spawns(tmp_path, monkeypatch):
    def _boom(*_a, **_k):
        raise AssertionError("subprocess spawned on the executor path")

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)

    root, db = str(tmp_path / "repo"), str(tmp_path / "graph.db")
    _write(root, "m.py", _PY_MAIN)
    with open(db, "wb") as fh:
        fh.write(b"seed-db")

    record: list = []
    ov = EditOverlay(root, db, executor=_ok_executor_factory(record),
                     reindex_timeout=123)
    ov.snapshot({"m.py": _PY_MAIN_V2})
    ov.apply()
    reindex_state, graph_state = ov.reindex()

    assert reindex_state == REINDEX_INCREMENTAL and graph_state == GRAPH_FRESH
    gt_calls = [
        (cmd, cwd, to) for cmd, cwd, to in record
        if os.path.basename(str(cmd[0])).startswith("gt-index")
    ]
    # the frozen CLI contract: gt-index -root R -output G.db -file F, run in the
    # repo root with the configured timeout — through the executor triple.
    assert gt_calls == [
        (["gt-index", "-root", root, "-output", db, "-file", "m.py"], root, 123)
    ]


def test_executor_failure_degrades_to_unavailable_never_raises(tmp_path):
    root, db = str(tmp_path / "repo"), str(tmp_path / "graph.db")
    _write(root, "m.py", _PY_MAIN)
    with open(db, "wb") as fh:
        fh.write(b"seed-db")

    def flaky(cmd, cwd, timeout):
        if os.path.basename(str(cmd[0])).startswith("gt-index"):
            raise OSError("container gone")
        return (0, "", "")

    ov = EditOverlay(root, db, executor=flaky)
    ov.snapshot({"m.py": _PY_MAIN_V2})
    ov.apply()
    reindex_state, graph_state = ov.reindex()   # must not raise
    assert reindex_state == REINDEX_UNAVAILABLE
    assert graph_state == GRAPH_STALE


def test_executor_contract_violation_is_failure_not_crash(tmp_path):
    root, db = str(tmp_path / "repo"), str(tmp_path / "graph.db")
    _write(root, "m.py", _PY_MAIN)
    with open(db, "wb") as fh:
        fh.write(b"seed-db")

    ov = EditOverlay(root, db, executor=lambda c, w, t: "not-a-tuple")
    ov.snapshot({"m.py": _PY_MAIN_V2})
    ov.apply()
    reindex_state, _ = ov.reindex()
    assert reindex_state == REINDEX_UNAVAILABLE


# --------------------------------------------------------------------------
# (7) revisions + stale-fact invalidation (pure helper)
# --------------------------------------------------------------------------
def test_mark_stale_envelopes_pure_invalidation():
    post = "b" * 64
    envs = [
        {"fact_id": "f1", "valid_until": "a" * 64},          # differs -> stale
        {"fact_id": "f2", "valid_until": post},               # matches -> fresh
        {"fact_id": "f3", "graph_revision": "a" * 64},        # fallback token -> stale
        {"fact_id": "f4"},                                    # unprovable -> NOT stale
        {"fact_id": "f5", "valid_until": ""},                 # unprovable -> NOT stale
    ]
    snapshot = [dict(e) for e in envs]
    out = mark_stale_envelopes(envs, post)
    assert [d["stale"] for d in out] == [True, False, True, False, False]
    assert envs == snapshot                                   # inputs not mutated
    assert [d["fact_id"] for d in out] == ["f1", "f2", "f3", "f4", "f5"]  # order kept
    # empty post_revision: staleness unprovable for everyone.
    assert all(d["stale"] is False for d in mark_stale_envelopes(envs, ""))


# --------------------------------------------------------------------------
# (8) language-matrix honesty
# --------------------------------------------------------------------------
def test_capability_matrix_static_honesty():
    m = capability_matrix()
    assert m[".py"] == "supported"
    assert m[".go"] == "supported"
    assert m[".js"] == "supported"
    assert m[".rb"] == "supported"
    assert m[".ts"] == "supported"                            # esbuild --write=false
    assert m[".tsx"] == "supported"
    for ext in (".jsx", ".rs", ".java"):
        assert m[ext] == "unsupported", ext                   # NEVER guessed clean
    assert list(m) == sorted(m)                               # deterministic order


def test_toolchain_missing_is_reported_not_guessed_clean(tmp_path):
    """A SUPPORTED language whose tool is absent must surface toolchain_missing —
    never 'available' and never a fabricated ok/clean verdict."""
    root, db = str(tmp_path / "repo"), str(tmp_path / "graph.db")
    _write(root, "main.go", _GO_MAIN)

    def no_toolchain(cmd, cwd, timeout):
        raise FileNotFoundError(cmd[0])      # every toolchain probe fails

    ov = EditOverlay(root, db, executor=no_toolchain)
    ov.snapshot({"main.go": _GO_MAIN_V2})
    ov.apply()
    ov.reindex()
    diags = ov.diagnostics_delta()
    res = ov.result(diags)
    assert diags == []                                        # no guessed verdict
    assert res.capability_states["syntax"][".go"] == SYNTAX_TOOLCHAIN_MISSING


def test_deleted_file_syntax_state_uses_before_verdict(tmp_path):
    """Deleting a .py file: the BEFORE check ran (real verdict) -> .py reports
    available, not toolchain_missing (the 'absent' after-state is not a tool state)."""
    root, db = str(tmp_path / "repo"), str(tmp_path / "graph.db")
    _write(root, "dead.py", "def dead():\n    return 0\n")
    ov = EditOverlay(root, db, gt_index_bin="gt-index-definitely-not-installed")
    ov.snapshot({"dead.py": None})
    ov.apply()
    ov.reindex()
    diags = ov.diagnostics_delta()
    res = ov.result(diags)
    assert res.capability_states["syntax"][".py"] == SYNTAX_AVAILABLE


# --------------------------------------------------------------------------
# (9) determinism — identical inputs => identical result dict; wallclock-free
# --------------------------------------------------------------------------
def _run_repeatable_transaction(base: str) -> dict:
    """One full transaction with REAL in-process diagnostics (executor=None) and a
    deliberately-absent gt-index binary: every result field is then a pure function
    of file/db CONTENT (edit-signature fold), independent of the tree's path."""
    root, db = os.path.join(base, "repo"), os.path.join(base, "graph.db")
    _write(root, "m.py", _PY_MAIN)
    _write(root, "b.py", _PY_BROKEN)          # baseline error (stays unattributed)
    with open(db, "wb") as fh:
        fh.write(b"identical-db-bytes")
    ov = EditOverlay(root, db, gt_index_bin="gt-index-definitely-not-installed")
    ov.snapshot({
        "m.py": _PY_MAIN_V2,
        "b.py": _PY_BROKEN + "# edited\n",
        "n.py": "def n(:\n    pass\n",        # NEW error (attributed)
    })
    ov.apply()
    ov.reindex()
    diags = ov.diagnostics_delta()
    contract = ov.contract_delta()
    ov.commit()
    return ov.result(diags, contract).to_dict()


def test_result_is_deterministic_and_wallclock_free(tmp_path):
    d1 = _run_repeatable_transaction(str(tmp_path / "one"))
    d2 = _run_repeatable_transaction(str(tmp_path / "two"))
    assert d1 == d2                                            # bit-identical result
    # sorted, stable orderings everywhere.
    assert d1["edited_files"] == sorted(d1["edited_files"])
    files = [x["file"] for x in d1["diagnostics_delta"]]
    assert files == sorted(files) == ["n.py"]
    # wallclock-free: no timing/duration field anywhere in the result.
    flat = str(sorted(d1.keys())) + str(sorted(d1["capability_states"].keys()))
    for token in ("time", "duration", "elapsed", "timestamp"):
        assert token not in flat.lower()


# --------------------------------------------------------------------------
# (10) flag gate + decide hook (apply_edit_transaction)
# --------------------------------------------------------------------------
def test_flag_off_touches_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("GT_EDIT_OVERLAY", raising=False)
    root, db = str(tmp_path / "repo"), str(tmp_path / "graph.db")
    _write(root, "m.py", _PY_MAIN)

    res = apply_edit_transaction(root, db, {"m.py": _PY_MAIN_V2})
    assert res.disabled and res.reason == "flag_off"
    assert not res.committed and not res.rolled_back
    assert _read_b(root, "m.py") == _PY_MAIN.encode("utf-8")   # nothing written
    assert not os.path.isfile(db)                              # nothing indexed


def test_flag_on_decide_false_rolls_back(tmp_path, monkeypatch):
    monkeypatch.setenv("GT_EDIT_OVERLAY", "1")
    root, db = str(tmp_path / "repo"), str(tmp_path / "graph.db")
    _write(root, "m.py", _PY_MAIN)

    res = apply_edit_transaction(
        root, db, {"m.py": "def alpha(:\n    pass\n"},         # a NEW syntax error
        gt_index_bin="gt-index-definitely-not-installed",      # real in-proc ast.parse
        decide=lambda diags, *_: not diags,                    # reject on new error
    )
    assert res.rolled_back and not res.committed
    assert [d.file for d in res.diagnostics_delta] == ["m.py"]
    assert _read_b(root, "m.py") == _PY_MAIN.encode("utf-8")   # byte-restored
    assert res.post_revision == res.pre_revision


def test_flag_on_default_commits(tmp_path, monkeypatch):
    monkeypatch.setenv("GT_EDIT_OVERLAY", "true")
    root, db = str(tmp_path / "repo"), str(tmp_path / "graph.db")
    _write(root, "m.py", _PY_MAIN)
    res = apply_edit_transaction(
        root, db, {"m.py": _PY_MAIN_V2}, executor=_ok_executor_factory([]),
    )
    assert res.committed and not res.rolled_back               # fail-open default
    assert _read_b(root, "m.py") == _PY_MAIN_V2.encode("utf-8")


# --------------------------------------------------------------------------
# (11) contract-delta wiring — patch_delta consumed at the post-edit state
# --------------------------------------------------------------------------
def test_contract_delta_passes_before_after_and_new_graph(tmp_path, monkeypatch):
    captured: dict = {}

    def spy(edited_files, repo_root, graph_db):
        captured["edited_files"] = dict(edited_files)
        captured["repo_root"] = repo_root
        captured["graph_db"] = graph_db
        from groundtruth.runtime.patch_delta import PatchDeltaResult
        return PatchDeltaResult()

    monkeypatch.setattr(eo, "analyze_patch_delta", spy)

    root, db = str(tmp_path / "repo"), str(tmp_path / "graph.db")
    _write(root, "m.py", _PY_MAIN)
    with open(db, "wb") as fh:
        fh.write(b"db")
    ov = EditOverlay(root, db, executor=_ok_executor_factory([]))
    ov.snapshot({"m.py": _PY_MAIN_V2, "new.py": "def fresh():\n    return 9\n"})
    ov.apply()
    ov.reindex()
    ov.contract_delta()

    assert captured["repo_root"] == root and captured["graph_db"] == db
    assert captured["edited_files"]["m.py"] == (_PY_MAIN, _PY_MAIN_V2)
    before, after = captured["edited_files"]["new.py"]
    assert before is None                                      # NEW file contract
    assert after == "def fresh():\n    return 9\n"


if __name__ == "__main__":
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q"]))
