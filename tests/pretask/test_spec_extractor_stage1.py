"""Oracle Stage 1 — Issue-as-SPEC extractor (red->green).

Per `ORACLE_ARCHITECTURE_PLAN.md` §5.1 + §8 Stage 1.  Deterministic, rule-based
(NO LLM) decomposition of issue text into obligations.  The extractor is a
SEPARATE consumer of the issue text from anchors.py with OPPOSITE filtering: it
KEEPS the requirement words (`async`, `await`, `returns`, ...) that the anchor
extractor deliberately drops as localization noise (anchors.py:256-267).

RED before `spec.py` exists (ImportError); GREEN after.

LEG 1 (generalization): rules key on requirement GRAMMAR (modals, behavior verbs,
parenthesized API qualifiers, numbered steps), not on specific keywords/repos.
Validated on the HELD-OUT `holdout_v1.jsonl` corpus (60 issues, 4 languages:
python/rust/typescript/go) — NONE of which are the 10/14 run tasks.  Extraction
precision and the silence property are asserted on held-out only.

LEG 2 (product): the obligation set is the `issue_verbatim` candidate class — the
top-severity, un-misdirectable context the oracle re-positions at the decision
point (the arrow: correct context -> correct code).  Metric moved: extraction
precision (marker-bearing) on held-out issues, and yield (>=1 obligation rate).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from groundtruth.pretask.spec import (
    _API_QUALIFIER_RE,
    _BEHAVIOR_VERB_RE,
    _MODAL_RE,
    _qualifier_keywords,
    extract_spec,
)

_ROOT = Path(__file__).resolve().parents[2]
_HELDOUT = _ROOT / "holdout_v1.jsonl"


def _heldout_issues() -> list[dict]:
    if not _HELDOUT.exists():
        pytest.skip("holdout_v1.jsonl not present")
    with open(_HELDOUT, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _body(rec: dict) -> str:
    return (rec.get("issue_title") or "") + "\n\n" + (rec.get("issue_body") or "")


# ---------------------------------------------------------------------------
# Named requirement shapes the plan calls out (the run-task killers).
# ---------------------------------------------------------------------------
def test_async_requirement_kept_verbatim():
    """The aiomonitor killer: the single word 'async', verbatim in the issue,
    which anchors.py DROPS as a language keyword.  The spec keeps it as a
    checkable obligation."""
    issue = "The capture_snapshot method should be async (it currently blocks)."
    spec = extract_spec(issue)
    assert spec.obligations
    assert "async" in spec.all_keywords
    obl = next(o for o in spec.obligations if "async" in o.keywords)
    assert "async" in obl.checkable_forms
    assert obl.kind in ("behavior", "signature")
    assert "async" in obl.verbatim_text  # verbatim, not paraphrased


def test_returns_and_raises_behavior_obligations():
    issue = "get_user must return Optional[User].\nWhen the id is missing it should raise KeyError."
    spec = extract_spec(issue)
    kinds = {o.kind for o in spec.obligations}
    assert "behavior" in kinds or "error" in kinds
    # error obligation captured for the raise
    assert any("raise" in o.verbatim_text.lower() for o in spec.obligations)


def test_api_shape_qualifier_signature_obligation():
    issue = "Add `capture_snapshot(async, optional name, returns ID)` to the monitor."
    spec = extract_spec(issue)
    sig = [o for o in spec.obligations if o.kind == "signature"]
    assert sig, f"no signature obligation: {[o.kind for o in spec.obligations]}"
    o = sig[0]
    assert "capture_snapshot" in o.symbols
    assert {"async", "optional"} & o.keywords


def test_impl_trait_for_type_checkable_form():
    issue = "You must add `impl Trace for EvaluationHandle` so it can be traced."
    spec = extract_spec(issue)
    forms = set()
    for o in spec.obligations:
        forms |= o.checkable_forms
    assert "impl Trace for EvaluationHandle" in forms


def test_numbered_repro_steps_kept_verbatim():
    issue = "Steps to reproduce:\n1. Run the parser on empty input\n2. Observe the crash"
    spec = extract_spec(issue)
    repro = [o for o in spec.obligations if o.kind == "repro"]
    assert len(repro) >= 2
    assert any("Run the parser" in o.verbatim_text for o in repro)


# ---------------------------------------------------------------------------
# Correct-or-quiet — pure prose yields no obligations.
# ---------------------------------------------------------------------------
def test_plain_prose_yields_nothing():
    prose = (
        "This project is a web framework. The codebase contains many modules. "
        "Here is some background on how the system generally works."
    )
    assert extract_spec(prose).obligations == []


def test_empty_issue_safe():
    assert extract_spec("").obligations == []
    assert extract_spec(None).obligations == []  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# LEG 1 — generality on HELD-OUT issues (4 languages, none are run tasks).
# ---------------------------------------------------------------------------
def test_heldout_yield_rate():
    """A meaningful fraction of real held-out issues yield >=1 obligation — the
    extractor is not vacuous and not tuned to one repo/language."""
    issues = _heldout_issues()
    assert len(issues) >= 40
    with_obl = sum(1 for x in issues if extract_spec(_body(x)).obligations)
    rate = with_obl / len(issues)
    assert rate >= 0.5, f"held-out obligation yield only {rate:.0%}"


def test_heldout_precision_marker_bearing():
    """PRECISION on held-out: every non-repro obligation carries a genuine
    requirement marker (modal / behavior verb / kept API qualifier).  A
    non-circular precision proxy: the assertion re-derives the marker
    independently of how the obligation was classified."""
    issues = _heldout_issues()
    total = 0
    bad = 0
    for x in issues:
        for o in extract_spec(_body(x)).obligations:
            if o.kind == "repro":
                continue
            total += 1
            has_marker = bool(
                _MODAL_RE.search(o.verbatim_text) or _BEHAVIOR_VERB_RE.search(o.verbatim_text)
            )
            has_qual = any(
                _qualifier_keywords(mm.group(2))
                for mm in _API_QUALIFIER_RE.finditer(o.verbatim_text)
            )
            if not (has_marker or has_qual):
                bad += 1
    assert total >= 30, f"too few held-out obligations to judge precision ({total})"
    precision = 1.0 - bad / total
    assert precision >= 0.95, f"held-out marker precision {precision:.3f} < 0.95"


def test_heldout_all_languages_covered():
    """The extractor fires across all 4 held-out languages — language-agnostic by
    construction (it keys on English requirement grammar, not syntax)."""
    issues = _heldout_issues()
    fired_langs = set()
    for x in issues:
        if extract_spec(_body(x)).obligations:
            fired_langs.add(x.get("language"))
    assert {"python", "rust", "typescript", "go"} <= fired_langs, fired_langs


def test_determinism():
    """Same issue -> same obligations, bit-for-bit (no LLM, no sampling)."""
    issue = "foo must return a list. It should raise on empty input. async behavior expected."
    a = extract_spec(issue).to_serializable()
    b = extract_spec(issue).to_serializable()
    assert a == b


# ---------------------------------------------------------------------------
# Serialization shape (rides gt_issue_anchors.json — plan §5.1 plumbing).
# ---------------------------------------------------------------------------
def test_serializable_shape():
    spec = extract_spec("get_user must return Optional[User] (async).")
    ser = spec.to_serializable()
    assert isinstance(ser, list) and ser
    rec = ser[0]
    assert set(rec) == {"verbatim_text", "kind", "symbols", "keywords", "checkable_forms"}
    assert isinstance(rec["symbols"], list)
    assert all(isinstance(s, str) for s in rec["keywords"])
