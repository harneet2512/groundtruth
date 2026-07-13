"""SS-6 gate SELFTEST — prove the gate BITES.

The SS gate (``scripts/swebench/ss_gate.py``) drives the REAL seam and, until SS-0/SS-1 land,
SKIPs the feature scenarios (byte-identical no-op) while enforcing the always-on invariants
(S9 leak+dose, S10 byte-identity). These tests do NOT prove GT passes — they prove the GATE
CORRECTLY DISTINGUISHES a spec-CORRECT SS implementation from a broken one, by driving the SAME
scenario code the gate runs against a SS-reference test double and a set of biting mutations.

Each mutation is a concrete WRONG behaviour that a landed SS feature could exhibit; the matching
scenario MUST turn RED. The gate consumes only the public driver seam (``driver.run``), so these
tests keep working unchanged as SS lands and the real seam starts responding to the flags.
"""
from __future__ import annotations

import pathlib
import re
import sys

import pytest

_SS_DIR = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "swebench"
if str(_SS_DIR) not in sys.path:
    sys.path.insert(0, str(_SS_DIR))

import ss_gate as G  # noqa: E402


# --------------------------------------------------------------------------- #
# A faithful SS-REFERENCE seam (test double). Given the fixture graph it knows,
# it implements the SS standard the gate enforces: step-behind suppression
# (ss_step_behind), caller_facts entity-set dedup (ss_semantic_dup), obligation/
# localization late-drop (ss_late), leak-safety, and <=1 dose. `mutations` toggle
# specific WRONG behaviours so the matching scenario turns RED — the biting proof.
# --------------------------------------------------------------------------- #
_GRAPH = {
    "run": ["pkg/mod_a.py", "pkg/mod_b.py"],       # ambiguous -> a def/ref partition delivery
    "gizmo": ["tmp/scratch.py", "htmlcov/x.js"],   # provenance fixture
}

