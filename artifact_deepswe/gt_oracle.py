"""GT delivery oracle — STAGE 2: the stateless pure decision gate (L5 only).

Per `ORACLE_ARCHITECTURE_PLAN.md` §1.2, §1.3, §8 Stage 2.  This is the
`oracle(candidates, trajectory) -> emit <=1 | silence` pure function, wired ONLY
to the existing L5 governor nudges FIRST, to prove **byte-parity** against the
current live behavior on the frozen replay corpus before any new behavior is
switched on.

Statelessness: the oracle holds NO memory between turns.  Every decision at turn k
is a pure function of `turns[:k+1]` (via the Stage-0 sensor).  The live patch's
fire-once latches (`_l5_fired`, `_l5_failure_fired`, `_l5_notest_fired`) and its
loop-history deque are recomputed from the trajectory prefix, not accumulated.

Byte-parity contract (Stage 2 acceptance): replaying the oracle over a frozen
trajectory must emit the SAME nudge reason at the SAME turn as the live patch did
— no new fires, no dropped fires, no reason/turn drift.  The decision function
scaffold (sense -> trigger -> confidence-floor -> relevance -> dedup -> budget ->
rank -> emit) is fully present, but for the L5 family every gate is a pass-through
in PARITY mode so the result is provably identical to the live governor.  The new
gates (dedup/relevance/budget across layers) are introduced in later stages.

NO LLM (hard rule): predicates + set intersections + content hashes.  Nothing
generative, learned, or sampled.
"""
from __future__ import annotations

import hashlib
import importlib.util as _ilu
import json as _json
import os as _os
import re as _re
import statistics as _stats
import sys as _sys
from dataclasses import dataclass, field, replace as _dc_replace

from groundtruth.runtime import obligations as _product_obligations


def _load_sibling(modname: str, filename: str):
    """Load a sibling module by path (deployment-agnostic, same pattern as the
    sensor).  Returns None if absent."""
    if modname in _sys.modules:
        return _sys.modules[modname]
    here = _os.path.dirname(_os.path.abspath(__file__))
    path = _os.path.join(here, filename)
    if not _os.path.isfile(path):
        return None
    prev = _os.environ.get("GT_BASELINE")
    _os.environ.setdefault("GT_BASELINE", "1")
    try:
        spec = _ilu.spec_from_file_location(modname, path)
        if spec and spec.loader:
            mod = _ilu.module_from_spec(spec)
            _sys.modules[modname] = mod
            spec.loader.exec_module(mod)
            return mod
    except Exception:  # noqa: BLE001
        return None
    finally:
        if prev is None:
            _os.environ.pop("GT_BASELINE", None)
    return None


_sense = _load_sibling("gt_oracle_sense_oracle", "gt_oracle_sense.py")
_gmp = _load_sibling("gt_mini_patch_oracle", "gt_mini_patch.py")

# Fail LOUD at import (LIPI 2026-06-10): every entry point dereferences the
# Stage-0 sensor (`_sense.sense`, `_sense.stream_states`, `_sense._WORD_RE`);
# a missing sibling would otherwise surface as `AttributeError: 'NoneType'`
# mid-replay.  _gmp MAY be None (every use is `_gmp is not None`-guarded);
# the sensor may not.
if _sense is None:
    raise ImportError(
        "gt_oracle: sibling gt_oracle_sense.py (Stage-0 sensor) not found — "
        "it must ship in the same directory as gt_oracle.py")


# ---------------------------------------------------------------------------
# Candidate schema (plan §2.1) — one record type, every layer emits it.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Candidate:
    id: str
    layer: str                  # L1|L3|L3b|L4|L5|L5b|consensus|spec
    kind: str
    content: str                # final payload, pre-rendered, deterministic
    confidence: float
    severity: str
    trigger: str                # named predicate over derived state
    relevance_keys: frozenset[str] = frozenset()
    dose_cost: int = 0
    decay: str = "one_shot"     # one_shot | rearm_on_change | persistent_until_met

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()[:8]


@dataclass(frozen=True)
class Emission:
    candidate_id: str
    layer: str
    channel: str
    payload_text: str
    dose_tokens: int
    trigger_matched: str
    reason_string: str


@dataclass
class SuppressionRecord:
    candidate_id: str
    eligible: bool
    suppressed_reason: str | None  # no_trigger|below_floor|irrelevant|delivered|self_discovered|over_budget|outranked
    severity_score: float
    confidence: float
    relevance_overlap: float


@dataclass
class OracleDecision:
    """The oracle's per-turn output: <=1 emission + full suppression telemetry."""
    turn_index: int
    emission: Emission | None
    telemetry: list[SuppressionRecord] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Severity ordinal (plan §3.4 WHAT ordering) — higher = more important.
# ---------------------------------------------------------------------------
_SEVERITY_RANK = {
    "obligation": 5,
    "verification_gap": 4,
    "contract_violation_risk": 3,
    "stuck": 2,
    "code_map": 1,
}


def _severity_score(sev: str) -> float:
    return float(_SEVERITY_RANK.get(sev, 0))


# ---------------------------------------------------------------------------
# DISTRIBUTION-DERIVED confidence floor (gt_gt §15.2 / plan §6 item 2) —
# "per-task distribution-derived floors, never hardcoded".  The floor is a pure
# statistic of the candidate pool's OWN scores: median + 1*MAD (the standard
# robust location+dispersion pair — Leys et al., J. Exp. Soc. Psych. 2013,
# "Detecting outliers: Do not use standard deviation around the mean, use
# absolute deviation around the median").  Properties:
#   - singleton pool: MAD == 0 -> floor == the candidate's own value -> passes
#     (>=); a lone triggered candidate is never suppressed by its own score.
#   - multi-candidate pool: candidates in the low tail (more than 1 MAD below
#     the pool's central mass... i.e. below median+MAD) are suppressed —
#     the gate scales with the data, no invented absolute threshold anywhere.
# ---------------------------------------------------------------------------
def _c1_floor_mad_multiplier() -> float:
    try:
        return float(_os.environ.get("GT_C1_CONFIDENCE_FLOOR_MAD_MULTIPLIER", "") or 1.0)
    except (TypeError, ValueError):
        return 1.0


def distribution_floor(confidences) -> float:
    vals = [float(c) for c in confidences]
    if not vals:
        return 0.0
    med = _stats.median(vals)
    mad = _stats.median(abs(v - med) for v in vals)
    k = _c1_floor_mad_multiplier()
    try:
        print(
            f"[GT_CONFIG] C1_CONFIDENCE_FLOOR_MAD_MULTIPLIER={k} "
            "context=artifact_deepswe.gt_oracle.distribution_floor",
            file=_sys.stderr,
            flush=True,
        )
    except Exception:
        pass
    return med + (k * mad)


