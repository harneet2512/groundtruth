#!/usr/bin/env python3
r"""Fail-closed live-run provenance: derive the terminal ``live_witness`` bit from
RUN PROVENANCE artifacts, never from gate quality or delivery evidence.

Doctrine (commit ea0eb16c0, reverting 2743f4000): "gates 1-6 passing cannot
distinguish a paid trajectory from a replay; offline evidence never sets the live
bit." This module joins ONLY workflow/seam provenance artifacts persisted next to a
task's outputs. It infers nothing from how well GT delivered.

────────────────────────────────────────────────────────────────────────────────
THE REPLAY HAZARD (spec recon item 3) — why the receipt alone cannot prove live
────────────────────────────────────────────────────────────────────────────────
``gt_profile_receipt.json`` (schema ``gt.profile_receipt.v1``) is written by the SEAM
``artifact_deepswe/gt_mini_patch.py._write_profile_receipt()`` at import-time
``_install()`` into ``dirname($GT_RUNTIME_LEDGER)`` (gt_mini_patch.py:18431-18478,
:18508). ANY import of ``gt_mini_patch`` in a non-baseline process with GT_RUNTIME_LEDGER
set regenerates it — and the offline replay harness does exactly that: its
``MiniSeamDriver._install()`` imports ``gt_mini_patch`` (install side-effect) and points
GT_RUNTIME_LEDGER at a temp ledger (ss_replay_oracle.py:531, :583). **A replay MINTS a
fresh receipt.** Therefore ``receipt_present`` alone is NOT proof of a paid trajectory.

The discriminator that a replay CANNOT forge:
``gt_profile_activation.json`` (schema ``gt.profile_activation.v1``) is written ONLY by
the GHA workflow's in-container profile-activation shell step — it ``eval``s
``rl_profile --emit-exports`` and writes the record to the ``/gt_out`` bind-mount
(.github/workflows/swebench_live_lite_full.yml:1650). No importable Python module writes
it; the replay/oracle path never runs that workflow step, so it can never produce this
artifact. It is the workflow-side witness of a live paid run.

CHOSEN CONJUNCTION (all four must hold for ``LIVE_PAID`` — fail-closed AND):
  1. ``activation_present``  — valid ``gt.profile_activation.v1`` with a non-empty profile
     token and >=1 activated member (the workflow ABORTS on 0 members,
     swebench_live_lite_full.yml:1635-1638, so >=1 is the live invariant). **The
     load-bearing replay discriminator: a replay cannot mint this file.**
  2. ``receipt_present``     — valid ``gt.profile_receipt.v1`` whose ``patched_classes`` is
     non-empty (the seam actually ATTACHED to an env class in a real agent process; a bare
     module load records ``patched=0`` and is GT-off in effect, gt_mini_patch.py:18343-18347).
  3. ``baseline_excluded``   — a valid ``gt.run_identity.v1`` is present AND its ``baseline``
     bool is False (swebench_live_lite_full.yml:914-926). The baseline/control arm writes
     NEITHER activation NOR receipt, but we ALSO require a POSITIVE non-baseline attestation:
     an absent/malformed/baseline-marked identity fails closed to NOT_LIVE.
  4. ``replay_excluded``     — NO replay-report artifact (``ss_replay_oracle_report.json`` or
     any JSON whose ``schema`` starts with ``ss.replay_oracle_report``,
     ss_replay_oracle.py:3091, :3256) is present beside the task outputs. A belt-and-
     suspenders positive replay marker on top of (1).

Any missing, malformed, ambiguous (two differing copies), or contradictory artifact ⇒
``NOT_LIVE`` with a named reason. No task IDs, no repo names, no gold paths, no benchmark
branches, no delivery-quality inference. Pure: reads files, mutates nothing, deterministic.
"""
from __future__ import annotations

import glob
import json
import os
from dataclasses import dataclass

_RECEIPT_NAME = "gt_profile_receipt.json"
_ACTIVATION_NAME = "gt_profile_activation.json"
_IDENTITY_NAME = "gt_run_identity.json"
_REPLAY_REPORT_NAME = "ss_replay_oracle_report.json"

