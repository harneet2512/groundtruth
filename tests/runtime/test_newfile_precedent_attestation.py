"""RED-first unit tests for the newfile_precedent producer-attestation factory.

The factory binds the exact delivered REGISTRATION missing-role to change_surface's OWN
registration derivation, re-run on the producer-captured registry bytes.  Truth is PASS
only when the snapshot self-seal holds, the derivation reproduces a genuine registration
precedent (>=2 sibling registrations, the entity NOT registered), the delivered header line
is in the shipped bytes, and the seal covers those bytes.  Freshness is MEASURED (PASS)
when the captured git commit + blob anchor are present and self-consistent; UNMEASURED
otherwise.  A tampered snapshot (bytes whose blob id no longer matches) is rejected.

The producer-side fixture builds a REAL tiny git repo and runs the REAL
``detect_change_surface`` producer + ``build_registration_snapshot`` capture — never a
hand-authored claim — so the test proves the ROUND TRIP: real delivered claim ->
re-derivation -> PASS.

Documented biting mutations (each verified to fail a test, then restored):

  * MUTATION N1 — finalize_newfile_precedent_attestation: drop the ``not entity_registered``
    leg of ``reproduced``.  ``test_entity_already_registered_is_unmeasured`` then WRONGLY
    reports truth PASS for an entity that IS already registered (no missing role exists).
    Bite confirmed.

  * MUTATION N2 — _rederive_registration: drop the git-blob self-seal guard (``return None``
    on blob mismatch).  ``test_tampered_bytes_are_rejected`` then re-derives on the tampered
    bytes and WRONGLY reports PASS.  Bite confirmed.

  * MUTATION N3 — finalize: drop the ``header in delivered_block`` leg.
    ``test_header_absent_from_block_is_unmeasured`` then WRONGLY attests a claim whose
    header the model never saw.  Bite confirmed.

  * MUTATION N-seal — drop the ``delivery_seal == _seal16(delivered_block)`` leg.
    ``test_forged_seal_is_unmeasured`` then WRONGLY reports PASS for a seal that does not
    cover the shipped bytes.  Bite confirmed.

  * MUTATION N-fresh — finalize: force ``freshness_complete = True``.
    ``test_missing_commit_anchor_freshness_unmeasured`` then WRONGLY reports freshness PASS
    with no commit anchor.  Bite confirmed.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from groundtruth.pretask.change_surface import ROLE_REGISTRATION
from groundtruth.runtime.newfile_precedent_attestation import (
    NewfilePrecedentSnapshot,
    build_registration_snapshot,
    finalize_newfile_precedent_attestation,
    git_blob_sha,
)
from groundtruth.runtime.producer_attestation import (
    FRESHNESS,
    PASS,
    TRUTH,
    UNMEASURED,
    validate,
)

_EVIDENCE_TYPE = "missing_role:registration"
_CID = "change_surface:missing_role:registration:fixture"

# A registry file that registers the aws + gcp sibling providers (two code lines) but not
# the azure entity — the exact shape change_surface's registration derivation reproduces.
_REGISTRY_BYTES = (
    b"from .aws import AwsProvider\n"
    b"from .gcp import GcpProvider\n"
    b"\n"
    b"PROVIDERS = {\n"
    b'    "aws": AwsProvider,\n'
    b'    "gcp": GcpProvider,\n'
    b"}\n"
)


def _seal(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()[:16]


def _snapshot(*, repo_head: str = "a" * 40, registry: bytes = _REGISTRY_BYTES,
              entity: str = "azure", members=("aws", "gcp"),
              reg_file: str = "providers/registry.py") -> NewfilePrecedentSnapshot:
    return NewfilePrecedentSnapshot(
        registration_file=reg_file,
        registration_bytes=registry,
        members=tuple(members),
        entity=entity,
        git_blob_sha=git_blob_sha(registry),
        repo_head=repo_head,
    )


def _delivered_block(snapshot: NewfilePrecedentSnapshot, *, distinct: int = 2) -> str:
    """Rebuild the exact shipped body change_surface delivers for this claim."""
    from groundtruth.runtime.newfile_precedent_attestation import _rederive_registration

    rd = _rederive_registration(snapshot)
    assert rd is not None
    distinct, reg_lines, _ = rd
    header = (
        f"registry {snapshot.registration_file} registers "
        f"{distinct} siblings but not '{snapshot.entity}'"
    )
    return "\n" + header + "\n" + "\n".join(f"line {ln}: {t}" for ln, t in reg_lines) + "\n"


# --------------------------------------------------------------------------- #
# The complete, honest registration precedent -> truth PASS, freshness PASS.
# --------------------------------------------------------------------------- #
def test_registration_precedent_yields_truth_and_freshness_pass() -> None:
    snap = _snapshot()
    block = _delivered_block(snap)
    seal = _seal(block)

    final = finalize_newfile_precedent_attestation(
        snap, evidence_type=_EVIDENCE_TYPE, delivered_block=block,
        candidate_id=_CID, delivery_seal=seal,
    )

    assert validate(final.attestation) == ()
    assert final.attestation.evidence_type == _EVIDENCE_TYPE
    assert final.attestation.runtime_producer_id == "change_surface"
    assert final.attestation.candidate_id == _CID
    assert final.attestation.delivery_seal == seal
    assert final.attestation.truth_verdict == PASS
    # freshness is MEASURED: the snapshot carries a re-checkable git commit + blob anchor.
    assert final.attestation.freshness_verdict == PASS
    (truth,) = final.attestation.truth_predicates
    assert truth.predicate_kind == TRUTH and truth.proof_refs
    (fresh,) = final.attestation.freshness_predicates
    assert fresh.predicate_kind == FRESHNESS and fresh.proof_refs


def test_missing_commit_anchor_freshness_unmeasured() -> None:
    # MUTATION N-fresh bites here: truth still PASS (the derivation is anchor-independent)
    # but freshness must stay UNMEASURED without a real commit sha.
    snap = _snapshot(repo_head="")
    block = _delivered_block(snap)

    final = finalize_newfile_precedent_attestation(
        snap, evidence_type=_EVIDENCE_TYPE, delivered_block=block,
        candidate_id=_CID, delivery_seal=_seal(block),
    )
    assert validate(final.attestation) == ()
    assert final.attestation.truth_verdict == PASS
    assert final.attestation.freshness_verdict == UNMEASURED
    (fresh,) = final.attestation.freshness_predicates
    assert fresh.proof_refs == ()


# --------------------------------------------------------------------------- #
# Correct-or-quiet UNMEASURED cases (RED-first: none of these may claim PASS).
# --------------------------------------------------------------------------- #
def test_entity_already_registered_is_unmeasured() -> None:
    # MUTATION N1 bites here: aws is already registered, so there is no missing role.
    snap = _snapshot(entity="aws", members=("aws", "gcp"))
    block = _delivered_block(snap)

    final = finalize_newfile_precedent_attestation(
        snap, evidence_type=_EVIDENCE_TYPE, delivered_block=block,
        candidate_id=_CID, delivery_seal=_seal(block),
    )
    assert final.attestation.truth_verdict == UNMEASURED
    assert final.attestation.freshness_verdict == UNMEASURED


def test_too_few_sibling_registrations_is_unmeasured() -> None:
    # Only one member registered in the file -> not a >=2 precedent.
    registry = b"from .aws import AwsProvider\n\nPROVIDERS = {\"aws\": AwsProvider}\n"
    snap = _snapshot(registry=registry, members=("aws", "gcp"))
    block = "\nregistry providers/registry.py registers 1 siblings but not 'azure'\n"

    final = finalize_newfile_precedent_attestation(
        snap, evidence_type=_EVIDENCE_TYPE, delivered_block=block,
        candidate_id=_CID, delivery_seal=_seal(block),
    )
    assert final.attestation.truth_verdict == UNMEASURED


def test_tampered_bytes_are_rejected() -> None:
    # MUTATION N2 bites here: the bytes are altered but the sealed blob id claims the
    # original -> the self-seal fails and the whole snapshot is rejected.
    snap = _snapshot()
    block = _delivered_block(snap)
    tampered = NewfilePrecedentSnapshot(
        registration_file=snap.registration_file,
        registration_bytes=snap.registration_bytes + b"# injected\n",
        members=snap.members,
        entity=snap.entity,
        git_blob_sha=snap.git_blob_sha,  # STALE — no longer matches the bytes
        repo_head=snap.repo_head,
    )
    final = finalize_newfile_precedent_attestation(
        tampered, evidence_type=_EVIDENCE_TYPE, delivered_block=block,
        candidate_id=_CID, delivery_seal=_seal(block),
    )
    assert final.attestation.truth_verdict == UNMEASURED
    assert final.attestation.freshness_verdict == UNMEASURED


def test_header_absent_from_block_is_unmeasured() -> None:
    # MUTATION N3 bites here: a delivered block that does NOT carry the derived header.
    snap = _snapshot()
    block = "\nsome unrelated observation the model saw\n"

    final = finalize_newfile_precedent_attestation(
        snap, evidence_type=_EVIDENCE_TYPE, delivered_block=block,
        candidate_id=_CID, delivery_seal=_seal(block),
    )
    assert final.attestation.truth_verdict == UNMEASURED


def test_forged_seal_is_unmeasured() -> None:
    # MUTATION N-seal bites here.
    snap = _snapshot()
    block = _delivered_block(snap)

    final = finalize_newfile_precedent_attestation(
        snap, evidence_type=_EVIDENCE_TYPE, delivered_block=block,
        candidate_id=_CID, delivery_seal="0" * 16,
    )
    assert final.attestation.truth_verdict == UNMEASURED


def test_wrong_event_is_unmeasured() -> None:
    snap = _snapshot()
    block = _delivered_block(snap)

    final = finalize_newfile_precedent_attestation(
        snap, evidence_type=_EVIDENCE_TYPE, delivered_block=block,
        candidate_id=_CID, delivery_seal=_seal(block), actual_event="edit_result",
    )
    assert final.attestation.truth_verdict == UNMEASURED


def test_malformed_seal_raises_and_is_never_persisted() -> None:
    snap = _snapshot()
    block = _delivered_block(snap)
    with pytest.raises(ValueError, match="invalid newfile_precedent attestation"):
        finalize_newfile_precedent_attestation(
            snap, evidence_type=_EVIDENCE_TYPE, delivered_block=block,
            candidate_id=_CID, delivery_seal="NOT_HEX",
        )


def test_unregistered_evidence_type_raises() -> None:
    snap = _snapshot()
    with pytest.raises(ValueError, match="not a newfile_precedent evidence type"):
        finalize_newfile_precedent_attestation(
            snap, evidence_type="caller_break", delivered_block="x",
            candidate_id=_CID, delivery_seal="0" * 16,
        )


def test_git_blob_matches_real_git(tmp_path: Path) -> None:
    data = b"provider registry precedent\n"
    proc = subprocess.run(
        ["git", "hash-object", "--stdin"], input=data, capture_output=True, check=False,
    )
    if proc.returncode == 0:
        assert git_blob_sha(data) == proc.stdout.decode().strip()


# --------------------------------------------------------------------------- #
# PRODUCER-SIDE round trip: real detect_change_surface + real git repo -> capture -> PASS.
# --------------------------------------------------------------------------- #
def _git(repo: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, check=False)


def test_real_producer_claim_round_trips_to_pass(tmp_path: Path, monkeypatch) -> None:
    from groundtruth.pretask.change_surface import detect_change_surface

    monkeypatch.setenv("GT_CHANGE_SURFACE", "1")
    repo = tmp_path / "repo"
    (repo / "providers").mkdir(parents=True)
    (repo / "providers" / "aws.py").write_text("class AwsProvider:\n    def connect(self): ...\n")
    (repo / "providers" / "gcp.py").write_text("class GcpProvider:\n    def connect(self): ...\n")
    (repo / "providers" / "registry.py").write_bytes(_REGISTRY_BYTES)

    if _git(str(repo), "init", "-q").returncode != 0:
        pytest.skip("git unavailable")
    _git(str(repo), "add", "-A")
    _git(str(repo), "-c", "user.email=a@b.c", "-c", "user.name=t", "commit", "-q", "-m", "init")
    head = _git(str(repo), "rev-parse", "HEAD").stdout.strip()

    issue = "Please add an azure provider analogous to the existing aws and gcp providers."
    res = detect_change_surface(issue, str(repo), None)
    reg_roles = [m for m in res.missing_roles if m.role == ROLE_REGISTRATION]
    assert reg_roles, "expected a registration missing-role for azure"
    m = reg_roles[0]

    delivered_block = "\n" + "\n".join(m.evidence[:4]) + "\n"
    seal = _seal(delivered_block)
    members = next(
        (g["members"] for g in res.sibling_groups if g.get("registry_file") == m.registration_file),
        [],
    )
    snap = build_registration_snapshot(
        entity=m.entity, registration_file=m.registration_file,
        members=members, repo_root=str(repo), repo_head=head,
    )
    assert snap is not None
    assert snap.repo_head == head and len(snap.repo_head) == 40

    final = finalize_newfile_precedent_attestation(
        snap, evidence_type=f"missing_role:{m.role}", delivered_block=delivered_block,
        candidate_id=_CID, delivery_seal=seal,
    )
    assert validate(final.attestation) == ()
    assert final.attestation.truth_verdict == PASS
    assert final.attestation.freshness_verdict == PASS


def test_build_snapshot_correct_or_quiet_on_missing_file(tmp_path: Path) -> None:
    # Unreadable registry file / too-few members -> None (caller persists nothing).
    assert build_registration_snapshot(
        entity="azure", registration_file="nope/missing.py", members=("aws", "gcp"),
        repo_root=str(tmp_path),
    ) is None
    (tmp_path / "reg.py").write_bytes(_REGISTRY_BYTES)
    assert build_registration_snapshot(
        entity="azure", registration_file="reg.py", members=("aws",),  # < 2 members
        repo_root=str(tmp_path),
    ) is None


# --------------------------------------------------------------------------- #
# #40 — the POST-CREATION form. `missing_role_postcreate:registration` is the same fact at an
# honest post-creation edit boundary (registry: deliver_by=edit_result), and the producer stashes
# the SAME snapshot for it. But the seam's consumer guard tested
# `startswith("missing_role:registration")`, which the postcreate string does not satisfy — so its
# snapshot was stashed and never popped, and the form could never attest.
#
# WIDENING THE GUARD ALONE WOULD HAVE BEEN WORSE THAN THE MISS. `validate` never compares
# `open_event` to `required_event`, the seam never passed `actual_event`, and the factory defaulted
# it to the hardcoded `failed_search`. So a postcreate claim would have produced a structurally
# valid PASS attestation asserting the decision opened at a boundary it never touched — the
# "false 100% ON_TIME" defect this repo already documented (gt_mini_patch.py:9609-9611).
#
# And passing the real event alone is not enough either: `truth_complete` compared against the
# hardcoded constant, so every postcreate claim would have flipped to UNMEASURED — a silent miss
# traded for a silent unmeasured. The boundary must be DERIVED from the evidence type.
#
# BITING MUTATIONS (applied, observed RED, reverted by targeted restore):
#   M1 — restore the hardcoded `actual_event == _ACTUAL_EVENT`: the postcreate PASS test goes RED
#        (every postcreate claim reads UNMEASURED).
#   M2 — restore the hardcoded `open_event` fallback: the open_event test goes RED and the
#        attestation binds the delivery to a boundary it never occurred at.
# --------------------------------------------------------------------------- #
_POSTCREATE_TYPE = "missing_role_postcreate:registration"


def test_postcreate_form_passes_at_its_own_boundary() -> None:
    """M1. Same fact, same snapshot, honest edit_result boundary."""
    snap = _snapshot()
    block = _delivered_block(snap)

    final = finalize_newfile_precedent_attestation(
        snap, evidence_type=_POSTCREATE_TYPE, delivered_block=block,
        candidate_id=_CID, delivery_seal=_seal(block), actual_event="edit_result",
    )

    assert validate(final.attestation) == ()
    assert final.attestation.truth_verdict == PASS


def test_postcreate_open_event_is_the_observed_boundary_not_a_default() -> None:
    """M2. A DecisionBinding that names a boundary the delivery never touched is a lie."""
    snap = _snapshot()
    block = _delivered_block(snap)

    final = finalize_newfile_precedent_attestation(
        snap, evidence_type=_POSTCREATE_TYPE, delivered_block=block,
        candidate_id=_CID, delivery_seal=_seal(block), actual_event="edit_result",
    )

    assert final.attestation.decision.open_event == "edit_result"
    assert final.attestation.decision.required_event == "edit_result"


def test_postcreate_at_the_precreate_boundary_is_unmeasured() -> None:
    """The check is DERIVED, not widened: a postcreate claim at failed_search still fails."""
    snap = _snapshot()
    block = _delivered_block(snap)

    final = finalize_newfile_precedent_attestation(
        snap, evidence_type=_POSTCREATE_TYPE, delivered_block=block,
        candidate_id=_CID, delivery_seal=_seal(block), actual_event="failed_search",
    )
    assert final.attestation.truth_verdict == UNMEASURED


def test_precreate_form_is_byte_identical_to_before() -> None:
    """CALIBRATION + regression: deriving the boundary must not move the pre-create case."""
    snap = _snapshot()
    block = _delivered_block(snap)

    final = finalize_newfile_precedent_attestation(
        snap, evidence_type=_EVIDENCE_TYPE, delivered_block=block,
        candidate_id=_CID, delivery_seal=_seal(block),
    )
    assert final.attestation.truth_verdict == PASS
    assert final.attestation.decision.open_event == "failed_search"
