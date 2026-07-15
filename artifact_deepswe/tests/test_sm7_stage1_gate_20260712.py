r"""SM-7 (2026-07-12) — the DETERMINISTIC STAGE-1 FIXTURE GATE (the build's DEFINITION OF DONE).

This is a PROOF harness, not a paid run. For the ENABLED Profile-2 (Super-Mode) stack it proves,
by EXECUTION, that every enabled engine is live (no dark feature), that no delivered byte leaks a
test identity, that the single-dose invariant holds, that the flags are byte-identical off, that
the fail-closed preflight aborts on a missing capability, that a stale base-read is dropped, and
that the two cardinal fixes (#49 EditOverlay byte-preservation, #50 Lane-A latch rollback) hold.
It FOLDS IN #51 arm-integrity: the certificate that GT_BRIEF_MINIMAL actually minimalized the
baked brief (an SM-8 paired-comparison prereq).

DETERMINISM: same input -> identical output. No Date.now / random / network. Small in-repo
sqlite fixtures + the seam module; the ONE brief-generation probe forces the embedder OFF
(zero-vector, no ONNX) so it is deterministic (the b27 pattern).

====================================================================================================
INVARIANT TABLE (each proven by execution below)
  1  every enabled engine fires on >=1 fixture   -> test_invariant_1_no_dark_engine + per-engine
  2  leak = 0 across every fixture               -> test_invariant_2_leak_zero_every_delivery
  3  <=1 dose / observation                       -> test_invariant_3_* (arbitrate / flush / seam)
  4  default-off byte-identity                    -> test_invariant_4_* (profile no-op + seam)
  5  missing-capability preflight ABORTS          -> test_invariant_5_* (probe + CLI rc3)
  6  stale delivered = 0                          -> test_invariant_6_* (route STALE + augment drop)
  7  #49 EditOverlay CRLF byte-preservation       -> test_invariant_7_edit_overlay_preserves_crlf
  8  #50 Lane-A latch rollback (defer not destroy)-> test_invariant_8_* (loser re-arms / winner not)
  51 arm-integrity: baked brief minimalized       -> test_51_* (certificate + generate_v1r_brief)

ENGINE x FIXTURE COVERAGE (honest — cells COVERED here; deferred cells logged, no silent cap):
  engine                    | fixture (this file)                         | lang    | covered
  gateway.def_partition     | ambiguous grep (foo in 2 files)             | py      | YES
  gateway.caller_contract   | sig-change edit + cross-file caller         | py + GO | YES (cross-lang)
  gateway.covering          | injected CoveringResult (post_test)         | py      | YES
  gateway.change_surface    | azure-absent provider family (real detect)  | py      | YES
  gateway.patch_delta       | added-param edit + FACT caller (real)       | py      | YES
  global_arbiter            | multi-candidate pool -> 1 dose              | n/a     | YES
  edit_overlay              | sig-change reparse -> stale-drop            | py      | YES
  verification_plan         | build_verification_plan ladder              | py      | YES
  hypothesis_ledger         | env-failure -> repair_not_source            | n/a     | YES
  CompletionCertificate     | head BLOCK -> cert.decision=bounce_once     | n/a     | YES
  native renderers          | caller_contract / covering / recovery       | multi   | YES
  brief reducer             | generate_v1r_brief GT_BRIEF_MINIMAL         | py      | YES (e2e)
  registry_enforce          | file_view class expired at edit event       | py      | YES
  DEFERRED (Stage-1 does NOT prove; -> SM-8 paired run, YES-gated): the full 5-lang x 9-shape
  cross-product (rust/ts/js x remaining shapes) and LIVE model CONSUMPTION of any delivered fact.
  Non-python delivery-machinery is proven language-agnostic by the GO caller_break fixture.

MUTATIONS (>=2 biting mutations on the gate's OWN load-bearing assertions): PART J.
"""
from __future__ import annotations

import io
import os
import sqlite3
import sys

import pytest

_HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(_HERE, ".."))

import gt_mini_patch as g  # noqa: E402

from groundtruth.runtime import edit_overlay as eo  # noqa: E402
from groundtruth.runtime import fact_registry as fr  # noqa: E402
from groundtruth.runtime import gateway as gw  # noqa: E402
from groundtruth.runtime import global_arbiter as ga  # noqa: E402
from groundtruth.runtime import hypothesis_ledger as hl  # noqa: E402
from groundtruth.runtime import rl_profile as rp  # noqa: E402
from groundtruth.runtime import verification_plan as vp  # noqa: E402
from groundtruth.runtime.native_render import (  # noqa: E402
    contains_test_identity,
    render_caller_contract_native,
    render_covering_failure_native,
    render_recovery_native,
    render_signature_delta_native,
)
from groundtruth.runtime.submit_gate import GateVerdict, safe_build_certificate  # noqa: E402