_RECEIPT_SCHEMA = "gt.profile_receipt.v1"
_ACTIVATION_SCHEMA = "gt.profile_activation.v1"
_IDENTITY_SCHEMA = "gt.run_identity.v1"
_REPLAY_SCHEMA_PREFIX = "ss.replay_oracle_report"

# The live workflow copies /gt_out artifacts into ``<task>/gt_artifacts/`` (the Collect
# step, swebench_live_lite_full.yml:2339-2340); recorded runs and unit fixtures may place
# them directly in the task dir. Search both, deterministically, and fail closed on a
# two-location content conflict.
_ARTIFACT_SUBDIRS = ("", "gt_artifacts")

_VERDICT_LIVE = "LIVE_PAID"
_VERDICT_NOT_LIVE = "NOT_LIVE"


@dataclass(frozen=True)
class LiveRunProvenance:
    """The terminal live-run join. ``verdict == "LIVE_PAID"`` iff all four gates hold."""

    receipt_present: bool
    activation_present: bool
    baseline_excluded: bool
    replay_excluded: bool
    profile_token: str | None
    verdict: str
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "receipt_present": self.receipt_present,
            "activation_present": self.activation_present,
            "baseline_excluded": self.baseline_excluded,
            "replay_excluded": self.replay_excluded,
            "profile_token": self.profile_token,
            "verdict": self.verdict,
            "reasons": list(self.reasons),
        }


def _candidate_paths(task_dir: str, name: str) -> list[str]:
    paths: list[str] = []
    for sub in _ARTIFACT_SUBDIRS:
        p = os.path.join(task_dir, sub, name) if sub else os.path.join(task_dir, name)
        if os.path.isfile(p):
            paths.append(p)
    return paths