def gate_pool(candidates) -> tuple[Candidate | None, list[SuppressionRecord]]:
    """Steps 3+7 of the decision function over a TRIGGERED candidate pool:
    CONFIDENCE (distribution-derived floor) then RANK (severity desc,
    confidence 8dp desc, id asc — deterministic tie-break).  Returns the single
    winner (or None) + full suppression telemetry for every candidate."""
    telemetry: list[SuppressionRecord] = []
    if not candidates:
        return None, telemetry
    floor = distribution_floor([c.confidence for c in candidates])
    passing = [c for c in candidates if c.confidence >= floor]
    winner: Candidate | None = None
    if passing:
        winner = sorted(
            passing,
            key=lambda c: (-_severity_score(c.severity),
                           -round(c.confidence, 8), c.id),
        )[0]
    for c in candidates:
        if c.confidence < floor:
            reason = "below_floor"
        elif winner is not None and c is not winner:
            reason = "outranked"
        else:
            reason = None
        telemetry.append(SuppressionRecord(
            candidate_id=c.id, eligible=(reason is None),
            suppressed_reason=reason,
            severity_score=_severity_score(c.severity),
            confidence=float(c.confidence), relevance_overlap=1.0,
        ))
    return winner, telemetry


# ---------------------------------------------------------------------------
# L5 governor candidate producers — encode the EXACT live predicates so the
# oracle's decision is byte-identical to gt_mini_patch's nudges.  These read the
# Stage-0 derived state for turn k (sense(turns[:k+1])) and the loop history.
# ---------------------------------------------------------------------------

# The EXACT nudge payloads (copied verbatim from gt_mini_patch so the emitted
# text is byte-identical).  Source: gt_mini_patch.py:1537 / :1542 / :1661 / :1692.
_NUDGE_LOOP = (
    '\n<gt-nudge reason="loop">\nGT: you have repeated the same command 4+ '
    "times with no progress. Stop, re-read the last error, and change approach "
    "(open a different file or test a new hypothesis).\n</gt-nudge>"
)
_NUDGE_SCAFFOLD = (
    '\n<gt-nudge reason="scaffold_trap">\nGT: 25+ actions and no source-file edit '
    "yet — you are likely stuck exploring/scaffolding. Use the brief's gt-scope to "
    "localize and make a concrete edit to a SOURCE file now.\n</gt-nudge>"
)
_NUDGE_NO_TEST = (
    '\n<gt-nudge reason="no_test_evidence">\nGT: your test commands have produced '
    "no visible test results (likely killed/timed out before any test ran). You have "
    "NOT observed a single test execute — do not conclude tests pass, and do not "
    "submit yet. Run a narrower target (one test name / one module) or raise the "
    "timeout until you see real pass/fail output.\n</gt-nudge>"
)
_NUDGE_FAILURE = (
    '\n<gt-nudge reason="failure_persisted">\nGT: the same test failure has '
    "persisted across your edit(s) — your current hypothesis is likely wrong. "
    "Re-read the failing assertion and reconsider the root cause / target file.\n</gt-nudge>"
)


def _loop_signature(cmd: str, raw_obs: str) -> str:
    """The live loop signature: command + collapsed observation (no-new-state
    proof).  gt_mini_patch.py:1531."""
    norm = (cmd or "").strip()
    return norm + "\x00" + " ".join((raw_obs or "").split())[:400]


def _fail_sig(text: str, parity_mode: bool) -> str:
    """The failure-signature of one observation.

    parity_mode=True  — the HISTORICAL predicate (every _TEST_FAIL_RE line),
      byte-parity with the recorded corpus (Stage-2 contract, frozen).
    parity_mode=False — the CORRECTED predicate (delivery-engine Stage 5):
      patch/apply bookkeeping lines excluded via gt_mini_patch._failure_lines
      (the fd `Hunk #N FAILED` green-run FP).  ONE implementation, bound from
      the live module — the twins cannot disagree."""
    if _gmp is None:
        return ""
    if parity_mode:
        fails = [ln.strip() for ln in (text or "").splitlines()
                 if _gmp._TEST_FAIL_RE.search(ln)]
    else:
        fails = _gmp._failure_lines(text)
    return "|".join(sorted(set(fails))[:3])[:200]


def _l5_decision_at(turns, k: int, parity_mode: bool = True) -> tuple[str, str, float] | None:
    """Re-derive which L5 nudge (if any) the live patch would emit AT turn k,
    statelessly, evaluating the SAME predicates over turns[:k+1] with the same
    fire-once-per-class semantics.  Returns (reason, payload, confidence) or None.

    Evaluation order mirrors _augment_output exactly:
      1. _l5_nudge: loop (>=4 same (cmd,obs) in the last-12 window) OR
         scaffold_trap (action_count>=25 and source_edit_count==0); fires once.
      2. _l5_failure_nudge: same TEST failure signature recurs >=2 with >=1 edit.
      3. _l5_no_test_evidence_nudge: 2nd blind real-test run with >=1 edit and no
         test evidence yet; fires once.
    A class that already fired at an EARLIER turn is latched off (the live _fired
    flags) — recomputed here by scanning earlier turns for that class's fire.

    parity_mode=False applies the delivery-engine Stage-5 failure_persisted
    corrections (patch-noise + baseline-failure exclusions)."""
    # Which classes already fired before turn k (the stateless latch).  loop and
    # scaffold_trap share the single live latch _l5_fired -> the "l5_stuck" key.
    fired_before: set[str] = set()
    for j in range(k):
        d = _l5_raw_fire(turns, j, fired_before, parity_mode)
        if d is not None:
            fired_before.add(_latch_key(d[0]))
    return _l5_raw_fire(turns, k, fired_before, parity_mode)


def _latch_key(reason: str) -> str:
    """Map a nudge reason to its live fire-once latch key.  loop + scaffold_trap
    share gt_mini_patch._l5_fired; the others have their own flag."""
    return "l5_stuck" if reason in ("loop", "scaffold_trap") else reason


