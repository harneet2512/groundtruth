"""Generate the terminal language-operation matrix from the Go registry specs.

The Go package remains authoritative. This bootstrap generator mirrors only the
public ``LanguageManifest`` projection so repositories without a Go toolchain can
refresh the checked JSON; the Go test compares its bytes with the live registry.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
from typing import Any, cast


OPERATIONS = (
    "callers",
    "definition",
    "exact_literal_search",
    "patch_impact",
    "references",
    "syntax",
    "verification_status",
)
SYNTAX_EXACT = frozenset({"go", "javascript", "python", "ruby", "typescript"})


def _array(text: str, field: str) -> list[str]:
    match = re.search(rf"{field}:\s*\[\]string\{{(?P<body>.*?)\}}", text, re.DOTALL)
    if match is None:
        return []
    return re.findall(r'"([^"\\]*(?:\\.[^"\\]*)*)"', match.group("body"))


def _string(text: str, field: str) -> str:
    match = re.search(rf'{field}:\s*"([^"\\]*(?:\\.[^"\\]*)*)"', text)
    return match.group(1) if match else ""


def registry_manifest(spec_dir: Path) -> dict[str, object]:
    languages = []
    ignored = {"spec.go", "manifest_test.go", "compatibility.go", "compatibility_test.go"}
    for path in sorted(spec_dir.glob("*.go")):
        if path.name in ignored:
            continue
        text = path.read_text(encoding="utf-8")
        name_match = re.search(r'Name:\s*"([a-z0-9_+-]+)"', text)
        if name_match is None:
            continue
        test_pattern = bool(
            re.search(r"TestFuncPattern:\s*(?:`[^`]+`|\"[^\"]+\")", text)
            or _array(text, "AssertionPatterns")
        )
        languages.append(
            {
                "name": name_match.group(1),
                "extensions": sorted(_array(text, "Extensions")),
                "definitions": bool(_array(text, "FunctionNodes") or _array(text, "ClassNodes")),
                "calls": bool(_array(text, "CallNodes")),
                "imports": bool(_array(text, "ImportNodes")),
                "bodies": bool(_string(text, "BodyField")),
                "parameters": bool(_string(text, "ParamsField")),
                "return_types": bool(_string(text, "ReturnTypeField")),
                "test_patterns": test_pattern,
            }
        )
    languages.sort(key=lambda item: item["name"])
    return {"schema": "gt.language_manifest.v1", "languages": languages}


def compatibility_document(source_manifest: dict[str, object]) -> dict[str, object]:
    manifest_bytes = json.dumps(
        source_manifest, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    rows = []
    languages = cast(list[dict[str, Any]], source_manifest["languages"])
    for language in languages:
        identity = language["name"]
        for operation in OPERATIONS:
            semantics = "removed"
            if operation == "exact_literal_search":
                semantics = "exact"
            elif operation == "verification_status":
                semantics = "execution_specific"
            elif operation == "syntax" and identity in SYNTAX_EXACT:
                semantics = "exact"
            rows.append(
                {
                    "registry_identity": identity,
                    "operation": operation,
                    "terminal_semantics": semantics,
                }
            )
    return {
        "schema": "gt.language_operation_compatibility.v1",
        "source_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "rows": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--spec-dir", type=Path,
        default=Path(__file__).resolve().parents[1] / "gt-index" / "internal" / "specs",
    )
    parser.add_argument(
        "--output", type=Path,
        default=Path(__file__).resolve().parents[1] / "src" / "groundtruth" / "runtime" / "generated_language_operation_compatibility.json",
    )
    args = parser.parse_args()
    document = compatibility_document(registry_manifest(args.spec_dir))
    rows = cast(list[dict[str, str]], document["rows"])
    if len(rows) != 210:
        raise SystemExit(f"expected 210 rows, got {len(rows)}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(
        json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )


if __name__ == "__main__":
    main()
