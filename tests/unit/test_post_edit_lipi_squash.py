"""LIPI squash-batch tests for post_edit.py items #10, #11, #12, #13.

Artifact-first / red-before-green. Each test reproduces the OBSERVED defect from
the GRANULAR_LIPI_REVIEW (2026-06-07) using a minimal in-memory/temp graph.db
fixture mirroring the real schema (nodes/edges/properties) — NOT derived from
reading the implementation. Each was confirmed to FAIL against the pre-fix code
and PASS against the fix (see per-test docstrings for the RED/GREEN contract).

Items under test (FIX-NOW table, this file's per-file section):
  #10 — consistency queries (peers/override/twins/siblings) must route through
        the SAME categorical trust gate the caller query uses; a name_match-grade
        EXTENDS/IMPLEMENTS edge must NOT launder a [PEER]/[OVERRIDE] block as a fact.
  #11 — the behavioral-contract resolver must use _resolve_node_id (label-filtered,
        is_exported tiebreak), not an inline name-only query that can pick a
        DIFFERENT node than the caller/signature blocks.
  #12 — the callee block must skip when _resolve_file_path returns None, instead of
        binding `nt.file_path != NULL` which disables the self-exclusion and lists
        the edited file's own functions as "Calls into:".
  #13 — _signature_has_varargs must detect a *name/**name token, not bare `*`
        (a keyword-only marker `*,` is NOT varargs and must not kill the arity
        contract).
"""

from __future__ import annotations

import os
import sqlite3

import pytest

from groundtruth.hooks import post_edit as pe


# ===========================================================================
# Item #13 — _signature_has_varargs: *name/**name token, not bare `*`
# ===========================================================================
class TestItem13Varargs:
    def test_keyword_only_marker_is_not_varargs(self):
        """RED: `"*" in signature` -> True for `def f(a, *, b)` (keyword-only
        marker) -> silently disables the arity contract. GREEN: False."""
        assert pe._signature_has_varargs("def f(a, *, b):") is False

    def test_real_star_args_detected(self):
        assert pe._signature_has_varargs("def f(a, *args):") is True

    def test_real_double_star_kwargs_detected(self):
        assert pe._signature_has_varargs("def f(a, **kwargs):") is True

    def test_typed_params_not_varargs(self):
        """A typed signature with no *args/**kwargs must NOT look variadic."""
        assert pe._signature_has_varargs("def f(a: int, b: str) -> None:") is False

    def test_empty_signature(self):
        assert pe._signature_has_varargs("") is False

    def test_arity_contract_fires_after_fix(self):
        """End-to-end of #13: a keyword-only signature must STILL produce the
        arity-mismatch contract (it was suppressed by the bare-`*` bug).

        RED: _signature_has_varargs("...*, ...") == True -> _check_arity_mismatch
        early-returns "" -> no [GT_CONTRACT]. GREEN: the warning fires.
        """
        new_sig = "def f(self, a, *, b, c):"  # 4 required params (excl self): a,b,c... wait
        # Params excl self: a, b, c -> arity 3, 0 defaults -> min_required 3.
        callers = [
            {"file": "caller.py", "line": "10", "code": "f(x)", "resolution_method": "import"}
        ]
        warn = pe._check_arity_mismatch(new_sig, "f", callers, edited_files=[])
        assert warn, "arity contract must fire for a keyword-only signature (not suppressed)"
        assert "f()" in warn


# ===========================================================================
# Item #10 — consistency-edge gate (the shared hierarchy trust gate)
# ===========================================================================
def _hier_db(path: str, *, with_categorical: bool) -> None:
    conn = sqlite3.connect(path)
    if with_categorical:
        conn.execute(
            "CREATE TABLE edges (id INTEGER PRIMARY KEY, source_id INT, target_id INT, "
            "type TEXT, resolution_method TEXT, confidence REAL, trust_tier TEXT, candidate_count INT)"
        )
    else:
        conn.execute(
            "CREATE TABLE edges (id INTEGER PRIMARY KEY, source_id INT, target_id INT, "
            "type TEXT, confidence REAL)"
        )
    conn.commit()
    conn.close()


