"""Typed graph queries over graphs the real gt-index producer writes.

Every graph here is produced by compiling gt-index from this checkout and
indexing a temp fixture repository (see ``tests/_real_graph.py``), so the
queries see the real label taxonomy (including analysis-layer ``Callsite``
nodes), real framework edges (HANDLES_ROUTE / API_CALL / MIDDLEWARE_ON), real
file hashes and real resolution methods.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests._real_graph import build_gt_index, index_repo, write_repo
from groundtruth.runtime.deterministic_queries import (
    DeterministicQueryContext,
    execute_query,
)
from groundtruth.runtime.diff_impact import diff_impact
from groundtruth.runtime.observation_compiler import (
    CONFIGURATION_BINDING_SCHEMA,
    REPOSITORY_SNAPSHOT_SCHEMA,
    ActionKind,
    ActionRequest,
    ConfigurationBinding,
    Coverage,
    EvidenceSemantics,
    RepositorySnapshot,
    RequestedFidelity,
    RevisionVector,
)

SOURCE_REVISION = "workspace-rev-0001"
CONTENT_REVISION = "repo-content-rev"
WORKING_TREE = "c" * 64

_ANALYSIS_EDGE_TYPES = {"HAS_CALLSITE", "CANDIDATE_TARGET", "SELECTED_TARGET"}

_XLANG_REPO = {
    "pkg/a.py": """\
import requests


class Session:
    def get(self, url):
        return url


def helper(x):
    return x + 1


def run(sess):
    return sess.get("y")


def fetch():
    return requests.get("x")


def main():
    helper(1)
    run(Session())
    fetch()
""",
    "pkg/b.py": """\
from pkg.a import helper


def z():
    return helper(2)
""",
    "js/c.js": """\
function helper() {
  return 1;
}

function useIt() {
  return helper();
}

module.exports = { helper, useIt };
""",
    # ``_anchor`` is the file's first node: the producer targets the file
    # anchor with HANDLES_ROUTE / MIDDLEWARE_ON / API_CALL edges and drops
    # self-loops, so neither the middleware nor a handler may be first.
    "web/server.js": """\
function _anchor() {
  return 0;
}

function auth(req, res, next) {
  next();
}

function listUsers(req, res) {
  res.send([]);
}

app.use(auth);
app.get('/users', listUsers);
app.get('/orders', (req, res) => { res.send([]); });
""",
    "web/client.js": """\
function loadOrders() {
  return fetch('/orders');
}

