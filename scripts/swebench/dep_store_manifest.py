#!/usr/bin/env python3
"""Write and validate dep-store manifests for substrate proof (P0-04/P0-05, P1-02).

Records which dependency stores were discovered/copied from the task image and
fail-closed when a language requires a store that is missing or empty.
"""
from __future__ import annotations

import argparse
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


def build_manifest(
    *,
    language: str,
    gomodcache_host: str,
    gomodcache_source: str,
    cargo_host: str,
    cargo_source: str,
    rustup_host: str,
    rustup_source: str,
) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "language": language,
        "stores": {
            "gomodcache": {
                "source_in_task_image": gomodcache_source or None,
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
        },
    }


def validate_manifest(manifest: dict[str, Any]) -> list[str]:
    """Return human-readable violations (empty == ok)."""
    problems: list[str] = []
    lang = (manifest.get("language") or "").strip().lower()
    stores = manifest.get("stores") or {}

    if lang == "go":
        gm = stores.get("gomodcache") or {}
        if not gm.get("exists") or int(gm.get("file_count") or 0) == 0:
            problems.append(
                "DEP_STORE_EMPTY: go task requires non-empty gomodcache "
                f"(source={gm.get('source_in_task_image')!r}, "
                f"path={gm.get('path')!r})"
            )

    if lang == "rust":
        for key in ("cargo", "rustup"):
            st = stores.get(key) or {}
            if not st.get("exists") or int(st.get("file_count") or 0) == 0:
                problems.append(
                    f"DEP_STORE_EMPTY: rust task requires non-empty {key} "
                    f"(source={st.get('source_in_task_image')!r}, "
                    f"path={st.get('path')!r})"
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
    ap.add_argument("--cargo-host", default="/tmp/gt/deps/cargo")
    ap.add_argument("--cargo-source", default="")
    ap.add_argument("--rustup-host", default="/tmp/gt/deps/rustup")
    ap.add_argument("--rustup-source", default="")
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
            cargo_host=args.cargo_host,
            cargo_source=args.cargo_source,
            rustup_host=args.rustup_host,
            rustup_source=args.rustup_source,
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