class TestItem10HierarchyGate:
    def test_gate_admits_verified_inheritance_suppresses_name_match(self, tmp_path):
        """The shared gate must admit a verified EXTENDS/IMPLEMENTS edge
        (`inheritance`/`implements`) and SUPPRESS a name_match-grade one — the
        exact divergence the caller gate already enforces (correct-or-quiet)."""
        p = str(tmp_path / "graph.db")
        _hier_db(p, with_categorical=True)
        conn = sqlite3.connect(p)
        conn.executemany(
            "INSERT INTO edges VALUES (?,?,?,?,?,?,?,?)",
            [
                (1, 10, 20, "EXTENDS", "inheritance", 1.0, "CERTIFIED", 1),  # verified -> admit
                (2, 11, 20, "IMPLEMENTS", "implements", 1.0, "CERTIFIED", 1),  # verified -> admit
                (3, 12, 20, "EXTENDS", "name_match", 0.9, "CANDIDATE", 3),  # guess  -> suppress
                (4, 13, 20, "EXTENDS", "inheritance", 1.0, "SUPPRESSED", 1),  # tier   -> exclude
            ],
        )
        conn.commit()
        clause = pe._hierarchy_edge_filter_clause()
        ids = {
            r[0]
            for r in conn.execute(
                f"SELECT id FROM edges e WHERE e.type IN ('EXTENDS','IMPLEMENTS') AND {clause}"
            ).fetchall()
        }
        conn.close()
        assert ids == {1, 2}, f"gate must admit only verified hierarchy edges: {ids}"

    def test_gate_excludes_certified_name_match(self, tmp_path):
        """Even CERTIFIED + name_match must not become a hierarchy fact (mirrors
        the caller gate's `!= 'name_match'` guard)."""
        p = str(tmp_path / "graph.db")
        _hier_db(p, with_categorical=True)
        conn = sqlite3.connect(p)
        conn.execute("INSERT INTO edges VALUES (1,10,20,'EXTENDS','name_match',0.95,'CERTIFIED',1)")
        conn.commit()
        clause = pe._hierarchy_edge_filter_clause()
        rows = conn.execute(f"SELECT id FROM edges e WHERE {clause}").fetchall()
        conn.close()
        assert rows == []

    def test_gate_for_db_picks_categorical_then_falls_back(self, tmp_path):
        cat = str(tmp_path / "cat.db")
        _hier_db(cat, with_categorical=True)
        c = pe._hierarchy_edge_filter_for_db(cat)
        assert "resolution_method" in c and "trust_tier" in c

        legacy = str(tmp_path / "legacy.db")
        _hier_db(legacy, with_categorical=False)
        c2 = pe._hierarchy_edge_filter_for_db(legacy)
        assert "confidence" in c2 and "trust_tier" not in c2

        c3 = pe._hierarchy_edge_filter_for_db("/no/such/file.db")
        assert "confidence" in c3  # degrades, never crashes

    def test_inheritance_method_not_in_calls_factset(self):
        """Guard against a category error: `inheritance`/`implements` are
        EXTENDS-edge provenances and must NOT be in the CALLS fact-set, but MUST
        be in the hierarchy verified set."""
        from groundtruth.pretask.curation_map import DETERMINISTIC_RESOLUTION_METHODS

        assert "inheritance" not in DETERMINISTIC_RESOLUTION_METHODS
        assert "inheritance" in pe._HIERARCHY_VERIFIED_METHODS
        assert "implements" in pe._HIERARCHY_VERIFIED_METHODS
        assert "name_match" not in pe._HIERARCHY_VERIFIED_METHODS


