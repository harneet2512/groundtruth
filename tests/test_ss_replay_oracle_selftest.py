"""SS-R replay-oracle SELFTEST — proves the reconstruction + oracle BITE, on synthetic
fixtures where the answer is known, with NO real seam (StubSeamDriver only).

The three mandated proofs (each paired with a biting mutation):
  (a) stripping recovers the EXACT native bytes        -> off-by-one window MUTATION leaves residual
  (b) the oracle flags a KILLED cardinal P5            -> present P5 PASSES, missing P5 FAILS
  (c) the oracle catches an UNSUPPRESSED dup           -> suppressed PASSES, recorded-identical FAILS

Plus: nested short-seal home resolution (the conan-17092 m49 shape), coherence count-accuracy,
the leak/dose/empty invariants, the manifest-free leak scanner's word-boundary guard, and a
StubSeamDriver end-to-end run.
"""
from __future__ import annotations

import json
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "scripts" / "swebench"))

import ss_replay_oracle as sro  # noqa: E402


def test_stage_current_v2_obligations_is_issue_bound_and_structural(tmp_path):
    recorded = tmp_path / "recorded"
    target = tmp_path / "target"
    recorded.mkdir()
    issue = """Parser currently crashes on empty input.

**Describe the bug**
Calling parse currently raises an internal exception.

**To Reproduce**
Returns:
The command throws ValueError.

**Expected behavior**
The parser should return an empty list.
"""
    (recorded / "issue.txt").write_text(issue, encoding="utf-8")

    path = sro.stage_current_v2_obligations(recorded, target)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["obligations_version"] == 2
    assert payload["issue_sha256"] == hashlib.sha256(issue.encode("utf-8")).hexdigest()
    text = [row["verbatim_text"] for row in payload["clauses"]]
    assert text == ["The parser should return an empty list"]
    assert not any("Returns:" in row or "currently" in row for row in text)


def test_stage_current_v2_obligations_fails_closed_without_issue(tmp_path):
    recorded = tmp_path / "recorded"
    recorded.mkdir()
    with pytest.raises(sro.SeamReplayBlocked, match="issue.txt"):
        sro.stage_current_v2_obligations(recorded, tmp_path / "target")


def test_stage_current_v2_obligations_drops_test_identity(tmp_path):
    recorded = tmp_path / "recorded"
    recorded.mkdir()
    (recorded / "issue.txt").write_text(
        "Expected behavior:\nThe runner should execute test_secret_gold before submit.",
        encoding="utf-8",
    )
    path = sro.stage_current_v2_obligations(recorded, tmp_path / "target")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["clauses"] == []


# ── synthetic fixture builders ────────────────────────────────────────────────
def _msgs(*tool_contents: str) -> list[dict]:
    """Build a system+user prelude then (assistant action, tool obs) pairs. tool_contents[k]
    is the observation for iteration k+1, so it lands at message index 2*(k+1)+1."""
    m = [{"role": "system", "content": "sys"}, {"role": "user", "content": "task"}]
    for i, tc in enumerate(tool_contents):
        m.append({"role": "assistant", "content": f"```bash\ncmd_{i+1}\n```"})
        m.append({"role": "tool", "content": tc})
    return m


def _seal_row(iteration: int, layer: str, delta: str, event_type: str = "post_view",
              outcome: str = "delivered") -> dict:
    return {"layer": layer, "event_type": event_type, "outcome": outcome,
            "chars_delivered": len(delta),
            "content_sha256_16": sro._sha16(delta.encode("utf-8")),
            "iteration": iteration, "reason": "", "file_path": "", "timestamp_ms": 0}


