"""Stage-1: the CompletionCertificate — termination reasoning with per-field honesty.

W5. EXTENDS ``submit_gate`` WITHOUT touching the frozen Gate Kernel seam
(``GateVerdict`` / ``gate_verdict`` / ``safe_gate_verdict``). The certificate wraps
the head with a per-field PASS|FAIL|UNKNOWN|NOT_APPLICABLE account (provenance +
freshness) and derives ``decision`` from the head ALONE — never a second decision
path.

The suite pins, RED-first:
- the certificate exists and every field maps from its engine's result shape;
- ``decision`` derives from the head (allow / bounce_once), with the head-supplied
  path authoritative;
- FRESHNESS: a stale PASS is demoted to UNKNOWN; a stale FAIL does NOT block (pure
  re-run mirror); a fresh FAIL blocks;
- the T2 REGRESSION LAW: obligations are ADVISORY — all-unmet + everything clean =>
  allow, reason clean, and obligation_coverage never in unresolved_failures;
- the FAIL-OPEN LAW: UNKNOWN is visible but never blocks;
- the allow_encouragement advisory (fact-shaped, obligations excluded);
- ``to_dict`` is deterministic + wallclock-free;
- BACKWARD-COMPAT: the frozen gate_verdict/safe_gate_verdict head is byte-stable.

Two MUTATIONS are documented in ``submit_gate._derive_decision``:
  (M1) ``if unknowns: return "bounce_once"`` -> the fail-open test bites.
  (M2) ``and obligation.status != FAIL`` -> the T2-law test bites.
"""

from __future__ import annotations

import json

from groundtruth.runtime import submit_gate as sg
from groundtruth.runtime.submit_gate import (
    FAIL,
    NOT_APPLICABLE,
    PASS,
    UNKNOWN,
    CompletionCertificate,
    FieldCert,
    GateVerdict,
    build_certificate,
    safe_build_certificate,
)


# --------------------------------------------------------------------------- #
# BACKWARD-COMPAT — the frozen head is byte-stable (the seam must keep working)
# --------------------------------------------------------------------------- #
def test_frozen_head_signatures_unchanged():
    # gate_verdict fail law — identical to the pre-certificate pins.
    assert sg.gate_verdict(covering={"verdict": "pass"}).allow is True
    assert sg.gate_verdict(covering={"verdict": "unavailable"}).allow is True
    assert sg.gate_verdict(covering=None, hygiene=None).allow is True
    v = sg.gate_verdict(covering={"verdict": "fail", "failing_test_names": ["t_x"]})
    assert v.allow is False and v.reason == "covering_test_failed"
    assert "t_x" not in v.detail                       # generic, leak-safe
    assert v.record["covering_failing_names"] == ["t_x"]
    assert sg.gate_verdict(hygiene={"blocking": True}).allow is False
    over = sg.gate_verdict(covering={"verdict": "fail"}, bounce_count=1, max_bounces=1)
    assert over.allow is True and over.reason == "gate_overridden"

    class Boom:
        def get(self, *a, **k):
            raise RuntimeError("kaboom")
    crashed = sg.safe_gate_verdict(covering=Boom())
    assert crashed.allow is True and crashed.reason == "gate_crash"


# --------------------------------------------------------------------------- #
# decision derives from the head — ONE decision path
# --------------------------------------------------------------------------- #
def test_clean_run_allows_reason_clean():
    cert = build_certificate(covering={"verdict": "pass"},
                             hygiene={"blocking": False}, syntax={"verdict": "ok"})
    assert isinstance(cert, CompletionCertificate)
    assert cert.decision == "allow"
    assert cert.head_reason == "clean"
    assert cert.selected_test_status.status == PASS
    assert cert.syntax_status.status == PASS
    assert cert.unresolved_failures == ()


def test_fresh_covering_fail_bounces_once():
    cert = build_certificate(covering={"verdict": "fail"})
    assert cert.decision == "bounce_once"
    assert cert.head_reason == "covering_test_failed"
    assert cert.selected_test_status.status == FAIL
    assert "selected_test_status" in cert.unresolved_failures


def test_hygiene_block_bounces_once():
    cert = build_certificate(hygiene={"blocking": True, "reason": "binary_file_in_diff"})
    assert cert.decision == "bounce_once"
    assert cert.head_reason == "hygiene"


