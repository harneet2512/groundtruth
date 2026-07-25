"""PROBE-SET SHAPE VERIFICATION — a mislabeled task becomes a false feature failure.

A feature can only be PROVEN on a run that TRIGGERS it. So a probe set is only as good as its shape
claims, and a task listed as NEW-FILE that creates no file will report `newfile_precedent` as
trigger-absent — which is indistinguishable from "the feature is broken". That is the exact ambiguity
the probe set exists to remove, so a wrong label is worse than no label.

This is not hypothetical. The curated blind_ext selections were validated against gold-patch evidence
on 2026-07-25 and three of seven did not match their claimed shape:
  * matplotlib-29721 (listed NEW-FILE): creates 0 files, changes 0 signatures — neither shape.
  * smolagents-285   (listed SIGNATURE): only def-ish hit is a docstring typo fix
                                          ("recurse trough" -> "recurse through").
  * csvkit-1274      (listed SIGNATURE): zero def-ish lines.
Measured across the pools: only 6/15 of nf_pool genuinely create a file, and only 47/270 of sig_pool
genuinely change a signature — so 83% of the signature pool would never have triggered the feature.

These predicates are the SAME ones used to build the v2 set, kept here so the rule is executable
rather than a one-off script that ran once and was forgotten.
"""
from __future__ import annotations
import json
import os
import re

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_DATA = os.path.join(_ROOT, "benchmarks", "data", "swebench_live_lite.jsonl")
_PROBE = os.path.join(_ROOT, ".claude", "reports", "feature_coverage_probe_set_20260725.txt")


def _rows():
    return {json.loads(l)["instance_id"]: json.loads(l)
            for l in open(_DATA, encoding="utf-8")}


def creates_a_file(patch: str) -> list:
    """A real creation: the gold patch adds a file from /dev/null."""
    return re.findall(r"^--- /dev/null\n\+\+\+ b/(\S+)", patch or "", re.M)


def changes_a_signature(patch: str) -> list:
    """A REAL signature change: the same `def NAME(` is removed AND added with DIFFERENT parameter
    text. Deliberately strict — prose mentioning "function", a renamed variable, or a docstring edit
    must NOT qualify, because those produce a task that never fires signature_delta."""
    rem, add = {}, {}
    for line in (patch or "").splitlines():
        m = re.match(r"^([-+])\s*(?:async\s+)?def\s+(\w+)\s*\((.*)", line)
        if m:
            (rem if m.group(1) == "-" else add).setdefault(m.group(2), []).append(m.group(3))
    return [n for n in rem if n in add and rem[n] != add[n]]


def _all_probe_tasks():
    """v3 lists five tasks with no section headers — each satisfies ALL shapes."""
    if not os.path.exists(_PROBE):
        return []
    return [l.split("#")[0].strip() for l in open(_PROBE, encoding="utf-8")
            if l.strip() and not l.strip().startswith("#")]


def _section(name):
    if not os.path.exists(_PROBE):
        return []
    out, cur = [], False
    for line in open(_PROBE, encoding="utf-8"):
        s = line.strip()
        if s.startswith("##"):
            cur = name in s.upper()
            continue
        if cur and s and not s.startswith("#"):
            out.append(s)
    return out


@pytest.mark.skipif(not os.path.exists(_DATA), reason="dataset not present")
@pytest.mark.skipif(not os.path.exists(_PROBE), reason="probe set not present (local-only file)")
def test_every_new_file_task_actually_creates_a_file():
    rows, bad = _rows(), []
    for t in (_section("NEW-FILE") or _all_probe_tasks()):
        if t not in rows or not creates_a_file(rows[t].get("patch")):
            bad.append(t)
    assert not bad, (
        "listed NEW-FILE but the gold patch creates no file — newfile_precedent/GT_CHANGE_SURFACE "
        f"would report trigger-absent, which reads as a feature failure: {bad}")


@pytest.mark.skipif(not os.path.exists(_DATA), reason="dataset not present")
@pytest.mark.skipif(not os.path.exists(_PROBE), reason="probe set not present (local-only file)")
def test_every_signature_task_actually_changes_a_signature():
    rows, bad = _rows(), []
    for t in (_section("SIGNATURE") or _all_probe_tasks()):
        if t not in rows or not changes_a_signature(rows[t].get("patch")):
            bad.append(t)
    assert not bad, (
        "listed SIGNATURE but no def changed parameters — signature_delta/GT_PATCH_DELTA would "
        f"report trigger-absent, which reads as a feature failure: {bad}")


@pytest.mark.skipif(not os.path.exists(_DATA), reason="dataset not present")
@pytest.mark.skipif(not os.path.exists(_PROBE), reason="probe set not present (local-only file)")
def test_every_probe_task_resolves_in_the_dataset():
    rows = _rows()
    listed = (_section("NEW-FILE") + _section("SIGNATURE") + _section("VERIFIABLE")
              or _all_probe_tasks())
    missing = [t for t in listed if t not in rows]
    assert not missing, f"probe task ids that do not resolve (the run would fail): {missing}"


def test_the_predicates_reject_the_known_false_positives():
    """NON-VACUOUS: the exact shapes that fooled the v1 selection must still be rejected."""
    assert not changes_a_signature(
        "-    This function will recurse trough the nodes\n"
        "+    This function will recurse through the nodes\n"), "docstring prose passed as a signature change"
    assert not changes_a_signature("-def f(a, b):\n+def f(a, b):\n"), "identical params passed"
    assert changes_a_signature("-def f(a):\n+def f(a, b):\n"), "a real param change was rejected"
    assert not creates_a_file("--- a/x.py\n+++ b/x.py\n"), "an edit passed as a creation"
    assert creates_a_file("--- /dev/null\n+++ b/new.py\n"), "a real creation was rejected"
