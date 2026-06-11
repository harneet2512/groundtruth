"""Centralized delivery path policy — vendored/generated/minified exclusions."""
from __future__ import annotations

import os

_VENDOR_DIR_MARKERS: tuple[str, ...] = (
    "/extern/", "/externals/", "/vendor/", "/vendored/", "/third_party/",
    "/thirdparty/", "/node_modules/", "/bower_components/", "/dist/",
    "/_generated/", "/generated/", "/site-packages/",
    "/static/", "/assets/",
)
_MINIFIED_SUFFIXES: tuple[str, ...] = (".min.js", ".min.css", ".min.mjs", ".min.map")
_GENERATED_FILE_MARKERS: tuple[str, ...] = (
    "zz_generated", ".pb.go", ".pb.gw.go", "_pb2.py", "_pb2_grpc.py",
    ".generated.", "/generated/", "_generated.go", ".g.dart", ".freezed.dart",
)
# post_view legacy segment patterns (subset; path_policy is superset)
_VENDOR_SEGMENT_PATTERNS: tuple[str, ...] = (
    "static/", "vendor/", "node_modules/", "dist/", ".min.", "assets/",
)


def _norm(fp: str) -> str:
    return "/" + (fp or "").replace("\\", "/").lstrip("./").lstrip("/").lower()


def is_vendored_path(fp: str) -> bool:
    """Path-class filter: vendored / minified / generated — never a DELIVERED fact."""
    f = _norm(fp)
    if any(m in f for m in _VENDOR_DIR_MARKERS):
        return True
    base = f.rsplit("/", 1)[-1]
    if base.endswith(_MINIFIED_SUFFIXES):
        return True
    if any(m in base for m in _GENERATED_FILE_MARKERS):
        return True
    norm = fp.replace("\\", "/")
    for p in _VENDOR_SEGMENT_PATTERNS:
        if p == ".min.":
            if ".min." in norm:
                return True
        elif f"/{p}" in norm or norm.startswith(p):
            return True
    return False


def is_generated(fp: str) -> bool:
    """Machine-generated files (protobuf, codegen) — ranking demote, not hard drop."""
    f = (fp or "").lower()
    return any(m in f for m in _GENERATED_FILE_MARKERS)


def is_delivery_excluded(fp: str, repo_root: str = "") -> bool:
    """True when path must never appear in a DELIVERED fact."""
    if is_vendored_path(fp):
        return True
    if repo_root:
        return is_minified_file(repo_root, fp)
    return False


_minified_cache: dict[str, bool] = {}
_MINIFIED_MEAN_LINE_LEN = 200


def is_minified_file(repo_root: str, rel: str) -> bool:
    if rel in _minified_cache:
        return _minified_cache[rel]
    verdict = False
    try:
        with open(os.path.join(repo_root or "", rel), encoding="utf-8", errors="ignore") as fh:
            head = fh.read(16384)
        lines = [ln for ln in head.splitlines() if ln.strip()]
        if lines:
            verdict = (sum(len(ln) for ln in lines) / len(lines)) > _MINIFIED_MEAN_LINE_LEN
    except OSError:
        verdict = False
    _minified_cache[rel] = verdict
    return verdict