# S2 caller_facts GROUP: editing a file delivers a caller-facts block with a known entity set.
# Editing mod_b delivers the SUPERSET (l3b.evidence); editing util.py delivers a byte-DISTINCT
# SUBSET (l3.contract). Both classes are in the ``caller_facts`` dedup group -> the second
# (subset) is entity-set-deduped when GT_SS_DEDUP2 is on.
_CALLER_FACTS = {
    "pkg/mod_b.py": ("l3b.evidence",
                     "\npkg/mod_a.py:4:consumer\npkg/mod_b.py:9:consumer\n"
                     "pkg/mod_a.py:4:alpha\npkg/util.py:4:sig_target",
                     frozenset({"pkg/mod_a.py", "pkg/mod_b.py", "pkg/util.py",
                                "consumer", "alpha", "sig_target"})),
    "pkg/util.py": ("l3.contract",
                    "\npkg/mod_b.py:21:return sig_target(1)",
                    frozenset({"pkg/mod_b.py", "sig_target"})),
}
_CALLER_FACTS_GROUP = "caller_facts"
# S6 late-drop: an ambiguous symbol whose def/ref partition surfaces ONLY the bare symbol as its
# code identifier (2-char def-file stems -> no dotted path token). Late-dropped when that symbol
# was already covered by a PASSING test (seeded into pass-tokens from the test command/output).
_LATE_SYMS = {"late_probe": ["pkg/io.py", "pkg/db.py"]}
# S8 empty-payload: an ambiguous symbol whose def sites are ALL leak-filtered (node_modules/),
# so the produced envelope renders ZERO bytes. Under GT_SS_ARBITER_V2 the empty envelope must
# NEVER become a delivered ledger row (the guard); off, the un-guarded old behaviour delivers a
# chars=0 row. (Through the REAL mini seam this is byte-identical in both arms — see scenario_s8;
# the fake makes the guard OBSERVABLE so the gate's empty-payload CHECK is proven to bite.)
_EMPTY_SYMS = {"vend_probe": ["node_modules/a.js", "node_modules/b.js"]}
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _fake_seam(mutations=frozenset()):
    mut = set(mutations)

    def behavior(events, ss_env, submit_attempts=0):
        viewed: set[str] = set()
        obs: list = []
        ledger: list[dict] = []
        edited: set[str] = set()          # S11: files the agent edited this episode
        last_failing: dict | None = None  # S11: last test touching an edit that FAILED (unresolved)
        pass_tokens: set[str] = set()     # S6: tokens observed in PASSING test events
        delivered_ents: dict = {}         # S2: dedup group -> [delivered entity sets]
        it = 0
        for ev in events:
            it += 1
            cmd = (ev.action.get("command") or "")
            before = ev.output or ""
            after = before
            out = ev.output or ""
            toks = cmd.split()
            if cmd == "str_replace":                         # S11/S2: an edit event
                p = ev.action.get("path")
                if p and ev.rc == 0:
                    edited.add(str(p))
                    # S2 caller_facts delivery for the known fixture edits (mod_b/util).
                    cf = _CALLER_FACTS.get(str(p))
                    if cf is not None:
                        kind, block, ents = cf
                        dedup_on = ss_env.get("GT_SS_DEDUP2") == "1"
                        prior = delivered_ents.get(_CALLER_FACTS_GROUP, ())
                        contained = any(ents <= pr for pr in prior)
                        # reference: suppress a same-group fact whose entity set ⊆ a prior one,
                        # ATTRIBUTED to ss_semantic_dup (the audit evidence). dedup2_wrong_reason:
                        # still suppress (an EFFECT), but mislabel it so the audit cannot attribute
                        # the drop to the semantic dedup -> S2 must FAIL (dup_row absent).
                        do_dedup = dedup_on and contained
                        if do_dedup:
                            _reason = ("delivered" if "dedup2_wrong_reason" in mut
                                       else "ss_semantic_dup")
                            ledger.append(dict(layer=kind, event_type="", file_path=str(p),
                                               outcome="suppressed_duplicate",
                                               reason=_reason,
                                               chars_delivered=0, iteration=it))
                        else:
                            after = before + block
                            ledger.append(dict(layer=kind, event_type="", file_path=str(p),
                                               outcome="delivered", reason="",
                                               chars_delivered=len(block), iteration=it))
                            delivered_ents.setdefault(_CALLER_FACTS_GROUP, []).append(ents)
            elif ("passed" in out) or ("failed" in out):     # S11/S6: a test event
                failed = ("failed" in out) or (ev.rc != 0)
                passed = ("passed" in out) and not failed
                import os as _os
                hay = cmd + "\n" + out
                touches = any(e and (e in hay or _os.path.basename(e) in hay) for e in edited)
                if touches and (failed or passed):
                    last_failing = {"cmd": cmd} if failed else None
                if passed:                                   # S6: seed pass-tokens from cmd+output
                    pass_tokens.update(t for t in _TOKEN_RE.findall(hay) if len(t) >= 3)
            if toks[:1] == ["cat"] and len(toks) >= 2:
                viewed.add(toks[1].strip())
            elif toks[:1] == ["grep"]:
                sym = next((t for t in toks[1:] if not t.startswith("-") and t != "."), None)
                if sym in _EMPTY_SYMS:
                    # S8 empty-payload guard. Reference: GT_SS_ARBITER_V2 DETECTS the empty
                    # envelope and DROPS it (a suppressed ss_empty_payload row, never a delivered
                    # row); off, the un-guarded path delivers the degenerate chars=0 row.
                    # arbiter_detects_but_delivers: the guard records the drop reason but FAILS to
                    # actually suppress — it STILL delivers the chars=0 row (an EFFECT, but the
                    # degenerate row survives) -> S8 must FAIL.
                    deffiles = _EMPTY_SYMS[sym]
                    arb_on = ss_env.get("GT_SS_ARBITER_V2") == "1"
                    if arb_on:
                        ledger.append(dict(layer="gateway", event_type="", file_path=deffiles[0],
                                           outcome="suppressed_hidden_only",
                                           reason="ss_empty_payload",
                                           chars_delivered=0, iteration=it))
                        if "arbiter_detects_but_delivers" in mut:
                            ledger.append(dict(layer="gateway", event_type="", file_path=deffiles[0],
                                               outcome="delivered", reason="",
                                               chars_delivered=0, iteration=it))
                    else:
                        # the empty-payload DELIVERED row (chars=0, zero model bytes) — the exact
                        # degenerate row the ARBITER_V2 guard exists to prevent.
                        ledger.append(dict(layer="gateway", event_type="", file_path=deffiles[0],
                                           outcome="delivered", reason="",
                                           chars_delivered=0, iteration=it))
                elif sym in _LATE_SYMS:
                    # S6 late-drop: a resurfaced localization partition whose ONLY code ident is
                    # the bare symbol. Late-dropped iff GT_SS_LATE_DROP on AND the symbol was
                    # passing-tested. latedrop_ignore_pass: deliver anyway (wrong) -> S6 FAIL.
                    deffiles = _LATE_SYMS[sym]
                    late_on = ss_env.get("GT_SS_LATE_DROP") == "1"
                    covered = sym in pass_tokens
                    # reference: drop the resurfaced fact ATTRIBUTED to ss_late. latedrop_wrong_reason:
                    # still drop (an EFFECT), but mislabel it as ss_step_behind so the audit cannot
                    # attribute it to the late-drop -> S6 must FAIL (late_row absent).
                    do_late = late_on and covered
                    if do_late:
                        _reason = ("ss_step_behind" if "latedrop_wrong_reason" in mut
                                   else "ss_late")
                        ledger.append(dict(layer="gateway", event_type="", file_path=deffiles[0],
                                           outcome="suppressed_hidden_only", reason=_reason,
                                           chars_delivered=0, iteration=it))
                    else:
                        block = "\n" + "\n".join("%s:6:%s" % (f, sym) for f in deffiles)
                        after = before + block
                        ledger.append(dict(layer="gateway", event_type="", file_path=deffiles[0],
                                           outcome="delivered", reason="",
                                           chars_delivered=len(block), iteration=it))
                else:
                    deffiles = _GRAPH.get(sym or "", [])
                    if len(deffiles) >= 2:  # ambiguous -> a partition would deliver
                        entity = set(deffiles) | {sym}
                        step_behind = entity <= (viewed | {sym})
                        ss_on = ss_env.get("GT_SS_NOVELTY") == "1"
                        # reference: suppress IFF the whole entity set was already seen.
                        # invert_suppression: suppress the NOVEL case instead (wrong) — still an
                        # EFFECT (so the gate detects "built") but violates the standard.
                        want_suppress = ((not step_behind) if "invert_suppression" in mut else step_behind)
                        do_suppress = ss_on and want_suppress
                        if do_suppress:
                            ledger.append(dict(layer="gateway", event_type="", file_path=deffiles[0],
                                               outcome="suppressed_hidden_only", reason="ss_step_behind",
                                               chars_delivered=0, iteration=it))
                        else:
                            block = ('\n<gt-search-facts symbol="%s">\n%s\n</gt-search-facts>'
                                     % (sym, "\n".join("def: " + f for f in deffiles)))
                            if "leak_test_token" in mut:
                                block += "\ntests/test_pkg.py:6: test_run"   # a model-facing leak
                            after = before + block
                            ledger.append(dict(layer="gateway", event_type="", file_path=deffiles[0],
                                               outcome="delivered", reason="",
                                               chars_delivered=len(block), iteration=it))
                            if "double_dose" in mut:               # a SECOND payload on one observation
                                after = after + block
                                ledger.append(dict(layer="l3.contract", event_type="", file_path=deffiles[1],
                                                   outcome="delivered", reason="",
                                                   chars_delivered=len(block), iteration=it))
            # fire_when_zero: emit a model byte when the flag is explicitly "0" (a broken
            # flag-gate that is NOT byte-identical vs the unset arm) -> S10 must bite it.
            if "fire_when_zero" in mut and ss_env.get("GT_SS_NOVELTY") == "0":
                after = after + "\nZZ"
            obs.append(G.Obs(before=before, after=after))

        # S11 SUBMIT-RED: exercise the submit boundary `submit_attempts` times AFTER the stream.
        # Reference: fire ONCE on an unresolved RED, silent afterwards; the flag gates it.
        # `refusal_loops` fires on EVERY attempt (violates single-dose); `fires_on_green` fires
        # even when the last touching test went green (violates the green-silent rule).
        refusals: list[str] = []
        ss_on = ss_env.get("GT_SS_SUBMIT_RED") == "1"
        fired = False
        for _ in range(max(0, int(submit_attempts))):
            should = ss_on and (last_failing is not None or "fires_on_green" in mut)
            if should and (not fired or "refusal_loops" in mut):
                cmdname = (last_failing or {}).get("cmd", "pytest")
                line = ("pre-commit hook failed:\npre-submit check: `%s` was last observed "
                        "FAILING and never re-run green\ncommit aborted (exit 1)" % cmdname)
                refusals.append(line)
                if not fired:
                    ledger.append(dict(layer="submit_gate", event_type="", file_path="",
                                       outcome="submit_blocked", reason="ss_submit_red",
                                       chars_delivered=len(line), iteration=999))
                fired = True
            else:
                if ss_on and fired and last_failing is not None:
                    ledger.append(dict(layer="submit_gate", event_type="", file_path="",
                                       outcome="submit_allow", reason="ss_submit_red",
                                       chars_delivered=0, iteration=999))
                refusals.append("")
        return G.SeamResult(observations=obs, ledger=ledger, submit_refusals=refusals)

    return behavior


