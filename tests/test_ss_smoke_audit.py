#!/usr/bin/env python3
r"""Tests for scripts/swebench/ss_smoke_audit.py — the SS-9 automated smoke verdict tool.

Ground truth is the byte-verified case manifest ``tests/fixtures/ss_replay/cases.json`` (the
29-task arm-4 causal audit) replayed against the RECORDED arm-4 artifacts at
``D:/gt_runs/29236533134/art``. The suite proves, RED-first:

  * the tool FINDS the three named violations
        - conan-17092  detect.coherence m55  (claims 4 rewrites, only 2 real writes)
        - babel-1179   spec.obligation m73 + obligation.resurface m87  (delivered after GREEN)
        - loguru-1297  l3.contract m19        (tmp/patch_fix.py scratch provenance)
  * the tool does NOT false-flag the three known-good P5 deliveries
        - conan-17123  consensus.scope m25
        - geopandas    edit.syntax m101
        - dynaconf     edit.syntax m219
    (babel's detect.coherence m97 is also caught: its fourth write occurred after delivery)
  * >= 2 biting mutations:
        (1) break the step-behind acquisition recompute  -> known step_behind cases missed
        (2) break the leak word-boundary                 -> ordinary prose false-flags

When the arm-4 recording is absent the data-backed tests skip; the pure-unit and mutation
tests always run.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "scripts" / "swebench"))

import ss_smoke_audit as ssa  # noqa: E402  # pyright: ignore[reportMissingImports]  (runtime sys.path)

ARM4 = Path("D:/gt_runs/29236533134/art")
_HAVE_ARM4 = ARM4.is_dir() and (ARM4 / "conan-io__conan-17092" / "mini-swe-agent.trajectory.json").is_file()
arm4 = pytest.mark.skipif(not _HAVE_ARM4, reason="arm-4 recorded data not present at D:/gt_runs/29236533134/art")


# ── helpers ──────────────────────────────────────────────────────────────────
_CACHE: dict[str, ssa.TaskReport] = {}


def _audit(task: str) -> ssa.TaskReport:
    if task not in _CACHE:
        _CACHE[task] = ssa.audit_task(task, ARM4)
    return _CACHE[task]


def _viol_kinds_at(report: ssa.TaskReport, home: int) -> set[str]:
    return {v.kind for v in report.violations if v.home_msg == home}


def _step_behind_homes(report: ssa.TaskReport) -> set[int]:
    return {v.home_msg for v in report.violations if v.kind == "step_behind"}


def _command_message(cmd: str) -> dict:
    return {"role": "assistant", "tool_calls": [{"function": {
        "name": "bash", "arguments": json.dumps({"command": cmd})}}]}


# ══════════════════════════════════════════════════════════════════════════════
# REQUIRED: the three named violations MUST be found
# ══════════════════════════════════════════════════════════════════════════════
@arm4
def test_conan17092_m55_coherence_miscount_flagged():
    r = _audit("conan-io__conan-17092")
    assert "coherence_unmeasured" in _viol_kinds_at(r, 55)


@arm4
def test_babel_m73_complete_obligation_proof_is_late():
    r = _audit("python-babel__babel-1179")
    assert "late" in _viol_kinds_at(r, 73)


@arm4
def test_babel_m87_late_resurface_flagged():
    r = _audit("python-babel__babel-1179")
    assert "late" in _viol_kinds_at(r, 87)


@arm4
def test_loguru_m19_tmp_provenance_flagged():
    r = _audit("delgan__loguru-1297")
    assert "provenance" in _viol_kinds_at(r, 19)


# ══════════════════════════════════════════════════════════════════════════════
# REQUIRED: the three known-good P5 deliveries MUST NOT be flagged
# ══════════════════════════════════════════════════════════════════════════════
@arm4
def test_conan17123_m25_consensus_scope_clean():
    r = _audit("conan-io__conan-17123")
    assert _viol_kinds_at(r, 25) == set()
    g = next(d for d in r.deliveries if d.home_msg == 25)
    assert g.pbucket == "P5"


@arm4
def test_geopandas_m101_edit_syntax_clean():
    r = _audit("geopandas__geopandas-3471")
    assert _viol_kinds_at(r, 101) == set()
    g = next(d for d in r.deliveries if d.home_msg == 101)
    assert g.pbucket == "P5"


@arm4
def test_dynaconf_m219_edit_syntax_clean():
    r = _audit("dynaconf__dynaconf-1225")
    assert _viol_kinds_at(r, 219) == set()


@arm4
def test_babel_m97_post_delivery_write_does_not_satisfy_claim():
    """Legacy arm-4 has no durable write receipts and must fail closed."""
    r = _audit("python-babel__babel-1179")
    assert "coherence_unmeasured" in _viol_kinds_at(r, 97)


@arm4
def test_geopandas_m103_executed_covering_not_unexecuted():
    """The EXECUTED covering verdict (real IndentationError traceback) carries evidence -> clean."""
    r = _audit("geopandas__geopandas-3471")
    assert "unexecuted_cover" not in _viol_kinds_at(r, 103)


@arm4
def test_conan17092_m69_advisory_not_unexecuted_cover():
    """'a covering test covers them - consider running' asserts NO verdict -> not flagged."""
    r = _audit("conan-io__conan-17092")
    assert "unexecuted_cover" not in _viol_kinds_at(r, 69)


@arm4
def test_conan17092_m71_source_build_pkg_not_provenance():
    """conan/tools/build/cppstd.py is a source package, not a generated build/ dir."""
    r = _audit("conan-io__conan-17092")
    assert "provenance" not in _viol_kinds_at(r, 71)


# ══════════════════════════════════════════════════════════════════════════════
# DELIVERY LEDGER JOIN — every delivered seal must byte-join
# ══════════════════════════════════════════════════════════════════════════════
@arm4
@pytest.mark.parametrize("task", ["conan-io__conan-17092", "python-babel__babel-1179",
                                  "delgan__loguru-1297", "geopandas__geopandas-3471"])
def test_every_delivery_byte_joins(task):
    r = _audit(task)
    assert r.residual_leaks == []
    assert r.unjoined == []
    assert all(d.joined for d in r.deliveries)


# ══════════════════════════════════════════════════════════════════════════════
# COHERENCE COUNT semantics — successful writes in the current post-GREEN streak, before delivery
# ══════════════════════════════════════════════════════════════════════════════
@arm4
def test_write_counter_conan_default_settings_is_two():
    import json
    msgs = json.loads((ARM4 / "conan-io__conan-17092" / "mini-swe-agent.trajectory.json"
                       ).read_text(encoding="utf-8"))["messages"]
    assert len(ssa._writes_to_basename(msgs, "default_settings.py")) == 2


@arm4
def test_write_counter_babel_dates_is_two_at_stale_delivery():
    import json
    msgs = json.loads((ARM4 / "python-babel__babel-1179" / "mini-swe-agent.trajectory.json"
                       ).read_text(encoding="utf-8"))["messages"]
    passing = ssa.passing_test_msgs(msgs)
    assert ssa._writes_to_basename(msgs, "dates.py", before=97, passing=passing) == [86, 94]


def test_write_counter_uses_green_boundary_delivery_boundary_and_tool_result():
    import json

    def command(cmd):
        return {"role": "assistant", "tool_calls": [{"function": {
            "name": "bash", "arguments": json.dumps({"command": cmd})}}]}

    msgs = [
        command("sed -i s/old/new/ pkg/dates.py"),
        {"role": "tool", "content": "<returncode>0</returncode>\nchanged"},
        command("pytest -q"),
        {"role": "tool", "content": "<returncode>0</returncode>\n3 passed"},
        command("sed -i s/a/b/ pkg/dates.py"),
        {"role": "tool", "content": "<returncode>0</returncode>\nchanged"},
        command("python -c \"open('pkg/dates.py','w').write('x')\""),
        {"role": "tool", "content": "<returncode>1</returncode>\npermission denied"},
        command("python -c \"open('pkg/dates.py','a').write('y')\""),
        {"role": "tool", "content": "<returncode>0</returncode>\nchanged"},
        {"role": "tool", "content": "you have rewritten dates.py 2 times"},  # delivery home
        command("sed -i s/b/c/ pkg/dates.py"),
        {"role": "tool", "content": "<returncode>0</returncode>\nchanged"},
    ]

    passing = ssa.passing_test_msgs(msgs)
    assert passing == [3]
    assert ssa._writes_to_basename(msgs, "dates.py", before=10, passing=passing) == [4, 8]


def test_write_counter_requires_explicit_success_and_pairs_by_tool_call_id():
    """Only an explicitly successful result for the matching call proves a write.

    A same-byte rc=0 write still counts: that is the producer's documented coherence
    semantics.  The auditor cannot silently turn a missing return code into success or
    attach an out-of-order failure to the wrong command.
    """
    msgs = [{
        "role": "assistant",
        "tool_calls": [
            {"id": "write", "function": {"name": "bash", "arguments":
                json.dumps({"command": "sed -i s/a/a/ pkg/dates.py"})}},
            {"id": "other", "function": {"name": "bash", "arguments":
                json.dumps({"command": "printf ignored"})}},
        ],
    }, {
        "role": "tool", "tool_call_id": "other",
        "content": "<returncode>1</returncode>\nfailed",
    }, {
        "role": "tool", "tool_call_id": "write",
        "content": "<returncode>0</returncode>\nno textual delta",
    }, _command_message("sed -i s/a/b/ pkg/dates.py"), {
        "role": "tool", "content": "changed but return code absent",
    }]

    assert ssa._writes_to_basename(msgs, "dates.py") == [0]


def test_coherence_receipts_count_exact_path_successful_byte_changes_after_latest_green():
    rows = [
        {"event_type": "source_write_proof", "file_path": "pkg/dates.py",
         "action_step": 2, "write_ok": True, "bytes_changed": True},
        {"event_type": "test_proof", "action_step": 3, "passed": True},
        {"event_type": "source_write_proof", "file_path": "other/dates.py",
         "action_step": 4, "write_ok": True, "bytes_changed": True},
        {"event_type": "source_write_proof", "file_path": "pkg/dates.py",
         "action_step": 5, "write_ok": False, "bytes_changed": True},
        {"event_type": "source_write_proof", "file_path": "pkg/dates.py",
         "action_step": 6, "write_ok": True, "bytes_changed": False},
        {"event_type": "source_write_proof", "file_path": "pkg/dates.py",
         "action_step": 7, "write_ok": True, "bytes_changed": True},
        {"event_type": "source_write_proof", "file_path": "pkg/dates.py",
         "action_step": 8, "write_ok": True, "bytes_changed": True},
        {"event_type": "source_write_proof", "file_path": "pkg/dates.py",
         "action_step": 9, "write_ok": True, "bytes_changed": True},
        {"outcome": "delivered", "content_sha256_16": "seal"},
        {"event_type": "source_write_proof", "file_path": "pkg/dates.py",
         "action_step": 9, "write_ok": True, "bytes_changed": True},
    ]

    proof = ssa.coherence_write_proof(rows, "pkg/dates.py", delivery_iteration=9,
                                      delivery_seal="seal")

    assert proof.measured is True
    assert proof.count == 3
    assert proof.write_steps == (7, 8, 9)
    assert proof.latest_passing_test_step == 3


@pytest.mark.parametrize("rows,detail", [
    ([], "absent"),
    ([{"event_type": "source_write_proof", "file_path": "pkg/dates.py",
       "action_step": 2, "write_ok": True}], "malformed"),
    ([{"event_type": "source_write_proof", "file_path": "pkg/dates.py",
       "action_step": 2, "write_ok": True, "bytes_changed": True},
      {"event_type": "source_write_proof", "file_path": "pkg/dates.py",
       "action_step": 2, "write_ok": True, "bytes_changed": True}], "duplicate"),
    ([{"event_type": "source_write_proof", "file_path": "pkg/dates.py",
       "action_step": 2, "write_ok": True, "bytes_changed": True},
      {"event_type": "test_proof", "action_step": 2, "passed": True}], "ambiguous"),
])
def test_coherence_receipts_fail_closed_when_absent_or_ambiguous(rows, detail):
    rows = [*rows, {"outcome": "delivered", "content_sha256_16": "seal"}]
    proof = ssa.coherence_write_proof(rows, "pkg/dates.py", delivery_iteration=9,
                                      delivery_seal="seal")

    assert proof.measured is False
    assert proof.count is None
    assert detail in proof.reason


def test_coherence_grade_never_falls_back_to_trajectory_command_text(monkeypatch):
    delivery = ssa.sso.Delivery(
        iteration=9, event_type="semantic_drift", layer="detect.coherence",
        outcome="delivered", reason="", chars=38,
        payload="you have rewritten pkg/dates.py 1 times", file_path="pkg/dates.py",
        sha16="0123456789abcdef", home_msg=2,
    )
    msgs = [
        _command_message("sed -i s/a/b/ pkg/dates.py"),
        {"role": "tool", "content": "<returncode>0</returncode>\nchanged"},
        {"role": "tool", "content": delivery.payload},
    ]
    monkeypatch.setattr(ssa, "_writes_to_basename", lambda *_a, **_k: [0], raising=False)

    grade = ssa.grade_delivery(
        delivery, msgs, {}, [], {}, proof_rows=[], receipt_level=1,
    )

    violation = next(v for v in grade.violations if v.kind == "coherence_unmeasured")
    assert "UNMEASURED" in violation.detail


def test_coherence_grade_accepts_matching_durable_receipts():
    delivery = ssa.sso.Delivery(
        iteration=9, event_type="semantic_drift", layer="detect.coherence",
        outcome="delivered", reason="", chars=38,
        payload="you have rewritten pkg/dates.py 2 times", file_path="pkg/dates.py",
        sha16="0123456789abcdef", home_msg=0,
    )
    rows = [
        {"event_type": "test_proof", "action_step": 3, "passed": True},
        {"event_type": "source_write_proof", "file_path": "pkg/dates.py",
         "action_step": 7, "write_ok": True, "bytes_changed": True},
        {"event_type": "source_write_proof", "file_path": "pkg/dates.py",
         "action_step": 8, "write_ok": True, "bytes_changed": True},
        {"outcome": "delivered", "content_sha256_16": delivery.sha16},
    ]

    grade = ssa.grade_delivery(delivery, [{"role": "tool", "content": delivery.payload}],
                               {}, [], {}, proof_rows=rows)

    assert not ({"coherence_miscount", "coherence_unmeasured"}
                & {violation.kind for violation in grade.violations})


# ══════════════════════════════════════════════════════════════════════════════
# GENERALIZATION — not overfit to the six named tasks
# ══════════════════════════════════════════════════════════════════════════════
@arm4
@pytest.mark.parametrize("task,home", [
    ("aiogram__aiogram-1594", 23),
    ("deepset-ai__haystack-8489", 59),
    ("fonttools__fonttools-3682", 57),
    ("privacyidea__privacyidea-4223", 117),
])
def test_more_coherence_miscounts_flagged(task, home):
    r = _audit(task)
    assert "coherence_unmeasured" in _viol_kinds_at(r, home)


@arm4
@pytest.mark.parametrize("task,home", [
    ("beancount__beancount-931", 25),     # tmp/patch_leafonly.py
    ("iterative__dvc-10711", 31),         # tmp/patch_remote.py
])
def test_more_provenance_flagged(task, home):
    r = _audit(task)
    assert "provenance" in _viol_kinds_at(r, home)


@arm4
@pytest.mark.parametrize("task,home", [
    ("conan-io__conan-17123", 37),        # stale [edited,untested] after GREEN m33/m35
    ("arviz-devs__arviz-2413", 71),       # passing covering evidence m67/m69 preceded it
])
def test_more_late_obligations_flagged(task, home):
    r = _audit(task)
    assert "late" in _viol_kinds_at(r, home)


@arm4
def test_conan17123_m25_good_but_m37_late_same_task():
    """Delivery-granular: the same task holds a P5 good (m25) AND a late violation (m37)."""
    r = _audit("conan-io__conan-17123")
    assert _viol_kinds_at(r, 25) == set()
    assert "late" in _viol_kinds_at(r, 37)


# ══════════════════════════════════════════════════════════════════════════════
# ACKNOWLEDGMENT — independent later-reference probe finds acks
# ══════════════════════════════════════════════════════════════════════════════
@arm4
def test_acknowledgment_rate_nonzero():
    r = _audit("conan-io__conan-17092")
    assert r.ack_count > 0
    assert any(d.ack_independent >= 0 for d in r.deliveries)


# ══════════════════════════════════════════════════════════════════════════════
# VERDICT LOGIC (pure) — SMOKE_PASS iff violations==0 AND ack>0 AND all joined
# ══════════════════════════════════════════════════════════════════════════════
def _grade(home, layer="l3b.evidence", violations=None, ack=True, joined=True):
    g = ssa.DeliveryGrade(home_msg=home, iteration=home // 2, layer=layer, chars=100,
                          file_path="pkg/mod.py", payload_head="x", joined=joined)
    g.violations = list(violations or [])
    g.ack_independent = home + 1 if ack else -1
    return g


def _report(task, grades):
    return ssa.TaskReport(task=task, n_messages=50, resolved=True, deliveries=grades,
                          residual_leaks=[], ss_reason_counts={})


def test_verdict_smoke_pass_on_clean_acked_joined():
    rep = _report("t1", [_grade(9), _grade(11)])
    assert rep.smoke_pass is True
    out = ssa.build_report([rep])
    assert out["verdict"] == "SMOKE_PASS"


def test_verdict_smoke_fail_on_any_violation():
    v = ssa.Violation("late", "b", 9, "spec.obligation", "after GREEN", "head")
    rep = _report("t1", [_grade(9, layer="spec.obligation", violations=[v])])
    assert rep.smoke_pass is False
    assert ssa.build_report([rep])["verdict"] == "SMOKE_FAIL"


def test_verdict_smoke_fail_on_zero_ack():
    rep = _report("t1", [_grade(9, ack=False)])
    assert rep.ack_count == 0
    assert rep.smoke_pass is False


def test_verdict_smoke_fail_on_unjoined():
    g = _grade(9, joined=False)
    g.violations = [ssa.Violation("unjoined", "1", -1, "l3b.evidence", "seal missing", "h")]
    rep = _report("t1", [g])
    assert rep.unjoined and rep.smoke_pass is False


def test_dose_flags_second_payload_same_home():
    grades = [_grade(9, violations=[]), _grade(9, violations=[])]
    ssa.apply_dose(grades)
    assert not grades[0].violations
    assert grades[1].violations and grades[1].violations[0].kind == "dose"


def test_p5_requires_acted_receipt_not_reference_only():
    referenced = ssa.DeliveryGrade(
        home_msg=9, iteration=4, layer="edit.syntax", chars=50,
        file_path="pkg/mod.py", payload_head="syntax", joined=True,
        receipt_level=2,
    )
    acted = ssa.DeliveryGrade(
        home_msg=9, iteration=4, layer="edit.syntax", chars=50,
        file_path="pkg/mod.py", payload_head="syntax", joined=True,
        receipt_level=3,
    )

    assert referenced.acknowledged is True
    assert ssa._assign_pbucket(referenced) != "P5"
    assert ssa._assign_pbucket(acted) == "P5"


def test_scope_p5_accepts_later_target_inspection_as_scope_action():
    scope = ssa.DeliveryGrade(
        home_msg=9, iteration=4, layer="consensus.scope", chars=50,
        file_path="pkg/mod.py", payload_head="scope", joined=True,
        receipt_level=2, acted_independent=12,
    )

    assert ssa._assign_pbucket(scope) == "P5"


def test_shadow_holdout_row_skipped_not_graded(tmp_path):
    """SS-8 shadow-holdout rows (outcome=shadow_holdout, chars=0) are counted separately and
    never enter the delivery grading (not a P1 chars-mismatch, not a dark delivery)."""
    import hashlib
    import json
    task = "acme__widget-1"
    d = tmp_path / task
    d.mkdir()
    delivered = "pkg/mod.py:10:foo() calls bar()"
    sha16 = hashlib.sha256(delivered.encode("utf-8")).hexdigest()[:16]
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "a", "tool_calls": [{"function": {
            "name": "bash", "arguments": json.dumps({"command": "cat pkg/mod.py"})}}]},
        {"role": "tool", "content": "native output line\n" + delivered},
    ]
    (d / "mini-swe-agent.trajectory.json").write_text(
        json.dumps({"messages": messages, "trajectory_format": "mini"}), encoding="utf-8")
    rows = [
        {"layer": "l3b.evidence", "event_type": "post_view", "file_path": "pkg/mod.py",
         "outcome": "delivered", "reason": "", "chars_delivered": len(delivered),
         "iteration": 1, "content_sha256_16": sha16},
        {"layer": "l3b.evidence", "event_type": "post_view", "file_path": "pkg/other.py",
         "outcome": "shadow_holdout", "reason": "", "chars_delivered": 0, "iteration": 1},
    ]
    (d / f"gt_runtime_ledger_{task}.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows), encoding="utf-8")

    rep = ssa.audit_task(task, tmp_path)
    assert rep.shadow_holdout_count == 1
    assert len(rep.deliveries) == 1                    # only the real sealed delivery
    assert rep.deliveries[0].file_path == "pkg/mod.py"
    assert all("shadow" not in v.kind for v in rep.violations)


# ══════════════════════════════════════════════════════════════════════════════
# UNIT: provenance whitelist / scratch classifier
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("path,scratch", [
    ("../tmp/patch_fix.py", True),
    ("tmp/patch_leafonly.py", True),
    ("/tmp/change_tracer.py", True),
    ("htmlcov/coverage_html_cb_x.js", True),
    ("build/lib/pkg/__pycache__/m.py", True),
    ("/testbed/geopandas/tools/_random.py", False),   # container repo root
    ("/usr/local/lib/python3.10/ast.py", False),      # stdlib
    ("conan/tools/build/cppstd.py", False),           # source pkg named 'build'
    ("dynaconf/utils/__init__.py", False),
])
def test_is_scratch_path(path, scratch):
    assert ssa.is_scratch_path(path) is scratch


# ══════════════════════════════════════════════════════════════════════════════
# UNIT: passing-test detection (drives the late check)
# ══════════════════════════════════════════════════════════════════════════════
def test_passing_test_msgs_detects_green_and_ignores_prose():
    msgs = [
        {"role": "assistant", "tool_calls": [{"function": {"name": "bash",
            "arguments": '{"command": "pytest tests/test_x.py"}'}}]},
        {"role": "tool", "content": "<returncode>0</returncode>\n2237 passed in 3.4s"},
        {"role": "assistant", "tool_calls": [{"function": {"name": "bash",
            "arguments": '{"command": "cat notes.txt"}'}}]},
        {"role": "tool", "content": "<returncode>0</returncode>\nthe word passed appears in prose"},
        {"role": "assistant", "tool_calls": [{"function": {"name": "bash",
            "arguments": '{"command": "pytest tests/test_y.py"}'}}]},
        {"role": "tool", "content": "<returncode>1</returncode>\n1 failed, 3 passed"},
    ]
    assert ssa.passing_test_msgs(msgs) == [1]  # only the real green pytest


def test_passing_test_msgs_requires_explicit_zero_returncode():
    msgs = [
        _command_message("pytest tests/test_inverse_match.py -q"),
        {"role": "tool", "content": "1 passed"},
    ]
    assert ssa.passing_test_msgs(msgs) == []


@pytest.mark.parametrize("output", [
    "<returncode>0</returncode>\n0 passed, 2 skipped",
    "<returncode>0</returncode>\ntest_inverse_match_bypassed was skipped",
    "<returncode>0</returncode>\ninverse_match was not passed",
])
def test_passing_test_msgs_rejects_nonpositive_pass_language(output):
    msgs = [
        _command_message("pytest tests/test_inverse_match.py -q"),
        {"role": "tool", "content": output},
    ]
    assert ssa.passing_test_msgs(msgs) == []


def test_passing_test_msgs_rejects_expected_pass_count_in_warning_prose():
    msgs = [
        _command_message("pytest tests/test_inverse_match.py -q"),
        {"role": "tool", "content":
            "<returncode>0</returncode>\n"
            "warning: expected 1 passed but collection produced none"},
    ]
    assert ssa.passing_test_msgs(msgs) == []


def test_obligation_late_requires_requirement_covering_green():
    """A green test is not evidence for every open issue obligation.

    The auditor may classify the obligation late only when the observed passing
    command/output names the obligation's code symbol. An unrelated green must
    leave the truthful edited-but-untested reminder on time.
    """
    payload = '[edited, untested] "Exercise inverse_match before submit"'
    obligation = ssa.sso.Delivery(
        layer="spec.obligation",
        event_type="test_result",
        iteration=2,
        chars=len(payload),
        sha16="a" * 16,
        home_msg=3,
        outcome="delivered",
        reason="",
        file_path="",
        payload=payload,
    )

    def _grade(test_command: str, test_output: str) -> ssa.DeliveryGrade:
        msgs = [
            {"role": "assistant", "tool_calls": [{"function": {
                "name": "bash", "arguments": '{"command": "' + test_command + '"}'}}]},
            {"role": "tool", "content": "<returncode>0</returncode>\n" + test_output},
            {"role": "assistant", "content": "reviewing the remaining requirement"},
            {"role": "tool", "content": obligation.payload},
        ]
        passing = ssa.passing_test_msgs(msgs)
        assert passing == [1]
        return ssa.grade_delivery(obligation, msgs, {}, passing, {})

    unrelated = _grade("pytest tests/test_parser.py -q", "test_parse_config PASSED\n1 passed")
    covering = _grade("pytest tests/test_inverse_match.py -q", "test_inverse_match PASSED\n1 passed")

    assert "late" not in {v.kind for v in unrelated.violations}
    assert "late" in {v.kind for v in covering.violations}


def test_behavioral_probe_requires_explicit_rc_checked_values_and_immediate_acceptance():
    payload = '[edited, untested] "Exercise inverse_match before submit"'
    checked = (
        "python3 -c \"# inverse_match\n"
        "from pkg.match import inverse_match\n"
        "print(f'first: {inverse_match(1)}') # True\n"
        "print(f'second: {inverse_match(2)}') # False\""
    )
    observed = "Test inverse_match\nfirst: True\nsecond: False\nAll tests passed"

    def evidence(tool_output: str, acceptance: list[dict]) -> int | None:
        msgs = [
            _command_message(checked),
            {"role": "tool", "content": tool_output},
            *acceptance,
            {"role": "tool", "content": payload},
        ]
        return ssa.requirement_evidence_before(payload, msgs, len(msgs) - 1, [])

    assert evidence(observed, [{"role": "assistant", "content": "That works correctly"}]) is None
    assert evidence(
        "<returncode>0</returncode>\n" + observed,
        [{"role": "assistant", "content": "Moving on"},
         {"role": "assistant", "content": "That works correctly"}],
    ) is None
    assert evidence(
        "<returncode>0</returncode>\n" + observed,
        [{"role": "assistant", "content": "All tests pass"}],
    ) == 1

    assert evidence(
        "<returncode>0</returncode>\n" + observed,
        [{"role": "assistant", "content": "This is not correct."}],
    ) is None

    literal_only = (
        "python3 -c \"# inverse_match\n"
        "print('first: True') # True\nprint('second: False') # False\""
    )
    literal_msgs = [
        _command_message(literal_only),
        {"role": "tool", "content": "<returncode>0</returncode>\n" + observed},
        {"role": "assistant", "content": "All tests pass"},
        {"role": "tool", "content": payload},
    ]
    assert ssa.requirement_evidence_before(payload, literal_msgs, 3, []) is None

    fabricated = [
        _command_message("python3 -c \"print('inverse_match tests passed')\""),
        {"role": "tool", "content": "<returncode>0</returncode>\ninverse_match tests passed"},
        {"role": "assistant", "content": "That works correctly"},
        {"role": "tool", "content": payload},
    ]
    assert ssa.requirement_evidence_before(payload, fabricated, 3, []) is None


def test_constructor_bound_behavioral_probe_uses_product_truth_and_auditor_ack():
    payload = '[edited, untested] "Exercise conanignore before submit"'
    command = """python3 - <<'PY'
