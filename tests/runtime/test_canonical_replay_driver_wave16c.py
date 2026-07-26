"""Wave 16c RED-first: the replay child must be able to drive the CANONICAL runtime.

WHY.  ``ss_replay_oracle.py`` drives the legacy seam at two sites (``:614``,
``:2005`` -> ``g._augment_output``) and contains no reference to
``install_canonical_runtime``.  So replay, like the gate, proves legacy-seam
behaviour and says nothing about the deterministic runtime.

WHY THIS IS *NOT* BLOCKED ON THE STALE BASELINE (corrected 2026-07-26).  Two
different things were conflated for many ticks:

* the replay's FIDELITY COMPARISON is uninterpretable against a recording that
  predates whole telemetry lanes (65.5%% of diffs are incomparable -- see
  handoff AD.1). That genuinely needs a re-record.
* whether the canonical runtime can be DRIVEN by the replay child at all is a
  wiring question, testable exactly the way the gate driver was.

Only the first needs a paid run.  Building the second does not.

ADDITIVE AND DEFAULT-OFF, deliberately.  Converting the default replay path
would change the semantics of the very runs used as evidence earlier today
(gate x2 + 5-task oracle).  The canonical path must be opt-in so legacy replay
output stays byte-comparable, and so the two can be run side by side.
"""

from __future__ import annotations

import pathlib
import sys
from types import SimpleNamespace


_SS_DIR = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "swebench"
if str(_SS_DIR) not in sys.path:
    sys.path.insert(0, str(_SS_DIR))

import ss_replay_oracle as O  # noqa: E402


def test_replay_exposes_a_canonical_runtime_installer() -> None:
    """RED until the replay child can install CanonicalRuntimeAttachment."""
    assert hasattr(O, "install_canonical_replay_runtime"), (
        "ss_replay_oracle drives only _augment_output (:614, :2005); it has no "
        "way to install the canonical runtime, so replay-green says nothing "
        "about the deterministic runtime"
    )


def test_canonical_replay_is_opt_in_and_off_by_default() -> None:
    """The default replay path must stay legacy.

    Biting: an implementation that switches the default would change the
    semantics of the gate x2 + 5-task oracle evidence produced earlier today,
    silently invalidating it.
    """
    assert hasattr(O, "canonical_replay_enabled")

    assert O.canonical_replay_enabled({}) is False
    assert O.canonical_replay_enabled({"GT_CANONICAL_REPLAY": "0"}) is False
    assert O.canonical_replay_enabled({"GT_CANONICAL_REPLAY": ""}) is False
    assert O.canonical_replay_enabled({"GT_CANONICAL_REPLAY": "1"}) is True


def test_canonical_replay_facts_shape() -> None:
    """Facts must be reported, and absent (not zeroed) when nothing installed.

    ``None`` keeps "never installed" distinguishable from "installed and did
    nothing" -- the same ambiguity that made 0/17 unfalsifiable.
    """
    assert hasattr(O, "canonical_replay_facts")

    assert O.canonical_replay_facts(None) is None

    runtime = SimpleNamespace(
        attempt_id="attempt-x",
        work_state=SimpleNamespace(sequence=4, phase="Phase.UNDERSTANDING"),
        failure_state=SimpleNamespace(health="HEALTHY"),
        journal=SimpleNamespace(
            events=lambda _a: [1, 2, 3, 4],
            evidence_records_for_attempt=lambda _a: [1, 2],
            compilations_for_attempt=lambda _a: [],
        ),
    )
    facts = O.canonical_replay_facts(SimpleNamespace(attempt_runtime=runtime))

    assert facts["committed_events"] == 4
    assert facts["evidence_records"] == 2
    assert facts["compilations"] == 0
    assert facts["sequence"] == 4


def test_canonical_replay_facts_survive_a_broken_journal() -> None:
    """A probe must never crash the replay child.

    Biting: an implementation without exception guards passes the happy-path
    test and fails this one.
    """
    def _boom(_a):
        raise RuntimeError("journal unavailable")

    runtime = SimpleNamespace(
        attempt_id="attempt-y",
        work_state=SimpleNamespace(sequence=0, phase=""),
        failure_state=SimpleNamespace(health=""),
        journal=SimpleNamespace(
            events=_boom,
            evidence_records_for_attempt=_boom,
            compilations_for_attempt=_boom,
        ),
    )

    facts = O.canonical_replay_facts(SimpleNamespace(attempt_runtime=runtime))

    assert facts["committed_events"] == 0
    assert facts["evidence_records"] == 0
    assert facts["compilations"] == 0
