"""GT_OBLIGATIONS_V2 — Stage-1 tests for the v2 extractor (plan §8, tests 1-7).

Fixtures are the REAL v1.0.0 instruction.md files (frozen from deep-swe@c33fa70e)
for the three measured 2026-07-08 miss cases. Each test pins the clause whose
absence lost a real run. Written RED-first against the stub.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from groundtruth.pretask.spec import extract_spec, extract_spec_v2

_FIX = Path(__file__).parent / "fixtures" / "obligations_v2"


def _load(name: str) -> str:
    return (_FIX / f"{name}.md").read_text(encoding="utf-8")


TRUE_MYTH = "true-myth-iterable-collection-combinators"
MOBLY = "mobly-grouped-test-barriers"
HAPPY_DOM = "happy-dom-abort-pending-body-reads"


_TEMPLATE_SEMANTICS = """Categorical input apparently not supported by `plot_hdi()`

**Describe the bug**
The function currently does not support categorical values.
The default `smooth=True` throws an implementation error.

**To Reproduce**
Returns:
Setting `smooth=False` does not return the expected plot.

**Expected behavior**
I expect the behavior to mirror `bambi.interpret`.

**Additional context**
Version 1.2.3.

---

I think a TypeError informing the user that categorical strings are unsupported
would be helpful.
"""


def _clauses_with_symbol(spec, sym: str):
    return [
        o for o in spec.obligations
        if sym in o.subject_symbols or sym in o.symbols
    ]


# ── 1. the true-myth killer: "Add `compact` and `filterMap` ..." ─────────────
def test_spec_v2_filtermap_extracts():
    spec = extract_spec_v2(_load(TRUE_MYTH))
    hits = _clauses_with_symbol(spec, "filterMap")
    assert hits, "filterMap clause not extracted (the 189/192 loss)"
    # the dual-signature clause must carry filterMap as a SUBJECT symbol
    assert any("filterMap" in o.subject_symbols for o in hits)
    assert any(o.kind == "signature" for o in hits), (
        "dual-signature clause must classify kind=signature"
    )


# ── 2. the mobly conditional-imperative (EARS template) ─────────────────────
def test_spec_v2_mobly_registered_objects_extracts():
    spec = extract_spec_v2(_load(MOBLY))
    hits = [
        o for o in spec.obligations
        if "paired 1:1" in o.verbatim_text or "use objects" in o.verbatim_text
    ]
    assert hits, "conditional-imperative 'If registered objects ... use objects' not extracted"


# ── 3. arrow mappings become ATOMIC error clauses with their own symbols ─────
def test_spec_v2_mobly_arrow_mappings_atomic():
    spec = extract_spec_v2(_load(MOBLY))
    neg = [o for o in spec.obligations if "timeout<0" in o.verbatim_text]
    zero = [o for o in spec.obligations if "timeout==0" in o.verbatim_text]
    assert neg and zero, "timeout arrow mappings not extracted"
    # atomic: the two mappings are SEPARATE clauses
    assert not any("timeout==0" in o.verbatim_text for o in neg), (
        "mappings not split — still one compound clause"
    )
    assert any(o.kind == "error" for o in neg)
    assert any("ValueError" in o.subject_symbols for o in neg)
    assert any(
        "TestError" in o.subject_symbols or "signals.TestError" in o.subject_symbols
        for o in zero
    )


# ── 4. the mobly Mode bullets (imperative bullets) each extract ──────────────
def test_spec_v2_bullet_mode_clauses():
    spec = extract_spec_v2(_load(MOBLY))
    text = " ".join(o.verbatim_text for o in spec.obligations)
    for marker in ("No entries", "Implicit", "Explicit"):
        assert marker in text, f"Mode bullet '{marker}' produced no obligation"


# ── 5. happy-dom compat clauses classify kind=compat ─────────────────────────
def test_spec_v2_happydom_compat():
    spec = extract_spec_v2(_load(HAPPY_DOM))
    compat = [o for o in spec.obligations if o.kind == "compat"]
    assert any("remain readable" in o.verbatim_text for o in compat), (
        "'bodies should remain readable after shutdown' must classify compat"
    )


def test_spec_v2_template_sections_exclude_observed_state_and_labels():
    """Issue-template observations are evidence/context, not requirements.

    The extractor must retain the reporter's expected/proposed outcomes without
    turning the title, current bug, reproduction result, or a label-only line
    into independent completion obligations.
    """
    rows = extract_spec_v2(_TEMPLATE_SEMANTICS).obligations
    text = [o.verbatim_text for o in rows]

    assert any("mirror `bambi.interpret`" in row for row in text)
    assert any("TypeError informing the user" in row for row in text)
    assert not any("apparently not supported" in row for row in text)
    assert not any("currently does not support" in row for row in text)
    assert not any("smooth=True" in row for row in text)
    assert not any("smooth=False" in row for row in text)
    assert "Returns:" not in text


def test_spec_v2_descriptive_section_keeps_explicit_requirement_grammar():
    issue = """**Describe the bug**
