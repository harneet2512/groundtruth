"""The Gate Kernel — a pure, deterministic submit decision.

Plan §1/§3 (DELIVER gate). ``gate_verdict`` decides ALLOW | BLOCK from FACTS only
(an executed covering-test verdict, a hygiene predicate) — no LLM, no network, no
task IDs, no gold labels. The caller (the agent-class seam, B2) renders a BLOCK as
a native pre-commit refusal and lets the loop continue.

Fail law (correct-or-quiet; plan §3):
- An UNAVAILABLE signal (no covering tests, env hard-fail, hygiene not computed) is
  PASS-WITH-RECORD — never a false block.
- A kernel exception is ALLOW + ``gate_crash`` (``safe_gate_verdict`` — never brick a
  run; the Lane-A bulkhead lesson).
- BLOCK only on POSITIVE evidence: a real covering-test failure or a hygiene refusal.
- Never deadlock a run at reward 0: after ``max_bounces`` refusals, fail OPEN and
  record ``gate_overridden``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class GateVerdict:
    allow: bool
    reason: str  # machine reason code
    detail: str = ""  # human/native detail (feeds render_submit_rejection)
    record: dict[str, Any] = field(default_factory=dict)


def gate_verdict(
    *,
    covering: dict[str, Any] | None = None,
    hygiene: dict[str, Any] | None = None,
    submit_block: dict[str, Any] | None = None,
    bounce_count: int = 0,
    max_bounces: int = 1,
) -> GateVerdict:
    """Decide whether a submit may proceed.

    ``covering``: a ``covering_runner.run_covering_tests`` result (or None). Only a
        ``verdict == "fail"`` is a block; ``pass``/``unavailable``/None never block.
    ``hygiene``: a hygiene-predicate result (or None). ``{"blocking": True, ...}``
        blocks; anything else does not. (Wired in B2; None in B1.)
    ``submit_block``: a positive, already-observed submit-boundary failure
        (for example an unresolved test RED the agent itself observed). This is
        the same decision head, not a second post-certificate gate.
    ``bounce_count``: how many times THIS run has already been refused.
    ``max_bounces``: refusals allowed before failing open (default 1).
    """
    cov_verdict = (covering or {}).get("verdict")
    cov_fail = cov_verdict == "fail"
    hyg_block = bool((hygiene or {}).get("blocking"))
    submit_blocking = bool((submit_block or {}).get("blocking"))

    record: dict[str, Any] = {
        "covering_verdict": cov_verdict,
        "covering_reason": (covering or {}).get("reason"),
        "hygiene_blocking": hyg_block,
        "hygiene_reason": (hygiene or {}).get("reason"),
        "bounce_count": bounce_count,
        "max_bounces": max_bounces,
    }
    if submit_block is not None:
        record["submit_blocking"] = submit_blocking
        record["submit_block_reason"] = (submit_block or {}).get("reason")

    if not cov_fail and not hyg_block and not submit_blocking:
        # No positive evidence to block on (pass / unavailable / not-computed).
        return GateVerdict(allow=True, reason="clean", record=record)

    if bounce_count >= max_bounces:
        # Fail OPEN — a gate must never deadlock a run at reward 0.
        record["overridden"] = True
        return GateVerdict(allow=True, reason="gate_overridden", record=record)

    # Positive evidence + refusals remaining -> BLOCK. Verification-gap (the #1
    # failure class) is reported first when both fire.
    if cov_fail:
        # (Fable F7b: the gate's synthetic rejection is a MODEL-FACING surface — keep
        # test NAMES out of it. The raw runner transcript, an agent-discoverable
        # executed surface, may carry names; this refusal stays generic. Names are
        # retained in `record` for HOST telemetry only.)
        record["block"] = "covering_test_failed"
        record["covering_failing_names"] = (covering or {}).get("failing_test_names") or []
        return GateVerdict(
            allow=False,
            reason="covering_test_failed",
            detail="a covering test is failing — re-run the repo's own tests and fix before submitting",
            record=record,
        )

    if hyg_block:
        record["block"] = "hygiene"
        return GateVerdict(
            allow=False,
            reason="hygiene",
            detail=str(
                (hygiene or {}).get("detail")
                or (hygiene or {}).get("reason")
                or "hygiene check failed"
            ),
            record=record,
        )

    reason = str((submit_block or {}).get("reason") or "submit_observed_failure")
    record["block"] = reason
    return GateVerdict(
        allow=False,
        reason=reason,
        detail=str((submit_block or {}).get("detail") or "an observed failure remains unresolved"),
        record=record,
    )


def safe_gate_verdict(**kwargs: Any) -> GateVerdict:
    """``gate_verdict`` that can never raise — a kernel crash fails OPEN (ALLOW).

    A bug in the gate must not brick a run; it degrades to GT-off for that submit
    and records ``gate_crash`` for the host to see.
    """
    try:
        return gate_verdict(**kwargs)
    except Exception as exc:  # noqa: BLE001 -- never brick the loop
        return GateVerdict(
            allow=True,
            reason="gate_crash",
            record={"error": type(exc).__name__, "message": str(exc)[:200]},
        )


# ===========================================================================
# CompletionCertificate — the termination-reasoning envelope (W5).
#
# EXTENDS the Gate Kernel WITHOUT touching it. ``GateVerdict`` / ``gate_verdict`` /
# ``safe_gate_verdict`` remain the FROZEN decision head consumed by the mini submit
# seam (gt_mini_patch :8250-8380). This certificate wraps that head with a per-field,
# honest account of WHY a run may terminate — every SDLC signal as a
# PASS|FAIL|UNKNOWN|NOT_APPLICABLE fact with provenance + freshness. The certificate
# NEVER opens a second decision path: ``decision`` is a PURE function of the head.
#
# CONTROL LAWS (frozen upstream; mirrored honestly here):
#   - BLOCK only on a measured, attributable, FRESH covering FAIL / hygiene block.
#     The head already enforces this — the certificate does NOT re-decide.
#   - UNKNOWN is VISIBLE (listed in ``unknowns``) and FAILS OPEN (never a block input).
#   - Unavailable never blocks.
#   - FRESHNESS applies to EVIDENCE-bearing fields (covering/selected_test, scope,
#     plan results): evidence whose stamped revision != the submit-time revision is
#     STALE. A stale PASS is demoted to UNKNOWN (a stale green is not a green). A stale
#     FAIL is re-run territory — a PURE function cannot re-run, so it too is demoted to
#     UNKNOWN and the stale covering is DROPPED from the head (the seam already re-runs
#     cached FAILs and passes a fresh result / its own head; this is the pure-function
#     mirror). HYGIENE is NOT freshness-dropped: the seam recomputes `git diff
#     --numstat` at submit time, so hygiene is fresh by construction, not by stamp.
#     Staleness that cannot be PROVEN (either revision unknown, empty, or whitespace)
#     is treated as fresh — correct-or-quiet never fabricates a stale.
#   - T2 REGRESSION LAW (measured 4/4, 2026-07-08): ``obligation_coverage`` is ADVISORY
#     ONLY. It may NEVER contribute to a block or to an allow-encouragement threshold.
# ===========================================================================

PASS = "PASS"
FAIL = "FAIL"
UNKNOWN = "UNKNOWN"
NOT_APPLICABLE = "NOT_APPLICABLE"

# Advisory FAILURE-TO-STOP threshold (deliverable 2). Conservative default: the
# encouragement is emitted only after this many CONSECUTIVE no-new-information events.
# Caller-tunable and ADVISORY ONLY — it gates NOTHING (never a decision input), so it
# is a rendering knob, not a measured behavioral threshold.
_DEFAULT_NO_NEW_INFO_THRESHOLD = 5

# Where a piece of evidence may stamp its own revision (graph or patch). First hit wins.
_REVISION_KEYS = ("patch_revision", "graph_revision", "revision", "graph_rev", "rev")

# verdict/status token -> certificate status, per source engine's own vocabulary.
_COVERING_MAP = {"pass": PASS, "fail": FAIL, "unavailable": UNKNOWN}
_SYNTAX_MAP = {"ok": PASS, "syntax_error": FAIL, "unavailable": UNKNOWN}
_PLAN_MAP = {
    "pass": PASS,
    "passed": PASS,
    "ok": PASS,
    "success": PASS,
    "green": PASS,
    "fail": FAIL,
    "failed": FAIL,
    "error": FAIL,
    "red": FAIL,
    "unavailable": UNKNOWN,
    "unknown": UNKNOWN,
    "skipped": UNKNOWN,
    "": UNKNOWN,
}
_SCOPE_MAP = {
    "pass": PASS,
    "compliant": PASS,
    "ok": PASS,
    "in_scope": PASS,
    "fail": FAIL,
    "violation": FAIL,
    "out_of_scope": FAIL,
    "noncompliant": FAIL,
    "unknown": UNKNOWN,
    "unavailable": UNKNOWN,
    "": UNKNOWN,
}


@dataclass(frozen=True)
class FieldCert:
    """One certificate field: an honest per-signal verdict + its provenance.

    ``status``     one of PASS | FAIL | UNKNOWN | NOT_APPLICABLE.
    ``provenance`` which engine produced the evidence (e.g. covering_runner).
    ``revision``   the evidence's own revision (graph/patch), or None if unstamped.
    ``fresh``      the evidence revision matches the submit-time revision (True when
                   staleness is UNPROVABLE — correct-or-quiet never fabricates stale).
    ``detail``     short host-side reason (never model-facing on this surface).
    """

    status: str
    provenance: str
    revision: str | None = None
    fresh: bool = True
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        # Sorted keys — the whole envelope (nested included) is emission-ordered
        # deterministically (matches CompletionCertificate.to_dict's claim).
        return {
            "detail": self.detail,
            "fresh": self.fresh,
            "provenance": self.provenance,
            "revision": self.revision,
            "status": self.status,
        }


def _evidence_revision(ev: Any) -> str | None:
    """The evidence dict's own stamped revision, or None (unstamped/not a dict).

    Empty and WHITESPACE-ONLY values are unstamped (strip before truthiness) — a
    blank stamp is the absence of a revision, never a comparable one (F1)."""
    if not isinstance(ev, dict):
        return None
    for k in _REVISION_KEYS:
        v = ev.get(k)
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    return None


def _is_fresh(ev: Any, submit_revision: str | None) -> bool:
    """True UNLESS both revisions are KNOWN and differ. Unprovable staleness -> fresh
    (correct-or-quiet: never fabricate a stale to demote real evidence).

    KNOWN means non-empty after strip on BOTH sides (F1): a failed `git rev-parse`
    hands the seam ``""`` — that is an UNKNOWN submit revision, and treating it as
    known would stale-demote (and silently discard from the head) a REAL fresh RED."""
    ev_rev = _evidence_revision(ev)
    if submit_revision is None or not str(submit_revision).strip() or ev_rev is None:
        return True
    return str(ev_rev) == str(submit_revision).strip()


def _env_failure_class(result: Any) -> str | None:
    """An environment-failure class carried by an engine result, or None."""
    if not isinstance(result, dict):
        return None
    cls = result.get("environment_failure_class")
    if cls:
        return str(cls)
    if str(result.get("reason") or "").strip().lower() == "environment_failure":
        return "environment_failure"
    return None


def _result_field(
    result: Any,
    vmap: dict[str, str],
    provenance: str,
    absent: str,
    submit_revision: str | None,
) -> tuple[FieldCert, bool, str | None]:
    """Map a ``{"verdict"|"status": ...}`` engine result to ``(FieldCert, stale, env)``.

    ``absent`` is the status used when ``result is None`` (UNKNOWN for a check that
    applies but did not run; NOT_APPLICABLE for an engine not in this pipeline yet).
    A stale PASS/FAIL is demoted to UNKNOWN (freshness law).
    """
    if result is None:
        return FieldCert(absent, provenance, None, True, "no_evidence"), False, None
    if not isinstance(result, dict):
        return FieldCert(UNKNOWN, provenance, None, True, "malformed_evidence"), False, None
    raw = str(result.get("verdict") or result.get("status") or "").strip().lower()
    status = vmap.get(raw, UNKNOWN)
    rev = _evidence_revision(result)
    fresh = _is_fresh(result, submit_revision)
    stale = (not fresh) and status in (PASS, FAIL)
    if stale:
        status = UNKNOWN  # a stale PASS is not a green; a stale FAIL is re-run territory
    return (
        FieldCert(status, provenance, rev, fresh, str(result.get("reason") or "")),
        stale,
        _env_failure_class(result),
    )


def _scope_field(value: Any, submit_revision: str | None) -> tuple[FieldCert, bool]:
    """Map the governor-supplied scope-compliance VALUE (bool / dict / str / None).

    The governor COMPUTES scope compliance; the certificate merely takes its value —
    it never runs the governor. Absent -> UNKNOWN (visible, fails open)."""
    prov = "governor.scope_compliance"
    if value is None:
        return FieldCert(UNKNOWN, prov, None, True, "no_value"), False
    if isinstance(value, bool):
        return FieldCert(PASS if value else FAIL, prov, None, True, ""), False
    if isinstance(value, dict):
        if "compliant" in value:
            status = PASS if value.get("compliant") else FAIL
        else:
            raw = str(value.get("status") or value.get("verdict") or "").strip().lower()
            status = _SCOPE_MAP.get(raw, UNKNOWN)
        rev = _evidence_revision(value)
        fresh = _is_fresh(value, submit_revision)
        stale = (not fresh) and status in (PASS, FAIL)
        if stale:
            status = UNKNOWN
        return FieldCert(status, prov, rev, fresh, str(value.get("reason") or "")), stale
    if isinstance(value, str):
        return FieldCert(
            _SCOPE_MAP.get(value.strip().lower(), UNKNOWN), prov, None, True, ""
        ), False
    return FieldCert(UNKNOWN, prov, None, True, "malformed_value"), False


def _obligation_field(obligations: Any) -> FieldCert:
    """ADVISORY-ONLY coverage of the run's obligations.

    T2 REGRESSION LAW (measured 4/4, 2026-07-08): an obligations COMPLETION CHECKLIST
    used as a DONE-CRITERION causes early submits. This field is therefore NEVER an
    input to ``decision``, to ``unresolved_failures``, to ``unknowns``, or to
    ``allow_encouragement`` — see ``build_certificate`` (obligations are excluded from
    every block-bearing aggregation) and ``_derive_decision`` (unread). It exists ONLY
    so a human reading the certificate can see coverage. It carries no revision
    (obligations are intent, not executed evidence) and gates nothing.
    """
    prov = "obligations"
    if obligations is None:
        return FieldCert(NOT_APPLICABLE, prov, None, True, "no_obligations")
    try:
        if isinstance(obligations, dict):
            unmet = obligations.get("unmet")
            if isinstance(unmet, (list, tuple, set)):
                total = obligations.get("total")
                return FieldCert(
                    PASS if not unmet else FAIL,
                    prov,
                    None,
                    True,
                    f"{len(unmet)} unmet" + (f"/{total}" if total is not None else ""),
                )
            total = obligations.get("total")
            met = obligations.get("met", obligations.get("covered"))
            if total is not None and met is not None:
                total_i, met_i = int(total), int(met)
                if total_i <= 0:
                    return FieldCert(UNKNOWN, prov, None, True, "no_obligations")
                return FieldCert(
                    PASS if met_i >= total_i else FAIL, prov, None, True, f"{met_i}/{total_i} met"
                )
            return FieldCert(UNKNOWN, prov, None, True, "unparsed_obligations")
        if isinstance(obligations, (list, tuple, set)):
            items = list(obligations)
            if not items:
                return FieldCert(UNKNOWN, prov, None, True, "no_obligations")

            def _met(x: Any) -> bool:
                if isinstance(x, dict):
                    return bool(x.get("met") or x.get("satisfied") or x.get("covered"))
                return bool(x)

            unmet_n = sum(0 if _met(x) else 1 for x in items)
            return FieldCert(
                PASS if unmet_n == 0 else FAIL, prov, None, True, f"{unmet_n} unmet/{len(items)}"
            )
    except Exception:  # noqa: BLE001 — ADVISORY field: ANY malformed/poisoned
        # obligations value (incl. a raising __bool__, F3) degrades to UNKNOWN;
        # it must never crash the certificate (and could never gate anyway — T2 law).
        return FieldCert(UNKNOWN, prov, None, True, "unparsed_obligations")
    return FieldCert(UNKNOWN, prov, None, True, "unparsed_obligations")


def _derive_decision(head: GateVerdict, obligation: FieldCert, unknowns: tuple[str, ...]) -> str:
    """THE ONLY decision path — a PURE function of the Gate Kernel head.

    Maps the head's ALLOW/BLOCK to ``allow`` | ``bounce_once``. The head already
    encodes every control law (block only on a fresh attributable FAIL / hygiene block;
    fail OPEN after ``max_bounces`` -> ``gate_overridden``; crash -> allow), so the
    certificate NEVER re-decides.

    ``obligation`` and ``unknowns`` are passed for LOCALITY of the two invariants they
    anchor, but are DELIBERATELY UNREAD:
      - T2 REGRESSION LAW: obligations may not gate a submit.
        (M2 mutation: ``and obligation.status != FAIL`` here -> the T2-law test bites.)
      - FAIL-OPEN LAW: UNKNOWN is visible but never blocks.
        (M1 mutation: ``if unknowns: return "bounce_once"`` here -> fail-open test bites.)
    """
    return "allow" if head.allow else "bounce_once"


def _allow_encouragement(
    decision: str, head_reason: str, no_new_info_events: Any, threshold: Any
) -> str:
    """FAILURE-TO-STOP advisory (deliverable 2): a FACT-shaped line, never a directive.

    Fires ONLY on a CLEAN allow (``head_reason == "clean"``) AND when the loop has
    produced no new information for ``threshold`` consecutive events. A non-clean
    allow — ``gate_overridden`` (a fresh FAIL failed OPEN past the bounce cap) or
    ``gate_crash`` — must NOT claim "all gating checks are satisfied or unavailable":
    that would be a FALSE fact (F2). The gate stays head-derived (single decision
    path preserved — this reads the head's reason, it never re-decides). Inputs are
    (decision, head_reason, counter, threshold) — OBLIGATIONS ARE EXCLUDED (T2 law).
    Returns "" when it should not fire.
    """
    if decision != "allow" or head_reason != "clean":
        return ""
    try:
        n = int(no_new_info_events)
        t = int(threshold)
    except (TypeError, ValueError):
        return ""
    if t <= 0 or n < t:
        return ""
    return (
        f"{n} consecutive actions produced no new verification information; "
        "all gating checks are satisfied or unavailable."
    )


@dataclass(frozen=True)
class CompletionCertificate:
    """Termination reasoning: the head's decision + a per-field honest account.

    ``decision`` is derived from the Gate Kernel head ALONE (``allow`` | ``bounce_once``).
    The status fields (``*_status`` / ``scope_compliance`` / ``obligation_coverage``)
    are DESCRIPTIVE FieldCerts — they explain the run's state; they do not re-decide.
    ``obligation_coverage`` is ADVISORY (T2 law) and is excluded from every
    block-bearing aggregation.
    """

    decision: str
    head_reason: str
    patch_revision: str | None
    diff_present: bool | None
    scope_compliance: FieldCert
    syntax_status: FieldCert
    type_status: FieldCert
    build_status: FieldCert
    selected_test_status: FieldCert
    reproduction_status: FieldCert
    obligation_coverage: FieldCert
    unresolved_failures: tuple[str, ...] = ()
    environment_failures: tuple[dict[str, Any], ...] = ()
    stale_evidence: tuple[dict[str, Any], ...] = ()
    unknowns: tuple[str, ...] = ()
    allow_encouragement: str = ""
    head_record: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Deterministic, wallclock-free serialization (envelope discipline). Keys are
        emitted in sorted order; nested list entries are sorted; no clock is read."""
        return {
            "allow_encouragement": self.allow_encouragement,
            "build_status": self.build_status.to_dict(),
            "decision": self.decision,
            "diff_present": self.diff_present,
            "environment_failures": [dict(sorted(e.items())) for e in self.environment_failures],
            "head_reason": self.head_reason,
            "head_record": dict(sorted(self.head_record.items())),
            "obligation_coverage": self.obligation_coverage.to_dict(),
            "patch_revision": self.patch_revision,
            "reproduction_status": self.reproduction_status.to_dict(),
            "scope_compliance": self.scope_compliance.to_dict(),
            "selected_test_status": self.selected_test_status.to_dict(),
            "stale_evidence": [dict(sorted(e.items())) for e in self.stale_evidence],
            "syntax_status": self.syntax_status.to_dict(),
            "type_status": self.type_status.to_dict(),
            "unknowns": list(self.unknowns),
            "unresolved_failures": list(self.unresolved_failures),
        }


def build_certificate(
    *,
    covering: dict[str, Any] | None = None,
    hygiene: dict[str, Any] | None = None,
    submit_block: dict[str, Any] | None = None,
    bounce_count: int = 0,
    max_bounces: int = 1,
    head: GateVerdict | None = None,
    submit_revision: str | None = None,
    syntax: dict[str, Any] | None = None,
    plan_results: dict[str, Any] | None = None,
    scope_compliance: Any = None,
    reproduction: dict[str, Any] | None = None,
    obligations: Any = None,
    diff_present: bool | None = None,
    numstat: str | None = None,
    no_new_info_events: int = 0,
    no_new_info_threshold: int = _DEFAULT_NO_NEW_INFO_THRESHOLD,
) -> CompletionCertificate:
    """Build the termination-reasoning certificate around the Gate Kernel head.

    PURE + deterministic: no I/O, no clock, no globals. Every argument is evidence a
    caller already computed with the frozen engines — this function CONSUMES shapes, it
    never re-runs an engine. Field sources:
      - ``syntax``       <- edit_check.check_edit_syntax result.
      - ``covering``     <- covering_runner.run_covering_tests result (selected_test).
      - ``plan_results`` <- W3 VerificationPlan results ``{"build": ..., "type": ...}``
                           when present; absent -> build/type NOT_APPLICABLE.
      - ``scope_compliance`` <- the governor's computed value (bool/dict/str).
      - ``hygiene``      <- patch_auditor.diff_hygiene record (feeds the head).
      - ``submit_block`` <- positive submit-boundary failure (feeds the same head).
      - ``reproduction`` <- an optional reproduction runner result; absent -> N/A.
      - ``obligations``  <- ADVISORY coverage only (T2 law).

    ``decision`` is a pure function of the head: ``head`` when the seam supplies its own
    verdict (authoritative), else ``safe_gate_verdict`` over the SAME covering/hygiene/
    bounce args the seam passes. A STALE covering (revision != ``submit_revision``) is
    dropped from the head computation so a submit is NEVER blocked on stale evidence —
    the seam re-runs cached FAILs upstream; this mirrors that for the pure path.
    """
    # --- head: the ONE decision --------------------------------------------
    if head is None:
        cov_for_head = covering if _is_fresh(covering, submit_revision) else None
        head = safe_gate_verdict(
            covering=cov_for_head,
            hygiene=hygiene,
            submit_block=submit_block,
            bounce_count=bounce_count,
            max_bounces=max_bounces,
        )

    stale_list: list[dict[str, Any]] = []
    env_list: list[dict[str, Any]] = []

    def _rf(
        name: str, result: Any, vmap: dict[str, str], provenance: str, absent: str
    ) -> FieldCert:
        fc, stale, env = _result_field(result, vmap, provenance, absent, submit_revision)
        if stale:
            stale_list.append({"field": name, "revision": fc.revision})
        if env:
            env_list.append({"field": name, "class": env})
        return fc

    # --- fields (descriptive; never re-decide) -----------------------------
    syntax_f = _rf("syntax_status", syntax, _SYNTAX_MAP, "edit_check.check_edit_syntax", UNKNOWN)
    seltest_f = _rf(
        "selected_test_status",
        covering,
        _COVERING_MAP,
        "covering_runner.run_covering_tests",
        UNKNOWN,
    )
    repro_f = _rf(
        "reproduction_status", reproduction, _COVERING_MAP, "reproduction_runner", NOT_APPLICABLE
    )
    _plan = plan_results if isinstance(plan_results, dict) else {}
    build_f = _rf(
        "build_status", _plan.get("build"), _PLAN_MAP, "verification_plan.build", NOT_APPLICABLE
    )
    type_f = _rf(
        "type_status", _plan.get("type"), _PLAN_MAP, "verification_plan.type", NOT_APPLICABLE
    )

    scope_f, scope_stale = _scope_field(scope_compliance, submit_revision)
    if scope_stale:
        stale_list.append({"field": "scope_compliance", "revision": scope_f.revision})

    obligation_f = _obligation_field(obligations)

    # --- aggregation (obligation_coverage EXCLUDED — T2 law) ---------------
    # The six SDLC fields are the ACCOUNTABLE set: they feed the DESCRIPTIVE
    # unresolved_failures / unknowns rollups only — they never block (the head alone
    # decides). obligation_coverage is never here (adding it is exactly the
    # forbidden T2 done-criterion).
    sdlc_fields = {
        "scope_compliance": scope_f,
        "syntax_status": syntax_f,
        "type_status": type_f,
        "build_status": build_f,
        "selected_test_status": seltest_f,
        "reproduction_status": repro_f,
    }
    unresolved = tuple(sorted(n for n, f in sdlc_fields.items() if f.status == FAIL))
    unknowns = tuple(sorted(n for n, f in sdlc_fields.items() if f.status == UNKNOWN))
    environment_failures = tuple(sorted(env_list, key=lambda d: (d["field"], d["class"])))
    stale_evidence = tuple(sorted(stale_list, key=lambda d: d["field"]))

    # --- decision (head only) + advisory encouragement ---------------------
    decision = _derive_decision(head, obligation_f, unknowns)
    encouragement = _allow_encouragement(
        decision, head.reason, no_new_info_events, no_new_info_threshold
    )

    # F3: a non-str numstat is unusable evidence, not a crash (correct-or-quiet).
    if diff_present is None and isinstance(numstat, str):
        diff_present = bool(numstat.strip())

    return CompletionCertificate(
        decision=decision,
        head_reason=head.reason,
        patch_revision=submit_revision,
        diff_present=diff_present,
        scope_compliance=scope_f,
        syntax_status=syntax_f,
        type_status=type_f,
        build_status=build_f,
        selected_test_status=seltest_f,
        reproduction_status=repro_f,
        obligation_coverage=obligation_f,
        unresolved_failures=unresolved,
        environment_failures=environment_failures,
        stale_evidence=stale_evidence,
        unknowns=unknowns,
        allow_encouragement=encouragement,
        head_record=dict(head.record),
    )


def safe_build_certificate(**kwargs: Any) -> CompletionCertificate:
    """``build_certificate`` that can never raise — the F3 bulkhead, mirroring
    ``safe_gate_verdict`` (a certificate bug must not brick a submit).

    On ANY exception the decision is recomputed through the SAME single path — the
    seam-supplied ``head`` if present, else ``safe_gate_verdict`` over the same
    covering/hygiene/bounce args (itself crash-proof: poisoned evidence degrades to
    ``gate_crash`` -> allow). A MINIMAL honest certificate is returned: every
    evidence field UNKNOWN (visible, fails open) or NOT_APPLICABLE, the crash
    recorded host-side in ``head_record["build_crash"]``. Never a second decision
    path; never a raise.
    """
    try:
        return build_certificate(**kwargs)
    except Exception as exc:  # noqa: BLE001 -- never brick the loop
        head = kwargs.get("head")
        if not isinstance(head, GateVerdict):
            head = safe_gate_verdict(
                covering=kwargs.get("covering"),
                hygiene=kwargs.get("hygiene"),
                submit_block=kwargs.get("submit_block"),
                bounce_count=kwargs.get("bounce_count", 0),
                max_bounces=kwargs.get("max_bounces", 1),
            )
        rev = kwargs.get("submit_revision")
        rec = dict(head.record)
        rec["build_crash"] = {"error": type(exc).__name__, "message": str(exc)[:200]}
        crash = "certificate_build_crash"
        unknown_fields = {
            "scope_compliance": FieldCert(UNKNOWN, "governor.scope_compliance", None, True, crash),
            "syntax_status": FieldCert(UNKNOWN, "edit_check.check_edit_syntax", None, True, crash),
            "selected_test_status": FieldCert(
                UNKNOWN, "covering_runner.run_covering_tests", None, True, crash
            ),
        }
        return CompletionCertificate(
            decision=_derive_decision(head, FieldCert(NOT_APPLICABLE, "obligations"), ()),
            head_reason=head.reason,
            patch_revision=rev if isinstance(rev, str) else None,
            diff_present=None,
            scope_compliance=unknown_fields["scope_compliance"],
            syntax_status=unknown_fields["syntax_status"],
            type_status=FieldCert(NOT_APPLICABLE, "verification_plan.type", None, True, crash),
            build_status=FieldCert(NOT_APPLICABLE, "verification_plan.build", None, True, crash),
            selected_test_status=unknown_fields["selected_test_status"],
            reproduction_status=FieldCert(NOT_APPLICABLE, "reproduction_runner", None, True, crash),
            obligation_coverage=FieldCert(NOT_APPLICABLE, "obligations", None, True, crash),
            unresolved_failures=(),
            environment_failures=(),
            stale_evidence=(),
            unknowns=tuple(sorted(unknown_fields)),  # UNKNOWN stays VISIBLE in the wreck
            allow_encouragement="",  # never a claim from a crashed build
            head_record=rec,
        )