def _l5_raw_fire(turns, k: int, fired_before: set[str],
                 parity_mode: bool = True) -> tuple[str, str, float] | None:
    """The single-turn fire decision GIVEN which classes already fired.  This is
    the exact body of _augment_output's nudge block for turn k.

    The returned confidence is PROVENANCE-DERIVED (plan §2.1: "provenance-
    derived, never invented") — a pure function of the sensed evidence:
      loop              -> count(sig in window) / len(window)   (no-new-state saturation)
      scaffold_trap     -> (actions - source_edits) / actions   (non-edit fraction; ==1.0 at fire)
      failure_persisted -> recurrences / real-test-runner runs  (failure-signature saturation)
      no_test_evidence  -> blind runs / total test runs         (blindness saturation)
    """
    if k >= len(turns):
        return None
    st = _sense.sense(turns[: k + 1])  # state AFTER processing turn k (counts incl. k)
    turn = turns[k]
    cmd = turn.command
    raw = turn.raw_obs

    # ---- 1. _l5_nudge: loop OR scaffold_trap (one latch: "l5_stuck") ----
    if "l5_stuck" not in fired_before:
        # loop: build the last-12 signature window over turns[:k+1] (only turns
        # with a non-empty normalized command append to history, matching live).
        norm = (cmd or "").strip()
        if norm:
            window: list[str] = []
            for j in range(k + 1):
                cj = (turns[j].command or "").strip()
                if not cj:
                    continue
                window.append(_loop_signature(turns[j].command, turns[j].raw_obs))
            window = window[-12:]
            sig = _loop_signature(cmd, raw)
            n_sig = window.count(sig)
            if n_sig >= 4:
                return ("loop", _NUDGE_LOOP, n_sig / len(window))
        # scaffold_trap: action_count>=25 and zero source edits.
        if st.action_count >= 25 and st.source_edit_count == 0:
            conf = (st.action_count - st.source_edit_count) / st.action_count
            return ("scaffold_trap", _NUDGE_SCAFFOLD, conf)

    # ---- 2. _l5_failure_nudge: same test failure recurs >=2 (after >=1 edit) ----
    if "failure_persisted" not in fired_before and raw:
        if _gmp is not None and _gmp._TEST_RUNNER_RE.search(cmd or ""):
            if not _gmp._ENV_FAIL_RE.search(raw):
                sig = _fail_sig(raw, parity_mode)
                if sig:
                    # count this signature's recurrence across all PRIOR + current
                    # real-test-runner turns (the live _test_fail_history.append).
                    # Non-parity (Stage 5): baseline failures — observed before
                    # any source edit or inside a `git stash` window — never
                    # enter the history and never fire (mirrors the live gates).
                    hist_count = 0
                    runner_count = 0
                    stash = 0
                    edits = 0
                    baseline: set[str] = set()
                    cur_is_baseline = False
                    for j in range(k + 1):
                        tj = turns[j]
                        cj = tj.command or ""
                        j_stashed = False
                        if not parity_mode:
                            # single-turn stash disproofs are baseline windows
                            pushes = len(_gmp._STASH_PUSH_RE.findall(cj))
                            pops = len(_gmp._STASH_POP_RE.findall(cj))
                            j_stashed = stash > 0 or pushes > 0
                            stash = max(0, stash + pushes - pops)
                            kj, fj_ = _gmp._classify(cj)
                            if kj == "post_edit" and fj_:
                                edits += 1
                        if not _gmp._TEST_RUNNER_RE.search(cj):
                            continue
                        if _gmp._ENV_FAIL_RE.search(tj.raw_obs or ""):
                            continue
                        runner_count += 1
                        sj = _fail_sig(tj.raw_obs, parity_mode)
                        if not sj:
                            continue
                        if not parity_mode:
                            if edits == 0 or j_stashed:
                                baseline.add(sj)
                                if j == k:
                                    cur_is_baseline = True
                                continue
                            if sj in baseline:
                                if j == k:
                                    cur_is_baseline = True
                                continue
                        if sj == sig:
                            hist_count += 1
                    if (hist_count >= 2 and st.source_edit_count >= 1
                            and not cur_is_baseline
                            and (parity_mode or sig not in baseline)):
                        return ("failure_persisted", _NUDGE_FAILURE,
                                hist_count / runner_count)

    # ---- 3. _l5_no_test_evidence_nudge: 2nd blind run + edit + no evidence ----
    if "no_test_evidence" not in fired_before:
        if _gmp is not None and _gmp._TEST_RUNNER_RE.search(cmd or ""):
            text = raw or ""
            has_result = bool(_gmp._TEST_FAIL_RE.search(text)
                              or _gmp._TEST_PASS_RE.search(text))
            is_env = bool(_gmp._ENV_FAIL_RE.search(text))
            is_compile = bool(_gmp._COMPILE_FAIL_RE.search(text))
            if not has_result and not is_env and not is_compile:
                # blind run: this is the (>=1)th blind run by sense; the live
                # counter increments then checks >=2.  st.blind_test_runs already
                # counts this turn (sense processed turn k).
                # test_evidence_seen reflects PRIOR turns only at the decision
                # point (the live check happens before this turn's result could
                # latch it — but a blind run has no result, so it never would).
                if (st.blind_test_runs >= 2 and not st.test_evidence_seen
                        and st.source_edit_count >= 1):
                    conf = st.blind_test_runs / max(len(st.test_runs), 1)
                    return ("no_test_evidence", _NUDGE_NO_TEST, conf)
    return None


def _l5_candidate(reason: str, payload: str, confidence: float) -> Candidate:
    sev = {
        "no_test_evidence": "verification_gap",
        "failure_persisted": "stuck",
        "loop": "stuck",
        "scaffold_trap": "stuck",
    }.get(reason, "stuck")
    return Candidate(
        id=f"l5.{reason}", layer="L5", kind=reason, content=payload,
        confidence=float(confidence), severity=sev, trigger=reason,
        dose_cost=len(payload) // 4, decay="one_shot",
    )


# ---------------------------------------------------------------------------
# SPEC producer wiring (fix 4) — the obligations spec.py extracts and
# v1r_brief.py serializes into gt_issue_anchors.json now have a READER: the
# oracle's candidate pool includes them.  Their TRIGGER (the anchor-aware
# test-evidence gap) lands in Stage 3; until then they are recorded in
# telemetry as suppressed `no_trigger` — wired, gated, silent.
# ---------------------------------------------------------------------------
_ANCHORS_PATH_DEFAULT = "/tmp/gt_issue_anchors.json"

# DRIFT class certification gate (plan §11 / gt_gt §15.5 LEG 1): the drift
# trigger's only consumption ground truth is ~14 hand-audited trajectories —
# NOT enough to certify per-class trigger precision.  Until a HELD-OUT,
# NON-BENCHMARK replay corpus certifies it, drift candidates are PRODUCED
# (telemetry-visible, auditable) but NEVER emitted: every drift record carries
# suppressed_reason="drift_uncertified".  Whitelist-only by construction (only
# spec.py's §5.2 SAFE checkable_forms are even examined).
DRIFT_CERTIFIED = False


def load_obligations(path: str = _ANCHORS_PATH_DEFAULT) -> list[dict]:
    """Read the `obligations[]` array from the gt_issue_anchors.json artifact
    (the F1 channel v1r_brief.py already ships per task).  Correct-or-quiet:
    missing file / bad JSON / absent key -> empty list, never raises."""
    try:
        with open(path, encoding="utf-8") as f:
            data = _json.load(f)
        obls = data.get("obligations") or []
        return [o for o in obls if isinstance(o, dict)]
    except Exception:  # noqa: BLE001
        return []


@dataclass(frozen=True)
class _ObligationView:
    """A parsed obligation: the symbol-part set drives the edited?/tested?
    intersections (plan §5.2)."""
    idx: int
    verbatim: str
    sym_parts: frozenset[str]
    keywords: frozenset[str]
    checkable: frozenset[str]


def _obligation_views(obligations) -> list[_ObligationView]:
    views: list[_ObligationView] = []
    for i, o in enumerate(obligations or []):
        if not isinstance(o, dict):
            continue
        verbatim = str(o.get("verbatim_text") or "")
        if not verbatim:
            continue
        symbols = [s for s in (o.get("symbols") or []) if isinstance(s, str)]
        parts: set[str] = set()
        for s in symbols:
            parts.add(s)
            parts.update(p for p in s.split(".") if len(p) >= 3)
        views.append(_ObligationView(
            idx=i, verbatim=verbatim, sym_parts=frozenset(parts),
            keywords=frozenset(kw for kw in (o.get("keywords") or [])
                               if isinstance(kw, str)),
            checkable=frozenset(cf for cf in (o.get("checkable_forms") or [])
                                if isinstance(cf, str)),
        ))
    return views


def _overlap(view: _ObligationView, tokens: set[str]) -> tuple[set[str], float]:
    """PROVENANCE-DERIVED evidence ratio (plan §5.2 'edited?'): which of the
    obligation's symbol parts appear in the given token set, and the fraction.
    No symbols -> 0.0 (a pure-prose obligation can never trigger)."""
    if not view.sym_parts:
        return set(), 0.0
    touched = {s for s in view.sym_parts if s in tokens}
    return touched, len(touched) / len(view.sym_parts)


