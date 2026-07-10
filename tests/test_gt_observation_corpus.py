#!/usr/bin/env python3
"""TTD fixture test for scripts/gt_observation_corpus.py (D0 observation-schema corpus).

RED-first: hand-crafts ONE tiny OpenHands-shaped (`output.jsonl`) and ONE tiny
mini-swe-shaped (`*.trajectory.json`) trajectory, each containing a known grep result
and a known test-fail, then asserts the extractor emits the expected canonical schema
(event class, sub-status, model-facing text-field PATH, fingerprint keypaths).

Determinism: same fixture dir -> byte-identical catalog JSON.

MUTATION handle: `text_field_for(shape, "search-result")` is the model-facing text
field path. The search-result assertions pin BOTH the path AND the extracted text; if
the text-field detection is broken for either shape, `model_text` diverges and the
test bites.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import gt_observation_corpus as oc  # noqa: E402  (hard import -> RED before the script exists)


# ---------------------------------------------------------------------------
# FIXTURES — tiny, hand-crafted, redaction-safe (no real repo content).
# ---------------------------------------------------------------------------
_OH_DOC = {
    "instruction": "<gt-localization confidence=\"high\">\nEdit target: a.py :: foo\n"
                   "</gt-localization>\nFix the foo bug.",
    "history": [
        {"source": "agent", "action": "run", "id": 1,
         "args": {"command": "grep -n foo a.py"}},
        {"source": "agent", "observation": "run", "id": 2, "cause": 1,
         "success": True, "content": "12:def foo():",
         "message": "Command `grep -n foo a.py` executed with exit code 0.",
         "extras": {"command": "grep -n foo a.py",
                    "metadata": {"exit_code": 0, "suffix": "\n[The command completed with exit code 0.]"}}},
        {"source": "agent", "action": "run", "id": 3,
         "args": {"command": "grep -n missing a.py"}},
        {"source": "agent", "observation": "run", "id": 4, "cause": 3,
         "success": False, "content": "",
         "message": "Command `grep -n missing a.py` executed with exit code 1.",
         "extras": {"command": "grep -n missing a.py",
                    "metadata": {"exit_code": 1, "suffix": "\n[The command completed with exit code 1.]"}}},
        {"source": "agent", "action": "run", "id": 5,
         "args": {"command": "python -m pytest test_a.py"}},
        {"source": "agent", "observation": "run", "id": 6, "cause": 5,
         "success": False, "content": "E   assert 1 == 2\nFAILED test_a.py::test_foo",
         "message": "Command `python -m pytest test_a.py` executed with exit code 1.",
         "extras": {"command": "python -m pytest test_a.py",
                    "metadata": {"exit_code": 1, "suffix": "\n[The command completed with exit code 1.]"}}},
        {"source": "agent", "action": "read", "id": 7, "args": {"path": "a.py"}},
        {"source": "agent", "observation": "read", "id": 8, "cause": 7,
         "content": "line1\nline2\nline3", "message": "I read the file a.py.",
         "extras": {"path": "a.py"}},
        {"source": "agent", "action": "edit", "id": 9, "args": {"path": "a.py"}},
        {"source": "agent", "observation": "edit", "id": 10, "cause": 9,
         "content": "The file a.py has been edited. Here's the result of running `cat -n`",
         "message": "I edited the file a.py.",
         "extras": {"path": "a.py", "diff": "@@ -1 +1 @@", "prev_exist": True}},
        {"source": "agent", "action": "finish", "id": 11,
         "args": {"final_thought": "done", "outputs": {}}},
    ],
}

_MINI_DOC = {
    "trajectory_format": "mini-swe-agent-1.1",
    "info": {"exit_status": "Submitted", "submission": "diff --git a/a.py b/a.py"},
    "messages": [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user",
         "content": "<pr_description>\n<gt-localization confidence=\"high\">\n"
                    "Edit target: a.py :: foo\n</gt-localization>\n</pr_description>"},
        {"role": "assistant", "content": "search",
         "extra": {"actions": [{"command": "grep -rn foo .", "tool_call_id": "c1"}]}},
        {"role": "tool", "content": "<returncode>0</returncode>\n<output>\n12:def foo():\n</output>",
         "extra": {"returncode": 0, "exception_info": "", "raw_output": "12:def foo():"}},
        {"role": "assistant", "content": "test",
         "extra": {"actions": [{"command": "python -m pytest test_a.py", "tool_call_id": "c2"}]}},
        {"role": "tool", "content": "<returncode>1</returncode>\n<output>\nFAILED test_a.py::test_foo\n</output>",
         "extra": {"returncode": 1, "exception_info": "", "raw_output": "FAILED"}},
        {"role": "exit", "content": "diff --git a/a.py b/a.py",
         "extra": {"exit_status": "Submitted", "submission": "diff --git a/a.py b/a.py"}},
    ],
}


@pytest.fixture()
def fixture_dir(tmp_path):
    oh = tmp_path / "oh_run" / "results" / "X" / "CodeActAgent" / "m"
    oh.mkdir(parents=True)
    with open(oh / "output.jsonl", "w", encoding="utf-8") as fh:
        fh.write(json.dumps(_OH_DOC) + "\n")
    mini = tmp_path / "mini_run" / "jobs" / "jobs" / "d" / "task__ABC" / "agent"
    mini.mkdir(parents=True)
    with open(mini / "mini-swe-agent.trajectory.json", "w", encoding="utf-8") as fh:
        json.dump(_MINI_DOC, fh)
    return tmp_path


# ---------------------------------------------------------------------------
# SHAPE DETECTION
# ---------------------------------------------------------------------------
def test_detect_shape():
    assert oc.detect_shape("a/b/output.jsonl") == "output_jsonl"
    assert oc.detect_shape("a/b/mini-swe-agent.trajectory.json") == "miniswe_trajectory"


# ---------------------------------------------------------------------------
# OH EXTRACTION
# ---------------------------------------------------------------------------
def test_oh_search_result_ok():
    events = oc.extract_events(_OH_DOC, "output_jsonl")
    hits = [e for e in events if e["event_class"] == "search-result"]
    assert hits, "expected a search-result event from the OH grep obs"
    ok = [e for e in hits if e["sub_status"] == "ok"]
    assert ok, "expected an OK (non-empty) search-result"
    e = ok[0]
    assert e["model_text_field"] == "history[].content"
    assert e["model_text"] == "12:def foo():"


def test_oh_search_result_empty():
    events = oc.extract_events(_OH_DOC, "output_jsonl")
    empty = [e for e in events
             if e["event_class"] == "search-result" and e["sub_status"] == "empty"]
    assert empty, "expected an EMPTY search-result (exit 1, blank content)"
    assert empty[0]["model_text"] == ""


def test_oh_test_run_fail():
    events = oc.extract_events(_OH_DOC, "output_jsonl")
    fails = [e for e in events
             if e["event_class"] == "test-run" and e["sub_status"] == "fail"]
    assert fails, "expected a test-run/fail from the pytest exit-1 obs"
    assert "FAILED" in fails[0]["model_text"]


def test_oh_file_view_and_edit_and_submit():
    events = oc.extract_events(_OH_DOC, "output_jsonl")
    classes = {e["event_class"] for e in events}
    assert "file-view" in classes
    assert "edit-result" in classes
    assert "submit-finish" in classes


def test_oh_fingerprint_contains_observation_keypath():
    events = oc.extract_events(_OH_DOC, "output_jsonl")
    e = next(e for e in events if e["event_class"] == "search-result")
    assert "history[].observation" in e["keypaths"]
    assert "history[].extras.metadata.exit_code" in e["keypaths"]


# ---------------------------------------------------------------------------
# MINI EXTRACTION
# ---------------------------------------------------------------------------
def test_mini_search_result_ok():
    events = oc.extract_events(_MINI_DOC, "miniswe_trajectory")
    ok = [e for e in events
          if e["event_class"] == "search-result" and e["sub_status"] == "ok"]
    assert ok, "expected an OK search-result from the mini grep tool obs"
    e = ok[0]
    assert e["model_text_field"] == "messages[].content"
    assert e["model_text"] == "<returncode>0</returncode>\n<output>\n12:def foo():\n</output>"


def test_mini_test_run_fail():
    events = oc.extract_events(_MINI_DOC, "miniswe_trajectory")
    fails = [e for e in events
             if e["event_class"] == "test-run" and e["sub_status"] == "fail"]
    assert fails, "expected a mini test-run/fail (returncode 1 after a pytest cmd)"
    assert "FAILED" in fails[0]["model_text"]


def test_mini_submit_finish():
    events = oc.extract_events(_MINI_DOC, "miniswe_trajectory")
    subs = [e for e in events if e["event_class"] == "submit-finish"]
    assert subs, "expected a submit-finish from role=exit"
    assert subs[0]["sub_status"] == "submitted"


def test_mini_fingerprint_contains_returncode_keypath():
    events = oc.extract_events(_MINI_DOC, "miniswe_trajectory")
    e = next(e for e in events if e["event_class"] == "search-result")
    assert "messages[].extra.returncode" in e["keypaths"]


# ---------------------------------------------------------------------------
# MUTATION HANDLE — the canonical text-field map, pinned per shape.
# ---------------------------------------------------------------------------
def test_text_field_map_is_canonical():
    assert oc.text_field_for("output_jsonl", "search-result") == "history[].content"
    assert oc.text_field_for("miniswe_trajectory", "search-result") == "messages[].content"
    # submit-finish is terminal / agent-authored -> no model-facing observation text.
    assert oc.text_field_for("output_jsonl", "submit-finish") is None
    assert oc.text_field_for("miniswe_trajectory", "submit-finish") is None


# ---------------------------------------------------------------------------
# CATALOG BUILD + DETERMINISM
# ---------------------------------------------------------------------------
def test_build_catalog_and_determinism(fixture_dir):
    cat1 = oc.build_catalog(str(fixture_dir))
    cat2 = oc.build_catalog(str(fixture_dir))
    s1 = json.dumps(cat1, indent=2, sort_keys=True, default=str)
    s2 = json.dumps(cat2, indent=2, sort_keys=True, default=str)
    assert s1 == s2, "catalog must be byte-identical for the same input dir"
    assert cat1["schema_version"]
    assert cat1["corpus"]["by_shape"]["output_jsonl"] == 1
    assert cat1["corpus"]["by_shape"]["miniswe_trajectory"] == 1
    # every catalogued class must name where the model-facing text lives (or None).
    for entry in cat1["event_classes"]:
        assert "model_facing_text_field" in entry
        assert "status" in entry
        assert entry["status"] in ("CAPTURED", "CAPTURED-UNSTABLE", "RARE", "ABSENT")


def test_catalog_search_and_test_classes_captured(fixture_dir):
    cat = oc.build_catalog(str(fixture_dir))
    idx = {(e["shape"], e["event_class"]): e for e in cat["event_classes"]}
    for shape in ("output_jsonl", "miniswe_trajectory"):
        assert (shape, "search-result") in idx
        assert (shape, "test-run") in idx
        assert idx[(shape, "search-result")]["seen_event_count"] >= 1
