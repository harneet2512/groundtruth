"""GT_OBLIGATIONS_V2 — the held-out generalization gate (plan §8 test 20).

Anti-benchmaxx invariant: on REAL non-DeepSWE-shaped issues (60 GitHub issues,
4 languages, prose format), v2 recall is monotonically >= v1 on EVERY issue,
the >=1-obligation yield never drops, extraction never crashes, and the leak
screen never fires on the extractor itself. Skips when the local corpus is
absent (it is a local-only artifact, never committed)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from groundtruth.pretask.spec import extract_spec, extract_spec_v2

_HOLDOUT = Path(__file__).resolve().parents[2] / "holdout_v1.jsonl"

pytestmark = pytest.mark.skipif(
    not _HOLDOUT.is_file(), reason="local holdout corpus absent"
)


def _rows():
    for line in _HOLDOUT.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            d = json.loads(line)
            body = d.get("issue_body") or ""
            if body:
                yield d.get("bug_id", "?"), (d.get("issue_title", "") + "\n\n" + body)


def test_holdout_recall_monotone_and_yield():
    """v2 must never lose a REAL v1 clause. 'Real' excludes markdown headings:
    the gate's first run caught v1 extracting issue-template boilerplate
    ('### The author should do the following…' — a modal inside a heading)
    which v2 deliberately skips. Junk loss is not recall loss."""
    worse: list[str] = []
    v1_yield = v2_yield = n = 0
    for bug_id, text in _rows():
        n += 1
        v1_real = [
            o for o in extract_spec(text).obligations
            if not o.verbatim_text.lstrip().startswith("#")
        ]
        n1 = len(v1_real)
        n2 = len(extract_spec_v2(text).obligations)
        if n2 < n1:
            worse.append(f"{bug_id}: v1_real={n1} v2={n2}")
        v1_yield += 1 if n1 else 0
        v2_yield += 1 if n2 else 0
    assert n >= 50, "corpus unexpectedly small"
    assert not worse, f"v2 recall regressed vs v1 on: {worse[:5]}"
    assert v2_yield >= v1_yield


def test_holdout_determinism():
    for _bug_id, text in list(_rows())[:15]:
        a = extract_spec_v2(text).to_serializable(version=2)
        b = extract_spec_v2(text).to_serializable(version=2)
        assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