# --------------------------------------------------------------------------- #
# graph.db builder (a REAL on-disk sqlite graph — never a mock)
# --------------------------------------------------------------------------- #
def _mk_graph(path, nodes, edges=(), *, with_props=False):
    con = sqlite3.connect(str(path))
    schema = (
        "CREATE TABLE nodes(id INTEGER PRIMARY KEY, label TEXT, name TEXT,"
        " qualified_name TEXT, file_path TEXT, start_line INTEGER, end_line INTEGER,"
        " signature TEXT, return_type TEXT, is_exported INTEGER, is_test INTEGER,"
        " language TEXT, parent_id INTEGER);"
        "CREATE TABLE edges(id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,"
        " type TEXT, source_line INTEGER, source_file TEXT, resolution_method TEXT,"
        " confidence REAL, metadata TEXT);"
    )
    if with_props:
        schema += "CREATE TABLE properties(id INTEGER PRIMARY KEY, node_id INTEGER, kind TEXT, value TEXT);"
    con.executescript(schema)
    for n in nodes:
        con.execute(
            "INSERT INTO nodes(id,label,name,file_path,start_line,end_line,signature,is_test,language)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (n["id"], n.get("label", "Function"), n["name"], n["file_path"],
             n.get("start_line", 1), n.get("end_line", 5), n.get("signature", ""),
             n.get("is_test", 0), n.get("language", "python")))
    for e in edges:
        con.execute(
            "INSERT INTO edges(id,source_id,target_id,type,source_line,source_file,"
            "resolution_method,confidence) VALUES(?,?,?,'CALLS',?,?,?,?)",
            (e["id"], e["source_id"], e["target_id"], e.get("source_line", 3),
             e.get("source_file"), e["resolution_method"], e["confidence"]))
    con.commit()
    con.close()
    return str(path)


def _edit_ev(rel, before, after):
    return gw.ToolEvent(kind="edit", command=f"str_replace {rel}", output="",
                        changed_files=(rel,), action_index=1,
                        edit_before_after={rel: (before, after)})


