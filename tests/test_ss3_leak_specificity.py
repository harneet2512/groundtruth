"""SS-3 defect-4 — LEAK-SCANNER SPECIFICITY.

The leak scanners flagged generic words ("test", "patch", "testbed") as leak hits
because short test-name tokens were substring-matched. The corrected scanner keys on
STRUCTURAL test identifiers only: ``::``-qualified ids, bare ``test_\\w{3,}`` function
names on a word boundary, and literal FAIL_TO_PASS/PASS_TO_PASS markers — never a naked
substring. Required fixtures: "no passing test between edits" prose does NOT flag, a
real FAIL_TO_PASS name DOES.

Each test carries a biting mutation note.
"""
from __future__ import annotations

import importlib.util
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CL_PATH = os.path.join(_ROOT, "scripts", "swebench", "consumption_ledger.py")
_spec = importlib.util.spec_from_file_location("consumption_ledger_ss3", _CL_PATH)
cl = importlib.util.module_from_spec(_spec)
sys.modules["consumption_ledger_ss3"] = cl
_spec.loader.exec_module(cl)

scan = cl.scan_test_identity_leaks


# --- the required false-positive fixtures: generic prose must NOT flag ------ #
def test_generic_test_prose_does_not_flag():
    assert scan("no passing test between edits") == []
    assert scan("run the project's own test suite before finishing") == []
    assert scan("apply the patch to /tmp/patch.txt then re-run") == []
    assert scan("the repo is checked out at /testbed") == []
    assert scan("these tests exercise the trainer") == []
    # MUTATION: substring-match a short token like "test"/"patch" → every one of these
    #           generic sentences false-flags a leak.


# --- the required true-positive fixture: a real F2P id DOES flag ------------ #
def test_real_fail_to_pass_id_flags():
    # the full ::-qualified id is flagged (its bare leaf/file-stem are ALSO genuine
    # test-identity leaks, so membership — not exact equality — is the contract).
    assert "tests/keras/test_trainer.py::test_fit_and_evaluate" in \
        scan("tests/keras/test_trainer.py::test_fit_and_evaluate")
    # class-qualified pytest id
    hits = scan("see pkg/x_test.py::TestTrainer::test_compile_metrics")
    assert "pkg/x_test.py::TestTrainer::test_compile_metrics" in hits
    # bare pytest function name (>=3 chars after test_)
    assert "test_fit_and_evaluate" in scan("the covering test test_fit_and_evaluate is red")
    # the FAIL_TO_PASS spec marker itself is a leak
    assert "FAIL_TO_PASS" in scan("FAIL_TO_PASS: ['tests/x.py::test_y']")
    # MUTATION: drop the ::-qualified / bare-name rules → the real F2P id no longer
    #           flags → answer leakage passes the gate silently.


# --- min-length + word-boundary guard --------------------------------------- #
def test_short_and_boundary_guards():
    # "testbed" and "test" have no test_-prefixed word → clean.
    assert scan("testbed") == []
    assert scan("test") == []
    assert scan("latest_version = 3") == []          # test_ not on a word boundary
    assert scan("contest_winner()") == []            # 'test' inside another word
    # bare name with < 3 chars after test_ is not flagged on its own (generic guard).
    assert scan("call test_x here") == []
    # but a <3-char leaf IS caught when ::-qualified (the id is specific).
    assert scan("tests/a.py::test_x") == ["tests/a.py::test_x"]


# --- known-F2P mode matches the full id / leaf, never a naked substring ----- #
def test_known_ids_full_and_leaf_match():
    f2p = ["tests/keras/test_trainer.py::test_fit_and_evaluate"]
    # clean prose stays clean even with the known id set supplied.
    assert scan("no passing test between edits", f2p) == []
    # the leaf name appearing bare is caught via the known-id leaf rule.
    assert "test_fit_and_evaluate" in scan("re-run test_fit_and_evaluate now", f2p)
    # a generic id whose leaf is too short is not turned into a substring probe.
    assert scan("edited trainer.py and ran the suite", ["a.py::test_x"]) == []


# --- the parametrize suffix on a leaf is tolerated -------------------------- #
def test_parametrized_leaf():
    hits = scan("tests/x.py::test_matrix[case-3]")
    assert any("test_matrix" in h for h in hits)


# --- wired into the v2 ledger output ---------------------------------------- #
def test_ledger_emits_leak_hits_field():
    clean = {"messages": [
        {"role": "user", "content": "<gt-task-brief>fix importer.py set_fields</gt-task-brief>"},
        {"role": "tool", "content": "<gt-evidence file=\"importer.py\">[CALLERS] set_fields</gt-evidence>"},
    ]}
    out = cl.build_consumption_ledger(clean)
    assert out["test_identity_leak_hits"] == []       # scrubbed GT bytes = clean

    leaky = {"messages": [
        {"role": "tool",
         "content": "<gt-evidence>covering: tests/x.py::test_boom must pass</gt-evidence>"},
    ]}
    out2 = cl.build_consumption_ledger(leaky)
    assert "tests/x.py::test_boom" in out2["test_identity_leak_hits"]
    # MUTATION: emit an empty list unconditionally → a real leak in a GT block goes
    #           unreported and a leaking arm reads as clean.
