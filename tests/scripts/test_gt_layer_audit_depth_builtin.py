"""Pins the §4.0 depth-audit internal-target check in scripts/gt_layer_audit.py.

THE BUG (measurement-tool, not a GT product bug): `audit_depth` flagged a depth class
(RAISES/PRECEDES/WRITES/READS/CO_SERIALIZES) as FAIL when its source-PROPERTIES existed but it had
0 promoted edges — WITHOUT checking whether the property targets were BUILTINS/literals. Per the
§2.6 non-invention bar, a relation whose endpoints are builtins/literals CORRECTLY stays a property
and must NOT become an edge, so 0 edges is correct, not a defect.

Proven false-FAIL (superjson, ts): exception_type=Error (builtin), exception_flow=throw new
Error(...), call_order=Object.values->freeze (builtin receivers) -> 0 edges is CORRECT, but the audit
scored depth FAIL.

These tests pin BOTH directions of the internal-target check:
  * builtin target  + 0 edges  -> CONSTRUCT-PRESENT-BUILTIN -> depth PASS  (non-invention honoured)
  * internal target + 0 edges  -> PROMOTE-GAP               -> depth FAIL  (a real promote gap)
plus a MUTATION CHECK that removing the builtin guard turns the builtin case wrongly FAIL (the test
bites), and a CLI smoke that the prior `depth_present` KeyError in the verdict line no longer aborts.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_audit():
    path = _REPO_ROOT / "scripts" / "gt_layer_audit.py"
    spec = importlib.util.spec_from_file_location("gt_layer_audit_under_test", str(path))
    assert spec and spec.loader, f"cannot build spec for {path}"
    mod = importlib.util.module_from_spec(spec)
    sys.modules["gt_layer_audit_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def audit():
    return _load_audit()


def _make_db(path, *, exception_target, target_is_node):
    """A minimal graph.db with ONE method that raises `exception_target` via an exception_type
    property but mints 0 RAISES edges. `target_is_node` controls whether a Class node of that name
    exists (an internal target the relation SHOULD have promoted to)."""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE nodes (id INTEGER PRIMARY KEY, label TEXT, name TEXT, file_path TEXT, language TEXT);
        CREATE TABLE edges (id INTEGER PRIMARY KEY, source_id INT, target_id INT, type TEXT,
                            resolution_method TEXT, confidence REAL, metadata TEXT);
        CREATE TABLE properties (id INTEGER PRIMARY KEY, node_id INT, kind TEXT, value TEXT,
                                 line INT, confidence REAL);
        INSERT INTO nodes VALUES (1, 'Method', 'f', 'a.src', 'python');
        -- a CALLS edge so calls_edges>0 (substrate prereq), but ZERO RAISES edges.
        INSERT INTO edges VALUES (1, 1, 1, 'CALLS', 'same_file', 1.0, NULL);
        """
    )
    if target_is_node:
        conn.execute(
            "INSERT INTO nodes VALUES (2, 'Class', ?, 'a.src', 'python')", (exception_target,)
        )
    conn.execute(
        "INSERT INTO properties VALUES (1, 1, 'exception_type', ?, 1, 1.0)", (exception_target,)
    )
    conn.commit()
    conn.close()


def test_builtin_target_zero_edges_is_construct_present_builtin(audit, tmp_path):
    """exception_type=Error (a builtin) + 0 RAISES edges -> CONSTRUCT-PRESENT-BUILTIN, depth PASS.
    0 edges is the §2.6 non-invention bar being honoured, NOT a defect."""
    db = str(tmp_path / "builtin.db")
    _make_db(db, exception_target="Error", target_is_node=False)
    rep = audit.audit_depth(db)
    assert rep["per_class"]["RAISES"]["status"] == "CONSTRUCT-PRESENT-BUILTIN"
    assert "RAISES" in rep["construct_builtin"]
    assert rep["promote_gap"] == []
    assert rep["fired"] is True, "builtin-target depth class with 0 edges must PASS (non-invention)"


def test_internal_target_zero_edges_is_promote_gap(audit, tmp_path):
    """exception_type=MyInternalError where MyInternalError IS a Class node + 0 RAISES edges ->
    PROMOTE-GAP, depth FAIL. promote SHOULD have minted that edge but didn't."""
    db = str(tmp_path / "gap.db")
    _make_db(db, exception_target="MyInternalError", target_is_node=True)
    rep = audit.audit_depth(db)
    assert rep["per_class"]["RAISES"]["status"] == "PROMOTE-GAP"
    assert "RAISES" in rep["promote_gap"]
    assert rep["fired"] is False, "internal-target promote gap must FAIL depth"


def test_builtin_set_mirrors_resolver_promote_list(audit):
    """The builtin set is MIRRORED from the resolver, not invented per-language: spot-check that the
    canonical RAISES builtins (and the brief's named receivers) are present."""
    for name in ("Error", "TypeError", "Exception", "RuntimeError", "ValueError"):
        assert name in audit._BUILTIN_EXCEPTIONS, name
    for recv in (
        "Object",
        "Map",
        "Set",
        "Array",
        "String",
        "string",
        "list",
        "dict",
        "panic",
        "unwrap",
    ):
        assert recv in audit._BUILTIN_RECEIVERS, recv


def test_mutation_removing_builtin_guard_makes_builtin_case_fail(audit, tmp_path, monkeypatch):
    """MUTATION CHECK: if the builtin guard is removed (so a builtin token is treated as a possible
    internal target), the builtin case would resolve to a node only if same-named — but the real
    proof the guard BITES is: force `_is_builtin_target` to always-False AND make the builtin name a
    node, and the builtin case must then wrongly flip to PROMOTE-GAP/FAIL. With the guard intact it
    stays PASS. This is the red-on-break that proves the guard is load-bearing, not decoration."""
    db = str(tmp_path / "mutation.db")
    # `Error` IS a node here: with the guard intact it is still treated as a builtin (PASS); with the
    # guard removed it would be read as an internal target (FAIL). That delta is what the guard buys.
    _make_db(db, exception_target="Error", target_is_node=True)

    # Guard intact: builtin wins even though a same-named node exists -> PASS (the accepted-ambiguity
    # parity with promote.go, which treats `Error` as the builtin).
    rep_ok = audit.audit_depth(db)
    assert rep_ok["fired"] is True
    assert rep_ok["per_class"]["RAISES"]["status"] == "CONSTRUCT-PRESENT-BUILTIN"

    # Mutate: break the builtin guard. The builtin case must now WRONGLY FAIL (test bites) -> proves
    # the guard is the thing keeping the false-FAIL closed. Restored automatically by monkeypatch.
    monkeypatch.setattr(audit, "_is_builtin_target", lambda tok: False)
    rep_broken = audit.audit_depth(db)
    assert rep_broken["fired"] is False, (
        "removing the builtin guard must reintroduce the false-FAIL"
    )
    assert rep_broken["per_class"]["RAISES"]["status"] == "PROMOTE-GAP"


def test_cli_verdict_line_does_not_raise_keyerror(audit, tmp_path, capsys):
    """The verdict line read report['L0_depth']['depth_present'] — a key that never existed — so the
    audit aborted with KeyError before writing its JSON. Pin that the CLI runs end-to-end."""
    db = str(tmp_path / "cli.db")
    _make_db(db, exception_target="Error", target_is_node=False)
    rc = audit.main(["--graph", db, "--lang", "typescript", "--task", "cli_smoke"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "SUBSTRATE_VERDICT[typescript] = PASS" in out
    assert "depth=present:" in out  # the rewritten verdict field renders without KeyError
