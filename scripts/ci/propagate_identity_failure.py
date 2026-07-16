#!/usr/bin/env python3
"""FIX 3 — the Run-identity gate must PROPAGATE an upstream proof failure, not overwrite it.

When the substrate proof already failed upstream (for example brief_emit ->
DETERMINISM_MISMATCH), ``run_manifest.json`` is never written. The later Run-identity gate
then reads an empty/absent manifest and computes ``IDENTITY_CONTENT_INVALID``. Stamping that
misleading code over ``proof_status.json`` HIDES the true cause from the auditor.

This helper decides the honest ``proof_status`` to write when the identity gate wants to fail:
it stays fail-closed (``state='failed'`` — the caller still exits 1 and refuses to spend), but
carries the TRUE upstream code/detail when one already exists, recording the identity-gate code
as a secondary field. When there is no upstream failure the identity gate IS the first failure,
so its own code is written as before.

The decision is a pure function (``resolve_proof_status``) so it is unit-testable with a biting
mutation; ``main`` is the thin file-IO wrapper the workflow calls.
"""
from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, Optional

PROOF_STATUS_SCHEMA = "gt.proof_status.v1"


def _as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def resolve_proof_status(
    identity_code: str,
    identity_detail: str,
    existing_status: Optional[dict],
    existing_failure: Optional[dict],
    *,
    ts: Optional[float] = None,
) -> dict:
    """Return the ``proof_status`` dict the identity gate should write.

    Fail-closed ALWAYS (``state='failed'``). If an upstream failure is already recorded
    (``existing_status.state == 'failed'`` or a non-empty ``existing_failure``), carry its
    code + detail forward as the primary label so the honest cause survives, and record the
    identity-gate code as ``identity_gate_code``. Otherwise the identity gate is the first
    failure and its own code is the label.
    """
    status = _as_dict(existing_status)
    failure = _as_dict(existing_failure)

    upstream_failed = status.get("state") == "failed" or bool(failure)
    if upstream_failed:
        # Prefer the granular proof_failure.json (it names the failing stage + carries the
        # cause in its message); fall back to the coarser proof_status.json code.
        if failure.get("code"):
            code = str(failure.get("code"))
            detail = str(failure.get("message") or failure.get("detail") or "")
            source = "proof_failure.json"
        else:
            code = str(status.get("code") or "GT_RUN_PROOF_FAIL")
            detail = str(status.get("detail") or "")
            source = "proof_status.json"
        return {
            "schema": PROOF_STATUS_SCHEMA,
            "state": "failed",
            "code": code,
            "detail": detail,
            "identity_gate_code": identity_code,
            "identity_gate_detail": identity_detail,
            "propagated_from": source,
            "ts": time.time() if ts is None else ts,
        }

    # No upstream failure — the identity gate is the first/only failure.
    return {
        "schema": PROOF_STATUS_SCHEMA,
        "state": "failed",
        "code": identity_code,
        "detail": identity_detail,
        "ts": time.time() if ts is None else ts,
    }


def _load(path: str) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    identity_code = argv[0] if len(argv) > 0 else ""
    identity_detail = argv[1] if len(argv) > 1 else ""
    proof_dir = os.environ.get("GT_IDENTITY_PROOF_DIR", "/tmp/gt")
    status_path = os.path.join(proof_dir, "proof_status.json")
    failure_path = os.path.join(proof_dir, "proof_failure.json")

    resolved = resolve_proof_status(
        identity_code,
        identity_detail,
        _load(status_path),
        _load(failure_path),
    )
    try:
        os.makedirs(proof_dir, exist_ok=True)
        with open(status_path, "w", encoding="utf-8") as fh:
            json.dump(resolved, fh)
    except OSError:
        # Never let the diagnostic write crash the (already-failing) gate step.
        pass
    print(
        "[GT] identity-gate proof_status: "
        f"code={resolved.get('code')} propagated_from={resolved.get('propagated_from', '<self>')}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
