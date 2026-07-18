#!/usr/bin/env python3
"""G1 (SS-REFEREE gate 4): eligibility-control refereeing is gradeable and honest.

Before G1 every eligibility CAP was structurally ungradeable: the correctness pass
skipped every non-mediator record and the terminal's ``mediated_fact_ids`` gate could
only be satisfied through delivery joins an eligibility referee never makes. These
tests pin the upgraded contract:

  * a BLOCKS ruling (dedup2 semantic_duplicate, novelty step_behind, shadow holdout,
    late_drop late) whose exact candidate bytes were delivered anyway grades ``False``
    (the refereeing lie);
  * a BLOCKS ruling whose bytes never shipped grades ``True``;
  * a PERMITS ruling (``NO_EFFECT`` with identity) whose bytes shipped is confirmed
    ``True``; permitted-but-not-shipped contributes nothing (arbiter may out-rank) —
    the asymmetry that forbids relabeling eligibility as mediation;
  * a ``(feature, decision)`` combination outside the declared polarity authority
    stays ungraded (correct-or-quiet, e.g. enabling actions like widened_prefix);
  * mediator semantics are byte-identical to the pre-G1 contract.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "swebench"))

from gt_feature_metrics import (  # noqa: E402
    _ELIGIBILITY_DECISION_POLARITY,
    _control_declared_effect_correctness,
    _infra_control_readiness,
    new_lifecycle,
)
from groundtruth.runtime.evidence_envelope import (  # noqa: E402
    build_observation_binding,
    observation_binding_to_dict,
)


_SEAL = "ab12cd34ef56ab12"


def _binding(candidate_id="cand-1", *, ordinal=0):
    return observation_binding_to_dict(build_observation_binding(
        batch_start_iteration=4,
        parent_policy_sha256="1" * 64,
        parent_policy_chars=16,
        action_batch_sha256="2" * 64,
        candidate_ordinal=ordinal,
        candidate_kind="gateway.def_ref_partition",
        candidate_id=candidate_id,
    ))


def _rec(feature_id, role, decision, *, seal=_SEAL, chars=64,
         fact_class="def_partition", candidate_id="cand-1",
         observation_binding=None, iteration=4, row_index=1):
    return {
        "row_index": row_index,
        "brief_row_index": None,
        "feature_id": feature_id,
        "role": role,
        "decision": decision,
        "iteration": iteration,
        "candidate_chars": chars,
        "candidate_sha256_16": seal,
        "fact_class": fact_class,
        "candidate_id": candidate_id,
        "observation_binding": (
            _binding(candidate_id) if observation_binding is None
            else observation_binding
        ),
    }


def _delivered_row(seal=_SEAL, chars=64, *, fact_class="def_partition",
                   candidate_id="cand-1", observation_binding=None, iteration=4):
    return {
        "outcome": "delivered",
        "iteration": iteration,
        "content_sha256_16": seal,
        "chars_delivered": chars,
        "lineage_schema": "gt.feature_lineage.v1",
        "fact_class": fact_class,
        "candidate_id": candidate_id,
        "observation_binding": (
            _binding(candidate_id) if observation_binding is None
            else observation_binding
        ),
    }


def _opportunity_row(candidate_id="cand-1", *, ordinal=0, observation_binding=None):
    """A committed feature.opportunity ledger row (formatter succeeded).

    NO-GO defect 4: eligibility rulings are graded ONLY when their exact validated
    observation binding matches one of these committed rows — a ruling evaluated
    during pure preparation whose observation never committed stays ungraded.
    """
    return {
        "layer": "feature.opportunity",
        "candidate_id": candidate_id,
        "observation_binding": (
            _binding(candidate_id, ordinal=ordinal) if observation_binding is None
            else observation_binding
        ),
    }


class EligibilityBlocksRulings(unittest.TestCase):
    def test_blocked_candidate_delivered_anyway_is_a_lie(self):
        for feature, decision in (
            ("GT_SS_DEDUP2", "APPLIED"),
            ("GT_SS_NOVELTY", "APPLIED"),
            ("GT_SS_SHADOW", "APPLIED"),
            ("GT_SS_LATE_DROP", "APPLIED"),
        ):
            with self.subTest(feature=feature):
                out = _control_declared_effect_correctness(
                    [_opportunity_row(), _delivered_row()],
                    {feature: [_rec(feature, "eligibility", decision)]},
                    {},
                )
                self.assertIs(out[feature], False)

    def test_blocked_candidate_never_shipped_is_correct(self):
        out = _control_declared_effect_correctness(
            [_opportunity_row()],  # committed opportunity, nothing delivered
            {"GT_SS_DEDUP2": [_rec("GT_SS_DEDUP2", "eligibility", "APPLIED")]},
            {},
        )
        self.assertIs(out["GT_SS_DEDUP2"], True)

    def test_uncommitted_opportunity_never_grades_a_ruling(self):
        # NO-GO defect 4: the formatter never committed this observation — a
        # blocked ruling must NOT earn vacuous True credit from the absence of
        # a delivery, and a delivered doppelganger changes nothing either way.
        for rows in ([], [_delivered_row()]):
            with self.subTest(rows=len(rows)):
                out = _control_declared_effect_correctness(
                    rows,
                    {"GT_SS_DEDUP2": [_rec("GT_SS_DEDUP2", "eligibility", "APPLIED")]},
                    {},
                )
                self.assertIsNone(out["GT_SS_DEDUP2"])

    def test_opportunity_commit_must_match_exact_binding(self):
        # A committed opportunity for a DIFFERENT candidate ordinal is not this
        # ruling's transaction — the ruling stays ungraded.
        out = _control_declared_effect_correctness(
            [_opportunity_row("cand-1", ordinal=5)],
            {"GT_SS_DEDUP2": [_rec("GT_SS_DEDUP2", "eligibility", "APPLIED")]},
            {},
        )
        self.assertIsNone(out["GT_SS_DEDUP2"])

    def test_prior_identical_delivery_does_not_refute_later_block(self):
        prior = _delivered_row(
            candidate_id="prior-candidate",
            observation_binding=_binding("prior-candidate", ordinal=1),
            iteration=3,
        )
        blocked = _rec(
            "GT_SS_DEDUP2", "eligibility", "APPLIED",
            candidate_id="later-candidate",
            observation_binding=_binding("later-candidate", ordinal=2),
            iteration=8,
        )
        out = _control_declared_effect_correctness(
            [prior, _opportunity_row("later-candidate", ordinal=2)],
            {"GT_SS_DEDUP2": [blocked]}, {},
        )
        self.assertIs(out["GT_SS_DEDUP2"], True)

    def test_same_identity_delivery_before_ruling_does_not_refute_block(self):
        binding = _binding("same-candidate")
        prior = _delivered_row(
            candidate_id="same-candidate",
            observation_binding=binding,
            iteration=3,
        )
        blocked = _rec(
            "GT_SS_DEDUP2", "eligibility", "APPLIED",
            candidate_id="same-candidate",
            observation_binding=binding,
            iteration=8,
        )
        out = _control_declared_effect_correctness(
            [prior, _opportunity_row(observation_binding=binding)],
            {"GT_SS_DEDUP2": [blocked]}, {},
        )
        self.assertIs(out["GT_SS_DEDUP2"], True)


class EligibilityPermitsRulings(unittest.TestCase):
    def test_permitted_and_shipped_is_confirmed(self):
        out = _control_declared_effect_correctness(
            [_opportunity_row(), _delivered_row()],
            {"GT_SS_NOVELTY": [_rec("GT_SS_NOVELTY", "eligibility", "NO_EFFECT")]},
            {},
        )
        self.assertIs(out["GT_SS_NOVELTY"], True)

    def test_permitted_but_not_shipped_contributes_nothing(self):
        # THE asymmetry vs mediation: a permit does not owe a delivery — the None
        # here is attributable to arbiter out-ranking, NOT a missing opportunity
        # (the opportunity below IS committed).
        out = _control_declared_effect_correctness(
            [_opportunity_row()],
            {"GT_SS_NOVELTY": [_rec("GT_SS_NOVELTY", "eligibility", "NO_EFFECT")]},
            {},
        )
        self.assertIsNone(out["GT_SS_NOVELTY"])

    def test_permit_requires_same_observation_candidate(self):
        permitted = _rec(
            "GT_SS_NOVELTY", "eligibility", "NO_EFFECT",
            candidate_id="permitted",
            observation_binding=_binding("permitted", ordinal=1),
        )
        other = _delivered_row(
            candidate_id="other",
            observation_binding=_binding("other", ordinal=2),
        )
        out = _control_declared_effect_correctness(
            [other, _opportunity_row("permitted", ordinal=1)],
            {"GT_SS_NOVELTY": [permitted]}, {},
        )
        self.assertIsNone(out["GT_SS_NOVELTY"])

    def test_permit_requires_same_opportunity_when_candidate_id_repeats(self):
        permitted = _rec(
            "GT_SS_NOVELTY", "eligibility", "NO_EFFECT",
            candidate_id="repeated",
            observation_binding=_binding("repeated", ordinal=1),
        )
        other_opportunity = _delivered_row(
            candidate_id="repeated",
            observation_binding=_binding("repeated", ordinal=2),
        )
        out = _control_declared_effect_correctness(
            [other_opportunity, _opportunity_row("repeated", ordinal=1)],
            {"GT_SS_NOVELTY": [permitted]}, {},
        )
        self.assertIsNone(out["GT_SS_NOVELTY"])

    def test_relabel_as_mediator_mutation_would_bite(self):
        # If eligibility were relabeled mediation, an identified NO_EFFECT record that
        # never joined would fall into the mediator branch and still contribute nothing,
        # BUT a mediator APPLIED without a join is False — a permit-styled APPLIED under
        # mediator semantics becomes a lie. Pin the divergence both ways.
        eligibility = _control_declared_effect_correctness(
            [_opportunity_row()],
            {"GT_SS_LATE_DROP": [_rec("GT_SS_LATE_DROP", "eligibility", "APPLIED")]},
            {},
        )
        self.assertIs(eligibility["GT_SS_LATE_DROP"], True)  # blocked, not shipped → correct
        mediator = _control_declared_effect_correctness(
            [_opportunity_row()],
            {"GT_SS_LATE_DROP": [_rec("GT_SS_LATE_DROP", "mediator", "APPLIED")]},
            {},
        )
        self.assertIs(mediator["GT_SS_LATE_DROP"], False)  # unjoined mediator APPLIED → lie


class CorrectOrQuietBoundaries(unittest.TestCase):
    def test_unknown_combination_stays_ungraded(self):
        # Enabling-action rulings (e.g. GT_SS_ELIGIBILITY widened_prefix) are absent
        # from the polarity authority and must never be graded by delivery presence.
        self.assertNotIn(("GT_SS_ELIGIBILITY", "APPLIED"), _ELIGIBILITY_DECISION_POLARITY)
        out = _control_declared_effect_correctness(
            [_opportunity_row(), _delivered_row()],
            {"GT_SS_ELIGIBILITY": [_rec("GT_SS_ELIGIBILITY", "eligibility", "APPLIED")]},
            {},
        )
        self.assertIsNone(out["GT_SS_ELIGIBILITY"])

    def test_identityless_ruling_stays_ungraded(self):
        rec = _rec(
            "GT_SS_DEDUP2", "eligibility", "APPLIED",
            seal="", chars=0, candidate_id="", observation_binding={},
        )
        out = _control_declared_effect_correctness(
            [_opportunity_row(), _delivered_row()], {"GT_SS_DEDUP2": [rec]}, {},
        )
        self.assertIsNone(out["GT_SS_DEDUP2"])

    def test_malformed_binding_cannot_satisfy_terminal_linkage(self):
        rec = _rec(
            "GT_SS_NOVELTY", "eligibility", "NO_EFFECT",
            observation_binding={},
        )
        readiness = _infra_control_readiness(
            "GT_SS_NOVELTY",
            ("def_partition",),
            {"def_partition": new_lifecycle("fixture")},
            ledger_artifact="ledger",
            control_evidence={
                "records": {"GT_SS_NOVELTY": [rec]},
                "joins": {},
                "correctness": {"GT_SS_NOVELTY": None},
            },
        )
        self.assertIsNone(readiness["gates"]["mediated_fact_ids"])
        self.assertEqual(
            readiness["mediation"]["runtime_linked_fact_ids"], [],
        )

    def test_worst_state_rollup_false_dominates(self):
        recs = [
            _rec("GT_SS_DEDUP2", "eligibility", "NO_EFFECT", row_index=1),  # permitted+shipped → True
            _rec("GT_SS_DEDUP2", "eligibility", "APPLIED", row_index=2),    # blocked+shipped → False
        ]
        out = _control_declared_effect_correctness(
            [_opportunity_row(), _delivered_row()], {"GT_SS_DEDUP2": recs}, {},
        )
        self.assertIs(out["GT_SS_DEDUP2"], False)


class MediatorSemanticsUnchanged(unittest.TestCase):
    def test_mediator_suppressed_carried_still_lies(self):
        out = _control_declared_effect_correctness(
            [_delivered_row()],
            {"GT_GATEWAY": [_rec("GT_GATEWAY", "mediator", "SUPPRESSED")]},
            {},
        )
        self.assertIs(out["GT_GATEWAY"], False)

    def test_mediator_no_effect_unjoined_still_none(self):
        out = _control_declared_effect_correctness(
            [],
            {"GT_GATEWAY": [_rec("GT_GATEWAY", "mediator", "NO_EFFECT")]},
            {},
        )
        self.assertIsNone(out["GT_GATEWAY"])


if __name__ == "__main__":
    unittest.main()