The current parser crashes on empty input. It must return an empty list instead.
"""
    text = [o.verbatim_text for o in extract_spec_v2(issue).obligations]
    assert not any("currently" in row for row in text)
    assert any("must return an empty list" in row for row in text)


@pytest.mark.parametrize(
    "title",
    [
        "fix(parser): handle invalid header names",
        "pkg: Correctly handle dependency upgrades",
        "refactor: replace the legacy state bridge",
    ],
)
def test_spec_v2_action_titles_are_requirements(title):
    rows = extract_spec_v2(title).obligations
    assert len(rows) == 1
    assert rows[0].modality_strength >= 2


def test_spec_v2_process_and_reproduction_sections_are_not_requirements():
    issue = """Add safe empty-input handling.

### To Reproduce
1. Run `parse(\"\")`; it must currently crash with ValueError.

### The author should do the following, if applicable
- [x] MUST run the formatter.
- [ ] Add documentation.

### Testing
Return `fallback_value` from the fixture.
"""
    text = [o.verbatim_text for o in extract_spec_v2(issue).obligations]
    assert text == ["Add safe empty-input handling"]


def test_spec_v2_normative_heading_promotes_plain_outcome():
    issue = """Parser output is inconsistent.

### Desired behavior
Empty input maps to an empty list.
"""
    rows = extract_spec_v2(issue).obligations
    assert [o.verbatim_text for o in rows] == ["Empty input maps to an empty list"]
    assert rows[0].modality == "expected"
    assert rows[0].modality_strength == 2


def test_spec_v2_feature_requests_exclude_status_and_open_design_questions():
    issue = """[enhancement] Add force removal for broken state

### What is your suggestion?
This still requires manual intervention and does not fit our needs to 100%.
It would be great if cleanup removed stale metadata before updating.
What shall happen when the source is unavailable?
Is it possible to add `--force` so `state.json` is removed when unreadable?
"""
    text = [o.verbatim_text for o in extract_spec_v2(issue).obligations]

    assert "Add force removal for broken state" in text
    assert any("cleanup removed stale metadata" in row for row in text)
    assert any("`--force`" in row and "unreadable" in row for row in text)
    assert not any("manual intervention" in row for row in text)
    assert not any("100%" in row for row in text)
    assert not any("What shall happen" in row for row in text)


# ── 6. inline code spans are never arrow-split ───────────────────────────────
def test_spec_v2_no_split_inside_code_spans():
    spec = extract_spec_v2(_load(TRUE_MYTH))
    for o in spec.obligations:
        assert o.verbatim_text.strip() != "(items)", "split inside a backtick span"
        # a mapping clause must never be born from the `(items) => result` span
        if o.kind == "error" and "=>" in o.verbatim_text:
            assert "(items) => result" not in o.verbatim_text


# ── 7. determinism: two runs -> byte-identical serialization ─────────────────
@pytest.mark.parametrize("name", [TRUE_MYTH, MOBLY, HAPPY_DOM])
def test_spec_v2_determinism(name):
    a = extract_spec_v2(_load(name)).to_serializable(version=2)
    b = extract_spec_v2(_load(name)).to_serializable(version=2)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


# ── guard: v1 path is untouched (flag-off byte identity at the extractor) ────
@pytest.mark.parametrize("name", [TRUE_MYTH, MOBLY, HAPPY_DOM])
def test_v1_serialization_unchanged_shape(name):
    rows = extract_spec(_load(name)).to_serializable()
    for r in rows:
        assert set(r.keys()) == {
            "verbatim_text", "kind", "symbols", "keywords", "checkable_forms"
        }, "v1 to_serializable() must emit exactly the five v1 keys"
