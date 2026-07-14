"""ACQ observability must survive the single-generation brief cache."""

from __future__ import annotations

import json
from types import SimpleNamespace

from groundtruth.runtime import brief_cache


def test_acq_observability_fields_survive_persist_and_load(tmp_path):
    receipts = [
        {
            "block_id": "localization-header",
            "fact_class": "localization",
            "label": "localization-header",
            "char_span": [0, 12],
            "content_hash": "a" * 64,
        }
    ]
    result = SimpleNamespace(
        graph_edge_count=3,
        structural_signal_count=4,
        fts5_signal_count=5,
        block_receipts=receipts,
        tokenizer_used="char4-estimate",
        budget_suppressed=["scope-chain"],
    )

    written = brief_cache.persist_brief(
        str(tmp_path), "sealed brief", result, identity="request-id"
    )
    loaded = brief_cache.load_cached_brief(str(tmp_path), expect_identity="request-id")

    assert loaded is not None
    expected = {
        "graph_edge_count": 3,
        "structural_signal_count": 4,
        "fts5_signal_count": 5,
        "block_receipts": receipts,
        "tokenizer_used": "char4-estimate",
        "budget_suppressed": ["scope-chain"],
    }
    assert {key: written["metrics"][key] for key in expected} == expected
    assert {key: loaded["metrics"][key] for key in expected} == expected
    # Pin that the persisted payload really contains the fields, not merely an
    # in-memory return value added after serialization.
    on_disk = json.loads((tmp_path / brief_cache.BRIEF_CACHE_BASENAME).read_text("utf-8"))
    assert {key: on_disk["metrics"][key] for key in expected} == expected