function loadUsers() {
  return fetch('/users');
}
""",
}


@pytest.fixture(scope="session")
def gt_index(tmp_path_factory):
    binary = build_gt_index(tmp_path_factory.mktemp("gt_index_bin"))
    if binary is None:
        pytest.skip("gt-index cannot be built (no Go toolchain / no override)")
    return binary


def _graph(gt_index, tmp_path: Path, files: dict[str, str], *, source_revision=SOURCE_REVISION):
    root = write_repo(tmp_path / "repo", files)
    db = index_repo(gt_index, root, tmp_path / "graph.db", source_revision=source_revision)
    return root, db


def _snapshot(graph_revision: str = SOURCE_REVISION) -> RepositorySnapshot:
    return RepositorySnapshot(
        schema=REPOSITORY_SNAPSHOT_SCHEMA,
        repository_id="fixture",
        root_sha256="a" * 64,
        git_revision="head",
        dirty_diff_sha256="b" * 64,
        working_tree_sha256=WORKING_TREE,
        revisions=RevisionVector(
            repository_content=CONTENT_REVISION,
            graph=graph_revision,
            lsp="lsp-rev",
            runtime_evidence="rt-rev",
        ),
        configuration=ConfigurationBinding(
            schema=CONFIGURATION_BINDING_SCHEMA,
            configuration_id="fixture-config",
            inputs_sha256="d" * 64,
            language_manifest_sha256="e" * 64,
            build_system="pytest",
        ),
    )


def _run(root: Path, db: Path, kind: ActionKind, arguments: dict, *, graph_revision=SOURCE_REVISION):
    request = ActionRequest.build(
        action_id="real-1",
        kind=kind,
        arguments=arguments,
        snapshot=_snapshot(graph_revision),
        requested_fidelity=RequestedFidelity.EXACT,
    )
    context = DeterministicQueryContext(
        repository_root=root,
        graph_db=db,
        repository_content_revision=CONTENT_REVISION,
        working_tree_sha256=WORKING_TREE,
        snapshot_files=(),
        snapshot_complete=True,
    )
    artifact = execute_query(request, context)
    return artifact, json.loads(artifact.direct_answer_json)


# ---------------------------------------------------------------------------
# Graph revision identity (CANON contract)
# ---------------------------------------------------------------------------


def test_graph_revision_is_source_revision_not_build_commit(gt_index, tmp_path):
    """project_meta.git_commit is the producer build commit; binding a typed
    answer to it made the freshness check tautological.  Without
    ``source_revision`` the graph revision is unavailable -> INCOMPLETE."""
    root, db = _graph(gt_index, tmp_path, _XLANG_REPO, source_revision=None)
    artifact, _ = _run(
        root, db, ActionKind.DEFINITION, {"symbol": "run"}, graph_revision="test-commit"
    )
    assert "graph_revision_unavailable" in artifact.omissions
    assert artifact.semantics is EvidenceSemantics.INCOMPLETE


def test_graph_revision_binds_when_source_revision_matches(gt_index, tmp_path):
    root, db = _graph(gt_index, tmp_path, _XLANG_REPO)
    artifact, _ = _run(root, db, ActionKind.DEFINITION, {"symbol": "run"})
    assert artifact.semantics is EvidenceSemantics.EXACT
    stale, _ = _run(
        root, db, ActionKind.DEFINITION, {"symbol": "run"}, graph_revision="other-rev"
    )
    assert "graph_revision_mismatch" in stale.omissions


# ---------------------------------------------------------------------------
# Source-symbol label filter
# ---------------------------------------------------------------------------


def test_references_exclude_analysis_layer_nodes(gt_index, tmp_path):
    root, db = _graph(gt_index, tmp_path, _XLANG_REPO)
    artifact, answer = _run(root, db, ActionKind.REFERENCES, {"symbol": "fetch"})
    assert answer["resolved_nodes"] == 1
    assert not (_ANALYSIS_EDGE_TYPES & set(answer["references_by_type"]))
    kinds = {s["kind"] for s in answer["resolved_symbols"]}
    assert kinds == {"Function"}


def test_rename_excludes_callsite_definitions_and_substrate_edges(gt_index, tmp_path):
    root, db = _graph(gt_index, tmp_path, _XLANG_REPO)
    _artifact, answer = _run(
        root, db, ActionKind.RENAME, {"symbol": "helper", "new_name": "h2", "language": "python"}
    )
    assert {d["kind"] for d in answer["definitions"]} == {"Function"}
    assert len(answer["definitions"]) == 1
    assert not (_ANALYSIS_EDGE_TYPES & set(answer["edit_sites_by_type"]))


def test_callers_exclude_callsite_nodes(gt_index, tmp_path):
    root, db = _graph(gt_index, tmp_path, _XLANG_REPO)
    _artifact, answer = _run(root, db, ActionKind.CALLERS, {"symbol": "fetch"})
    assert answer["resolved_nodes"] == 1


# ---------------------------------------------------------------------------
# Same-name ambiguity across languages / files
# ---------------------------------------------------------------------------


def test_references_same_name_across_languages_is_ambiguous(gt_index, tmp_path):
    root, db = _graph(gt_index, tmp_path, _XLANG_REPO)
    artifact, answer = _run(root, db, ActionKind.REFERENCES, {"symbol": "helper"})
    assert artifact.semantics is EvidenceSemantics.INCOMPLETE
    assert len(artifact.ambiguity) == 2
    languages = {s["language"] for s in answer["resolved_symbols"]}
    assert languages == {"python", "javascript"}
    for rows in answer["references_by_type"].values():
        assert all(r["language"] in {"python", "javascript"} for r in rows)


def test_references_language_hint_filters_and_is_exact(gt_index, tmp_path):
    root, db = _graph(gt_index, tmp_path, _XLANG_REPO)
    artifact, answer = _run(
        root, db, ActionKind.REFERENCES, {"symbol": "helper", "language": "python"}
    )
    assert artifact.ambiguity == ()
    assert artifact.semantics is EvidenceSemantics.EXACT
    callers = {r["name"] for r in answer["references_by_type"]["CALLS"]}
    assert callers == {"main", "z"}
    assert {r["language"] for r in answer["references_by_type"]["CALLS"]} == {"python"}


def test_language_hint_never_falls_back_to_other_languages(gt_index, tmp_path):
    root, db = _graph(gt_index, tmp_path, _XLANG_REPO)
    artifact, answer = _run(
        root, db, ActionKind.DEFINITION, {"symbol": "helper", "language": "rust"}
    )
    assert "symbol_not_found" in artifact.omissions
    assert answer["definitions"] == []


def test_callers_ambiguous_symbol_is_not_exact(gt_index, tmp_path):
    root, db = _graph(gt_index, tmp_path, _XLANG_REPO)
    artifact, answer = _run(root, db, ActionKind.CALLERS, {"symbol": "helper"})
    assert artifact.semantics is EvidenceSemantics.INCOMPLETE
    assert len(artifact.ambiguity) == 2
    rows = answer["callers_by_depth"]["1"]
    assert {r["language"] for r in rows} == {"python", "javascript"}
    exact, answer_py = _run(
        root, db, ActionKind.CALLERS, {"symbol": "helper", "language": "python"}
    )
    assert exact.semantics is EvidenceSemantics.EXACT
    assert {r["name"] for r in answer_py["callers_by_depth"]["1"]} == {"main", "z"}


def test_symbol_context_reports_ambiguity_instead_of_picking_first(gt_index, tmp_path):
    root, db = _graph(gt_index, tmp_path, _XLANG_REPO)
    artifact, answer = _run(root, db, ActionKind.SYMBOL_CONTEXT, {"symbol": "helper"})
    assert artifact.semantics is EvidenceSemantics.INCOMPLETE
    assert len(artifact.ambiguity) == 2
    assert "ambiguous_symbol" in artifact.omissions
    assert len(answer["candidates"]) == 2
    single, answer1 = _run(
        root, db, ActionKind.SYMBOL_CONTEXT, {"symbol": "helper", "language": "javascript"}
    )
    assert single.ambiguity == ()
    assert answer1["definition"]["file_path"] == "js/c.js"


# ---------------------------------------------------------------------------
# Truncation is an omission; counts are pre-truncation
# ---------------------------------------------------------------------------


def _fanin_repo(n: int) -> dict[str, str]:
    body = ["def target():", "    return 1", ""]
    for i in range(n):
        body += ["", f"def c{i:02d}():", "    return target()", ""]
    return {"fan.py": "\n".join(body) + "\n"}


def test_callers_truncation_recorded_and_count_is_pre_truncation(gt_index, tmp_path):
    root, db = _graph(gt_index, tmp_path, _fanin_repo(23))
    artifact, answer = _run(root, db, ActionKind.CALLERS, {"symbol": "target", "depth": 1})
    assert answer["caller_count"] == 23
    assert answer["returned_count"] == 20
    assert len(answer["callers_by_depth"]["1"]) == 20
    assert "callers_truncated" in artifact.omissions
    assert artifact.semantics is EvidenceSemantics.INCOMPLETE


def test_references_truncation_recorded(gt_index, tmp_path):
    root, db = _graph(gt_index, tmp_path, _fanin_repo(23))
    artifact, answer = _run(root, db, ActionKind.REFERENCES, {"symbol": "target"})
    assert answer["reference_count_by_type"]["CALLS"] == 23
    assert len(answer["references_by_type"]["CALLS"]) == 20
    assert "references_truncated:CALLS" in artifact.omissions
    assert artifact.semantics is EvidenceSemantics.INCOMPLETE


def test_symbol_context_truncation_recorded(gt_index, tmp_path):
    root, db = _graph(gt_index, tmp_path, _fanin_repo(12))
    artifact, answer = _run(root, db, ActionKind.SYMBOL_CONTEXT, {"symbol": "target"})
    assert answer["caller_count"] == 12
    assert len(answer["callers"]) == 10
    assert "callers_truncated" in artifact.omissions
    assert artifact.semantics is EvidenceSemantics.INCOMPLETE


# ---------------------------------------------------------------------------
# route_map / api_impact
# ---------------------------------------------------------------------------


def test_route_map_api_call_only_route_does_not_null_the_answer(gt_index, tmp_path):
    """An inline-handler route (API_CALL, no HANDLES_ROUTE) has no handler
    line: it must not become anchor line 0 and crash the artifact."""
    root, db = _graph(gt_index, tmp_path, _XLANG_REPO)
    artifact, answer = _run(root, db, ActionKind.ROUTE_MAP, {})
    by_route = {r["route"]: r for r in answer["routes"]}
    assert by_route["/orders"]["handler"] is None
    assert by_route["/orders"]["discovered_via"] == "api_call"
    assert "route_handler_unresolved:/orders" in artifact.omissions
    assert artifact.semantics is EvidenceSemantics.INCOMPLETE
    assert all(a.line >= 1 for a in artifact.anchors)
    assert ("web/client.js", 2) in {(a.path, a.line) for a in artifact.anchors}


def test_api_impact_unfiltered_survives_api_call_only_route(gt_index, tmp_path):
    root, db = _graph(gt_index, tmp_path, _XLANG_REPO)
    artifact, answer = _run(root, db, ActionKind.API_IMPACT, {})
    assert {r["name"] for r in answer["routes"]} >= {"/orders", "/users"}
    assert all(a.line >= 1 for a in artifact.anchors)


def test_route_map_reads_path_and_method_from_edge_metadata(gt_index, tmp_path):
    """HANDLES_ROUTE metadata carries route/method; the answer must not
    depend on re-reading a (possibly edited) source line."""
    root, db = _graph(gt_index, tmp_path, _XLANG_REPO)
    (root / "web" / "server.js").write_text("// rewritten after indexing\n")
    _artifact, answer = _run(root, db, ActionKind.ROUTE_MAP, {})
    by_route = {r["route"]: r for r in answer["routes"]}
    assert "/users" in by_route
    assert by_route["/users"]["method"] == "GET"
    assert by_route["/users"]["handler"] == "listUsers"


def test_route_map_populates_middleware_from_middleware_on_edges(gt_index, tmp_path):
    root, db = _graph(gt_index, tmp_path, _XLANG_REPO)
    _artifact, answer = _run(root, db, ActionKind.ROUTE_MAP, {})
    users = next(r for r in answer["routes"] if r["route"] == "/users")
    names = [m["name"] for m in users["middleware"]]
    assert names == ["auth"]
    assert users["middleware"][0]["mechanism"] == "express_use"


# ---------------------------------------------------------------------------
# taint
# ---------------------------------------------------------------------------


def test_taint_does_not_follow_low_confidence_name_match(gt_index, tmp_path):
    """``fetch('/orders')`` in JS resolves by name_match (0.2) to a Python
    function named fetch; taint must not traverse it unless asked."""
    root, db = _graph(gt_index, tmp_path, _XLANG_REPO)
    artifact, answer = _run(root, db, ActionKind.TAINT, {"source": "loadOrders", "sink": "fetch"})
    assert answer["paths_found"] == 0
    assert "name_match_edges_excluded" in artifact.omissions
    _a2, opted = _run(
        root,
        db,
        ActionKind.TAINT,
        {"source": "loadOrders", "sink": "fetch", "include_name_matched": True},
    )
    assert opted["paths"] == []
    assert [p["path"] for p in opted["uncertain_paths"]] == [["loadOrders", "fetch"]]


# ---------------------------------------------------------------------------
# diff_impact / patch_impact
# ---------------------------------------------------------------------------

_SAME_NAME_CALLERS = {
    "core.py": "def target():\n    return 1\n",
    "a/x.py": "from core import target\n\n\ndef run():\n    return target()\n",
    "b/y.py": "from core import target\n\n\ndef run():\n    return target()\n",
}


def test_diff_impact_keeps_same_named_callers_in_different_files(gt_index, tmp_path):
    _root, db = _graph(gt_index, tmp_path, _SAME_NAME_CALLERS)
    diff = (
        "--- a/core.py\n+++ b/core.py\n@@ -2 +2 @@\n-    return 1\n+    return 2\n"
    )
    result = diff_impact(db, diff)
    locations = sorted(loc for _name, loc in result.callers_by_depth[1])
    assert locations == ["a/x.py:4", "b/y.py:4"]
    details = result.caller_details_by_depth[1]
    assert {d.file_path for d in details} == {"a/x.py", "b/y.py"}
    assert all(d.trust_tier and d.confidence > 0 for d in details)


_TWO_FUNCS = (
    "def first():\n"
    "    a = 1\n"
    "    return a\n"
    "\n"
    "def second():\n"
    "    b = 2\n"
    "    return b\n"
)


def _long_funcs() -> str:
    first = ["def first():"] + [f"    a{i} = {i}" for i in range(10)] + ["    return a0", ""]
    second = ["def second():"] + [f"    b{i} = {i}" for i in range(10)] + ["    return b0", ""]
    third = ["def third():"] + [f"    c{i} = {i}" for i in range(10)] + ["    return c0", ""]
    return "\n".join(first + [""] + second + [""] + third)


def test_patch_impact_maps_post_edit_hunks_through_pre_edit_lines(gt_index, tmp_path):
    """Ten lines inserted into ``first`` shift ``second`` down; its edit's
    post-edit line lies past every pre-edit node, so only the old-side
    line numbers can place it."""
    before = _long_funcs()
    root, db = _graph(gt_index, tmp_path, {"mod.py": before})
    inserted = "".join(f"    x{i} = {i}\n" for i in range(10))
    after = before.replace("    a0 = 0\n", "    a0 = 0\n" + inserted).replace(
        "    b8 = 8\n", "    b8 = 88\n"
    )
    artifact, answer = _run(
        root,
        db,
        ActionKind.PATCH_IMPACT,
        {"edited_files": {"mod.py": {"before": before, "after": after}}},
    )
    changed = sorted(s["name"] for s in answer["impact"]["changed_symbols"])
    assert changed == ["first", "second"]
    assert not any(o.startswith("line_mapping_approximate") for o in artifact.omissions)


def test_patch_impact_insert_only_edit_does_not_touch_neighbour(gt_index, tmp_path):
    root, db = _graph(gt_index, tmp_path, {"mod.py": _TWO_FUNCS})
    after = _TWO_FUNCS.replace("    a = 1\n", "    a = 1\n    a += 1\n")
    _artifact, answer = _run(
        root,
        db,
        ActionKind.PATCH_IMPACT,
        {"edited_files": {"mod.py": {"before": _TWO_FUNCS, "after": after}}},
    )
    assert [s["name"] for s in answer["impact"]["changed_symbols"]] == ["first"]


def test_patch_impact_unknown_graph_state_is_approximate(gt_index, tmp_path):
    root, db = _graph(gt_index, tmp_path, {"mod.py": _TWO_FUNCS})
    other_before = _TWO_FUNCS.replace("b = 2", "b = 9")
    after = other_before.replace("b = 9", "b = 10")
    artifact, _answer = _run(
        root,
        db,
        ActionKind.PATCH_IMPACT,
        {"edited_files": {"mod.py": {"before": other_before, "after": after}}},
    )
    assert "line_mapping_approximate:mod.py" in artifact.omissions
    assert artifact.coverage is Coverage.PARTIAL