def test_fail_open_after_max_bounces_is_allow():
    cert = build_certificate(covering={"verdict": "fail"}, bounce_count=1, max_bounces=1)
    assert cert.decision == "allow"                    # gate_overridden -> allow
    assert cert.head_reason == "gate_overridden"


def test_supplied_head_is_authoritative():
    # The seam passes its EXACT verdict; certificate must not re-decide from covering.
    head = GateVerdict(allow=False, reason="hygiene", detail="x")
    cert = build_certificate(head=head, covering={"verdict": "pass"})
    assert cert.decision == "bounce_once"
    assert cert.head_reason == "hygiene"


# --------------------------------------------------------------------------- #
# per-field mapping + provenance
# --------------------------------------------------------------------------- #
def test_syntax_field_maps_all_verdicts():
    assert build_certificate(syntax={"verdict": "ok"}).syntax_status.status == PASS
    assert build_certificate(syntax={"verdict": "syntax_error"}).syntax_status.status == FAIL
    assert build_certificate(syntax={"verdict": "unavailable"}).syntax_status.status == UNKNOWN
    # absent -> UNKNOWN (the check applies to any source edit; we simply don't know).
    f = build_certificate(syntax=None).syntax_status
    assert f.status == UNKNOWN
    assert f.provenance == "edit_check.check_edit_syntax"


def test_selected_test_field_maps_covering_shape():
    assert build_certificate(covering={"verdict": "pass"}).selected_test_status.status == PASS
    assert build_certificate(covering={"verdict": "fail"}).selected_test_status.status == FAIL
    assert build_certificate(covering={"verdict": "unavailable"}).selected_test_status.status == UNKNOWN
    f = build_certificate(covering={"verdict": "pass"}).selected_test_status
    assert f.provenance == "covering_runner.run_covering_tests"


def test_build_and_type_default_not_applicable_without_plan():
    cert = build_certificate()
    assert cert.build_status.status == NOT_APPLICABLE
    assert cert.type_status.status == NOT_APPLICABLE
    assert cert.build_status.provenance == "verification_plan.build"


def test_build_and_type_map_from_plan_results():
    cert = build_certificate(plan_results={"build": {"status": "pass"},
                                            "type": {"status": "fail"}})
    assert cert.build_status.status == PASS
    assert cert.type_status.status == FAIL
    assert "type_status" in cert.unresolved_failures


def test_scope_field_from_governor_value():
    assert build_certificate(scope_compliance=True).scope_compliance.status == PASS
    assert build_certificate(scope_compliance=False).scope_compliance.status == FAIL
    assert build_certificate(scope_compliance=None).scope_compliance.status == UNKNOWN
    assert build_certificate(scope_compliance={"compliant": True}).scope_compliance.status == PASS
    assert build_certificate(scope_compliance={"status": "out_of_scope"}).scope_compliance.status == FAIL


def test_reproduction_defaults_not_applicable():
    assert build_certificate().reproduction_status.status == NOT_APPLICABLE
    assert build_certificate(reproduction={"verdict": "pass"}).reproduction_status.status == PASS


def test_patch_revision_and_diff_present():
    cert = build_certificate(submit_revision="rev123", numstat="3\t0\tfoo.py\n")
    assert cert.patch_revision == "rev123"
    assert cert.diff_present is True
    assert build_certificate(numstat="").diff_present is False
    assert build_certificate(diff_present=True).diff_present is True


# --------------------------------------------------------------------------- #
# FRESHNESS — stale PASS -> UNKNOWN; stale FAIL does NOT block; fresh FAIL blocks
# --------------------------------------------------------------------------- #
def test_stale_pass_demotes_to_unknown():
    cert = build_certificate(covering={"verdict": "pass", "patch_revision": "OLD"},
                             submit_revision="NEW")
    assert cert.selected_test_status.status == UNKNOWN          # stale green != green
    assert cert.selected_test_status.fresh is False
    assert {"field": "selected_test_status", "revision": "OLD"} in cert.stale_evidence
    assert cert.decision == "allow"


