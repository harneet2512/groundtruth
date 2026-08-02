"""TTD for the VerificationPlan engine (W3).

RED-first: with ``verification_plan`` absent the import fails -> collection error
(the module-absent RED). Per-feature failing tests then pin each contract:
- command discovery from a pyproject fixture (config BEFORE extension fallback);
- UNKNOWN on an empty repo (missing toolchain -> kind-level UNKNOWN, never guessed);
- the k=2 caller-closure cap;
- the GREEN law rejecting a bare exit-0-unmapped result;
- determinism (same fixture -> byte-identical plan).

Two MUTATION harnesses are embedded (monkeypatched, in-process — no file edits):
- (M1) drop the conf>=0.7 gate in the closure -> a fact-tier test bites;
- (M2) let green() accept an unfresh result -> the freshness test bites.

Real graph.db fixtures are built in-memory (schema mirrors CLAUDE.md graph.db) so
the FACT rule + closure + covering are exercised on genuine SQL, not mocks.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest

from groundtruth.pretask.curation_map import DETERMINISTIC_RESOLUTION_METHODS
from groundtruth.runtime import verification_plan as vp
from groundtruth.runtime.verification_plan import (
    Check,
    CheckResult,
    VerificationPlan,
    build_verification_plan,
    derive_graph_revision,
    derive_patch_revision,
    discover_test_command,
    green,
    run_plan,
    select_targeted_tests,
)

_DET = sorted(DETERMINISTIC_RESOLUTION_METHODS)[0]  # a real FACT-tier method


# ---------------------------------------------------------------------------
# graph.db fixture builders (real SQLite, CLAUDE.md schema)
# ---------------------------------------------------------------------------
def _make_graph(path: str, nodes: list[dict], edges: list[dict]) -> None:
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY, label TEXT, name TEXT, "
        "qualified_name TEXT, file_path TEXT, start_line INTEGER, end_line INTEGER, "
        "signature TEXT, return_type TEXT, is_exported INTEGER, is_test INTEGER, "
        "language TEXT, parent_id INTEGER)"
    )
    con.execute(
        "CREATE TABLE edges (id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER, "
        "type TEXT, source_line INTEGER, source_file TEXT, resolution_method TEXT, "
        "confidence REAL, metadata TEXT)"
    )
    for n in nodes:
        con.execute(
            "INSERT INTO nodes (id,label,name,file_path,is_test,language) VALUES (?,?,?,?,?,?)",
            (n["id"], n.get("label", "Function"), n["name"], n["file_path"],
             int(n.get("is_test", 0)), n.get("language", "python")),
        )
    for e in edges:
        con.execute(
            "INSERT INTO edges (source_id,target_id,type,resolution_method,confidence) "
            "VALUES (?,?,?,?,?)",
            (e["source_id"], e["target_id"], e.get("type", "CALLS"),
             e.get("resolution_method", _DET), e.get("confidence", 1.0)),
        )
    con.commit()
    con.close()


@pytest.fixture
def chain_graph(tmp_path):
    """test_entry -> caller_one -> caller_two -> edited_leaf (all FACT-tier).

    ``edited_leaf`` is the changed entity. NO test directly calls it (direct
    covering = miss), but ``test_entry`` calls ``caller_one`` which transitively
    reaches ``edited_leaf`` at hop 2 -> the k=2 closure must recover the test."""
    db = str(tmp_path / "chain.db")
    nodes = [
        {"id": 1, "name": "edited_leaf", "file_path": "pkg/leaf.py"},
        {"id": 2, "name": "caller_two", "file_path": "pkg/mid.py"},
        {"id": 3, "name": "caller_one", "file_path": "pkg/top.py"},
        {"id": 4, "name": "test_entry", "file_path": "tests/test_top.py", "is_test": 1},
    ]
    edges = [
        {"source_id": 2, "target_id": 1},  # caller_two -> edited_leaf
        {"source_id": 3, "target_id": 2},  # caller_one -> caller_two
        {"source_id": 4, "target_id": 3},  # test_entry -> caller_one  (hop-2 from leaf)
    ]
    _make_graph(db, nodes, edges)
    return db


@pytest.fixture
def direct_graph(tmp_path):
    """A test that DIRECTLY calls the edited entity (direct fact covering hits)."""
    db = str(tmp_path / "direct.db")
    nodes = [
        {"id": 1, "name": "edited_fn", "file_path": "pkg/mod.py"},
        {"id": 2, "name": "test_edited", "file_path": "tests/test_mod.py", "is_test": 1},
    ]
    edges = [{"source_id": 2, "target_id": 1}]
    _make_graph(db, nodes, edges)
    return db


# ---------------------------------------------------------------------------
# RED-anchor: the module and its public surface exist
# ---------------------------------------------------------------------------
def test_module_surface_present():
    for sym in ("build_verification_plan", "run_plan", "green",
                "select_targeted_tests", "discover_test_command"):
        assert hasattr(vp, sym), f"missing public symbol {sym}"


# ---------------------------------------------------------------------------
# COMMAND DISCOVERY — config BEFORE extension fallback
# ---------------------------------------------------------------------------
def test_discover_pyproject_pytest(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\naddopts = '-q'\n", encoding="utf-8"
    )
    cmd, basis, conf = discover_test_command(str(tmp_path))
    assert cmd == ("pytest",)
    assert basis == "config:pyproject.pytest"
    assert conf == "medium"  # corroborated pytest ini shape


def test_discover_package_json(tmp_path):
    (tmp_path / "package.json").write_text(
        '{"scripts": {"test": "jest --ci"}}', encoding="utf-8"
    )
    cmd, basis, conf = discover_test_command(str(tmp_path))
    assert cmd == ("npm", "test")
    assert basis == "config:package_json"
    assert conf == "medium"  # names a known runner token (jest)


def test_discover_go_and_cargo(tmp_path):
    (tmp_path / "go.mod").write_text("module x\n", encoding="utf-8")
    cmd, basis, conf = discover_test_command(str(tmp_path))
    assert cmd == ("go", "test", "./...") and basis == "config:go_mod"
    assert conf == "low"  # uncorroborated: manifest presence only


def test_discover_makefile_test_target(tmp_path):
    (tmp_path / "Makefile").write_text("test:\n\tpytest -q\n", encoding="utf-8")
    cmd, basis, conf = discover_test_command(str(tmp_path))
    assert cmd == ("make", "test") and basis == "config:makefile"
    assert conf == "low"  # a target NAMED test can run anything (measured FP)


def test_discover_unknown_when_no_config(tmp_path):
    # An empty dir with no manifest -> UNKNOWN, never a guessed command.
    cmd, basis, conf = discover_test_command(str(tmp_path))
    assert cmd is None and basis == "unknown" and conf == "unknown"


def test_config_precedes_extension_fallback(tmp_path):
    # pyproject config must win over the manifest/extension fallback.
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n", encoding="utf-8"
    )
    (tmp_path / "foo.py").write_text("x = 1\n", encoding="utf-8")
    cmd, basis, _conf = discover_test_command(str(tmp_path))
    assert basis == "config:pyproject.pytest"


# ---------------------------------------------------------------------------
# F3 — discovery false-positive classes (each was a MEASURED FP; RED per class)
# ---------------------------------------------------------------------------
def test_f3a2_makefile_variable_assignment_not_discovered(tmp_path):
    # `test := ./deploy.sh` is a VARIABLE ASSIGNMENT, not a target. The old
    # `A or (B and C)` precedence admitted it.
    (tmp_path / "Makefile").write_text(
        "test := ./deploy.sh\nall:\n\techo hi\n", encoding="utf-8"
    )
    cmd, basis, _conf = discover_test_command(str(tmp_path))
    assert basis != "config:makefile"
    assert cmd is None  # nothing else in the dir -> UNKNOWN


def test_f3a2_makefile_plain_assignment_not_discovered(tmp_path):
    (tmp_path / "Makefile").write_text("test = ./deploy.sh\n", encoding="utf-8")
    cmd, basis, _conf = discover_test_command(str(tmp_path))
    assert basis != "config:makefile" and cmd is None


def test_f3_makefile_recipe_line_not_discovered(tmp_path):
    # A tab-indented RECIPE line containing `test:` is not a target.
    (tmp_path / "Makefile").write_text("all:\n\ttest: foo\n", encoding="utf-8")
    cmd, basis, _conf = discover_test_command(str(tmp_path))
    assert basis != "config:makefile" and cmd is None


def test_f3b_npm_init_placeholder_not_discovered(tmp_path):
    # The npm-init default exits 1 and runs no tests. It must yield UNKNOWN and
    # BLOCK the extension fallback (which would re-emit `npm test` — the very
    # command that runs this placeholder).
    (tmp_path / "package.json").write_text(
        '{"scripts": {"test": "echo \\"Error: no test specified\\" && exit 1"}}',
        encoding="utf-8",
    )
    cmd, basis, conf = discover_test_command(str(tmp_path))
    assert cmd is None
    assert basis == "config:package_json_placeholder" and conf == "unknown"


def test_f3b_npm_bare_echo_not_discovered(tmp_path):
    (tmp_path / "package.json").write_text(
        '{"scripts": {"test": "echo no tests"}}', encoding="utf-8"
    )
    cmd, basis, conf = discover_test_command(str(tmp_path))
    assert cmd is None
    assert basis == "config:package_json_placeholder" and conf == "unknown"


def test_f3b_npm_uncorroborated_script_is_low(tmp_path):
    # A real (non-placeholder) script with no known runner token -> emitted at LOW.
    (tmp_path / "package.json").write_text(
        '{"scripts": {"test": "./run_tests.sh"}}', encoding="utf-8"
    )
    cmd, basis, conf = discover_test_command(str(tmp_path))
    assert cmd == ("npm", "test") and basis == "config:package_json"
    assert conf == "low"


def test_f3c_poetry_scripts_test_not_a_trigger(tmp_path):
    # [tool.poetry.scripts] declares console-script ENTRY POINTS, not a task
    # runner: `test = "mypkg.deploy:main"` was a measured FP. The poetry trigger
    # must never fire — `poetry run test` would EXECUTE the deploy entry point.
    # (pyproject.toml's presence still admits the manifest fallback -> ["pytest"]
    # at LOW confidence, which runs pytest, not the entry point — acceptable.)
    (tmp_path / "pyproject.toml").write_text(
        '[tool.poetry]\nname = "mypkg"\n\n[tool.poetry.scripts]\n'
        'test = "mypkg.deploy:main"\n', encoding="utf-8"
    )
    cmd, basis, conf = discover_test_command(str(tmp_path))
    assert basis != "config:pyproject.poetry_script"
    assert cmd != ("poetry", "run", "test")
    assert basis == "extension_fallback" and conf == "low"


def test_f3c2_bare_tool_pytest_not_a_trigger(tmp_path):
    # pytest reads ONLY [tool.pytest.ini_options]; a bare [tool.pytest] table is
    # ignored by pytest and must not be the CONFIG trigger (medium). The manifest
    # fallback may still offer pytest — at LOW confidence, not config-corroborated.
    (tmp_path / "pyproject.toml").write_text("[tool.pytest]\nfoo = 1\n", encoding="utf-8")
    cmd, basis, conf = discover_test_command(str(tmp_path))
    assert basis != "config:pyproject.pytest"
    assert conf != "medium"


# ---------------------------------------------------------------------------
# UNKNOWN on an empty repo — kind-level UNKNOWN, never a fabricated command
# ---------------------------------------------------------------------------
def test_empty_repo_yields_unknown_rungs(tmp_path):
    empty_db = str(tmp_path / "none.db")  # does not exist
    plan = build_verification_plan(empty_db, str(tmp_path), ["some_symbol"])
    kinds = {c.kind for c in plan.checks}
    assert "syntax" in kinds and "integration" in kinds
    for c in plan.checks:
        # No graph, no config -> every emitted rung is UNKNOWN with no command.
        assert c.confidence == "unknown"
        assert c.command is None
    assert plan.graph_revision == "absent"


# ---------------------------------------------------------------------------
# k=2 CLOSURE — recovers the transitive-caller test; cap holds
# ---------------------------------------------------------------------------
def test_direct_covering_hits(direct_graph):
    sel = select_targeted_tests(direct_graph, "", ["edited_fn"])
    files = {r["file"] for r in sel}
    assert "tests/test_mod.py" in files
    assert any(r["selection_basis"] == "fact_covering" for r in sel)


def test_k2_closure_recovers_transitive_test(chain_graph):
    # Direct covering MISSES (no test calls edited_leaf directly)...
    con = sqlite3.connect(chain_graph)
    from groundtruth.runtime.covering_runner import select_covering_tests
    direct = select_covering_tests(chain_graph, {"edited_leaf"}, limit=8)
    con.close()
    assert direct == [], "no test should DIRECTLY call edited_leaf"
    # ...but the k=2 caller closure recovers tests/test_top.py.
    sel = select_targeted_tests(chain_graph, "", ["edited_leaf"])
    files = {r["file"] for r in sel}
    assert "tests/test_top.py" in files
    assert any(r["selection_basis"] == "closure_k2" for r in sel)


def test_selection_capped_at_eight(tmp_path):
    # 12 tests directly call the edited entity -> selection must cap at 8.
    db = str(tmp_path / "wide.db")
    nodes = [{"id": 1, "name": "hub", "file_path": "pkg/hub.py"}]
    edges = []
    for i in range(12):
        nid = 100 + i
        nodes.append({"id": nid, "name": f"test_{i}", "file_path": f"tests/test_{i}.py", "is_test": 1})
        edges.append({"source_id": nid, "target_id": 1})
    _make_graph(db, nodes, edges)
    sel = select_targeted_tests(db, "", ["hub"])
    assert len(sel) <= 8


def test_closure_node_cap_bounds_expansion(tmp_path, monkeypatch):
    # A star of 100 direct callers of the edited node; the closure must collect at
    # most _CLOSURE_NODE_CAP (50) intermediates regardless.
    db = str(tmp_path / "star.db")
    nodes = [{"id": 1, "name": "leaf", "file_path": "pkg/leaf.py"}]
    edges = []
    for i in range(100):
        nid = 200 + i
        nodes.append({"id": nid, "name": f"caller_{i}", "file_path": f"pkg/c{i}.py"})
        edges.append({"source_id": nid, "target_id": 1})
    _make_graph(db, nodes, edges)
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    from groundtruth.runtime.verification_plan import _fact_caller_closure, _seed_node_ids
    seeds = _seed_node_ids(con, ["leaf"])
    closure = _fact_caller_closure(con, seeds, has_conf=True)
    con.close()
    assert len(closure) <= vp._CLOSURE_NODE_CAP


# ---------------------------------------------------------------------------
# name_match is NEVER a covering/closure edge (the FACT rule)
# ---------------------------------------------------------------------------
def test_name_match_edge_not_covered(tmp_path):
    db = str(tmp_path / "nm.db")
    nodes = [
        {"id": 1, "name": "edited", "file_path": "pkg/m.py"},
        {"id": 2, "name": "test_nm", "file_path": "tests/test_m.py", "is_test": 1},
    ]
    # The only test->edited edge is name_match -> must NOT be selected as covering.
    edges = [{"source_id": 2, "target_id": 1, "resolution_method": "name_match", "confidence": 0.9}]
    _make_graph(db, nodes, edges)
    sel = select_targeted_tests(db, "", ["edited"])
    assert sel == []


def test_low_confidence_fact_edge_not_covered(tmp_path):
    db = str(tmp_path / "lowconf.db")
    nodes = [
        {"id": 1, "name": "edited", "file_path": "pkg/m.py"},
        {"id": 2, "name": "test_lc", "file_path": "tests/test_m.py", "is_test": 1},
    ]
    # FACT-tier method but confidence 0.6 (< 0.7 floor) -> not a fact -> not covered.
    edges = [{"source_id": 2, "target_id": 1, "resolution_method": _DET, "confidence": 0.6}]
    _make_graph(db, nodes, edges)
    sel = select_targeted_tests(db, "", ["edited"])
    assert sel == []


# ---------------------------------------------------------------------------
# GREEN law
# ---------------------------------------------------------------------------
def _plan_with_revs(g="G1", p="P1") -> VerificationPlan:
    return VerificationPlan(
        patch_revision=p, graph_revision=g,
        changed_entities=("edited",), obligations=(),
        checks=(), edited_files=("pkg/m.py",),
    )


def _result(**kw) -> CheckResult:
    base = dict(
        kind="unit", selection_basis="fact_covering", executed=True, verdict="pass",
        graph_revision="G1", patch_revision="P1",
        covered_entities=("edited",), covered_obligations=(),
        attribution_requirement="none", attribution_satisfied=True, detail={},
    )
    base.update(kw)
    return CheckResult(**base)


def test_green_happy_path():
    assert green(_result(), _plan_with_revs()).green is True


def test_green_rejects_exit0_unmapped():
    # Executed + fresh + pass but covers NOTHING -> executed_unmapped, never green.
    r = _result(covered_entities=(), covered_obligations=())
    gv = green(r, _plan_with_revs())
    assert gv.green is False and gv.status == "executed_unmapped"


def test_green_rejects_stale():
    r = _result(graph_revision="OLD")
    gv = green(r, _plan_with_revs())
    assert gv.green is False and gv.status == "stale"


def test_green_rejects_not_executed():
    assert green(_result(executed=False), _plan_with_revs()).status == "not_executed"


def test_green_reports_red_on_fail():
    assert green(_result(verdict="fail"), _plan_with_revs()).status == "red"


def test_green_rejects_attribution_unmet():
    r = _result(attribution_requirement="edit_attributed", attribution_satisfied=False)
    gv = green(r, _plan_with_revs())
    assert gv.green is False and gv.status == "attribution_unmet"


# ---------------------------------------------------------------------------
# F4 — green() membership: claimed coverage must be entities THE PLAN NAMES
# ---------------------------------------------------------------------------
def test_f4_green_rejects_phantom_entity():
    # Matching revisions + pass + executed, but the claimed coverage names an
    # entity the plan never declared -> executed_unmapped, never green.
    r = _result(covered_entities=("TOTALLY_DIFFERENT_ENTITY",))
    gv = green(r, _plan_with_revs())
    assert gv.green is False and gv.status == "executed_unmapped"


def test_f4_green_rejects_phantom_obligation():
    r = _result(covered_entities=(), covered_obligations=("phantom_obligation",))
    gv = green(r, _plan_with_revs())
    assert gv.green is False and gv.status == "executed_unmapped"


def test_f4_green_accepts_plan_named_subset():
    # Coverage claims that ARE a subset of the plan's named entities stay green.
    r = _result(covered_entities=("edited",))
    assert green(r, _plan_with_revs()).green is True


# ---------------------------------------------------------------------------
# F2 — integration positive evidence: exit-0 with ZERO tests parsed NEVER greens
# ---------------------------------------------------------------------------
def _deploy_repo(tmp_path):
    """The reviewer's attack fixture: a Makefile `test:` target that DEPLOYS."""
    repo = tmp_path / "deployrepo"
    repo.mkdir()
    (repo / "Makefile").write_text(
        "test:\n\t./scripts/deploy_to_prod.sh --force\n", encoding="utf-8"
    )
    return str(repo)


