"""SS-2 feature 3 (GT_SS_SUBMIT_RED) at the mini seam — SUBMIT CONSUMES THE OBSERVED RED.

Causal-audit context (run 29236533134): conan-17092 stamped submit_clean AFTER the agent
observed a gold-relevant test FAIL at m106 and rationalized it away. Graph covering SELECTION
was empty on 28/29 tasks (leaf helper / unindexed file / phantom node — the SS-2 diagnosis), so
the graph-covering submit gate had nothing to block on. GT_SS_SUBMIT_RED consumes the agent's
OWN unresolved observed test RED instead: ONE native pre-commit refusal, SINGLE DOSE (a 2nd
submit passes silent), ledger rows both times. Off -> byte-identical.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import gt_mini_patch as g  # noqa: E402


class Submitted(Exception):
    """A stand-in for mini-swe-agent's Submitted (matched by class NAME in the seam)."""


def _base(monkeypatch):
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    for k in ("GT_SS_SUBMIT_RED", "GT_VERIFY_EXECUTE"):
        monkeypatch.delenv(k, raising=False)
    g._reset_oracle_state()


def _capture(monkeypatch):
    recs: list = []
    monkeypatch.setattr(g, "_runtime_ledger_record", lambda **k: recs.append(k))
    return recs


# --------------------------------------------------------------------------- #
# touch-an-edited-surface relatedness
# --------------------------------------------------------------------------- #
def test_touches_edit_relatedness(monkeypatch):
    _base(monkeypatch)
    monkeypatch.setattr(g, "_oracle_edited_rels", {"pkg/mod_a.py"})
    # edited REL appears in the failing output traceback -> touches.
    assert g._ss_test_touches_edit("pytest -q", "pkg/mod_a.py:8: E assert\n1 failed")
    # the edited file's basename appears in the test command -> touches.
    assert g._ss_test_touches_edit("pytest -q tests/test_mod_a.py", "1 failed")
    # neither rel nor basename anywhere -> does NOT touch.
    assert not g._ss_test_touches_edit("pytest -q tests/test_other.py", "unrelated\n1 failed")
    # no edits yet -> never touches (a pre-existing failure is not the agent's RED).
    monkeypatch.setattr(g, "_oracle_edited_rels", set())
    assert not g._ss_test_touches_edit("pytest -q", "pkg/mod_a.py:8: E\n1 failed")


# --------------------------------------------------------------------------- #
# _ss_record_test builds / clears the unresolved-RED latch
# --------------------------------------------------------------------------- #
def test_record_test_sets_and_clears_last_failing(monkeypatch):
    _base(monkeypatch)
    monkeypatch.setattr(g, "_oracle_edited_rels", {"pkg/mod_a.py"})
    monkeypatch.setattr(g, "_action_count", 5)
    # a FAILING test touching the edit -> latch set.
    g._ss_record_test("pytest -q", "pkg/mod_a.py:8: E assert\n1 failed", failed=True, passed=False)
    assert g._ss_last_failing_test == {"cmd": "pytest -q", "step": 5}
    # a later PASSING test touching the edit -> latch cleared (went green).
    monkeypatch.setattr(g, "_action_count", 6)
    g._ss_record_test("pytest -q", "pkg/mod_a.py ok\n1 passed", failed=False, passed=True)
    assert g._ss_last_failing_test is None


def test_record_test_unrelated_fail_does_not_set(monkeypatch):
    _base(monkeypatch)
    monkeypatch.setattr(g, "_oracle_edited_rels", {"pkg/mod_a.py"})
    monkeypatch.setattr(g, "_action_count", 5)
    g._ss_record_test("pytest -q tests/test_other.py", "unrelated boom\n1 failed",
                      failed=True, passed=False)
    assert g._ss_last_failing_test is None  # not touching the edit -> not the agent's RED


