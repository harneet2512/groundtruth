"""#36 — the reactive localization forms are DELIBERATELY unattested. Pinned so the blank is
not mistaken for a regression, and so anyone who removes it must confront why it is there.

`localization` ships in three forms. Only the task_start one can be honestly attested:

    brief_localization  deliver_by=task_start     producer=v1r_brief            ATTESTED
    localization        deliver_by=search_result  producer=ranked_localization  deliberately not
    trace_frame         deliver_by=failure_obs    producer=trace                deliberately not

WHY NOT. The reactive claim's load-bearing word is "highest-ranked", and rank is the output of a
fused ranker that reads up to 200 WORKING-TREE files (which the agent mutates mid-episode), a
process-global embedder that resolves differently per host, and ~10 import-time env knobs recorded
nowhere. No snapshot re-derives that. Recording the ranker's own scores and hashing them proves
only "the producer said so" — the fabrication this codebase already reverted.

Attesting the weaker "these rows exist at revision R" would set truth_valid=PASS, and therefore
correct_info=True, for a delivery whose claim is strictly stronger. Over-claiming is worse than
no attestation.

AND A WEAK ATTESTATION IS ACTIVELY DESTRUCTIVE, which is the part that settles it: `join_truth`
aggregates PER FACT CLASS, and `_aggregate_bool` returns None unless EVERY joined verdict is PASS.
`localization` already earns truth=True from the brief form. A reactive attestation landing
UNMEASURED — which it would on any incomplete snapshot — DEMOTES the whole class from
correct_info=True to None. Adding proof machinery would remove measured coverage.

THE ONLY HONEST PATH would change the DELIVERED BYTES first: drop the ranking assertion from the
claim and carry a def_partition-shaped sidecar (graph_revision + a real definition node id per
row + the per-row witness the producer currently discards). That is a product change to what GT
SAYS, not a proof change.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT / "src"), str(_ROOT / "scripts" / "swebench")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import attestation_join as aj  # noqa: E402
from groundtruth.runtime.fact_registry import registration_for, required_event  # noqa: E402
from groundtruth.runtime.gateway_attestation_factory import _SUPPORTED  # noqa: E402
from groundtruth.runtime.producer_attestation import PASS, UNMEASURED  # noqa: E402


_REACTIVE_FORMS = ("localization", "trace_frame")


def test_the_three_localization_forms_and_their_boundaries() -> None:
    """CALIBRATION for everything below: the forms exist and differ by boundary."""
    assert required_event("brief_localization") == "task_start"
    assert required_event("localization") == "search_result"
    assert required_event("trace_frame") == "failure_obs"
    for form in ("brief_localization",) + _REACTIVE_FORMS:
        assert registration_for(form).fact_class == "localization"


def test_the_reactive_forms_have_no_gateway_attestation_path() -> None:
    """The deliberate blank. If this goes RED someone added an attestation — read the module
    docstring before assuming that is an improvement."""
    for form in _REACTIVE_FORMS:
        assert form not in _SUPPORTED, (
            f"{form} gained a gateway attestation path. Its delivered claim asserts a RANKING "
            "that no snapshot re-derives, and a weak attestation DEMOTES the whole "
            "localization class -- see this module's docstring."
        )


def test_the_task_start_form_is_still_the_attested_one() -> None:
    """Calibration: the absence above is SPECIFIC, not 'localization cannot be attested'."""
    assert "localization" in aj.ATTESTED_FACT_CLASSES


def test_a_weak_second_attestation_demotes_the_whole_class() -> None:
    """THE REASON, pinned executably rather than argued in a comment.

    `join_truth` aggregates per fact class. One UNMEASURED alongside a PASS is not 'partial
    credit' -- it is None for the class, i.e. `correct_info` drops from True to unmeasured.
    """
    assert aj._aggregate_bool([PASS]) is True
    assert aj._aggregate_bool([PASS, UNMEASURED]) is None
    assert aj._aggregate_bool([PASS, PASS]) is True


def test_both_reactive_forms_bypass_the_wrong_event_split() -> None:
    """A timing gap no attestation would have closed anyway.

    `trace_frame` is `is_reactive`, so the adjudicator structurally cannot report WRONG_EVENT for
    it. I recorded 'exactly ONE class is reactive' in the ledger; it is two. Pinned so the
    correction survives.
    """
    from groundtruth.runtime.fact_registry import is_reactive

    assert is_reactive("trace_frame") is True
    assert is_reactive("covering_red") is True
    assert is_reactive("localization") is False