def _driver(mutations=frozenset()):
    return G.FakeSeamDriver(_fake_seam(mutations))


# --------------------------------------------------------------------------- #
# 1) the reference SS seam makes the enforced scenarios PASS (the gate is not
#    vacuously failing / not vacuously passing — it recognises a CORRECT impl).
# --------------------------------------------------------------------------- #
def test_reference_seam_passes_enforced_scenarios():
    d = _driver()
    assert G.scenario_s1(d).verdict == G.PASS, G.scenario_s1(d).detail
    assert G.scenario_s2(d).verdict == G.PASS, G.scenario_s2(d).detail   # SS-1 SEMANTIC-DEDUP
    assert G.scenario_s6(d).verdict == G.PASS, G.scenario_s6(d).detail   # SS-1 LATE-DROP
    # S8 EMPTY-PAYLOAD: the fake makes the guard OBSERVABLE (it PASSes here) so the gate's
    # empty-payload CHECK is validated. NOTE the REAL mini seam byte-identically SKIPs S8 (an
    # empty delta bails before any delivered row in both arms — see scenario_s8's docstring).
    assert G.scenario_s8(d).verdict == G.PASS, G.scenario_s8(d).detail   # SS-1 ARBITER_V2
    assert G.scenario_s9(d).verdict == G.PASS, G.scenario_s9(d).detail
    assert G.scenario_s10(d).verdict == G.PASS, G.scenario_s10(d).detail
    assert G.scenario_s11(d).verdict == G.PASS, G.scenario_s11(d).detail   # SS-2 SUBMIT-RED


