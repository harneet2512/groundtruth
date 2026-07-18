"""RED-first tests for the newfile_precedent producer-attestation -> ledger truth join.

Mirrors ``test_attestation_join_submit_refusal.py``: fixtures use the REAL
``newfile_precedent_attestation.finalize_newfile_precedent_attestation`` factory (its truth
re-derived by re-running change_surface's own registration derivation on the captured
registry bytes) and the REAL ``persist_attestation`` store — never hand-written JSON. The
delivered ledger row carries the EXACT ``(candidate_id, content_sha256_16)`` identity the
gateway seam stamps.

Unlike submit_refusal, freshness here is MEASURED (the snapshot carries a git commit + blob
anchor), so a valid join reports ``freshness is True``.

Documented biting mutations (each verified to fail a test, then restored):

  * MUTATION NJ-A — attestation_join.join_truth: relax the identity to candidate_id only
    (drop the ``content_sha256_16``/``delivery_seal`` leg).
    ``test_newfile_precedent_seal_mismatch_produces_no_join`` then WRONGLY joins the
    seal-mismatched row. Bite confirmed.

  * MUTATION NJ-B — attestation_join.ATTESTED_FACT_CLASSES: remove ``"newfile_precedent"``.
    ``test_newfile_precedent_in_attested_set`` then fails — the grader loop would never
    apply the join for the class even with a valid PASS bundle. Bite confirmed.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT / "src", ROOT / "scripts" / "swebench"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import attestation_join as aj  # noqa: E402
from groundtruth.pretask.change_surface import ROLE_REGISTRATION  # noqa: E402
from groundtruth.runtime.attestation_store import persist_attestation  # noqa: E402
from groundtruth.runtime.feature_lineage import (  # noqa: E402
    build_lineage,
    lineage_ledger_extra,
)
from groundtruth.runtime.newfile_precedent_attestation import (  # noqa: E402
    NewfilePrecedentSnapshot,
    finalize_newfile_precedent_attestation,
    git_blob_sha,
)
from groundtruth.runtime.producer_attestation import PASS  # noqa: E402

_EVIDENCE_TYPE = "missing_role:registration"
_CID = "change_surface:missing_role:registration:join-fixture"
_REGISTRY_BYTES = (
    b"from .aws import AwsProvider\n"
    b"from .gcp import GcpProvider\n"
    b"\n"
    b'PROVIDERS = {"aws": AwsProvider, "gcp": GcpProvider}\n'
)


def _seal(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()[:16]


def _snapshot(repo_head: str = "b" * 40) -> NewfilePrecedentSnapshot:
    return NewfilePrecedentSnapshot(
        registration_file="providers/registry.py",
        registration_bytes=_REGISTRY_BYTES,
        members=("aws", "gcp"),
        entity="azure",
        git_blob_sha=git_blob_sha(_REGISTRY_BYTES),
        repo_head=repo_head,
    )


def _precedent_attestation(repo_head: str = "b" * 40):
    from groundtruth.runtime.newfile_precedent_attestation import _rederive_registration

    snap = _snapshot(repo_head)
    distinct, reg_lines, _ = _rederive_registration(snap)
    header = f"registry {snap.registration_file} registers {distinct} siblings but not '{snap.entity}'"
    block = "\n" + header + "\n" + "\n".join(f"line {ln}: {t}" for ln, t in reg_lines) + "\n"
    seal = _seal(block)
    final = finalize_newfile_precedent_attestation(
        snap, evidence_type=_EVIDENCE_TYPE, delivered_block=block,
        candidate_id=_CID, delivery_seal=seal,
    )
    assert final.attestation.truth_verdict == PASS
    return final.attestation, final.artifact_mapping(), _CID, seal


def _delivered_row(candidate_id: str, seal: str) -> dict:
    """A DELIVERED gateway change_surface ledger row exactly as the seam writes it —
    including the typed FACT lineage columns (J6: required to seat a truth join)."""
    return {
        "layer": "gateway.missing_role:registration",
        "event_type": "failed_search",
        "file_path": "providers/registry.py",
        "outcome": "delivered",
        "reason": "",
        "chars_delivered": 120,
        "iteration": 4,
        "content_sha256_16": seal,
        "seal_scope": "block",
        "candidate_id": candidate_id,
        **lineage_ledger_extra(build_lineage(
            runtime_producer_id="change_surface",
            evidence_type=_EVIDENCE_TYPE,
            actual_event="failed_search",
        )),
    }


def _persist(task_dir: Path, attestation, artifacts: dict) -> None:
    persist_attestation(attestation, artifacts, task_dir / "art" / "producer_attestations")


# --------------------------------------------------------------------------- #
# ATTESTED set membership (the grader loop iterates this).
# --------------------------------------------------------------------------- #
def test_newfile_precedent_in_attested_set() -> None:
    # MUTATION NJ-B bites here.
    assert "newfile_precedent" in aj.ATTESTED_FACT_CLASSES


# --------------------------------------------------------------------------- #
# join_truth
# --------------------------------------------------------------------------- #
def test_newfile_precedent_valid_join_yields_truth_true(tmp_path: Path) -> None:
    attestation, artifacts, cid, seal = _precedent_attestation()
    _persist(tmp_path, attestation, artifacts)
    rows = [_delivered_row(cid, seal)]

    joins = aj.join_truth(aj.load_attestations(str(tmp_path)).attestations, rows)

    assert set(joins) == {"newfile_precedent"}
    tj = joins["newfile_precedent"]
    assert tj.truth is True
    assert tj.authority is True  # rides the truth-PASS join
    # freshness is MEASURED here (the snapshot carries a real git commit + blob anchor).
    assert tj.freshness is True
    assert tj.attestation_count == 1
    assert tj.joined_delivery_row_indices == (0,)


def test_newfile_precedent_freshness_unmeasured_without_commit_anchor(tmp_path: Path) -> None:
    # No commit anchor -> truth still PASS but freshness UNMEASURED (honest).
    attestation, artifacts, cid, seal = _precedent_attestation(repo_head="")
    _persist(tmp_path, attestation, artifacts)
    rows = [_delivered_row(cid, seal)]

    joins = aj.join_truth(aj.load_attestations(str(tmp_path)).attestations, rows)
    tj = joins["newfile_precedent"]
    assert tj.truth is True and tj.authority is True
    assert tj.freshness is None


def test_newfile_precedent_seal_mismatch_produces_no_join(tmp_path: Path) -> None:
    # MUTATION NJ-A bites here.
    attestation, artifacts, cid, _ = _precedent_attestation()
    _persist(tmp_path, attestation, artifacts)
    rows = [_delivered_row(cid, "f" * 16)]

    joins = aj.join_truth(aj.load_attestations(str(tmp_path)).attestations, rows)
    assert joins == {}


def test_newfile_precedent_candidate_mismatch_produces_no_join(tmp_path: Path) -> None:
    attestation, artifacts, _, seal = _precedent_attestation()
    _persist(tmp_path, attestation, artifacts)
    rows = [_delivered_row("change_surface:other", seal)]

    joins = aj.join_truth(aj.load_attestations(str(tmp_path)).attestations, rows)
    assert joins == {}


def test_newfile_precedent_non_delivered_row_does_not_join(tmp_path: Path) -> None:
    attestation, artifacts, cid, seal = _precedent_attestation()
    _persist(tmp_path, attestation, artifacts)
    row = _delivered_row(cid, seal)
    row["outcome"] = "abstain"
    joins = aj.join_truth(aj.load_attestations(str(tmp_path)).attestations, [row])
    assert joins == {}


def test_tampered_stored_bundle_is_skipped(tmp_path: Path) -> None:
    # BYTE-INTEGRITY: mutating the stored attestation.json makes the canonical re-serialize
    # diverge -> load_attestations skips it with a named reason -> no join.
    attestation, artifacts, cid, seal = _precedent_attestation()
    _persist(tmp_path, attestation, artifacts)
    bundles = list(tmp_path.glob("**/producer_attestations/index/*/attestation.json"))
    assert bundles
    raw = bundles[0].read_bytes()
    assert b"missing_role:registration" in raw
    bundles[0].write_bytes(raw.replace(b"missing_role:registration", b"missing_role:registratioX"))
    load = aj.load_attestations(str(tmp_path))
    assert load.attestations == ()  # skipped fail-closed
    assert load.diagnostics  # with a named rejection reason (never a silent pass)
    assert aj.join_truth(load.attestations, [_delivered_row(cid, seal)]) == {}