def _write_task(tmp: Path, task: str, messages: list[dict], rows: list[dict]) -> Path:
    d = tmp / task
    d.mkdir(parents=True, exist_ok=True)
    (d / "mini-swe-agent.trajectory.json").write_text(
        json.dumps({"messages": messages}), encoding="utf-8")
    with open(d / f"gt_runtime_ledger_{task}.jsonl", "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return tmp


# clean GT deltas (NO test identifiers — must survive the leak scan)
_L3B = "\n[gt] callers of run(): pkg/a.py:10, pkg/b.py:22 (2 callers)\n"
_SHORT = "[gt] contract: run() -> None\n"


# ══════════════════════════════════════════════════════════════════════════════
# (a) RECONSTRUCTION recovers exact native bytes  +  off-by-one MUTATION bites
# ══════════════════════════════════════════════════════════════════════════════
def test_locate_seal_exact_hit_and_offbyone_mutation_is_none(tmp_path):
    native = "<returncode>0</returncode>\n<output>\npkg/a.py\n</output>"
    content = native + _L3B
    n = len(_L3B)
    sha = sro._sha16(_L3B.encode("utf-8"))
    # exact length locates the window at the correct char offset
    off = sro.locate_seal(content, n, sha)
    assert off is not None and content[off:off + n] == _L3B
    # MUTATION: off-by-one window length can NEVER match the seal (exactness is load-bearing)
    assert sro.locate_seal(content, n + 1, sha) is None
    assert sro.locate_seal(content, n - 1, sha) is None


def test_reconstruct_recovers_exact_native_bytes(tmp_path):
    native1 = "<output>\npkg/a.py:10: def run(): ...\n</output>"
    native2 = "<output>\nno GT here\n</output>"
    native3 = "<output>\npkg/c.py edited\n</output>"
    root = _write_task(
        tmp_path, "syn__1",
        _msgs(native1 + _L3B, native2, native3 + _SHORT),
        [_seal_row(1, "l3b.evidence", _L3B),
         _seal_row(3, "l3.contract", _SHORT, event_type="post_edit")])
    rt = sro.reconstruct_task("syn__1", root)
    # native observations recovered EXACTLY (GT bytes removed)
    obs = [o for _a, o in rt.pairs]
    assert obs[0] == native1              # delta stripped
    assert obs[1] == native2              # untouched (no delivery)
    assert obs[2] == native3              # delta stripped
    # zero residual: nothing that still matches a seal
    assert rt.residual_leaks == []
    # payloads captured verbatim + homed to the right message index
    by_layer = {d.layer: d for d in rt.recorded_deliveries}
    assert by_layer["l3b.evidence"].payload == _L3B
    assert by_layer["l3b.evidence"].home_msg == 3          # 2*1+1
    assert by_layer["l3.contract"].payload == _SHORT
    assert by_layer["l3.contract"].home_msg == 7           # 2*3+1


def test_reconstruct_prefers_strict_extra_returncode_and_cross_checks_wrapper(tmp_path):
    messages = _msgs("<returncode>7</returncode>\n<output>failed</output>")
    messages[3]["extra"] = {"raw_output": "failed", "returncode": 7}
    root = _write_task(tmp_path, "syn__rc", messages, [])
    assert sro.reconstruct_task("syn__rc", root).rcs == [7]

    messages[3]["extra"]["returncode"] = 8
    root = _write_task(tmp_path, "syn__rc_mismatch", messages, [])
    with pytest.raises(ValueError, match="returncode mismatch"):
        sro.reconstruct_task("syn__rc_mismatch", root)


@pytest.mark.parametrize("invalid", [True, False, "7", 7.0, None])
def test_reconstruct_rejects_non_integer_extra_returncode(tmp_path, invalid):
    task = f"syn__bad_rc_{str(invalid).replace('.', '_')}"
    messages = _msgs("<returncode>7</returncode>\n<output>failed</output>")
    messages[3]["extra"] = {"raw_output": "failed", "returncode": invalid}
    root = _write_task(tmp_path, task, messages, [])
    with pytest.raises(ValueError, match="extra.returncode must be an int"):
        sro.reconstruct_task(task, root)


def test_offbyone_strip_leaves_residual_mutation(tmp_path):
    """MUTATION: strip using the WRONG (n+1) window. The seal can't be located, so the bytes
    survive and the residual-leak invariant BITES (proving exact-length stripping is required)."""
    native = "<output>\npkg/a.py\n</output>"
    content = native + _L3B
    n = len(_L3B)
    sha = sro._sha16(_L3B.encode("utf-8"))
    # correct strip removes the seal -> no residual
    good_off = sro.locate_seal(content, n, sha)
    good_stripped = content[:good_off] + content[good_off + n:]
    assert sro.locate_seal(good_stripped, n, sha) is None
    # mutated strip (n+1) cannot even locate the seal -> the delta remains in the buffer
    assert sro.locate_seal(content, n + 1, sha) is None
    assert sro.locate_seal(content, n, sha) is not None   # bytes still present == residual


def test_nested_short_seal_lands_on_own_home(tmp_path):
    """The conan-17092 m49 shape: a SHORT seal that is also a substring of a LONGER seal's bytes
    at an earlier message. Progressive stripping in ledger order (long first) must home the short
    seal to its OWN message, not the earlier collision."""
    long_delta = "\n[gt] evidence block; " + _SHORT + "extra tail bytes here for length\n"
    short_delta = _SHORT
    assert short_delta in long_delta                       # the collision is real
    native_a = "<output>A</output>"
    native_b = "<output>B</output>"
    root = _write_task(
        tmp_path, "syn__nest",
        _msgs(native_a + long_delta, native_b + short_delta),
        [_seal_row(1, "l3b.evidence", long_delta),         # home m3, contains short
         _seal_row(2, "l3.contract", short_delta)])        # home m5, its own
    # pre-strip: short bytes appear at BOTH m3 and m5
    msgs = json.loads((root / "syn__nest" / "mini-swe-agent.trajectory.json").read_text("utf-8"))["messages"]
    assert short_delta in msgs[3]["content"] and short_delta in msgs[5]["content"]
    rt = sro.reconstruct_task("syn__nest", root)
    homes = {d.layer: d.home_msg for d in rt.recorded_deliveries}
    assert homes["l3b.evidence"] == 3 and homes["l3.contract"] == 5
    assert rt.residual_leaks == []


# ══════════════════════════════════════════════════════════════════════════════
# oracle scaffolding
# ══════════════════════════════════════════════════════════════════════════════
def _rec(layer, home_msg, chars, payload="", reason="", outcome="delivered"):
    return sro.Delivery(layer=layer, event_type="post_view", iteration=(home_msg - 1) // 2,
                        chars=chars, sha16="x" * 16, home_msg=home_msg, outcome=outcome,
                        reason=reason, file_path="", payload=payload)


def _rep(layer, m, chars, payload="", reason="", outcome="delivered",
         sha16="x" * 16, iteration=None):
    return {"layer": layer, "m": m, "chars_delivered": chars, "outcome": outcome,
            "reason": reason, "payload": payload, "content_sha256_16": sha16,
            **({"iteration": iteration} if iteration is not None else {})}


_TASK = "conan-io__conan-17123"
_DUP_TASK = "conan-io__conan-17092"


# ══════════════════════════════════════════════════════════════════════════════
# (b) KILLED cardinal P5 -> FAIL ; present P5 -> PASS
# ══════════════════════════════════════════════════════════════════════════════
def test_oracle_preserve_present_passes_killed_p5_fails():
    cases = {"preserve": [
        {"task": _TASK, "delivery": "consensus.scope m25",
         "why": "P5 consumed (G5+U) — scope constraint", "assert": "still delivered"}]}
    recorded = {_TASK: [_rec("consensus.scope", 25, 367, payload="scope: edit only pkg/x.py")]}

    # present -> PASS
    rep_present = {_TASK: [_rep("consensus.scope", 25, 367, payload="scope: edit only pkg/x.py")]}
    v_present = sro.evaluate_cases(cases, recorded, rep_present, None)
    assert len(v_present) == 1 and v_present[0].verdict == sro.PASS and v_present[0].cardinal

    # MUTATION: a seam that KILLS the P5 (replayed ledger has no such delivery) -> FAIL + cardinal
    rep_killed = {_TASK: [_rep("l3b.evidence", 11, 100)]}   # some other delivery, not the P5
    v_killed = sro.evaluate_cases(cases, recorded, rep_killed, None)
    assert v_killed[0].verdict == sro.FAIL and v_killed[0].cardinal
    assert "KILLED" in v_killed[0].reason or "absent" in v_killed[0].reason


def test_oracle_preserve_requires_exact_mapped_boundary_and_exact_sha():
    cases = {"preserve": [{
        "task": "T", "delivery": "l3b m11", "why": "P5 consumed",
    }]}
    recorded = {"T": [sro.Delivery(
        "l3b.evidence", "post_view", 5, 311, "a" * 16, 11,
        "delivered", "", "", payload="p",
    )]}
    fx = sro.FixpointResult(
        "T", True, True, True, float("inf"), 8, 8, drift_map=[(5, 7)],
    )

    exact = {"T": [{
        "layer": "l3b.evidence", "iteration": 7, "chars_delivered": 311,
        "outcome": "delivered", "content_sha256_16": "a" * 16,
    }]}
    wrong_boundary = {"T": [dict(exact["T"][0], iteration=8)]}
    wrong_sha = {"T": [dict(exact["T"][0], content_sha256_16="b" * 16)]}

    assert sro.evaluate_cases(cases, recorded, exact, None, {"T": fx})[0].verdict == sro.PASS
    # Biting mutation 1: correct bytes one iteration late are not preserved in place.
    assert sro.evaluate_cases(cases, recorded, wrong_boundary, None, {"T": fx})[0].verdict == sro.FAIL
    # Biting mutation 2: different bytes at the exact boundary are not preserved bytes.
    assert sro.evaluate_cases(cases, recorded, wrong_sha, None, {"T": fx})[0].verdict == sro.FAIL


def test_oracle_preserve_target_does_not_borrow_adjacent_same_layer_seal():
    cases = {"preserve": [{
        "task": "T", "delivery": "l3b m9", "why": "NOVEL_PRESERVE",
    }]}
    recorded = {"T": [
        sro.Delivery(
            "l3b.evidence", "post_view", 5, 100, "a" * 16, 9,
            "delivered", "", "", payload="first",
        ),
        sro.Delivery(
            "l3b.evidence", "post_view", 6, 100, "b" * 16, 10,
            "delivered", "", "", payload="neighbor",
        ),
    ]}
    fx = sro.FixpointResult(
        "T", True, True, True, float("inf"), 8, 8,
        drift_map=[(5, 5), (6, 6)],
    )
    neighbor_only = {"T": [{
        "layer": "l3b.evidence", "iteration": 6, "chars_delivered": 100,
        "outcome": "delivered", "content_sha256_16": "b" * 16,
    }]}
    verdict = sro.evaluate_cases(
        cases, recorded, neighbor_only, None, fidelity={"T": fx})[0]
    assert verdict.verdict == sro.FAIL


def test_cardinal_flag_excludes_invalid_historical_witness():
    cases = json.loads((_REPO / "tests" / "fixtures" / "ss_replay" / "cases.json").read_text("utf-8"))
    cardinals = [c for c in cases["preserve"] if sro._is_cardinal_preserve(c)]
    assert len(cardinals) == 2
    assert {c["task"] for c in cardinals} == {
        "geopandas__geopandas-3471", "dynaconf__dynaconf-1225"}


def test_oracle_conan_historical_invalid_and_corrected_boundaries():
    """Conan's raw iter-12 scope receipt is retained as history, never promoted to a P5.

    Corrected-boundary cases are judged against their explicit replay placement. This is a
    case-local assertion, not a relaxation of ``FixpointResult.faithful``.
    """
    cases = {
        "historical_invalid": [{
            "task": _TASK,
            "delivery": "consensus.scope m25",
            "recorded_iteration": 12,
            "recorded_chars": 367,
            "recorded_sha": "d5beca5ac5aa94ef",
            "fails_gates": ["correct_info", "correct_rl_adhered_time", "fair_probe"],
            "acknowledgment": "HISTORICAL_ONLY_NOT_TRANSFERABLE",
        }],
        "corrected_boundaries": [{
            "task": _TASK,
            "label": "consensus.scope after first real source write",
            "layer": "consensus.scope",
            "decision_pair": 19,
            "expected_iteration": 20,
            "expected_outcome": "delivered",
            "expected_chars": 367,
            "expected_sha": "7ab99be457a48275",
            "acknowledgment": "UNPROVEN_LIVE_REQUIRED",
        }, {
            "task": _TASK,
            "label": "same-turn stale obligation after behavioral proof",
            "layer": "obligation.unexercised",
            "passing_test_pair": 15,
            "expected_iteration": 16,
            "expected_outcome": "suppressed",
            "expected_reason": "ss_late",
            "expected_chars": 0,
            "expected_clause_id": "8c8a5079",
            "expected_subject_digest": "f530f49a477795ba",
            "expected_artifact_issue_sha256": "f7f9940d48fc5825",
            "expected_proof_turn": 16,
            "forbidden_delivery_text": (
                "However, they would be more useful if they supported inverse matching"
            ),
            "acknowledgment": "NOT_APPLICABLE_SUPPRESSED",
        }],
    }
    recorded = {_TASK: [sro.Delivery(
        "consensus.scope", "review_transition", 12, 367, "d5beca5ac5aa94ef",
        25, "delivered", "", "", payload="You edited 1 of 9")]
    }
    replayed = {_TASK: [{
        "layer": "consensus.scope", "iteration": 20, "chars_delivered": 367,
        "outcome": "delivered", "content_sha256_16": "7ab99be457a48275", "reason": "",
    }, {
        "layer": "obligation.unexercised", "iteration": 16, "chars_delivered": 0,
        "outcome": "suppressed_hidden_only", "reason": "ss_late",
        "clause_id": "8c8a5079", "subject_digest": "f530f49a477795ba",
        "artifact_issue_sha256": "f7f9940d48fc5825", "proof_turn": 16,
    }, {
        "layer": "obligation.unexercised", "iteration": 16,
        "chars_delivered": 27, "outcome": "delivered",
        "content_sha256_16": "b" * 16, "payload": "Another unverified clause.",
    }]}
    fx = sro.FixpointResult(_TASK, True, False, False, 18.0, 30, 24)
    verdicts = sro.evaluate_cases(cases, recorded, replayed, None, fidelity={_TASK: fx})
    assert [v.verdict for v in verdicts] == [sro.PASS, sro.PASS, sro.PASS]
    assert fx.faithful is False  # corrected cases never launder the global fixpoint
    assert all(not v.cardinal for v in verdicts)

    # MUTATION 1: moving the corrected scope back to the false scratch boundary must fail.
    replayed[_TASK][0]["iteration"] = 12
    moved = sro.evaluate_cases(cases, recorded, replayed, None, fidelity={_TASK: fx})
    assert moved[1].verdict == sro.FAIL
    replayed[_TASK][0]["iteration"] = 20

    # MUTATION 2: dropping the late attribution must fail exact suppression proof.
    replayed[_TASK][1]["reason"] = ""
    unattributed = sro.evaluate_cases(cases, recorded, replayed, None, fidelity={_TASK: fx})
    assert unattributed[2].verdict == sro.FAIL
    replayed[_TASK][1]["reason"] = "ss_late"

    # MUTATION 3: a same-turn suppression for another clause cannot satisfy the case.
    replayed[_TASK][1]["clause_id"] = "another-clause"
    wrong_clause = sro.evaluate_cases(cases, recorded, replayed, None, fidelity={_TASK: fx})
    assert wrong_clause[2].verdict == sro.FAIL
    replayed[_TASK][1]["clause_id"] = "8c8a5079"

    # MUTATION 4: delivering the exact target clause alongside its suppression must fail.
    replayed[_TASK][2]["payload"] = cases["corrected_boundaries"][1]["forbidden_delivery_text"]
    target_delivered = sro.evaluate_cases(
        cases, recorded, replayed, None, fidelity={_TASK: fx})
    assert target_delivered[2].verdict == sro.FAIL
    replayed[_TASK][2]["payload"] = "Another unverified clause."

    # MUTATION 5: free-form pseudo-gates cannot make a diagnostic row pass.
    cases["historical_invalid"][0]["fails_gates"] = ["whatever_we_claim"]
    invented = sro.evaluate_cases(cases, recorded, replayed, None, fidelity={_TASK: fx})
    assert invented[0].verdict == sro.FAIL


# ══════════════════════════════════════════════════════════════════════════════
# (c) UNSUPPRESSED dup -> FAIL ; suppressed/absent -> PASS
# ══════════════════════════════════════════════════════════════════════════════
def test_oracle_semantic_dup_unsuppressed_fails_suppressed_passes():
    cases = {"suppress_semantic_dup": [
        {"task": _DUP_TASK, "deliveries": ["l3.contract m49"],
         "why": "cross-class re-delivery", "assert": "reason=ss_semantic_dup"}]}
    recorded = {_DUP_TASK: [_rec("l3.contract", 49, 72, payload="dup evidence")]}

    # MUTATION: a no-op seam re-delivers the dup byte-for-byte, NO ss_ reason -> FAIL
    rep_dup = {_DUP_TASK: [_rep("l3.contract", 49, 72, payload="dup evidence")]}
    v_dup = sro.evaluate_cases(cases, recorded, rep_dup, None)
    assert v_dup[0].verdict == sro.FAIL and "ss_semantic_dup" in v_dup[0].reason

    # SS-correct: the dup is suppressed with the expected reason -> PASS
    rep_supp = {_DUP_TASK: [_rep("l3.contract", 49, 0, outcome="suppressed",
                                 reason="ss_semantic_dup")]}
    v_supp = sro.evaluate_cases(cases, recorded, rep_supp, None)
    assert v_supp[0].verdict == sro.PASS

    # Disappearance is DARK, not attributed suppression.
    v_absent = sro.evaluate_cases(cases, recorded, {_DUP_TASK: []}, None)
    assert v_absent[0].verdict == sro.FAIL


def test_oracle_suppression_requires_attribution_at_exact_boundary_and_target_absent():
    task = "T"
    cases = {"suppress_late": [{
        "task": task, "delivery": "spec.obligation m11", "why": "stale",
    }]}
    recorded = {task: [sro.Delivery(
        "spec.obligation", "post_edit", 5, 100, "a" * 16, 11,
        "delivered", "", "", payload="old",
    )]}
    fx = sro.FixpointResult(
        task, True, True, True, float("inf"), 8, 8, drift_map=[(5, 7)],
    )
    suppressed = {
        "layer": "spec.obligation", "iteration": 7, "chars_delivered": 0,
        "outcome": "suppressed_hidden_only", "reason": "ss_late",
    }

    exact = {task: [suppressed]}
    v2_owner_exact = {task: [dict(suppressed, layer="obligation.unexercised")]}
    wrong_owner_exact = {task: [dict(suppressed, layer="obligation.resurface")]}
    wrong_boundary = {task: [dict(suppressed, iteration=8)]}
    delivered_too = {task: [suppressed, {
        "layer": "spec.obligation", "iteration": 7, "chars_delivered": 100,
        "outcome": "delivered", "content_sha256_16": "a" * 16,
    }]}

    assert sro.evaluate_cases(cases, recorded, exact, None, {task: fx})[0].verdict == sro.PASS
    assert sro.evaluate_cases(
        cases, recorded, v2_owner_exact, None, {task: fx}
    )[0].verdict == sro.PASS
    assert sro.evaluate_cases(
        cases, recorded, wrong_owner_exact, None, {task: fx}
    )[0].verdict == sro.FAIL
    assert sro.evaluate_cases(cases, recorded, wrong_boundary, None, {task: fx})[0].verdict == sro.FAIL
    assert sro.evaluate_cases(cases, recorded, delivered_too, None, {task: fx})[0].verdict == sro.FAIL
    # Iteration-only child rows also fail closed without a fidelity map; a missing
    # home-message coordinate must not become a wildcard.
    unmapped_wrong_boundary = {task: [dict(suppressed, iteration=6)]}
    assert sro.evaluate_cases(
        cases, recorded, unmapped_wrong_boundary, None)[0].verdict == sro.FAIL


def test_oracle_provenance_accepts_exact_sealed_filtered_delivery():
    """A mixed historical partition may remain delivered after every forbidden row is removed."""
    task = "T"
    cases = {"suppress_provenance": [{
        "task": task,
        "delivery": "gateway.def_ref_partition m7",
        "paths": ["htmlcov/coverage_html_cb_*.js"],
    }]}
    recorded = {task: [_rec(
        "gateway.def_ref_partition", 7, 127,
        payload=("htmlcov/coverage_html_cb_a.js:1:event\n"
                 "pkg/event.py:4:event"),
    )]}
    clean = "pkg/event.py:4:event"
    replayed = {task: [_rep(
        "gateway.def_ref_partition", 7, len(clean), payload=clean,
        sha16=sro._sha16(clean.encode("utf-8")),
    )]}

    verdict = sro.evaluate_cases(cases, recorded, replayed, None)[0]

    assert verdict.verdict == sro.PASS
    assert "filtered delivery" in verdict.reason


def test_oracle_provenance_filtered_delivery_requires_exact_evidence_type():
    """A clean neighboring gateway fact cannot stand in for the manifest's target fact."""
    task = "T"
    cases = {"suppress_provenance": [{
        "task": task,
        "delivery": "gateway.def_ref_partition m7",
        "paths": ["htmlcov/coverage_html_cb_*.js"],
    }]}
    recorded = {task: [_rec(
        "gateway.def_ref_partition", 7, 127,
        payload=("htmlcov/coverage_html_cb_a.js:1:event\n"
                 "pkg/event.py:4:event"),
    )]}
    clean = "pkg/event.py:4:event"

    unrelated = sro.evaluate_cases(
        cases, recorded,
        {task: [_rep(
            "gateway.localization", 7, len(clean), payload=clean,
            sha16=sro._sha16(clean.encode("utf-8")),
        )]}, None,
    )[0]
    exact = sro.evaluate_cases(
        cases, recorded,
        {task: [_rep(
            "gateway.def_ref_partition", 7, len(clean), payload=clean,
            sha16=sro._sha16(clean.encode("utf-8")),
        )]}, None,
    )[0]

    assert unrelated.verdict == sro.FAIL
    assert exact.verdict == sro.PASS


def test_oracle_provenance_target_suppression_ignores_unrelated_gateway_delivery():
    """Only delivery of the suppressed evidence type can invalidate its suppression."""
    task = "T"
    cases = {"suppress_provenance": [{
        "task": task,
        "delivery": "gateway.def_ref_partition m7",
        "paths": ["htmlcov/coverage_html_cb_*.js"],
    }]}
    recorded = {task: [_rec(
        "gateway.def_ref_partition", 7, 127,
        payload=("htmlcov/coverage_html_cb_a.js:1:event\n"
                 "pkg/event.py:4:event"),
    )]}
    suppression = _rep(
        "gateway.def_ref_partition", 7, 0,
        reason="ss_provenance", outcome="suppressed_hidden_only",
    )
    clean = "pkg/other.py:4:other"
    unrelated_delivery = _rep(
        "gateway.localization", 7, len(clean), payload=clean,
        sha16=sro._sha16(clean.encode("utf-8")),
    )
    target_delivery = _rep(
        "gateway.def_ref_partition", 7, len(clean), payload=clean,
        sha16=sro._sha16(clean.encode("utf-8")),
    )

    unrelated = sro.evaluate_cases(
        cases, recorded, {task: [suppression, unrelated_delivery]}, None,
    )[0]
    target = sro.evaluate_cases(
        cases, recorded, {task: [suppression, target_delivery]}, None,
    )[0]

    assert unrelated.verdict == sro.PASS
    assert target.verdict == sro.FAIL


def test_oracle_provenance_rejects_unsealed_or_forbidden_delivery():
    """Delivery credit requires exact bytes, and any surviving forbidden path remains a failure."""
    task = "T"
    cases = {"suppress_provenance": [{
        "task": task,
        "delivery": "gateway.def_ref_partition m7",
        "paths": ["htmlcov/coverage_html_cb_*.js"],
    }]}
    recorded = {task: [_rec(
        "gateway.def_ref_partition", 7, 127,
        payload="htmlcov/coverage_html_cb_a.js:1:event\npkg/event.py:4:event",
    )]}
    dirty = "htmlcov/coverage_html_cb_a.js:1:event\npkg/event.py:4:event"
    unsealed_clean = "pkg/event.py:4:event"

    dirty_verdict = sro.evaluate_cases(
        cases, recorded,
        {task: [_rep(
            "gateway.def_ref_partition", 7, len(dirty), payload=dirty,
            sha16=sro._sha16(dirty.encode("utf-8")),
        )]}, None,
    )[0]
    unsealed_verdict = sro.evaluate_cases(
        cases, recorded,
        {task: [_rep(
            "gateway.def_ref_partition", 7, len(unsealed_clean),
            payload=unsealed_clean, sha16="0" * 16,
        )]}, None,
    )[0]

    assert dirty_verdict.verdict == sro.FAIL
    assert unsealed_verdict.verdict == sro.FAIL


@pytest.mark.parametrize(
    ("payload", "forbidden"),
    [
        ("tmp/patch.py:4:run", True),
        ("/tmp/change.py:4:run", True),
        ("build/generated.js:4:run", True),
        ("pkg/__pycache__/mod.py:4:run", True),
        ("coverage.xml:4:run", True),
        (".git/config:4:run", True),
        ("../outside.py:4:run", True),
        ("C:/outside/mod.py:4:run", True),
        ("/testbed/pkg/mod.py:4:run", False),
        ("pkg/mod.py:4:run", False),
        ("coverage.value is a dotted symbol", False),
    ],
)
def test_oracle_provenance_path_classification_is_general(payload, forbidden):
    assert sro._payload_has_forbidden_provenance(payload) is forbidden


@pytest.mark.parametrize(
    ("target_home", "target_iteration", "neighbor_iteration"),
    [(9, 5, 6), (10, 6, 5)],
)
def test_oracle_suppression_recorded_target_does_not_absorb_adjacent_same_layer_delivery(
        target_home, target_iteration, neighbor_iteration):
    """One neighboring l3b outcome cannot change the audited message's verdict."""
    task = "T"
    cases = {"suppress_step_behind": [{
        "task": task, "delivery": f"l3b m{target_home}", "why": "echo",
    }]}
    recorded = {task: [
        sro.Delivery(
            "l3b.evidence", "post_view", 5, 100, "a" * 16, 9,
            "delivered", "", "", payload="first",
        ),
        sro.Delivery(
            "l3b.evidence", "post_view", 6, 100, "b" * 16, 10,
            "delivered", "", "", payload="second",
        ),
    ]}
    fx = sro.FixpointResult(
        task, True, True, True, float("inf"), 8, 8,
        drift_map=[(5, 5), (6, 6)],
    )
    replayed = {task: [{
        "layer": "l3b.evidence", "iteration": target_iteration,
        "chars_delivered": 0, "outcome": "suppressed_hidden_only",
        "reason": "ss_step_behind",
    }, {
        "layer": "l3b.evidence", "iteration": neighbor_iteration,
        "chars_delivered": 100, "outcome": "delivered",
        "content_sha256_16": "c" * 16,
    }]}

    verdict = sro.evaluate_cases(
        cases, recorded, replayed, None, fidelity={task: fx})[0]
    assert verdict.verdict == sro.PASS


def test_oracle_late_suppression_allows_only_nonstale_remainder():
    task = "T"
    artifact_sha = "a" * 64
    cases = {"suppress_late": [{
        "task": task,
        "delivery": "spec.obligation m11",
        "stale_subjects": ["TypeError"],
        "artifact_issue_sha256": artifact_sha,
        "why": "one clause has fresh behavioral proof",
    }]}
    recorded = {task: [_rec("spec.obligation", 11, 100, payload="TypeError stale")]}
    suppression = _rep(
        "obligation.unexercised", 11, 0,
        outcome="suppressed", reason="ss_late",
    )
    suppression["subject_term_digests"] = [
        hashlib.sha256(b"typeerror").hexdigest()[:16]
    ]
    suppression["artifact_issue_sha256"] = artifact_sha
    wrong_suppression = dict(
        suppression,
        subject_term_digests=[hashlib.sha256(b"unrelated").hexdigest()[:16]],
    )
    truthful_remainder = _rep(
        "obligation.unexercised", 11, 40, payload="bambi alternative remains",
    )
    stale_remainder = _rep(
        "obligation.unexercised", 11, 40, payload="TypeError remains",
    )
    payload_missing = _rep("obligation.unexercised", 11, 40)

    assert sro.evaluate_cases(
        cases, recorded, {task: [suppression, truthful_remainder]}, None
    )[0].verdict == sro.PASS
    assert sro.evaluate_cases(
        cases, recorded, {task: [suppression, stale_remainder]}, None
    )[0].verdict == sro.FAIL
    assert sro.evaluate_cases(
        cases, recorded, {task: [suppression, payload_missing]}, None
    )[0].verdict == sro.FAIL
    assert sro.evaluate_cases(
        cases, recorded, {task: [wrong_suppression, truthful_remainder]}, None
    )[0].verdict == sro.FAIL


def test_oracle_late_suppression_requires_every_complete_subject_identity():
    task = "T"
    artifact_sha = "b" * 64
    cases = {"suppress_late": [{
        "task": task,
        "delivery": "spec.obligation m11",
        "stale_subjects": ["TypeError", "categorical values"],
        "artifact_issue_sha256": artifact_sha,
        "why": "both clauses have fresh behavioral proof",
    }]}
    recorded = {task: [_rec(
        "spec.obligation", 11, 100,
        payload="TypeError and categorical values are stale",
    )]}

    def suppression(subject: str) -> dict:
        row = _rep(
            "obligation.unexercised", 11, 0,
            outcome="suppressed", reason="ss_late",
        )
        from groundtruth.runtime.obligations import obligation_subject_terms
        row["subject_term_digests"] = [
            hashlib.sha256(term.encode("utf-8")).hexdigest()[:16]
            for term in obligation_subject_terms(subject)
        ]
        row["artifact_issue_sha256"] = artifact_sha
        return row

    typeerror = suppression("TypeError")
    categorical = suppression("categorical values")
    assert sro.evaluate_cases(
        cases, recorded, {task: [typeerror]}, None
    )[0].verdict == sro.FAIL
    assert sro.evaluate_cases(
        cases, recorded, {task: [typeerror, categorical]}, None
    )[0].verdict == sro.PASS


def test_oracle_late_suppression_prefers_exact_clause_and_artifact_identity():
    task = "T"
    artifact_sha = "c" * 64
    subject_digest = hashlib.sha256(b"typeerror").hexdigest()[:16]
    cases = {"suppress_late": [{
        "task": task,
        "delivery": "spec.obligation m11",
        "stale_subjects": ["TypeError"],
        "stale_clauses": [{
            "clause_id": "clause-1",
            "subject_digest": subject_digest,
            "artifact_issue_sha256": artifact_sha,
        }],
        "why": "the exact clause has fresh behavioral proof",
    }]}
    recorded = {task: [_rec("spec.obligation", 11, 100, payload="TypeError stale")]}
    exact = _rep(
        "obligation.unexercised", 11, 0,
        outcome="suppressed", reason="ss_late",
    )
    exact.update({
        "clause_id": "clause-1",
        "subject_digest": subject_digest,
        "subject_term_digests": [subject_digest],
        "artifact_issue_sha256": artifact_sha,
    })
    wrong_clause = dict(exact, clause_id="clause-2")
    wrong_artifact = dict(exact, artifact_issue_sha256="d" * 64)

    assert sro.evaluate_cases(
        cases, recorded, {task: [wrong_clause]}, None
    )[0].verdict == sro.FAIL
    assert sro.evaluate_cases(
        cases, recorded, {task: [wrong_artifact]}, None
    )[0].verdict == sro.FAIL
    assert sro.evaluate_cases(
        cases, recorded, {task: [exact]}, None
    )[0].verdict == sro.PASS


def test_oracle_step_behind_and_late_suppress_directions():
    cases = {
        "suppress_step_behind": [{"task": _DUP_TASK, "deliveries": ["l3b m9"], "why": "echo"}],
        "suppress_late": [{"task": _TASK, "delivery": "spec.obligation m37", "why": "verified green"}],
    }
    recorded = {
        _DUP_TASK: [_rec("l3b.evidence", 9, 238)],
        _TASK: [_rec("spec.obligation", 37, 488)],
    }
    # unsuppressed -> FAIL for both
    rep_bad = {_DUP_TASK: [_rep("l3b.evidence", 9, 238)],
               _TASK: [_rep("spec.obligation", 37, 488)]}
    vb = sro.evaluate_cases(cases, recorded, rep_bad, None)
    assert all(v.verdict == sro.FAIL for v in vb)
    # suppressed with the right reasons -> PASS for both
    rep_ok = {_DUP_TASK: [_rep("l3b.evidence", 9, 0, outcome="suppressed", reason="ss_step_behind")],
              _TASK: [_rep("spec.obligation", 37, 0, outcome="suppressed", reason="ss_late")]}
    vo = sro.evaluate_cases(cases, recorded, rep_ok, None)
    assert all(v.verdict == sro.PASS for v in vo)


# ══════════════════════════════════════════════════════════════════════════════
# COUNT-ACCURACY (coherence): silent | exact-count PASS ; inflated FAIL
# ══════════════════════════════════════════════════════════════════════════════
def test_oracle_coherence_count_accuracy():
    cases = {"suppress_coherence_miscount": [
        {"task": _DUP_TASK, "delivery": "detect.coherence m55", "actual_writes": 2, "claimed": 4}]}
    recorded = {_DUP_TASK: [_rec("detect.coherence", 55, 220, payload="4 rewrites of trainer.py")]}

    # silent (no coherence delivery) -> PASS
    v_silent = sro.evaluate_cases(cases, recorded, {_DUP_TASK: []}, None)
    assert v_silent[0].verdict == sro.PASS and "silent" in v_silent[0].reason

    # MUTATION: still fires with the inflated claimed count 4 -> FAIL
    v_bad = sro.evaluate_cases(cases, recorded,
                               {_DUP_TASK: [_rep("detect.coherence", 55, 90, payload="4 writes")]}, None)
    assert v_bad[0].verdict == sro.FAIL

    # fires with the EXACT verified count 2 (and not 4) -> PASS
    v_ok = sro.evaluate_cases(cases, recorded,
                              {_DUP_TASK: [_rep("detect.coherence", 55, 90, payload="2 writes to file")]}, None)
    assert v_ok[0].verdict == sro.PASS


# ══════════════════════════════════════════════════════════════════════════════
# INVARIANTS: leak / dose / empty
# ══════════════════════════════════════════════════════════════════════════════
def test_oracle_coherence_count_is_scoped_to_exact_decision_boundary():
    """A later valid churn firing is not the historical miscount being audited."""
    task = "aiogram__aiogram-1594"
    cases = {"suppress_coherence_miscount": [{
        "task": task,
        "delivery": "detect.coherence m23",
        "actual_writes": 2,
        "claimed": 4,
    }]}
    recorded = {task: [sro.Delivery(
        layer="detect.coherence", event_type="post_edit", iteration=11,
        chars=314, sha16="a" * 16, home_msg=23, outcome="delivered",
        reason="", file_path="aiogram/fsm/context.py", payload="4 writes",
    )]}
    fidelity = {task: sro.FixpointResult(
        task, True, False, True, float("inf"), 47, 45,
        drift_map=[(11, 11)],
    )}

    # At recorded iteration 11 only two writes existed. The corrected producer
    # is silent there, then legitimately fires after a third write at iteration
    # 12. That later event is a different decision boundary and a different case.
    later_valid = {task: [{
        "layer": "detect.coherence", "iteration": 12,
        "chars_delivered": 314, "outcome": "delivered",
        "reason": "", "payload": "rewritten 3 times",
    }]}
    verdict = sro.evaluate_cases(
        cases, recorded, later_valid, None, fidelity=fidelity)[0]
    assert verdict.verdict == sro.PASS
    assert "silent" in verdict.reason

    # Biting mutation: that wrong count at the exact audited boundary must fail.
    later_valid[task][0]["iteration"] = 11
    boundary_bad = sro.evaluate_cases(
        cases, recorded, later_valid, None, fidelity=fidelity)[0]
    assert boundary_bad.verdict == sro.FAIL

    # Stub/no-fidelity callers match by home message rather than mapped
    # iteration. The exact tolerance must propagate through that fallback too.
    later_by_home = {task: [_rep(
        "detect.coherence", 24, 314, payload="rewritten 3 times")
    ]}
    fallback = sro.evaluate_cases(
        cases, recorded, later_by_home, None, fidelity=None)[0]
    assert fallback.verdict == sro.PASS
    assert "silent" in fallback.reason

    # Real child-ledger rows carry iteration but no home_msg/m. Even without a
    # fidelity map, a later iteration-only row must not be admitted as the exact
    # historical boundary merely because its home field is absent.
    later_by_iteration = {task: [{
        "layer": "detect.coherence", "iteration": 12,
        "chars_delivered": 314, "outcome": "delivered",
        "reason": "", "payload": "rewritten 3 times",
    }]}
    iteration_fallback = sro.evaluate_cases(
        cases, recorded, later_by_iteration, None, fidelity=None)[0]
    assert iteration_fallback.verdict == sro.PASS
    assert "silent" in iteration_fallback.reason

    # Biting mutation: the same iteration-only row at the exact recorded
    # iteration remains attributable and must fail its wrong count.
    later_by_iteration[task][0]["iteration"] = 11
    iteration_boundary_bad = sro.evaluate_cases(
        cases, recorded, later_by_iteration, None, fidelity=None)[0]
    assert iteration_boundary_bad.verdict == sro.FAIL


def test_exact_recorded_boundary_rejects_missing_home_coordinate():
    """Missing location evidence cannot wildcard-match an exact manifest boundary."""
    rows = [{"layer": "l3b.evidence", "home_msg": None, "iteration": 99}]

    assert sro._rows_matching(rows, "l3b", 9, tol=0) == []


def test_exact_recorded_boundary_rejects_adjacent_home_coordinate():
    rows = [{"layer": "l3b.evidence", "home_msg": 10, "iteration": 4}]

    assert sro._rows_matching(rows, "l3b", 9, tol=0) == []


def test_invariants_leak_dose_empty_bite():
    recorded = {"t": []}
    recorded_rows = {"t": []}

    # clean replayed stream -> all PASS
    clean = {"t": [_rep("l3b.evidence", 5, 40, payload="pkg/a.py:10 run()")]}
    res = {r.name.split(" ")[0]: r for r in sro.evaluate_invariants(recorded, clean, recorded_rows, None)}
    assert all(r.verdict == sro.PASS for r in res.values() if not r.name.startswith("off-flag"))

    # leak: a delivered payload cites a test node-id -> leak invariant FAILS
    leaky = {"t": [_rep("l3b.evidence", 5, 40, payload="see tests/test_pkg.py::test_run")]}
    inv = sro.evaluate_invariants(recorded, leaky, recorded_rows, None)
    assert next(i for i in inv if i.name.startswith("leak")).verdict == sro.FAIL

    # dose: two delivered payloads at the SAME observation -> dose invariant FAILS
    doubled = {"t": [_rep("l3b.evidence", 5, 40, payload="ok"),
                     _rep("l3.contract", 5, 30, payload="also ok")]}
    inv = sro.evaluate_invariants(recorded, doubled, recorded_rows, None)
    assert next(i for i in inv if i.name.startswith("<=1 dose")).verdict == sro.FAIL

    # empty: a delivered row with 0 bytes -> empty invariant FAILS
    empty = {"t": [{"layer": "l3b.evidence", "m": 5, "delivered": True, "chars": 0, "payload": ""}]}
    inv = sro.evaluate_invariants(recorded, empty, recorded_rows, None)
    assert next(i for i in inv if i.name.startswith("no empty")).verdict == sro.FAIL

    # A real ledger row uses outcome="delivered", not the synthetic ``delivered`` boolean.
    # It must remain visible to the invariant even when chars_delivered is zero.
    raw_empty = {"t": [{"layer": "ga.trace_frame", "iteration": 30,
                         "outcome": "delivered", "chars_delivered": 0,
                         "payload": ""}]}
    inv = sro.evaluate_invariants(recorded, raw_empty, recorded_rows, None)
    assert next(i for i in inv if i.name.startswith("no empty")).verdict == sro.FAIL


def test_offflag_fixpoint_bites_on_divergence():
    recorded_rows = {"t": [{"layer": "l3b.evidence", "outcome": "delivered",
                            "chars_delivered": 40, "iteration": 2, "timestamp_ms": 111}]}
    recorded = {"t": []}
    # identical (modulo timestamp) -> PASS
    same = {"t": [{"layer": "l3b.evidence", "outcome": "delivered",
                   "chars_delivered": 40, "iteration": 2, "timestamp_ms": 999}]}
    inv = sro.evaluate_invariants(recorded, same, recorded_rows, None)
    assert next(i for i in inv if i.name.startswith("off-flag")).verdict == sro.PASS
    # a divergent row (all SS off should be byte-identical to recorded) -> FAIL
    diff = {"t": [{"layer": "l3b.evidence", "outcome": "delivered",
                   "chars_delivered": 41, "iteration": 2, "timestamp_ms": 999}]}
    inv = sro.evaluate_invariants(recorded, diff, recorded_rows, None)
    assert next(i for i in inv if i.name.startswith("off-flag")).verdict == sro.FAIL


# ══════════════════════════════════════════════════════════════════════════════
# manifest-free leak scanner: word-boundary + length guard
# ══════════════════════════════════════════════════════════════════════════════
def test_leak_scanner_wordboundary_guard():
    # ordinary English near the pattern must NOT trip the scan
    for benign in ["the latest greatest contest", "attest to the protest", "run() -> None",
                   "pkg/a.py:10 def run(): ...", "callers: 3 in 2 files"]:
        assert sro.leak_tokens(benign) == [], f"false positive on {benign!r}"
    # genuine test identifiers MUST be caught (over-detection of overlapping tokens is fine —
    # a leak detector only needs to be NON-EMPTY on a real leak)
    assert "tests/test_pkg.py" in sro.leak_tokens("see tests/test_pkg.py")
    assert sro.leak_tokens("node ::test_run here")
    assert "test_foo" in sro.leak_tokens("call test_foo()")
    assert sro.leak_tokens("the widget_test module")
    assert "FAIL_TO_PASS" in sro.leak_tokens("FAIL_TO_PASS = [x]")


# ══════════════════════════════════════════════════════════════════════════════
# LAYER-2: StubSeamDriver end-to-end (no real seam)
# ══════════════════════════════════════════════════════════════════════════════
def test_stub_seam_driver_end_to_end(tmp_path):
    native1 = "<output>pkg/a.py</output>"
    native2 = "<output>edited</output>"
    root = _write_task(tmp_path, "syn__e2e",
                       _msgs(native1 + _L3B, native2),
                       [_seal_row(1, "l3b.evidence", _L3B)])
    rt = sro.reconstruct_task("syn__e2e", root)

    # a stub 'SS-correct' seam: it SUPPRESSES the step-behind l3b delivery
    def behavior(task):
        return [{"layer": "l3b.evidence", "m": 3, "chars_delivered": 0,
                 "outcome": "suppressed", "reason": "ss_step_behind", "payload": ""}]

    driver = sro.StubSeamDriver(behavior)
    replayed = sro.replay_task(rt, driver, root)
    assert isinstance(replayed, list) and replayed and replayed[0]["reason"] == "ss_step_behind"

    cases = {"suppress_step_behind": [
        {"task": "syn__e2e", "deliveries": ["l3b m3"], "why": "echo"}]}
    recorded = {"syn__e2e": rt.recorded_deliveries}
    verdicts = sro.evaluate_cases(cases, recorded, {"syn__e2e": replayed}, None)
    assert verdicts[0].verdict == sro.PASS


def test_seam_blocked_note_is_precise_not_bare_todo():
    """The MiniSeamDriver, with no repo snapshot, must BLOCK with a note that NAMES the missing
    input (the repo checkout) — never a bare TODO."""
    driver = sro.MiniSeamDriver(flag_env={"GT_SS_STEP_BEHIND": "1"}, repo_snapshot_root=None)
    rec_root = Path(sro.__file__).resolve()  # any path; begin_task should block before using it
    fake_root = Path("D:/gt_runs/29236533134/art")
    if not (fake_root / "conan-io__conan-17123" / "graph.db").is_file():
        pytest.skip("recorded artifacts not present in this environment")
    with pytest.raises(sro.SeamReplayBlocked) as ei:
        driver.begin_task("conan-io__conan-17123", fake_root)
    msg = str(ei.value)
    assert "REPO CHECKOUT" in msg and "_root()" in msg and "graph.db IS present" in msg
    assert "TODO" not in msg


# ══════════════════════════════════════════════════════════════════════════════
# SS-R2 — FIXPOINT DIFFER + GATING (the coordinator's fixpoint-first bounce)
# ══════════════════════════════════════════════════════════════════════════════
def _ledrow(it, layer, outcome="delivered", chars=0, sha=None, fp="", reason="", evt=""):
    return {"layer": layer, "event_type": evt, "outcome": outcome, "chars_delivered": chars,
            "content_sha256_16": sha, "file_path": fp, "reason": reason,
            "iteration": it, "timestamp_ms": 123}


def test_diff_ledgers_strict_and_channel_faithful():
    rows = [_ledrow(1, "L6", "STAGED_OK", reason="work=/tmp/gt_work.db"),
            _ledrow(5, "l3b.evidence", "delivered", 311, "aa" * 8, "pkg/a.py", evt="post_view")]
    fx = sro.diff_ledgers("t", rows, [dict(r, timestamp_ms=999) for r in rows])
    assert fx.strict and fx.channel and fx.faithful
    assert fx.boundary_iter == float("inf") and fx.diffs == []


def test_diff_ledgers_same_length_different_sha_is_not_moved():
    """MUTATION-CLASS bug the sha-aware classifier bites: a delivered payload of the SAME
    length but DIFFERENT bytes must be a real divergence (missing+extra), never 'moved'."""
    rec = [_ledrow(5, "l3.contract", "delivered", 525, "f" * 16, "pkg/a.py", evt="post_edit")]
    rep = [_ledrow(5, "l3.contract", "delivered", 525, "5" * 16, "pkg/a.py", evt="post_edit")]
    fx = sro.diff_ledgers("t", rec, rep)
    assert not fx.channel and fx.boundary_iter == 5.0
    ops = {d["op"] for d in fx.diffs}
    assert ops == {"missing", "extra"} and all(d["op"] != "moved" for d in fx.diffs)


def test_diff_ledgers_hidden_diffs_do_not_gate():
    """Hidden telemetry rows (candidate stamps, L6 counters, arbitration losers) may differ
    host-side; the GATE is the delivered plane. A hidden-only diff keeps the task faithful."""
    common = _ledrow(5, "l3b.evidence", "delivered", 311, "aa" * 8, "pkg/a.py", evt="post_view")
    rec = [_ledrow(1, "L6", "REINDEX_OK", reason="file=../tmp/x.py nodes_before=0 nodes_after=3"),
           common]
    rep = [_ledrow(1, "L6", "REINDEX_OK", reason="file=../tmp/x.py nodes_before=0 nodes_after=0"),
           dict(common, timestamp_ms=7)]
    fx = sro.diff_ledgers("t", rec, rep)
    assert fx.channel and fx.faithful and not fx.strict
    assert fx.n_hidden_diffs > 0 and fx.boundary_iter == float("inf")


def test_fixpoint_gate_unfaithful_task_yields_replay_unfaithful_not_fail():
    """THE GATE (and its mutation): on a task whose off-flag replay diverged BEFORE the case,
    a killed P5 must be REPLAY_UNFAITHFUL. With the gate disabled (fidelity=None — the
    pre-SS-R2 behavior) the same input produces a false CARDINAL KILL."""
    cases = {"preserve": [{"task": "T", "delivery": "consensus.scope m25", "why": "P5 consumed"}]}
    recorded = {"T": [sro.Delivery("consensus.scope", "review_transition", 12, 367, "x" * 16,
                                   25, "delivered", "", "", payload="p")]}
    rep = {"T": []}   # delivery gone — but the channel itself is broken
    fx = sro.FixpointResult("T", True, False, False, 9.0, 10, 3)   # diverged at iter 9 < 12
    gated = sro.evaluate_cases(cases, recorded, rep, None, fidelity={"T": fx})
    assert gated[0].verdict == sro.REPLAY_UNFAITHFUL and gated[0].cardinal
    # MUTATION: gate removed -> the SAME input yields a false cardinal kill
    ungated = sro.evaluate_cases(cases, recorded, rep, None, fidelity=None)
    assert ungated[0].verdict == sro.FAIL


def test_fixpoint_gate_trusted_prefix_judges_early_cases():
    """A case whose recorded delivery sits strictly BEFORE the first divergence is judged."""
    cases = {"preserve": [{"task": "T", "delivery": "l3b m11", "why": "P5 consumed"}]}
    recorded = {"T": [sro.Delivery("l3b.evidence", "post_view", 5, 311, "a" * 16,
                                   11, "delivered", "", "", payload="p")]}
    fx = sro.FixpointResult("T", True, False, False, 20.0, 10, 9,
                            drift_map=[(5, 5)])           # diverges at 20 -> iter 5 is trusted
    rep = {"T": [{"layer": "l3b.evidence", "iteration": 5, "chars_delivered": 311,
                  "outcome": "delivered", "content_sha256_16": "a" * 16, "reason": ""}]}
    v = sro.evaluate_cases(cases, recorded, rep, None, fidelity={"T": fx})
    assert v[0].verdict == sro.PASS


def test_fixpoint_gate_not_replayed_task():
    cases = {"preserve": [{"task": "T", "delivery": "l3b m11", "why": "P5 consumed"}]}
    recorded = {"T": [sro.Delivery("l3b.evidence", "post_view", 5, 311, "a" * 16,
                                   11, "delivered", "", "", payload="p")]}
    fx = sro.FixpointResult("T", False, False, False, -1.0, 10, 0, error="child rc=2")
    v = sro.evaluate_cases(cases, recorded, {"T": []}, None, fidelity={"T": fx})
    assert v[0].verdict == sro.REPLAY_UNFAITHFUL and "replay failed" in v[0].reason


def test_map_rec_iter_drift():
    fx = sro.FixpointResult("T", True, True, True, float("inf"), 5, 5,
                            drift_map=[(5, 5), (18, 16)])
    assert sro.map_rec_iter(fx, 5) == 5
    assert sro.map_rec_iter(fx, 12) == 12          # nearest anchor (5,5) + offset 7
    assert sro.map_rec_iter(fx, 19) == 17          # nearest anchor (18,16) + offset 1


def test_edit_applier_allowlist_shapes():
    """Decision-only checks (no execution): the three mutation shapes are recognized and
    the dangerous shapes are refused."""
    strip = sro._strip_lead_cd
    assert strip("cd $(cat /tmp/gt_root.txt) && sed -i 's/a/b/' x.py") == "sed -i 's/a/b/' x.py"
    # heredoc BODY containing denied words must not block the write (head-only checks)
    body = "cat > /tmp/p.py << 'EOF'\nimport pip, pytest\nEOF"
    assert sro._EDIT_ALLOW.match(body) and not sro._EDIT_DENY.search(sro._head_of(body))
    assert sro._EDIT_SCRATCH_PY.match("python3 /tmp/patch_code.py")
    assert sro._EDIT_PY_HEREDOC.match("python3 << 'EOF'\nprint(1)\nEOF")
    # refusals
    assert not sro._EDIT_SCRATCH_PY.match("python3 /testbed/setup.py install")
    assert sro._EDIT_DENY.search("pip install requests")
    assert sro._shell_redirect_targets("python3 -c 'if size > 1: print(size)'") == []
    assert sro._shell_redirect_targets("echo x > /tmp/ok.txt") == ["/tmp/ok.txt"]
    assert not sro._redirect_targets_ok("echo x > /etc/passwd")
    assert sro._redirect_targets_ok("echo x > /tmp/ok.txt")
    assert sro._redirect_targets_ok("git diff > /tmp/patch.txt")


@pytest.mark.parametrize("command", [
    "python3 -c 'value = 1 << 2; print(value)'",
    'python3 -c "value = 1 << 2; print(value)"',
])
def test_inline_python_left_shift_is_not_a_heredoc_boundary(command):
    assert sro._head_of(command) == command
    assert sro._shell_redirect_targets(command) == []


def test_unquoted_heredoc_operator_still_terminates_shell_head():
    command = "cat > /tmp/patch.py << 'PY'\nprint('content')\nPY"

    assert sro._head_of(command) == "cat > /tmp/patch.py "


def test_container_path_after_quoted_left_shift_is_still_rewritten():
    command = '''python3 -c "value = 1 << 2; print('/testbed/output.py')"'''
    drive = Path.cwd().drive or "D:"

    assert sro._rewrite_container_paths(command) == (
        f'''python3 -c "value = 1 << 2; print('{drive}/testbed/output.py')"'''
    )


def _git_fixture(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
    (repo / "sample.py").write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "sample.py"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=SS Test",
         "-c", "user.email=ss@example.invalid", "commit", "--quiet", "-m", "fixture"],
        check=True,
    )
    return repo


