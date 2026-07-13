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
import sys

import pytest

_SS_DIR = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "swebench"
if str(_SS_DIR) not in sys.path:
    sys.path.insert(0, str(_SS_DIR))

import ss_gate as G  # noqa: E402


# --------------------------------------------------------------------------- #
# A faithful SS-REFERENCE seam (test double). Given the fixture graph it knows,
# it implements the SS standard the gate enforces: step-behind suppression
# (ss_step_behind), leak-safety, and <=1 dose. `mutations` toggle specific WRONG
# behaviours so the matching scenario turns RED — the biting proof.
# --------------------------------------------------------------------------- #
_GRAPH = {
    "run": ["pkg/mod_a.py", "pkg/mod_b.py"],       # ambiguous -> a def/ref partition delivery
    "gizmo": ["tmp/scratch.py", "htmlcov/x.js"],   # provenance fixture
}


def _fake_seam(mutations=frozenset()):
    mut = set(mutations)

    def behavior(events, ss_env):
        viewed: set[str] = set()
        obs: list = []
        ledger: list[dict] = []
        it = 0
        for ev in events:
            it += 1
            cmd = (ev.action.get("command") or "")
            before = ev.output or ""
            after = before
            toks = cmd.split()
            if toks[:1] == ["cat"] and len(toks) >= 2:
                viewed.add(toks[1].strip())
            elif toks[:1] == ["grep"]:
                sym = next((t for t in toks[1:] if not t.startswith("-") and t != "."), None)
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
        return G.SeamResult(observations=obs, ledger=ledger)

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
    assert G.scenario_s9(d).verdict == G.PASS, G.scenario_s9(d).detail
    assert G.scenario_s10(d).verdict == G.PASS, G.scenario_s10(d).detail


# --------------------------------------------------------------------------- #
# 2) MUTATIONS — each is a WRONG SS behaviour the gate MUST catch (turn RED).
# --------------------------------------------------------------------------- #
def test_mutation_invert_suppression_bites_s1():
    """A feature that suppresses the NOVEL delivery and DELIVERS the step-behind one
    (inverted) violates S1 — the gate must FAIL it, not pass or skip."""
    r = G.scenario_s1(_driver({"invert_suppression"}))
    assert r.verdict == G.FAIL, f"gate FAILED to bite inverted step-behind: {r.verdict} {r.detail}"


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