def test_compiled_rust_test_binary_failure_reaches_submit_red(monkeypatch):
    """Cargo can execute the compiled test binary directly after discovery.  The
    command is not spelled ``cargo test``, but its native Rust test protocol is
    definitive test evidence and must reach the same unresolved-RED latch."""
    _base(monkeypatch)
    monkeypatch.setenv("GT_SS_SUBMIT_RED", "1")
    monkeypatch.setattr(g, "_oracle_edited_rels", {"core/engine/src/job.rs"})
    command = (
        "timeout 30 ./target/debug/deps/boa_engine-abc123 "
        "tests::job --nocapture"
    )
    output = "\n".join((
        "running 1 test",
        "thread 'tests::job' panicked at core/engine/src/job.rs:42:5:",
        "assertion failed: false",
        "test tests::job ... FAILED",
        "test result: FAILED. 0 passed; 1 failed; 0 ignored",
    ))

    g._augment_output(
        {"command": command},
        {"output": output, "returncode": 101},
    )

    assert g._ss_last_failing_test is not None, (
        "REASON: native compiled-test output was discarded before _ss_record_test"
    )
    assert g._ss_last_failing_test["cmd"] == command


# --------------------------------------------------------------------------- #
# _ss_submit_red_refusal — single dose, off byte-identical, ledger both times
# --------------------------------------------------------------------------- #
def test_refusal_off_is_quiet(monkeypatch):
    _base(monkeypatch)
    monkeypatch.setattr(g, "_ss_last_failing_test", {"cmd": "pytest -q", "step": 3})
    recs = _capture(monkeypatch)
    assert g._ss_submit_red_refusal() == ""       # flag off -> quiet
    assert recs == []


def test_refusal_no_unresolved_red_is_quiet(monkeypatch):
    _base(monkeypatch)
    monkeypatch.setenv("GT_SS_SUBMIT_RED", "1")
    monkeypatch.setattr(g, "_ss_last_failing_test", None)
    recs = _capture(monkeypatch)
    assert g._ss_submit_red_refusal() == ""
    assert recs == []


def test_refusal_single_dose_and_ledger(monkeypatch):
    _base(monkeypatch)
    monkeypatch.setenv("GT_SS_SUBMIT_RED", "1")
    monkeypatch.setattr(g, "_ss_last_failing_test", {"cmd": "pytest -q tests/test_widget.py", "step": 4})
    recs = _capture(monkeypatch)
    # 1st submit -> ONE refusal + a blocked ledger row.
    first = g._ss_submit_red_refusal()
    assert first.startswith("pre-commit hook failed:")
    # W5 (live leak run 29594276655): the echoed command passes _final_scrub, so the
    # test-file identity is REDACTED to `<test>` on this GT-delivered surface (both live
    # firings leaked the hidden-test path pre-W5 — native_render.py:449-475). The RED
    # signal survives; the agent has its own concrete command in its history.
    assert "pytest -q <test>" in first                        # scrubbed form of the agent's OWN command
    assert "tests/test_widget.py" not in first                # the raw path must NOT leak
    assert g._ss_submit_red_fired is True
    # 2nd submit -> SILENT (single dose) + an allow ledger row.
    second = g._ss_submit_red_refusal()
    assert second == ""
    blocked = [r for r in recs if r.get("outcome") == "submit_blocked" and r.get("reason") == "ss_submit_red"]
    allowed = [r for r in recs if r.get("outcome") == "submit_allow" and r.get("reason") == "ss_submit_red"]
    assert len(blocked) == 1 and len(allowed) == 1              # delivered then allow


def test_refusal_leak_safe_only_agent_command(monkeypatch):
    _base(monkeypatch)
    monkeypatch.setenv("GT_SS_SUBMIT_RED", "1")
    monkeypatch.setattr(g, "_ss_last_failing_test", {"cmd": "pytest -q", "step": 4})
    out = g._ss_submit_red_refusal()
    assert "<gt-" not in out


# --------------------------------------------------------------------------- #
# integration through the real submit chokepoint _gt_gate_submit_exception
# --------------------------------------------------------------------------- #
def _stub_head_allows(monkeypatch):
    """Force the graph-covering head to ALLOW (no hygiene block, no covering fail) so the
    SS submit-RED path is what decides — the 28/29 'graph gate dark' case."""
    monkeypatch.setattr(g, "_gt_submit_hygiene", lambda root: None)
    monkeypatch.setattr(g, "_gt_submit_covering", lambda root: (None, []))
    monkeypatch.setattr(g, "_root", lambda: "/repo")
    monkeypatch.setattr(g, "_build_env_executor", lambda: None, raising=False)