@pytest.mark.parametrize("command", [
    "python3 -c \"print(open('sample.py', encoding='utf-8').read())\"",
    "python3 -c \"import sys; sys.stdout.write('probe')\"",
    "cat sample.py",
    "git diff",
    "git add sample.py",
])
def test_materializer_excludes_read_only_probes(tmp_path, command):
    repo = _git_fixture(tmp_path)
    receipt = sro.apply_edit_command(command, cwd=str(repo))
    assert not receipt.candidate
    assert not receipt.executed
    assert not receipt.applied
    assert receipt.targets == []


def test_failed_python_write_is_not_applied_and_exposes_stderr(tmp_path):
    repo = _git_fixture(tmp_path)
    command = (
        "python3 -c \"from pathlib import Path; "
        "Path('sample.py').write_text('value = 2\\n', encoding='utf-8'); "
        "raise RuntimeError('materialization exploded')\""
    )
    receipt = sro.apply_edit_command(command, cwd=str(repo))
    assert receipt.candidate and receipt.executed
    assert receipt.rc != 0
    assert not receipt.applied
    assert "materialization exploded" in receipt.stderr
    assert any(t.changed and t.before_sha256 != t.after_sha256 for t in receipt.targets)


def test_successful_noop_write_requires_changed_hash(tmp_path):
    repo = _git_fixture(tmp_path)
    command = (
        "python3 -c \"from pathlib import Path; "
        "Path('sample.py').write_text('value = 1\\n', encoding='utf-8')\""
    )
    receipt = sro.apply_edit_command(command, cwd=str(repo))
    assert receipt.candidate and receipt.executed and receipt.rc == 0
    assert not receipt.applied
    assert receipt.targets
    assert all(not t.changed for t in receipt.targets)


