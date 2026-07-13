"""SS-3 defect-2 (K15) — DELIVERY metrics from the sealed RUNTIME LEDGER.

Reproduces run-29236533134: gt_deep_metrics reported gt_delivery all-zeros and
gt_injected_tokens_total=0 while the runtime ledger held real sealed deliveries
(outcome="delivered" + content_sha256_16 + chars_delivered). The delivery leg must
count from the ledger rows — NEVER from <gt-*> tag scans (tagless native era) — and
byte-join each seal against the model-visible trajectory.

Fixtures are hermetic (synthetic ledger + trajectory). Each test carries a biting
mutation note. A baseline-diff test proves the no-ledger reader path is unchanged.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DM_PATH = os.path.join(_ROOT, "scripts", "swebench", "gt_deep_metrics.py")
_spec = importlib.util.spec_from_file_location("gt_deep_metrics_ss3", _DM_PATH)
dm = importlib.util.module_from_spec(_spec)
sys.modules["gt_deep_metrics_ss3"] = dm
_spec.loader.exec_module(dm)


def _sha16(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", errors="replace")).hexdigest()[:16]


def _write_ledger(path: str, rows: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


# --- delivered-count/chars derive from the ledger, not tags ----------------- #
def test_delivered_count_and_chars_from_ledger(tmp_path):
    block_a = "[EVIDENCE] config_installer.py callers=3"
    block_b = "[CONTRACT] returns Optional[Config]"
    led = str(tmp_path / "gt_runtime_ledger_x.jsonl")
    _write_ledger(led, [
        {"layer": "l3b.evidence", "event_type": "post_view", "outcome": "delivered",
         "chars_delivered": len(block_a), "content_sha256_16": _sha16(block_a)},
        {"layer": "l3.contract", "event_type": "post_edit", "outcome": "delivered",
         "chars_delivered": len(block_b), "content_sha256_16": _sha16(block_b)},
        # non-delivered outcomes and seal-less rows MUST NOT count.
        {"layer": "l3b.evidence", "event_type": "l3b.evidence", "outcome": "eligible",
         "chars_delivered": 0},
        {"layer": "consensus", "event_type": "", "outcome": "suppressed_hidden_only",
         "chars_delivered": 99},
        {"layer": "L6", "event_type": "", "outcome": "delivered", "chars_delivered": 0},
    ])
    res = dm._delivery_from_runtime_ledger(led)
    assert res["present"] is True
    assert res["delivered_count"] == 2           # only the two sealed delivered rows
    assert res["delivered_chars"] == len(block_a) + len(block_b)
    assert res["per_layer"] == {"l3b.evidence": 1, "l3.contract": 1}
    # MUTATION 1: drop the `content_sha256_16` filter → the seal-less L6 "delivered"
    #             row counts → delivered_count==3 (over-count).
    # MUTATION 2: count any outcome (not just "delivered") → the eligible/suppressed
    #             rows count → delivered_count==5.


# --- byte-join: seal must appear verbatim in the trajectory ----------------- #
def test_byte_join_verifies_seal_in_trajectory():
    block = "[EVIDENCE] importer.py::set_fields callers=2"
    rows = [{"content_sha256_16": _sha16(block), "chars_delivered": len(block)}]
    present = ["some log preamble\n" + block + "\ntrailing output"]
    absent = ["completely unrelated observation with no gt bytes"]
    assert dm._byte_join_verified(rows, present) == 1
    assert dm._byte_join_verified(rows, absent) == 0
    # MUTATION: hash the whole text instead of the length-`chars_delivered` window →
    #           never matches an embedded block → verified drops to 0 on `present`.


def test_byte_join_is_capped_and_never_raises():
    # a large text + a length that never matches: must return promptly (budget) not hang.
    rows = [{"content_sha256_16": "deadbeefdeadbeef", "chars_delivered": 50}]
    big = ["x" * 200_000]
    assert dm._byte_join_verified(rows, big, op_budget=10_000) == 0


# --- tagless: ledger deliveries counted even with ZERO <gt-*> tags ---------- #
def test_counts_deliveries_with_no_gt_tags_present(tmp_path):
    block = "[SCOPE] primary target: importer.py"  # NOTE: no <gt-*> tag anywhere
    led = str(tmp_path / "gt_runtime_ledger_y.jsonl")
    _write_ledger(led, [
        {"layer": "consensus.scope", "event_type": "post_view", "outcome": "delivered",
         "chars_delivered": len(block), "content_sha256_16": _sha16(block)},
    ])
    res = dm._delivery_from_runtime_ledger(led, trajectory_texts=["obs\n" + block])
    assert res["delivered_count"] == 1
    assert res["byte_join_verified_count"] == 1


# --- baseline-diff: no-ledger path is unchanged ----------------------------- #
def test_no_ledger_returns_empty_shape(tmp_path):
    # missing ledger → present False, all-zero, no byte-join; contributes nothing so the
    # legacy gt_delivery tag counts and token totals are untouched (byte-identical path).
    res = dm._delivery_from_runtime_ledger(str(tmp_path / "does_not_exist.jsonl"))
    assert res == {
        "present": False,
        "runtime_ledger_path": str(tmp_path / "does_not_exist.jsonl"),
        "delivered_count": 0,
        "delivered_chars": 0,
        "sealed_count": 0,
        "byte_join_verified_count": None,
        "per_layer": {},
        "per_event_type": {},
    }


def test_build_gt_delivery_runtime_ledger_block_present(tmp_path, monkeypatch):
    # A minimal build() where only a ledger exists (no trajectory): the runtime_ledger
    # sub-block appears with the sealed delivery and the tokens are filled from it.
    # chdir into the isolated tmp dir so build()'s cwd-relative globs (`.`) can't pick
    # up a stray trajectory from the repo tree (that would inflate the token total).
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GT_RUNTIME_LEDGER", raising=False)
    block = "[EVIDENCE] x.py callers=1"
    led = str(tmp_path / "gt_runtime_ledger_z.jsonl")
    _write_ledger(led, [
        {"layer": "l3b.evidence", "event_type": "post_view", "outcome": "delivered",
         "chars_delivered": len(block), "content_sha256_16": _sha16(block)},
    ])
    deep = dm.build("z", str(tmp_path))
    rl = deep["gt_delivery"]["runtime_ledger"]
    assert rl["present"] is True
    assert rl["delivered_count"] == 1
    assert rl["delivered_chars"] == len(block)
    # tagless run → the token total is filled from the sealed ledger chars, not left 0.
    assert deep["gt_injected_tokens_total"] == float(len(block))
    assert deep["gt_injected_tokens_source"] == "runtime_ledger_sealed_chars"