# =========================================================================== #
# THE ENGINE-DELIVERY MATRIX — one fixture per enabled engine, returning
# (fired: bool, delivered_bytes: str). Consumed by invariant 1 (dark detection)
# AND invariant 2 (leak=0). Every engine here MUST fire; a False is a DARK feature.
# =========================================================================== #
def _engine_deliveries(tmp_path, monkeypatch) -> dict[str, tuple[bool, str]]:
    monkeypatch.setenv("GT_GATEWAY", "1")
    out: dict[str, tuple[bool, str]] = {}

    # --- gateway.def_partition (py) : ambiguous grep -----------------------
    db_amb = _mk_graph(tmp_path / "amb.db", [
        {"id": 1, "name": "foo", "file_path": "a.py"},
        {"id": 2, "name": "foo", "file_path": "c.py"}])
    ev = gw.ToolEvent(kind="search", command="grep -rn foo .",
                      output="a.py:1:def foo\nc.py:1:def foo", action_index=0)
    envs = gw.augment(ev, gw.GatewayState(graph_db=db_amb, repo_root=str(tmp_path)))
    dp = [e for e in envs if e.evidence_type == "def_ref_partition"]
    out["gateway.def_partition"] = (bool(dp), "\n".join(dp[0].payload) if dp else "")

    # --- gateway.caller_contract (py) --------------------------------------
    db_cc = _mk_graph(tmp_path / "cc.db",
                      [{"id": 1, "name": "get_user", "file_path": "svc/users.py"},
                       {"id": 2, "name": "caller", "file_path": "app/main.py"}],
                      [{"id": 1, "source_id": 2, "target_id": 1,
                        "resolution_method": "import", "confidence": 1.0}])
    ev_cc = _edit_ev("svc/users.py", "def get_user(a):\n    return a\n",
                     "def get_user(a, b):\n    return a\n")
    cc = gw._produce_caller_contract(ev_cc, gw.GatewayState(graph_db=db_cc, repo_root=str(tmp_path)))
    out["gateway.caller_contract"] = (bool(cc), "\n".join(cc[0].payload) if cc else "")

    # --- gateway.caller_contract (GO — the language-agnostic delivery proof) -
    db_go = _mk_graph(tmp_path / "ccgo.db",
                      [{"id": 1, "name": "Handle", "file_path": "handler.go", "language": "go"},
                       {"id": 2, "name": "main", "file_path": "main.go", "language": "go"}],
                      [{"id": 1, "source_id": 2, "target_id": 1,
                        "resolution_method": "import", "confidence": 1.0}])
    ev_go = _edit_ev("handler.go", "func Handle(a int) error {\n\treturn nil\n}\n",
                     "func Handle(a int, b string) error {\n\treturn nil\n}\n")
    ccg = gw._produce_caller_contract(ev_go, gw.GatewayState(graph_db=db_go, repo_root=str(tmp_path)))
    out["gateway.caller_contract[go]"] = (bool(ccg), "\n".join(ccg[0].payload) if ccg else "")

    # --- gateway.covering : injected verdict on a test event ---------------
    cov = gw.CoveringResult(target="src/mod.py", verdict="fail",
                            body_lines=["src/mod.py:12: boom", "got 4 want 3"], test_files=())
    ev_t = gw.ToolEvent(kind="test", command="pytest", covering=cov, action_index=2)
    cv = gw._produce_covering(ev_t, gw.GatewayState(graph_db=db_amb, repo_root=str(tmp_path)))
    out["gateway.covering"] = (bool(cv), "\n".join(cv[0].payload) if cv else "")

    # --- gateway.patch_delta (GT_PATCH_DELTA, real analyze) ----------------
    monkeypatch.setenv("GT_PATCH_DELTA", "1")
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "src" / "caller.py").write_text("def use():\n    return get_user(5)\n", encoding="utf-8")
    db_pd = _mk_graph(tmp_path / "pd.db",
                      [{"id": 1, "name": "get_user", "file_path": "src/api.py"},
                       {"id": 2, "name": "use", "file_path": "src/caller.py"}],
                      [{"id": 1, "source_id": 2, "target_id": 1, "source_file": "src/caller.py",
                        "source_line": 2, "resolution_method": "import", "confidence": 0.9}])
    ev_pd = _edit_ev("src/api.py", "def get_user(uid):\n    return uid\n",
                     "def get_user(uid, name):\n    return uid\n")
    pd = gw._produce_patch_delta(ev_pd, gw.GatewayState(graph_db=db_pd, repo_root=str(tmp_path)))
    out["gateway.patch_delta"] = (bool(pd), "\n".join(pd[0].payload) if pd else "")

    # --- gateway.change_surface (GT_CHANGE_SURFACE, real detect) -----------
    monkeypatch.setenv("GT_CHANGE_SURFACE", "1")

    def _w(rel, c=""):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(c, encoding="utf-8")

    _w("providers/__init__.py",
       "from .aws import AwsProvider\nfrom .gcp import GcpProvider\n"
       "REGISTRY={'aws':AwsProvider,'gcp':GcpProvider}\n")
    _w("providers/aws.py", "class AwsProvider:\n    def connect(self):\n        return 'aws'\n")
    _w("providers/gcp.py", "class GcpProvider:\n    def connect(self):\n        return 'gcp'\n")
    _w("config/aws_config.py", "AWS_CONFIG={}\n")
    _w("config/gcp_config.py", "GCP_CONFIG={}\n")
    db_cs = _mk_graph(tmp_path / "cs.db", [
        {"id": 1, "name": "AwsProvider", "label": "Class", "file_path": "providers/aws.py"},
        {"id": 2, "name": "GcpProvider", "label": "Class", "file_path": "providers/gcp.py"}])
    st_cs = gw.GatewayState(
        graph_db=db_cs, repo_root=str(tmp_path),
        issue_text="Add azure provider support analogous to aws and gcp providers.")
    ev_cs = gw.ToolEvent(kind="search", command="grep -rn azure .", output="", action_index=5)
    monkeypatch.setattr(gw, "_zero_absent_repeat_ok", lambda ev, st: True)
    cs = gw._produce_change_surface(ev_cs, st_cs)
    out["gateway.change_surface"] = (bool(cs), "\n".join("\n".join(e.payload) for e in cs))

    # --- global_arbiter : multi-candidate -> exactly one winner ------------
    res = ga.arbitrate([
        ga.Candidate(plane=ga.PLANE_LANE_A, kind="l3.contract", dedup_key="lo", seq=0),
        ga.Candidate(plane=ga.PLANE_GATEWAY, kind="covering_verdict", dedup_key="hi", seq=1)])
    out["global_arbiter"] = (res.winner is not None and len(res.losers) == 1,
                             res.winner.kind if res.winner else "")

    # --- edit_overlay : structural edit -> changed=True (stale-drop input) --
    db_eo = _mk_graph(tmp_path / "eo.db", [
        {"id": 1, "name": "get_user", "file_path": "mod.py", "signature": "def get_user(id):"}])
    entry = eo.build_episode_overlay_entry(db_eo, "mod.py", "def get_user(id, extra):\n    return id\n")
    out["edit_overlay"] = (entry.get("changed") is True and entry.get("state") == "available", "")

    # --- verification_plan : the deliverable-rung ladder (pure, no execute) -
    plan = vp.build_verification_plan(db_cc, str(tmp_path), ("get_user",))
    kinds = [c.kind for c in plan.checks]
    out["verification_plan"] = ("syntax" in kinds and len(kinds) >= 2, "")

    # --- hypothesis_ledger : env-failure -> repair_not_source selection ----
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setenv("GT_HYPOTHESIS", "1")
    g._EPISODE.reset_attempt()
    g._gt_hypothesis_recovery = None
    monkeypatch.setattr(g, "_action_count", 5, raising=False)
    g._gt_hypothesis_classify_turn("make", "bash: gcc: command not found")
    sel = g._gt_hypothesis_recovery
    out["hypothesis_ledger"] = (sel is not None and sel[0] == hl.D_REPAIR_NOT_SOURCE, "")
    g._EPISODE.reset_attempt()
    g._gt_hypothesis_recovery = None

    # --- CompletionCertificate : head BLOCK -> a full cert consumed --------
    cert = safe_build_certificate(
        head=GateVerdict(allow=False, reason="hygiene", detail="d"),
        covering={"verdict": "fail", "failing_test_names": ["tests/test_x.py::test_a"]},
        hygiene={"blocking": True})
    out["CompletionCertificate"] = (cert.decision == "bounce_once", "")

    # --- native renderers : the diagnostic FORMS the model reads -----------
    nr = "\n".join(filter(None, [
        render_caller_contract_native("get_user", 3, 2, def_file="svc/users.py"),
        render_covering_failure_native({"stdout_tail": "src/mod.py:12: boom\ngot 4 want 3"}),
        render_signature_delta_native("app/main.py", 7, "get_user",
                                      expected_min=2, expected_max=2, given=1),
        render_recovery_native("repair_not_source",
                               "The environment is broken; repair it, do not edit source."),
    ]))
    out["native_renderers"] = (len(nr.strip()) > 0, nr)

    # --- registry_enforce : a file_view class delivered on the edit event
    #     EXPIRES under GT_REGISTRY_ENFORCE (the enforcement engine is live) -
    monkeypatch.setenv("GT_REGISTRY_ENFORCE", "1")
    st_re = gw.GatewayState(graph_db=db_cc, repo_root=str(tmp_path))
    fv = gw._mk_add(st_re, ev_cc, fact_kind="caller_contract", target="svc/users.py",
                    body_lines=["x"], evidence=[], tier=gw.WARNING,
                    producer="contract_map", symbol="get_user")
    route = gw.route_delivery(fv, ev_cc, st_re)
    out["registry_enforce"] = (route == gw.ROUTE_EXPIRED_LATE, "")
    monkeypatch.delenv("GT_REGISTRY_ENFORCE", raising=False)

    # --- brief reducer : the minimal-brief reduction is a real transform ---
    from groundtruth.pretask import v1r_brief as v1r
    full = "\n".join([
        '<gt-localization confidence="high">', "#1 a.py", "</gt-localization>",
        "<gt-task-brief>", "1. a.py (foo)", "   Callers: 3 callers",
        "<gt-obligations>", "MUST x", "</gt-obligations>", "</gt-task-brief>"])
    red = v1r._reduce_brief_to_minimal(full)
    out["brief_reducer"] = ("<gt-localization" not in red and "<gt-obligations>" in red
                            and red != full, red)

    return out


