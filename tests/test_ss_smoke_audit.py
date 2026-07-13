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
    (and babel's ACCURATE detect.coherence m97 — 4 real writes, claimed 4 — stays clean)
  * >= 2 biting mutations:
        (1) break the step-behind acquisition recompute  -> known step_behind cases missed
        (2) break the leak word-boundary                 -> ordinary prose false-flags

When the arm-4 recording is absent the data-backed tests skip; the pure-unit and mutation
tests always run.
"""
from __future__ import annotations

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


# ══════════════════════════════════════════════════════════════════════════════
# REQUIRED: the three named violations MUST be found
# ══════════════════════════════════════════════════════════════════════════════
@arm4
def test_conan17092_m55_coherence_miscount_flagged():
    r = _audit("conan-io__conan-17092")
    assert "coherence_miscount" in _viol_kinds_at(r, 55)


@arm4
def test_babel_m73_late_obligation_flagged():
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
def test_babel_m97_accurate_coherence_not_flagged():
    """The ACCURATE coherence firing (4 real writes m64/m86/m94/m98, claimed 4) is clean."""
    r = _audit("python-babel__babel-1179")
    assert "coherence_miscount" not in _viol_kinds_at(r, 97)


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
# COHERENCE COUNT semantics — conan=2 (flag), babel=4 (no-flag), loguru=2, geopandas=3
# ══════════════════════════════════════════════════════════════════════════════
@arm4
def test_write_counter_conan_default_settings_is_two():
    import json
    msgs = json.loads((ARM4 / "conan-io__conan-17092" / "mini-swe-agent.trajectory.json"
                       ).read_text(encoding="utf-8"))["messages"]
    assert len(ssa._writes_to_basename(msgs, "default_settings.py")) == 2


@arm4
def test_write_counter_babel_dates_is_four():
    import json
    msgs = json.loads((ARM4 / "python-babel__babel-1179" / "mini-swe-agent.trajectory.json"
                       ).read_text(encoding="utf-8"))["messages"]
    assert len(ssa._writes_to_basename(msgs, "dates.py")) == 4


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
    assert "coherence_miscount" in _viol_kinds_at(r, home)


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
