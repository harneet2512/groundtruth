"""Fable-bounce fixes on the DELIVERED brief (v1r_brief.py) — Brief-F1,F2,F3,F7,F8,F9.

Cardinal invariant under test: leak=0 on the delivered brief. The reviewer proved the
test-name screen was pytest-convention-ONLY (Go ``TestReconnect`` / camelCase ``testFoo``
/ ``path.py::TestX`` nodeids / ``tests/`` paths all sailed through onto BOTH the B-1
``Expected behavior:`` surface and the ``<gt-obligations>`` block), and that the
assertion screen was asymmetric (only on the Expected-Behavior spec, not on the
obligations block).

TTD: each test asserts the DESIRED post-fix behavior. RED on the pre-fix tree (watched
fail before the fix); GREEN after. Mutation targets are noted per-finding.

Runs with PYTHONIOENCODING=utf-8. Hermetic (no repo, no embedder): the no-match branch
monkeypatches ``run_v74`` to an empty result and points ``GT_ANCHORS_PATH`` at a
nonexistent file so obligation extraction is LIVE from the issue text.
"""

from __future__ import annotations

import hashlib
import sqlite3
import types

import pytest

import groundtruth.pretask.v1r_brief as vb
from groundtruth.pretask.v1r_brief import (
    _brief_block_receipts,
    _count_tokens,
    _enforce_token_rail,
    _expected_behavior_spec,
    _obligation_is_leaky,
    _render_obligations_block,
    _tokenizer_kind,
    generate_v1r_brief,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Brief-F1 — the test-name screen is LANGUAGE-AGNOSTIC (was pytest-convention-only)
# ═══════════════════════════════════════════════════════════════════════════════
# Mutation: revert _OBLIG_TEST_NAME_RE to r"\b(?:test_[A-Za-z0-9_]+|[A-Za-z0-9_]+_test)\b"
# -> every _dropped case below bites.

_LEAK_NAMES = [
    "TestReconnect",                                # Go exported (capital T, no _)
    "run TestReconnect until the socket recovers",  # Go name inside prose
    "testFoo",                                      # camelCase (JS/TS/Java-ish)
    "the fix must make testReconnectFlow pass",     # camelCase inside prose
    "tests/net/conn_flow.py::TestConnFlow",         # pytest nodeid
    "reproduce with pkg/mod_test.go::TestX",        # go-style nodeid
    "see tests/net/conn_flow.py for the repro",     # tests/ path segment
    "the fix must make test_parse_node pass",       # pytest snake (pre-existing)
    "conn_test must round-trip the frame",          # *_test tail (pre-existing)
]

# Legit PRODUCTION identifiers / language scope-resolution that MUST survive
# (correct-or-quiet cuts both ways — over-match here would drop real obligations).
_KEEP_NAMES = [
    "TestingConfig",                 # lowercase 'i' after Test -> not a Go test name
    "the TestingConfig loader must read defaults on startup",
    "contest_handler",               # 'test' has no word boundary before it
    "latest_value must never go stale",
    "std::collections::HashMap",     # Rust scope resolution (no .ext before ::)
    "use std::io::Write in the writer",
    "Foo::bar() must preserve ordering",  # C++ scope resolution
    "the parser must coalesce adjacent semantics before returning",
    "attestation records must be immutable",
]


@pytest.mark.parametrize("txt", _LEAK_NAMES)
def test_f1_test_identifier_dropped_verbatim(txt):
    assert _obligation_is_leaky(txt, set(), set()) is True, f"leaked: {txt!r}"


@pytest.mark.parametrize("txt", _LEAK_NAMES)
def test_f1_test_identifier_dropped_as_symbol(txt):
    # the symbol leg must screen the same conventions as the verbatim leg
    assert _obligation_is_leaky("clean sentence", {txt}, set()) is True, f"leaked: {txt!r}"


@pytest.mark.parametrize("txt", _KEEP_NAMES)
def test_f1_production_identifier_kept(txt):
    assert _obligation_is_leaky(txt, set(), set()) is False, f"over-matched: {txt!r}"


def test_f1_expected_behavior_drops_go_test_name():
    # B-1 surface: a Go test name in the Expected-Behavior snippet is dropped whole.
    issue = "### Expected Behavior\nTestReconnect should pass once the retry lands here\n"
    assert _expected_behavior_spec(issue) is None


def test_f1_expected_behavior_drops_nodeid():
    issue = "### Expected Behavior\nthe case at tests/net/flow.py::TestConnFlow now passes cleanly\n"
    assert _expected_behavior_spec(issue) is None


def test_f1_expected_behavior_keeps_near_miss_production_word():
    # A clean spec that merely contains a near-miss ('latest') still reaches the brief.
    issue = "### Expected Behavior\nthe reader returns the latest value rather than raising\n"
    assert _expected_behavior_spec(issue) == "the reader returns the latest value rather than raising"


# ═══════════════════════════════════════════════════════════════════════════════
# Brief-F2 — the assertion screen is applied in BOTH surfaces (was asymmetric)
# ═══════════════════════════════════════════════════════════════════════════════
# Mutation: drop the `or _ASSERT_LEAK_RE.search(...)` clause in _obligation_is_leaky
# -> test_f2_assert_obligation_is_leaky + the e2e bite.

def test_f2_assert_verb_obligation_is_kept():
    # Fable-LIPI round-2 brief Finding-2 (2026-07-11): the RFC-2119/EARS verb "must assert that …"
    # is a legit, grader-INDEPENDENT obligation, NOT a test identity — round-1's optional-tail
    # regex ate it whole. The narrowed screen releases the bare verb while real assertion
    # CALL/macro forms (assertEqual / assert_eq! / assert()) still drop (siblings below).
    assert _obligation_is_leaky(
        "The client must assert that the retry counter equals 5", set(), set()
    ) is False


def test_f2_unittest_assert_obligation_is_leaky():
    assert _obligation_is_leaky("self.assertEqual(retry, 5) after the fix", set(), set()) is True


def test_f2_assert_symbol_is_leaky():
    # ONE canonical screen — the SYMBOL leg screens the assertion keyword too (an
    # obligation whose named symbol is an assertion helper is still grader-coupled).
    # Nothing else in the screen catches a bare 'assertEqual' symbol, so this pins the
    # symbol-leg assert clause specifically.
    assert _obligation_is_leaky("the helper runs on submit", {"assertEqual"}, set()) is True


def test_f2_english_asserts_is_not_a_leak():
    # the ENGLISH verb 'asserts'/'assertion' must NOT trip the keyword screen
    assert _obligation_is_leaky(
        "the function asserts nothing and returns the value directly", set(), set()
    ) is False


# ── e2e F1+F2 through _render_obligations_block (the <gt-obligations> renderer) ──

def _cap(s: str) -> str:
    return s


def test_f1f2_obligations_block_drops_leaks_keeps_clean(tmp_path, monkeypatch):
    monkeypatch.setenv("GT_ANCHORS_PATH", str(tmp_path / "nope.json"))
    issue = (
        "The client must reconnect on a dropped socket.\n"        # clean -> renders
        "The fix must make TestReconnect pass.\n"                 # Go test name -> dropped
        "The retry loop must assertEqual(counter, 5) here.\n"     # unittest CALL form -> dropped
        "The handler must assert that the counter equals 5.\n"    # RFC-2119 verb -> KEPT (round-2)
    )
    out = "\n".join(_render_obligations_block(issue, [], _cap, require_anchor=False))
    assert "reconnect on a dropped socket" in out, f"clean obligation lost: {out!r}"
    assert "TestReconnect" not in out, f"Go test name leaked: {out!r}"
    # real assertion CALL/macro form (assertEqual) still drops — leak invariant intact.
    assert "assertEqual" not in out, f"unittest assertion leaked: {out!r}"
    # Fable-LIPI round-2 brief Finding-2: the bare RFC-2119 "must assert that" obligation is a
    # legit grader-independent requirement and now RENDERS (round-1 over-dropped it).
    assert "counter equals 5" in out, f"assert-verb obligation over-dropped: {out!r}"


# ═══════════════════════════════════════════════════════════════════════════════
# no-match branch harness (shared by F3, F7, F9)
# ═══════════════════════════════════════════════════════════════════════════════
def _empty_graph(tmp_path) -> str:
    db = str(tmp_path / "graph.db")
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY, label TEXT, name TEXT, "
        "qualified_name TEXT, file_path TEXT, start_line INTEGER, end_line INTEGER, "
        "signature TEXT, return_type TEXT, is_exported BOOLEAN, is_test BOOLEAN, "
        "language TEXT, parent_id INTEGER);"
        "CREATE TABLE edges (id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER, "
        "type TEXT, source_line INTEGER, source_file TEXT, resolution_method TEXT, "
        "confidence REAL, metadata TEXT);"
    )
    conn.commit()
    conn.close()
    return db


