"""A view must put the SYMBOLS it showed into focus, not only the file.

THE DEFECT (the largest single cause of GT's silence, verified 2026-07-27).

`work_state.focused_symbols` is PERMANENTLY EMPTY on this harness. The only write is the
`SemanticKind.SYMBOL_VIEWED` branch of the reducer (`reasoning_runtime.py:506-509`), which is
reached only from `ActionOperation.VIEW_SYMBOL` (`adapters/miniswe.py:238`), which
`_native_action_operation` (`gt_mini_patch.py:21894-21930`) can never return -- it emits only
EDIT / VIEW_SOURCE / SEARCH / TEST / SUBMIT / OTHER. Nothing anywhere in the repo assigns it.

That empty set kills features by TWO INDEPENDENT mechanisms:

  1. THE RELEVANCE GATE. `evaluate_feature_contract` (`reasoning_runtime.py:4765`) requires
     `active_semantic_nodes & evidence_semantic_nodes != {}`. Active nodes reduce to
     `{obligation:task} | {subject:<focused_file>}`; evidence nodes to `{fact:X, subject:Y}`
     (`gateway.py:1840-1844`), and `fact:` can never match. So the gate is `subject:` string
     equality against `focused_files`, and EVERY record whose canonical_subject is a SYMBOL is
     HELD forever -- def_partition (4 variants), newfile_precedent, signature_delta,
     caller_contract's edit form, localization's trace_frame. There is no escape: the composer's
     graph fallback (`:5781-5790`) is dead because node ids are minted `hyp:{subject}` (`:1110`),
     never `subject:`.
  2. PRODUCER STARVATION. `covering_red` never even reaches the gate:
     `symbols = set(work_state.focused_symbols)` (`gt_mini_patch.py:21673`) is empty, so
     `select_covering_tests` fail-closes (`covering_runner.py:171-173`) and the whole execution
     block is skipped.

WHY NOT PARSE THE SHELL COMMAND (user directive). Recovering "which symbol was viewed" from
`sed -n '100,140p' foo.py` or an arbitrary grep pipeline is SEMANTIC READING. GT is LLM-free and
deterministic by mandate, so that is not solvable in general. Authority must come from the
operation and repository state -- here, resolving the viewed file against `graph.db` -- and NEVER
from parsing rendered output.

THE FIX CONTRACTED HERE. `CanonicalResult` carries the symbols the operation actually showed, and
the adapter emits one `SYMBOL_VIEWED` per symbol ALONGSIDE the file's `SOURCE_VIEWED`. This mirrors
the existing `files_hit` field exactly (declared `reasoning_runtime.py:214`, emitted
`adapters/miniswe.py:232-233`), and the adapter already proves one action may emit several outcomes
-- EDIT emits `EDIT_EXECUTED` + `DIFF_CREATED` (`adapters/miniswe.py:243-247`).

NOT A BEHAVIOUR CHANGE FOR THE AGENT. The agent keeps issuing ordinary shell commands; only GT's
derivation of what they MEANT becomes authoritative.
"""

from __future__ import annotations

from groundtruth.runtime import reasoning_runtime as rr
from groundtruth.runtime.adapters import miniswe
from groundtruth.runtime.gateway import ToolEvent


REVISION = rr.RevisionVector(
    repository_content="repo-focus",
    graph="graph-focus",
    lsp="lsp-focus",
    runtime_evidence="runtime-focus",
)

VIEWED_FILE = "src/pkg/session.py"


def _view_pair(*, viewed_symbols: tuple[str, ...], seq: int = 1):
    """One canonical proposal/result pair for a VIEW of `VIEWED_FILE`.

    `seq` exists because `reduce_event` enforces sequence continuity: a test folding two views
    into one state must advance it, or the second pair is rejected as a gap.
    """
    action = rr.CanonicalAction(
        action_id=f"action-focus-{seq}",
        operation=rr.ActionOperation.VIEW_SOURCE,
        tool_family="shell",
        tool_name="mini-swe",
        structured_operation="view",
        subject=VIEWED_FILE,
        query="",
        targets=(VIEWED_FILE,),
        raw_command=f"sed -n '1,80p' {VIEWED_FILE}",
    )
    proposal = miniswe.canonicalize_action_proposal(
        action,
        event_id=f"event-focus-proposal-{seq}",
        attempt_id="attempt-focus",
        sequence=seq,
        model_turn_id="call-focus",
        observation_id="obs-focus",
        revision=REVISION,
        previous_event_hash="",
    )
    result = miniswe.canonicalize_tool_result(
        ToolEvent(
            kind="view",
            carrier_kind="view",
            command=f"sed -n '1,80p' {VIEWED_FILE}",
            output="def refresh_session(token):\n    ...",
            exit_status=0,
            semantic_events=(),
            semantics_authoritative=True,
        ),
        proposal=proposal,
        result=rr.CanonicalResult(
            status="success",
            exit_code=0,
            viewed_symbols=viewed_symbols,
        ),
        event_id=f"event-focus-result-{seq}",
        sequence=seq + 1,
        observation_id="obs-focus",
        revision_after=REVISION,
        previous_event_hash=proposal.content_hash,
    )
    return proposal, result


def _work_state() -> rr.WorkState:
    return rr.WorkState(attempt_id="attempt-focus", revision=REVISION)


