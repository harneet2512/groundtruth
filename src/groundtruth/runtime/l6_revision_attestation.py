"""Immutable carrier for an L6 graph-reindex observation.

The carrier binds one edit action and graph generation to the exact post-edit source
bytes, repository root, authoritative source graph, selected graph before and after
the subprocess, and the subprocess result.
It deliberately performs no filesystem I/O and makes no delivery, acknowledgment,
causality, or SS-LIVE claim.  The L6 producer owns byte hashing; a downstream join
must separately prove that a delivered producer read ``selected_db_after.sha256``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping


L6_REVISION_ATTESTATION_SCHEMA = "gt.l6_revision_attestation.v1"
_SHA256_WIDTH = 64
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


class L6RevisionAttestationError(ValueError):
    """A strict decoding or validation failure."""


@dataclass(frozen=True, order=True)
class DbIdentity:
    """Path and full SHA-256 of one exact graph database byte sequence."""

    path: str
    sha256: str


@dataclass(frozen=True, order=True)
class SubprocessResult:
    """Deterministic result identity for the exact reindex invocation."""

    argv: tuple[str, ...]
    returncode: int | None
    timed_out: bool
    exception_type: str
    stdout_sha256: str
    stdout_bytes: int
    stderr_sha256: str
    stderr_bytes: int


@dataclass(frozen=True)
class L6RevisionAttestation:
    """One immutable, audit-only graph-revision observation."""

    schema: str
    action_count: int
    graph_generation: int
    repo_root: str
    edited_path: str
    edited_source_sha256: str
    source_db: DbIdentity
    selected_db_before: DbIdentity
    selected_db_after: DbIdentity
    subprocess: SubprocessResult

    @property
    def reindex_succeeded(self) -> bool:
        """Whether process execution succeeded; this is not an SS promotion."""

        return (
            self.subprocess.returncode == 0
            and not self.subprocess.timed_out
            and not self.subprocess.exception_type
        )

    @property
    def revision_advanced(self) -> bool:
        """Whether the selected database bytes changed across the invocation."""

        return self.selected_db_before.sha256 != self.selected_db_after.sha256


def _is_nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == _SHA256_WIDTH
        and all(char in "0123456789abcdef" for char in value)
    )


def _is_text_identity(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and "\x00" not in value


def _validate_db(identity: object, prefix: str) -> list[str]:
    if not isinstance(identity, DbIdentity):
        return [f"{prefix}:wrong_type"]
    errors: list[str] = []
    if not _is_text_identity(identity.path):
        errors.append(f"{prefix}:path:invalid")
    if not _is_sha256(identity.sha256):
        errors.append(f"{prefix}:sha256:not_64_lower_hex")
    return errors


def _validate_subprocess(result: object) -> list[str]:
    if not isinstance(result, SubprocessResult):
        return ["subprocess:wrong_type"]
    errors: list[str] = []
    if not isinstance(result.argv, tuple) or not result.argv:
        errors.append("subprocess:argv:not_nonempty_tuple")
    elif not all(_is_text_identity(value) for value in result.argv):
        for index, value in enumerate(result.argv):
            if not _is_text_identity(value):
                errors.append(f"subprocess:argv[{index}]:invalid")
    if result.returncode is not None and (
        not isinstance(result.returncode, int) or isinstance(result.returncode, bool)
    ):
        errors.append("subprocess:returncode:not_int")
    if not isinstance(result.timed_out, bool):
        errors.append("subprocess:timed_out:not_bool")
    if not isinstance(result.exception_type, str) or "\x00" in result.exception_type:
        errors.append("subprocess:exception_type:invalid")
    if result.timed_out and result.returncode is not None:
        errors.append("subprocess:timed_out:returncode_forbidden")
    if result.exception_type and result.returncode is not None:
        errors.append("subprocess:exception:returncode_forbidden")
    if result.returncode is None and not result.timed_out and not result.exception_type:
        errors.append("subprocess:returncode:not_int")
    for stream in ("stdout", "stderr"):
        digest = getattr(result, f"{stream}_sha256", None)
        length = getattr(result, f"{stream}_bytes", None)
        if not _is_sha256(digest):
            errors.append(f"subprocess:{stream}_sha256:not_64_lower_hex")
        if not _is_nonnegative_int(length):
            errors.append(f"subprocess:{stream}_bytes:not_nonnegative_int")
        elif length == 0 and digest != _EMPTY_SHA256:
            errors.append(f"subprocess:{stream}_sha256:not_empty_digest")
    return errors


def validate(attestation: object) -> tuple[str, ...]:
    """Return all deterministic contract violations; absence never becomes proof."""

    if not isinstance(attestation, L6RevisionAttestation):
        return ("attestation:wrong_type",)
    errors: list[str] = []
    if attestation.schema != L6_REVISION_ATTESTATION_SCHEMA:
        errors.append(f"schema:unsupported:{attestation.schema!r}")
    if not _is_nonnegative_int(attestation.action_count):
        errors.append("action_count:not_nonnegative_int")
    if not _is_nonnegative_int(attestation.graph_generation):
        errors.append("graph_generation:not_nonnegative_int")
    if not _is_text_identity(attestation.repo_root):
        errors.append("repo_root:invalid")
    if not _is_text_identity(attestation.edited_path):
        errors.append("edited_path:invalid")
    if not _is_sha256(attestation.edited_source_sha256):
        errors.append("edited_source_sha256:not_64_lower_hex")
    errors.extend(_validate_db(attestation.source_db, "source_db"))
    errors.extend(_validate_db(attestation.selected_db_before, "selected_db_before"))
    errors.extend(_validate_db(attestation.selected_db_after, "selected_db_after"))
    if (
        isinstance(attestation.selected_db_before, DbIdentity)
        and isinstance(attestation.selected_db_after, DbIdentity)
        and attestation.selected_db_before.path != attestation.selected_db_after.path
    ):
        errors.append("selected_db_after:path:mismatch")
    errors.extend(_validate_subprocess(attestation.subprocess))
    if isinstance(attestation.subprocess, SubprocessResult):
        if f"-root={attestation.repo_root}" not in attestation.subprocess.argv:
            errors.append("subprocess:argv:repo_root_unbound")
        if f"-file={attestation.edited_path}" not in attestation.subprocess.argv:
            errors.append("subprocess:argv:edited_path_unbound")
        if (
            isinstance(attestation.selected_db_before, DbIdentity)
            and f"-output={attestation.selected_db_before.path}"
            not in attestation.subprocess.argv
        ):
            errors.append("subprocess:argv:selected_db_unbound")
    return tuple(errors)


def _db_to_dict(identity: DbIdentity) -> dict[str, str]:
    return {"path": identity.path, "sha256": identity.sha256}


def _subprocess_to_dict(result: SubprocessResult) -> dict[str, Any]:
    return {
        "argv": list(result.argv),
        "returncode": result.returncode,
        "timed_out": result.timed_out,
        "exception_type": result.exception_type,
        "stdout_sha256": result.stdout_sha256,
        "stdout_bytes": result.stdout_bytes,
        "stderr_sha256": result.stderr_sha256,
        "stderr_bytes": result.stderr_bytes,
    }


def to_dict(attestation: L6RevisionAttestation) -> dict[str, Any]:
    """Return the exact JSON-native representation used by the commitment."""

    errors = validate(attestation)
    if errors:
        raise L6RevisionAttestationError("attestation:invalid:" + "|".join(errors))
    return {
        "schema": attestation.schema,
        "action_count": attestation.action_count,
        "graph_generation": attestation.graph_generation,
        "repo_root": attestation.repo_root,
        "edited_path": attestation.edited_path,
        "edited_source_sha256": attestation.edited_source_sha256,
        "source_db": _db_to_dict(attestation.source_db),
        "selected_db_before": _db_to_dict(attestation.selected_db_before),
        "selected_db_after": _db_to_dict(attestation.selected_db_after),
        "subprocess": _subprocess_to_dict(attestation.subprocess),
    }


def canonical_bytes(attestation: L6RevisionAttestation) -> bytes:
    """Canonical UTF-8 JSON bytes, stable across processes and hash seeds."""

    return json.dumps(
        to_dict(attestation),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def canonical_sha256(attestation: L6RevisionAttestation) -> str:
    """Full commitment to every carrier field."""

    return hashlib.sha256(canonical_bytes(attestation)).hexdigest()


_TOP_FIELDS = frozenset(
    {
        "schema",
        "action_count",
        "graph_generation",
        "repo_root",
        "edited_path",
        "edited_source_sha256",
        "source_db",
        "selected_db_before",
        "selected_db_after",
        "subprocess",
    }
)
_DB_FIELDS = frozenset({"path", "sha256"})
_SUBPROCESS_FIELDS = frozenset(
    {
        "argv",
        "returncode",
        "timed_out",
        "exception_type",
        "stdout_sha256",
        "stdout_bytes",
        "stderr_sha256",
        "stderr_bytes",
    }
)


def _exact_fields(value: object, expected: frozenset[str], prefix: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise L6RevisionAttestationError(f"{prefix}:not_mapping")
    actual = set(value)
    missing = sorted(expected - actual)
    unknown = sorted((actual - expected), key=repr)
    if missing:
        raise L6RevisionAttestationError(f"{prefix}:missing_fields:{missing!r}")
    if unknown:
        raise L6RevisionAttestationError(f"{prefix}:unknown_fields:{unknown!r}")
    return value


def _db_from_dict(value: object, prefix: str) -> DbIdentity:
    raw = _exact_fields(value, _DB_FIELDS, prefix)
    return DbIdentity(path=raw["path"], sha256=raw["sha256"])


def _subprocess_from_dict(value: object) -> SubprocessResult:
    raw = _exact_fields(value, _SUBPROCESS_FIELDS, "subprocess")
    argv = raw["argv"]
    if not isinstance(argv, list):
        raise L6RevisionAttestationError("subprocess:argv:not_list")
    return SubprocessResult(
        argv=tuple(argv),
        returncode=raw["returncode"],
        timed_out=raw["timed_out"],
        exception_type=raw["exception_type"],
        stdout_sha256=raw["stdout_sha256"],
        stdout_bytes=raw["stdout_bytes"],
        stderr_sha256=raw["stderr_sha256"],
        stderr_bytes=raw["stderr_bytes"],
    )


def from_dict(value: object) -> L6RevisionAttestation:
    """Strict inverse of :func:`to_dict`; rejects coercion and schema drift."""

    raw = _exact_fields(value, _TOP_FIELDS, "attestation")
    attestation = L6RevisionAttestation(
        schema=raw["schema"],
        action_count=raw["action_count"],
        graph_generation=raw["graph_generation"],
        repo_root=raw["repo_root"],
        edited_path=raw["edited_path"],
        edited_source_sha256=raw["edited_source_sha256"],
        source_db=_db_from_dict(raw["source_db"], "source_db"),
        selected_db_before=_db_from_dict(
            raw["selected_db_before"], "selected_db_before"
        ),
        selected_db_after=_db_from_dict(raw["selected_db_after"], "selected_db_after"),
        subprocess=_subprocess_from_dict(raw["subprocess"]),
    )
    errors = validate(attestation)
    if errors:
        raise L6RevisionAttestationError("attestation:invalid:" + "|".join(errors))
    return attestation


__all__ = [
    "L6_REVISION_ATTESTATION_SCHEMA",
    "DbIdentity",
    "L6RevisionAttestation",
    "L6RevisionAttestationError",
    "SubprocessResult",
    "canonical_bytes",
    "canonical_sha256",
    "from_dict",
    "to_dict",
    "validate",
]
