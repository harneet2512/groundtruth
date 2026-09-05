"""Fail-open shadow integration for immutable legacy localization projections."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import threading
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Sequence

from .engine import localize_vnext
from .model import LocalizationRequest


def _norm_path(path: str) -> str:
    normalized = (path or "").replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.lstrip("/")


def shadow_enabled() -> bool:
    return os.getenv("GT_LOC_VNEXT_SHADOW", "0") == "1"


def _primitive(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _primitive(asdict(value))
    if isinstance(value, dict):
        return {str(key): _primitive(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_primitive(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        _primitive(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _revision_identity(repository_root: str, graph_db: str) -> str:
    explicit = os.getenv("GT_REVISION_ID", "").strip()
    if explicit:
        return explicit
    head = Path(repository_root) / ".git" / "HEAD"
    try:
        raw = head.read_text(encoding="utf-8", errors="replace").strip()
        if raw.startswith("ref:"):
            ref = Path(repository_root) / ".git" / raw.split(":", 1)[1].strip()
            if ref.is_file():
                return ref.read_text(encoding="ascii", errors="replace").strip()
        if raw:
            return raw
    except OSError:
        pass
    try:
        stat = Path(graph_db).stat()
        return f"graph:{stat.st_size}:{stat.st_mtime_ns}"
    except OSError:
        return "unknown"


def legacy_discoveries_from_projection(
    result: Any,
    source_projection: str,
) -> Sequence[Any]:
    if source_projection == "localize":
        candidates = tuple(getattr(result, "candidates", ()) or ())
        semantic_paths = {
            _norm_path(str(path)) for path in (getattr(result, "semantic_body_paths", ()) or ())
        }
        content_paths = {
            _norm_path(str(path)) for path in (getattr(result, "content_leg_paths", ()) or ())
        }
        signals_by_file = dict(getattr(result, "signals_by_file", {}) or {})
        signal_rows = []
        for candidate in candidates:
            path = _norm_path(str(getattr(candidate, "file_path", "")))
            signals = set(signals_by_file.get(path, ()) or ())
            signal_rows.append(
                {
                    "path": path,
                    "score": float(getattr(candidate, "score", 0.0) or 0.0),
                    "symbol": "",
                    "entered_via": ("graph_rescue" if "structural" in signals else "semantic_seed"),
                    "components": {
                        "sem": 1.0 if path in semantic_paths or "semantic" in signals else 0.0,
                        "lex": 1.0 if path in content_paths or "grep" in signals else 0.0,
                        "reach": 1.0 if "structural" in signals else 0.0,
                    },
                }
            )
        return (*candidates, *signal_rows)
    if source_projection == "run_v74":
        return tuple(getattr(result, "ranked_full", ()) or ())
    return ()


def _legacy_snapshot(result: Any, source_projection: str) -> dict[str, Any]:
    primitive = _primitive(result)
    output_bytes = _canonical_bytes(primitive)
    snapshot: dict[str, Any] = {
        "projection": source_projection,
        "output_sha256": hashlib.sha256(output_bytes).hexdigest(),
        "output": primitive,
    }
    if source_projection == "localize":
        snapshot["candidate_order"] = [
            str(getattr(candidate, "file_path", ""))
            for candidate in getattr(result, "candidates", ()) or ()
        ]
        snapshot["scores"] = [
            float(getattr(candidate, "score", 0.0) or 0.0)
            for candidate in getattr(result, "candidates", ()) or ()
        ]
        snapshot["witnesses"] = [
            str(candidate.render_witness()) for candidate in getattr(result, "candidates", ()) or ()
        ]
        snapshot["confidence"] = float(getattr(result, "confidence", 0.0) or 0.0)
        snapshot["gate_reason"] = str(getattr(result, "gate_reason", ""))
    elif source_projection == "run_v74":
        snapshot["candidate_order"] = [
            str(row.get("path", "")) for row in getattr(result, "ranked_full", ()) or ()
        ]
        snapshot["scores"] = [
            float(row.get("score", 0.0) or 0.0) for row in getattr(result, "ranked_full", ()) or ()
        ]
        snapshot["elapsed_ms"] = int(getattr(result, "elapsed_ms", 0) or 0)
    return snapshot


def _sidecar_directory() -> Path:
    configured = os.getenv("GT_LOC_VNEXT_SIDECAR_DIR", "").strip()
    if configured:
        return Path(configured)
    return Path(tempfile.gettempdir()) / "groundtruth-localization-vnext"


def write_shadow_sidecar(payload: dict[str, Any], path: Path) -> None:
    """Atomically publish one complete sidecar; never expose a partial JSON file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(
        payload,
        sort_keys=True,
        indent=2,
        ensure_ascii=False,
    ).encode("utf-8")
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.{threading.get_ident()}.tmp")
    temporary.write_bytes(encoded)
    os.replace(temporary, path)


def record_shadow_projection(
    *,
    issue_text: str,
    repository_root: str,
    graph_db: str,
    legacy_result: Any,
    source_projection: str,
) -> Path | None:
    """Compute and persist vNext without allowing any failure into legacy."""
    if not shadow_enabled():
        return None
    try:
        revision = _revision_identity(repository_root, graph_db)
        request = LocalizationRequest(
            issue_text=issue_text,
            repository_root=repository_root,
            graph_db=graph_db,
            revision_identity=revision,
        )
        result = localize_vnext(
            request,
            legacy_discoveries=legacy_discoveries_from_projection(
                legacy_result,
                source_projection,
            ),
        )
        issue_hash = hashlib.sha256((issue_text or "").encode("utf-8")).hexdigest()[:16]
        revision_hash = hashlib.sha256(revision.encode("utf-8")).hexdigest()[:12]
        path = _sidecar_directory() / (
            f"{source_projection}.{issue_hash}.{revision_hash}.gt.localization.vnext.v1.json"
        )
        payload = {
            "schema": "gt.localization.vnext.v1",
            "source_projection": source_projection,
            "request": {
                "issue_sha256": hashlib.sha256((issue_text or "").encode("utf-8")).hexdigest(),
                "repository_root_sha256": hashlib.sha256(
                    str(Path(repository_root).resolve()).encode("utf-8", "replace")
                ).hexdigest(),
                "graph_db_sha256": hashlib.sha256(
                    str(Path(graph_db).resolve()).encode("utf-8", "replace")
                ).hexdigest(),
                "revision_identity": revision,
            },
            "legacy": _legacy_snapshot(legacy_result, source_projection),
            "vnext": result.to_dict(),
        }
        write_shadow_sidecar(payload, path)
        return path
    except Exception as exc:
        if os.getenv("GT_LOC_VNEXT_DEBUG", "0") == "1":
            print(f"[GT LOC VNEXT SHADOW] fail-open: {exc!r}", file=sys.stderr)
        return None


__all__ = [
    "record_shadow_projection",
    "legacy_discoveries_from_projection",
    "shadow_enabled",
    "write_shadow_sidecar",
]