# --------------------------------------------------------------------------- #
# 2) MUTATIONS — each is a WRONG SS behaviour the gate MUST catch (turn RED).
# --------------------------------------------------------------------------- #
def test_mutation_invert_suppression_bites_s1():
    """A feature that suppresses the NOVEL delivery and DELIVERS the step-behind one
    (inverted) violates S1 — the gate must FAIL it, not pass or skip."""
    r = G.scenario_s1(_driver({"invert_suppression"}))
    assert r.verdict == G.FAIL, f"gate FAILED to bite inverted step-behind: {r.verdict} {r.detail}"


def test_mutation_dedup2_wrong_reason_bites_s2():
    """A caller_facts entity-set dedup that suppresses the byte-distinct semantic repeat but
    MISLABELS the drop (not ss_semantic_dup) is unauditable — the gate must FAIL it (the
    suppression is real, so it is 'built', but the ss_semantic_dup attribution is missing)."""
    r = G.scenario_s2(_driver({"dedup2_wrong_reason"}))
    assert r.verdict == G.FAIL, f"gate FAILED to bite an unauditable entity-set dedup: {r.verdict} {r.detail}"


def test_mutation_latedrop_wrong_reason_bites_s6():
    """A late-drop that drops the resurfaced already-green fact but MISLABELS the drop (not
    ss_late) is unauditable — the gate must FAIL it (the drop is real / 'built', but the
    ss_late attribution is missing)."""
    r = G.scenario_s6(_driver({"latedrop_wrong_reason"}))
    assert r.verdict == G.FAIL, f"gate FAILED to bite an unauditable late-drop: {r.verdict} {r.detail}"