def _is_compound_symbol(part: str) -> bool:
    """Structural (constant-free) test for a symbol-shaped compound name —
    snake_case, dotted, digit-bearing, or CamelCase-internal.  Compound names
    are specific enough that SUBSTRING containment in a test token is
    meaningful evidence (`capture_snapshot` inside `test_capture_snapshot`);
    plain single words only ever match exactly."""
    if "_" in part or "." in part or any(c.isdigit() for c in part):
        return True
    return any(c.isupper() for c in part[1:])


def _obligation_tested(view: _ObligationView, tested_tokens: set[str]) -> bool:
    """plan §5.2 'tested?': obligation symbols ∩ observed test command/output.
    Exact token intersection, plus substring containment for compound symbol
    names (test functions conventionally prefix the API name: the token
    `test_capture_snapshot` IS observed evidence for `capture_snapshot`).
    Looser matching here is the SAFE direction — 'tested' SUPPRESSES a fire
    (correct-or-quiet: when in doubt, stay silent)."""
    if view.sym_parts & tested_tokens:
        return True
    compound = [p for p in view.sym_parts if _is_compound_symbol(p)]
    if not compound:
        return False
    return any(p in t for t in tested_tokens for p in compound)


def _render_obligation_nudge(verbatim: str, sym: str) -> str:
    """The Rank-1 active check (plan §3.2 WHERE): short, imperative, at recency
    position, quoting the issue requirement VERBATIM (`issue_verbatim`
    provenance — the un-misdirectable class).  Carries a stable content-hash
    marker (plan §1.2 delivered-set anchor)."""
    quote = verbatim if len(verbatim) <= 200 else verbatim[:197] + "..."
    body = (
        f"GT: none of the test output you have observed mentions `{sym}`. "
        f"The issue states: \"{quote}\" — you have edited this surface but have "
        f"not observed it execute. Run a test that exercises `{sym}` before "
        f"concluding this requirement is met."
    )
    h = hashlib.sha256(body.encode("utf-8")).hexdigest()[:8]
    return f'\n<gt-nudge reason="test_evidence_gap" h="{h}">\n{body}\n</gt-nudge>'


def _obligation_candidate(view: _ObligationView, touched: set[str],
                          conf: float) -> Candidate:
    sym = sorted(touched, key=lambda s: (-len(s), s))[0]
    payload = _render_obligation_nudge(view.verbatim, sym)
    return Candidate(
        id=f"spec.obligation.{view.idx}",
        layer="spec", kind="test_evidence_gap", content=payload,
        confidence=float(conf), severity="obligation",
        trigger="test_evidence_gap",
        relevance_keys=view.sym_parts | view.keywords,
        dose_cost=len(payload) // 4, decay="persistent_until_met",
    )


# ---------------------------------------------------------------------------
# STAGE 2 (delivery engine, 2026-06-11) — per-obligation STATUS at the review
# transition.  The #1 lever from DEEP_TRAJECTORY_ANALYSIS_ORACLE_RUN §3.3:
# "review-transition unmet-obligation emission … would have intersected the
# killing defect in 7-8 of 9 tasks; every trajectory HAD the event; every
# trajectory got silence."  The status vector (edited?/tested? per obligation,
# recomputed statelessly from the trajectory) is BOTH the payload and the
# dedup key: same vector -> suppress, changed vector -> re-fire (the awilix
# h=0e1a89ba one-shot content-hash defect closed by construction).
# Research: TRAJEVAL (arXiv 2603.24631) once-per-triggering-CONTEXT dosing;
# Wink (arXiv 2602.17037) class-keyed sparse corrective text; severity from
# the Zilberstein (AI Magazine 1996) contract-algorithm urgency form.
# ---------------------------------------------------------------------------
OBL_TESTED = "tested"
OBL_EDITED_UNTESTED = "edited_untested"
OBL_UNADDRESSED = "unaddressed"


def obligation_statuses(views, edited_tokens, tested_tokens):
    """Per-obligation status, recomputed statelessly from the §5.2 surfaces:
    (view, status, touched_symbols, edit_overlap_conf) per obligation."""
    out = []
    edited = set(edited_tokens or ())
    tested = set(tested_tokens or ())
    for v in views:
        touched, conf = _overlap(v, edited)
        if _obligation_tested(v, tested):
            status = OBL_TESTED
        elif touched:
            status = OBL_EDITED_UNTESTED
        else:
            status = OBL_UNADDRESSED
        out.append((v, status, touched, conf))
    return out


def status_vector_hash(statuses) -> str:
    """The content×phase×status dedup key: a hash of the (obligation, status)
    vector.  Phase is fixed (REVIEW — the only trigger), content is the
    obligation set — so the vector hash IS the full dedup key."""
    body = "|".join(f"{v.idx}:{s}" for v, s, _t, _c in statuses)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:8]


if _gmp is not None and hasattr(_gmp, "composite_severity"):
    # ONE formula (delivery-engine Stage 4): bind the live module's
    # composite-severity primitive — live and replay can never disagree.
    composite_severity = _gmp.composite_severity
else:  # pragma: no cover - standalone replay without the sibling
    def composite_severity(base: float, budget_fraction: float,
                           unmet_ratio: float) -> float:
        """severity = base + (budget_fraction × 2) + (unmet_ratio × 1) —
        Zilberstein 1996 contract-algorithm urgency; hybrid 3-signal
        composite. Fallback copy of gt_mini_patch.composite_severity."""
        return (float(base) + 2.0 * float(budget_fraction)
                + 1.0 * float(unmet_ratio))


def order_unmet(statuses):
    """Deterministic display order: edited-but-untested first (the proven
    convertible class — the intersection that aimed 3/3 at the hidden
    verifier's target), then unaddressed; stable by obligation index."""
    unmet = [x for x in statuses if x[1] != OBL_TESTED]
    unmet.sort(key=lambda x: (0 if x[1] == OBL_EDITED_UNTESTED else 1, x[0].idx))
    return unmet


# ---------------------------------------------------------------------------
# CP011 — persistent obligation tracker (lifecycle across turns).
# ---------------------------------------------------------------------------
@dataclass
class Obligation:
    """One issue obligation with lifecycle status and evidence trail."""
    id: int
    verbatim: str
    symbols: frozenset[str]
    status: str  # unedited | edited | tested | satisfied | contradicted
    evidence: list[str] = field(default_factory=list)
    last_turn: int = 0


def _status_to_obligation_lifecycle(obl_status: str) -> str:
    if obl_status == OBL_TESTED:
        return "tested"
    if obl_status == OBL_EDITED_UNTESTED:
        return "edited"
    return "unedited"


def _obligation_lifecycle_to_status(lifecycle: str) -> str:
    if lifecycle in ("tested", "satisfied"):
        return OBL_TESTED
    if lifecycle == "edited":
        return OBL_EDITED_UNTESTED
    return OBL_UNADDRESSED


def _obligation_status_certainty(status: str) -> str:
    if status == "satisfied":
        return "explicit_satisfaction_evidence"
    if status == "contradicted":
        return "contradicting_evidence"
    if status == "tested":
        return "observed_test_token"
    if status == "edited":
        return "observed_edit_token"
    return "no_runtime_evidence"