def test_f2_deploy_target_never_greens(tmp_path, direct_graph):
    repo = _deploy_repo(tmp_path)
    plan = build_verification_plan(direct_graph, repo, ["edited_fn"])
    integ = [c for c in plan.checks if c.kind == "integration"][0]
    assert integ.command == ("make", "test")
    assert integ.confidence == "low"  # F3: makefile discovery is never medium
    # Stub executor: the deploy runs, prints no test output, exits 0.
    def deploy_executor(cmd, cwd, timeout):
        return 0, "deployed to prod\n", ""
    results = run_plan(plan, executor=deploy_executor, repo_root=repo)
    integ_res = [r for r in results if r.kind == "integration"][0]
    assert integ_res.verdict == "executed_no_tests"
    gv = green(integ_res, plan)
    assert gv.green is False


def test_f2_integration_pass_needs_parsed_tests(tmp_path, direct_graph):
    # Same plan, but the executor emits REAL pytest output with parsed passes ->
    # positive evidence -> pass -> green (all other clauses satisfied).
    repo = tmp_path / "pyrepo"
    repo.mkdir()
    (repo / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    plan = build_verification_plan(direct_graph, str(repo), ["edited_fn"])
    def pytest_executor(cmd, cwd, timeout):
        return 0, "===== 3 passed in 0.01s =====\n", ""
    results = run_plan(plan, executor=pytest_executor, repo_root=str(repo))
    integ_res = [r for r in results if r.kind == "integration"][0]
    assert integ_res.verdict == "pass"
    assert green(integ_res, plan).green is True


# ---------------------------------------------------------------------------
# F5 — attribution honesty: only a fact_covering-basis pass self-attributes
# ---------------------------------------------------------------------------
def _unit_check(basis: str) -> Check:
    return Check(
        kind="unit", command=("pytest", "tests/test_orphan.py"),
        selection_basis=basis, covered_entities=("edited",),
        covered_obligations=(), expected_cost="medium",
        confidence="low", attribution_requirement="edit_attributed",
        targets=("tests/test_orphan.py",), reason="",
    )


def _plan_for_unit(check: Check) -> VerificationPlan:
    return VerificationPlan(
        patch_revision="P1", graph_revision="G1",
        changed_entities=("edited",), obligations=(),
        checks=(check,), edited_files=("pkg/orphan.py",),
    )


def test_f5_convention_pass_is_advisory_not_green(monkeypatch):
    # Graph-orphan entity + convention-selected test + a (stubbed) pass: must NOT
    # full-green — a filename match proves nothing structural about reach.
    check = _unit_check("test_dir_convention")
    plan = _plan_for_unit(check)
    monkeypatch.setattr(vp, "run_covering_tests",
                        lambda *a, **k: {"verdict": "pass", "executed": True})
    results = run_plan(plan, repo_root="X:/nowhere")
    r = results[0]
    assert r.verdict == "pass" and r.executed
    assert r.attribution_satisfied is False
    gv = green(r, plan)
    assert gv.green is False and gv.status == "attribution_unmet"


def test_f5_closure_pass_is_advisory_not_green(monkeypatch):
    check = _unit_check("closure_k2")
    plan = _plan_for_unit(check)
    monkeypatch.setattr(vp, "run_covering_tests",
                        lambda *a, **k: {"verdict": "pass", "executed": True})
    r = run_plan(plan, repo_root="X:/nowhere")[0]
    assert r.attribution_satisfied is False
    assert green(r, plan).status == "attribution_unmet"


def test_f5_fact_basis_pass_self_attributes(monkeypatch):
    # The FACT covering edge structurally proves the test reaches the edited
    # symbol (deterministic method + conf>=0.7) -> a fact-basis pass is attributed.
    check = _unit_check("fact_covering")
    plan = _plan_for_unit(check)
    monkeypatch.setattr(vp, "run_covering_tests",
                        lambda *a, **k: {"verdict": "pass", "executed": True})
    r = run_plan(plan, repo_root="X:/nowhere")[0]
    assert r.attribution_satisfied is True
    assert green(r, plan).green is True


# ---------------------------------------------------------------------------
# F9 — syntax rung: "ok" requires ALL checkable targets to parse; mixed = partial
# ---------------------------------------------------------------------------
@pytest.fixture
def two_file_graph(tmp_path):
    """`edited_fn` defined in TWO files (overload-style) -> both become syntax
    targets."""
    db = str(tmp_path / "two.db")
    nodes = [
        {"id": 1, "name": "edited_fn", "file_path": "pkg/a.py"},
        {"id": 2, "name": "edited_fn", "file_path": "pkg/b.py"},
        {"id": 3, "name": "test_e", "file_path": "tests/test_a.py", "is_test": 1},
    ]
    edges = [{"source_id": 3, "target_id": 1}]
    _make_graph(db, nodes, edges)
    return db


def test_f9_syntax_partial_when_mixed(tmp_path, two_file_graph):
    # a.py exists and parses; b.py is MISSING on disk (unavailable) -> the rung is
    # "partial", never "ok", never green.
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "a.py").write_text("def edited_fn():\n    return 1\n", encoding="utf-8")
    plan = build_verification_plan(two_file_graph, str(repo), ["edited_fn"])
    results = run_plan(plan, repo_root=str(repo))
    syn = [r for r in results if r.kind == "syntax"][0]
    assert syn.verdict == "partial"
    assert green(syn, plan).green is False