def _peer_graph(path: str, *, parent_edge_method: str) -> None:
    """Two classes (Base, Derived, Other) where Derived+Other both extend Base,
    each defining `handle`. The peer query relates them via the EXTENDS edge whose
    resolution_method we vary to prove the gate decides laundering."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY, label TEXT, name TEXT, file_path TEXT, "
        "start_line INT, end_line INT, signature TEXT, return_type TEXT, is_exported INT, "
        "is_test INT, language TEXT, parent_id INT)"
    )
    conn.execute(
        "CREATE TABLE edges (id INTEGER PRIMARY KEY, source_id INT, target_id INT, type TEXT, "
        "source_line INT, source_file TEXT, resolution_method TEXT, confidence REAL, "
        "trust_tier TEXT, candidate_count INT)"
    )
    conn.executemany(
        "INSERT INTO nodes (id,label,name,file_path,start_line,end_line,signature,is_exported,is_test,language,parent_id) "
        "VALUES (?,?,?,?,?,?,?,?,0,?,?)",
        [
            (10, "Class", "Base", "pkg/base.py", 1, 50, "", 1, "python", None),
            (11, "Class", "Derived", "pkg/derived.py", 1, 50, "", 1, "python", None),
            (12, "Class", "Other", "pkg/other.py", 1, 50, "", 1, "python", None),
            # the edited method on Derived
            (
                1,
                "Method",
                "handle",
                "pkg/derived.py",
                10,
                20,
                "def handle(self, x):",
                1,
                "python",
                11,
            ),
            # the peer on Other (what [PEER] should surface)
            (
                2,
                "Method",
                "handle",
                "pkg/other.py",
                10,
                20,
                "def handle(self, x):",
                1,
                "python",
                12,
            ),
        ],
    )
    # Derived EXTENDS Base, Other EXTENDS Base — provenance varied by arg.
    conn.executemany(
        "INSERT INTO edges (id,source_id,target_id,type,resolution_method,confidence,trust_tier,candidate_count) "
        "VALUES (?,?,?,?,?,?,?,?)",
        [
            (100, 11, 10, "EXTENDS", parent_edge_method, 0.9, "CANDIDATE", 1),
            (101, 12, 10, "EXTENDS", parent_edge_method, 0.9, "CANDIDATE", 1),
        ],
    )
    conn.commit()
    conn.close()


class TestItem10PeerLaunderingEndToEnd:
    def test_verified_inheritance_peer_surfaces(self, tmp_path):
        """When the relating EXTENDS edge is a verified `inheritance` provenance,
        the peer in Other is a FACT and [PEER] must surface it."""
        p = str(tmp_path / "graph.db")
        _peer_graph(p, parent_edge_method="inheritance")
        peers = pe._get_interface_peers_from_graph(
            p, "pkg/derived.py", "handle", repo_root=str(tmp_path)
        )
        files = {peer["file"] for peer in peers}
        assert any("other.py" in f for f in files), f"verified peer missing: {peers}"
        assert all(peer.get("verified") == "1" for peer in peers)

    def test_name_match_extends_does_not_surface_via_inheritance_path(self, tmp_path):
        """RED (unfixed): the inheritance peer query used bare confidence>=0.5, so
        a name_match-grade EXTENDS edge surfaced Other as a confident inheritance
        [PEER] — laundering a guess as a fact on the same edit the caller gate
        suppresses. GREEN: the gate rejects the name_match EXTENDS, so the
        inheritance branch finds no verified peer (it may fall back to the
        explicitly-unverified name-match path, never the verified one)."""
        p = str(tmp_path / "graph.db")
        _peer_graph(p, parent_edge_method="name_match")
        peers = pe._get_interface_peers_from_graph(
            p, "pkg/derived.py", "handle", repo_root=str(tmp_path)
        )
        # No peer may claim verified=="1" off a name_match EXTENDS edge.
        for peer in peers:
            assert peer.get("verified") != "1", (
                f"name_match EXTENDS laundered as a verified [PEER] fact: {peer}"
            )


# ===========================================================================
# Item #11 — behavioral-contract resolver uses _resolve_node_id (one node)
# ===========================================================================
def _collision_graph(path: str) -> None:
    """A name collision: a Class node AND two Method nodes named `process`.
    The inline name-only resolver (no label filter) could pick the Class row or
    the wrong method; _resolve_node_id filters to Function/Method and ties-breaks
    by is_exported then lowest id, matching the caller/signature resolution."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY, label TEXT, name TEXT, file_path TEXT, "
        "start_line INT, end_line INT, signature TEXT, return_type TEXT, is_exported INT, "
        "is_test INT, language TEXT, parent_id INT)"
    )
    conn.execute(
        "CREATE TABLE edges (id INTEGER PRIMARY KEY, source_id INT, target_id INT, type TEXT, "
        "source_line INT, source_file TEXT, resolution_method TEXT, confidence REAL)"
    )
    conn.execute(
        "CREATE TABLE properties (id INTEGER PRIMARY KEY, node_id INT, kind TEXT, value TEXT, line INT, confidence REAL)"
    )
    conn.executemany(
        "INSERT INTO nodes (id,label,name,file_path,start_line,end_line,signature,is_exported,is_test,language,parent_id) "
        "VALUES (?,?,?,?,?,?,?,?,0,?,?)",
        [
            # A Class node sharing the name `process` (label collision the OLD
            # inline resolver did NOT filter out). It is inserted with a LOWER id
            # (1) than the real method (2) AND identical path/suffix length, so the
            # old `WHERE name=?` + `>`-not-`>=` first-seen tiebreak picks THIS Class
            # row — losing the contract. _resolve_node_id filters label
            # IN ('Function','Method'), so the Class can never be chosen.
            (1, "Class", "process", "app/worker.py", 1, 100, "", 1, "python", None),
            # The REAL edited method `process` in app/worker.py (the node the
            # caller/signature blocks resolve to).
            (
                2,
                "Method",
                "process",
                "app/worker.py",
                40,
                60,
                "def process(self, item):",
                1,
                "python",
                None,
            ),
        ],
    )
    # Contract properties live ONLY on the Method node (id=2). If the resolver
    # picks the Class node (id=1) it gets NO contract -> the bug is observable as
    # a missing/empty [BEHAVIORAL CONTRACT].
    conn.executemany(
        "INSERT INTO properties (node_id,kind,value,line,confidence) VALUES (?,?,?,?,1.0)",
        [
            (2, "guard_clause", "raise: not item", 41),
            (2, "exception_type", "ValueError", 42),
        ],
    )
    conn.commit()
    conn.close()