class ObligationTracker:
    """Persistent per-run obligation lifecycle — survives across turns."""

    def __init__(self, obligations: list[dict] | None = None):
        self.obligations: list[Obligation] = []
        views = _obligation_views(obligations or [])
        for v in views:
            self.obligations.append(Obligation(
                id=v.idx,
                verbatim=v.verbatim,
                symbols=v.sym_parts,
                status="unedited",
            ))
        self._views = views

    # D4: monotonic lifecycle ordering — status only moves FORWARD.
    _LIFECYCLE_ORDER = {"unedited": 0, "edited": 1, "tested": 2, "satisfied": 3}

    def update(
        self,
        edited_tokens: set[str],
        tested_tokens: set[str],
        turn: int,
    ) -> list[tuple[int, str, str]]:
        """Apply turn evidence; return [(id, old_status, new_status)] transitions.
        D4 fix: monotonic ratchet — status only moves forward (unedited→edited→
        tested→satisfied), never backward. Min symbol length 4 to avoid false
        positives like 'map' matching every command."""
        # Filter tokens: min length 4 avoids 3-char false positives (e.g. 'map')
        filtered_edit = {t for t in edited_tokens if len(t) >= 4}
        filtered_test = {t for t in tested_tokens if len(t) >= 4}
        statuses = obligation_statuses(self._views, filtered_edit, filtered_test)
        transitions: list[tuple[int, str, str]] = []
        by_id = {o.id: o for o in self.obligations}
        for v, st, touched, _conf in statuses:
            ob = by_id.get(v.idx)
            if ob is None:
                continue
            new_lifecycle = _status_to_obligation_lifecycle(st)
            if st == OBL_EDITED_UNTESTED and touched:
                new_lifecycle = "edited"
            if st == OBL_TESTED:
                new_lifecycle = "tested"
            old = ob.status
            old_rank = self._LIFECYCLE_ORDER.get(old, 0)
            new_rank = self._LIFECYCLE_ORDER.get(new_lifecycle, 0)
            if new_rank > old_rank:
                transitions.append((ob.id, old, new_lifecycle))
                ob.status = new_lifecycle
                ob.last_turn = turn
                if touched:
                    ob.evidence.append(f"turn{turn}:edited={','.join(sorted(touched)[:3])}")
                if st == OBL_TESTED:
                    ob.evidence.append(f"turn{turn}:tested")
        return transitions

    def mark_satisfied(self, obligation_id: int, evidence: str, turn: int) -> bool:
        """Promote an obligation only when an integration has explicit proof."""
        return self._set_explicit_status(
            obligation_id, "satisfied", evidence=evidence, turn=turn
        )

    def mark_contradicted(self, obligation_id: int, evidence: str, turn: int) -> bool:
        """Mark an obligation contradicted by observed behavior or failure output."""
        return self._set_explicit_status(
            obligation_id, "contradicted", evidence=evidence, turn=turn
        )

    def _set_explicit_status(
        self, obligation_id: int, status: str, *, evidence: str, turn: int
    ) -> bool:
        if status not in {"satisfied", "contradicted"}:
            return False
        for ob in self.obligations:
            if ob.id != obligation_id:
                continue
            ob.status = status
            ob.last_turn = turn
            if evidence:
                ob.evidence.append(f"turn{turn}:{status}={evidence[:120]}")
            return True
        return False

    def statuses_tuple(self, edited_tokens: set[str], tested_tokens: set[str]):
        """Compatibility shim: same tuple shape as obligation_statuses()."""
        edited = set(edited_tokens or ())
        tested = set(tested_tokens or ())
        out = []
        by_id = {o.id: o for o in self.obligations}
        for v in self._views:
            ob = by_id.get(v.idx)
            if ob is None:
                continue
            st = _obligation_lifecycle_to_status(ob.status)
            touched, conf = _overlap(v, edited)
            if ob.status == "contradicted":
                st = OBL_EDITED_UNTESTED if touched else OBL_UNADDRESSED
            elif ob.status in ("tested", "satisfied") or _obligation_tested(v, tested):
                st = OBL_TESTED
            elif ob.status == "edited" or touched:
                st = OBL_EDITED_UNTESTED
            else:
                st = OBL_UNADDRESSED
            out.append((v, st, touched, conf))
        return out

    def unmet(self) -> list[Obligation]:
        return [
            o for o in self.obligations
            if o.status not in ("tested", "satisfied")
        ]

    def coverage_ratio(self) -> float:
        if not self.obligations:
            return 1.0
        done = sum(1 for o in self.obligations if o.status in ("tested", "satisfied"))
        return done / len(self.obligations)

    def snapshot(self) -> list[dict]:
        """Serializable obligation vector for post-run truth (P1-18/19)."""
        return [
            {
                "id": o.id,
                "status": o.status,
                "status_certainty": _obligation_status_certainty(o.status),
                "last_turn": o.last_turn,
                "verbatim": (o.verbatim or "")[:160],
                "evidence": list(o.evidence[-3:]),
            }
            for o in self.obligations
        ]


def render_obligation_status_block(statuses, covering=None,
                                   max_listed: int | None = None) -> str:
    """Render the review-transition checklist without leaking exact tests.

    `covering` may contain exact graph-linked test identifiers for internal
    targeting. Agent-visible text only states that targeted verification is
    required; it never names the test, file, or single-test command.
    """
    unmet = order_unmet(statuses)
    if not unmet:
        return ""
    h = status_vector_hash(statuses)
    tested_n = sum(1 for _v, s, _t, _c in statuses if s == OBL_TESTED)
    lines = [
        "GT: requirement status from the issue - sensed from your own edit "
        "commands and observed test output:",
    ]
    listed = unmet if max_listed is None else unmet[:max_listed]
    for v, s, _touched, _conf in listed:
        quote = v.verbatim if len(v.verbatim) <= 160 else v.verbatim[:157] + "..."
        mark = ("[edited, untested]" if s == OBL_EDITED_UNTESTED
                else "[not addressed]")
        line = f"{mark} \"{quote}\""
        if (covering or {}).get(v.idx) and s == OBL_EDITED_UNTESTED:
            line += (
                "\n    targeted verification: graph-linked covering test "
                "available; run the narrowest relevant repo test target."
            )
        lines.append(line)
    if max_listed is not None and len(unmet) > max_listed:
        lines.append(f"(+{len(unmet) - max_listed} more unverified requirement(s))")
    if tested_n:
        lines.append(f"{tested_n} requirement(s) already show test evidence.")
    lines.append(
        "Run targeted verification for each untested requirement before "
        "concluding it is met; an unverified submission cannot be fixed after "
        "submit.")
    body = "\n".join(lines)
    return (f'\n<gt-nudge reason="test_evidence_gap" h="{h}">\n{body}\n</gt-nudge>')


# Product-owned obligation lifecycle. Keep the historical gt_oracle names as
# adapter-facing shims so live and replay paths use the same status vocabulary.
OBL_TESTED = _product_obligations.OBL_TESTED
OBL_EDITED_UNTESTED = _product_obligations.OBL_EDITED_UNTESTED
OBL_UNADDRESSED = _product_obligations.OBL_UNADDRESSED
obligation_statuses = _product_obligations.obligation_statuses
status_vector_hash = _product_obligations.status_vector_hash
order_unmet = _product_obligations.order_unmet
render_obligation_status_block = _product_obligations.render_obligation_status_block