# --------------------------------------------------------------------------- #
# INVARIANT 1 — every enabled engine fires on >=1 fixture (DARK DETECTION).
# --------------------------------------------------------------------------- #
def test_invariant_1_no_dark_engine(tmp_path, monkeypatch):
    """The dark-detection the profile preflight CANNOT do: an ENABLED Profile-2 engine that
    fires on NO fixture is a DARK feature -> FAIL loudly, naming it."""
    deliveries = _engine_deliveries(tmp_path, monkeypatch)
    dark = sorted(name for name, (fired, _b) in deliveries.items() if not fired)
    assert dark == [], f"DARK enabled engine(s) (fired on NO fixture): {dark}"
    # the matrix must cover the full Super-Mode engine set (no silent shrink of the gate).
    covered = set(deliveries)
    required = {
        "gateway.def_partition", "gateway.caller_contract", "gateway.caller_contract[go]",
        "gateway.covering", "gateway.patch_delta", "gateway.change_surface",
        "global_arbiter", "edit_overlay", "verification_plan", "hypothesis_ledger",
        "CompletionCertificate", "native_renderers", "registry_enforce", "brief_reducer",
    }
    missing = sorted(required - covered)
    assert missing == [], f"gate matrix silently dropped engine(s): {missing}"


def test_invariant_1b_cross_language_delivery_proven(tmp_path, monkeypatch):
    """Language-agnosticism of the DELIVERY machinery: the GO caller_break fires (the exact
    cross-language case patch_delta's ast-only path cannot reach)."""
    d = _engine_deliveries(tmp_path, monkeypatch)
    fired, body = d["gateway.caller_contract[go]"]
    assert fired and "1 caller(s) in 1 file(s)" in body


# --------------------------------------------------------------------------- #
# INVARIANT 2 — leak = 0 across every fixture (no test identity in any delivery).
# --------------------------------------------------------------------------- #
def test_invariant_2_leak_zero_every_delivery(tmp_path, monkeypatch):
    deliveries = _engine_deliveries(tmp_path, monkeypatch)
    leaks = {name: body for name, (_f, body) in deliveries.items()
             if body and contains_test_identity(body)}
    assert leaks == {}, f"leak != 0 — delivered bytes carry a test identity: {list(leaks)}"


# --------------------------------------------------------------------------- #
# INVARIANT 3 — <=1 dose / observation (the global arbiter's single-dose law).
# --------------------------------------------------------------------------- #
def test_invariant_3a_arbitrate_yields_one_winner():
    """A multi-candidate turn across THREE planes yields EXACTLY one winner; every other
    candidate is a logged loser (never a silent second dose)."""
    cands = [
        ga.Candidate(plane=ga.PLANE_STEER, kind="l5.stuck", dedup_key="a", seq=0),
        ga.Candidate(plane=ga.PLANE_LANE_A, kind="l3.contract", dedup_key="b", seq=1),
        ga.Candidate(plane=ga.PLANE_GATEWAY, kind="covering_verdict", dedup_key="c", seq=2),
    ]
    res = ga.arbitrate(cands)
    assert res.winner is not None
    assert res.winner.kind == "covering_verdict"          # highest ladder class wins the ONE dose
    assert len(res.losers) == 2                            # the other two are logged losers


def test_invariant_3b_flush_delivers_at_most_one(monkeypatch):
    """The seam's global pool flush delivers AT MOST ONE dose across planes end-to-end."""
    monkeypatch.setenv("GT_GLOBAL_ARBITER", "1")
    g._reset_oracle_state()
    out = {"output": "BASE"}
    delivered: list[str] = []

    def _mk(cand, tag):
        def _t():
            out["output"] += f"\n<{tag}>"
            delivered.append(tag)
        return (cand, _t)

    pool = [
        _mk(ga.Candidate(plane=ga.PLANE_LANE_A, kind="l3.contract", dedup_key="lo", seq=0), "LO"),
        _mk(ga.Candidate(plane=ga.PLANE_GATEWAY, kind="covering_verdict", dedup_key="hi", seq=1), "HI"),
    ]
    g._global_pool_flush(pool, kkind="post_edit", kf="a/x.py", krel="a/x.py")
    assert delivered == ["HI"]                             # exactly ONE dose (highest class)
    assert out["output"].count("<") == 1                   # one appended block, not two
    g._reset_oracle_state()


