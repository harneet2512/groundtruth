"""W4 content guards — pure, deterministic recovery/obligation steering guards.

Four generalized guards, each grounded in a measured smoke30-ss128 false-fire and
each PURE / DETERMINISTIC / LLM-FREE / stdlib-only, so the deterministic Stage-1
core is unit-testable in isolation and the seam wires each behind its own flag
(byte-identical when off). Guard 1 (infra/teardown noise) lives in
:mod:`groundtruth.runtime.patterns` (``is_infra_noise``) next to the other
classification patterns; the remaining three live here.

  * GUARD 2 — HYPOTHESIS-CONTRADICTION. A recovery steer must not assert "your
    hypothesis is likely wrong, reconsider the target file" when the agent's
    CURRENT target agrees with GT's own independent evidence. The measured
    false-fire (amoffat/sh-744, BAD_INFO_RESISTED): the agent's hypothesis was
    RIGHT, the target matched the issue anchors, yet l5.failure told it to
    reconsider the target. Gold-agnostic: the signal is the graph/issue-anchor
    AGREEMENT between the edited target's tokens and the issue anchors — never a
    gold label. Fail toward silence (suppress the wrong-target steer) when they
    agree.

  * GUARD 3 — RECOVERY FORM AT REPEAT-N. A recovery nudge that was correct but
    IGNORED (dynaconf: 3 correct thrash warnings ignored into budget death — the
    form was too weak) must ESCALATE on repeat from an advisory to the
    proven-consumed SHORT · ACTIVE (imperative) form, and must NEVER be weaker on
    a later repeat than an earlier one (monotone strength).

  * GUARD 4 — OBLIGATION STEERING. An obligations/scope block may quote the issue
    requirements but must not EXCLUDE files or symbols that another GT surface
    (localization) ranks highly (aiogram: the obligation narrowing steered the
    agent away from rank-1 gold ``scene.py``). Fail toward silence: when narrowing
    to the obligation set would drop a top-ranked localization file, do not narrow.

All functions are correct-or-quiet, order-independent, and free of time/randomness/
I/O so they are byte-stable across processes and seeds.
"""

from __future__ import annotations


__all__ = [
    # guard 2 — hypothesis contradiction
    "target_corroboration_score",
    "target_is_gt_corroborated",
    # guard 3 — recovery escalation
    "recovery_escalation_level",
    "escalate_recovery_form",
    "recovery_form_strength",
    # guard 4 — obligation steering
    "obligation_narrowing_excludes_ranked",
    "guarded_obligation_symbols",
]


def _norm(p: str) -> str:
    """Normalize a path token for set membership (slashes + leading ./ folded)."""
    return (p or "").replace("\\", "/").strip().lstrip("./")


# --------------------------------------------------------------------------- #
# GUARD 2 — hypothesis-contradiction guard
# --------------------------------------------------------------------------- #
def target_corroboration_score(
    target_tokens: "set[str] | list[str] | tuple[str, ...] | None",
    issue_anchors: "set[str] | list[str] | tuple[str, ...] | None",
) -> int:
    """The graph/issue-anchor AGREEMENT score for the agent's current target: the
    number of the edited target's tokens (symbol names / file stem) that appear in
    the issue anchors. Gold-agnostic — the anchors are GT's own issue-derived
    surface, never a gold label. 0 when either side is empty (no evidence -> no
    corroboration -> the recovery steer is NOT suppressed)."""
    tt = {t for t in (target_tokens or ()) if isinstance(t, str) and t}
    ia = {a for a in (issue_anchors or ()) if isinstance(a, str) and a}
    if not tt or not ia:
        return 0
    return len(tt & ia)


def target_is_gt_corroborated(
    target_tokens: "set[str] | list[str] | tuple[str, ...] | None",
    issue_anchors: "set[str] | list[str] | tuple[str, ...] | None",
    *,
    min_agreement: int = 1,
) -> bool:
    """True when the agent's CURRENT target is corroborated by GT's independent
    issue-anchor evidence at or above ``min_agreement`` — the condition under which
    a "your hypothesis is likely wrong / reconsider the target file" steer must be
    SUPPRESSED (fail toward silence: a wrong-target steer against a right target is
    worse than none). Conservative: needs at least one shared anchor token; an
    empty-evidence case is never corroborated, so genuine recovery still fires."""
    return target_corroboration_score(target_tokens, issue_anchors) >= max(1, int(min_agreement))