class ObligationTracker(_product_obligations.ObligationTracker):
    """Compatibility wrapper: old callers pass raw obligation dicts."""

    def __init__(self, obligations: list[dict] | None = None):
        super().__init__(_obligation_views(obligations or []))


def _drift_records(views, turn, st, k: int) -> list[SuppressionRecord]:
    """WHITELIST-ONLY drift production (plan §5.2 SAFE set), ALWAYS suppressed
    while DRIFT_CERTIFIED is False.  A drift candidate exists when this turn's
    edit evidence touches an obligation symbol but LACKS the obligation's
    checkable surface form (e.g. obligation says `async`, the edit that touched
    the symbol carries no async/await shape).  Anything not whitelisted is not
    even examined (correct-or-quiet)."""
    out: list[SuppressionRecord] = []
    edited_this_turn = any(e.turn_index == k for e in st.edit_events)
    if not edited_this_turn:
        return out
    tokens = {w for w in _sense._WORD_RE.findall(turn.command or "") if len(w) >= 3}
    low = {t.lower() for t in tokens}
    for v in views:
        if not v.checkable:
            continue
        touched, conf = _overlap(v, tokens)
        if not touched:
            continue
        for form in sorted(v.checkable):
            if form in ("async", "await"):
                contradiction = form not in low
            else:
                contradiction = form not in (turn.command or "")
            if contradiction:
                out.append(SuppressionRecord(
                    candidate_id=f"spec.drift.{v.idx}",
                    eligible=False,
                    suppressed_reason="drift_uncertified",
                    severity_score=_severity_score("obligation"),
                    confidence=float(conf), relevance_overlap=float(conf),
                ))
                break  # one record per obligation per turn
    return out


# ---------------------------------------------------------------------------
# STAGE 4 — the dumping layers become candidate PRODUCERS (plan §2.2 routing
# table).  Delivered payload blocks are reconstructed from the trajectory's
# recorded observations into the one candidate schema; the oracle's
# RELEVANCE / DEDUP / BUDGET gates then decide what would actually reach the
# agent.  gt-nudge is NOT reconstructed (the L5 producer owns it); the C0
# brief (gt-task-brief in the task prompt) is the single front-load exemption.
# ---------------------------------------------------------------------------
_PAYLOAD_BLOCK_RE = _re.compile(
    r"<(gt-(?:evidence|scope|contract|cochange|graph|localization))\b[^>]*>"
    r".*?</\1>",
    _re.S,
)
_TAG_ROUTE = {
    "gt-evidence": ("L3b", "witness", "code_map", "one_shot"),
    "gt-scope": ("consensus", "scope", "code_map", "one_shot"),
    "gt-contract": ("L3", "contract", "contract_violation_risk", "rearm_on_change"),
    "gt-cochange": ("L3", "cochange", "code_map", "rearm_on_change"),
    "gt-graph": ("L1", "graph_map", "code_map", "one_shot"),
    "gt-localization": ("L1", "localization", "code_map", "one_shot"),
}


def delivered_payload_candidates(turns) -> list[tuple[int, Candidate]]:
    """Reconstruct every GT payload block recorded in the trajectory's
    observations as (turn_index, Candidate).  relevance_keys = the block's
    identifier tokens (the surface the relevance gate intersects with the
    agent's focus)."""
    out: list[tuple[int, Candidate]] = []
    for k, t in enumerate(turns):
        for m in _PAYLOAD_BLOCK_RE.finditer(t.full_obs or ""):
            tag = m.group(1)
            layer, kind, sev, decay = _TAG_ROUTE[tag]
            text = m.group(0)
            keys = frozenset(
                w for w in _sense._WORD_RE.findall(text) if len(w) >= 3)
            out.append((k, Candidate(
                id=f"{layer}.{kind}.{k}.{len(out)}",
                layer=layer, kind=kind, content=text,
                confidence=0.0, severity=sev, trigger="view|edit",
                relevance_keys=keys, dose_cost=len(text) // 4, decay=decay,
            )))
    return out


def _focus_set(st, anchors: set[str], views, tested_tokens: set[str]) -> set[str]:
    """The agent's CURRENT focus (plan §1.3 step 4): issue anchors ∪ UNMET
    obligation symbols ∪ the edited-file stems so far."""
    focus = set(anchors or ())
    for v in views:
        if not _obligation_tested(v, tested_tokens):
            focus |= v.sym_parts
    for f in st.edited_files():
        stem = _os.path.splitext(_os.path.basename(f))[0]
        if len(stem) >= 3:
            focus.add(stem)
    return focus


def stage4_replay(turns, *, obligations: list[dict] | None = None,
                  anchors: set[str] | None = None) -> list[OracleDecision]:
    """The Stage-4 replay instrument: L5 + obligations (Stage 3 semantics,
    unchanged) PLUS the reconstructed layer payloads, gated by relevance /
    dedup / budget.  Pure function of (turns, obligations, anchors)."""
    payload_pool: dict[int, list[Candidate]] = {}
    for k, c in delivered_payload_candidates(turns):
        payload_pool.setdefault(k, []).append(c)
    return _decide_walk(turns, obligations, payload_pool=payload_pool,
                        anchors=set(anchors or ()))


# ---------------------------------------------------------------------------
# The pure decision function (plan §1.3).  PARITY MODE for the L5 family.
# ---------------------------------------------------------------------------
def oracle_decide(turns, k: int, *, parity_mode: bool = True,
                  obligations: list[dict] | None = None) -> OracleDecision:
    """Decide what (if anything) to emit at turn k, statelessly.

    Without obligations this is the Stage-2 pure L5 reference path (byte-parity
    with the live governor on the frozen corpus; a singleton triggered pool
    always passes its own distribution floor).  With obligations (Stage 3) the
    decision is the combined walk over the prefix — still a pure function of
    (turns[:k+1], obligations): same input, same output, bit-for-bit."""
    if obligations:
        return _decide_walk(turns[: k + 1], obligations,
                            parity_mode=parity_mode)[k]

    telemetry: list[SuppressionRecord] = []
    # 1+2. SENSE + TRIGGER: the L5 family produces at most one triggered candidate
    # per turn (the live governor emits at most one nudge of each class, and the
    # _l5_decision_at order yields the first one that fires this turn).
    fired = _l5_decision_at(turns, k, parity_mode)
    triggered: list[Candidate] = []
    if fired is not None:
        reason, payload, conf = fired
        triggered.append(_l5_candidate(reason, payload, conf))

    # 3-7. CONFIDENCE (distribution floor) + RANK over the triggered pool.
    winner, gate_telemetry = gate_pool(triggered)
    telemetry.extend(gate_telemetry)
    if winner is None:
        return OracleDecision(turn_index=k, emission=None, telemetry=telemetry)

    # 8. EMIT
    emission = Emission(
        candidate_id=winner.id, layer=winner.layer, channel="C1",
        payload_text=winner.content, dose_tokens=winner.dose_cost,
        trigger_matched=winner.trigger, reason_string=winner.kind,
    )
    return OracleDecision(turn_index=k, emission=emission, telemetry=telemetry)


