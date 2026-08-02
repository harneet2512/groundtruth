#!/usr/bin/env python3
"""Write and validate dep-store manifests for substrate proof (P0-04/P0-05, P1-02).

Records which dependency stores were discovered/copied from the task image and
fail-closed when a language requires a store that is missing or empty.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from pathlib import Path
from typing import Any


SCHEMA = "gt.dep_store_manifest.v1"


def _dir_stats(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.is_dir():
        return {"path": str(p), "exists": False, "file_count": 0, "total_bytes": 0}
    file_count = 0
    total_bytes = 0
    for root, _dirs, files in os.walk(p):
        for name in files:
            fp = Path(root) / name
            try:
                total_bytes += fp.stat().st_size
                file_count += 1
            except OSError:
                pass
    return {
        "path": str(p.resolve()),
        "exists": True,
        "file_count": file_count,
        "total_bytes": total_bytes,
    }


def _find_rust_src(rustup_host: str | Path, active_toolchain: str = "") -> dict[str, Any]:
    root = Path(rustup_host)
    if not root.is_dir():
        return {
            "path": str(root),
            "exists": False,
            "file_count": 0,
            "total_bytes": 0,
            "active_toolchain": active_toolchain or None,
            "source_in_task_image": None,
        }
    rel = os.path.join("lib", "rustlib", "src", "rust", "library")
    candidates: list[Path] = []
    if active_toolchain:
        target = root / "toolchains" / active_toolchain / rel
        stats = _dir_stats(target)
        stats["active_toolchain"] = active_toolchain or None
        stats["source_in_task_image"] = None
        if stats.get("exists") and int(stats.get("file_count") or 0) > 0:
            return stats
        return stats
    candidates.extend(
        Path(p) for p in glob.glob(str(root / "toolchains" / "*" / rel))
    )
    seen: set[str] = set()
    for cand in candidates:
        key = str(cand)
        if key in seen:
            continue
        seen.add(key)
        stats = _dir_stats(cand)
        if stats.get("exists") and int(stats.get("file_count") or 0) > 0:
            stats["active_toolchain"] = active_toolchain or None
            stats["source_in_task_image"] = None
            return stats
    return {
        **_dir_stats(root / "toolchains" / "*" / rel),
        "active_toolchain": active_toolchain or None,
        "source_in_task_image": None,
    }


def build_manifest(
    *,
    language: str,
    gomodcache_host: str,
    gomodcache_source: str,
    gomodcache_declared_source: str = "",
    cargo_host: str,
    cargo_source: str,
    rustup_host: str,
    rustup_source: str,
    rust_toolchain: str = "",
) -> dict[str, Any]:
    rust_src = _find_rust_src(rustup_host, rust_toolchain)
    if rustup_source and rust_src.get("exists"):
        active_toolchain = rust_src.get("active_toolchain")
        if active_toolchain:
            rust_src["source_in_task_image"] = (
                f"{rustup_source}/toolchains/{active_toolchain}/lib/rustlib/src/rust/library"
            )
        else:
            rust_src["source_in_task_image"] = f"{rustup_source}/toolchains/*/lib/rustlib/src/rust/library"
    return {
        "schema": SCHEMA,
        "language": language,
        "stores": {
            "gomodcache": {
                "source_in_task_image": gomodcache_source or None,
                "declared_in_task_image": gomodcache_declared_source or None,
                **_dir_stats(gomodcache_host),
            },
            "cargo": {
                "source_in_task_image": cargo_source or None,
                **_dir_stats(cargo_host),
            },
            "rustup": {
                "source_in_task_image": rustup_source or None,
                **_dir_stats(rustup_host),
            },
            "rust_src": rust_src,
        },
    }


def validate_manifest(manifest: dict[str, Any]) -> list[str]:
    """Return human-readable violations (empty == ok)."""
    problems: list[str] = []
    lang = (manifest.get("language") or "").strip().lower()
    stores = manifest.get("stores") or {}

    if lang == "rust":
        for key in ("cargo", "rustup"):
            st = stores.get(key) or {}
            if not st.get("exists") or int(st.get("file_count") or 0) == 0:
                problems.append(
                    f"DEP_STORE_EMPTY: rust task requires non-empty {key} "
                    f"(source={st.get('source_in_task_image')!r}, "
                    f"path={st.get('path')!r})"
                )
        rust_src = stores.get("rust_src") or {}
        if not rust_src.get("exists") or int(rust_src.get("file_count") or 0) == 0:
            problems.append(
                "DEP_STORE_EMPTY: rust task requires non-empty rust_src "
                f"(source={rust_src.get('source_in_task_image')!r}, "
                f"path={rust_src.get('path')!r})"
            )
    return problems


def write_manifest(path: str | Path, manifest: dict[str, Any]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="GT dep-store manifest writer/validator")
    ap.add_argument("--out", required=True, help="manifest JSON path")
    ap.add_argument("--language", required=True)
    ap.add_argument("--gomodcache-host", default="/tmp/gt/deps/gomodcache")
    ap.add_argument("--gomodcache-source", default="")
    ap.add_argument("--gomodcache-declared-source", default="")
    ap.add_argument("--cargo-host", default="/tmp/gt/deps/cargo")
    ap.add_argument("--cargo-source", default="")
    ap.add_argument("--rustup-host", default="/tmp/gt/deps/rustup")
    ap.add_argument("--rustup-source", default="")
    ap.add_argument("--rust-toolchain", default="")
    ap.add_argument("--validate-only", action="store_true")
    args = ap.parse_args(argv)

    if args.validate_only:
        if not os.path.exists(args.out):
            print(f"DEP_STORE_MANIFEST_MISSING: {args.out}", file=sys.stderr)
            return 2
        with open(args.out, encoding="utf-8") as f:
            manifest = json.load(f)
    else:
        manifest = build_manifest(
            language=args.language,
            gomodcache_host=args.gomodcache_host,
            gomodcache_source=args.gomodcache_source,
            gomodcache_declared_source=args.gomodcache_declared_source,
            cargo_host=args.cargo_host,
            cargo_source=args.cargo_source,
            rustup_host=args.rustup_host,
            rustup_source=args.rustup_source,
            rust_toolchain=args.rust_toolchain,
        )
        write_manifest(args.out, manifest)

    problems = validate_manifest(manifest)
    if problems:
        for p in problems:
            print(p, file=sys.stderr)
        return 2
    print(f"dep_store_manifest ok: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