def _no_match_env(tmp_path, monkeypatch) -> str:
    monkeypatch.delenv("GT_TOKENIZER_JSON", raising=False)
    monkeypatch.delenv("GT_MODELS_ROOT", raising=False)
    monkeypatch.setenv("GT_ANCHORS_PATH", str(tmp_path / "nope.json"))
    stub = types.SimpleNamespace(ranked_full=[], effective_w_sem=0.31, k_sem_top_effective=7)
    monkeypatch.setattr(vb, "run_v74", lambda *a, **k: stub)
    return _empty_graph(tmp_path)


_ISSUE = (
    "The parser must coalesce adjacent semantics.\n"
    "parse_node should merge nodes and raise ParseError on bad input.\n"
)


# ═══════════════════════════════════════════════════════════════════════════════
# Brief-F3 — the no-match brief obeys the HARD token rail (was bypassed)
# ═══════════════════════════════════════════════════════════════════════════════
# Mutation: delete the `_enforce_token_rail(_nm_brief, ...)` call on the no-match path
# -> test_f3_no_match_brief_respects_small_cap bites (113 tokens for cap 10).

@pytest.mark.parametrize("cap", [10, 20, 40])
def test_f3_no_match_brief_respects_small_cap(tmp_path, monkeypatch, cap):
    db = _no_match_env(tmp_path, monkeypatch)
    res = generate_v1r_brief(_ISSUE, str(tmp_path), db, max_brief_tokens=cap)
    assert res.files == []
    assert res.token_estimate <= cap, (
        f"no-match brief reported {res.token_estimate} tokens for cap {cap}"
    )
    assert _count_tokens(res.brief_text) <= cap, (
        f"RAIL BREACH: {_count_tokens(res.brief_text)} > {cap}: {res.brief_text!r}"
    )


