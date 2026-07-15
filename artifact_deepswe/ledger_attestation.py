"""Terminal integrity proof for the live runtime-ledger artifact.

The ledger is append-only while an agent runs.  A terminal attestation binds the
exact bytes harvested after ``agent.run`` to their canonical row count.  Readers
must recompute the proof; artifact existence alone is never sufficient.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any


SCHEMA = "gt.runtime_ledger_attestation.v1"
_REQUIRED_FIELDS = {
    "layer", "event_type", "file_path", "outcome", "reason",
    "chars_delivered", "iteration",
}


def attestation_path_for(ledger_path: str | os.PathLike[str]) -> Path:
    """Return the task-local attestation path for a canonical ledger filename."""
    ledger = Path(ledger_path)
    prefix = "gt_runtime_ledger_"
    if ledger.name.startswith(prefix) and ledger.name.endswith(".jsonl"):
        suffix = ledger.name[len(prefix):-len(".jsonl")]
        return ledger.with_name(f"gt_runtime_ledger_attestation_{suffix}.json")
    return ledger.with_name("gt_runtime_ledger_attestation.json")


def _canonical_rows(raw: bytes) -> list[dict[str, Any]]:
    if not raw or not raw.endswith(b"\n"):
        raise ValueError("runtime ledger must be nonempty and newline-terminated")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("runtime ledger is not UTF-8") from exc

    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line:
            raise ValueError(f"runtime ledger has blank row {line_number}")
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"runtime ledger row {line_number} is malformed") from exc
        if not isinstance(row, dict) or not _REQUIRED_FIELDS.issubset(row):
            raise ValueError(f"runtime ledger row {line_number} is noncanonical")
        if not isinstance(row["layer"], str) or not row["layer"]:
            raise ValueError(f"runtime ledger row {line_number} has no layer")
        if not isinstance(row["outcome"], str) or not row["outcome"]:
            raise ValueError(f"runtime ledger row {line_number} has no outcome")
        chars = row["chars_delivered"]
        iteration = row["iteration"]
        if isinstance(chars, bool) or not isinstance(chars, int) or chars < 0:
            raise ValueError(f"runtime ledger row {line_number} has invalid chars")
        if isinstance(iteration, bool) or not isinstance(iteration, int) or iteration < 0:
            raise ValueError(f"runtime ledger row {line_number} has invalid iteration")
        if row["outcome"] == "delivered":
            seal = row.get("content_sha256_16")
            if chars <= 0 or not isinstance(seal, str) or len(seal) != 16:
                raise ValueError(f"runtime ledger row {line_number} has invalid delivery seal")
            try:
                int(seal, 16)
            except ValueError as exc:
                raise ValueError(
                    f"runtime ledger row {line_number} has non-hex delivery seal"
                ) from exc
        rows.append(row)
    return rows


def _document(ledger: Path, raw: bytes, *, write_failures: int) -> dict[str, Any]:
    rows = _canonical_rows(raw)
    if isinstance(write_failures, bool) or not isinstance(write_failures, int):
        raise ValueError("write_failures must be an integer")
    return {
        "schema": SCHEMA,
        "ledger_filename": ledger.name,
        "row_count": len(rows),
        "byte_count": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "final_newline": True,
        "write_failures": write_failures,
    }


def write_attestation(
    ledger_path: str | os.PathLike[str],
    output_path: str | os.PathLike[str] | None = None,
    *,
    write_failures: int,
) -> dict[str, Any]:
    """Atomically write an attestation for the ledger's exact terminal bytes."""
    ledger = Path(ledger_path)
    output = Path(output_path) if output_path is not None else attestation_path_for(ledger)
    document = _document(ledger, ledger.read_bytes(), write_failures=write_failures)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f"{output.name}.tmp.{os.getpid()}")
    try:
        temporary.write_text(
            json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        os.replace(temporary, output)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return document


def validate_attestation(
    ledger_path: str | os.PathLike[str],
    attestation_path: str | os.PathLike[str] | None = None,
) -> bool:
    """Recompute and exact-compare a zero-write-failure terminal attestation."""
    ledger = Path(ledger_path)
    attestation = (
        Path(attestation_path)
        if attestation_path is not None
        else attestation_path_for(ledger)
    )
    try:
        raw = ledger.read_bytes()
        document = json.loads(attestation.read_text(encoding="utf-8"))
        if not isinstance(document, dict) or document.get("schema") != SCHEMA:
            return False
        if document.get("ledger_filename") != ledger.name:
            return False
        expected = _document(
            ledger, raw, write_failures=int(document.get("write_failures", -1))
        )
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        return False
    return document == expected and document.get("write_failures") == 0