def test_stale_fail_does_not_block():
    # A stale FAIL is re-run territory; a pure function cannot re-run, so it is
    # demoted to UNKNOWN and dropped from the head -> allow (mirror the seam's
    # "re-run cached FAIL" semantic). MUTATION: pass the stale covering into the
    # head (skip the freshness drop) -> decision would flip to bounce_once -> bites.
    cert = build_certificate(covering={"verdict": "fail", "patch_revision": "OLD"},
                             submit_revision="NEW")
    assert cert.decision == "allow"
    assert cert.selected_test_status.status == UNKNOWN
    assert cert.selected_test_status.fresh is False
    assert "selected_test_status" in cert.unknowns
    assert "selected_test_status" not in cert.unresolved_failures


def test_fresh_fail_blocks_when_revisions_match():
    cert = build_certificate(covering={"verdict": "fail", "patch_revision": "SAME"},
                             submit_revision="SAME")
    assert cert.selected_test_status.status == FAIL
    assert cert.selected_test_status.fresh is True
    assert cert.decision == "bounce_once"


def test_unprovable_freshness_is_trusted_not_fabricated_stale():
    # No revision on the evidence and/or no submit_revision -> correct-or-quiet:
    # never fabricate staleness. A fresh-by-default PASS stays PASS.
    cert = build_certificate(covering={"verdict": "pass"}, submit_revision="NEW")
    assert cert.selected_test_status.status == PASS
    assert cert.selected_test_status.fresh is True
    assert cert.stale_evidence == ()


# --------------------------------------------------------------------------- #
# T2 REGRESSION LAW — obligation_coverage is ADVISORY ONLY (M2 mutation bites)
# --------------------------------------------------------------------------- #
def test_t2_law_obligations_all_unmet_still_allows():
    # obligations ALL unmet, everything else CLEAN -> allow, reason clean.
    # obligation_coverage is present + honest (FAIL) but never gates and never joins
    # unresolved_failures. M2 mutation (`and obligation.status != FAIL` in
    # _derive_decision) flips this decision to bounce_once -> this test bites.
    cert = build_certificate(
        covering={"verdict": "pass"},
        hygiene={"blocking": False},
        obligations={"total": 4, "met": 0, "unmet": ["a", "b", "c", "d"]},
    )
    assert cert.decision == "allow"
    assert cert.head_reason == "clean"
    assert cert.obligation_coverage.status == FAIL          # honest coverage
    assert cert.obligation_coverage.provenance == "obligations"
    assert "obligation_coverage" not in cert.unresolved_failures
    assert "obligation_coverage" not in cert.unknowns


def test_obligations_absent_is_not_applicable():
    assert build_certificate().obligation_coverage.status == NOT_APPLICABLE


def test_obligations_all_met_is_pass_advisory():
    cert = build_certificate(obligations={"total": 2, "met": 2, "unmet": []})
    assert cert.obligation_coverage.status == PASS
    assert "obligation_coverage" not in cert.unresolved_failures


# --------------------------------------------------------------------------- #
# FAIL-OPEN LAW — UNKNOWN is visible but never blocks (M1 mutation bites)
# --------------------------------------------------------------------------- #
def test_fail_open_all_unknown_allows():
    # NO evidence at all -> every applicable field UNKNOWN / NOT_APPLICABLE, unknowns
    # visible, decision allow. M1 mutation (`if unknowns: return "bounce_once"` in
    # _derive_decision) flips this to bounce_once -> this test bites.
    cert = build_certificate()
    assert cert.decision == "allow"
    assert cert.head_reason == "clean"
    assert cert.unknowns != ()                               # UNKNOWN is VISIBLE
    assert "selected_test_status" in cert.unknowns
    assert "syntax_status" in cert.unknowns


# --------------------------------------------------------------------------- #
# aggregation — unresolved / environment / unknowns
# --------------------------------------------------------------------------- #
def test_unresolved_failures_aggregates_excluding_obligations():
    cert = build_certificate(
        syntax={"verdict": "syntax_error"},
        covering={"verdict": "fail"},
        obligations={"total": 1, "met": 0, "unmet": ["x"]},
    )
    assert set(cert.unresolved_failures) == {"syntax_status", "selected_test_status"}
    assert "obligation_coverage" not in cert.unresolved_failures


def test_environment_failures_surfaced():
    cert = build_certificate(
        covering={"verdict": "unavailable", "environment_failure_class": "missing_dep"})
    assert {"field": "selected_test_status", "class": "missing_dep"} in cert.environment_failures
    assert cert.selected_test_status.status == UNKNOWN