def test_gate_blocks_once_then_allows(monkeypatch):
    _base(monkeypatch)
    monkeypatch.setenv("GT_VERIFY_EXECUTE", "1")   # the chokepoint is only reached under this
    monkeypatch.setenv("GT_SS_SUBMIT_RED", "1")
    _stub_head_allows(monkeypatch)
    _capture(monkeypatch)
    monkeypatch.setattr(g, "_ss_last_failing_test", {"cmd": "pytest -q", "step": 4})
    exc = Submitted()
    # 1st submit -> BLOCK (a native refusal dict with returncode 1).
    blocked = g._gt_gate_submit_exception(object(), {"command": "echo done"}, exc)
    assert isinstance(blocked, dict) and blocked.get("returncode") == 1
    assert "pre-commit hook failed:" in blocked.get("output", "")
    assert g._ss_submit_red_fired is False
    pending = blocked.pop("_gt_pending_delivery")
    g._commit_precommitted_batch_dose({
        "payload": blocked["output"], "pending_delivery": pending,
    })
    assert g._ss_submit_red_fired is True
    # 2nd submit -> ALLOW (None -> the caller re-raises Submitted -> submission proceeds).
    allowed = g._gt_gate_submit_exception(object(), {"command": "echo done"}, exc)
    assert allowed is None


def test_completion_certificate_observes_submit_red_before_it_records(monkeypatch):
    _base(monkeypatch)
    monkeypatch.setenv("GT_VERIFY_EXECUTE", "1")
    monkeypatch.setenv("GT_SS_SUBMIT_RED", "1")
    monkeypatch.setenv("GT_COMPLETION_CERT", "1")
    _stub_head_allows(monkeypatch)
    monkeypatch.setattr(
        g, "_ss_last_failing_test", {"cmd": "pytest -q", "step": 4})
    recorded = []
    monkeypatch.setattr(g, "_gt_completion_cert_record", recorded.append)

    blocked = g._gt_gate_submit_exception(object(), {"command": "submit"}, Submitted())

    assert blocked is not None and blocked["returncode"] == 1
    assert len(recorded) == 1
    assert recorded[0].decision == "bounce_once"
    assert recorded[0].head_reason == "ss_submit_red"
    assert blocked["_gt_pending_delivery"]["verdict"].reason == "ss_submit_red"


def test_gate_without_formatter_ownership_does_not_spend_submit_red(monkeypatch):
    _base(monkeypatch)
    monkeypatch.setenv("GT_VERIFY_EXECUTE", "1")
    monkeypatch.setenv("GT_SS_SUBMIT_RED", "1")
    monkeypatch.setattr(g, "_ORACLE_ROUTE", False)
    _stub_head_allows(monkeypatch)
    records = _capture(monkeypatch)
    monkeypatch.setattr(g, "_ss_last_failing_test", {"cmd": "pytest -q", "step": 4})
    g._batch_context.set(None)

    def submit(_env, _action, *args, **kwargs):
        raise Submitted()

    with pytest.raises(Submitted):
        g._wrap_execute(submit)(object(), {"command": "submit"})

    assert g._ss_submit_red_fired is False
    assert g._gt_submit_bounce_count == 0
    assert not [row for row in records if row.get("outcome") == "delivered"]


def test_gate_allows_when_no_observed_red(monkeypatch):
    _base(monkeypatch)
    monkeypatch.setenv("GT_VERIFY_EXECUTE", "1")
    monkeypatch.setenv("GT_SS_SUBMIT_RED", "1")
    _stub_head_allows(monkeypatch)
    _capture(monkeypatch)
    monkeypatch.setattr(g, "_ss_last_failing_test", None)  # no unresolved RED
    assert g._gt_gate_submit_exception(object(), {"command": "echo done"}, Submitted()) is None


def test_gate_off_flag_is_byte_identical_allow(monkeypatch):
    _base(monkeypatch)
    monkeypatch.setenv("GT_VERIFY_EXECUTE", "1")   # chokepoint reached, but SUBMIT_RED off
    _stub_head_allows(monkeypatch)
    _capture(monkeypatch)
    monkeypatch.setattr(g, "_ss_last_failing_test", {"cmd": "pytest -q", "step": 4})
    # flag OFF -> the SS path is inert -> the head allow stands (None).
    assert g._gt_gate_submit_exception(object(), {"command": "echo done"}, Submitted()) is None