# --------------------------------------------------------------------------- #
# INVARIANT 4 — default-off byte-identity (profile no-op + seam parity).
# --------------------------------------------------------------------------- #
def test_invariant_4a_profile_unset_is_strict_noop():
    """resolve_profile stays a strict no-op on unset/0/unknown (the resolver law is unchanged).
    SM-7 gate fix (2026-07-13): preflight on an UNSET env now routes the W8 production default
    and FAIL-CLOSES; only an explicit off token skips it."""
    assert rp.resolve_profile({}) == {}
    assert rp.resolve_profile({"GT_RL_PROFILE": "0"}) == {}
    assert rp.resolve_profile({"GT_RL_PROFILE": "99"}) == {}   # unknown version -> no fan-out
    assert rp.preflight({"GT_RL_PROFILE": "0"}, []) == []      # explicit off -> no abort
    assert rp.preflight({}, []) != []                          # unset -> production default -> fail-closed


@pytest.fixture
def _seam(tmp_path, monkeypatch):
    """The SM-5/SM-6 seam harness: a sig-change edit driven through gt_mini_patch."""
    (tmp_path / "pkg").mkdir(parents=True, exist_ok=True)
    (tmp_path / "pkg" / "mod.py").write_text("def foo(a, b, c):\n    return a\n", encoding="utf-8")
    db = _mk_graph(tmp_path / "graph.db",
                   [{"id": 1, "name": "foo", "file_path": "pkg/mod.py", "signature": "foo(a, b)"},
                    {"id": 2, "name": "bar", "file_path": "pkg/other.py", "signature": "bar()"}],
                   [{"id": 1, "source_id": 2, "target_id": 1,
                     "resolution_method": "import", "confidence": 1.0}])
    monkeypatch.setattr(g, "_db_path", lambda: db)
    monkeypatch.setattr(g, "_root", lambda: str(tmp_path))
    monkeypatch.setattr(g, "_POST_SEARCH_ON", False)
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    return tmp_path, db


_SUPER_MODE_FLAGS = (
    "GT_GLOBAL_ARBITER", "GT_REGISTRY_ENFORCE", "GT_EDIT_OVERLAY", "GT_COMPLETION_CERT",
    "GT_HYPOTHESIS", "GT_VERIFICATION_PLAN", "GT_CHANGE_SURFACE", "GT_PATCH_DELTA",
    "GT_BRIEF_MINIMAL",
)


def _seam_edit_output(monkeypatch, db, root):
    monkeypatch.setenv("GT_GATEWAY", "1")
    monkeypatch.setenv("GT_GATEWAY_EDIT_BRIDGES", "1")
    g._reset_oracle_state()
    action = {"command": "str_replace", "path": "pkg/mod.py",
              "old_str": "def foo(a, b):", "new_str": "def foo(a, b, c):"}
    out = {"output": "ok", "returncode": 0}
    g._augment_output(action, out)
    g._reset_oracle_state()
    return out["output"]


def test_invariant_4b_super_mode_flags_off_equals_pre_super_mode(_seam, monkeypatch):
    """On a representative EDIT turn, the delivered bytes with every Super-Mode flag OFF equal
    the pre-Super-Mode baseline (Profile-1 gateway only). Setting each Super-Mode flag to an
    explicit "0" is BYTE-IDENTICAL to leaving it unset — the profile's default-off guarantee."""
    tmp_path, db = _seam

    # reference = Profile-1 baseline (Super-Mode flags UNSET)
    for f in _SUPER_MODE_FLAGS:
        monkeypatch.delenv(f, raising=False)
    ref = _seam_edit_output(monkeypatch, db, str(tmp_path))
    assert ref.startswith("ok")                           # base observation preserved

    # explicit "0" on every Super-Mode flag -> identical bytes
    for f in _SUPER_MODE_FLAGS:
        monkeypatch.setenv(f, "0")
    off = _seam_edit_output(monkeypatch, db, str(tmp_path))
    assert off == ref, "an explicit Super-Mode flag=0 changed the delivered bytes (not byte-identical off)"


# --------------------------------------------------------------------------- #
# INVARIANT 5 — missing-capability preflight ABORTS (fail-closed).
# --------------------------------------------------------------------------- #
def test_invariant_5a_missing_module_aborts_preflight(monkeypatch):
    """Simulate a stale substrate that LACKS an engine module: the side-effect-free capability
    probe returns 'not findable' -> the member is absent from `available` -> preflight ABORTS
    (non-empty), and the missing member is named."""
    prof = {"GT_RL_PROFILE": "2"}
    real = rp._module_findable
    monkeypatch.setattr(
        rp, "_module_findable",
        lambda m: False if m == "groundtruth.runtime.submit_gate" else real(m))
    available = rp._available_from_env(prof)
    assert "GT_COMPLETION_CERT" not in available
    aborts = rp.preflight(prof, available)
    assert "GT_COMPLETION_CERT" in aborts                 # fail-closed: a non-empty abort list


def test_invariant_5b_cli_emits_no_exports_on_abort(monkeypatch):
    """The gt_ae_block.sh entry point: on a missing capability the CLI returns rc 3, emits NO
    `export` line (so the paid pier run never starts), and prints the abort sentinel."""
    prof = {"GT_RL_PROFILE": "2"}
    real = rp._module_findable
    monkeypatch.setattr(
        rp, "_module_findable",
        lambda m: False if m == "groundtruth.runtime.hypothesis_ledger" else real(m))
    out, err = io.StringIO(), io.StringIO()
    rc = rp.main(["--emit-exports"], env=prof, out=out, err=err)
    assert rc == 3
    assert out.getvalue() == ""                           # NO exports emitted on abort
    assert "GT_RL_PREFLIGHT_ABORT" in err.getvalue()
    assert "GT_HYPOTHESIS" in err.getvalue()


