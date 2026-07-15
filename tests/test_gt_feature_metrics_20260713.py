"""RED-first tests for the per-feature GT metrics (gt_feature_metrics + gt_feature_schema).

Every test carries at least one BITING MUTATION comment: the exact change that would make
the assertion fail, proving the test defends behaviour rather than the happy path. Fixtures
are SYNTHETIC (built in-test) so the suite runs anywhere; the real-fixture regressions live
in test_metric_defect_fixes_20260713.py.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(_ROOT, "src"), os.path.join(_ROOT, "scripts", "swebench")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import gt_feature_schema as s  # noqa: E402
import gt_feature_metrics as g  # noqa: E402
from groundtruth.runtime import rl_profile as rp  # noqa: E402


# --------------------------------------------------------------------------- #
# synthetic task-dir builder
# --------------------------------------------------------------------------- #
def _sha16(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _write_task(tmp_path, *, messages, ledger_rows, oracle_rows=None, submission="") -> str:
    d = str(tmp_path)
    os.makedirs(d, exist_ok=True)
    traj = {"messages": messages, "info": {"submission": submission},
            "trajectory_format": "mini-swe-agent"}
    with open(os.path.join(d, "mini-swe-agent.trajectory.json"), "w", encoding="utf-8") as f:
        json.dump(traj, f)
    with open(os.path.join(d, "gt_runtime_ledger_synthetic.jsonl"), "w", encoding="utf-8") as f:
        for r in ledger_rows:
            f.write(json.dumps(r) + "\n")
    if oracle_rows:
        with open(os.path.join(d, "gt_oracle_events_synthetic.jsonl"), "w", encoding="utf-8") as f:
            for r in oracle_rows:
                f.write(json.dumps(r) + "\n")
    return d


def _asst(cmd: str, content: str = ""):
    return {"role": "assistant", "content": content,
            "tool_calls": [{"function": {"arguments": json.dumps({"command": cmd})}}]}


def _tool(content: str):
    return {"role": "tool", "content": content}


def _delivered(layer, event="", file="", chars=100, reason="", **extra):
    r = {"layer": layer, "event_type": event, "file_path": file,
         "outcome": "delivered", "chars_delivered": chars, "reason": reason, "iteration": 1}
    r.update(extra)
    return r


# =========================================================================== #
# 1. schema — MetricValue never fabricates a measured zero for missing data
# =========================================================================== #
def test_mv_measured_none_is_rejected():
    # a real measured 0 is allowed; a MEASURED with value None is a construction bug.
    assert s.measured(0)["value"] == 0
    with pytest.raises(ValueError):
        s.mv(None, s.STATUS_MEASURED)
    # MUTATION: if mv() accepted (None, MEASURED), missing data could masquerade as a value.
    with pytest.raises(ValueError):
        s.mv(5, s.STATUS_UNMEASURED)  # UNMEASURED must carry value None


def test_new_lifecycle_all_unmeasured_never_zero():
    lc = s.new_lifecycle("x")
    for field in s.LIFECYCLE_FIELDS:
        assert lc[field]["status"] == s.STATUS_UNMEASURED
        assert lc[field]["value"] is None  # MUTATION: a default of 0 here would read as measured
        assert lc[field]["grader_version"] == s.GRADER_VERSION


# =========================================================================== #
# 7. every Profile-2 member appears — DYNAMICALLY from PROFILE_MEMBERS["2"]
# =========================================================================== #
def test_every_profile2_member_present(tmp_path):
    d = _write_task(tmp_path, messages=[_asst("ls")], ledger_rows=[])
    rec = g.collect_task("synthetic__task-1", d, profile="2")
    expected = set(rp.PROFILE_MEMBERS["2"])  # source of truth — never a copied list
    assert set(rec["features"]) == expected
    # MUTATION: hardcoding the member list (drop one) would fail this equality.
    assert rec["integrity"]["all_members_present"] is True


def test_unclassified_member_fails_loud(monkeypatch):
    # a Profile-2 member with no role classification must FAIL LOUD, never drop silently.
    with pytest.raises(KeyError):
        g.member_role("GT_TOTALLY_NEW_FLAG_NOT_CLASSIFIED")
    # MUTATION: a default 'infrastructure' fallback in member_role would swallow the drift.


def test_import_crosscheck_catches_bad_factclass(monkeypatch):
    monkeypatch.setitem(g._DIRECT_MEMBER_FACTCLASS, "GT_EDIT_CHECK", "not_a_real_class")
    with pytest.raises(ValueError):
        g._import_time_crosscheck()
    # MUTATION: removing the registry cross-check would let a bad fact class ship.


@pytest.mark.parametrize(("layer", "fact_class"), [
    ("consensus.scope", "localization"),
    ("edit.syntax", "syntax_result"),
    ("semantic_drift", "cochange_prior"),
    ("ga.trace_frame", "localization"),
    ("verify.horizon.executed", "covering_red"),
])
def test_real_runtime_layers_map_to_fact_classes(layer, fact_class):
    """Every layer observed as delivered in arm-4 must enter its canonical FACT lifecycle."""
    assert g.layer_to_fact_class(layer) == fact_class
    # MUTATION: removing the explicit legacy mapping or the registry fallback makes a delivered
    # row disappear from FACT receipt metrics even though its sealed bytes reached the model.


# =========================================================================== #
# 3. native/tag-free facts detected through the content SEAL (defect #2)
# =========================================================================== #
def test_native_tagfree_detected_via_seal(tmp_path):
    native_bytes = "answer.py:42: def to_dict(self) -> dict"  # NO <gt-*> tag anywhere
    seal = _sha16(native_bytes)
    messages = [
        {"role": "user", "content": "task"},
        _asst("grep -rn to_dict ."),
        _tool("search output ...\n" + native_bytes + "\n... more output"),
    ]
    rows = [_delivered("gateway.def_ref_partition", file="haystack/dataclasses/answer.py",
                       chars=len(native_bytes), content_sha256_16=seal, native_text=native_bytes)]
    vis = g.native_visible_by_fact_class(rows, messages)
    assert vis.get("def_partition") == 1  # located in the observation via the seal
    rec = g.collect_task("synthetic__native-1", _write_task(tmp_path, messages=messages, ledger_rows=rows),
                         profile="2")
    dp = rec["fact_classes"]["def_partition"]
    assert dp["delivered"]["value"] is True          # delivered WITHOUT any <gt-*> tag
    assert dp["receipt_level"]["status"] == s.STATUS_MEASURED
    assert dp["receipt_level"]["value"] == 1          # model-visible confirmed via seal


def test_native_seal_mismatch_is_unmeasured_not_false(tmp_path):
    native_bytes = "answer.py:42: def to_dict(self) -> dict"
    messages = [_tool("search output ...\n" + native_bytes)]
    rows = [_delivered("gateway.def_ref_partition", chars=len(native_bytes),
                       content_sha256_16="deadbeefdeadbeef")]  # WRONG seal
    vis = g.native_visible_by_fact_class(rows, messages)
    assert vis.get("def_partition", 0) == 0
    join = g.join_native_delivery(rows, messages)
    assert join[0] is None  # unconfirmable → None (UNMEASURED), never a silent False
    # MUTATION: fuzzy substring matching (ignoring the seal) would wrongly report received.


def test_delivery_detected_from_ledger_without_tags(tmp_path):
    # a gateway delivery with NO <gt-*> tag in the whole trajectory is STILL delivered.
    rows = [_delivered("gateway.trace_frame", file="posthog/types.py", chars=53)]
    messages = [_asst("cat posthog/types.py")]
    rec = g.collect_task("synthetic__notag", _write_task(tmp_path, messages=messages, ledger_rows=rows),
                         profile="2")
    assert rec["fact_classes"]["localization"]["delivered"]["value"] is True
    # MUTATION: relying only on W1's <gt-*> tag scan would report delivered=False here.


# =========================================================================== #
# 6. defect #6 — missing inputs stay null/UNMEASURED, never zero
# =========================================================================== #
def test_missing_ledger_stays_null_not_zero(tmp_path):
    rec = g.collect_task("synthetic__empty", _write_task(tmp_path, messages=[], ledger_rows=[]),
                         profile="2")
    # a class with no evidence: dose_tokens / steps_saved must be UNMEASURED, not 0.
    for fc in ("covering_red", "signature_delta"):
        lc = rec["fact_classes"][fc]
        assert lc["dose_tokens"]["status"] != s.STATUS_MEASURED or lc["dose_tokens"]["value"] is None
        assert lc["steps_saved"]["status"] in (s.STATUS_UNMEASURED,)
        assert lc["steps_saved"]["value"] is None
    # MUTATION: defaulting a missing metric to 0.0 would flip value to 0 and fail.


# =========================================================================== #
# 9. zero opportunity => NOT_ELIGIBLE (never CUT for correct silence)
# =========================================================================== #
def test_zero_opportunity_not_eligible(tmp_path):
    # no new file created, no search miss → newfile_precedent has no opportunity.
    rec = g.collect_task("synthetic__noop", _write_task(tmp_path, messages=[_asst("ls")], ledger_rows=[]),
                         profile="2")
    nf = rec["fact_classes"]["newfile_precedent"]
    assert nf["eligible"]["value"] is False
    assert nf["not_eligible"]["value"] is True
    assert rec["features"]["GT_CHANGE_SURFACE"]["verdict"] in (s.VERDICT_HOLD,)  # never CUT
    # MUTATION: an eligibility default of True would make this NOT NOT_ELIGIBLE.


# =========================================================================== #
# 8. enabled-but-dark => explicit DARK, not silent absence
# =========================================================================== #
def test_produced_but_suppressed_is_dark_present(tmp_path):
    # localization PRODUCED (consensus.scope_map) but ALL suppressed by the arbiter → DARK,
    # and the feature is still PRESENT in the output (never dropped).
    rows = [
        {"layer": "ga.consensus.scope_map", "event_type": "", "file_path": "a.py",
         "outcome": "suppressed_hidden_only", "chars_delivered": 0,
         "reason": "global_arbiter:outranked", "iteration": 1}
        for _ in range(3)
    ]
    messages = [_asst("grep -rn foo ."), _tool("a.py:1: foo")]
    rec = g.collect_task("synthetic__dark", _write_task(tmp_path, messages=messages, ledger_rows=rows),
                         profile="2")
    loc = rec["fact_classes"]["localization"]
    assert loc["produced"]["value"] is True
    assert loc["delivered"]["value"] is False
    assert rec["features"]["GT_LOC_RESLOT"]["verdict"] == s.VERDICT_DARK
    assert "GT_LOC_RESLOT" in rec["features"]  # present, not silently absent
    # MUTATION: verdict falling through to HOLD, or dropping the record, both fail.


# =========================================================================== #
# 10. aggregate reconciles + fail-closed on a missing feature record
# =========================================================================== #
def test_aggregate_reconciles_and_fail_closed(tmp_path):
    d1 = _write_task(tmp_path / "t1", messages=[_asst("ls")], ledger_rows=[
        _delivered("l3.contract", event="post_edit", file="x.py", chars=50)])
    os.makedirs(str(tmp_path / "t2"), exist_ok=True)
    d2 = _write_task(tmp_path / "t2", messages=[_asst("ls")], ledger_rows=[])
    r1 = g.collect_task("t1", d1, profile="2")
    r2 = g.collect_task("t2", d2, profile="2")
    agg = g.aggregate_run("run-x", [r1, r2], profile="2")
    # These legacy fixtures intentionally lack the canonical SS inputs. The
    # headline integrity bit must fold that failure instead of contradicting
    # the nested SS integrity artifact.
    assert agg["ss_integrity"]["publishable"] is False
    assert agg["integrity"]["publishable"] is False
    assert agg["integrity"]["reconciliation"]["enabled_member_count"] == len(rp.PROFILE_MEMBERS["2"])
    # now DELETE a feature record from one task → aggregate must REFUSE to publish.
    del r2["features"]["GT_GATEWAY"]
    r2["integrity"]["all_members_present"] = False
    agg2 = g.aggregate_run("run-x", [r1, r2], profile="2")
    assert agg2["integrity"]["publishable"] is False
    assert any(m["member"] == "GT_GATEWAY" for m in agg2["integrity"]["missing_records"])
    # MUTATION: an aggregate that tolerated a missing record would still be publishable.


# =========================================================================== #
# 11. leak canary — zero on clean, catches a gold path, invalidates the run
# =========================================================================== #
def test_leak_canary_catches_gold_path(tmp_path):
    assert g.leak_canary(["src/foo.py"], "owner__repo-1", ["src/foo.py"]) == ["gold_path:foo.py"]
    assert g.leak_canary(["src/foo.py"], "owner__repo-1", ["tests/gold_test.py"]) == []
    # a leaked gold path in a delivered identity must flip publishable=False.
    rec = g.collect_task("owner__repo-1",
                         _write_task(tmp_path, messages=[_asst("ls")],
                                     ledger_rows=[_delivered("l3.contract", file="src/secret_gold.py")]),
                         profile="2", gold_paths=["src/secret_gold.py"])
    assert rec["integrity"]["leak_count"] == 1
    agg = g.aggregate_run("run-y", [rec], profile="2")
    assert agg["integrity"]["publishable"] is False
    # MUTATION: skipping the leak check in the aggregate would keep publishable=True.


def test_integrity_scans_exact_delivered_payload_for_test_identity_leaks(tmp_path):
    payload = "candidate.py:4 FAIL_TO_PASS tests/x.py::test_hidden_case"
    rows = [_delivered(
        "gateway.trace_frame", chars=len(payload),
        content_sha256_16=_sha16(payload), native_text=payload,
    )]
    rec = g.collect_task(
        "synthetic__payload-leak",
        _write_task(tmp_path, messages=[_tool(payload)], ledger_rows=rows),
        profile="2",
    )

    assert rec["integrity"]["leak_count"] >= 1
    assert "FAIL_TO_PASS" in rec["integrity"]["leak_canary"]
    assert g.aggregate_run("run-leak", [rec], profile="2")["integrity"]["publishable"] is False


def test_integrity_rejects_more_than_one_gt_dose_in_an_observation(tmp_path):
    first = "src/a.py:1 preserve alpha"
    second = "src/b.py:2 preserve beta"
    rows = [
        _delivered("gateway.trace_frame", chars=len(first), content_sha256_16=_sha16(first)),
        _delivered("edit.syntax", chars=len(second), content_sha256_16=_sha16(second)),
    ]
    rec = g.collect_task(
        "synthetic__double-dose",
        _write_task(tmp_path, messages=[_tool(first + "\n" + second)], ledger_rows=rows),
        profile="2",
    )

    assert rec["integrity"]["max_dose_per_observation"] == 2
    assert rec["integrity"]["dose_violation_count"] == 1
    assert g.aggregate_run("run-dose", [rec], profile="2")["integrity"]["publishable"] is False


def test_integrity_groups_parallel_tool_results_into_one_model_observation(tmp_path):
    """Every contiguous tool-result batch feeds one subsequent model policy call."""
    first = "src/a.py:1 preserve alpha"
    second = "src/b.py:2 preserve beta"
    rows = [
        _delivered("gateway.trace_frame", chars=len(first),
                   content_sha256_16=_sha16(first)),
        _delivered("edit.syntax", chars=len(second),
                   content_sha256_16=_sha16(second)),
    ]
    messages = [
        _asst("parallel tool calls"),
        _tool(first),
        _tool(second),
        _asst("consume both results"),
    ]

    rec = g.collect_task(
        "synthetic__parallel-double-dose",
        _write_task(tmp_path, messages=messages, ledger_rows=rows),
        profile="2",
    )

    assert rec["integrity"]["max_dose_per_observation"] == 2
    assert rec["integrity"]["dose_violation_count"] == 1


# =========================================================================== #
# stale delivered fact => FIX verdict, zero efficacy credit
# =========================================================================== #
def test_stale_delivered_gets_fix_and_no_efficacy(tmp_path):
    rows = [_delivered("l3.contract", event="post_edit", file="x.py", chars=50, reason="stale_graph")]
    rec = g.collect_task("synthetic__stale",
                         _write_task(tmp_path, messages=[_asst("edit x.py")], ledger_rows=rows,
                                     submission="diff --git a/x.py b/x.py"),
                         profile="2")
    cc = rec["fact_classes"]["caller_contract"]
    assert cc["stale"]["value"] is True
    # The FACT owns the stale delivery. Its CAP mediator links the fact but must
    # not inherit the FACT timing failure as if it owned those bytes.
    assert rec["features"]["GT_CONTRACT_MODE"]["verdict"] == s.VERDICT_HOLD
    assert rec["features"]["GT_CONTRACT_MODE"]["lifecycle"]["stale"][
        "status"
    ] == "NOT_ELIGIBLE"
    # a stale delivered fact earns NO positive efficacy credit.
    assert cc["steps_saved"]["value"] is None
    # MUTATION: ignoring the delivered-row stale reason would drop FIX to HOLD.


# =========================================================================== #
# tool output cannot promote consumption (chronological receipt grading only)
# =========================================================================== #
def test_tool_output_cannot_promote_consumption(tmp_path):
    # a brief block is delivered; a TOOL message repeats the entity but NO assistant acts.
    brief = '<gt-contract file="auth.py">def get_user [CALLERS] get_user</gt-contract>'
    messages = [
        {"role": "user", "content": brief},           # brief delivery (channel=brief)
        _tool("get_user appears again in tool output only"),  # tool repeats it — must NOT promote
    ]
    rec = g.collect_task("synthetic__toolonly",
                         _write_task(tmp_path, messages=messages, ledger_rows=[]), profile="2")
    cc = rec["fact_classes"]["caller_contract"]
    # receipt (if measured) must be < 3 (acted) — a tool echo is not an agent action.
    lvl = cc["receipt_level"]["value"]
    assert lvl is None or lvl < 3
    # MUTATION: promoting a receipt from tool output would push this to >=3.


def test_ss_readiness_is_conjunctive_and_requires_live_witness():
    lifecycle = s.new_lifecycle("fixture")
    lifecycle.update({
        "delivered": s.measured(True),
        "truth_valid": s.measured(True),
        "authority_valid": s.measured(True),
        "stale": s.measured(False),
        "expired_late": s.measured(False),
        "receipt_level": s.measured(3),
    })

    offline = g.ss_gate_readiness(
        lifecycle, byte_proven=True, leak_free=True, dose_ok=True,
        fair_probe=True, live_witness=False, chronological_time=True,
    )
    assert offline["ss_live"] is False
    assert offline["gates"]["acknowledged"] is True
    assert "live_witness" in offline["blockers"]

    live = g.ss_gate_readiness(
        lifecycle, byte_proven=True, leak_free=True, dose_ok=True,
        fair_probe=True, live_witness=True, chronological_time=True,
    )
    assert live["ss_live"] is True


def test_no_late_counter_is_not_authoritative_chronological_proof():
    lifecycle = s.new_lifecycle("fixture")
    lifecycle.update({
        "delivered": s.measured(True),
        "truth_valid": s.measured(True),
        "authority_valid": s.measured(True),
        "stale": s.measured(False),
        "expired_late": s.measured(False),
        "receipt_level": s.measured(3),
    })

    readiness = g.ss_gate_readiness(
        lifecycle, byte_proven=True, leak_free=True, dose_ok=True,
        fair_probe=True, live_witness=False,
    )

    assert readiness["gates"]["correct_rl_adhered_time"] is None


def test_collector_never_promotes_unknown_truth_or_fair_probe(tmp_path):
    payload = "src/pkg.py:12 preserve callers"
    row = _delivered(
        "l3.contract", event="file_view", file="src/pkg.py",
        chars=len(payload), content_sha256_16=_sha16(payload), native_text=payload,
    )
    rec = g.collect_task(
        "synthetic__readiness",
        _write_task(tmp_path, messages=[_tool(payload)], ledger_rows=[row]),
        profile="2",
    )
    readiness = rec["features"]["GT_CONTRACT_NATIVE"]["ss_readiness"]

    # The sealed row proves the FACT bytes, not which of several CAP mediators
    # produced them. Infra controls expose mediation links, never FACT gates.
    assert readiness["role"] == "infra_control"
    assert "delivered_byte_proven" not in readiness["gates"]
    assert readiness["gates"]["runtime_member_control_receipt"] is None
    assert readiness["gates"]["mediation_correct"] is None
    assert readiness["mediation"]["delivered_fact_ids"] == ["caller_contract"]
    assert readiness["ss_live"] is False


# =========================================================================== #
# baseline matched status flows through
# =========================================================================== #
def test_behavioural_endpoints_and_baseline_status(tmp_path):
    d = _write_task(tmp_path, messages=[_asst("ls"), _asst("edit a.py")], ledger_rows=[])
    rec = g.collect_task("synthetic__be", d, profile="2", baseline_root=None)
    be = rec["behavioural_endpoints"]
    assert be["baseline_status"] == s.BASELINE_UNAVAILABLE   # no baseline root supplied
    assert be["gt_on"]["total_steps"] == 2
    # MUTATION: a fabricated MATCHED status without a baseline trajectory would fail here.
