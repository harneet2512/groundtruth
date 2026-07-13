"""SS-8 — the shadow-holdout causal kernel (gt-math E10) + the offline analyzer.

RED-first: on a tree without ``groundtruth.runtime.shadow_holdout`` every test here ImportErrors
(the module is the unit under test). Each invariant carries at least one BITING MUTATION comment —
the exact source change that re-reddens it — so the suite proves behavior, not the happy path.

The five kernel invariants under test:
  1. DETERMINISM       — same inputs -> same verdict (x1000).
  2. RATE FLOOR        — rate 0 (and "0"/None/""/malformed) never withholds -> production-safe.
  3. SAFETY ALLOWLIST  — the cardinal classes never held out at ANY rate; only advisory participate.
  4. STRATIFICATION    — the fact class is folded into the hash (per-class ~rate share).
  5. MONOTONICITY      — a higher rate only ADDS holdouts (never flips one back to deliver).

The analyzer tests drive :mod:`scripts.swebench.shadow_report` on synthetic fixture run dirs
(delivered-vs-withheld pairing) AND assert the graceful zero-holdout path.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

from groundtruth.runtime import shadow_holdout as sh

# import fact_registry for the 11-class coverage cross-check (the participate/exclude table must
# partition the EXACT fact universe — none forgotten, none invented).
from groundtruth.runtime import fact_registry as fr


# --------------------------------------------------------------------------- #
# 1. DETERMINISM
# --------------------------------------------------------------------------- #
def test_determinism_same_inputs_same_verdict_1000x():
    args = ("task-A", "caller_contract", "c:deadbeef00", "0.4")
    first = sh.assign(*args)
    assert first in (sh.DELIVER, sh.HOLDOUT)
    for _ in range(1000):
        assert sh.assign(*args) == first
    # MUTATION[seed _bucket with time.time() / random] -> this flaps -> RED.


def test_verdict_is_only_the_two_tokens_never_bytes():
    for cls in list(sh.PARTICIPATING_CLASSES) + ["syntax_result", "unknownzz"]:
        v = sh.assign("t", cls, "k", "1")
        assert v in (sh.DELIVER, sh.HOLDOUT)  # never model-facing bytes (leak-safe by construction)


# --------------------------------------------------------------------------- #
# 2. RATE FLOOR — production-safe posture
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("rate", ["0", 0, 0.0, "", None, "  ", "not-a-number", "0.0"])
def test_rate_zero_or_malformed_never_holds_out(rate):
    for cls in sh.PARTICIPATING_CLASSES:
        for k in ("k1", "k2", "k3", "k4", "k5"):
            assert sh.assign("task", cls, k, rate) == sh.DELIVER
    # MUTATION[drop the `if r <= 0.0: return DELIVER` floor] -> a malformed rate withholds -> RED.


def test_parse_rate_clamps_and_failsafe():
    assert sh.parse_rate("0.2") == 0.2
    assert sh.parse_rate(0.5) == 0.5
    assert sh.parse_rate("1") == 1.0
    assert sh.parse_rate("1.5") == 1.0     # clamp >1 -> 1
    assert sh.parse_rate(2) == 1.0
    assert sh.parse_rate("-0.3") == 0.0    # clamp <0 -> 0
    assert sh.parse_rate("abc") == 0.0     # malformed -> fail safe
    assert sh.parse_rate("") == 0.0
    assert sh.parse_rate(None) == 0.0
    assert sh.parse_rate(float("nan")) == 0.0
    # MUTATION[remove the >1 clamp] -> parse_rate("1.5")==1.5 -> RED.


# --------------------------------------------------------------------------- #
# 3. SAFETY ALLOWLIST — the cardinal E10 rule
# --------------------------------------------------------------------------- #
_SAFETY_IDENTIFIERS = [
    "syntax_result", "submit_refusal",      # canonical safety classes
    "edit.syntax", "edit_check",             # seam kinds -> syntax_result
    "submit_gate", "completion_cert", "submit.refusal",  # seam kinds -> submit_refusal
]


@pytest.mark.parametrize("rate", ["0.5", "1", "1.0", "2.0", 1, 0.999999])
@pytest.mark.parametrize("ident", _SAFETY_IDENTIFIERS)
def test_safety_classes_never_held_out_at_any_rate(ident, rate):
    for k in ("k1", "k2", "k3", "kk", "zz", "c:1", "c:2"):
        assert sh.assign("task", ident, k, rate) == sh.DELIVER
    # MUTATION[flip the allowlist to a denylist: `if canon in SAFETY_EXCLUDED_CLASSES`] -> a
    # safety class withholds at rate 1 -> RED. This is the load-bearing E10 safety mutation.


@pytest.mark.parametrize("rate", ["0.5", "1", "2.0"])
def test_unknown_class_never_held_out(rate):
    for ident in ("totally_unknown", "l9.mystery", "", "   ", "ga.nope", "x:y"):
        assert sh.assign("task", ident, "k", rate) == sh.DELIVER
    # MUTATION[default an unresolved class into PARTICIPATING] -> an unknown withholds -> RED.


def test_rate_one_holds_out_all_participating():
    for cls in sh.PARTICIPATING_CLASSES:
        for k in ("k1", "k2", "k3", "k4"):
            assert sh.assign("task", cls, k, "1") == sh.HOLDOUT
    # MUTATION[remove the `r >= 1.0` short-circuit AND make threshold int(1.0*2**64) exclude the
    # top bucket] -> a max-bucket key delivers at rate 1 -> RED.


# --------------------------------------------------------------------------- #
# 3b. participate/exclude table integrity
# --------------------------------------------------------------------------- #
def test_participate_and_safety_partition_the_fact_registry_universe():
    universe = set(fr.all_fact_classes())
    assert sh.PARTICIPATING_CLASSES.isdisjoint(sh.SAFETY_EXCLUDED_CLASSES)
    assert (sh.PARTICIPATING_CLASSES | sh.SAFETY_EXCLUDED_CLASSES) == universe, (
        "participate ∪ safety must equal the EXACT 11 fact_registry classes"
    )
    # MUTATION[drop covering_red from PARTICIPATING] -> the union != universe -> RED.


def test_safety_set_is_exactly_the_two_cardinal_classes():
    assert sh.SAFETY_EXCLUDED_CLASSES == frozenset({"syntax_result", "submit_refusal"})


def test_canonical_class_resolution():
    # canonical identity
    assert sh.canonical_class("caller_contract") == "caller_contract"
    # finer fact_registry aliases
    assert sh.canonical_class("def_ref_partition") == "def_partition"
    assert sh.canonical_class("signature_mismatch") == "signature_delta"
    assert sh.canonical_class("covering_verdict") == "covering_red"
    # seam tagged kinds
    assert sh.canonical_class("l3.contract") == "caller_contract"
    assert sh.canonical_class("post_search.localize") == "localization"
    assert sh.canonical_class("verify.horizon.advisory") == "recovery"
    assert sh.canonical_class("spec.obligation") == "obligations"
    # global-arbiter telemetry prefix
    assert sh.canonical_class("ga.l3.contract") == "caller_contract"
    # gateway-plane prefix (arm-4 truth: gateway.trace_frame x6 + gateway.def_ref_partition x2
    # were the 8 'unknown' rows in the shadow analyzer — the bare kinds ARE in the table).
    # MUTATION[drop the 'gateway.' strip] -> these resolve to None again -> RED.
    assert sh.canonical_class("gateway.trace_frame") == "localization"
    assert sh.canonical_class("gateway.def_ref_partition") == "def_partition"
    # a gateway-prefixed SAFETY kind stays safety (allowlist semantics preserved under strip)
    assert sh.canonical_class("gateway.edit.syntax") == "syntax_result"
    assert not sh.is_participating("gateway.edit.syntax")
    # dynamic base:suffix
    assert sh.canonical_class("missing_role:handler") == "newfile_precedent"
    # unresolvable
    assert sh.canonical_class("nope") is None
    assert sh.canonical_class("") is None
    assert sh.canonical_class(None) is None


def test_is_participating():
    assert sh.is_participating("l3.contract")
    assert sh.is_participating("recovery")
    assert not sh.is_participating("edit.syntax")
    assert not sh.is_participating("submit_refusal")
    assert not sh.is_participating("unknownzz")


# --------------------------------------------------------------------------- #
# 4. STRATIFICATION — the fact class is folded into the hash
# --------------------------------------------------------------------------- #
def test_class_is_folded_into_the_bucket():
    # two DIFFERENT participating classes at the SAME (task, dedup_key) must draw INDEPENDENTLY.
    key = "c:sharedkey"
    a = sh._bucket("task", "localization", key)
    b = sh._bucket("task", "caller_contract", key)
    assert a != b
    # MUTATION[drop `canon` from the _bucket join] -> the two buckets are EQUAL -> RED (the
    # holdout is no longer stratified — one class's draw mirrors another's).


def test_per_class_holdout_fraction_approximates_rate():
    rate = "0.3"
    keys = [f"c:{i:08x}" for i in range(2000)]
    for cls in sh.PARTICIPATING_CLASSES:
        held = sum(1 for k in keys if sh.assign("taskX", cls, k, rate) == sh.HOLDOUT)
        frac = held / len(keys)
        assert 0.24 < frac < 0.36, f"{cls}: holdout fraction {frac:.3f} not ~0.30"
    # MUTATION[make the threshold `rate/2 * 2**64`] -> fraction ~0.15 -> RED.


# --------------------------------------------------------------------------- #
# 5. MONOTONICITY — a higher rate only adds holdouts
# --------------------------------------------------------------------------- #
def test_holdout_is_monotone_in_rate():
    keys = [f"c:{i:06x}" for i in range(500)]
    for cls in ("localization", "recovery", "obligations"):
        for k in keys:
            if sh.assign("t", cls, k, "0.3") == sh.HOLDOUT:
                # once withheld at 0.3, must stay withheld at every higher rate.
                assert sh.assign("t", cls, k, "0.6") == sh.HOLDOUT
                assert sh.assign("t", cls, k, "1.0") == sh.HOLDOUT
    # MUTATION[compare bucket > threshold instead of <] -> monotonicity inverts -> RED.


# --------------------------------------------------------------------------- #
# the offline analyzer — scripts/swebench/shadow_report.py
# --------------------------------------------------------------------------- #
def _load_shadow_report():
    root = Path(__file__).resolve().parents[2]
    path = root / "scripts" / "swebench" / "shadow_report.py"
    spec = importlib.util.spec_from_file_location("shadow_report_under_test", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_ledger(dirpath: Path, task: str, rows: list[dict]) -> None:
    dirpath.mkdir(parents=True, exist_ok=True)
    with open(dirpath / f"gt_runtime_ledger_{task}.jsonl", "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def test_analyzer_pairs_delivered_and_withheld_per_class(tmp_path):
    sr = _load_shadow_report()
    run = tmp_path / "run" / "art"
    # task 1: a delivered caller_contract + a shadow-held caller_contract (the CONTRAST)
    _write_ledger(run / "repo__x-1", "repo__x-1", [
        {"layer": "l3.contract", "event_type": "post_view", "file_path": "a.py",
         "outcome": "delivered", "chars_delivered": 100, "iteration": 2,
         "content_sha256_16": "aa"},
        {"layer": "l3.contract", "event_type": "post_view", "file_path": "b.py",
         "outcome": "shadow_holdout", "chars_delivered": 0, "iteration": 3,
         "content_sha256_16": "bb", "chars_would": 120, "fact_class": "caller_contract",
         "dedup_key": "c:beef"},
        # a host-side ga.* repair_support telemetry row (chars=0) must NOT count as a delivery.
        {"layer": "ga.l3.contract", "outcome": "delivered", "chars_delivered": 0, "iteration": 3},
    ])
    report = sr.build_report(str(run))
    assert report["tasks_discovered"] == 1
    assert report["total_deliver_events"] == 1     # the ga.* chars=0 row excluded
    assert report["total_holdout_events"] == 1
    assert report["zero_holdout_run"] is False
    cc = report["classes"]["caller_contract"]
    assert cc["deliver"]["n"] == 1 and cc["deliver"]["chars"] == 100
    assert cc["holdout"]["n"] == 1 and cc["holdout"]["chars"] == 120   # chars_would summed
    assert cc["contrast_available"] is True


def test_analyzer_reports_zero_holdout_run_gracefully(tmp_path):
    sr = _load_shadow_report()
    run = tmp_path / "art"
    _write_ledger(run / "repo__y-9", "repo__y-9", [
        {"layer": "l3b.evidence", "file_path": "z.py", "outcome": "delivered",
         "chars_delivered": 50, "iteration": 1},
    ])
    report = sr.build_report(str(run))
    assert report["zero_holdout_run"] is True
    assert report["total_holdout_events"] == 0
    cc = report["classes"]["caller_contract"]
    assert cc["deliver"]["n"] == 1
    assert cc["holdout"]["n"] == 0
    assert cc["contrast_available"] is False
    # render must not crash on a zero-holdout report and must announce it.
    text = sr.render_text(report)
    assert "ZERO-HOLDOUT RUN" in text