def _load_unique(task_dir: str, name: str) -> tuple[str, dict | None]:
    """Load an artifact from the task dir or its gt_artifacts subdir.

    Returns (status, data): status in {"absent", "malformed", "ambiguous", "ok"}.
    Two existing copies with differing parsed content ⇒ "ambiguous" (fail closed).
    """
    paths = _candidate_paths(task_dir, name)
    if not paths:
        return "absent", None
    parsed: list[dict] = []
    for p in paths:
        try:
            with open(p, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return "malformed", None
        if not isinstance(data, dict):
            return "malformed", None
        parsed.append(data)
    first = parsed[0]
    for other in parsed[1:]:
        if other != first:
            return "ambiguous", None
    return "ok", first


def _valid_receipt(data: dict) -> tuple[bool, str | None]:
    if data.get("schema") != _RECEIPT_SCHEMA:
        return False, "receipt_schema_invalid"
    patched = data.get("patched_classes")
    if not isinstance(patched, list) or not patched:
        return False, "receipt_no_patched_classes"
    if any(not isinstance(item, str) or not item for item in patched):
        return False, "receipt_no_patched_classes"
    if not isinstance(data.get("gt_rl_profile"), str):
        return False, "receipt_schema_invalid"
    if not isinstance(data.get("members_on"), list):
        return False, "receipt_schema_invalid"
    return True, None


def _valid_activation(data: dict) -> tuple[bool, str | None]:
    if data.get("schema") != _ACTIVATION_SCHEMA:
        return False, "activation_schema_invalid"
    profile = data.get("profile")
    if not isinstance(profile, str) or not profile:
        return False, "activation_no_profile"
    members = data.get("members")
    if not isinstance(members, list) or not members:
        return False, "activation_no_members"
    if any(not isinstance(item, str) or not item for item in members):
        return False, "activation_no_members"
    return True, None


def _valid_identity(data: dict) -> tuple[bool, str | None]:
    if data.get("schema") != _IDENTITY_SCHEMA:
        return False, "run_identity_schema_invalid"
    if not isinstance(data.get("baseline"), bool):
        return False, "run_identity_schema_invalid"
    return True, None


def _scan_replay_reports(task_dir: str) -> bool:
    """True iff a replay-report artifact is present beside the task outputs.

    Matches the known filename OR any JSON whose top-level ``schema`` begins with
    ``ss.replay_oracle_report``. Unparseable JSON is not treated as a replay marker
    here — the per-artifact loaders own malformed detection for the gating files.
    """
    for sub in _ARTIFACT_SUBDIRS:
        base = os.path.join(task_dir, sub) if sub else task_dir
        if not os.path.isdir(base):
            continue
        if os.path.isfile(os.path.join(base, _REPLAY_REPORT_NAME)):
            return True
        for p in sorted(glob.glob(os.path.join(base, "*.json"))):
            try:
                with open(p, encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, ValueError):
                continue
            if isinstance(data, dict):
                schema = data.get("schema")
                if isinstance(schema, str) and schema.startswith(_REPLAY_SCHEMA_PREFIX):
                    return True
    return False


def detect_live_run(task_dir: str) -> LiveRunProvenance:
    """Join run-provenance artifacts in ``task_dir`` into a fail-closed live verdict.

    LIVE_PAID requires: activation (workflow-only witness) + receipt (seam attached) +
    non-baseline run identity + no replay-report artifact, with no profile-token conflict.
    Every failure path names its reason; the default is NOT_LIVE.
    """
    reasons: list[str] = []

    # 1) receipt (seam witness; replay-mintable, so necessary but not sufficient)
    receipt_present = False
    receipt: dict | None = None
    r_status, r_data = _load_unique(task_dir, _RECEIPT_NAME)
    if r_status == "ok":
        ok, why = _valid_receipt(r_data)  # type: ignore[arg-type]
        if ok:
            receipt_present, receipt = True, r_data
        else:
            reasons.append(why or "receipt_schema_invalid")
    else:
        reasons.append(f"receipt_{r_status}")

    # 2) activation (workflow-only witness; the load-bearing replay discriminator)
    activation_present = False
    activation: dict | None = None
    profile_token: str | None = None
    a_status, a_data = _load_unique(task_dir, _ACTIVATION_NAME)
    if a_status == "ok":
        ok, why = _valid_activation(a_data)  # type: ignore[arg-type]
        if ok:
            activation_present, activation = True, a_data
            profile_token = str(a_data.get("profile"))  # type: ignore[union-attr]
        else:
            reasons.append(why or "activation_schema_invalid")
    else:
        reasons.append(f"activation_{a_status}")

    # 3) baseline exclusion via a positive non-baseline run-identity attestation
    baseline_excluded = False
    i_status, i_data = _load_unique(task_dir, _IDENTITY_NAME)
    if i_status == "ok":
        ok, why = _valid_identity(i_data)  # type: ignore[arg-type]
        if not ok:
            reasons.append(why or "run_identity_schema_invalid")
        elif i_data.get("baseline") is True:  # type: ignore[union-attr]
            reasons.append("baseline_arm")
        else:
            baseline_excluded = True
    else:
        reasons.append(f"run_identity_{i_status}")

    # 4) replay-report exclusion (positive marker)
    replay_excluded = not _scan_replay_reports(task_dir)
    if not replay_excluded:
        reasons.append("replay_report_present")

    # integrity cross-check: the two independently-written witnesses must agree on the
    # profile token when both are present (a divergence is a forged/mismatched pairing).
    token_conflict = False
    if receipt_present and activation_present:
        r_token = str((receipt or {}).get("gt_rl_profile") or "")
        a_token = str((activation or {}).get("profile") or "")
        if r_token and a_token and r_token != a_token:
            token_conflict = True
            reasons.append("profile_token_conflict")

    live = (
        receipt_present
        and activation_present
        and baseline_excluded
        and replay_excluded
        and not token_conflict
    )
    verdict = _VERDICT_LIVE if live else _VERDICT_NOT_LIVE
    return LiveRunProvenance(
        receipt_present=receipt_present,
        activation_present=activation_present,
        baseline_excluded=baseline_excluded,
        replay_excluded=replay_excluded,
        profile_token=profile_token,
        verdict=verdict,
        reasons=tuple(reasons),
    )