def test_f9_syntax_ok_requires_all(tmp_path, two_file_graph):
    # Both files present and valid -> "ok" -> green.
    repo = tmp_path / "repo2"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "a.py").write_text("def edited_fn():\n    return 1\n", encoding="utf-8")
    (repo / "pkg" / "b.py").write_text("def edited_fn():\n    return 2\n", encoding="utf-8")
    plan = build_verification_plan(two_file_graph, str(repo), ["edited_fn"])
    syn = [r for r in run_plan(plan, repo_root=str(repo)) if r.kind == "syntax"][0]
    assert syn.verdict == "ok"
    assert green(syn, plan).green is True


# ---------------------------------------------------------------------------
# DETERMINISM — byte-identical plan across builds on the same fixture
# ---------------------------------------------------------------------------
def test_plan_byte_identical(direct_graph, tmp_path):
    a = build_verification_plan(direct_graph, str(tmp_path), ["edited_fn"])
    b = build_verification_plan(direct_graph, str(tmp_path), ["edited_fn"])
    assert a.canonical_json() == b.canonical_json()


def test_graph_revision_stable(direct_graph):
    assert derive_graph_revision(direct_graph) == derive_graph_revision(direct_graph)
    assert derive_graph_revision(direct_graph) != "absent"


def test_patch_revision_deterministic():
    assert derive_patch_revision(["b", "a"]) == derive_patch_revision(["a", "b"])