def test_f3_no_match_records_budget_suppressed(tmp_path, monkeypatch):
    db = _no_match_env(tmp_path, monkeypatch)
    res = generate_v1r_brief(_ISSUE, str(tmp_path), db, max_brief_tokens=10)
    assert res.budget_suppressed, "over-cap no-match trim must record what it suppressed"


def test_f3_no_match_default_cap_untouched(tmp_path, monkeypatch):
    # Idempotent: at the default cap the no-match brief is NOT trimmed (byte-identical),
    # nothing suppressed — the fix only bites when over budget.
    db = _no_match_env(tmp_path, monkeypatch)
    res = generate_v1r_brief(_ISSUE, str(tmp_path), db)  # default 600
    assert res.budget_suppressed == []
    assert "<gt-obligations>" in res.brief_text
    assert "coalesce adjacent semantics" in res.brief_text


# ═══════════════════════════════════════════════════════════════════════════════
# Brief-F7 — B-6 receipts cover the fact-bearing no-match brief (was [])
# ═══════════════════════════════════════════════════════════════════════════════
# Mutation: hard-code `_nm_receipts = []` on the no-match path -> the ON assertion bites.

def test_f7_no_match_receipts_off_by_default(tmp_path, monkeypatch):
    db = _no_match_env(tmp_path, monkeypatch)
    monkeypatch.delenv("GT_BLOCK_RECEIPTS", raising=False)
    res = generate_v1r_brief(_ISSUE, str(tmp_path), db)
    assert res.block_receipts == []