def test_mutation_arbiter_detects_but_delivers_bites_s8():
    """An ARBITER_V2 empty-payload guard that detects the empty envelope (records the drop
    reason) but FAILS to suppress it — the chars=0 degenerate row still delivers — violates S8.
    The gate must FAIL it (a chars_delivered==0 delivered row is present)."""
    r = G.scenario_s8(_driver({"arbiter_detects_but_delivers"}))
    assert r.verdict == G.FAIL, f"gate FAILED to bite a chars=0 empty-payload delivery: {r.verdict} {r.detail}"


def test_mutation_leak_bites_s9():
    """A delivered payload carrying a test identifier must FAIL S9's leak invariant."""
    r = G.scenario_s9(_driver({"leak_test_token"}))
    assert r.verdict == G.FAIL, f"gate FAILED to bite a test-identifier leak: {r.verdict} {r.detail}"


def test_mutation_double_dose_bites_s9():
    """Two GT payloads on one observation must FAIL S9's <=1-dose invariant."""
    r = G.scenario_s9(_driver({"double_dose"}))
    assert r.verdict == G.FAIL, f"gate FAILED to bite a >1 dose: {r.verdict} {r.detail}"


def test_mutation_fire_when_zero_bites_s10():
    """A feature not byte-identical between explicit-0 and unset must FAIL S10."""
    r = G.scenario_s10(_driver({"fire_when_zero"}))
    assert r.verdict == G.FAIL, f"gate FAILED to bite a fire-when-zero regression: {r.verdict} {r.detail}"


def test_mutation_refusal_loops_bites_s11():
    """A submit refusal that fires on EVERY attempt (no single-dose latch) would loop a run
    to reward 0 — S11 must FAIL it (want exactly ONE refusal)."""
    r = G.scenario_s11(_driver({"refusal_loops"}))
    assert r.verdict == G.FAIL, f"gate FAILED to bite a looping submit refusal: {r.verdict} {r.detail}"


def test_mutation_fires_on_green_bites_s11():
    """A submit refusal that fires even after the last touching test went GREEN is a false
    block — S11 must FAIL it (green must stay silent)."""
    r = G.scenario_s11(_driver({"fires_on_green"}))
    assert r.verdict == G.FAIL, f"gate FAILED to bite a fire-on-green submit refusal: {r.verdict} {r.detail}"


# --------------------------------------------------------------------------- #
# 3) a NON-effecting flag is SKIP:flag-not-built, never a false PASS/FAIL — the
#    honest "feature not landed" verdict (the current real-seam state for S1-S8).
# --------------------------------------------------------------------------- #
def test_no_effect_flag_is_skip_not_pass():
    # a fake that ignores GT_SS_NOVELTY entirely -> on-arm == off-arm -> SKIP.
    def inert(events, ss_env):
        return G.SeamResult(observations=[G.Obs(ev.output or "", ev.output or "") for ev in events],
                            ledger=[])
    r = G.scenario_s1(G.FakeSeamDriver(inert))
    assert r.verdict == G.SKIP, f"a no-op flag must SKIP, got {r.verdict}"


# --------------------------------------------------------------------------- #
# 4) INTEGRATION smoke: the always-on invariants (S9 leak+dose, S10 byte-identity)
#    PASS on the REAL seam. Skips gracefully if the seam cannot be installed.
# --------------------------------------------------------------------------- #
def test_real_seam_invariants_hold():
    try:
        driver = G.RealSeamDriver()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"real seam not installable here: {exc}")
    assert G.scenario_s9(driver).verdict == G.PASS
    assert G.scenario_s10(driver).verdict == G.PASS
    # and every SS feature scenario is a clean SKIP:flag-not-built (nothing landed / nothing false)
    for fn in (G.scenario_s1, G.scenario_s2, G.scenario_s3, G.scenario_s4,
               G.scenario_s5, G.scenario_s6, G.scenario_s7, G.scenario_s8):
        assert fn(driver).verdict in (G.SKIP, G.PASS, G.FAIL)  # honest verdict, never ERROR


