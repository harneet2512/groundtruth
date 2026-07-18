"""Blind extension selector (Cluster-5 ITEM 4, 2026-07-18).

The selector reproduces the vFINAL frozen 7-id blind selection bit-for-bit from the real dataset +
locked-30 bytes, refuses (fail-closed) on any input-hash drift, and reads ONLY blind fields (never
gold/test). RED-first: on a tree without benchmarks/data/blind_ext_selection_vfinal_20260718.json or
the selector module, none of this exists.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for _p in (ROOT / "scripts" / "swebench",):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import blind_ext_selector as sel  # noqa: E402

DATASET = ROOT / "benchmarks" / "data" / "swebench_live_lite.jsonl"
LOCKED30 = ROOT / ".claude" / "reports" / "mixture_bag_30_20260712.txt"
FROZEN = ROOT / "benchmarks" / "data" / "blind_ext_selection_vfinal_20260718.json"


def _bytes():
    return DATASET.read_bytes(), LOCKED30.read_bytes()


def test_reproduces_frozen_seven_bit_for_bit() -> None:
    frozen = json.loads(FROZEN.read_text(encoding="utf-8"))
    ds, lk = _bytes()
    result = sel.select(ds, lk)
    assert result["selected_signature"] == frozen["selected_signature"]
    assert result["selected_newfile"] == frozen["selected_newfile"]
    assert len(result["selected"]) == 7
    # The independent scratchpad-frozen ids, pinned here as the oracle.
    assert result["selected_signature"] == [
        "deepset-ai__haystack-8609", "huggingface__smolagents-285", "wireservice__csvkit-1274",
    ]
    assert result["selected_newfile"] == [
        "reflex-dev__reflex-4720", "matplotlib__matplotlib-29721",
        "tox-dev__tox-3409", "python-telegram-bot__python-telegram-bot-4673",
    ]


def test_refuses_on_dataset_hash_mismatch() -> None:
    ds, lk = _bytes()
    import pytest
    with pytest.raises(sel.SelectorRefusal, match="dataset sha256 mismatch"):
        sel.select(ds + b"\n{}", lk)  # one extra byte -> different sha256


def test_refuses_on_locked30_hash_mismatch() -> None:
    ds, lk = _bytes()
    import pytest
    with pytest.raises(sel.SelectorRefusal, match="locked-30 sha256 mismatch"):
        sel.select(ds, lk + b"# drift\n")


def test_refuses_unknown_version() -> None:
    ds, lk = _bytes()
    import pytest
    with pytest.raises(sel.SelectorRefusal, match="unknown selector version"):
        sel.select(ds, lk, version="some-other-version")


def test_blind_projection_never_carries_gold_or_test_fields() -> None:
    ds, _lk = _bytes()
    projected = sel.project_blind(ds)
    assert len(projected) == 300
    for rec in projected:
        assert set(rec) == set(sel.BLIND_FIELDS)  # EXACTLY the four blind keys, nothing else
        assert not (sel.FORBIDDEN_FIELDS & set(rec))
    # Every selected id is reachable from the blind projection alone (no gold field needed).
    ids = {r["instance_id"] for r in projected}
    for i in sel.select(ds, _lk)["selected"]:
        assert i in ids


def test_campaign_manifest_expands_locked30_plus_seven() -> None:
    ds, lk = _bytes()
    manifest = sel.build_campaign_manifest(ds, lk)
    assert manifest["schema"] == "gt.blind_ext_campaign.v1"
    assert manifest["counts"]["locked30"] == 30
    assert manifest["counts"]["selected"] == 7
    # locked-30 + 7 selected, minus any selected already in locked (here: disjoint -> 37).
    assert manifest["counts"]["expanded"] == 37
    assert len(manifest["campaign"]) == 37
    buckets = {row["bucket"] for row in manifest["campaign"]}
    assert buckets == {"locked30", "blind_ext_selected"}
    # Every expanded row carries only blind provenance (repo/base_commit), never a gold field.
    for row in manifest["campaign"]:
        assert set(row) == {"instance_id", "repo", "base_commit", "bucket"}