def _reduce(pair, state: "rr.WorkState | None" = None) -> rr.WorkState:
    """Fold a proposal/result pair through the REAL reducer, in order.

    `reduce_event` enforces sequence continuity, so the proposal must be folded before its
    result -- feeding the result alone raises `StateIntegrityError`. Driving the real reducer
    (rather than hand-constructing a WorkState) is the point: a fixture that skips it would
    prove the adapter emits an outcome while saying nothing about whether focus receives it.
    """
    proposal, result = pair
    state = _work_state() if state is None else state
    return rr.reduce_event(rr.reduce_event(state, proposal), result)


def _kinds(result) -> list:
    return [o.kind for o in result.outcomes]


def _subjects_for(result, kind) -> set[str]:
    return {o.subject for o in result.outcomes if o.kind is kind}


def test_canonical_result_can_carry_the_symbols_the_view_showed():
    """The field must exist, mirroring `files_hit`. Without it the operation has no way to
    report what it actually displayed, and the seam is forced back to parsing stdout."""
    probe = rr.CanonicalResult(status="success", viewed_symbols=("refresh_session",))
    assert probe.viewed_symbols == ("refresh_session",)


def test_the_field_is_normalized_to_a_tuple_like_its_siblings():
    """ADDED BECAUSE A MUTATION SURVIVED.

    Deleting the `__post_init__` normalization left every behavioural test green: they all pass
    tuples already. But `CanonicalResult` is a FROZEN dataclass whose instances are hashed and
    compared as part of canonical event identity, and `files_hit`/`changed_files` are both
    normalized for exactly that reason. A caller passing a list would produce an unhashable
    result and a mutable field inside a frozen, hash-chained event -- a defect that would not
    surface until an event journal write, far from this seam.
    """
    probe = rr.CanonicalResult(status="success", viewed_symbols=["a", "b"])
    assert probe.viewed_symbols == ("a", "b")
    assert isinstance(probe.viewed_symbols, tuple), (
        "viewed_symbols was not normalized to a tuple -- unlike files_hit and changed_files"
    )
    hash(probe)  # frozen dataclass identity must survive a list argument


def test_a_view_emits_symbol_viewed_for_each_symbol_it_showed():
    """THE FIX. One action, several outcomes -- exactly as EDIT already does."""
    _proposal, result = _view_pair(viewed_symbols=("refresh_session", "TokenStore"))
    kinds = _kinds(result)
    assert rr.SemanticKind.SOURCE_VIEWED in kinds, "the file-level outcome must survive"
    assert kinds.count(rr.SemanticKind.SYMBOL_VIEWED) == 2, (
        f"expected one SYMBOL_VIEWED per resolved symbol, got kinds={kinds}"
    )
    assert _subjects_for(result, rr.SemanticKind.SYMBOL_VIEWED) == {
        "refresh_session", "TokenStore"
    }, "the symbol outcomes must carry the SYMBOL as subject, not the file path"


def test_the_reducer_puts_those_symbols_into_focus():
    """THE POINT. `focused_symbols` is what the :4765 intersection and covering-test selection
    both read; a symbol outcome that does not reach it changes nothing."""
    state = _reduce(_view_pair(viewed_symbols=("refresh_session", "TokenStore")))
    assert set(state.focused_symbols) == {"refresh_session", "TokenStore"}, (
        f"focused_symbols={state.focused_symbols!r} -- symbol-subject evidence stays HELD "
        "forever at reasoning_runtime.py:4765 and covering_red stays starved"
    )


def test_the_file_still_reaches_focus_too():
    """POSITIVE CONTROL + ANTI-REGRESSION. `focused_files` is the ONLY thing that works today
    (it is why caller_contract's view form is the one healthy path). It must not be traded away
    for the symbols."""
    state = _reduce(_view_pair(viewed_symbols=("refresh_session",)))
    assert VIEWED_FILE in state.focused_files, (
        "the viewed FILE dropped out of focus -- this would break the one delivery path that "
        "currently works"
    )


def test_no_resolved_symbols_means_no_extra_outcomes():
    """CORRECT-OR-QUIET, and the anti-weakening line. When graph.db cannot resolve the viewed
    region, the operation must say NOTHING extra rather than guess a symbol from the path or the
    output text. A wrong symbol in focus is worse than an empty one: it would admit unrelated
    evidence through the relevance gate, which is precisely the failure mode `correct-or-quiet`
    exists to prevent."""
    pair = _view_pair(viewed_symbols=())
    result = pair[1]
    kinds = _kinds(result)
    assert kinds == [rr.SemanticKind.SOURCE_VIEWED], (
        f"unresolved view emitted more than the file outcome: {kinds}"
    )
    state = _reduce(pair)
    assert state.focused_symbols == (), (
        f"a symbol was invented with nothing to resolve from: {state.focused_symbols!r}"
    )


def test_symbols_are_deduplicated_and_order_preserved():
    """The reducer appends uniquely (`_append_unique`); repeated views of the same file must not
    grow focus without bound, or the active neighbourhood degrades into a match-anything set."""
    state = _reduce(_view_pair(viewed_symbols=("refresh_session", "TokenStore")))
    second = _view_pair(viewed_symbols=("TokenStore", "refresh_session"), seq=3)
    state = _reduce(second, state)
    assert list(state.focused_symbols) == ["refresh_session", "TokenStore"], (
        f"focus is not deduplicated/stable: {state.focused_symbols!r}"
    )