from pkg.ignore import IgnoreMatcher
conanignore_path = '/tmp/config/.conanignore'
matcher = IgnoreMatcher(conanignore_path)
print(f'profiles entry: {matcher.matches("profiles/settings.yml")}')  # False
print(f'random entry: {matcher.matches("random.txt")}')  # True
PY"""
    output = "profiles entry: False\nrandom entry: True"

    def evidence(acknowledgment: str | None):
        messages = [
            _command_message(command),
            {"role": "tool", "content": "<returncode>0</returncode>\n" + output},
        ]
        if acknowledgment is not None:
            messages.append({"role": "assistant", "content": acknowledgment})
        messages.append({"role": "tool", "content": payload})
        return ssa.requirement_evidence_before(payload, messages, len(messages) - 1, [])

    assert evidence(None) is None
    assert evidence("All tests pass") == 1


def test_behavioral_probe_delegates_exact_transport_fields(monkeypatch):
    payload = '[edited, untested] "Exercise inverse_match before submit"'
    calls = []

    def classify(command, output, returncode, subjects, *, turn):
        calls.append((command, output, returncode, frozenset(subjects), turn))
        return object()

    monkeypatch.setattr(ssa, "classify_checked_behavioral_proof", classify)
    command = (
        "python3 -c \"from pkg.match import inverse_match; "
        "print(f'first: {inverse_match(1)}') # True; "
        "print(f'second: {inverse_match(2)}') # False\""
    )
    original_output = "first: True\nsecond: False"
    messages = [
        _command_message(command),
        {"role": "tool", "content": "<returncode>0</returncode>\n" + original_output},
        {"role": "assistant", "content": "All tests pass"},
        {"role": "tool", "content": payload},
    ]

    assert ssa.requirement_evidence_before(payload, messages, 3, []) == 1
    assert calls == [(command, original_output, 0, frozenset({"inverse_match"}), 1)]


def test_behavioral_probe_product_truth_does_not_bypass_auditor_ack(monkeypatch):
    payload = '[edited, untested] "Exercise inverse_match before submit"'
    monkeypatch.setattr(ssa, "classify_checked_behavioral_proof", lambda *a, **k: object())
    messages = [
        _command_message("python3 -c \"print('inverse_match')\""),
        {"role": "tool", "content": "<returncode>0</returncode>\ninverse_match"},
        {"role": "tool", "content": payload},
    ]

    assert ssa.requirement_evidence_before(payload, messages, 2, []) is None


def test_behavioral_probe_product_classifier_is_not_blocked_by_local_prefilter(monkeypatch):
    payload = '[edited, untested] "Handle alpha beta before submit"'
    monkeypatch.setattr(ssa, "classify_checked_behavioral_proof", lambda *a, **k: object())
    messages = [
        _command_message("python3 -c \"print('dynamic values')\""),
        {"role": "tool", "content": "<returncode>0</returncode>\ndynamic values"},
        {"role": "assistant", "content": "All tests pass"},
        {"role": "tool", "content": payload},
    ]

    assert ssa.requirement_evidence_before(payload, messages, 3, []) == 1


def test_smoke_parser_uses_canonical_independent_status_groups():
    payload = (
        '[edited, untested] "Preserve normalize_path behavior for Windows paths"\n'
        '[not addressed] "Raise ConfigError when policy loading fails"\n'
    )

    assert ssa._obligation_term_groups(payload) == [
        set(group) for group in ssa.rendered_obligation_subject_groups(payload)
    ]


def test_smoke_grades_each_independent_group_without_cross_row_deletion():
    payload = (
        '[edited, untested] "Exercise inverse_match before submit"\n'
        '[not addressed] "Preserve normalize_path before submit"\n'
    )
    messages = [
        _command_message("python3 -m pytest tests/test_inverse_match.py -q"),
        {"role": "tool", "content": "<returncode>0</returncode>\n1 passed inverse_match"},
        {"role": "tool", "content": payload},
    ]

    assert ssa.requirement_evidence_before(payload, messages, 2, [1]) == 1


def test_behavioral_probe_rejects_call_text_only_in_expected_comment():
    payload = '[edited, untested] "Exercise inverse_match before submit"'
    comment_only_calls = (
        "python3 -c \"# inverse_match\n"
        "print('first: True') # True {inverse_match(1)}\n"
        "print('second: False') # False {inverse_match(2)}\""
    )
    msgs = [
        _command_message(comment_only_calls),
        {"role": "tool", "content":
            "<returncode>0</returncode>\nfirst: True\nsecond: False"},
        {"role": "assistant", "content": "All tests passed"},
        {"role": "tool", "content": payload},
    ]

    assert ssa.requirement_evidence_before(payload, msgs, 3, []) is None


def test_behavioral_probe_rejects_unrelated_assert_with_literal_expected_got():
    payload = '[edited, untested] "Exercise inverse_match before submit"'
    unrelated_assert = (
        "python3 -c \"# inverse_match\n"
        "assert True\n"
        "print('Expected: good')\nprint('Got: good')\""
    )
    msgs = [
        _command_message(unrelated_assert),
        {"role": "tool", "content":
            "<returncode>0</returncode>\nExpected: good\nGot: good"},
        {"role": "assistant", "content": "All tests passed"},
        {"role": "tool", "content": payload},
    ]

    assert ssa.requirement_evidence_before(payload, msgs, 3, []) is None


def test_behavioral_probe_rejects_unrelated_dynamic_assignment_matrix():
    payload = '[edited, untested] "Exercise inverse_match before submit"'
    unrelated_assignment = (
        "python3 -c \"# inverse_match\n"
        "value = os.getcwd()\n"
        "print(f'inverse_match -> {value}')\n"
        "print('other -> literal')\""
    )
    msgs = [
        _command_message(unrelated_assignment),
        {"role": "tool", "content":
            "<returncode>0</returncode>\ninverse_match -> /tmp\nother -> literal"},
        {"role": "assistant", "content": "All tests passed"},
        {"role": "tool", "content": payload},
    ]

    assert ssa.requirement_evidence_before(payload, msgs, 3, []) is None


def test_behavioral_probe_rejects_unreachable_ternary_calls():
    payload = '[edited, untested] "Exercise inverse_match before submit"'
    unreachable_calls = (
        "python3 -c \"# inverse_match\n"
        "print(f'x: {True if True else inverse_match(1)}') # True\n"
        "print(f'y: {False if True else inverse_match(2)}') # False\""
    )
    msgs = [
        _command_message(unreachable_calls),
        {"role": "tool", "content":
            "<returncode>0</returncode>\nx: True\ny: False"},
        {"role": "assistant", "content": "All tests passed"},
        {"role": "tool", "content": payload},
    ]

    assert ssa.requirement_evidence_before(payload, msgs, 3, []) is None


def test_behavioral_probe_rejects_literal_requirement_prefix_on_unrelated_matrix_value():
    payload = '[edited, untested] "Exercise inverse_match before submit"'
    unrelated_matrix = (
        "python3 -c \"# inverse_match\n"
        "for label in ('a', 'b'):\n"
        "    value = os.getcwd()\n"
        "    print(f'{label} -> inverse_match {value}')\""
    )
    msgs = [
        _command_message(unrelated_matrix),
        {"role": "tool", "content":
            "<returncode>0</returncode>\n"
            "a -> inverse_match /tmp\nb -> inverse_match /tmp"},
        {"role": "assistant", "content": "All tests passed"},
        {"role": "tool", "content": payload},
    ]

    assert ssa.requirement_evidence_before(payload, msgs, 3, []) is None


def test_behavioral_probe_rejects_requirement_from_separate_literal_matrix_field():
    payload = '[edited, untested] "Exercise inverse_match before submit"'
    separate_literal_field = (
        "python3 -c \"# inverse_match\n"
        "term = 'inverse_match'\n"
        "for label in ('a', 'b'):\n"
        "    value = os.getcwd()\n"
        "    print(f'{label} -> {term} {value}')\""
    )
    msgs = [
        _command_message(separate_literal_field),
        {"role": "tool", "content":
            "<returncode>0</returncode>\n"
            "a -> inverse_match /tmp\nb -> inverse_match /tmp"},
        {"role": "assistant", "content": "All tests passed"},
        {"role": "tool", "content": payload},
    ]

    assert ssa.requirement_evidence_before(payload, msgs, 3, []) is None


def test_behavioral_probe_rejects_call_value_overwritten_before_matrix_print():
    payload = '[edited, untested] "Exercise inverse_match before submit"'
    overwritten_call_value = (
        "python3 -c \"# inverse_match\n"
        "for label in ('a', 'b'):\n"
        "    value = os.getcwd()\n"
        "    value = 'inverse_match'\n"
        "    print(f'{label} -> {value}')\""
    )
    msgs = [
        _command_message(overwritten_call_value),
        {"role": "tool", "content":
            "<returncode>0</returncode>\n"
            "a -> inverse_match\nb -> inverse_match"},
        {"role": "assistant", "content": "All tests passed"},
        {"role": "tool", "content": payload},
    ]

    assert ssa.requirement_evidence_before(payload, msgs, 3, []) is None


def test_behavioral_probe_rejects_call_value_augmented_before_matrix_print():
    payload = '[edited, untested] "Exercise inverse_match before submit"'
    augmented_call_value = (
        "python3 -c \"# inverse_match\n"
        "for label in ('a', 'b'):\n"
        "    value = os.getcwd()\n"
        "    value += 'inverse_match'\n"
        "    print(f'{label} -> {value}')\""
    )
    msgs = [
        _command_message(augmented_call_value),
        {"role": "tool", "content":
            "<returncode>0</returncode>\n"
            "a -> /tmpinverse_match\nb -> /tmpinverse_match"},
        {"role": "assistant", "content": "All tests passed"},
        {"role": "tool", "content": payload},
    ]

    assert ssa.requirement_evidence_before(payload, msgs, 3, []) is None


def test_behavioral_probe_rejects_negated_affirmative_phrases():
    msgs = [
        {"role": "tool", "content": "<returncode>0</returncode>"},
        {"role": "assistant", "content": ""},
        {"role": "tool", "content": "obligation"},
    ]
    for prose in (
        "This does not mean all tests passed.",
        "It is false that results are correct.",
    ):
        msgs[1]["content"] = prose
        assert ssa._assistant_accepted_probe(msgs, 0, 2) is False


def test_behavioral_probe_rejects_questions_and_exception_receipts():
    msgs = [
        {"role": "tool", "content": "<returncode>0</returncode>"},
        {"role": "assistant", "content": ""},
        {"role": "tool", "content": "obligation"},
    ]
    for prose in (
        "All tests passed?",
        "All tests passed? No.",
        "All tests passed; however, inverse_match failed.",
        "All tests passed except inverse_match failed.",
    ):
        msgs[1]["content"] = prose
        assert ssa._assistant_accepted_probe(msgs, 0, 2) is False


def test_behavioral_probe_rejects_later_or_unlisted_contradictions():
    msgs = [
        {"role": "tool", "content": "<returncode>0</returncode>"},
        {"role": "assistant", "content": ""},
        {"role": "tool", "content": "obligation"},
    ]
    for prose in (
        "All tests passed.\nBut inverse_match fails.",
        "All tests passed. Actually inverse_match still breaks.",
        "All tests passed, apart from inverse_match.",
    ):
        msgs[1]["content"] = prose
        assert ssa._assistant_accepted_probe(msgs, 0, 2) is False


def test_behavioral_probe_rejects_yet_unfortunately_and_conditional_receipts():
    msgs = [
        {"role": "tool", "content": "<returncode>0</returncode>"},
        {"role": "assistant", "content": ""},
        {"role": "tool", "content": "obligation"},
    ]
    for prose in (
        "All tests passed. Yet inverse_match fails.",
        "All tests passed. Unfortunately inverse_match still breaks.",
        "All tests passed if we ignore inverse_match.",
    ):
        msgs[1]["content"] = prose
        assert ssa._assistant_accepted_probe(msgs, 0, 2) is False


def test_behavioral_probe_rejects_contradiction_after_permitted_test_action():
    msgs = [
        {"role": "tool", "content": "<returncode>0</returncode>"},
        {
            "role": "assistant",
            "content": "All tests passed. Let me run tests. Yet inverse_match fails.",
            "tool_calls": [{"id": "verify", "function": {"name": "bash", "arguments":
                json.dumps({"command": "pytest -q"})}}],
        },
        {"role": "tool", "content": "obligation"},
    ]

    assert ssa._assistant_accepted_probe(msgs, 0, 2) is False


def test_requirement_evidence_handles_rows_separately_and_plain_terms_exactly():
    payload = (
        '[edited, untested] "Exercise inverse_match"\n'
        '[edited, untested] "Exercise cache_reset"'
    )
    msgs = [
        _command_message("pytest tests/test_inverse_match.py -q"),
        {"role": "tool", "content": "<returncode>0</returncode>\n1 passed"},
        _command_message("pytest tests/test_cache_reset.py -q"),
        {"role": "tool", "content": "<returncode>0</returncode>\n1 passed"},
        {"role": "tool", "content": payload},
    ]
    assert ssa.requirement_evidence_before(payload, msgs, 4, [1, 3]) == 1

    plain = '[edited, untested] "Exercise store"'
    storefront = [
        _command_message("pytest tests/test_storefront.py -q"),
        {"role": "tool", "content": "<returncode>0</returncode>\n1 passed"},
        {"role": "tool", "content": plain},
    ]
    assert ssa.requirement_evidence_before(plain, storefront, 2, [1]) is None

    unit = '[edited, untested] "Exercise unit"'
    united = [
        _command_message("pytest tests/test_united.py -q"),
        {"role": "tool", "content": "<returncode>0</returncode>\n1 passed"},
        {"role": "tool", "content": unit},
    ]
    assert ssa.requirement_evidence_before(unit, united, 2, [1]) is None

    prose = '[edited, untested] "Return None when cache is empty"'
    empty_cache = [
        _command_message("pytest tests/test_empty_cache.py -q"),
        {"role": "tool", "content": "<returncode>0</returncode>\n1 passed"},
        {"role": "tool", "content": prose},
    ]
    assert ssa.requirement_evidence_before(prose, empty_cache, 2, [1]) is None


def test_command_result_pairing_uses_tool_call_ids_out_of_order():
    msgs = [{
        "role": "assistant",
        "tool_calls": [
            {"id": "call_inverse", "function": {"name": "bash", "arguments":
                '{"command": "pytest tests/test_inverse_match.py -q"}'}},
            {"id": "call_other", "function": {"name": "bash", "arguments":
                '{"command": "pytest tests/test_parser.py -q"}'}},
        ],
    }, {
        "role": "tool", "tool_call_id": "call_other",
        "content": "<returncode>0</returncode>\n1 passed",
    }, {
        "role": "tool", "tool_call_id": "call_inverse",
        "content": "<returncode>0</returncode>\n1 passed",
    }]

    assert ssa._commands_by_tool_message(msgs) == {
        1: "pytest tests/test_parser.py -q",
        2: "pytest tests/test_inverse_match.py -q",
    }


def test_command_result_pairing_rejects_stale_id_before_unidentified_result():
    msgs = [{
        "role": "assistant",
        "tool_calls": [{"id": "expected", "function": {"name": "bash", "arguments":
            json.dumps({"command": "pytest tests/test_inverse_match.py -q"})}}],
    }, {
        "role": "tool", "tool_call_id": "different",
        "content": "<returncode>1</returncode>\nfailed",
    }, {
        "role": "tool",
        "content": "<returncode>0</returncode>\n1 passed",
    }]

    assert ssa._commands_by_tool_message(msgs) == {}
    assert ssa.passing_test_msgs(msgs) == []


def test_command_result_pairing_keeps_ids_aligned_after_malformed_call():
    msgs = [{
        "role": "assistant",
        "tool_calls": [
            {"id": "bad", "function": {"name": "bash", "arguments": "{"}},
            {"id": "good", "function": {"name": "bash", "arguments":
                json.dumps({"command": "pytest tests/test_inverse_match.py -q"})}},
        ],
    }, {
        "role": "tool", "tool_call_id": "good",
        "content": "<returncode>0</returncode>\n1 passed",
    }]

    assert ssa._commands_by_tool_message(msgs) == {
        1: "pytest tests/test_inverse_match.py -q",
    }


# ══════════════════════════════════════════════════════════════════════════════
# UNIT: leak scan catches real test-ids
# ══════════════════════════════════════════════════════════════════════════════
def test_leak_scan_catches_real_test_ids():
    assert ssa.leak_scan("tests/test_dates.py::test_week_number failed")
    assert ssa.leak_scan("FAIL_TO_PASS includes test_foo")
    assert ssa.leak_scan("something_test broke")


def test_leak_scan_clean_on_ordinary_prose():
    # the word-boundary + length guards keep English out of the scan
    assert ssa.leak_scan("run the fastest_run and the latest_results after the contest") == []
    assert ssa.leak_scan("you have rewritten dates.py 4 times with no passing test") == []


# ══════════════════════════════════════════════════════════════════════════════
# MUTATION 1 (biting) — break the step-behind acquisition recompute -> cases MISSED
# ══════════════════════════════════════════════════════════════════════════════
@arm4
def test_step_behind_found_on_arm4():
    """The independent acquisition recompute flags the manifest's conan-17092 step_behind set."""
    r = _audit("conan-io__conan-17092")
    manifest_step_behind = {9, 10, 13, 17, 28}
    assert manifest_step_behind <= _step_behind_homes(r)