# ---------------------------------------------------------------------------
# run_plan wiring (host executor, no container) — syntax rung executes edit_check
# ---------------------------------------------------------------------------
def test_run_plan_syntax_ok_is_green(tmp_path, direct_graph):
    # A real, well-formed python file for the syntax rung to parse.
    src_dir = tmp_path / "repo"
    (src_dir / "pkg").mkdir(parents=True)
    (src_dir / "pkg" / "mod.py").write_text("def edited_fn():\n    return 1\n", encoding="utf-8")
    plan = build_verification_plan(direct_graph, str(src_dir), ["edited_fn"])
    results = run_plan(plan, repo_root=str(src_dir))
    syn = [r for r in results if r.kind == "syntax"]
    assert syn and syn[0].verdict == "ok"
    assert green(syn[0], plan).green is True


def test_run_plan_syntax_error_is_red(tmp_path, direct_graph):
    src_dir = tmp_path / "repo"
    (src_dir / "pkg").mkdir(parents=True)
    (src_dir / "pkg" / "mod.py").write_text("def edited_fn(:\n  broken\n", encoding="utf-8")
    plan = build_verification_plan(direct_graph, str(src_dir), ["edited_fn"])
    results = run_plan(plan, repo_root=str(src_dir))
    syn = [r for r in results if r.kind == "syntax"][0]
    assert syn.verdict == "syntax_error"
    assert green(syn, plan).status == "red"


