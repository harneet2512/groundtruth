#!/usr/bin/env python
"""Build a localization corpus with MULTI-FILE, LINE-RANGE and SYMBOL gold.

The oss-60 corpus carries `gold_files` only, exactly one file per case, so every
precision metric is unscorable and there are no multi-file fixes at all.
SWE-bench-Live Lite ships the real fix patch per instance, from which file, line
and symbol gold can be derived deterministically.

Gold is derived ONLY from the fix patch (`patch`), never from `test_patch`, and
never from anything the localization engine can see. The engine receives issue
text, repo and base commit; gold is joined afterwards by the scorer.

SYMBOL DERIVATION - read this before changing it. git's hunk header shows the last
context line matching the funcname pattern BEFORE the hunk starts, so when a fix
adds or edits a definition the header names the PRECEDING SIBLING. Measured on
this dataset: in 92 of 92 hunks where the header named a symbol and the body
defined one, the header named a DIFFERENT symbol - never once the edited one. So
the header is used ONLY as the enclosing-scope fallback for a hunk whose body
defines nothing; otherwise the symbol is the nearest definition at or above each
CHANGED line inside the hunk. `_enclosing_symbol` is that one decision, and the
build ABORTS if any hunk reports the header while its own changed lines define
something - the exact regression this paragraph warns about.

LINE COORDINATES - read this before changing it. The localization engine indexes
the repository at `base_commit`, i.e. the PRE-fix tree, and every region it emits
carries line numbers of THAT tree. Gold line numbers must therefore be pre-image
numbers, taken from the hunk's '-' side: a removed line IS a pre-image line, an
added line is not, so a run of additions anchors on the last pre-image line
before it (the line it is inserted after). A hunk with no pre-image line at all -
a new file - yields NO range rather than a fabricated one; the file and its
symbols stay gold. Cases carry `gold_line_coordinates` so no consumer can join
these numbers to a post-fix artifact by accident.

Usage:
    python scripts/build_localization_gold_corpus.py \
        --input benchmarks/data/swebench_live_lite.jsonl \
        --out benchmarks/data/swebench_live_gold_cases.json \
        --repos-out benchmarks/data/swebench_live_gold_repos.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, NamedTuple

# Only source files can be a localization target. A doc or changelog edit is a
# real part of the commit but is not what "find the code to change" means.
SUFFIX_LANGUAGE = {
    ".py": "python", ".pyi": "python",
    ".go": "go",
    ".rs": "rust",
    ".ts": "typescript", ".tsx": "typescript",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript",
    ".java": "java", ".rb": "ruby", ".c": "c", ".h": "c",
    ".cc": "cpp", ".cpp": "cpp", ".hpp": "cpp", ".cs": "csharp",
    ".kt": "kotlin", ".swift": "swift", ".php": "php", ".scala": "scala",
}
# ANCHORED path segments. A bare "test_" substring also matches
# "latest_release.py" or "src/contest_runner.go"; anchoring to a path segment or
# a basename prefix is the difference between excluding tests and silently
# deleting real source from gold.
TEST_DIR_SEGMENTS = {"test", "tests", "testing", "spec", "specs", "__tests__", "e2e"}
DOC_DIR_SEGMENTS = {"doc", "docs", "changes", "changelog", "changelogs", "news", "examples"}

# git emits a path in exactly two forms: C-quoted when it holds a non-ASCII or
# control byte, bare otherwise - and it appends a TAB to the ---/+++ line when
# the path holds a space (measured, git 2.53.0). A pattern that accepts neither
# form drops the file silently, and `\S+` accepts neither.
_PATH_TOKEN = r'"(?:\\.|[^"\\])*"|[^\t\r\n]*?'
_DIFF_RE = re.compile(
    r'^diff --git (?P<a>"(?:\\.|[^"\\])*"|.+?) (?P<b>"(?:\\.|[^"\\])*"|.+)$', re.M
)
_NEWPATH_RE = re.compile(rf"^\+\+\+ (?P<path>{_PATH_TOKEN})\t?\r?$", re.M)
_OLDPATH_RE = re.compile(rf"^--- (?P<path>{_PATH_TOKEN})\t?\r?$", re.M)
_RENAME_TO_RE = re.compile(r"^rename to (?P<path>.+?)\r?$", re.M)
_HUNK_RE = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<start>\d+)(?:,(?P<count>\d+))? @@(?P<hint>.*)$",
    re.M,
)
# A corpus-wide bar: above this share of single-character symbols the extractor
# is corrupt, not the code. Applied PER LANGUAGE - a language is a separate
# extraction path, and a majority language would otherwise dilute a total
# corruption of a minority one below the bar.
_SINGLE_CHAR_LIMIT = 0.02
_C_ESCAPES = {
    "a": "\a", "b": "\b", "f": "\f", "n": "\n",
    "r": "\r", "t": "\t", "v": "\v", "\\": "\\", '"': '"',
}
_DEF_RE = re.compile(
    r"^(?:async\s+)?(?:def|fn|function)\s+(?P<fn>[A-Za-z_]\w*)"
    r"|^class\s+(?P<cls>[A-Za-z_]\w*)"
    r"|^func\s+(?:\([^)]*\)\s*)?(?P<go>[A-Za-z_]\w*)"
    r"|^(?:impl|struct|interface|type)\s+(?P<ty>[A-Za-z_]\w*)"
)


def _segments(path: str) -> list[str]:
    return [seg for seg in path.replace("\\", "/").lower().split("/") if seg]


def _is_test_path(path: str) -> bool:
    segs = _segments(path)
    if any(seg in TEST_DIR_SEGMENTS for seg in segs[:-1]):
        return True
    base = segs[-1] if segs else ""
    return (
        base.startswith("test_")
        or base.startswith("spec_")
        or base.endswith("_test.py")
        or ".test." in base
        or ".spec." in base
        or base.startswith("conftest.")
    )


def _is_doc_path(path: str) -> bool:
    segs = _segments(path)
    return any(seg in DOC_DIR_SEGMENTS for seg in segs[:-1])


def _language_of(path: str) -> str:
    lowered = path.lower()
    suffix = "." + lowered.rsplit(".", 1)[-1] if "." in lowered else ""
    return SUFFIX_LANGUAGE.get(suffix, "")


def _unquote(raw: str) -> str:
    """Decode git's C-style path quoting.

    git escapes a non-ASCII byte as an OCTAL escape (`\\303\\244`), which is not
    JSON: decoding this with `json.loads` raises, and the previous fallback of
    stripping the quotes left the escapes in the path, so the case could never
    join to any path the engine emits.
    """
    if not (len(raw) >= 2 and raw.startswith('"') and raw.endswith('"')):
        return raw
    body = raw[1:-1]
    out = bytearray()
    index = 0
    while index < len(body):
        character = body[index]
        if character != "\\":
            out += character.encode("utf-8")
            index += 1
            continue
        index += 1
        if index >= len(body):
            break
        escape = body[index]
        if escape in "01234567" and len(body) - index >= 3:
            out.append(int(body[index : index + 3], 8) & 0xFF)
            index += 3
            continue
        out += _C_ESCAPES.get(escape, escape).encode("utf-8")
        index += 1
    return out.decode("utf-8", errors="replace")


def _strip_prefix(path: str) -> str:
    return path[2:] if path.startswith(("a/", "b/")) else path


def _block_identity(preamble: str) -> str:
    """Best available name for a block that yields no post-image path."""
    for pattern in (_RENAME_TO_RE, _OLDPATH_RE):
        found = pattern.search(preamble)
        if found:
            path = _unquote(found.group("path"))
            if path != "/dev/null":
                return _strip_prefix(path)
    header = _DIFF_RE.search(preamble)
    return _strip_prefix(_unquote(header.group("b"))) if header else ""


def _file_blocks(
    patch: str, dropped: list[dict[str, str]] | None = None
) -> list[tuple[str, str]]:
    """Split a unified diff into (new_path, body) blocks, in patch order.

    The path is taken from the `+++ b/...` line, not from `diff --git`, because
    that is the authoritative post-image path and it survives renames. A block
    whose post-image is /dev/null is a DELETION, and a pure rename carries no
    `+++` line at all; neither yields a localization target, and both are
    appended to `dropped` rather than vanishing.

    Paths are read from the block PREAMBLE only. A removed line whose content
    begins with `--` renders as `--- ...` inside a hunk body and would otherwise
    be mistaken for a header.
    """
    matches = list(_DIFF_RE.finditer(patch))
    blocks: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(patch)
        body = patch[match.start() : end]
        first_hunk = _HUNK_RE.search(body)
        preamble = body[: first_hunk.start()] if first_hunk else body
        plus = _NEWPATH_RE.search(preamble)
        path = _unquote(plus.group("path")) if plus else "/dev/null"
        if path == "/dev/null":
            if dropped is not None:
                dropped.append(
                    {
                        "path": _block_identity(preamble),
                        "reason": "no_post_image_path",
                    }
                )
            continue
        blocks.append((_strip_prefix(path), body))
    return blocks


def _hunk_bodies(body: str) -> list[tuple[re.Match[str], str]]:
    """Pair each hunk header with its body text, WITHOUT the header's newline.

    Keeping that newline made `splitlines()` yield a leading empty element that
    every line-number walk counted as a body line, shifting every gold range by
    one.
    """
    hunks = list(_HUNK_RE.finditer(body))
    out: list[tuple[re.Match[str], str]] = []
    for index, hunk in enumerate(hunks):
        end = hunks[index + 1].start() if index + 1 < len(hunks) else len(body)
        start = hunk.end() + 1 if body[hunk.end() : hunk.end() + 1] == "\n" else hunk.end()
        out.append((hunk, body[start:end]))
    return out


def _enclosing_symbol(body_symbol: str, header_symbol: str) -> str:
    """The one decision the SYMBOL DERIVATION note governs: body wins."""
    return body_symbol or header_symbol


class HunkGold(NamedTuple):
    ranges: list[tuple[int, int]]
    symbols: list[str]
    header_symbol: str
    defined_on_changed_lines: list[str]


def _changed_ranges_and_symbols(hunk: re.Match[str], text: str) -> HunkGold:
    """PRE-image ranges of CHANGED lines only, plus their enclosing symbols.

    A hunk carries +/-3 unchanged context lines; treating the whole hunk as gold
    inflates every line metric. A removed line is itself a pre-image line; a run
    of added lines has none, so it anchors on the last pre-image line before it.
    A hunk with no pre-image line at all (`@@ -0,0`) yields no range.
    """
    old_start = int(hunk.group("old_start"))
    header_symbol = ""
    found = _DEF_RE.search(hunk.group("hint").strip())
    if found:
        header_symbol = next((g for g in found.groups() if g), "")

    pre_line = old_start
    changed: set[int] = set()
    enclosing: list[str] = []
    defined_on_changed: list[str] = []
    current = ""
    for raw in text.splitlines():
        if raw.startswith("\\"):
            continue  # "\ No newline at end of file"
        content = raw[1:] if raw[:1] in ("+", "-", " ") else raw
        definition = _DEF_RE.search(content.strip())
        name = next((g for g in definition.groups() if g), "") if definition else ""
        if raw.startswith("-"):
            changed.add(pre_line)  # a removed line IS a pre-image line
            pre_line += 1
            continue  # a removed definition never becomes the enclosing scope
        current = name or current
        if raw.startswith("+"):
            if old_start >= 1:
                # inserted AFTER this pre-image line; `-0,0` has none at all
                changed.add(max(old_start, pre_line - 1))
            enclosing.append(_enclosing_symbol(current, header_symbol))
            if name:
                defined_on_changed.append(name)
            continue
        pre_line += 1

    if not enclosing:
        symbols = [header_symbol] if header_symbol else []
    else:
        symbols = [s for s in dict.fromkeys(enclosing) if s]

    ranges: list[tuple[int, int]] = []
    for value in sorted(changed):
        if ranges and value == ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], value)
            continue
        ranges.append((value, value))
    return HunkGold(ranges, symbols, header_symbol, defined_on_changed)


def _skip_reason(path: str) -> str:
    if _is_test_path(path):
        return "test_path"
    if _is_doc_path(path):
        return "doc_path"
    if not _language_of(path):
        return "unsupported_language"
    return ""


class PatchGold(NamedTuple):
    gold: dict[str, Any]
    dispositions: Counter
    skipped_files: list[dict[str, str]]
    symbol_anomalies: list[dict[str, Any]]


def extract_gold_details(patch: str) -> PatchGold:
    """Gold plus the full disposition of every file block the patch carries."""
    gold_files: list[str] = []
    line_ranges: list[dict[str, Any]] = []
    symbols: list[str] = []
    skipped: list[dict[str, str]] = []
    anomalies: list[dict[str, Any]] = []
    dispositions: Counter = Counter()

    blocks = _file_blocks(patch, dropped=skipped)
    dispositions["no_post_image_path"] = len(skipped)
    for path, body in blocks:
        reason = _skip_reason(path)
        if reason:
            dispositions[reason] += 1
            skipped.append({"path": path, "reason": reason})
            continue
        dispositions["kept"] += 1
        if path not in gold_files:
            gold_files.append(path)
        for hunk, text in _hunk_bodies(body):
            found = _changed_ranges_and_symbols(hunk, text)
            for lo, hi in found.ranges:
                line_ranges.append({"file": path, "start": lo, "end": hi})
            for name in found.symbols:
                if name not in symbols:
                    symbols.append(name)
            missing = [
                name
                for name in found.defined_on_changed_lines
                if name not in found.symbols
            ]
            if missing:
                anomalies.append(
                    {
                        "file": path,
                        "header_symbol": found.header_symbol,
                        "reported_symbols": list(found.symbols),
                        "unreported_definitions": sorted(set(missing)),
                    }
                )
    gold = {
        "gold_files": gold_files,
        "gold_line_ranges": line_ranges,
        "gold_line_coordinates": "pre_image",
        "gold_symbols": symbols,
    }
    return PatchGold(gold, dispositions, skipped, anomalies)


def extract_gold(patch: str) -> dict[str, Any]:
    return extract_gold_details(patch).gold


def _split_of(instance_id: str) -> str:
    """Deterministic 80/20 primary/holdout split.

    evaluate_winner needs a `random` PRIMARY comparison set and refuses a verdict
    without one; a corpus that is 100% one split can never produce a result.
    """
    digest = hashlib.sha256(instance_id.encode("utf-8")).hexdigest()
    return "held" if int(digest[:8], 16) % 5 == 0 else "random"


def build(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    repos: dict[str, Any] = {}
    dropped: list[dict[str, str]] = []
    skipped_files: list[dict[str, str]] = []
    dispositions: Counter = Counter(
        {
            key: 0
            for key in (
                "kept",
                "test_path",
                "doc_path",
                "unsupported_language",
                "no_post_image_path",
            )
        }
    )
    anomalies: list[dict[str, Any]] = []
    for row in rows:
        details = extract_gold_details(row["patch"])
        gold = details.gold
        # Census BEFORE any case-level drop: a case that never ships still
        # consumed file blocks, and hiding them overstates corpus coverage.
        dispositions.update(details.dispositions)
        skipped_files.extend(
            {"id": row["instance_id"], **entry} for entry in details.skipped_files
        )
        anomalies.extend(
            {"id": row["instance_id"], **entry} for entry in details.symbol_anomalies
        )
        if not gold["gold_files"]:
            dropped.append({"id": row["instance_id"], "reason": "no_code_file_in_fix_patch"})
            continue
        issue = str(row.get("problem_statement") or "").strip()
        if not issue:
            dropped.append({"id": row["instance_id"], "reason": "empty_problem_statement"})
            continue
        languages = Counter(_language_of(p) for p in gold["gold_files"])
        language = languages.most_common(1)[0][0]
        split = _split_of(row["instance_id"])
        # One repo entry PER CASE: instances of the same repo sit at different
        # base commits, and the repos manifest carries a single commit per key.
        repo_key = row["instance_id"]
        repos[repo_key] = {
            "url": f"https://github.com/{row['repo']}",
            "commit": row["base_commit"],
        }
        cases.append(
            {
                "id": f"{split}_{row['instance_id']}",
                "repo": repo_key,
                "issue_text": issue,
                "base_commit": row["base_commit"],
                "revision_identity": row["base_commit"],
                "language": language,
                "split": split,
                "upstream_repo": row["repo"],
                **gold,
                "fix_commit": row.get("commit_url") or None,
                "patch_sha256": hashlib.sha256(row["patch"].encode("utf-8")).hexdigest(),
            }
        )

    if anomalies:
        raise SystemExit(
            f"ABORT: {len(anomalies)} hunks report the hunk header's symbol while "
            "their own changed lines define another - the PRECEDING SIBLING "
            f"failure the module docstring describes: {anomalies[:5]}"
        )
    symbols_by_language: dict[str, list[str]] = {}
    for case in cases:
        symbols_by_language.setdefault(case["language"], []).extend(case["gold_symbols"])
    corrupt: dict[str, Any] = {}
    for language, values in sorted(symbols_by_language.items()):
        single = [s for s in values if len(s) <= 1]
        if values and len(single) / len(values) > _SINGLE_CHAR_LIMIT:
            corrupt[language] = {
                "symbols": len(values),
                "single_character": len(single),
                "examples": sorted(set(single))[:10],
            }
    if corrupt:
        raise SystemExit(
            f"ABORT: single-character symbols exceed {_SINGLE_CHAR_LIMIT:.0%} of a "
            f"language's symbols, so extraction is corrupt for it: {corrupt}"
        )
    all_symbols = [s for c in cases for s in c["gold_symbols"]]
    counts = Counter(len(c["gold_files"]) for c in cases)
    report = {
        "input_rows": len(rows),
        "cases": len(cases),
        "dropped": dropped,
        "file_dispositions": dict(sorted(dispositions.items())),
        "skipped_files": skipped_files,
        "gold_files_per_case": dict(sorted(counts.items())),
        "multi_file_cases": sum(n for size, n in counts.items() if size >= 2),
        "languages": dict(Counter(c["language"] for c in cases)),
        "splits": dict(Counter(c["split"] for c in cases)),
        "cases_with_line_ranges": sum(1 for c in cases if c["gold_line_ranges"]),
        "cases_with_symbols": sum(1 for c in cases if c["gold_symbols"]),
        "gold_line_coordinates": "pre_image",
        "total_line_ranges": sum(len(c["gold_line_ranges"]) for c in cases),
        "total_symbols": len(all_symbols),
        "symbols_by_language": {
            language: {
                "symbols": len(values),
                "single_character": len([s for s in values if len(s) <= 1]),
            }
            for language, values in sorted(symbols_by_language.items())
        },
        "repos": len(repos),
    }
    return cases, repos, report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--repos-out", required=True)
    ap.add_argument("--report", default="")
    args = ap.parse_args()

    rows = [
        json.loads(line)
        for line in Path(args.input).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    cases, repos, report = build(rows)
    Path(args.out).write_text(
        json.dumps(cases, indent=2, sort_keys=True, ensure_ascii=True), encoding="utf-8"
    )
    Path(args.repos_out).write_text(
        json.dumps(repos, indent=2, sort_keys=True, ensure_ascii=True), encoding="utf-8"
    )
    text = json.dumps(report, indent=2, sort_keys=True)
    if args.report:
        Path(args.report).write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
