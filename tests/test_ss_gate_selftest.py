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
import stat
import sys

import pytest

_SS_DIR = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "swebench"
if str(_SS_DIR) not in sys.path:
    sys.path.insert(0, str(_SS_DIR))

import ss_gate as G  # noqa: E402


def test_str_replace_materializer_changes_exactly_one_match_and_preserves_mode(tmp_path):
    target = tmp_path / "pkg" / "mod.py"
    target.parent.mkdir()
    target.write_text("before\nreturn 'a'\nafter\n", encoding="utf-8")
    target.chmod(0o640)
    original_mode = stat.S_IMODE(target.stat().st_mode)

    changed = G._materialize_str_replace(
        tmp_path,
        {"command": "str_replace", "path": "pkg/mod.py",
         "old_str": "return 'a'", "new_str": "return 'b'"},
    )

    assert changed == target.resolve()
    assert target.read_text(encoding="utf-8") == "before\nreturn 'b'\nafter\n"
    assert stat.S_IMODE(target.stat().st_mode) == original_mode


@pytest.mark.parametrize(
    "path",
    ["../escape.py", "pkg/../../escape.py", "/absolute.py", "C:/absolute.py"],
)
def test_str_replace_materializer_rejects_paths_outside_repo(tmp_path, path):
    with pytest.raises(G.GateDriverError):
        G._materialize_str_replace(
            tmp_path,
            {"command": "str_replace", "path": path,
             "old_str": "old", "new_str": "new"},
        )


def test_str_replace_materializer_rejects_missing_or_nonunique_old_bytes(tmp_path):
    target = tmp_path / "mod.py"
    action = {"command": "str_replace", "path": "mod.py",
              "old_str": "old", "new_str": "new"}
    target.write_text("no match", encoding="utf-8")
    with pytest.raises(G.GateDriverError, match="exactly once"):
        G._materialize_str_replace(tmp_path, action)
    target.write_text("old old", encoding="utf-8")
    with pytest.raises(G.GateDriverError, match="exactly once"):
        G._materialize_str_replace(tmp_path, action)


def test_drive_event_orders_preimage_then_materialization_then_augment(tmp_path):
    target = tmp_path / "mod.py"
    target.write_text("old", encoding="utf-8")
    calls = []

    class Seam:
        def _ss_capture_write_preimage(self, action):
            calls.append(("preimage", target.read_text(encoding="utf-8"), action["path"]))

        def _augment_output(self, action, out):
            calls.append(("augment", target.read_text(encoding="utf-8"), action["path"]))

    ev = G.Event(
        action={"command": "str_replace", "path": "mod.py",
                "old_str": "old", "new_str": "new"},
        rc=0,
    )
    G._drive_event(Seam(), tmp_path, ev)

    assert calls == [("preimage", "old", "mod.py"), ("augment", "new", "mod.py")]


def test_drive_event_captures_but_does_not_materialize_failed_write(tmp_path):
    target = tmp_path / "mod.py"
    target.write_text("old", encoding="utf-8")
    calls = []

    class Seam:
        def _ss_capture_write_preimage(self, _action):
            calls.append("preimage")

        def _augment_output(self, _action, _out):
            calls.append("augment")

    ev = G.Event(
        action={"command": "str_replace", "path": "mod.py",
                "old_str": "old", "new_str": "new"},
        rc=1,
    )
    G._drive_event(Seam(), tmp_path, ev)

    assert calls == ["preimage", "augment"]
    assert target.read_text(encoding="utf-8") == "old"


def test_drive_event_fails_closed_when_preimage_hook_is_missing(tmp_path):
    target = tmp_path / "mod.py"
    target.write_text("old", encoding="utf-8")

    class Seam:
        def _augment_output(self, _action, _out):
            raise AssertionError("must not augment without preimage capture")

    ev = G.Event(
        action={"command": "str_replace", "path": "mod.py",
                "old_str": "old", "new_str": "new"},
        rc=0,
    )
    with pytest.raises(G.GateDriverError, match="preimage"):
        G._drive_event(Seam(), tmp_path, ev)
    assert target.read_text(encoding="utf-8") == "old"