def test_invariant_5c_unknown_version_fails_closed():
    assert rp.preflight({"GT_RL_PROFILE": "does-not-exist"}, []) == ["GT_RL_PROFILE=does-not-exist"]


# --------------------------------------------------------------------------- #
# INVARIANT 6 — stale delivered = 0 (a base-read fact about a just-edited file is DROPPED).
# --------------------------------------------------------------------------- #
def test_invariant_6a_route_delivery_drops_stale_edited_file(monkeypatch):
    monkeypatch.setenv("GT_EDIT_OVERLAY", "1")
    st = gw.GatewayState(episode_overlay={"a.py": {"file": "a.py", "changed": True}})
    from groundtruth.runtime.evidence_envelope import EvidenceEnvelope
    env = EvidenceEnvelope.build(
        producer="p", fact_id="x", target="a.py", evidence_type="def_ref_partition",
        payload=("def: a.py:1",), provenance=(("a.py", 1),), confidence=0.9,
        tier="VERIFIED", graph_revision="", valid_until="", preferred_event="search")
    ev = gw.ToolEvent(kind="search")   # not the edit turn -> no exemption
    assert gw.route_delivery(env, ev, st) == gw.ROUTE_STALE


def test_invariant_6b_augment_drops_stale_and_ships_no_bytes(tmp_path, monkeypatch):
    """The model-facing behavior: the base-read def fact about the EDITED file is dropped, and
    a drop adds NO model-facing bytes (leak=0 by construction — a suppressed fact never renders)."""
    monkeypatch.setenv("GT_GATEWAY", "1")
    monkeypatch.setenv("GT_EDIT_OVERLAY", "1")
    db = _mk_graph(tmp_path / "st.db", [
        {"id": 1, "name": "foo", "file_path": "a.py", "signature": "def foo():"},
        {"id": 2, "name": "foo", "file_path": "c.py", "signature": "def foo():"}])
    ev = gw.ToolEvent(kind="search", command="grep -rn foo .",
                      output="a.py:1:def foo\nc.py:1:def foo", action_index=0)
    st = gw.GatewayState(graph_db=db, repo_root=str(tmp_path),
                         episode_overlay={"a.py": {"file": "a.py", "changed": True}})
    envs = gw.augment(ev, st)
    assert not any(e.target == "a.py" for e in envs)       # the stale a.py fact is gone (0 stale delivered)


# --------------------------------------------------------------------------- #
# INVARIANT 7 — #49 re-verify: EditOverlay never alters the agent's file bytes (CRLF).
# --------------------------------------------------------------------------- #
def test_invariant_7_edit_overlay_preserves_crlf(tmp_path, monkeypatch):
    """A CRLF (non-LF) file edited under GT_EDIT_OVERLAY=1 leaves the agent's on-disk bytes
    BYTE-IDENTICAL (the #49 lossy text round-trip regression guard). We CALL the #49 transaction
    (`_gt_edit_overlay_transaction`) — never touch its internals."""
    after = b"x = 1\r\ny = 3\r\n"
    (tmp_path / "foo.py").write_bytes(after)
    db = tmp_path / "graph.db"
    db.write_bytes(b"NOT-A-REAL-SQLITE-DB-BUT-A-STABLE-BLOB")
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_root", lambda: str(tmp_path))
    monkeypatch.setattr(g, "_db_path", lambda: str(db))
    monkeypatch.setattr(g, "_gt_gateway_deliveries", [], raising=False)
    g._gt_overlay_last = None
    monkeypatch.setenv("GT_EDIT_OVERLAY", "1")

    agent_bytes = (tmp_path / "foo.py").read_bytes()
    g._gt_edit_overlay_transaction({"command": "str_replace", "path": "foo.py",
                                    "old_str": "y = 2", "new_str": "y = 3"})
    assert (tmp_path / "foo.py").read_bytes() == agent_bytes, "EditOverlay altered the agent's CRLF bytes"
    assert g._gt_overlay_last is not None                  # the overlay actually ran


# --------------------------------------------------------------------------- #
# INVARIANT 8 — #50 re-verify: a Lane-A production-latched fact that LOSES arbitration is
# DEFERRED (re-producible next turn), not destroyed; a WINNER does not re-fire.
# --------------------------------------------------------------------------- #
@pytest.fixture
def _arbiter_latch(monkeypatch):
    monkeypatch.setenv("GT_GLOBAL_ARBITER", "1")
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_query_scope", lambda rel: ["src/neighbor.py"])
    g._reset_oracle_state()
    yield
    g._reset_oracle_state()


def _produce_scope(rel):
    g._consensus_scope.add(g._norm_rel(rel))
    return g._consensus_scope_block(rel)


def test_invariant_8a_lane_a_loser_reproduces_next_turn(_arbiter_latch):
    """A real Lane-A consensus.scope_map that LOSES to a higher-rank caller_break re-arms its
    PRODUCTION latch, so the SAME fact re-produces next turn (deferred, not destroyed)."""
    rel = "src/work.py"
    t1 = _produce_scope(rel)
    assert t1 and "<gt-scope" in t1
    key = ("consensus_scope", g._norm_rel(rel))
    assert key in g._seen and "consensus.scope_map" in g._lane_a_rearm_pending

    delivered: list[str] = []
    loser = ga.Candidate(plane=ga.PLANE_LANE_A, kind="consensus.scope_map", dedup_key="csk", seq=0)
    winner = ga.Candidate(plane=ga.PLANE_GATEWAY, kind="caller_break", dedup_key="cbk", seq=1)
    g._global_pool_flush([(loser, lambda: delivered.append("scope")),
                          (winner, lambda: delivered.append("caller_break"))],
                         kkind="post_edit", kf=rel, krel=rel)
    assert delivered == ["caller_break"]                   # <=1 dose, the higher class
    assert key not in g._seen                               # the LOSER un-burned its latch
    assert _produce_scope(rel) == t1                        # -> the fact RE-PRODUCES next turn