# --------------------------------------------------------------------------- #
# allow_encouragement — advisory, fact-shaped, obligations excluded
# --------------------------------------------------------------------------- #
def test_allow_encouragement_fires_only_on_allow_over_threshold():
    # below threshold -> no encouragement.
    assert build_certificate(covering={"verdict": "pass"},
                             no_new_info_events=2,
                             no_new_info_threshold=5).allow_encouragement == ""
    # at/over threshold + allow -> a fact-shaped line (no imperative).
    enc = build_certificate(covering={"verdict": "pass"},
                            no_new_info_events=6,
                            no_new_info_threshold=5).allow_encouragement
    assert enc
    low = enc.lower()
    assert "no new" in low
    for directive in ("you should", "you must", "please ", "stop now", "submit now"):
        assert directive not in low                          # never a directive


def test_allow_encouragement_suppressed_on_bounce():
    enc = build_certificate(covering={"verdict": "fail"},
                            no_new_info_events=99,
                            no_new_info_threshold=1).allow_encouragement
    assert enc == ""                                         # not on a block


def test_allow_encouragement_ignores_obligations():
    # obligations state must NOT change the encouragement (T2 law: excluded input).
    common = dict(covering={"verdict": "pass"}, no_new_info_events=9,
                  no_new_info_threshold=5)
    met = build_certificate(obligations={"total": 3, "met": 3, "unmet": []}, **common)
    unmet = build_certificate(obligations={"total": 3, "met": 0, "unmet": [1, 2, 3]}, **common)
    assert met.allow_encouragement == unmet.allow_encouragement != ""


