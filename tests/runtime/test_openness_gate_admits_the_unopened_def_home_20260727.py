r"""C12 — the relevance gate learns an OPENNESS rule: the def-home of a focused symbol.

THE DEFECT (characterized in `test_relevance_gate_is_a_possession_test_20260727.py`, still true for
the cases it pins): `evaluate_feature_contract` intersects evidence subjects against work-state
focus, and focus holds only what the agent ALREADY OPENED. `localization`'s subject is the ranked
top FILE — a file the agent has by definition not opened, because naming an unopened file IS the
feature (CLAUDE.md §3). So the gate was empty exactly when the feature had something to say.

`_resolved_search_symbols` had already put the graph-validated search OPERAND into focus, which
un-held SYMBOL-subject records (`def_partition`). FILE-subject records were left behind — the
ledger's own note: "today's symbol-focus fix un-held SYMBOL-subject records and does NOTHING for
FILE-subject records."

THE FIX, proven here end to end: the DEFINITION HOME of a focused symbol is also an active subject.
The agent searched `parse_url`; the graph says `parse_url` is defined in `src/pkg/urls.py`;
therefore evidence naming `src/pkg/urls.py` is evidence about the work in progress, even though the
agent has never opened it. That is deterministic graph truth — the relation the product is built on
— not a relaxed bar.

WHY THIS IS NOT "ADMIT EVERYTHING", which is the failure mode that would turn GT into the context
flood it exists to prevent. Four independent brakes, each pinned by a test below:
  1. the symbol must be DEFINED in the graph (a typo or a prose word resolves to nothing);
  2. a name resolving to >3 def files is AMBIGUOUS and contributes nothing;
  3. a total ceiling bounds the expansion, because focus is append-only with no eviction anywhere;
  4. unrelated evidence still intersects nothing and is still HELD.

METHOD NOTE. The pre-existing possession-test file is NOT edited. Its assertions remain true: with
focus holding only an opened FILE and no symbol, an arbitrary unopened file is still HELD — and it
SHOULD be, because in that state there is no inquiry to connect the evidence to, and nothing
distinguishes the "right" unopened file from `vendor/unrelated/thing.py`. Its `strict=True` xfail
therefore still xfails, and is addressed in that file rather than here.
"""

from __future__ import annotations

import dataclasses
import sqlite3
from pathlib import Path

import pytest

from artifact_deepswe import gt_mini_patch as seam
from groundtruth.runtime import reasoning_runtime as rr
from groundtruth.runtime.gateway import definition_files_for_symbol

REVISION = rr.RevisionVector(
    repository_content="repo-1",
    graph="graph-1",
    lsp="lsp-1",
    runtime_evidence="runtime-1",
)

SATISFIED = frozenset(
    {
        rr.TemporalPredicate.PRODUCER_COMPUTATION_COMPLETE,
        rr.TemporalPredicate.REVISION_DEPENDENCIES_CAPTURED,
        rr.TemporalPredicate.ACTIVE_DECISION_CONTEXT_MATCHES,
        rr.TemporalPredicate.ACTIVE_DECISION_ID_MATCHES,
        rr.TemporalPredicate.REASONING_GRAPH_CONNECTED,
        rr.TemporalPredicate.COMMITMENT_WINDOW_OPEN,
    }
)

SEARCHED = "parse_url"                 # the agent's graph-validated search operand
DEF_HOME = "src/pkg/urls.py"           # where the graph says it lives -- NEVER opened
UNRELATED = "vendor/unrelated/thing.py"
AMBIGUOUS = "helper"                   # deliberately defined in 4 files