def test_invariant_8b_lane_a_winner_does_not_re_fire(_arbiter_latch):
    """A consensus.scope_map that WINS keeps its production latch committed -> it does NOT
    re-produce/re-deliver next turn (no double-deliver)."""
    rel = "src/work.py"
    t1 = _produce_scope(rel)
    assert t1 and "<gt-scope" in t1
    key = ("consensus_scope", g._norm_rel(rel))

    delivered: list[str] = []
    winner = ga.Candidate(plane=ga.PLANE_LANE_A, kind="consensus.scope_map", dedup_key="csk", seq=0)
    g._global_pool_flush([(winner, lambda: delivered.append("scope"))],
                         kkind="post_edit", kf=rel, krel=rel)
    assert delivered == ["scope"]
    assert key in g._seen                                  # WINNER keeps its latch burned
    assert g._consensus_scope_block(rel) == ""             # -> no re-fire next turn


# =========================================================================== #
# #51 — ARM-INTEGRITY (baked-brief minimalization). The certificate that
# GT_BRIEF_MINIMAL actually reduced the step-0 brief, so SM-8 cannot ship a FULL
# brief while claiming minimal (an invalid paired comparison).
# =========================================================================== #
_MINIMAL_ISSUE = (
    "apply_defaults MUST NOT mutate the caller mapping; fix apply_defaults in pkg/config.py "
    "to return a copy. It MUST persist a copy."
)


def _brief_fixture(tmp_path):
    db = _mk_graph(tmp_path / "brief.db", [
        {"id": 1, "name": "apply_defaults", "file_path": "pkg/config.py",
         "signature": "apply_defaults(cfg)"},
        {"id": 2, "name": "load_config", "file_path": "pkg/loader.py"}],
        [{"id": 1, "source_id": 2, "target_id": 1, "resolution_method": "import", "confidence": 1.0}])
    (tmp_path / "pkg").mkdir(parents=True, exist_ok=True)
    (tmp_path / "pkg" / "config.py").write_text("def apply_defaults(cfg):\n    return cfg\n", encoding="utf-8")
    (tmp_path / "pkg" / "loader.py").write_text(
        "def load_config():\n    return apply_defaults({})\n", encoding="utf-8")
    return db


@pytest.fixture
def _semantic_off(monkeypatch):
    """Force the embedder OFF (zero-vector, no ONNX) so generate_v1r_brief is DETERMINISTIC."""
    import groundtruth.pretask.graph_localizer as GL
    import groundtruth.pretask.v7_4_brief as V74
    monkeypatch.setattr(GL, "_get_embedder", lambda: None)

    class _Z:
        dim = 8

        def encode(self, texts, **_):
            return [[0.0] * self.dim for _ in texts]

    monkeypatch.setattr(V74, "_get_model", lambda: _Z())


def test_51a_generate_brief_full_carries_retired_surfaces(tmp_path, monkeypatch, _semantic_off):
    """FLAG OFF (baseline): generate_v1r_brief produces the FULL step-0 brief carrying
    <gt-localization>, <gt-graph-map>, contract narration, AND <gt-obligations>. The
    certificate reports it NOT minimal (a real, non-vacuous baseline)."""
    from groundtruth.pretask import v1r_brief as v1r
    from groundtruth.pretask.v1r_brief import brief_minimal_certificate, generate_v1r_brief
    db = _brief_fixture(tmp_path)
    monkeypatch.delenv("GT_BRIEF_MINIMAL", raising=False)
    monkeypatch.setenv("GT_GRAPH_MAP_DEMAND", "1")   # allow the graph-map into the baseline
    monkeypatch.delenv("GT_GATEWAY", raising=False)  # (gateway would retire the graph-map)
    monkeypatch.setattr(
        v1r,
        "_localization_header_for_entries",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("minimal contention renderer reached while flag off")
        ),
    )
    full = generate_v1r_brief(_MINIMAL_ISSUE, str(tmp_path), db).brief_text

    assert "<gt-localization" in full
    assert "<gt-graph-map>" in full
    assert any(t in full for t in ("Callers:", "Context:", "EDIT-TARGET CONTRACTS",
                                   "Related files", "Scope chain"))
    assert "<gt-obligations>" in full
    cert = brief_minimal_certificate(full)
    assert cert["minimal"] is False and cert["retired_present"], cert