@arm4
def test_step_behind_recompute_mutation_bites(monkeypatch):
    """If the acquisition recompute is broken (never acquires anything), the KNOWN step_behind
    cases are MISSED — proving the recomputation is load-bearing, not decorative."""
    # baseline (correct): the cases are flagged
    good = ssa.audit_task("conan-io__conan-17092", ARM4)
    assert {9, 10, 13, 17, 28} <= _step_behind_homes(good)
    # MUTATION: acquisition set is always empty -> no subject is ever "already read"
    monkeypatch.setattr(ssa, "acquisition_before", lambda _msgs, _home: set())
    broken = ssa.audit_task("conan-io__conan-17092", ARM4)
    missed = {9, 10, 13, 17, 28} & _step_behind_homes(broken)
    assert missed == set(), f"broken recompute should miss all step_behind cases, still flagged {missed}"


# ══════════════════════════════════════════════════════════════════════════════
# MUTATION 2 (biting) — break the leak word-boundary -> ordinary prose false-flags
# ══════════════════════════════════════════════════════════════════════════════
def test_leak_word_boundary_mutation_bites(monkeypatch):
    """The \\b boundary on the test_ pattern is load-bearing: without it, 'fastest_run' (which
    contains the substring 'test_run') false-flags as a test identifier."""
    prose = "the fastest_run and latest_results finished"
    # correct (word-boundary): clean
    assert ssa.leak_scan(prose) == []
    # MUTATION: strip the leading \b from the test_ pattern
    broken = [
        re.compile(r"\btests?/[^\s:'\"]+\.py\b"),
        re.compile(r"::test[A-Za-z0-9_]*\b"),
        re.compile(r"test_[A-Za-z0-9_]{3,}"),   # <-- boundary-less (the bug)
        re.compile(r"\b[A-Za-z0-9]{2,}_test\b"),
        re.compile(r"\bFAIL_TO_PASS\b|\bPASS_TO_PASS\b"),
    ]
    monkeypatch.setattr(ssa, "LEAK_PATTERNS", broken)
    hits = ssa.leak_scan(prose)
    assert hits, "boundary-less pattern must false-flag ordinary prose (proves the guard bites)"
    assert any("test_run" in h or "test_results" in h for h in hits)


# ══════════════════════════════════════════════════════════════════════════════
# END-TO-END — the tool runs on the whole recording and returns a coherent verdict
# ══════════════════════════════════════════════════════════════════════════════
@arm4
def test_end_to_end_two_task_smoke_fail():
    """The real smoke shape is a 2-task held-out run. arm-4 is the pre-SS-fix recording, so the
    verdict MUST be SMOKE_FAIL with a concrete, usable instance list (task, m#, kind, check)."""
    reports = [_audit("delgan__loguru-1297"), _audit("python-babel__babel-1179")]
    out = ssa.build_report(reports)
    assert out["verdict"] == "SMOKE_FAIL"
    assert out["total_violations"] > 0
    assert out["total_ack_count"] > 0
    for t in out["tasks"]:
        for v in t["violations"]:
            assert v["kind"] and v["check"] in {"a", "b", "c", "d", "e", "1"}


@arm4
def test_discover_finds_all_tasks():
    """The tool enumerates every auditable task dir in the run (sanity: the 29-task recording)."""
    assert len(ssa.discover_tasks(ARM4)) >= 2