# --------------------------------------------------------------------------- #
# GUARD 3 — recovery FORM at repeat-N (escalate, never weaker)
# --------------------------------------------------------------------------- #
# The monotone escalation lead for each level. Level 0 is the base (byte-identical
# first fire). Each higher level is a STRICTLY stronger short-active imperative
# prefix (the proven-consumed SHORT · ACTIVE form) — never softened on repeat.
_ESCALATION_LEADS: dict[int, str] = {
    1: "Change your approach now —",
    2: "This has failed repeatedly; stop and change your approach immediately —",
}
_MAX_ESCALATION_LEVEL = max(_ESCALATION_LEADS)


def recovery_escalation_level(repeat_n: int) -> int:
    """The escalation LEVEL for the ``repeat_n``-th delivery of a recovery class
    (1-based). MONOTONE non-decreasing in ``repeat_n``: 1st delivery -> 0 (base),
    2nd -> 1, 3rd+ -> 2 (capped). This ordinal is the "never weaker on repeat"
    invariant made explicit and testable."""
    try:
        n = int(repeat_n)
    except (TypeError, ValueError):
        return 0
    if n <= 1:
        return 0
    return min(n - 1, _MAX_ESCALATION_LEVEL)


def recovery_form_strength(text: str) -> int:
    """An ordinal STRENGTH of a rendered recovery form, so the monotone invariant is
    checkable on the emitted text (not just the level): 0 when it carries no
    escalation lead, else the level of the highest lead it starts with. Used by the
    Stage-1 tests to prove escalate_recovery_form is never weaker on a later
    repeat."""
    t = (text or "").strip()
    best = 0
    for lvl, lead in _ESCALATION_LEADS.items():
        if t.startswith(lead):
            best = max(best, lvl)
    return best


def escalate_recovery_form(base_imperative: str, repeat_n: int) -> str:
    """Return the recovery imperative for the ``repeat_n``-th delivery, escalating
    advisory -> imperative short-active on repeat and NEVER weaker than an earlier
    repeat. ``repeat_n <= 1`` (or empty base) returns the base VERBATIM — the first
    fire is byte-identical, so a single-shot delivery is unchanged. A repeat
    prepends the monotone short-active lead for its level; the base action is
    preserved so no information is lost, only urgency added. Whitespace in the base
    is collapsed to keep the escalated form a single short line (the consumed
    form is SHORT)."""
    b = " ".join((base_imperative or "").split())
    lvl = recovery_escalation_level(repeat_n)
    if lvl == 0 or not b:
        return b
    return f"{_ESCALATION_LEADS[lvl]} {b}"


# --------------------------------------------------------------------------- #
# GUARD 4 — obligation steering guard
# --------------------------------------------------------------------------- #
def obligation_narrowing_excludes_ranked(
    obligation_files: "set[str] | list[str] | tuple[str, ...] | None",
    ranked_top_files: "set[str] | list[str] | tuple[str, ...] | None",
) -> bool:
    """True when narrowing attention to ``obligation_files`` would EXCLUDE a file
    that GT localization ranks in its top set — the adverse "narrow away from the
    rank-1 gold file" shape. Only meaningful when the obligation block actually
    names files (an empty obligation set narrows nothing -> False) and localization
    has a ranked top set. Path-normalized set difference; order-independent."""
    of = {_norm(f) for f in (obligation_files or ()) if _norm(f)}
    rt = {_norm(f) for f in (ranked_top_files or ()) if _norm(f)}
    if not of or not rt:
        return False
    return bool(rt - of)


def guarded_obligation_symbols(
    obligation_symbols: "set[str] | list[str] | tuple[str, ...] | None",
    ranked_top_symbols: "set[str] | list[str] | tuple[str, ...] | None",
) -> set[str]:
    """The obligation symbol set used to NARROW ambient producers, GUARDED so it can
    never EXCLUDE a symbol from a top-ranked localization file. Fail toward silence:
    when the raw obligation set would drop a ranked-top symbol, UNION the ranked-top
    symbols in (so the narrowing keeps them) rather than let the requirement quote
    steer attention away from what localization ranks highest. When the obligation
    set is empty (no narrowing requested) it is returned unchanged — the guard adds
    nothing where there is nothing to narrow."""
    os_ = {s for s in (obligation_symbols or ()) if isinstance(s, str) and s}
    rt = {s for s in (ranked_top_symbols or ()) if isinstance(s, str) and s}
    if not os_:
        return os_
    return os_ | rt