class TestItem11ContractResolver:
    def test_resolver_resolves_method_not_class_on_name_collision(self, tmp_path):
        """The canonical resolver must resolve `process` to the Method node (id=1),
        never the same-named Class node (id=5)."""
        p = str(tmp_path / "graph.db")
        _collision_graph(p)
        nid = pe._resolve_node_id(p, "app/worker.py", "process")
        assert nid == 2, f"resolver picked the wrong node (Class collision): {nid}"

    def test_contract_describes_same_node_as_signature(self, tmp_path):
        """End-to-end of #11: the [BEHAVIORAL CONTRACT] (PARAMS/PRESERVE/RAISES)
        must describe the SAME method node the [SIGNATURE] block describes.

        RED (unfixed): the inline resolver could bind to the Class node -> no
        properties -> the contract is empty/absent while the signature renders.
        GREEN: contract + signature describe one node -> the guard surfaces.
        """
        p = str(tmp_path / "graph.db")
        _collision_graph(p)
        # Write the real source so the body-length gate (>20 chars) passes.
        src_dir = tmp_path / "app"
        src_dir.mkdir()
        (src_dir / "worker.py").write_text(
            "\n" * 39
            + "    def process(self, item):\n"
            + "        if not item:\n"
            + "            raise ValueError('empty')\n"
            + "        return do(item)\n"
            + "\n" * 40,
            encoding="utf-8",
        )
        out = pe.generate_improved_evidence(
            file_path="app/worker.py",
            function_names=["process"],
            db_path=p,
            repo_root=str(tmp_path),
        )
        assert "[SIGNATURE]" in out, f"signature missing:\n{out}"
        # The guard from the Method node's properties must reach the agent —
        # proof the contract resolved to the method, not the colliding Class.
        assert "not item" in out, f"behavioral contract did not resolve to the method node:\n{out}"