def test_successful_write_receipt_has_confined_before_after_hashes(tmp_path):
    repo = _git_fixture(tmp_path)
    command = (
        "python3 -c \"from pathlib import Path; "
        "Path('sample.py').write_text('value = 3\\n', encoding='utf-8')\""
    )
    receipt = sro.apply_edit_command(command, cwd=str(repo))
    assert receipt.applied and receipt.rc == 0
    target = next(t for t in receipt.targets if t.path == "sample.py")
    assert target.confined and target.changed
    assert len(target.before_sha256 or "") == 64
    assert len(target.after_sha256 or "") == 64


@pytest.mark.parametrize("script", [
    "from pathlib import Path; Path('sample.py').rename('../escaped.py')",
    "from pathlib import Path; Path('sample.py').replace('../escaped.py')",
    "from pathlib import Path; Path('../escaped.py').open('w').write('escaped')",
    "open('../escaped.py', 'w').write('escaped')",
    "from pathlib import Path; Path('../escaped.py').unlink()",
    "import os; os.rename('sample.py', '../escaped.py')",
    "import os; os.replace('sample.py', '../escaped.py')",
    "import os; os.unlink('../escaped.py')",
    "import shutil; shutil.copy('sample.py', '../escaped.py')",
    "import shutil; shutil.copy2('sample.py', '../escaped.py')",
    "import shutil; shutil.copyfile('sample.py', '../escaped.py')",
    "import shutil; shutil.move('sample.py', '../escaped.py')",
    "from pathlib import Path; target = input(); Path(target).write_text('escaped')",
    "sink = object(); sink.write('escaped')",
])
def test_python_escape_and_dynamic_targets_are_rejected_before_execution(tmp_path, script):
    repo = _git_fixture(tmp_path)
    escaped = repo.parent / "escaped.py"
    receipt = sro.apply_edit_command(f'python3 -c "{script}"', cwd=str(repo))
    assert receipt.candidate and not receipt.executed and not receipt.applied
    assert "unsafe-python" in receipt.reason
    assert not escaped.exists()
    assert (repo / "sample.py").is_file()