def _l5_fire_streaming(turns, states=None,
                       parity_mode: bool = True) -> list[tuple[str, str, float] | None]:
    """Efficient forward pass that yields, per turn, the (reason, payload,
    confidence) the L5 family would emit — IDENTICAL to calling
    _l5_decision_at(turns, k) for every k, but O(n) instead of O(n^3).  It
    threads the fire-once latch and the loop window once, left to right,
    exactly as the live _augment_output does.

    Equivalence to the pure path is asserted by the parity tests; this is purely
    a performance route for full-trajectory replay (the single-turn oracle_decide
    remains the pure reference)."""
    if states is None:
        states = _sense.stream_states(turns)  # states[k] = sense(turns[:k+1])
    out: list[tuple[str, str, float] | None] = []
    fired: set[str] = set()
    loop_window: list[str] = []
    # streaming failure-signature history (mirrors _test_fail_history.append).
    fail_hist: list[str] = []
    runner_count = 0  # real-test-runner turns (non-env) — failure-conf denominator
    # Stage-5 (non-parity) baseline-failure state (mirrors the live gates).
    stream_stash = 0
    baseline_sigs: set[str] = set()
    for k, turn in enumerate(turns):
        st = states[k]
        cmd = turn.command
        raw = turn.raw_obs
        decision: tuple[str, str, float] | None = None

        # 1. l5_stuck (loop OR scaffold_trap)
        norm = (cmd or "").strip()
        if "l5_stuck" not in fired:
            if norm:
                sig = _loop_signature(cmd, raw)
                loop_window.append(sig)
                if len(loop_window) > 12:
                    del loop_window[0]
                n_sig = loop_window.count(sig)
                if n_sig >= 4:
                    decision = ("loop", _NUDGE_LOOP, n_sig / len(loop_window))
            if decision is None and st.action_count >= 25 and st.source_edit_count == 0:
                conf = (st.action_count - st.source_edit_count) / st.action_count
                decision = ("scaffold_trap", _NUDGE_SCAFFOLD, conf)
        elif norm:
            # keep the window coherent even after the latch (matches live append).
            loop_window.append(_loop_signature(cmd, raw))
            if len(loop_window) > 12:
                del loop_window[0]

        # 2. failure_persisted (runner_count threads the non-env runner total so
        #    the streamed confidence equals the pure path's recount).  An
        #    EMPTY-observation runner turn (timeout/SIGKILL -> no output) still
        #    counts in the denominator — the pure reference (_l5_raw_fire's
        #    recount) includes it; dropping it inflated the streamed confidence
        #    (LIPI 2026-06-10).
        if "failure_persisted" not in fired and _gmp is not None:
            turn_is_stashed = False
            if not parity_mode:
                # single-turn `git stash && test … ; git stash pop` disproofs
                # count as baseline windows (mirrors the live governor).
                pushes = len(_gmp._STASH_PUSH_RE.findall(cmd or ""))
                pops = len(_gmp._STASH_POP_RE.findall(cmd or ""))
                turn_is_stashed = stream_stash > 0 or pushes > 0
                stream_stash = max(0, stream_stash + pushes - pops)
            if (_gmp._TEST_RUNNER_RE.search(cmd or "")
                    and not _gmp._ENV_FAIL_RE.search(raw or "")):
                runner_count += 1
                sig = _fail_sig(raw, parity_mode)
                if sig:
                    if not parity_mode and (st.source_edit_count == 0
                                            or turn_is_stashed):
                        baseline_sigs.add(sig)  # repo's own state, never fires
                    elif not parity_mode and sig in baseline_sigs:
                        pass  # pre-existing failure: excluded from history
                    else:
                        fail_hist.append(sig)
                        n_rec = fail_hist.count(sig)
                        if (decision is None and n_rec >= 2
                                and st.source_edit_count >= 1):
                            decision = ("failure_persisted", _NUDGE_FAILURE,
                                        n_rec / runner_count)

        # 3. no_test_evidence
        if decision is None and "no_test_evidence" not in fired and _gmp is not None:
            if _gmp._TEST_RUNNER_RE.search(cmd or ""):
                text = raw or ""
                has_result = bool(_gmp._TEST_FAIL_RE.search(text)
                                  or _gmp._TEST_PASS_RE.search(text))
                is_env = bool(_gmp._ENV_FAIL_RE.search(text))
                is_compile = bool(_gmp._COMPILE_FAIL_RE.search(text))
                if (not has_result and not is_env and not is_compile
                        and st.blind_test_runs >= 2 and not st.test_evidence_seen
                        and st.source_edit_count >= 1):
                    conf = st.blind_test_runs / max(len(st.test_runs), 1)
                    decision = ("no_test_evidence", _NUDGE_NO_TEST, conf)

        if decision is not None:
            reason = decision[0]
            fired.add("l5_stuck" if reason in ("loop", "scaffold_trap") else reason)
        out.append(decision)
    return out