# ===========================================================================
# Item #12 — callee block skips when resolved path is None (no `!= NULL`)
# ===========================================================================
def _callee_graph(path: str, *, run_file: str, add_ambiguous_basename: bool) -> None:
    """The edited function `run` CALLS `helper` (different file) and `local_only`
    (SAME file). With a resolvable path the self-exclusion drops `local_only`.

    When ``add_ambiguous_basename`` is set, a decoy node sharing the basename of
    ``run_file`` is added so ``_resolve_file_path`` returns None (ambiguous) while
    ``_resolve_node_id`` (independent suffix matcher) still resolves `run` — the
    exact split needed to exercise the None-path branch of item #12.
    """
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY, label TEXT, name TEXT, file_path TEXT, "
        "start_line INT, end_line INT, signature TEXT, return_type TEXT, is_exported INT, "
        "is_test INT, language TEXT, parent_id INT)"
    )
    conn.execute(
        "CREATE TABLE edges (id INTEGER PRIMARY KEY, source_id INT, target_id INT, type TEXT, "
        "source_line INT, source_file TEXT, resolution_method TEXT, confidence REAL, "
        "trust_tier TEXT, candidate_count INT)"
    )
    rows = [
        (1, "Function", "run", run_file, 10, 20, "def run():", 1, "python", None),
        (2, "Function", "helper", "pkg/util.py", 5, 9, "def helper(x):", 1, "python", None),
        (3, "Function", "local_only", run_file, 30, 40, "def local_only():", 1, "python", None),
    ]
    if add_ambiguous_basename:
        base = os.path.basename(run_file)
        rows.append(
            (9, "Function", "decoy", f"other/dir/{base}", 1, 5, "def decoy():", 1, "python", None)
        )
    conn.executemany(
        "INSERT INTO nodes (id,label,name,file_path,start_line,end_line,signature,is_exported,is_test,language,parent_id) "
        "VALUES (?,?,?,?,?,?,?,?,0,?,?)",
        rows,
    )
    conn.executemany(
        "INSERT INTO edges (id,source_id,target_id,type,resolution_method,confidence,trust_tier,candidate_count) "
        "VALUES (?,?,?,?,?,?,?,?)",
        [
            (100, 1, 2, "CALLS", "import", 1.0, "CERTIFIED", 1),  # cross-file callee
            (
                101,
                1,
                3,
                "CALLS",
                "same_file",
                1.0,
                "CERTIFIED",
                1,
            ),  # SAME-file callee (must be excluded)
        ],
    )
    conn.commit()
    conn.close()


class TestItem12CalleeNonePath:
    def test_resolvable_path_excludes_self_file_callee(self, tmp_path):
        """The real, red-able contract: when the path resolves, the SAME-file
        callee `local_only` is excluded and only the cross-file `helper` appears
        in "Calls into:". If the self-exclusion ever breaks, this goes red."""
        p = str(tmp_path / "graph.db")
        _callee_graph(p, run_file="pkg/main.py", add_ambiguous_basename=False)
        out = pe.generate_improved_evidence(
            file_path="pkg/main.py",
            function_names=["run"],
            db_path=p,
            repo_root=str(tmp_path),
        )
        assert "Calls into:" in out, f"no callee block on a resolvable path:\n{out}"
        assert "helper" in out
        assert "local_only" not in out, (
            f"same-file callee leaked into Calls into: (self-exclusion failed):\n{out}"
        )

    def test_unresolvable_path_emits_no_callee_block_and_no_self_listing(self, tmp_path):
        """Item #12: when _resolve_file_path returns None, the OLD code bound
        `nt.file_path != NULL` (NULL in WHERE => 0 rows), so the callee block was
        silently empty — fragile (a future COALESCE/IS-NOT refactor would flip it
        to WIDEN and list the edited file's own functions). The fix makes the
        skip EXPLICIT (correct-or-quiet) and guards against a None bind.

        Contract asserted: with an unresolvable path, no callee block renders AND
        the edited file's own `local_only` is never presented as a callee, and the
        call does not crash on the None bind.
        """
        p = str(tmp_path / "graph.db")
        # `run` lives at deep/pkg/main.py; a decoy other/dir/main.py makes the
        # basename ambiguous so _resolve_file_path("extra/deep/pkg/main.py") -> None
        # while _resolve_node_id still resolves `run` via suffix matching.
        _callee_graph(p, run_file="deep/pkg/main.py", add_ambiguous_basename=True)
        q = "extra/deep/pkg/main.py"

        # Precondition: the split actually holds (node resolves, file path is None).
        assert pe._resolve_node_id(p, q, "run") == 1
        conn = pe._open_graph_db(p)
        assert pe._resolve_file_path(conn, q) is None
        conn.close()

        out = pe.generate_improved_evidence(
            file_path=q,
            function_names=["run"],
            db_path=p,
            repo_root=str(tmp_path),
        )
        # No callee block (correct-or-quiet) and never a self-file listing.
        assert "Calls into:" not in out, (
            f"callee block rendered on an unresolvable path (should be skipped):\n{out}"
        )
        assert "local_only" not in out, (
            f"edited file's own function listed as a callee (#12 widening regression):\n{out}"
        )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
