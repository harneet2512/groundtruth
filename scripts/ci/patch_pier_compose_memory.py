#!/usr/bin/env python3
"""Patch datacurve-pier 0.2.0's docker compose base with runtime memory caps.

Pier 0.2.0 ships only deploy.resources.limits.memory, which plain
`docker compose up` ignores unless compose is run in compatibility mode. The
DeepSWE harness uses plain compose through pier, so add the compose-spec runtime
keys that are honored in this path.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def compose_base_path() -> Path:
    spec = importlib.util.find_spec("pier")
    if not spec or not spec.origin:
        raise SystemExit("PIER_COMPOSE_PATCH_FAIL: pier package not importable")
    return (
        Path(spec.origin).parent
        / "environments"
        / "docker"
        / "docker-compose-base.yaml"
    )


def patch_text(raw: str) -> tuple[str, bool]:
    lines = raw.splitlines()
    changed = False
    out: list[str] = []
    in_main = False
    inserted = False
    have_mem_limit = False
    have_memswap_limit = False

    for line in lines:
        if line.startswith("  main:"):
            in_main = True
            out.append(line)
            continue

        if in_main and line.startswith("  ") and not line.startswith("    "):
            if not have_mem_limit:
                out.append("    mem_limit: ${MEMORY}")
                changed = True
            if not have_memswap_limit:
                out.append("    memswap_limit: ${MEMORY}")
                changed = True
            inserted = True
            in_main = False

        if in_main and line.startswith("    mem_limit:"):
            have_mem_limit = True
            if line != "    mem_limit: ${MEMORY}":
                line = "    mem_limit: ${MEMORY}"
                changed = True
        elif in_main and line.startswith("    memswap_limit:"):
            have_memswap_limit = True
            if line != "    memswap_limit: ${MEMORY}":
                line = "    memswap_limit: ${MEMORY}"
                changed = True

        out.append(line)

    if in_main and not inserted:
        if not have_mem_limit:
            out.append("    mem_limit: ${MEMORY}")
            changed = True
        if not have_memswap_limit:
            out.append("    memswap_limit: ${MEMORY}")
            changed = True

    patched = "\n".join(out) + ("\n" if raw.endswith("\n") else "")
    if "    mem_limit: ${MEMORY}" not in patched:
        raise SystemExit("PIER_COMPOSE_PATCH_FAIL: mem_limit missing after patch")
    if "    memswap_limit: ${MEMORY}" not in patched:
        raise SystemExit("PIER_COMPOSE_PATCH_FAIL: memswap_limit missing after patch")
    return patched, changed


def main() -> int:
    path = compose_base_path()
    if not path.is_file():
        raise SystemExit(f"PIER_COMPOSE_PATCH_FAIL: compose base missing at {path}")
    raw = path.read_text(encoding="utf-8")
    patched, changed = patch_text(raw)
    if changed:
        path.write_text(patched, encoding="utf-8")
        print(f"pier compose memory patch applied: {path}")
    else:
        print(f"pier compose memory patch already present: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