def test_python_import_side_effect_is_rejected_before_execution(tmp_path):
    repo = _git_fixture(tmp_path)
    escaped = repo.parent / "escaped.py"
    (repo / "local_side_effect.py").write_text(
        "from pathlib import Path\n"
        "Path('../escaped.py').write_text('escaped', encoding='utf-8')\n",
        encoding="utf-8",
    )
    command = (
        "python3 -c \"import local_side_effect; from pathlib import Path; "
        "Path('sample.py').write_text('value = 9\\n', encoding='utf-8')\""
    )

    receipt = sro.apply_edit_command(command, cwd=str(repo))

    assert receipt.candidate and not receipt.executed and not receipt.applied
    assert "unsafe-python" in receipt.reason
    assert not escaped.exists()
    assert (repo / "sample.py").read_text("utf-8") == "value = 1\n"


@pytest.mark.parametrize("command", [
    "echo changed > sample.py; touch ../escaped.py",
    "echo changed > \"$(touch ../escaped.py; printf sample.py)\"",
])
def test_shell_secondary_execution_is_rejected_before_execution(tmp_path, command):
    repo = _git_fixture(tmp_path)
    escaped = repo.parent / "escaped.py"

    receipt = sro.apply_edit_command(command, cwd=str(repo))

    assert receipt.candidate and not receipt.executed and not receipt.applied
    assert "unsafe-shell" in receipt.reason
    assert not escaped.exists()
    assert (repo / "sample.py").read_text("utf-8") == "value = 1\n"


def test_shell_preflight_confines_every_sed_target_not_only_the_last(tmp_path):
    repo = _git_fixture(tmp_path)
    escaped = repo.parent / "escaped.py"
    escaped.write_text("value = 1\n", encoding="utf-8")

    receipt = sro.apply_edit_command(
        "sed -i 's/value = 1/value = 2/' ../escaped.py sample.py",
        cwd=str(repo),
    )

    assert receipt.candidate and not receipt.executed and not receipt.applied
    assert "target-outside-roots" in receipt.reason
    assert escaped.read_text("utf-8") == "value = 1\n"
    assert (repo / "sample.py").read_text("utf-8") == "value = 1\n"


def test_shell_preflight_confines_every_mkdir_target(tmp_path):
    repo = _git_fixture(tmp_path)
    escaped = repo.parent / "escaped-dir"

    receipt = sro.apply_edit_command(
        "mkdir -p ../escaped-dir safe-dir",
        cwd=str(repo),
    )

    assert receipt.candidate and not receipt.executed and not receipt.applied
    assert "target-outside-roots" in receipt.reason
    assert not escaped.exists()
    assert not (repo / "safe-dir").exists()


@pytest.mark.parametrize("option", ["patch --unsafe-paths", "git apply --unsafe-paths"])
def test_patch_unsafe_paths_option_is_denied_before_execution(tmp_path, option):
    repo = _git_fixture(tmp_path)
    command = (
        f"{option} <<'PATCH'\n"
        "--- sample.py\n"
        "+++ sample.py\n"
        "@@ -1 +1 @@\n"
        "-value = 1\n"
        "+value = 2\n"
        "PATCH"
    )

    receipt = sro.apply_edit_command(command, cwd=str(repo))

    assert receipt.candidate and not receipt.executed and not receipt.applied
    assert "unsafe-paths" in receipt.reason
    assert (repo / "sample.py").read_text("utf-8") == "value = 1\n"


def test_patch_headers_must_be_statically_present_and_confined(tmp_path):
    repo = _git_fixture(tmp_path)
    escaped = repo.parent / "escaped.py"
    escaped.write_text("value = 1\n", encoding="utf-8")
    escaping_patch = (
        "patch -p0 <<'PATCH'\n"
        "--- ../escaped.py\n"
        "+++ ../escaped.py\n"
        "@@ -1 +1 @@\n"
        "-value = 1\n"
        "+value = 2\n"
        "PATCH"
    )

    escaped_receipt = sro.apply_edit_command(escaping_patch, cwd=str(repo))
    unknown_receipt = sro.apply_edit_command("patch -p0 < /tmp/uninspected.patch", cwd=str(repo))

    assert escaped_receipt.candidate and not escaped_receipt.executed
    assert "patch-target-outside-roots" in escaped_receipt.reason
    assert unknown_receipt.candidate and not unknown_receipt.executed
    assert "patch-targets-unavailable" in unknown_receipt.reason
    assert escaped.read_text("utf-8") == "value = 1\n"