# --------------------------------------------------------------------------- #
# serialization — deterministic + wallclock-free
# --------------------------------------------------------------------------- #
def test_to_dict_is_deterministic_and_wallclock_free():
    kw = dict(covering={"verdict": "fail", "patch_revision": "OLD"},
              submit_revision="NEW", syntax={"verdict": "ok"},
              plan_results={"build": {"status": "pass"}},
              scope_compliance=True, obligations={"total": 1, "met": 0, "unmet": ["x"]},
              numstat="1\t0\tf.py\n")
    d1 = build_certificate(**kw).to_dict()
    d2 = build_certificate(**kw).to_dict()
    assert d1 == d2                                          # identical across builds
    blob = json.dumps(d1, sort_keys=True)
    assert json.loads(blob) == d1                            # round-trips

    # wallclock-free: no time-ish KEY anywhere in the envelope (the real discipline —
    # a raw substring scan false-positives on words like "unknown"/"candidate").
    def _keys(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                yield str(k)
                yield from _keys(v)
        elif isinstance(obj, list):
            for v in obj:
                yield from _keys(v)

    for key in _keys(d1):
        low = key.lower()
        for banned in ("timestamp", "wallclock", "elapsed", "epoch", "_time", "created_at"):
            assert banned not in low, f"time-ish key leaked: {key}"
    # field structure is present + typed.
    assert d1["decision"] in ("allow", "bounce_once")
    assert set(d1["syntax_status"]) == {"status", "provenance", "revision", "fresh", "detail"}


def test_field_cert_to_dict_shape():
    fc = FieldCert(PASS, "engine", revision="r1", fresh=True, detail="ok")
    assert fc.to_dict() == {"status": "PASS", "provenance": "engine",
                            "revision": "r1", "fresh": True, "detail": "ok"}
    # F6: nested envelopes are sorted too (matches the to_dict determinism claim).
    assert list(fc.to_dict()) == sorted(fc.to_dict())


# --------------------------------------------------------------------------- #
# F1 — empty/whitespace revisions must NEVER fabricate staleness
# --------------------------------------------------------------------------- #
def test_empty_submit_revision_never_fabricates_stale():
    # submit_revision="" (the realistic failed-`git rev-parse` shape) is UNKNOWN,
    # not a known revision: a FRESH covering FAIL must still BLOCK, never be
    # stale-demoted and silently discarded from the head.
    cert = build_certificate(covering={"verdict": "fail", "patch_revision": "OLD"},
                             submit_revision="")
    assert cert.decision == "bounce_once"
    assert cert.head_reason == "covering_test_failed"
    assert cert.selected_test_status.status == FAIL
    assert cert.selected_test_status.fresh is True
    assert cert.stale_evidence == ()


def test_whitespace_submit_revision_never_fabricates_stale():
    cert = build_certificate(covering={"verdict": "fail", "patch_revision": "OLD"},
                             submit_revision="   ")
    assert cert.decision == "bounce_once"
    assert cert.selected_test_status.status == FAIL
    assert cert.stale_evidence == ()


def test_whitespace_evidence_revision_is_unstamped_fresh():
    # A whitespace-only evidence revision is UNSTAMPED (strip before truthiness),
    # so a PASS with rev="   " vs submit_revision="NEW" is fresh, never stale.
    cert = build_certificate(covering={"verdict": "pass", "patch_revision": "   "},
                             submit_revision="NEW")
    assert cert.selected_test_status.status == PASS
    assert cert.selected_test_status.fresh is True
    assert cert.selected_test_status.revision is None
    assert cert.stale_evidence == ()


# --------------------------------------------------------------------------- #
# F2 — allow_encouragement only on a CLEAN allow (never a false fact)
# --------------------------------------------------------------------------- #
def test_encouragement_suppressed_on_gate_overridden_allow():
    # gate_overridden IS an allow, but a fresh FAIL sits in unresolved_failures —
    # "all gating checks are satisfied or unavailable" would be a FALSE fact.
    cert = build_certificate(covering={"verdict": "fail"},
                             bounce_count=1, max_bounces=1,
                             no_new_info_events=9, no_new_info_threshold=1)
    assert cert.decision == "allow"
    assert cert.head_reason == "gate_overridden"
    assert "selected_test_status" in cert.unresolved_failures
    assert cert.allow_encouragement == ""


def test_encouragement_suppressed_on_gate_crash_allow():
    class Boom:
        def get(self, *a, **k):
            raise RuntimeError("kaboom")
    head = sg.safe_gate_verdict(covering=Boom())
    assert head.reason == "gate_crash"
    cert = build_certificate(head=head, no_new_info_events=9, no_new_info_threshold=1)
    assert cert.decision == "allow"
    assert cert.allow_encouragement == ""


# --------------------------------------------------------------------------- #
# F3 — bulkhead: certificate construction can never brick a submit
# --------------------------------------------------------------------------- #
class _Poison(Exception):
    """Deliberately NOT a TypeError/ValueError — escapes narrow excepts."""


class _RaisingBool:
    def __bool__(self):
        raise _Poison("bool bomb")


class _PoisonDict(dict):
    def get(self, *a, **k):  # type: ignore[override]
        raise _Poison("poisoned get")


def test_numstat_non_string_does_not_crash():
    cert = build_certificate(numstat=5)  # type: ignore[arg-type]
    assert cert.diff_present is None     # unusable numstat -> unknown, not a crash


def test_obligations_raising_bool_is_unknown_not_crash():
    # a list item whose __bool__ raises a NON-(TypeError|ValueError) must degrade to
    # UNKNOWN (advisory field, correct-or-quiet), never crash the certificate.
    cert = build_certificate(obligations=[_RaisingBool()])
    assert cert.obligation_coverage.status == UNKNOWN
    assert cert.decision == "allow"


def test_safe_build_certificate_never_raises():
    # a NON-EMPTY dict-subclass covering whose .get raises escapes build_certificate
    # (at _evidence_revision) -> the bulkhead returns a MINIMAL honest certificate;
    # the head (safe_gate_verdict over the same poison) stays the single decision
    # path -> gate_crash allow. (Non-empty matters: an EMPTY poison dict is falsy,
    # `(covering or {})` swaps it out in the head, and the head is honestly "clean".)
    cert = safe_build_certificate(covering=_PoisonDict(verdict="fail"))
    assert isinstance(cert, CompletionCertificate)
    assert cert.decision == "allow"
    assert cert.head_reason == "gate_crash"
    assert "build_crash" in cert.head_record
    assert cert.unknowns != ()                    # UNKNOWN stays VISIBLE in the wreck
    d = cert.to_dict()
    assert json.loads(json.dumps(d, sort_keys=True)) == d


def test_safe_build_certificate_clean_path_matches_direct():
    kw = dict(covering={"verdict": "pass"}, syntax={"verdict": "ok"},
              obligations={"total": 1, "met": 1, "unmet": []})
    assert safe_build_certificate(**kw).to_dict() == build_certificate(**kw).to_dict()