def test_f7_no_match_receipts_populated_when_on(tmp_path, monkeypatch):
    db = _no_match_env(tmp_path, monkeypatch)
    monkeypatch.setenv("GT_BLOCK_RECEIPTS", "1")
    res = generate_v1r_brief(_ISSUE, str(tmp_path), db)
    assert "<gt-obligations>" in res.brief_text
    assert res.block_receipts, "fact-bearing no-match brief shipped with no receipts"
    classes = {r["fact_class"] for r in res.block_receipts}
    assert "obligations" in classes, classes
    # spans + hashes are faithful to the DELIVERED brief bytes
    for r in res.block_receipts:
        s, e = r["char_span"]
        assert r["content_hash"] == hashlib.sha256(
            res.brief_text[s:e].encode("utf-8")
        ).hexdigest(), r


def test_f7_no_match_brief_bytes_identical_on_vs_off(tmp_path, monkeypatch):
    # METADATA ONLY: turning receipts ON must not change the delivered no-match brief.
    db = _no_match_env(tmp_path, monkeypatch)
    monkeypatch.delenv("GT_BLOCK_RECEIPTS", raising=False)
    off = generate_v1r_brief(_ISSUE, str(tmp_path), db)
    monkeypatch.setenv("GT_BLOCK_RECEIPTS", "1")
    on = generate_v1r_brief(_ISSUE, str(tmp_path), db)
    assert on.brief_text == off.brief_text, "F7 receipts changed the no-match brief TEXT"
    assert off.block_receipts == [] and on.block_receipts


# ═══════════════════════════════════════════════════════════════════════════════
# Brief-F8 — budget <= 0 DISABLES the rail (documented contract)
# ═══════════════════════════════════════════════════════════════════════════════
# Mutation: change `if budget <= 0 or ...` to `if budget < 0 or ...` -> budget==0 falls
# through to trimming and truncates the text -> test_f8_zero_budget_is_disabled bites.

_RAILTEXT = "line one has words\nline two has more words\nline three closes it out"


def test_f8_zero_budget_is_disabled():
    assert _enforce_token_rail(_RAILTEXT, 0) == (_RAILTEXT, [])


def test_f8_negative_budget_is_disabled():
    assert _enforce_token_rail(_RAILTEXT, -5) == (_RAILTEXT, [])


def test_f8_positive_budget_still_enforces():
    # sanity: the disabled path is ONLY <= 0; a positive over-budget cap still trims.
    out, suppressed = _enforce_token_rail(_RAILTEXT, 3)
    assert _count_tokens(out) <= 3
    assert suppressed


# ═══════════════════════════════════════════════════════════════════════════════
# Brief-F9 — a `tokenizer_used` marker records which counter ran (was silent)
# ═══════════════════════════════════════════════════════════════════════════════
# Mutation: make _tokenizer_kind() always return "gte-modernbert-bpe" -> the fallback
# assertion bites.

def test_f9_kind_is_fallback_when_no_tokenizer(monkeypatch):
    monkeypatch.delenv("GT_TOKENIZER_JSON", raising=False)
    monkeypatch.delenv("GT_MODELS_ROOT", raising=False)
    assert _tokenizer_kind() == "char4-estimate"


def test_f9_kind_is_bpe_when_tokenizer_present(monkeypatch):
    # a loadable tokenizer -> the marker reflects the REAL BPE path
    monkeypatch.setattr(vb, "_get_tokenizer", lambda: object())
    assert _tokenizer_kind() == "gte-modernbert-bpe"


def test_f9_result_carries_marker(tmp_path, monkeypatch):
    # the marker reaches the result (here the char/4 fallback path)
    db = _no_match_env(tmp_path, monkeypatch)
    res = generate_v1r_brief(_ISSUE, str(tmp_path), db)
    assert res.tokenizer_used == "char4-estimate"