def test_inline_patch_with_confined_headers_remains_materializable(tmp_path):
    repo = _git_fixture(tmp_path)
    command = (
        "patch -p0 <<'PATCH'\n"
        "--- sample.py\n"
        "+++ sample.py\n"
        "@@ -1 +1 @@\n"
        "-value = 1\n"
        "+value = 2\n"
        "PATCH"
    )

    receipt = sro.apply_edit_command(command, cwd=str(repo))

    assert receipt.candidate and receipt.executed and receipt.applied, receipt.reason
    assert receipt.rc == 0
    assert (repo / "sample.py").read_text("utf-8") == "value = 2\n"


def test_single_quoted_backticks_are_literal_in_allowlisted_sed(tmp_path):
    repo = _git_fixture(tmp_path)
    (repo / "sample.py").write_text("anchor\n", encoding="utf-8")
    command = "sed -i '1a\\# example: `0 foo`' sample.py"

    receipt = sro.apply_edit_command(command, cwd=str(repo))

    assert receipt.candidate and receipt.executed and receipt.applied, receipt.reason
    assert (repo / "sample.py").read_text("utf-8") == "anchor\n# example: `0 foo`\n"


def test_real_dynaconf_pair_101_single_quoted_backticks_pass_preflight():
    recorded = Path("D:/gt_runs/29236533134/art")
    if not (recorded / "dynaconf__dynaconf-1225").is_dir():
        pytest.skip("dynaconf recording not present")
    recon = sro.reconstruct_task("dynaconf__dynaconf-1225", recorded)
    body = sro._strip_lead_cd(recon.pairs[101][0])

    safe, reason = sro._shell_preflight(body, "D:/testbed")

    assert safe, reason


def test_real_dynaconf_pair_101_materializes_recorded_write(tmp_path):
    recorded = Path("D:/gt_runs/29236533134/art")
    snapshot = Path("D:/gt_runs/ss_replay_snapshots/dynaconf__dynaconf-1225")
    if not (recorded / "dynaconf__dynaconf-1225").is_dir() or not snapshot.is_dir():
        pytest.skip("dynaconf recording or snapshot not present")
    repo = _git_fixture(tmp_path)
    target = repo / "dynaconf/utils/parse_conf.py"
    target.parent.mkdir(parents=True)
    target.write_bytes((snapshot / "dynaconf/utils/parse_conf.py").read_bytes())
    recon = sro.reconstruct_task("dynaconf__dynaconf-1225", recorded)

    receipt = sro.apply_edit_command(
        sro._strip_lead_cd(recon.pairs[101][0]), cwd=str(repo)
    )

    assert receipt.candidate and receipt.executed and receipt.applied, receipt.reason
    assert receipt.rc == recon.rcs[101] == 0
    assert b"class Insert(MetaValue):" in target.read_bytes()


def test_real_dynaconf_pair_122_does_not_treat_python_comparison_as_redirect():
    recorded = Path("D:/gt_runs/29236533134/art")
    if not (recorded / "dynaconf__dynaconf-1225").is_dir():
        pytest.skip("dynaconf recording not present")
    recon = sro.reconstruct_task("dynaconf__dynaconf-1225", recorded)
    body = sro._strip_lead_cd(recon.pairs[122][0])

    plan = sro._python_write_plan(body, "D:/testbed")
    declared, unsafe = sro._declared_targets(body, "D:/testbed")

    assert plan.candidate and plan.safe, plan.reason
    assert plan.targets == ("dynaconf/loaders/__init__.py",)
    assert unsafe == []
    assert "1:" not in declared


@pytest.mark.parametrize("command", [
    'echo changed > "$(printf sample.py)"',
    'echo changed > "`printf sample.py`"',
])
def test_double_quoted_shell_substitution_remains_rejected(tmp_path, command):
    repo = _git_fixture(tmp_path)

    receipt = sro.apply_edit_command(command, cwd=str(repo))

    assert receipt.candidate and not receipt.executed and not receipt.applied
    assert receipt.reason == "unsafe-shell:dynamic-execution"
    assert (repo / "sample.py").read_text("utf-8") == "value = 1\n"


def test_replay_child_forces_utf8_default_file_io(tmp_path, monkeypatch):
    monkeypatch.delenv("PYTHONUTF8", raising=False)
    env = sro.child_env(tmp_path, "synthetic__task", tmp_path / "ledger.jsonl", {})

    assert env["PYTHONUTF8"] == "1"


def test_utf8_child_default_materializes_ordinary_open_rewrite(tmp_path, monkeypatch):
    repo = _git_fixture(tmp_path)
    (repo / "sample.py").write_text("value = 'control-\u009d'\n", encoding="utf-8")
    monkeypatch.setenv("PYTHONUTF8", "1")
    command = (
        'python3 -c "p=\'sample.py\'; '
        "content=open(p, 'r').read(); "
        "open(p, 'w').write(content.replace('value', 'result'))\""
    )

    receipt = sro.apply_edit_command(command, cwd=str(repo))

    assert receipt.candidate and receipt.executed and receipt.applied, receipt.stderr
    assert (repo / "sample.py").read_text("utf-8") == "result = 'control-\u009d'\n"


def test_pathlib_open_write_is_classified_and_materialized(tmp_path):
    repo = _git_fixture(tmp_path)
    command = (
        "python3 -c \"from pathlib import Path; "
        "Path('sample.py').open('w', encoding='utf-8').write('value = 4\\n')\""
    )
    receipt = sro.apply_edit_command(command, cwd=str(repo))
    assert receipt.candidate and receipt.executed and receipt.applied
    assert (repo / "sample.py").read_text("utf-8") == "value = 4\n"


def test_confined_rename_receipt_covers_source_and_destination(tmp_path):
    repo = _git_fixture(tmp_path)
    command = (
        "python3 -c \"from pathlib import Path; "
        "Path('sample.py').rename('renamed.py')\""
    )
    receipt = sro.apply_edit_command(command, cwd=str(repo))
    assert receipt.applied
    targets = {target.path: target for target in receipt.targets}
    assert targets["sample.py"].before_sha256 and targets["sample.py"].after_sha256 is None
    assert targets["renamed.py"].before_sha256 is None and targets["renamed.py"].after_sha256
    assert all(target.confined and target.changed for target in targets.values())


def test_shell_move_cannot_remove_source_outside_repo(tmp_path):
    repo = _git_fixture(tmp_path)
    outside = repo.parent / "outside.py"
    outside.write_text("keep\n", encoding="utf-8")
    receipt = sro.apply_edit_command("mv ../outside.py imported.py", cwd=str(repo))
    assert receipt.candidate and not receipt.executed and not receipt.applied
    assert outside.read_text("utf-8") == "keep\n"
    assert not (repo / "imported.py").exists()


def test_real_babel_python_patch_is_static_and_confined():
    recorded = Path("D:/gt_runs/29236533134/art")
    if not (recorded / "python-babel__babel-1179").is_dir():
        pytest.skip("Babel recording not present")
    recon = sro.reconstruct_task("python-babel__babel-1179", recorded)
    command = recon.pairs[31][0]
    plan = sro._python_write_plan(sro._strip_lead_cd(command), "D:/testbed")
    assert plan.candidate and plan.safe, plan.reason
    assert plan.targets == ("babel/dates.py",)


def test_real_babel_pair_31_materializes_with_linux_utf8_default(tmp_path):
    recorded = Path("D:/gt_runs/29236533134/art")
    snapshot = Path("D:/gt_runs/ss_replay_snapshots/python-babel__babel-1179")
    if not (recorded / "python-babel__babel-1179").is_dir() or not snapshot.is_dir():
        pytest.skip("Babel recording or snapshot not present")
    repo = _git_fixture(tmp_path)
    target = repo / "babel/dates.py"
    target.parent.mkdir(parents=True)
    target.write_bytes((snapshot / "babel/dates.py").read_bytes())
    recon = sro.reconstruct_task("python-babel__babel-1179", recorded)

    receipt = sro.apply_edit_command(
        sro._strip_lead_cd(recon.pairs[31][0]), cwd=str(repo)
    )

    assert receipt.candidate and receipt.executed and receipt.applied, receipt.stderr
    assert receipt.rc == recon.rcs[31] == 0
    assert target.read_bytes() != (snapshot / "babel/dates.py").read_bytes()


def test_real_babel_pair_42_is_successful_same_byte_materialization(tmp_path):
    """The historical post-edit row at pair 42 is not evidence of changed bytes."""
    recorded = Path("D:/gt_runs/29236533134/art")
    snapshot = Path("D:/gt_runs/ss_replay_snapshots/python-babel__babel-1179")
    if not (recorded / "python-babel__babel-1179").is_dir() or not snapshot.is_dir():
        pytest.skip("Babel recording or snapshot not present")
    repo = _git_fixture(tmp_path)
    target = repo / "babel/dates.py"
    target.parent.mkdir(parents=True)
    target.write_bytes((snapshot / "babel/dates.py").read_bytes())
    recon = sro.reconstruct_task("python-babel__babel-1179", recorded)

    first_write = sro.apply_edit_command(
        sro._strip_lead_cd(recon.pairs[31][0]), cwd=str(repo)
    )
    assert first_write.candidate and first_write.executed and first_write.applied
    before_pair_42 = target.read_bytes()

    same_byte = sro.apply_edit_command(
        sro._strip_lead_cd(recon.pairs[42][0]), cwd=str(repo)
    )

    assert same_byte.candidate and same_byte.executed and not same_byte.applied
    assert same_byte.rc == recon.rcs[42] == 0
    assert same_byte.reason == "materialized-same-bytes"
    assert target.read_bytes() == before_pair_42
    receipts = {receipt.path: receipt for receipt in same_byte.targets}
    babel = receipts["babel/dates.py"]
    assert babel.confined and not babel.changed
    assert babel.before_sha256 == babel.after_sha256 == sro._sha256_path(target)
    assert sro._materialization_error(same_byte, recorded_rc=0, pair=42) is None


def test_temporary_only_python_inspection_is_not_a_repo_mutation_candidate():
    command = """python3 << 'PYEOF'
import os
import shutil
import tempfile
from arbitrary_package.matcher import Matcher

tmpdir = tempfile.mkdtemp()
config_path = os.path.join(tmpdir, '.config')
with open(config_path, 'w') as handle:
    handle.write('*.log\\n')
matcher = Matcher(config_path)
print(matcher.matches('example.log'))
shutil.rmtree(tmpdir)
PYEOF"""

    plan = sro._python_write_plan(command, "D:/testbed")

    assert not plan.candidate
    assert plan.safe
    assert plan.reason == "temporary-only-python"


def test_real_conan_pair15_is_temporary_only_inspection():
    recorded = Path("D:/gt_runs/29236533134/art")
    if not (recorded / "conan-io__conan-17123").is_dir():
        pytest.skip("Conan recording not present")
    recon = sro.reconstruct_task("conan-io__conan-17123", recorded)
    command = sro._strip_lead_cd(recon.pairs[15][0])

    plan = sro._python_write_plan(command, "D:/testbed")

    assert not plan.candidate
    assert plan.safe
    assert plan.reason == "temporary-only-python"


def test_real_conan_off_chronology_and_v2_write_truth_split(monkeypatch):
    """Exact Conan pairs: the real pair-13 source write is observed in both arms, while the
    pair-15 import/temp-file probe remains observable only in the immutable legacy OFF arm."""
    recorded = Path("D:/gt_runs/29236533134/art")
    if not (recorded / "conan-io__conan-17123").is_dir():
        pytest.skip("Conan recording not present")
    artifact = str(_REPO / "artifact_deepswe")
    if artifact not in sys.path:
        sys.path.insert(0, artifact)
    import gt_mini_patch as seam

    recon = sro.reconstruct_task("conan-io__conan-17123", recorded)
    commands = [sro._strip_lead_cd(recon.pairs[i][0]) for i in (13, 15)]
    classified = [seam._classify(cmd) for cmd in commands]

    monkeypatch.setenv("GT_SS_COHERENCE_V2", "0")
    monkeypatch.setenv("GT_SS_RECOVERY_V2", "0")
    off = [seam._ss_write_observation_required(cmd, kind, path)
           for cmd, (kind, path) in zip(commands, classified)]

    monkeypatch.setenv("GT_SS_COHERENCE_V2", "1")
    on = [seam._ss_write_observation_required(cmd, kind, path)
          for cmd, (kind, path) in zip(commands, classified)]

    assert off == [True, True]
    assert on == [True, False]


def test_real_conan_pair15_routes_only_through_legacy_off_observer(monkeypatch, tmp_path):
    """Drive the exact pair-15 command through the real seam routing, not only its predicate."""
    recorded = Path("D:/gt_runs/29236533134/art")
    if not (recorded / "conan-io__conan-17123").is_dir():
        pytest.skip("Conan recording not present")
    artifact = str(_REPO / "artifact_deepswe")
    if artifact not in sys.path:
        sys.path.insert(0, artifact)
    import gt_mini_patch as seam

    recon = sro.reconstruct_task("conan-io__conan-17123", recorded)
    command = sro._strip_lead_cd(recon.pairs[15][0])
    calls = []
    monkeypatch.setattr(seam, "_GT_BASELINE", False)
    monkeypatch.setattr(seam, "_ORACLE_ROUTE", True)
    monkeypatch.setattr(seam, "_root", lambda: str(tmp_path))
    monkeypatch.setattr(
        seam, "_subprocess_write_targets",
        lambda root, **kwargs: calls.append((root, kwargs)) or [],
    )

    monkeypatch.setenv("GT_SS_COHERENCE_V2", "0")
    monkeypatch.setenv("GT_SS_RECOVERY_V2", "0")
    seam._reset_oracle_state()
    seam._augment_output({"command": command}, {"output": recon.pairs[15][1], "returncode": 0})
    assert len(calls) == 1
    assert calls[0][1].get("force_paths") is None

    calls.clear()
    monkeypatch.setenv("GT_SS_COHERENCE_V2", "1")
    seam._reset_oracle_state()
    seam._augment_output({"command": command}, {"output": recon.pairs[15][1], "returncode": 0})
    assert calls == []


