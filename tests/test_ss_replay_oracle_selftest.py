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
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "scripts" / "swebench"))

import ss_replay_oracle as sro  # noqa: E402


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


def _rep(layer, m, chars, payload="", reason="", outcome="delivered"):
    return {"layer": layer, "m": m, "chars_delivered": chars, "outcome": outcome,
            "reason": reason, "payload": payload}


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


def test_cardinal_flag_selects_exactly_the_three_p5s():
    cases = json.loads((_REPO / "tests" / "fixtures" / "ss_replay" / "cases.json").read_text("utf-8"))
    cardinals = [c for c in cases["preserve"] if sro._is_cardinal_preserve(c)]
    assert len(cardinals) == 3
    assert {c["task"] for c in cardinals} == {
        "conan-io__conan-17123", "geopandas__geopandas-3471", "dynaconf__dynaconf-1225"}


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

    # also PASS when the dup is simply absent from the replayed ledger
    v_absent = sro.evaluate_cases(cases, recorded, {_DUP_TASK: []}, None)
    assert v_absent[0].verdict == sro.PASS


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