def test_run_plan_routes_syntax_through_dedicated_executor(tmp_path, direct_graph):
    src_dir = tmp_path / "repo-dedicated"
    (src_dir / "pkg").mkdir(parents=True)
    (src_dir / "pkg" / "mod.py").write_text("def edited_fn(:\n", encoding="utf-8")
    plan = build_verification_plan(direct_graph, str(src_dir), ["edited_fn"])
    parse_calls = []

    def parse_executor(cmd, cwd, timeout):
        parse_calls.append((cmd, cwd, timeout))
        return 1, "", "SyntaxError: invalid syntax"

    def covering_executor(cmd, cwd, timeout):
        if cmd and cmd[0] != "pytest":
            raise AssertionError("non-pytest argv reached covering executor")
        return 0, "1 passed", ""

    results = run_plan(
        plan, executor=covering_executor, syntax_executor=parse_executor,
        repo_root=str(src_dir),
    )
    syn = [r for r in results if r.kind == "syntax"][0]

    assert syn.verdict == "syntax_error"
    assert parse_calls and parse_calls[0][0][:3] == ["python", "-I", "-c"]


def test_syntax_surface_matches_edit_check_support() -> None:
    assert {".pyi", ".ts", ".tsx", ".jsx"}.issubset(vp._SYNTAX_CHECKABLE_EXTS)