# --------------------------------------------------------------------------- #
# 5) HERMETICITY (SS-6b) — the channel canary must ERROR (loud), never SKIP (silent),
#    when the fixture-graph delivery channel is dead in the CORE arm. This is the exact
#    drift SS-6b closes: ambient state (a leaked GT_BASELINE, an ambient /tmp/gt-index, a
#    stale /tmp work-copy) silently killed the def/ref channel, and every flag-gated
#    scenario reported SKIP:flag-not-built — a dead channel masquerading as an unlanded SS
#    feature. The canary converts that silent degradation into a loud, named failure.
# --------------------------------------------------------------------------- #
def _inert_driver():
    """A seam double that produces ZERO deliveries for ANY input — a dead fixture-graph
    channel. scenario_s1 against it reports SKIP:flag-not-built (the silent-degradation the
    canary must catch)."""
    def inert(events, ss_env):
        return G.SeamResult(
            observations=[G.Obs(ev.output or "", ev.output or "") for ev in events],
            ledger=[])
    return G.FakeSeamDriver(inert)


def test_channel_canary_live_on_reference_seam():
    """The SS-reference seam delivers a def/ref partition on the bare `run` grep -> the
    hermeticity canary reports the CORE-arm channel LIVE."""
    ok, detail = G._channel_canary(_driver())
    assert ok is True, f"canary wrongly declared a LIVE reference channel dead: {detail}"


def test_channel_canary_dead_on_inert_seam_bites_the_silent_skip():
    """THE BITE: an inert (zero-delivery) seam makes scenario_s1 report SKIP:flag-not-built
    (indistinguishable from 'feature not landed'), but the canary detects the DEAD channel and
    returns False -> main() reports ERROR. Proves the canary catches EXACTLY the silent
    channel-death that the SKIP verdict would otherwise hide."""
    dead = _inert_driver()
    # Without the canary this looks like an honest 'not landed':
    assert G.scenario_s1(dead).verdict == G.SKIP
    # With the canary it is caught as a dead channel:
    ok, detail = G._channel_canary(dead)
    assert ok is False, "canary FAILED to bite a dead (zero-delivery) core-arm channel"
    assert "ZERO deliveries" in detail


def test_main_errors_not_skips_on_dead_channel(tmp_path, monkeypatch):
    """END-TO-END: when the channel is dead, `main` must exit 1 with an S0 CHANNEL-CANARY
    ERROR — NEVER a green (exit 0) run whose scenarios all SKIP. This is the regression guard
    for the SS-6b drift: a silently-dead channel can no longer pass the gate as 'nothing
    landed'."""
    monkeypatch.setattr(G, "RealSeamDriver", lambda: _inert_driver())
    out = tmp_path / "report.json"
    code = G.main(["--out", str(out)])
    assert code == 1, "gate returned GREEN on a dead fixture-graph channel"
    report = __import__("json").loads(out.read_text(encoding="utf-8"))
    s0 = report["scenarios"][0]
    assert s0["id"] == "S0" and s0["verdict"] == G.ERROR
    # and it did NOT emit a single false SKIP:flag-not-built in place of the ERROR
    assert all(s["verdict"] != G.SKIP for s in report["scenarios"])


def test_real_seam_hermetic_under_leaked_gt_baseline():
    """Hermeticity on the REAL seam: a leaked GT_BASELINE=1 in the ambient env (the historical
    poison that darkened S1/S2/S5/S6/S7/S8 to SKIP) must NOT change the gate's verdicts — the
    gate strips the whole GT_* namespace + forces the import-frozen _GT_BASELINE global per
    run, so the channel stays live and deterministic."""
    import os
    try:
        driver = G.RealSeamDriver()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"real seam not installable here: {exc}")
    prior = os.environ.get("GT_BASELINE")
    os.environ["GT_BASELINE"] = "1"  # simulate the leaked shell var
    try:
        ok, _ = G._channel_canary(driver)
        assert ok is True, "leaked GT_BASELINE=1 darkened the CORE-arm channel (non-hermetic)"
        v1 = G.scenario_s1(driver).verdict
        v2 = G.scenario_s1(driver).verdict
        assert v1 == v2 == G.PASS, f"S1 non-deterministic/darkened under GT_BASELINE leak: {v1},{v2}"
    finally:
        if prior is None:
            os.environ.pop("GT_BASELINE", None)
        else:
            os.environ["GT_BASELINE"] = prior
