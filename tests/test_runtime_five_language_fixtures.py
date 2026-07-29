"""Five-language deterministic product receipts for runtime control behavior."""
from __future__ import annotations

import pytest

from groundtruth.runtime.action_translation import ACTION_TEMPLATES, translate_to_action
from groundtruth.runtime.context_budget import ContextBudgeter
from groundtruth.runtime.context_policy import Event, Phase, should_emit
from groundtruth.runtime.obligations import ObligationTracker
from groundtruth.runtime.trajectory_state import Turn, derive_phase, derive_state
from groundtruth.runtime.verification_horizon import render_verify_emission


class _ObligationView:
    idx = 1
    verbatim = "changed behavior must be covered by targeted verification"
    sym_parts = frozenset({"targetedBehavior"})


LANGUAGE_FIXTURES = [
    (
        "python",
        [
            Turn(command="sed -n '1,80p' src/app.py"),
            Turn(command="python -c \"open('src/app.py','w').write('def targetedBehavior(): pass')\""),
            Turn(command="pytest tests/test_app.py", observation="1 passed"),
        ],
        "src/app.py",
    ),
    (
        "go",
        [
            Turn(command="sed -n '1,80p' pkg/app.go"),
            Turn(command="python -c \"open('pkg/app.go','w').write('func targetedBehavior() {}')\""),
            Turn(command="go test ./...", observation="ok example/pkg 0.01s"),
        ],
        "pkg/app.go",
    ),
    (
        "rust",
        [
            Turn(command="sed -n '1,80p' src/lib.rs"),
            Turn(command="python -c \"open('src/lib.rs','w').write('fn targetedBehavior() {}')\""),
            Turn(command="cargo test", observation="test result: ok. 1 passed; 0 failed"),
        ],
        "src/lib.rs",
    ),
    (
        "typescript",
        [
            Turn(command="sed -n '1,80p' src/app.ts"),
            Turn(command="python -c \"open('src/app.ts','w').write('export function targetedBehavior() {}')\""),
            Turn(command="npm test", observation="1 passed"),
        ],
        "src/app.ts",
    ),
    (
        "java",
        [
            Turn(command="sed -n '1,80p' src/main/java/App.java"),
            Turn(command="python -c \"open('src/main/java/App.java','w').write('class App { void targetedBehavior() {} }')\""),
            Turn(command="mvn test", observation="BUILD SUCCESS"),
        ],
        "src/main/java/App.java",
    ),
]


@pytest.mark.parametrize(("language", "turns", "edited_file"), LANGUAGE_FIXTURES)
def test_runtime_state_policy_and_verification_are_language_agnostic(language, turns, edited_file):
    state = derive_state(turns, step_limit=100)
    assert edited_file in state.viewed_files
    assert edited_file in state.edited_files
    assert state.source_edit_count == 1
    assert state.test_count == 1
    assert derive_phase(state) == Phase.VERIFY

    assert should_emit("l3b.evidence", Phase.VIEW, event=Event.POST_VIEW, event_bound=True).allowed
    assert should_emit("spec.obligation", Phase.VERIFY, event=Event.REVIEW_TRANSITION, event_bound=True).allowed

    rendered = render_verify_emission(
        "urgent", 70, 100, {edited_file}, [f"tests/{language}/hidden_exact_name"],
    )
    assert "hidden_exact_name" not in rendered
    assert "relevant repo test target" in rendered

    # Budgeter dedup is language-agnostic. NOTE the propose/commit split (the "D1 fix",
    # context_budget.py:80-88 + tests/test_context_budget.py:49-58): `trim` is PURE and
    # deliberately does NOT burn the fact, so a candidate that LOSES its gate cannot
    # destroy evidence it never delivered. Suppression is earned by `commit_delivered`.
    # This assertion used to trim twice and expect the second to be empty, which pinned
    # the pre-D1 semantics; it now exercises both halves of the real contract.
    # NOTE the fact form: the producer emits "called by ->" (action_translation.py:30-31,
    # gt_mini_patch.py:6165). This fixture said "call by", which matches no regex in the
    # product, so the translation assertion below was never exercising a real witness.
    witness = "[WITNESS] targetedBehavior called by -> src/App:10"
    payload = f"{witness}\nInspect src/App"
    budget = ContextBudgeter()
    first = budget.trim(payload, 30)
    assert first.text
    uncommitted = budget.trim(payload, 30)
    assert uncommitted.text, "an UNCOMMITTED fact must not be suppressed (gate-loss safety)"
    budget.commit_delivered(first.pending_lines)
    assert budget.trim(payload, 30).text == "", "a COMMITTED fact must be suppressed"

    # The caller-direction witness translates to the caller_risk action, and the original
    # fact is KEPT alongside it (action_translation.py:48-53). Expected text is rendered
    # from the product's own template so a wording change updates both sides at once —
    # the previous hard-coded "Inspect targetedBehavior at src/App:10" was a wording no
    # template has produced for some time.
    action = translate_to_action(witness, Phase.EDIT)
    assert witness in action, "the original fact must survive translation"
    assert ACTION_TEMPLATES["caller_risk"].format(
        callee="targetedBehavior", loc="src/App:10") in action

    tracker = ObligationTracker([_ObligationView()])
    tracker.update({"targetedBehavior"}, set(), 2)
    assert tracker.snapshot()[0]["status"] == "edited"
    tracker.update({"targetedBehavior"}, {"targetedBehavior"}, 3)
    assert tracker.snapshot()[0]["status"] == "tested"