def test_syntax_rung_stops_at_total_budget(monkeypatch) -> None:
    check = Check(
        kind="syntax",
        command=None,
        selection_basis="edit_check",
        covered_entities=("edited",),
        covered_obligations=(),
        expected_cost="low",
        confidence="high",
        attribution_requirement="none",
        targets=("a.py", "b.py", "c.py"),
        reason="",
    )
    plan = VerificationPlan(
        patch_revision="P1",
        graph_revision="G1",
        changed_entities=("edited",),
        obligations=(),
        checks=(check,),
        edited_files=("a.py", "b.py", "c.py"),
    )
    clock = iter((0.0, 0.0, 0.0, 0.0, 20.0))
    monkeypatch.setattr(vp.time, "monotonic", lambda: next(clock))
    calls: list[str] = []

    def syntax(file_path, *_args, **_kwargs):
        calls.append(file_path)
        return {"file": file_path, "verdict": "ok"}

    monkeypatch.setattr(vp, "check_edit_syntax", syntax)
    result = run_plan(plan, repo_root=".", total_budget_seconds=5)[0]

    assert calls == ["a.py"]
    assert result.verdict == "partial"
    assert [row["reason"] for row in result.detail["per_file"][1:]] == [
        "total_budget_exhausted",
        "total_budget_exhausted",
    ]