def _graph(tmp_path: Path) -> str:
    db = tmp_path / "graph.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE nodes (id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT NOT NULL,
          name TEXT NOT NULL, qualified_name TEXT, file_path TEXT NOT NULL,
          start_line INTEGER, end_line INTEGER, signature TEXT, return_type TEXT,
          is_exported BOOLEAN DEFAULT 0, is_test BOOLEAN DEFAULT 0, language TEXT NOT NULL,
          parent_id INTEGER);
        CREATE TABLE edges (id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER NOT NULL,
          target_id INTEGER NOT NULL, type TEXT NOT NULL, source_line INTEGER,
          source_file TEXT, resolution_method TEXT, confidence REAL DEFAULT 0.0,
          metadata TEXT);
        """
    )
    rows = [
        ("Function", SEARCHED, DEF_HOME, 12, 0),
        # A TEST definition of the same name must never enter focus.
        ("Function", SEARCHED, "tests/test_urls.py", 4, 1),
        ("Function", AMBIGUOUS, "src/a.py", 3, 0),
        ("Function", AMBIGUOUS, "src/b.py", 3, 0),
        ("Function", AMBIGUOUS, "src/c.py", 3, 0),
        ("Function", AMBIGUOUS, "src/d.py", 3, 0),
        # start_line 0 must be rejected -- the predicate the two resolvers once disagreed on.
        ("Function", "ghost", "src/ghost.py", 0, 0),
    ]
    con.executemany(
        "INSERT INTO nodes (label,name,file_path,start_line,is_test,language) "
        "VALUES (?,?,?,?,?,'python')",
        rows,
    )
    con.commit()
    con.close()
    return str(db)


@pytest.fixture
def graph_db(tmp_path, monkeypatch) -> str:
    db = _graph(tmp_path)
    monkeypatch.setenv("GT_HOST_GRAPH_DB", db)
    # Graph rows are already repo-relative, so root translation is a no-op; pinning it keeps
    # the test independent of whatever gt_root.txt happens to hold on the machine.
    monkeypatch.setattr(seam, "_root", lambda: str(tmp_path))
    return db


def _decision(*, symbols=(), files=()) -> rr.ActiveDecision:
    work_state = dataclasses.replace(
        rr.WorkState.initial(attempt_id="attempt-1", revision=REVISION),
        focused_symbols=tuple(symbols),
        focused_files=tuple(files),
    )
    return seam.CanonicalRuntimeAttachment._active_decision(
        (), work_state, REVISION, ()
    )


def _record(feature_id: str, subject: str) -> rr.EvidenceRecord:
    """The EXACT three-node neighbourhood `gateway.py` mints -- and deliberately no
    `obligation:task`, whose presence in every other in-repo fixture masks the subject
    comparison entirely and is why a 2000-test suite never caught C12."""
    contract = rr.feature_contract_for(feature_id)
    return rr.EvidenceRecord(
        evidence_id=f"ev-{feature_id}",
        feature_id=feature_id,
        decision_context=contract.decision_context,
        roles=contract.roles,
        subject=subject,
        claim=f"{feature_id} claim about {subject}",
        actionable_consequence=f"act on the {feature_id} finding for {subject}",
        provenance=(f"{subject}:7",),
        grade=rr.EvidenceGrade.VERIFIED,
        revision=REVISION,
        causal_neighborhood=(
            f"decision:{contract.decision_context.value}",
            f"fact:{feature_id}",
            f"subject:{subject}",
        ),
        lifecycle=rr.EvidenceLifecycle.READY,
        fresh=True,
        already_visible=False,
        superseded=False,
        mandatory_reason=None,
        revision_dependencies=contract.revision_dependencies,
        token_cost=120,
        failure_prevention=3,
        causal_value=3,
        contradiction_resolution=0,
        anchoring_risk=0,
        observed_substrates=tuple(
            sorted(
                set(contract.fallback_policy.preferred_substrates)
                or set(contract.fallback_policy.fallback_substrates)
            )
        ),
    )


def _evaluate(record, decision):
    contract = rr.feature_contract_for(record.feature_id)
    return rr.evaluate_feature_contract(
        contract,
        record,
        rr.TemporalRuntimeContext(
            active_decision=decision,
            satisfied_predicates=SATISFIED,
            commitment_window=rr.CommitmentWindowState.OPEN,
            current_revision=REVISION,
            available_substrates=contract.fallback_policy.preferred_substrates,
        ),
        role_driven=False,
    )


# --------------------------------------------------------------------------- #
# The resolver.
# --------------------------------------------------------------------------- #
def test_positive_control_the_resolver_finds_the_def_home(graph_db):
    """Run FIRST. Every () below is unreadable until the resolver is shown able to return
    a non-empty answer on this graph."""
    assert definition_files_for_symbol(graph_db, SEARCHED) == (DEF_HOME,)


def test_resolver_excludes_test_definitions(graph_db):
    """`tests/test_urls.py` also defines the name. A test file entering focus would hand the
    relevance gate a leak vector, since focus is compared against evidence subjects.

    CALIBRATED, and the result corrects a natural assumption: deleting `COALESCE(is_test,0)=0`
    from the query does NOT make this test fail. Two independent guards cover this path and the
    `_is_leaky` path filter is the one actually holding it. That is defense in depth rather than
    redundancy — the SQL predicate keeps the resolver's row set identical to
    `defined_symbols_for_file`'s (pinned separately by the zero-start_line test), while
    `_is_leaky` is what keeps test identity out of focus. Do not "simplify" either away on the
    grounds that this test still passes without it.
    """
    assert "tests/test_urls.py" not in definition_files_for_symbol(graph_db, SEARCHED)


def test_resolver_abstains_on_an_ambiguous_name(graph_db):
    """4 def sites > the 3-file bound -> (), not four files shoved into focus."""
    assert definition_files_for_symbol(graph_db, AMBIGUOUS) == ()


def test_resolver_rejects_a_zero_start_line(graph_db):
    """The predicate must match `defined_symbols_for_file` and `_resolved_search_symbols`
    character for character. Those two once disagreed on exactly this column, and a focus
    WRITER that admits a definition the focus READER cannot see re-creates that ghost."""
    assert definition_files_for_symbol(graph_db, "ghost") == ()


def test_resolver_is_quiet_without_a_graph(tmp_path):
    assert definition_files_for_symbol(str(tmp_path / "nope.db"), SEARCHED) == ()
    assert definition_files_for_symbol("", SEARCHED) == ()


# --------------------------------------------------------------------------- #
# The focus expansion.
# --------------------------------------------------------------------------- #
def test_focus_expansion_adds_the_def_home(graph_db):
    assert seam._focus_definition_files((SEARCHED,)) == (DEF_HOME,)


def test_focus_expansion_is_quiet_on_empty_and_unresolvable_input(graph_db):
    assert seam._focus_definition_files(()) == ()
    assert seam._focus_definition_files(("no_such_symbol_anywhere",)) == ()


def test_the_active_decision_carries_the_unopened_def_home_as_a_subject(graph_db):
    decision = _decision(symbols=(SEARCHED,))
    assert f"subject:{DEF_HOME}" in decision.causal_neighborhood, (
        "the def-home never reached the active neighbourhood, so the gate cannot see it"
    )


# --------------------------------------------------------------------------- #
# THE FIX, at the gate.
# --------------------------------------------------------------------------- #
def test_localization_naming_an_UNOPENED_def_home_is_now_RELEASABLE(graph_db):
    """C12, closed for the FILE-subject case that `_resolved_search_symbols` could not reach.

    The agent searched `parse_url` and has opened NOTHING. GT names `src/pkg/urls.py`. Before
    this fix that record was HELD, so GT could only ever recommend a file the agent had already
    read — the product inverted.
    """
    decision = _decision(symbols=(SEARCHED,))
    ev = _evaluate(_record("localization", DEF_HOME), decision)
    assert ev.relevant is True, (
        "evidence naming the def-home of the agent's own search operand is still HELD; C12 is "
        "not fixed for FILE-subject records"
    )
    assert ev.release_allowed is True


def test_the_defect_is_really_gone_same_record_was_HELD_without_the_expansion(
        graph_db, monkeypatch):
    """THE NEGATIVE HALF, so the test above cannot pass for an unrelated reason. Identical
    record and identical focus, with ONLY the expansion neutralised -> HELD. This is the
    before/after on one variable."""
    monkeypatch.setattr(seam, "_focus_definition_files", lambda _symbols: ())
    decision = _decision(symbols=(SEARCHED,))
    ev = _evaluate(_record("localization", DEF_HOME), decision)
    assert ev.relevant is False
    assert ev.next_lifecycle is rr.EvidenceLifecycle.HELD


# --------------------------------------------------------------------------- #
# NO BAR WEAKENED.
# --------------------------------------------------------------------------- #
def test_unrelated_evidence_is_still_HELD(graph_db):
    """The whole point of a gate. If this ever passes, GT has become the context flood it
    exists to prevent, and the C12 'fix' was actually 'admit everything'."""
    decision = _decision(symbols=(SEARCHED,))
    assert _evaluate(_record("localization", UNRELATED), decision).relevant is False


def test_an_ambiguous_symbol_admits_none_of_its_candidate_files(graph_db):
    """Ambiguity must abstain, not admit all four. Otherwise a common name like `helper` in
    focus would make a quarter of the repository 'relevant'."""
    decision = _decision(symbols=(AMBIGUOUS,))
    for path in ("src/a.py", "src/b.py", "src/c.py", "src/d.py"):
        assert _evaluate(_record("localization", path), decision).relevant is False


def test_without_a_graph_the_gate_is_exactly_as_strict_as_before(tmp_path, monkeypatch):
    """Correct-or-quiet. A missing graph must degrade to the OLD behaviour, never to an
    exception and never to a permissive default."""
    monkeypatch.setenv("GT_HOST_GRAPH_DB", str(tmp_path / "absent.db"))
    decision = _decision(symbols=(SEARCHED,))
    assert _evaluate(_record("localization", DEF_HOME), decision).relevant is False


def test_the_expansion_is_bounded_because_focus_never_evicts(graph_db):
    """Focus is append-only — there is no eviction anywhere in `reduce_event`. An unbounded
    expansion would therefore grow for the whole trajectory and drive the intersection toward
    always-true, which is the same always-true failure the gate exists to avoid."""
    assert seam._FOCUS_DEF_FILES_TOTAL > 0
    many = tuple(f"sym{i}" for i in range(500)) + (SEARCHED,)
    assert len(seam._focus_definition_files(many)) <= seam._FOCUS_DEF_FILES_TOTAL
