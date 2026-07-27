"""A search that FAILED to run is not evidence of interest in its operand.

FOUND BY ADVERSARIAL REVIEW. The `resolved_symbols` metadata was attached for all three search
kinds, and the reducer's metadata loop sits inside the branch covering all of them. So a grep that
errored -- a bad flag, an unreadable path, exit code 2 -- still pushed its operand into
`focused_symbols`, and focus feeds the relevance intersection that decides what evidence the model
may see.

WHERE THE LINE IS, and why it is not "drop everything that found nothing":

  SEARCH_FAILED   the command did not run. Its operand tells us nothing about what the agent is
                  working on -- the shell rejected the invocation. EXCLUDED.
  SEARCH_EMPTY    the command ran fine and found nothing. That IS interest: the agent looked for
                  a symbol the graph defines and looked in the wrong place. A zero-hit search is
                  one of the more informative things a trajectory contains. KEPT.
  SEARCH_RESULT   obviously kept.

Dropping SEARCH_EMPTY as well would discard exactly the searches where GT has the most to add.
The commit narrative said "the search that resolved the symbol"; this makes the code match that
claim without over-correcting into silence.
"""

from __future__ import annotations

from groundtruth.runtime import reasoning_runtime as rr
from groundtruth.runtime.adapters import miniswe
from groundtruth.runtime.gateway import ToolEvent


REVISION = rr.RevisionVector(
    repository_content="r", graph="g", lsp="l", runtime_evidence="rt",
)


def _pair(*, status: str, hit_count: int, seq: int = 1):
    action = rr.CanonicalAction(
        action_id=f"a-{seq}", operation=rr.ActionOperation.SEARCH,
        tool_family="shell", tool_name="mini-swe", structured_operation="search",
        subject="grep -rn sym .", query="sym", targets=(),
        raw_command="grep -rn sym .",
    )
    proposal = miniswe.canonicalize_action_proposal(
        action, event_id=f"e-p-{seq}", attempt_id="att", sequence=seq,
        model_turn_id="c", observation_id="o", revision=REVISION,
        previous_event_hash="",
    )
    result = miniswe.canonicalize_tool_result(
        ToolEvent(kind="search", carrier_kind="search", command="grep -rn sym .",
                  output="", exit_status=0 if status == "success" else 2,
                  semantic_events=(), semantics_authoritative=True),
        proposal=proposal,
        result=rr.CanonicalResult(
            status=status, exit_code=0 if status == "success" else 2,
            hit_count=hit_count, resolved_symbols=("refresh_session",),
        ),
        event_id=f"e-r-{seq}", sequence=seq + 1, observation_id="o",
        revision_after=REVISION, previous_event_hash=proposal.content_hash,
    )
    return proposal, result


def _focus(pair):
    proposal, result = pair
    state = rr.WorkState(attempt_id="att", revision=REVISION)
    return rr.reduce_event(rr.reduce_event(state, proposal), result).focused_symbols


def _kind(pair):
    return pair[1].outcomes[0].kind


def test_positive_control_a_successful_search_focuses_its_operand():
    """Without this the exclusion below could pass because the whole path is dead."""
    pair = _pair(status="success", hit_count=4)
    assert _kind(pair) is rr.SemanticKind.SEARCH_RESULT
    assert "refresh_session" in _focus(pair)


def test_an_empty_search_STILL_focuses_its_operand():
    """ANTI-OVER-CORRECTION. A search that ran and found nothing is real interest -- the agent
    looked for a symbol the graph defines, in the wrong place. Those are the searches GT most
    needs to answer; silencing them would trade a small over-claim for a large blind spot."""
    pair = _pair(status="success", hit_count=0)
    assert _kind(pair) is rr.SemanticKind.SEARCH_EMPTY
    assert "refresh_session" in _focus(pair), (
        "a zero-hit search stopped contributing focus -- over-corrected into silence"
    )


def test_a_FAILED_search_does_not_focus_its_operand():
    """THE FIX. The command did not run; its operand is not evidence of anything."""
    pair = _pair(status="failed", hit_count=0)
    assert _kind(pair) is rr.SemanticKind.SEARCH_FAILED
    assert _focus(pair) == (), (
        "a search that failed to execute still pushed its operand into focus, widening the "
        "relevance intersection on the strength of a command the shell rejected"
    )