def test_historical_invalid_uses_only_canonical_failed_gates():
    cases = json.loads(
        (_REPO / "tests" / "fixtures" / "ss_replay" / "cases.json").read_text("utf-8")
    )
    allowed = {"delivered", "correct_info", "correct_rl_adhered_time", "acknowledged",
               "leak", "dose", "fair_probe"}
    assert all(set(case["fails_gates"]) <= allowed for case in cases["historical_invalid"])
    scope = next(case for case in cases["historical_invalid"]
                 if case["delivery"] == "consensus.scope m25")
    assert "fair_probe" in scope["fails_gates"]


def test_temporary_scratch_plus_repo_write_remains_a_mutation_candidate():
    command = """python3 << 'PYEOF'
import os
import tempfile

tmpdir = tempfile.mkdtemp()
with open(os.path.join(tmpdir, 'scratch'), 'w') as handle:
    handle.write('temporary')
with open('sample.py', 'w') as handle:
    handle.write('repository')
PYEOF"""

    plan = sro._python_write_plan(command, "D:/testbed")

    assert plan.candidate
    assert plan.targets == ("sample.py",)


def test_temporary_path_traversal_is_not_treated_as_disposable_scratch():
    command = """python3 << 'PYEOF'
import os
import tempfile

tmpdir = tempfile.mkdtemp()
escaped = os.path.join(tmpdir, '..', '..', 'sample.py')
with open(escaped, 'w') as handle:
    handle.write('not temporary')
PYEOF"""

    plan = sro._python_write_plan(command, "D:/testbed")

    assert plan.candidate and not plan.safe
    assert "dynamic-target" in plan.reason


def test_temporary_scratch_plus_unscoped_execution_fails_closed():
    command = """python3 << 'PYEOF'
import os
import tempfile

tmpdir = tempfile.mkdtemp()
with open(os.path.join(tmpdir, 'scratch'), 'w') as handle:
    handle.write('temporary')
os.system('printf changed > sample.py')
PYEOF"""

    plan = sro._python_write_plan(command, "D:/testbed")

    assert plan.candidate and not plan.safe
    assert "unsafe-call:os.system" in plan.reason


def test_denied_test_suffix_preserves_allowlisted_git_stash_state_transition(tmp_path):
    repo = _git_fixture(tmp_path)
    (repo / "sample.py").write_text("value = 2\n", encoding="utf-8")
    command = "git stash && python3 -m pytest tests/test_sample.py -x 2>&1 | tail -20"

    stash = sro.apply_edit_command(command, cwd=str(repo))

    assert stash.candidate and stash.executed and stash.applied
    assert stash.rc == 0
    assert (repo / "sample.py").read_text("utf-8") == "value = 1\n"

    restore = sro.apply_edit_command("git stash pop", cwd=str(repo))
    assert restore.candidate and restore.executed and restore.applied
    assert restore.rc == 0
    assert (repo / "sample.py").read_text("utf-8") == "value = 2\n"


def test_real_privacyidea_stash_then_pop_pair_is_reconstructed_chronologically():
    recorded = Path("D:/gt_runs/29236533134/art")
    if not (recorded / "privacyidea__privacyidea-4223").is_dir():
        pytest.skip("PrivacyIDEA recording not present")
    recon = sro.reconstruct_task("privacyidea__privacyidea-4223", recorded)

    stash_body = sro._strip_lead_cd(recon.pairs[63][0])
    projected, changed = sro._project_allowlisted_state_prefix(stash_body)
    pop_body = sro._strip_lead_cd(recon.pairs[64][0])

    assert changed and projected == "git stash"
    assert pop_body.strip() == "git stash pop"
    assert recon.rcs[63:65] == [0, 0]


def test_materialization_output_is_deterministically_bounded(tmp_path):
    repo = _git_fixture(tmp_path)
    command = (
        "python3 -c \"from pathlib import Path; import sys; "
        "Path('sample.py').write_text('changed', encoding='utf-8'); "
        "print('o' * 20000); print('e' * 20000, file=sys.stderr)\""
    )
    receipt = sro.apply_edit_command(command, cwd=str(repo))
    assert receipt.applied
    assert len(receipt.stdout) <= sro._MATERIALIZATION_OUTPUT_LIMIT
    assert len(receipt.stderr) <= sro._MATERIALIZATION_OUTPUT_LIMIT
    assert "truncated" in receipt.stdout and "truncated" in receipt.stderr


def test_recorded_success_sed_failure_fails_closed_and_keeps_stderr(tmp_path):
    repo = _git_fixture(tmp_path)
    receipt = sro.apply_edit_command("sed -i 's/old/new/' missing.py",
                                     cwd=str(repo))
    assert receipt.candidate and receipt.executed and receipt.rc != 0
    assert not receipt.applied
    error = sro._materialization_error(receipt, recorded_rc=0, pair=7)
    assert error == f"pair 7: replay returncode {receipt.rc} != recorded returncode 0"
    assert receipt.stderr


def test_recorded_success_same_byte_write_is_valid_materialization():
    """A historical post-edit row is not proof that a successful write changed bytes."""
    digest = "a" * 64
    receipt = sro.MaterializationReceipt(
        candidate=True, executed=True, applied=False,
        reason="changed no confined target bytes", rc=0,
        targets=[sro.MaterializedTarget("sample.py", digest, digest, False, True)],
    )
    assert sro._materialization_error(
        receipt, recorded_rc=0, pair=11) is None


def test_materialization_returncode_must_match_recording():
    receipt = sro.MaterializationReceipt(
        candidate=True, executed=True, applied=False,
        reason="changed no confined target bytes", rc=0,
    )

    error = sro._materialization_error(
        receipt, recorded_rc=1, pair=11)

    assert error == "pair 11: replay returncode 0 != recorded returncode 1"


def test_explicit_edit_then_restore_is_faithful_net_zero(tmp_path):
    repo = _git_fixture(tmp_path)
    receipt = sro.apply_edit_command(
        "sed -i 's/value = 1/value = 2/' sample.py && "
        "git checkout -- sample.py",
        cwd=str(repo),
    )

    assert receipt.candidate and receipt.executed and receipt.rc == 0
    assert not receipt.applied
    assert receipt.reason == "materialized-explicit-restore"
    assert (repo / "sample.py").read_text("utf-8") == "value = 1\n"
    assert sro._materialization_error(
        receipt, recorded_rc=0, pair=11
    ) is None


def test_child_never_feeds_seam_after_materialization_failure(tmp_path, monkeypatch):
    calls: list[dict] = []

    class FakeSeam:
        @staticmethod
        def _build_env_executor():
            return "unrelated-live-executor"

        @staticmethod
        def _build_edit_check_executor():
            return None

        @staticmethod
        def _ss_capture_write_preimage(_action):
            return None

        @staticmethod
        def _augment_output(action, out):
            calls.append({"action": action, "out": out})

    receipt = sro.MaterializationReceipt(
        candidate=True, executed=True, applied=False, reason="rc=1", rc=1,
        stderr="host write failed",
    )
    recon = sro.ReconstructedTask("syn__materialize", [("sed -i s/a/b/ x.py", "native")],
                                  [], [], [], 4, rcs=[0])
    monkeypatch.setitem(sys.modules, "gt_mini_patch", FakeSeam)
    monkeypatch.setattr(sro, "reconstruct_task", lambda *_args: recon)
    monkeypatch.setattr(sro, "apply_edit_command", lambda *_args, **_kwargs: receipt)
    monkeypatch.setenv("GT_RUNTIME_LEDGER", str(tmp_path / "ledger.jsonl"))
    out = tmp_path / "child.json"
    assert sro.replay_child_main("syn__materialize", tmp_path, out, apply_edits=True) == 0
    payload = json.loads(out.read_text("utf-8"))
    assert calls == []
    assert payload["rows"] == []
    assert payload["materialization_error"] == (
        "pair 0: replay returncode 1 != recorded returncode 0"
    )


def test_child_never_feeds_seam_when_replay_succeeds_but_recording_failed(
        tmp_path, monkeypatch):
    calls: list[dict] = []

    class FakeSeam:
        @staticmethod
        def _build_edit_check_executor():
            return None

        @staticmethod
        def _ss_capture_write_preimage(_action):
            return None

        @staticmethod
        def _augment_output(action, out):
            calls.append({"action": action, "out": out})

    receipt = sro.MaterializationReceipt(
        candidate=True, executed=True, applied=False,
        reason="materialized-same-bytes", rc=0,
    )
    recon = sro.ReconstructedTask(
        "syn__reverse_rc", [("touch existing.py", "recorded failure")],
        [], [], [], 4, rcs=[1],
    )
    monkeypatch.setitem(sys.modules, "gt_mini_patch", FakeSeam)
    monkeypatch.setattr(sro, "reconstruct_task", lambda *_args: recon)
    monkeypatch.setattr(sro, "apply_edit_command", lambda *_args, **_kwargs: receipt)
    monkeypatch.setenv("GT_RUNTIME_LEDGER", str(tmp_path / "ledger.jsonl"))
    out = tmp_path / "child_reverse_rc.json"

    assert sro.replay_child_main("syn__reverse_rc", tmp_path, out, apply_edits=True) == 0
    payload = json.loads(out.read_text("utf-8"))
    assert calls == []
    assert payload["rows"] == []
    assert payload["materialization_error"] == (
        "pair 0: replay returncode 0 != recorded returncode 1"
    )