def _decide_walk(turns, obligations: list[dict] | None,
                 states=None, payload_pool: dict[int, list[Candidate]] | None = None,
                 anchors: set[str] | None = None,
                 parity_mode: bool = True) -> list[OracleDecision]:
    """The combined per-turn decision walk: L5 candidates (Stage 2, unchanged)
    + obligation candidates at their Stage-3 trigger + whitelist-only drift
    production (always suppressed while uncertified).

    Stage-3 trigger — the anchor-aware test-evidence gap (plan §3.1 / the
    Rank-1 lever): an obligation candidate fires at the REVIEW TRANSITION edge
    (the proven GT_VERIFY predicate: >=1 source edit then >=3 non-edit actions)
    when (a) its symbols intersect the sensed EDIT evidence (edited?) and
    (b) they do NOT intersect any observed test output (tested? == False).
    DOSE (conservative, pre-calibration): the obligation CLASS emits at most
    ONCE per task in Stage 3.  The class is unvalidated for consumption (the
    §11 calibration gap); over-fire is harm with receipts (csstree detour,
    2026-05-25 "MORE GT = WORSE"), and a 40-obligation issue would otherwise
    nudge at every review round.  One anchored, severity-ranked re-surface at
    the first qualifying review transition is the correct-or-quiet dose until
    Stage-6 calibration certifies more on held-out data.  After the class
    emission, further triggers are recorded `over_budget`; the emitted
    obligation itself is `delivered`."""
    if states is None:
        states = _sense.stream_states(turns)
    fires = _l5_fire_streaming(turns, states, parity_mode)
    views = _obligation_views(obligations) if obligations else []
    emitted_oblig: set[int] = set()
    emitted_hashes: set[str] = set()  # Stage-4 dedup (the oracle's OWN emissions)
    oblig_class_spent = False
    out: list[OracleDecision] = []
    prev_phase = "ORIENT"
    for k, turn in enumerate(turns):
        st = states[k]
        telemetry: list[SuppressionRecord] = []
        triggered: list[Candidate] = []

        # L5 family (unchanged Stage-2 semantics).
        if fires[k] is not None:
            reason, payload, conf = fires[k]
            triggered.append(_l5_candidate(reason, payload, conf))

        # SPEC obligations: trigger only on the REVIEW-transition edge.
        review_edge = (st.phase == "REVIEW" and prev_phase != "REVIEW")
        edited_tokens = getattr(st, "edited_tokens", set()) or set()
        tested_tokens = getattr(st, "tested_tokens", set()) or set()
        for v in views:
            touched, conf = _overlap(v, edited_tokens)
            tested = _obligation_tested(v, tested_tokens)
            if v.idx in emitted_oblig:
                telemetry.append(SuppressionRecord(
                    candidate_id=f"spec.obligation.{v.idx}", eligible=False,
                    suppressed_reason="delivered",
                    severity_score=_severity_score("obligation"),
                    confidence=float(conf), relevance_overlap=float(conf),
                ))
            elif review_edge and touched and not tested:
                if oblig_class_spent:
                    telemetry.append(SuppressionRecord(
                        candidate_id=f"spec.obligation.{v.idx}", eligible=False,
                        suppressed_reason="over_budget",
                        severity_score=_severity_score("obligation"),
                        confidence=float(conf), relevance_overlap=float(conf),
                    ))
                else:
                    triggered.append(_obligation_candidate(v, touched, conf))
            else:
                telemetry.append(SuppressionRecord(
                    candidate_id=f"spec.obligation.{v.idx}", eligible=False,
                    suppressed_reason="no_trigger",
                    severity_score=_severity_score("obligation"),
                    confidence=float(conf), relevance_overlap=float(conf),
                ))

        # DRIFT: whitelist-only, produced for audit, NEVER emitted uncertified.
        if views and not DRIFT_CERTIFIED:
            telemetry.extend(_drift_records(views, turn, st, k))

        # STAGE 4 — layer payload candidates (L1/L3/L3b/consensus), gated:
        #   RELEVANCE: keys ∩ focus (anchors ∪ unmet obligations ∪ edit set),
        #     except L3 candidates EDIT-BOUND to this turn (their trigger IS
        #     the edit — relevance by construction);
        #   DEDUP: the oracle's own emitted content-hash set (rearm_on_change:
        #     changed content carries a new hash and re-arms);
        #   BUDGET: the shared <=1/turn winner gate below.
        if payload_pool:
            focus = _focus_set(st, anchors or set(), views, tested_tokens)
            edited_this_turn = any(e.turn_index == k for e in st.edit_events)
            for c in payload_pool.get(k, ()):  # candidates recorded AT turn k
                if c.content_hash in emitted_hashes:
                    telemetry.append(SuppressionRecord(
                        candidate_id=c.id, eligible=False,
                        suppressed_reason="delivered",
                        severity_score=_severity_score(c.severity),
                        confidence=0.0, relevance_overlap=0.0,
                    ))
                    continue
                matched = c.relevance_keys & focus
                edit_bound = (c.layer == "L3" and edited_this_turn)
                if not edit_bound and not matched:
                    telemetry.append(SuppressionRecord(
                        candidate_id=c.id, eligible=False,
                        suppressed_reason="irrelevant",
                        severity_score=_severity_score(c.severity),
                        confidence=0.0, relevance_overlap=0.0,
                    ))
                    continue
                conf = (len(matched) / len(focus)) if focus else (
                    1.0 if edit_bound else 0.0)
                triggered.append(_dc_replace(c, confidence=float(conf)))

        winner, gate_telemetry = gate_pool(triggered)
        telemetry.extend(gate_telemetry)
        prev_phase = st.phase
        if winner is None:
            out.append(OracleDecision(turn_index=k, emission=None,
                                      telemetry=telemetry))
            continue
        if winner.layer in ("L1", "L3", "L3b", "consensus"):
            emitted_hashes.add(winner.content_hash)
        if winner.layer == "spec":
            emitted_oblig.add(int(winner.id.rsplit(".", 1)[1]))
            oblig_class_spent = True  # the once-per-task class dose (above)
        emission = Emission(
            candidate_id=winner.id, layer=winner.layer, channel="C1",
            payload_text=winner.content, dose_tokens=winner.dose_cost,
            trigger_matched=winner.trigger, reason_string=winner.kind,
        )
        out.append(OracleDecision(turn_index=k, emission=emission,
                                  telemetry=telemetry))
    return out


def replay(turns, *, parity_mode: bool = True,
           obligations: list[dict] | None = None) -> list[OracleDecision]:
    """Run the oracle over a full trajectory, statelessly, returning the per-turn
    decisions.  This is the Stage-2 byte-parity instrument (without obligations)
    and the Stage-3 replay instrument (with them).  Uses the streaming L5 pass
    for performance; the per-turn emission/telemetry shape is identical to
    oracle_decide.

    parity_mode=True (default): byte-parity with the FROZEN corpus — the
    historical predicates, never to drift (the Stage-2 contract).
    parity_mode=False: the CORRECTED product semantics (delivery-engine
    Stage 5 failure_persisted FP closure) — what the live patch now does."""
    return _decide_walk(turns, obligations, parity_mode=parity_mode)


# ---------------------------------------------------------------------------
# Telemetry persistence (plan §1.4): per-candidate suppression records, ALWAYS
# (even on silence), 8-dp precision — gt_oracle_events_<task>.jsonl.
# ---------------------------------------------------------------------------
def _f8(x: float) -> float:
    """8-dp float per the deep-logging rule (full precision, fixed places)."""
    return float(f"{float(x):.8f}")


def write_oracle_events(decisions, path: str) -> None:
    """Serialize per-turn oracle decisions (emission + full suppression
    telemetry) to a JSONL file, one line per turn, all scores at 8dp."""
    with open(path, "w", encoding="utf-8") as f:
        for d in decisions:
            rec = {
                "turn_index": d.turn_index,
                "emission": None if d.emission is None else {
                    "candidate_id": d.emission.candidate_id,
                    "layer": d.emission.layer,
                    "channel": d.emission.channel,
                    "dose_tokens": d.emission.dose_tokens,
                    "trigger_matched": d.emission.trigger_matched,
                    "reason_string": d.emission.reason_string,
                },
                "telemetry": [
                    {
                        "candidate_id": t.candidate_id,
                        "eligible": t.eligible,
                        "suppressed_reason": t.suppressed_reason,
                        "severity_score": _f8(t.severity_score),
                        "confidence": _f8(t.confidence),
                        "relevance_overlap": _f8(t.relevance_overlap),
                    }
                    for t in d.telemetry
                ],
            }
            f.write(_json.dumps(rec) + "\n")


def replay_nudge_reasons(turns, *, parity_mode: bool = True) -> list[str]:
    """The ordered list of nudge reasons the oracle emits over a trajectory — the
    direct byte-parity comparand against the recorded <gt-nudge reason=...> set."""
    out: list[str] = []
    for d in replay(turns, parity_mode=parity_mode):
        if d.emission is not None:
            out.append(d.emission.reason_string)
    return out  # parity_mode threads through replay (Stage-5 corrections off by default)


__all__ = [
    "Candidate", "Emission", "SuppressionRecord", "OracleDecision",
    "oracle_decide", "replay", "replay_nudge_reasons",
    "distribution_floor", "gate_pool", "load_obligations",
    "write_oracle_events", "delivered_payload_candidates", "stage4_replay",
    "DRIFT_CERTIFIED",
    # Stage-2 obligation-status machinery (delivery engine 2026-06-11)
    "obligation_statuses", "status_vector_hash", "composite_severity",
    "order_unmet", "render_obligation_status_block",
    "OBL_TESTED", "OBL_EDITED_UNTESTED", "OBL_UNADDRESSED",
    "Obligation", "ObligationTracker",
]