def test_runtime_ledger_reader_fails_closed_on_invalid_json(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text('{"valid": true}\nnot-json\n', encoding="utf-8")
    with pytest.raises(G.GateDriverError, match="ledger"):
        G._read_runtime_ledger(ledger)


def test_runtime_ledger_reader_fails_closed_when_file_is_missing(tmp_path):
    with pytest.raises(G.GateDriverError, match="missing"):
        G._read_runtime_ledger(tmp_path / "missing-ledger.jsonl")


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
                            ledger.append(dict(layer="gateway.def_ref_partition", event_type="", file_path=deffiles[0],
                                               outcome="suppressed_hidden_only", reason="ss_step_behind",
                                               chars_delivered=0, iteration=it))
                        else:
                            block = ('\n<gt-search-facts symbol="%s">\n%s\n</gt-search-facts>'
                                     % (sym, "\n".join("def: " + f for f in deffiles)))
                            if "leak_test_token" in mut:
                                block += "\ntests/test_pkg.py:6: test_run"   # a model-facing leak
                            after = before + block
                            ledger.append(dict(layer="gateway.def_ref_partition", event_type="", file_path=deffiles[0],
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


def test_s1_accepts_unrelated_fallback_after_target_fact_is_step_behind():
    """A correct novelty drop removes the target fact, not the whole observation dose.

    The global arbiter may select a different, still-novel fact after the step-behind
    def/ref partition is removed. S1 must join the suppression to its target identity
    instead of requiring the complete observation delta to be empty.
    """
    def behavior(events, ss_env, submit_attempts=0):
        del submit_attempts
        on = ss_env.get("GT_SS_NOVELTY") == "1"
        observations = [G.Obs(ev.output or "", ev.output or "") for ev in events]
        ledger = []
        is_suppressed_episode = len(events) == 3
        if is_suppressed_episode and on:
            fallback = "\npkg/util.py:4:sig_target"
            observations[-1] = G.Obs(
                events[-1].output or "", (events[-1].output or "") + fallback)
            ledger = [
                dict(layer="ga.def_ref_partition", file_path="pkg/mod_a.py",
                     outcome="suppressed_hidden_only", reason="ss_step_behind",
                     chars_delivered=0, iteration=3),
                dict(layer="gateway.localization", file_path="pkg/util.py",
                     outcome="delivered", reason="", chars_delivered=len(fallback),
                     iteration=3),
            ]
        else:
            target = "\npkg/mod_a.py:8:run\npkg/mod_b.py:9:run"
            observations[-1] = G.Obs(
                events[-1].output or "", (events[-1].output or "") + target)
            ledger = [
                dict(layer="gateway.def_ref_partition", file_path="pkg/mod_a.py",
                     outcome="delivered", reason="", chars_delivered=len(target),
                     iteration=len(events)),
            ]
        return G.SeamResult(observations=observations, ledger=ledger)

    result = G.scenario_s1(G.FakeSeamDriver(behavior))
    assert result.verdict == G.PASS, result.detail


def test_s1_rejects_step_behind_row_when_same_target_is_marked_delivered():
    """A suppression row is not proof when its exact target also has a delivery row."""
    def behavior(events, ss_env, submit_attempts=0):
        del submit_attempts
        observations = [G.Obs(ev.output or "", ev.output or "") for ev in events]
        target = "\npkg/mod_a.py:8:run\npkg/mod_b.py:9:run"
        is_suppressed_episode = len(events) == 3
        ledger = []
        if is_suppressed_episode and ss_env.get("GT_SS_NOVELTY") == "1":
            # Contradictory product evidence: the same fact is both suppressed and
            # delivered. Keep model bytes empty so a raw-delta-only gate would miss it.
            ledger = [
                dict(layer="ga.def_ref_partition", file_path="pkg/mod_a.py",
                     outcome="suppressed_hidden_only", reason="ss_step_behind",
                     chars_delivered=0, iteration=3),
                dict(layer="gateway.def_ref_partition", file_path="pkg/mod_a.py",
                     outcome="delivered", reason="", chars_delivered=len(target),
                     iteration=3),
            ]
        else:
            observations[-1] = G.Obs(
                events[-1].output or "", (events[-1].output or "") + target)
            ledger = [
                dict(layer="gateway.def_ref_partition", file_path="pkg/mod_a.py",
                     outcome="delivered", reason="", chars_delivered=len(target),
                     iteration=len(events)),
            ]
        return G.SeamResult(observations=observations, ledger=ledger)

    result = G.scenario_s1(G.FakeSeamDriver(behavior))
    assert result.verdict == G.FAIL, result.detail


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


def test_s6_accepts_unrelated_fallback_after_target_fact_is_late_dropped():
    """A correct late-drop removes the targeted fact, not the whole observation dose.

    Once Gateway fallbacks survive until global arbitration, an unrelated eligible fact may
    legitimately fill the one-dose slot. S6 must identify the late target by fact/file identity
    instead of requiring fewer total deliveries in the ON arm.
    """
    def behavior(events, ss_env, submit_attempts=0):
        del submit_attempts
        on = ss_env.get("GT_SS_LATE_DROP") == "1"
        second_before = events[-1].output or ""
        if on:
            fallback = "\npkg/other.py:4:other"
            ledger = [
                dict(layer="ga.def_ref_partition", file_path="pkg/db.py",
                     outcome="suppressed_hidden_only", reason="ss_late",
                     chars_delivered=0, iteration=2),
                dict(layer="gateway.localization", file_path="pkg/other.py",
                     outcome="delivered", reason="", chars_delivered=len(fallback),
                     iteration=2),
            ]
            second_after = second_before + fallback
        else:
            target = "\npkg/db.py:6:late_probe"
            ledger = [
                dict(layer="gateway.def_ref_partition", file_path="pkg/db.py",
                     outcome="delivered", reason="", chars_delivered=len(target),
                     iteration=2),
            ]
            second_after = second_before + target
        return G.SeamResult(
            observations=[G.Obs(events[0].output or "", events[0].output or ""),
                          G.Obs(second_before, second_after)],
            ledger=ledger,
        )

    result = G.scenario_s6(G.FakeSeamDriver(behavior))
    assert result.verdict == G.PASS, result.detail


def test_s6_rejects_late_row_when_the_same_target_still_delivers():
    """A host-side ``ss_late`` row is not proof if the target bytes still ship."""
    def behavior(events, ss_env, submit_attempts=0):
        del submit_attempts
        target = "\npkg/db.py:6:late_probe"
        second_before = events[-1].output or ""
        ledger = []
        if ss_env.get("GT_SS_LATE_DROP") == "1":
            ledger.append(
                dict(layer="ga.def_ref_partition", file_path="pkg/db.py",
                     outcome="suppressed_hidden_only", reason="ss_late",
                     chars_delivered=0, iteration=2)
            )
        ledger.append(
            dict(layer="gateway.def_ref_partition", file_path="pkg/db.py",
                 outcome="delivered", reason="", chars_delivered=len(target), iteration=2)
        )
        return G.SeamResult(
            observations=[G.Obs(events[0].output or "", events[0].output or ""),
                          G.Obs(second_before, second_before + target)],
            ledger=ledger,
        )

    result = G.scenario_s6(G.FakeSeamDriver(behavior))
    assert result.verdict == G.FAIL, result.detail


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


def test_s3_requires_exact_rendered_coherence_count_not_incidental_digit():
    def behavior(events, ss_env):
        before = [G.Obs(ev.output or "", ev.output or "") for ev in events]
        if ss_env.get("GT_SS_COHERENCE_V2") != "1" or len(events) != 3:
            return G.SeamResult(observations=before, ledger=[])
        delta = "\nGT: you have rewritten mod_a.py 13 times with no passing test between edits"
        before[-1] = G.Obs(events[-1].output or "", (events[-1].output or "") + delta)
        return G.SeamResult(
            observations=before,
            ledger=[{"outcome": "delivered", "reason": "ss_coherence",
                     "chars_delivered": len(delta), "iteration": 3}],
        )

    result = G.scenario_s3(G.FakeSeamDriver(behavior))
    assert result.verdict == G.FAIL
    assert "exact_count_3=False" in result.detail


@pytest.mark.parametrize(
    "row_patch",
    [
        {"file_path": "pkg/mod_b.py"},
        {"iteration": 2},
        {"layer": "detect.loop"},
        {"reason": "ss_other"},
    ],
)
def test_s3_requires_exact_witness_bound_to_matching_ledger_home(row_patch):
    def behavior(events, ss_env):
        observations = [G.Obs(ev.output or "", ev.output or "") for ev in events]
        if ss_env.get("GT_SS_COHERENCE_V2") != "1" or len(events) != 3:
            return G.SeamResult(observations=observations, ledger=[])
        delta = "\nGT: you have rewritten mod_a.py 3 times with no passing test between edits"
        observations[2] = G.Obs(events[2].output or "", (events[2].output or "") + delta)
        row = {"layer": "detect.coherence", "file_path": "pkg/mod_a.py",
               "outcome": "suppressed_internal_only", "reason": "ss_coherence",
               "chars_delivered": 0, "iteration": 3}
        row.update(row_patch)
        return G.SeamResult(observations=observations, ledger=[row])

    result = G.scenario_s3(G.FakeSeamDriver(behavior))
    assert result.verdict == G.FAIL
    assert "bound_row=False" in result.detail


def test_s3_accepts_exact_witness_and_matching_ledger_home():
    def behavior(events, ss_env):
        observations = [G.Obs(ev.output or "", ev.output or "") for ev in events]
        if ss_env.get("GT_SS_COHERENCE_V2") != "1" or len(events) != 3:
            return G.SeamResult(observations=observations, ledger=[])
        delta = "\nGT: you have rewritten mod_a.py 3 times with no passing test between edits"
        observations[2] = G.Obs(events[2].output or "", (events[2].output or "") + delta)
        return G.SeamResult(
            observations=observations,
            ledger=[{"layer": "detect.coherence", "file_path": "pkg/mod_a.py",
                     "outcome": "suppressed_internal_only", "reason": "ss_coherence",
                     "chars_delivered": 0, "iteration": 3}],
        )

    result = G.scenario_s3(G.FakeSeamDriver(behavior))
    assert result.verdict == G.PASS, result.detail


def test_s10_checks_byte_identity_independently_per_stream():
    calls = []

    def behavior(events, ss_env):
        calls.append((len(events), dict(ss_env)))
        return G.SeamResult(
            observations=[G.Obs(ev.output or "", ev.output or "") for ev in events],
            ledger=[],
        )

    result = G.scenario_s10(G.FakeSeamDriver(behavior))
    assert result.verdict == G.PASS
    assert len(calls) == 2 * len(G._all_streams())
    assert len(result.subchecks) == 2 * len(G._all_streams())
    assert all(name.startswith("stream#") for name, _ in result.subchecks)


def test_exit_code_requires_exact_s0_through_s11_shape_and_only_s8_skip():
    correct = [G.ScenarioResult(f"S{i}", f"n{i}", "f", G.PASS, "") for i in range(12)]
    correct[8].verdict = G.SKIP
    assert G._exit_code(correct) == 0

    wrong_skip = [G.ScenarioResult(r.sid, r.name, r.flag, r.verdict, r.detail) for r in correct]
    wrong_skip[7].verdict = G.SKIP
    assert G._exit_code(wrong_skip) == 1

    duplicate = [G.ScenarioResult(r.sid, r.name, r.flag, r.verdict, r.detail) for r in correct]
    duplicate[-1].sid = "S10"
    assert G._exit_code(duplicate) == 1

    missing = correct[:-1]
    assert G._exit_code(missing) == 1


def test_main_returns_nonzero_when_report_cannot_be_written(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace

    exact = [G.ScenarioResult(f"S{i}", f"n{i}", "f", G.PASS, "") for i in range(12)]
    exact[8].verdict = G.SKIP
    monkeypatch.setattr(G, "_oracle_lock_holder", lambda: None)
    monkeypatch.setattr(G, "RealSeamDriver", lambda: SimpleNamespace(name="fake"))
    monkeypatch.setattr(G, "_channel_canary", lambda _driver: (True, "live"))
    monkeypatch.setattr(G, "run_all", lambda _driver: exact[1:])

    def fail_write(self, *args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(pathlib.Path, "write_text", fail_write)
    assert G.main(["--out", str(tmp_path / "report.json")]) != 0
    captured = capsys.readouterr()
    assert "FATAL: could not write report" in captured.err
    assert "report NOT WRITTEN:" in captured.out


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


# --------------------------------------------------------------------------- #
# 6) ORACLE/GATE RUN-LOCK INTERLOCK (SS-10) — both programs junction-mirror the
#    same drive-global paths. The gate must fail before constructing its driver
#    when a live oracle owns the lock, while a dead holder is merely stale. The
#    gate is a reader only: it never creates, steals, or deletes the oracle lock.
# --------------------------------------------------------------------------- #
def test_oracle_lock_holder_distinguishes_live_from_stale(tmp_path, monkeypatch):
    import ss_replay_oracle as oracle

    lock = tmp_path / "ssr_replay_oracle.lock"
    lock.write_text("4242", encoding="utf-8")
    monkeypatch.setattr(G, "_ORACLE_RUN_LOCK", lock)

    monkeypatch.setattr(oracle, "_pid_holds_oracle", lambda pid: pid == 4242)
    assert G._oracle_lock_holder() == 4242
    assert lock.read_text(encoding="utf-8") == "4242"  # reader never mutates ownership

    monkeypatch.setattr(oracle, "_pid_holds_oracle", lambda _pid: False)
    assert G._oracle_lock_holder() is None
    assert lock.is_file(), "the gate must never delete the oracle-owned lock"


@pytest.mark.parametrize("contents", ["", "not-a-pid", "0", "-7"])
def test_oracle_lock_holder_fails_closed_on_invalid_existing_lock(tmp_path, monkeypatch, contents):
    lock = tmp_path / "ssr_replay_oracle.lock"
    lock.write_text(contents, encoding="utf-8")
    monkeypatch.setattr(G, "_ORACLE_RUN_LOCK", lock)

    with pytest.raises(G.OracleLockStateError):
        G._oracle_lock_holder()
    assert lock.read_text(encoding="utf-8") == contents


def test_oracle_lock_holder_fails_closed_when_lock_metadata_is_unreadable(monkeypatch):
    class UnreadableLock:
        def exists(self):
            raise OSError("access denied")

        def __str__(self):
            return "unreadable-oracle-lock"

    monkeypatch.setattr(G, "_ORACLE_RUN_LOCK", UnreadableLock())
    with pytest.raises(G.OracleLockStateError):
        G._oracle_lock_holder()


def test_pid_alive_matches_exact_tasklist_pid_not_substring(monkeypatch):
    import subprocess

    class Result:
        returncode = 0
        stdout = '"python.exe","1234","Console","1","10,000 K"\n'

    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: Result())
    assert G._pid_alive(123) is False
    assert G._pid_alive(1234) is True


def test_main_aborts_before_driver_when_lock_state_is_invalid(tmp_path, monkeypatch, capsys):
    lock = tmp_path / "ssr_replay_oracle.lock"
    lock.write_text("corrupt", encoding="utf-8")
    monkeypatch.setattr(G, "_ORACLE_RUN_LOCK", lock)

    def _must_not_construct():
        raise AssertionError("gate constructed RealSeamDriver with unknown lock ownership")

    monkeypatch.setattr(G, "RealSeamDriver", _must_not_construct)
    code = G.main(["--out", str(tmp_path / "must-not-exist.json")])
    err = capsys.readouterr().err

    assert code == 2
    assert "run-lock state is invalid" in err
    assert lock.read_text(encoding="utf-8") == "corrupt"


def test_main_aborts_before_driver_when_live_oracle_holds_lock(tmp_path, monkeypatch, capsys):
    import ss_replay_oracle as oracle

    lock = tmp_path / "ssr_replay_oracle.lock"
    lock.write_text("5151", encoding="utf-8")
    monkeypatch.setattr(G, "_ORACLE_RUN_LOCK", lock)
    # A live PID is not sufficient proof of ownership because PIDs are reused.
    # Exercise the oracle-identity authority consulted by _oracle_lock_holder.
    monkeypatch.setattr(oracle, "_pid_holds_oracle", lambda pid: pid == 5151)

    def _must_not_construct():
        raise AssertionError("gate constructed RealSeamDriver while oracle lock was live")

    monkeypatch.setattr(G, "RealSeamDriver", _must_not_construct)
    code = G.main(["--out", str(tmp_path / "must-not-exist.json")])
    err = capsys.readouterr().err

    assert code == 2
    assert "run-lock held by oracle run 5151" in err
    assert lock.read_text(encoding="utf-8") == "5151"
