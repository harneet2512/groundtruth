"""A search for a REAL symbol puts that symbol in play — without advancing the phase.

WHY. `_produce_def_ref_partition` sets `canonical_subject = search_pattern(event.command)` — the
bare symbol from the command's own argument. The relevance gate reduces to `subject:` equality
against focus, so if that same symbol is in `focused_symbols` the intersection MATCHES on the
`search_result` boundary the fact is contracted to. Without it, def_partition's four variants are
delivered only later (once the agent happens to open the defining file, via the view path) or not
at all — i.e. off-boundary, which is a link-3 failure even when delivery eventually happens.

AUTHORITY, and why this is not the banned channel. The operand is a LITERAL command argument, not
an inference from rendered output: `search_pattern` returns it only when it is a bare symbol
(>=3 chars, no metacharacters or path separators) and abstains otherwise. It is then VALIDATED
against `graph.db` — a symbol enters focus only if the graph holds a non-test definition for it.
A typo, a log string or a regex therefore resolves to nothing. No output text is ever parsed.

THE PHASE TRAP THIS AVOIDS. The obvious carrier — emitting `SYMBOL_VIEWED` — is WRONG. Its
reducer branch sets `phase = Phase.UNDERSTANDING`, so every search would falsely advance the
trajectory out of DISCOVERY/LOCALIZATION, corrupting `_active_decision` (which derives the open
decision from the phase) and therefore the oracle's notion of which decision is open. Searching is
not understanding. The symbol rides `SEARCH_RESULT` metadata instead, and the SEARCH branch adds
it to focus while leaving phase handling exactly as it was.
"""

from __future__ import annotations

from groundtruth.runtime import reasoning_runtime as rr
from groundtruth.runtime.adapters import miniswe
from groundtruth.runtime.gateway import ToolEvent


REVISION = rr.RevisionVector(
    repository_content="repo-s", graph="graph-s", lsp="lsp-s", runtime_evidence="rt-s",
)


def _search_pair(*, resolved_symbols: tuple[str, ...], hit_count: int = 4, seq: int = 1):
    action = rr.CanonicalAction(
        action_id=f"a-s-{seq}",
        operation=rr.ActionOperation.SEARCH,
        tool_family="shell",
        tool_name="mini-swe",
        structured_operation="search",
        subject="grep -rn refresh_session .",
        query="refresh_session",
        targets=(),
        raw_command="grep -rn refresh_session .",
    )
    proposal = miniswe.canonicalize_action_proposal(
        action,
        event_id=f"e-s-p-{seq}",
        attempt_id="attempt-s",
        sequence=seq,
        model_turn_id="call-s",
        observation_id="obs-s",
        revision=REVISION,
        previous_event_hash="",
    )
    result = miniswe.canonicalize_tool_result(
        ToolEvent(
            kind="search", carrier_kind="search",
            command="grep -rn refresh_session .",
            output="src/pkg/session.py:10:def refresh_session(token):",
            exit_status=0, semantic_events=(), semantics_authoritative=True,
        ),
        proposal=proposal,
        result=rr.CanonicalResult(
            status="success", exit_code=0, hit_count=hit_count,
            resolved_symbols=resolved_symbols,
        ),
        event_id=f"e-s-r-{seq}",
        sequence=seq + 1,
        observation_id="obs-s",
        revision_after=REVISION,
        previous_event_hash=proposal.content_hash,
    )
    return proposal, result


def _reduce(pair, state=None):
    proposal, result = pair
    state = state or rr.WorkState(attempt_id="attempt-s", revision=REVISION)
    return rr.reduce_event(rr.reduce_event(state, proposal), result)


def test_canonical_result_carries_the_resolved_operand():
    probe = rr.CanonicalResult(status="success", resolved_symbols=("refresh_session",))
    assert probe.resolved_symbols == ("refresh_session",)
    assert isinstance(probe.resolved_symbols, tuple)


def test_a_resolved_search_operand_enters_focus():
    """THE FIX. def_partition's subject IS this symbol, so focus now intersects it on the
    search boundary rather than only after some later view."""
    state = _reduce(_search_pair(resolved_symbols=("refresh_session",)))
    assert "refresh_session" in state.focused_symbols, (
        f"focused_symbols={state.focused_symbols!r} -- def_partition stays off-boundary"
    )


def test_the_phase_is_NOT_advanced_by_a_search():
    """THE TRAP. Searching is not understanding. If this fails, `_active_decision` derives the
    wrong open decision from the wrong phase and the oracle reasons about the wrong moment."""
    state = _reduce(_search_pair(resolved_symbols=("refresh_session",)))
    assert state.phase is not rr.Phase.UNDERSTANDING, (
        f"a search advanced the phase to {state.phase} -- the SYMBOL_VIEWED carrier leaked in"
    )


def test_the_orientation_guard_still_sees_focus_AS_IT_WAS_BEFORE_this_search():
    """ADDED BECAUSE A MUTATION SURVIVED — and it is the hazard the production comment names.

    The SEARCH branch's phase guard reads `not focused_symbols`. If focus is extended BEFORE
    that guard runs, the very search that resolved the symbol suppresses
    `search_without_selected_symbol`, and the trajectory stays in ORIENTATION instead of
    advancing to DISCOVERY. That silently changes phase behaviour which PREDATES this feature,
    and phase drives `_active_decision` -- so the oracle would open a different decision.

    Moving the loop above the guard left every other test green, including the weaker
    "phase is not UNDERSTANDING" assertion. Order is part of the contract, so it is pinned here.
    """
    state = _reduce(_search_pair(resolved_symbols=("refresh_session",)))
    assert state.phase is rr.Phase.DISCOVERY, (
        f"phase={state.phase} -- the orientation guard saw the symbol this same search added, "
        "so it suppressed the DISCOVERY advance that a symbol-less search must produce"
    )
    # And the symbol still made it into focus on that same observation.
    assert "refresh_session" in state.focused_symbols


def test_an_unresolved_search_adds_nothing():
    """CORRECT-OR-QUIET. `search_pattern` abstains on regexes and phrases, and the graph
    rejects names it does not define -- both arrive here as an empty tuple, and neither may
    invent focus."""
    state = _reduce(_search_pair(resolved_symbols=()))
    assert state.focused_symbols == ()


def test_search_still_counts_as_a_search():
    """ANTI-REGRESSION on the branch being edited: the existing SEARCH bookkeeping must be
    untouched."""
    state = _reduce(_search_pair(resolved_symbols=("refresh_session",)))
    assert state.search_count == 1


def test_focus_from_search_is_deduplicated():
    """Repeated searches for the same symbol must not grow focus without bound -- every extra
    member widens what the relevance intersection admits."""
    state = _reduce(_search_pair(resolved_symbols=("refresh_session",)))
    state = _reduce(
        _search_pair(resolved_symbols=("refresh_session",), seq=3), state
    )
    assert list(state.focused_symbols) == ["refresh_session"]
