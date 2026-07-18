"""RED contract — Cluster-3 (G): the suppress_late attribution writer must emit the FULL
identity.

``_v2_attribute_proven_truth`` recorded a PARTIAL ss_late identity (clause_id +
artifact_issue_sha256 + boundary + proof_turn) while its sibling ``_v2_partition_fresh_proofs``
emitted the FULL identity (also subject_digest + subject_term_digests). The replay-oracle's
suppress_late matchers (``_matches_exact_clause`` needs subject_digest; ``_matches_subject``
needs subject_term_digests) therefore FAILED against this writer's rows. This test pins the full
identity, derived EXACTLY as ``_v2_clause_fresh_behavioral_proof`` derives it.

BITING MUTATIONS (each turns an assertion RED):
  M1 — drop subject_digest from the identity dict: ``test_identity_carries_full_digests`` RED.
  M2 — derive subject_digest over the UNSORTED subjects (drop ``sorted``):
       ``test_subject_digest_matches_canonical_derivation`` RED.
"""

from __future__ import annotations

import hashlib
import re
from types import SimpleNamespace

import gt_mini_patch as g
from groundtruth.runtime.obligations import ObligationTruthState, obligation_subject_terms


_VERBATIM = "The parser must preserve compute_widget behavior for negative offsets."
_ISSUE_SHA = "a" * 64


def _proven_rows():
    view = SimpleNamespace(clause_id="c-777", idx=3, verbatim=_VERBATIM)
    proof = SimpleNamespace(turn=9)
    return [SimpleNamespace(state=ObligationTruthState.PROVEN, proof=proof, view=view)]


def _capture(monkeypatch):
    calls: list[dict] = []

    def _rec(*, kind, outcome, reason="", chars=0, file_path="", event=None,
             content=None, extra=None):
        calls.append({"kind": kind, "outcome": outcome, "reason": reason,
                      "extra": dict(extra or {})})
        return True

    monkeypatch.setattr(g, "_runtime_ledger_record", _rec)
    monkeypatch.setattr(g, "_ss_late_drop_on", lambda: True)
    monkeypatch.setattr(g, "_load_obligations_v2", lambda: {"issue_sha256": _ISSUE_SHA})
    # a fresh suppression-dedup set so the row is not swallowed by a prior test's key.
    monkeypatch.setattr(g, "_unexercised_late_suppressed", set())
    return calls


def test_identity_carries_full_digests(monkeypatch) -> None:
    calls = _capture(monkeypatch)
    g._v2_attribute_proven_truth(
        _proven_rows(), kind="obligation.resurface", boundary="post_edit")
    assert len(calls) == 1
    extra = calls[0]["extra"]
    assert calls[0]["reason"] == "ss_late"
    # M1: the FULL identity — every field the oracle matchers require.
    for key in ("clause_id", "subject_digest", "subject_term_digests",
                "artifact_issue_sha256", "boundary"):
        assert key in extra, f"missing {key} from ss_late identity"
    assert extra["clause_id"] == "c-777"
    assert extra["boundary"] == "post_edit"
    assert extra["artifact_issue_sha256"] == _ISSUE_SHA
    assert re.fullmatch(r"[0-9a-f]{16}", extra["subject_digest"])
    assert isinstance(extra["subject_term_digests"], list)
    assert all(re.fullmatch(r"[0-9a-f]{16}", d) for d in extra["subject_term_digests"])


def test_subject_digest_matches_canonical_derivation(monkeypatch) -> None:
    calls = _capture(monkeypatch)
    g._v2_attribute_proven_truth(
        _proven_rows(), kind="obligation.unexercised", boundary="test_result")
    extra = calls[0]["extra"]
    # M2: derived EXACTLY as _v2_clause_fresh_behavioral_proof does (sorted subjects; the two
    # sha256[:16] constructions) — this is what the recorded oracle rows were sealed with.
    subjects = obligation_subject_terms(_VERBATIM)
    expected_digest = hashlib.sha256(
        "|".join(sorted(subjects)).encode("utf-8")).hexdigest()[:16]
    expected_terms = [
        hashlib.sha256(s.encode("utf-8")).hexdigest()[:16] for s in sorted(subjects)
    ]
    assert extra["subject_digest"] == expected_digest
    assert extra["subject_term_digests"] == expected_terms


def test_non_proven_rows_emit_nothing(monkeypatch) -> None:
    calls = _capture(monkeypatch)
    view = SimpleNamespace(clause_id="c-1", idx=0, verbatim=_VERBATIM)
    rows = [SimpleNamespace(state=ObligationTruthState.UNEXERCISED, proof=None, view=view)]
    g._v2_attribute_proven_truth(rows, kind="obligation.resurface", boundary="post_edit")
    assert calls == []  # attribution only accounts PROVEN silence
