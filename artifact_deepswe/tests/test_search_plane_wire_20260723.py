"""Search-plane wire fixes (129 WORKING buckets tranche 1).

1. Path-acquisition novelty must not starve post_search.localize / is_loc.
2. Under GT_LOC_RESLOT, ranked localization spends on first isolated search
   (bare OR broad), not only when _search_pattern returns None.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import gt_mini_patch as g  # noqa: E402


def _base(monkeypatch):
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_record_hook_fire", lambda *a, **k: None)
    for k in ("GT_POST_SEARCH", "GT_LOC_RESLOT", "GT_SS_NOVELTY", "GT_INSEAM_METRICS"):
        monkeypatch.delenv(k, raising=False)
    g._reset_oracle_state()
    g._loc_reslot_delivered = False


def test_novelty_path_acq_does_not_starve_def_partition():
    g._ss_acquired_files.clear()
    g._ss_acquired_files.add("pkg/mod.py")
    assert g._ss_novelty_suppresses(
        "post_search.localize",
        "pkg/mod.py:10:def parse()\ncallers: 2",
        "",
    ) is False


def test_loc_reslot_fires_on_first_bare_symbol_search(monkeypatch, tmp_path):
    """Bare-symbol isolated grep under LOC_RESLOT must get ranked loc once."""
    _base(monkeypatch)
    monkeypatch.setenv("GT_POST_SEARCH", "1")
    monkeypatch.setenv("GT_LOC_RESLOT", "1")
    g._POST_SEARCH_ON = True
    monkeypatch.setattr(g, "_loc_reslot_on", lambda: True)
    monkeypatch.setattr(g, "_search_command_isolated", lambda cmd: True)
    monkeypatch.setattr(g, "_grep_result_empty", lambda cmd, out: False)
    monkeypatch.setattr(g, "_search_pattern", lambda cmd: "parse")
    monkeypatch.setattr(g, "_search_probe_tokens", lambda cmd: ["parse"])
    monkeypatch.setattr(g, "_action_count", 3)
    monkeypatch.setattr(
        g, "_loc_reslot_payload",
        lambda: ("path/a.py:1:parse\npath/b.py:2:parse\n", None),
    )
    # Must NOT fall through to graph def_partition.
    monkeypatch.setattr(g, "_db_path", lambda: str(tmp_path / "missing.db"))
    dec = g._search_localize_decision("grep -rn parse .", out="path/a.py:1:parse\n")
    assert dec.text.startswith("path/a.py:1:parse")
    assert dec.evidence_type == "localization" or dec.producer == "ranked_localization" or (
        getattr(dec, "fact_class", None) == "localization"
    )


def test_loc_reslot_second_search_falls_through(monkeypatch, tmp_path):
    _base(monkeypatch)
    monkeypatch.setenv("GT_POST_SEARCH", "1")
    g._POST_SEARCH_ON = True
    monkeypatch.setattr(g, "_loc_reslot_on", lambda: True)
    g._loc_reslot_delivered = True  # latch already spent
    monkeypatch.setattr(g, "_search_command_isolated", lambda cmd: True)
    monkeypatch.setattr(g, "_grep_result_empty", lambda cmd, out: False)
    monkeypatch.setattr(g, "_search_pattern", lambda cmd: None)  # broad
    monkeypatch.setattr(g, "_search_probe_tokens", lambda cmd: ["foo", "bar"])
    monkeypatch.setattr(g, "_action_count", 4)
    called = {"n": 0}

    def _payload():
        called["n"] += 1
        return ("SHOULD_NOT", None)

    monkeypatch.setattr(g, "_loc_reslot_payload", _payload)
    dec = g._search_localize_decision("grep -rn 'foo\\|bar' .", out="")
    assert called["n"] == 0  # short-circuit: latch spent, don't call payload
    assert not (dec.text or "").strip()