def test_child_captures_seam_preimage_before_materializing_edit(tmp_path, monkeypatch):
    chronology: list[str] = []

    class FakeSeam:
        @staticmethod
        def _build_env_executor():
            return "unrelated-live-executor"

        @staticmethod
        def _build_edit_check_executor():
            return None

        @staticmethod
        def _ss_capture_write_preimage(action):
            assert action == {"command": "sed -i s/a/b/ x.py"}
            chronology.append("preimage")

        @staticmethod
        def _augment_output(action, out):
            assert os.environ["GT_VERIFY_EXECUTE"] == "0"
            assert FakeSeam._build_env_executor() == "unrelated-live-executor"
            rc, stdout, stderr = FakeSeam._build_edit_check_executor()(
                ["python", "-V"], "D:/testbed", 10)
            assert rc is None and stdout == "" and "toolchain_unavailable" in stderr
            chronology.append("seam")

    receipt = sro.MaterializationReceipt(
        candidate=True, executed=True, applied=True, reason="materialized", rc=0,
        targets=[sro.MaterializedTarget("x.py", "a" * 64, "b" * 64, True, True)],
    )
    recon = sro.ReconstructedTask(
        "syn__chronology", [("sed -i s/a/b/ x.py", "native")], [], [], [], 4, rcs=[0]
    )
    monkeypatch.setitem(sys.modules, "gt_mini_patch", FakeSeam)
    monkeypatch.setattr(sro, "reconstruct_task", lambda *_args: recon)

    def materialize(*_args, **_kwargs):
        chronology.append("materialize")
        return receipt

    monkeypatch.setattr(sro, "apply_edit_command", materialize)
    monkeypatch.setenv("GT_RUNTIME_LEDGER", str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv("GT_VERIFY_EXECUTE", "0")
    out = tmp_path / "child.json"

    assert sro.replay_child_main("syn__chronology", tmp_path, out, apply_edits=True) == 0
    assert chronology == ["preimage", "materialize", "seam"]


def test_child_fails_closed_when_preimage_capture_faults(tmp_path, monkeypatch):
    chronology: list[str] = []

    class FakeSeam:
        @staticmethod
        def _build_edit_check_executor():
            return None

        @staticmethod
        def _ss_capture_write_preimage(_action):
            chronology.append("preimage")
            raise OSError("snapshot unavailable")

        @staticmethod
        def _augment_output(_action, _out):
            chronology.append("seam")

    recon = sro.ReconstructedTask(
        "syn__capture_fault",
        [("sed -i s/a/b/ x.py", "native"), ("sed -i s/b/c/ x.py", "native-2")],
        [], [], [], 4, rcs=[0, 0],
    )
    monkeypatch.setitem(sys.modules, "gt_mini_patch", FakeSeam)
    monkeypatch.setattr(sro, "reconstruct_task", lambda *_args: recon)
    monkeypatch.setattr(
        sro, "apply_edit_command",
        lambda *_args, **_kwargs: chronology.append("materialize"),
    )
    monkeypatch.setenv("GT_RUNTIME_LEDGER", str(tmp_path / "ledger.jsonl"))
    out = tmp_path / "child_capture_fault.json"

    assert sro.replay_child_main("syn__capture_fault", tmp_path, out, apply_edits=True) == 0
    payload = json.loads(out.read_text("utf-8"))
    assert chronology == ["preimage"]
    assert payload["rows"] == []
    assert payload["materialization_error"] == (
        "pair 0: pre-materialization capture failed (OSError: snapshot unavailable)"
    )


@pytest.mark.parametrize("post_edit_witness", [False, True])
def test_child_feeds_successful_same_byte_write_regardless_of_post_edit_telemetry(
        tmp_path, monkeypatch, post_edit_witness):
    calls: list[dict] = []

    class FakeSeam:
        @staticmethod
        def _build_edit_check_executor():
            return None

        @staticmethod
        def _ss_capture_write_preimage(_action):
            return None

        @staticmethod
        def _augment_output(action, out):
            assert FakeSeam._batch_commit_installed is True
            assert FakeSeam._batch_install_failed is False
            calls.append(action)

    receipt = sro.MaterializationReceipt(
        candidate=True, executed=True, applied=False,
        reason="changed no confined target bytes", rc=0,
    )
    rows = ([{"event_type": "post_edit", "iteration": 1}] if post_edit_witness else [])
    recon = sro.ReconstructedTask("syn__noop", [("touch existing.py", "native")],
                                  [], [], rows, 4, rcs=[0])
    monkeypatch.setitem(sys.modules, "gt_mini_patch", FakeSeam)
    monkeypatch.setattr(sro, "reconstruct_task", lambda *_args: recon)
    monkeypatch.setattr(sro, "apply_edit_command", lambda *_args, **_kwargs: receipt)
    monkeypatch.setenv("GT_RUNTIME_LEDGER", str(tmp_path / "ledger.jsonl"))
    out = tmp_path / f"child_{post_edit_witness}.json"
    sro.replay_child_main("syn__noop", tmp_path, out, apply_edits=True)
    payload = json.loads(out.read_text("utf-8"))
    assert len(calls) == 1
    assert payload["materialization_error"] is None


def test_conan_corrected_boundary_manifest_matches_observed_sealed_rows():
    cases = json.loads(
        (_REPO / "tests" / "fixtures" / "ss_replay" / "cases.json").read_text("utf-8")
    )
    by_label = {case["label"]: case for case in cases["corrected_boundaries"]}

    scope = by_label["consensus.scope after first real source write"]
    assert (scope["expected_iteration"], scope["expected_chars"], scope["expected_sha"]) == (
        20, 367, "7ab99be457a48275"
    )

    obligation = by_label["same-turn stale obligation after behavioral proof"]
    assert obligation["layer"] == "obligation.unexercised"
    assert obligation["passing_test_pair"] == 15
    assert obligation["expected_iteration"] == 16
    assert obligation["expected_outcome"] == "suppressed"
    assert obligation["expected_reason"] == "ss_late"
    assert obligation["expected_chars"] == 0
    assert obligation["expected_clause_id"] == "8c8a5079"
    assert obligation["expected_subject_digest"] == "f530f49a477795ba"
    assert obligation["expected_proof_turn"] == 16
    assert obligation["forbidden_delivery_text"].startswith(
        "However, they would be more useful")
    assert obligation["acknowledgment"] == "NOT_APPLICABLE_SUPPRESSED"


def test_real_babel_pair42_is_a_successful_same_byte_write(tmp_path):
    recorded = Path("D:/gt_runs/29236533134/art")
    snapshot = Path("D:/gt_runs/ss_replay_snapshots/python-babel__babel-1179")
    source = snapshot / "babel" / "dates.py"
    if not (recorded / "python-babel__babel-1179").is_dir() or not source.is_file():
        pytest.skip("Babel recording/snapshot not present")

    repo = _git_fixture(tmp_path)
    (repo / "babel").mkdir()
    shutil.copyfile(source, repo / "babel" / "dates.py")
    subprocess.run(["git", "-C", str(repo), "add", "babel/dates.py"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "add babel fixture"],
        check=True, capture_output=True,
    )
    recon = sro.reconstruct_task("python-babel__babel-1179", recorded)

    first = sro.apply_edit_command(sro._strip_lead_cd(recon.pairs[31][0]), str(repo))
    assert first.candidate and first.executed and first.applied and first.rc == 0
    before_noop = (repo / "babel" / "dates.py").read_bytes()

    noop = sro.apply_edit_command(sro._strip_lead_cd(recon.pairs[42][0]), str(repo))

    assert noop.candidate and noop.executed and noop.rc == 0 and not noop.applied
    assert noop.reason == "materialized-same-bytes"
    assert len(noop.targets) == 1 and noop.targets[0].confined
    assert noop.targets[0].before_sha256 == noop.targets[0].after_sha256
    assert (repo / "babel" / "dates.py").read_bytes() == before_noop
    assert sro._materialization_error(noop, recorded_rc=0, pair=42) is None


def test_real_babel_pair_46_allows_builtin_repr_but_not_dynamic_call_resolution():
    """A pure builtin repr observation must not poison an otherwise confined write plan."""
    recorded = Path("D:/gt_runs/29236533134/art")
    if not (recorded / "python-babel__babel-1179").is_dir():
        pytest.skip("Babel recording not present")
    recon = sro.reconstruct_task("python-babel__babel-1179", recorded)

    plan = sro._python_write_plan(
        sro._strip_lead_cd(recon.pairs[46][0]), "D:/testbed"
    )

    assert plan.candidate and plan.safe, plan.reason
    assert plan.targets == ("babel/dates.py",)
    assert recon.rcs[46] == 0

    dynamic = sro._python_write_plan(
        "python3 -c \"from pathlib import Path; "
        "formatter = globals()['repr']; "
        "Path('sample.py').write_text(formatter('value'), encoding='utf-8')\"",
        "D:/testbed",
    )
    assert dynamic.candidate and not dynamic.safe
    assert "unknown-call:globals" in dynamic.reason
    assert "unknown-call:formatter" in dynamic.reason


def test_manifest_covers_every_recorded_trajectory_and_classifies_audit_only_tasks():
    rec_root = Path("D:/gt_runs/29236533134/art")
    if not rec_root.is_dir():
        pytest.skip("recorded artifacts not present in this environment")
    recorded = {
        d.name for d in rec_root.iterdir()
        if d.is_dir() and (d / "mini-swe-agent.trajectory.json").is_file()
    }
    cases = json.loads((_REPO / "tests" / "fixtures" / "ss_replay" / "cases.json").read_text("utf-8"))
    manifest = set(sro._manifest_tasks(cases))
    assert len(recorded) == 29
    assert manifest == recorded
    audit_only = {c["task"] for c in cases["audit_only"]}
    assert audit_only == {
        "aws-cloudformation__cfn-lint-3749",
        "beeware__briefcase-2075",
        "bridgecrewio__checkov-6893",
        "facebookresearch__hydra-3005",
        "ipython__ipython-14798",
        "jupyterlab__jupyter-ai-1294",
        "keras-team__keras-20443",
    }
    assert all("snapshot" in c.get("why", "").lower() for c in cases["audit_only"])


def test_babel_coherence_fixture_uses_post_pass_successful_write_window():
    cases = json.loads(
        (_REPO / "tests" / "fixtures" / "ss_replay" / "cases.json").read_text("utf-8")
    )
    task = "python-babel__babel-1179"
    preserved = [case for case in cases["preserve"] if case.get("task") == task]
    accuracy = [
        case for case in cases["suppress_coherence_miscount"]
        if isinstance(case, dict) and case.get("task") == task
    ]

    assert preserved == []
    assert accuracy == [{
        "task": task,
        "delivery": "detect.coherence m97",
        "actual_success": 2,
        "claimed": 4,
        "note": "passing test at m75 resets the window; successful writes m86/m94 precede m97",
    }]


def test_oracle_explicitly_toggles_internal_edit_diagnostic_refinement():
    assert "GT_SS_EDIT_DIAG" in sro._SS_FLAGS_ALL


def test_requested_tasks_rejects_unknown_ids_with_sorted_diagnostics():
    cases = {"preserve": [{"task": "known__task", "delivery": "x"}]}
    selected, unknown = sro._requested_tasks(cases, "known__task, typo__task,known__task")
    assert selected == ["known__task"]
    assert unknown == ["typo__task"]


def test_replay_child_disables_live_test_execution_and_preserves_arm_override(tmp_path):
    env = sro.child_env(
        tmp_path, "synthetic__task", tmp_path / "ledger.jsonl",
        {"GT_SS_EDIT_DIAG": "0"},
    )

    assert env["GT_VERIFY_EXECUTE"] == "0"
    assert env["GT_SS_EDIT_DIAG"] == "0"


def test_audit_only_coverage_requires_a_faithful_replay():
    cases = {"audit_only": [
        {"task": "A", "why": "repo snapshot absent"},
        {"task": "B", "why": "repo snapshot absent"},
    ]}
    faithful = sro.FixpointResult("A", True, False, True, float("inf"), 3, 3)
    unfaithful = sro.FixpointResult("B", True, False, False, 7.0, 3, 2)
    findings = sro._audit_coverage_findings(cases, {"A": faithful, "B": unfaithful})
    assert len(findings) == 1 and findings[0].startswith("B:") and "unfaithful" in findings[0]
    assert sro._audit_coverage_findings(cases, {"A": faithful, "B": faithful}) == []


def test_full_scope_coverage_rejects_every_unfaithful_task_and_case():
    faithful = sro.FixpointResult("A", True, False, True, float("inf"), 3, 3)
    unfaithful = sro.FixpointResult("B", True, False, False, 7.0, 3, 2)
    verdicts = [sro.CaseVerdict("coherence", "B", "detect.coherence m9",
                                sro.REPLAY_UNFAITHFUL, "outside trusted prefix", False)]
    findings = sro._full_coverage_findings(
        ["A", "B"], {"A": faithful, "B": unfaithful}, verdicts, full_scope=True)
    assert any("B: unfaithful" in finding for finding in findings)
    assert any("REPLAY_UNFAITHFUL" in finding and "detect.coherence m9" in finding
               for finding in findings)
    assert sro._full_coverage_findings(
        ["A"], {"A": faithful}, [], full_scope=True) == []


def test_targeted_scope_retains_trusted_prefix_inconclusive_semantics():
    unfaithful = sro.FixpointResult("B", True, False, False, 7.0, 3, 2)
    verdicts = [sro.CaseVerdict("coherence", "B", "detect.coherence m9",
                                sro.REPLAY_UNFAITHFUL, "outside trusted prefix", False)]
    assert sro._full_coverage_findings(
        ["B"], {"B": unfaithful}, verdicts, full_scope=False) == []


def test_selected_cases_exclude_off_subset_tasks_without_weakening_full_selection():
    cases = {
        "schema": "synthetic",
        "preserve": [{"task": "A", "delivery": "l3b m3"}],
        "recovery_earlier_release": [{"task": "B", "why": "whole-run"}],
        "suppress_coherence_miscount": [
            {"task": "A", "delivery": "detect.coherence m5"},
            "assert: global family note",
        ],
        "invariants": {"leak": "0"},
    }
    selected = sro._select_cases(cases, {"A"})
    assert selected["preserve"] == cases["preserve"]
    assert selected["recovery_earlier_release"] == []
    assert selected["suppress_coherence_miscount"] == cases["suppress_coherence_miscount"]
    assert selected["invariants"] == cases["invariants"]
    assert sro._select_cases(cases, {"A", "B"}) == cases


def test_real_hydra_zero_byte_delivered_outcome_bites_empty_invariant():
    task = "facebookresearch__hydra-3005"
    ledger = (Path("D:/gt_runs/29236533134/art") / task /
              f"gt_runtime_ledger_{task}.jsonl")
    if not ledger.is_file():
        pytest.skip("Hydra recording not present in this environment")
    rows = [json.loads(line) for line in ledger.read_text("utf-8").splitlines() if line.strip()]
    witness = [r for r in rows if r.get("layer") == "ga.trace_frame"
               and r.get("outcome") == "delivered" and r.get("chars_delivered") == 0]
    assert len(witness) == 1
    inv = sro.evaluate_invariants({task: []}, {task: rows}, {task: rows}, None)
    assert next(i for i in inv if i.name.startswith("no empty")).verdict == sro.FAIL


# ── guarded real-data coverage (only when the recording is on disk) ───────────
def test_real_recording_reconstructs_zero_residual():
    rec_root = Path("D:/gt_runs/29236533134/art")
    if not (rec_root / "conan-io__conan-17092" / "graph.db").is_file():
        pytest.skip("recorded artifacts not present in this environment")
    for task in ("conan-io__conan-17092", "conan-io__conan-17123", "python-babel__babel-1179"):
        rt = sro.reconstruct_task(task, rec_root)
        assert rt.residual_leaks == [], f"{task}: {rt.residual_leaks}"
        assert rt.recorded_deliveries, f"{task}: no deliveries reconstructed"
        # every reconstructed payload is leak-free
        for d in rt.recorded_deliveries:
            assert sro.leak_tokens(d.payload) == [], f"{task} {d.layer}: {sro.leak_tokens(d.payload)}"
