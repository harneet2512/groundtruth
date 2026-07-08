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