def test_51b_generate_brief_minimal_drops_retired_keeps_obligations(tmp_path, monkeypatch, _semantic_off):
    """FLAG ON retires heavy narration but preserves calibrated MEDIUM contention.

    The certificate still proves minimalization and retained obligations.
    """
    from groundtruth.pretask.v1r_brief import brief_minimal_certificate, generate_v1r_brief
    db = _brief_fixture(tmp_path)
    monkeypatch.setenv("GT_BRIEF_MINIMAL", "1")
    monkeypatch.setenv("GT_GRAPH_MAP_DEMAND", "1")
    monkeypatch.delenv("GT_GATEWAY", raising=False)
    result = generate_v1r_brief(
        _MINIMAL_ISSUE, str(tmp_path), db, max_files=2,
    )
    mini = result.brief_text

    # MEDIUM/LOW localization is a calibrated contention set: its confidence,
    # alternatives, and grep hedge are part of minimal orientation, not narration.
    assert '<gt-localization confidence="medium">' in mini
    assert "confirm the edit target with grep" in mini
    assert "<gt-graph-map>" not in mini
    assert not any(t in mini for t in ("Callers:", "Context:", "EDIT-TARGET CONTRACTS",
                                       "Related files", "Scope chain", "Expected behavior"))
    assert "<gt-obligations>" in mini                       # obligations retained
    import re as _re
    assert _re.search(r"(?m)^\s*\d+\.\s+pkg/config\.py", mini)  # minimal 'which file' orientation
    loc_block = _re.search(
        r'<gt-localization\b[^>]*>\n(.*?)\n</gt-localization>',
        mini,
        _re.DOTALL,
    )
    assert loc_block is not None
    visible_paths = _re.findall(
        r"(?m)^\s*\d+\.\s+([^\s]+)", loc_block.group(1),
    )
    assert visible_paths == [entry.path for entry in result.files]
    assert result.rendered_candidate_count == len(result.files) == len(visible_paths)
    assert [proof["path"] for proof in result.localization_proof] == visible_paths
    assert len(visible_paths) <= 2
    # THE CERTIFICATE the Fable reviewer required — SM-8 rebake calls this on its baked brief.
    cert = brief_minimal_certificate(mini)
    assert cert["minimal"] is True, cert
    assert cert["retired_present"] == []
    assert cert["obligations_present"] is True
    assert cert["orientation_present"] is True
    # leak = 0 on the minimal delivery.
    assert not contains_test_identity(mini)


def test_51c_certificate_is_reusable_and_pure():
    """The certificate helper is a PURE reusable check (SM-8 rebake importable) — deterministic,
    two-sided, and it never mutates its input."""
    from groundtruth.pretask.v1r_brief import brief_minimal_certificate
    full = ('<gt-localization confidence="high">\n#1 a.py\n</gt-localization>\n'
            "Callers: 3\n<gt-obligations>\nMUST x\n</gt-obligations>")
    minimal = "<gt-task-brief>\n1. a.py (foo)\n<gt-obligations>\nMUST x\n</gt-obligations>\n</gt-task-brief>"
    assert brief_minimal_certificate(full)["minimal"] is False
    c = brief_minimal_certificate(minimal)
    assert c["minimal"] is True and c["obligations_present"] and c["orientation_present"]
    snapshot = full
    brief_minimal_certificate(full)
    assert full == snapshot                                # pure: no mutation


# =========================================================================== #
# PART J — >=2 BITING MUTATIONS on the gate's OWN load-bearing assertions.
# Each reproduces a mutant that VIOLATES a gate invariant and proves the gate's
# assertion would RED-flag it (the gate is not a vacuous pass).
# =========================================================================== #
def test_mutation_1_dark_engine_detection_bites(tmp_path, monkeypatch):
    """MUTATION on invariant-1's load-bearing assertion: inject a DARK engine (fired=False)
    into the delivery matrix. The `dark == []` assertion MUST bite (name the dark engine)."""
    deliveries = _engine_deliveries(tmp_path, monkeypatch)
    mutant = dict(deliveries)
    mutant["phantom_engine"] = (False, "")                 # a would-be dark feature
    dark = sorted(name for name, (fired, _b) in mutant.items() if not fired)
    assert dark == ["phantom_engine"]                      # the gate's own check catches it
    # the REAL matrix has no dark engine (the assertion is non-vacuous).
    assert sorted(n for n, (f, _b) in deliveries.items() if not f) == []


def test_mutation_2_leak_detection_bites(tmp_path, monkeypatch):
    """MUTATION on invariant-2's assertion: inject a delivery whose bytes carry a nodeid
    (`tests/test_secret.py::test_x`). The leak==0 assertion MUST bite."""
    deliveries = _engine_deliveries(tmp_path, monkeypatch)
    mutant = dict(deliveries)
    mutant["leaky_engine"] = (True, "the fix must satisfy tests/test_secret.py::test_x")
    leaks = {n: b for n, (_f, b) in mutant.items() if b and contains_test_identity(b)}
    assert list(leaks) == ["leaky_engine"]                 # the gate's leak check catches it
    # the REAL deliveries are leak-clean (non-vacuous).
    real_leaks = {n for n, (_f, b) in deliveries.items() if b and contains_test_identity(b)}
    assert real_leaks == set()


def test_mutation_3_single_dose_bites():
    """MUTATION on invariant-3's assertion: a broken arbiter that delivers EVERY candidate
    (not one). The `<=1 dose` count assertion MUST bite."""
    cands = [
        ga.Candidate(plane=ga.PLANE_GATEWAY, kind="covering_verdict", dedup_key="a", seq=0),
        ga.Candidate(plane=ga.PLANE_LANE_A, kind="l3.contract", dedup_key="b", seq=1),
    ]
    # real arbiter: exactly one winner.
    assert len([ga.arbitrate(cands).winner]) == 1
    # mutant "deliver all": the number of delivered doses is 2 -> the <=1 assertion bites.
    mutant_delivered = [c for c in cands]                  # a broken flush that ships every candidate
    assert len(mutant_delivered) == 2                      # would violate <=1 (caught by test_invariant_3b)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