# ---------------------------------------------------------------------------
# MUTATION HARNESSES (embedded, monkeypatched — prove the tests bite)
# ---------------------------------------------------------------------------
def test_mutation_M1_drop_conf_gate_bites(tmp_path, monkeypatch):
    """M1: if the closure drops the conf>=0.7 FACT gate, a sub-floor 'fact-tier'
    edge would leak into the closure and a covering test would be selected that the
    honest gate rejects. We simulate the mutant by monkeypatching the closure SQL
    builder to omit the confidence gate, and assert the honest code excludes it."""
    db = str(tmp_path / "m1.db")
    # edited <- caller (conf 0.6, FACT method but BELOW floor); test -> caller (conf 1.0).
    nodes = [
        {"id": 1, "name": "edited", "file_path": "pkg/leaf.py"},
        {"id": 2, "name": "caller", "file_path": "pkg/top.py"},
        {"id": 3, "name": "test_caller", "file_path": "tests/test_top.py", "is_test": 1},
    ]
    edges = [
        {"source_id": 2, "target_id": 1, "resolution_method": _DET, "confidence": 0.6},
        {"source_id": 3, "target_id": 2, "resolution_method": _DET, "confidence": 1.0},
    ]
    _make_graph(db, nodes, edges)

    # HONEST: the 0.6 edited<-caller edge is sub-floor, so caller is NOT in the
    # closure, so test_top is NOT selected.
    honest = {r["file"] for r in select_targeted_tests(db, "", ["edited"])}
    assert "tests/test_top.py" not in honest

    # MUTANT: force the closure to accept the sub-floor edge (drop the conf gate).
    real = vp._fact_caller_closure

    def _mutant(con, seed_ids, *, has_conf):
        return real(con, seed_ids, has_conf=False)  # pretend no conf column -> no gate

    monkeypatch.setattr(vp, "_fact_caller_closure", _mutant)
    mutant = {r["file"] for r in select_targeted_tests(db, "", ["edited"])}
    # The mutant WOULD select the test the honest gate excludes -> the gate matters.
    assert "tests/test_top.py" in mutant
    assert honest != mutant


