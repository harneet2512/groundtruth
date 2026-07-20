"""Part 2 of the caller_contract search co-fact (2026-07-20) — the DELIVERY seam.

When the pre-edit ``post_search.localize`` def-facts block physically carries a
``callers:`` line, ``_post_search_cofact_extra`` mints a NAMESPACED ``co_fact`` sub-dict
crediting the caller_contract FACT class on the SAME physical delivery, typed at the
``caller_contract_search`` boundary override (search_result). The seal is over the model
``content``, never ``extra`` — so the co-fact is an additive audit sidecar that changes
ZERO delivered bytes and is INERT until the grader (Part 3) reads ``co_fact``.

Correct-or-quiet: no callers line (a symbol with no verified callers, or a caller line
dropped by a pre-gate mutation) => NO co-fact. Wrong lane => NO co-fact.
"""
from __future__ import annotations

from artifact_deepswe import gt_mini_patch as gm

# a def-facts block exactly as _fmt_def_facts_native renders it, WITH a callers line
BLOCK_WITH_CALLERS = (
    "src/app/config.py:88:load_config\n"
    "callers: src/app/api.py:12 (get_settings); src/app/cli.py:40 (main)\n"
    "test refs: 3"
)
# the same producer, a symbol with no verified callers => no callers line
BLOCK_NO_CALLERS = (
    "src/app/config.py:88:load_config\n"
    "test refs: 3"
)


def test_cofact_minted_when_callers_line_present():
    extra = gm._post_search_cofact_extra("post_search.localize", BLOCK_WITH_CALLERS)
    assert "co_fact" in extra, "callers line present -> co_fact must be credited"
    cf = extra["co_fact"]
    assert cf["fact_class"] == "caller_contract"
    assert cf["evidence_type"] == "caller_contract_search"
    assert cf["required_event"] == "search_result"
    assert cf["actual_event"] == "search_result"
    assert cf["producer_registration_match"] is True
    assert cf["lineage_schema"] == "gt.feature_lineage.v1"


def test_no_cofact_without_callers_line():
    # correct-or-quiet: no caller bytes in the delivered block -> no caller_contract credit
    assert gm._post_search_cofact_extra("post_search.localize", BLOCK_NO_CALLERS) == {}


def test_no_cofact_on_other_lanes():
    # only the pre-edit def-partition search delivery carries these bytes
    assert gm._post_search_cofact_extra("l3b.evidence", BLOCK_WITH_CALLERS) == {}
    assert gm._post_search_cofact_extra("l3.contract", BLOCK_WITH_CALLERS) == {}
    assert gm._post_search_cofact_extra("", BLOCK_WITH_CALLERS) == {}


def test_cofact_is_namespaced_additive_only():
    # the ONLY key the co-fact adds is ``co_fact`` — it can never clobber the host
    # def_partition row's own top-level lineage (fact_class/lineage_schema/evidence_type).
    extra = gm._post_search_cofact_extra("post_search.localize", BLOCK_WITH_CALLERS)
    assert set(extra.keys()) == {"co_fact"}


def test_empty_text_quiet():
    assert gm._post_search_cofact_extra("post_search.localize", "") == {}
