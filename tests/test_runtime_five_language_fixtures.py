"""Five-language deterministic product receipts for runtime control behavior."""

from __future__ import annotations

import pytest

from groundtruth.runtime.action_translation import translate_to_action
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
            Turn(
                command="python -c \"open('src/app.py','w').write('def targetedBehavior(): pass')\""
            ),
            Turn(command="pytest tests/test_app.py", observation="1 passed"),
        ],
        "src/app.py",
    ),
    (
        "go",
        [
            Turn(command="sed -n '1,80p' pkg/app.go"),
            Turn(
                command="python -c \"open('pkg/app.go','w').write('func targetedBehavior() {}')\""
            ),
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
            Turn(
                command="python -c \"open('src/app.ts','w').write('export function targetedBehavior() {}')\""
            ),
            Turn(command="npm test", observation="1 passed"),
        ],
        "src/app.ts",
    ),
    (
        "java",
        [
            Turn(command="sed -n '1,80p' src/main/java/App.java"),
            Turn(
                command="python -c \"open('src/main/java/App.java','w').write('class App { void targetedBehavior() {} }')\""
            ),
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
    assert should_emit(
        "spec.obligation", Phase.VERIFY, event=Event.REVIEW_TRANSITION, event_bound=True
    ).allowed

    rendered = render_verify_emission(
        "urgent",
        70,
        100,
        {edited_file},
        [f"tests/{language}/hidden_exact_name"],
    )
    assert "hidden_exact_name" not in rendered
    assert "relevant repo test target" in rendered

    budget = ContextBudgeter()
    first = budget.trim("[WITNESS] targetedBehavior call by -> src/App:10\nInspect src/App", 30)
    second = budget.trim("[WITNESS] targetedBehavior call by -> src/App:10\nInspect src/App", 30)
    assert first.text
    assert second.text == ""

    action = translate_to_action(
        "[WITNESS] targetedBehavior call by -> src/App:10",
        Phase.EDIT,
    )
    assert "Inspect targetedBehavior at src/App:10" in action

    tracker = ObligationTracker([_ObligationView()])
    tracker.update({"targetedBehavior"}, set(), 2)
    assert tracker.snapshot()[0]["status"] == "edited"
    tracker.update({"targetedBehavior"}, {"targetedBehavior"}, 3)
    assert tracker.snapshot()[0]["status"] == "tested"