def test_mutation_M2_green_accepts_unfresh_bites(monkeypatch):
    """M2: if green() stops enforcing freshness, a stale result passes. We build a
    stale result, confirm the honest green() rejects it (status='stale'), then a
    freshness-blind mutant accepts it -> the freshness clause is load-bearing."""
    plan = _plan_with_revs(g="G2", p="P2")
    stale = _result(graph_revision="OLD", patch_revision="OLD")

    # Honest: stale -> not green.
    assert green(stale, plan).status == "stale"

    # Mutant green() that ignores freshness:
    def _mutant_green(result: CheckResult, pl: VerificationPlan) -> vp.GreenVerdict:
        if not result.executed:
            return vp.GreenVerdict(False, "not_executed", result.kind)
        if result.verdict in vp._FAIL_VERDICTS:
            return vp.GreenVerdict(False, "red", result.kind)
        if result.verdict not in vp._PASS_VERDICTS:
            return vp.GreenVerdict(False, "unavailable", result.kind)
        # freshness clause DELETED
        covered = len(result.covered_entities) + len(result.covered_obligations)
        if covered < 1:
            return vp.GreenVerdict(False, "executed_unmapped", result.kind)
        return vp.GreenVerdict(True, "green", result.kind)

    assert _mutant_green(stale, plan).green is True  # mutant wrongly greens a stale result
    assert green(stale, plan).green is False  # honest code does not
