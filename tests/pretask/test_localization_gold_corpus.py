"""Gold-derivation tests for scripts/build_localization_gold_corpus.py.

Every expectation here is HAND-VERIFIED against a real fix patch taken from
``benchmarks/data/swebench_live_lite.jsonl`` by instance_id.  The per-case
comments carry the line arithmetic (hunk header vs. body) that produced the
expected value, so a future reader can re-check the number without re-running
the builder.

Two shapes the builder must survive do not occur anywhere in that dataset --
``test_dataset_carries_no_rename_and_no_quoted_path`` asserts that fact rather
than claiming it in prose.  Their fixtures are therefore the verbatim stdout of
a real ``git diff`` (git 2.53.0.windows.1) over a scratch repository, not
hand-written diff text: git's own quoting/tab conventions are the thing under
test and could not be reproduced from memory.

Coordinate systems are the subject of most of this file.  The engine localizes
the repository at ``base_commit`` -- the PRE-fix tree -- so gold line numbers
must be pre-image line numbers.  A hunk's post-image numbering (the ``+`` side)
addresses a file that does not exist until after the fix lands.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import scripts.build_localization_gold_corpus as builder
from scripts.build_localization_gold_corpus import (
    _file_blocks,
    _hunk_bodies,
    _unquote,
    build,
    extract_gold,
    extract_gold_details,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DATASET = REPO_ROOT / "benchmarks" / "data" / "swebench_live_lite.jsonl"

_ROW_CACHE: dict[str, dict] = {}


def _rows(*instance_ids: str) -> dict[str, dict]:
    """Load real dataset rows by instance_id (one pass, cached)."""
    wanted = set(instance_ids)
    missing = wanted - set(_ROW_CACHE)
    if missing:
        with DATASET.open(encoding="utf-8") as handle:
            for line in handle:
                if not any(name in line for name in missing):
                    continue
                row = json.loads(line)
                if row["instance_id"] in missing:
                    _ROW_CACHE[row["instance_id"]] = row
    absent = sorted(wanted - set(_ROW_CACHE))
    assert not absent, f"dataset does not carry fixture instances: {absent}"
    return {name: _ROW_CACHE[name] for name in wanted}


def _all_rows() -> list[dict]:
    with DATASET.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _patch(instance_id: str) -> str:
    patch = _rows(instance_id)[instance_id]["patch"]
    # Non-vacuity: a fixture that silently became empty must not pass.
    assert patch.startswith("diff --git "), instance_id
    return patch


def _ranges(gold: dict, path: str) -> list[tuple[int, int]]:
    return [
        (int(item["start"]), int(item["end"]))
        for item in gold["gold_line_ranges"]
        if item["file"] == path
    ]


# --------------------------------------------------------------------------
# Real git output for the two shapes SWE-bench-Live Lite does not contain.
# Produced by: git init; commit two modules; `git mv` one and edit both; then
# `git diff --cached -M`.  Reproduced verbatim, including the TAB that git
# appends to the ---/+++ path when the path contains a space.
# --------------------------------------------------------------------------
RENAME_WITH_EDIT_PATCH = (
    "diff --git a/pkg/old_name.py b/pkg/new_name.py\n"
    "similarity index 79%\n"
    "rename from pkg/old_name.py\n"
    "rename to pkg/new_name.py\n"
    "index 8029c06..4d3252d 100644\n"
    "--- a/pkg/old_name.py\n"
    "+++ b/pkg/new_name.py\n"
    "@@ -9,5 +9,5 @@ def helper(value):\n"
    " \n"
    " def target(value):\n"
    "     if value < 0:\n"
    '-        raise ValueError("negative")\n'
    '+        raise ValueError("negative value")\n'
    "     return value\n"
)
RENAME_ONLY_PATCH = (
    "diff --git a/pkg/old_name.py b/pkg/moved_name.py\n"
    "similarity index 100%\n"
    "rename from pkg/old_name.py\n"
    "rename to pkg/moved_name.py\n"
)
# space + non-ASCII: git C-quotes the path AND appends a tab.
QUOTED_SPACE_UNICODE_PATCH = (
    'diff --git "a/pkg/naive f\\303\\244llt.py" "b/pkg/naive f\\303\\244llt.py"\n'
    "index 5653372..609dc5f 100644\n"
    '--- "a/pkg/naive f\\303\\244llt.py"\t\n'
    '+++ "b/pkg/naive f\\303\\244llt.py"\t\n'
    "@@ -9,5 +9,5 @@ def sibling(value):\n"
    " \n"
    " def edited(value):\n"
    "     if value < 0:\n"
    '-        raise ValueError("negative")\n'
    '+        raise ValueError("negative value")\n'
    "     return value\n"
)
# non-ASCII only: git C-quotes, no trailing tab.
QUOTED_UNICODE_PATCH = (
    'diff --git "a/pkg/naive_f\\303\\244llt.py" "b/pkg/naive_f\\303\\244llt.py"\n'
    "index a2b6af3..eda84f5 100644\n"
    '--- "a/pkg/naive_f\\303\\244llt.py"\n'
    '+++ "b/pkg/naive_f\\303\\244llt.py"\n'
    "@@ -9,5 +9,5 @@ def sibling(value):\n"
    " \n"
    " def edited(value):\n"
    "     if value < 0:\n"
    '-        raise ValueError("negative")\n'
    '+        raise ValueError("negative value")\n'
    "     return value\n"
)
# space only: git does NOT quote, it appends a tab instead.
SPACE_PATH_PATCH = (
    "diff --git a/pkg/with space.py b/pkg/with space.py\n"
    "index 4fc77c3..a2e1dcf 100644\n"
    "--- a/pkg/with space.py\t\n"
    "+++ b/pkg/with space.py\t\n"
    "@@ -9,5 +9,5 @@ def sibling(value):\n"
    " \n"
    " def edited(value):\n"
    "     if value < 0:\n"
    '-        raise ValueError("negative")\n'
    '+        raise ValueError("negative value")\n'
    "     return value\n"
)


# --------------------------------------------------------------------------
# DEFECT 1 -- hand-verified gold for each patch shape.
# --------------------------------------------------------------------------
def test_single_file_patch_gold_is_hand_verified():
    """amoffat__sh-744: one file, one hunk, one replaced line.

    @@ -889,7 +889,10 @@ def __next__(self):
      889  def __await__(self):            context
      890      async def wait_for_completion():
      891          await self.aio_output_complete.wait()
      892          return str(self)          REMOVED  <- the only edited line
          (+4 lines land here)
      893 / 894 / 895                        context (7 pre-image lines = -889,7)
    """
    patch = _patch("amoffat__sh-744")
    blocks = _file_blocks(patch)
    assert len(blocks) == 1, blocks
    hunks = _hunk_bodies(blocks[0][1])
    assert len(hunks) == 1
    # The hunk header names the PRECEDING sibling, not the edited function.
    assert hunks[0][0].group("hint").strip() == "def __next__(self):"

    gold = extract_gold(patch)

    assert gold["gold_files"] == ["sh.py"]
    assert gold["gold_symbols"] == ["wait_for_completion"]
    assert gold["gold_line_ranges"] == [{"file": "sh.py", "start": 892, "end": 892}]
    assert gold["gold_line_coordinates"] == "pre_image"


def test_three_file_patch_gold_is_hand_verified():
    """scrapy-plugins__scrapy-splash-324: three source files, four hunks.

    request.py @@ -31,7 @@   pre 31,32,33 context; 34 `url,` REMOVED     -> 34
    request.py @@ -48,6 @@   pre 48,49,50 context; +2 lines after 50        -> 50
    utils.py   @@ -5,12 @@   pre 5,6,7 context; 8..13 REMOVED              -> 8-13
    setup.py   @@ -30,5 @@   pre 30,31,32 context; 33 install_requires REMOVED -> 33
    """
    patch = _patch("scrapy-plugins__scrapy-splash-324")
    blocks = _file_blocks(patch)
    assert [path for path, _ in blocks] == [
        "scrapy_splash/request.py",
        "scrapy_splash/utils.py",
        "setup.py",
    ]

    gold = extract_gold(patch)

    assert gold["gold_files"] == [
        "scrapy_splash/request.py",
        "scrapy_splash/utils.py",
        "setup.py",
    ]
    assert _ranges(gold, "scrapy_splash/request.py") == [(34, 34), (50, 50)]
    assert _ranges(gold, "scrapy_splash/utils.py") == [(8, 13)]
    assert _ranges(gold, "setup.py") == [(33, 33)]
    # utils.py/setup.py edits sit at module level: no enclosing definition, and
    # the hunk headers carry no symbol either, so nothing is invented.
    assert gold["gold_symbols"] == ["__init__"]


def test_pure_deletion_hunk_anchors_on_the_removed_pre_image_line():
    """kozea__weasyprint-2303: hunk 2 removes a line and adds nothing.

    @@ -105,7 +107,6 @@ def set_color(...)
      105 else:                     context
      106 LOGGER.warn(...)          context
      107 self.set_color_rgb(...)   context
      108 self.set_alpha(...)       REMOVED  <- the edit
    A deletion-only hunk has no post-image line at all; the pre-image line it
    removes is the only honest anchor.
    """
    patch = _patch("kozea__weasyprint-2303")
    blocks = _file_blocks(patch)
    assert len(blocks) == 1
    hunks = _hunk_bodies(blocks[0][1])
    assert len(hunks) == 2
    deletion_body = hunks[1][1]
    # Non-vacuity: hunk 2 really is deletion-only.
    assert any(line.startswith("-") for line in deletion_body.splitlines())
    assert not any(line.startswith("+") for line in deletion_body.splitlines())

    gold = extract_gold(patch)

    assert gold["gold_files"] == ["weasyprint/pdf/stream.py"]
    assert _ranges(gold, "weasyprint/pdf/stream.py") == [(83, 83), (108, 108)]
    # Deletion-only hunk keeps the enclosing-scope fallback from its header.
    assert gold["gold_symbols"] == ["set_color"]


def test_new_file_has_no_pre_image_lines_to_localize():
    """joke2k__faker-2162: `--- /dev/null`, @@ -0,0 +1,20 @@.

    The file does not exist in the tree the engine indexes, so NO line of that
    tree is gold.  The file and its symbols are still gold.
    """
    patch = _patch("joke2k__faker-2162")
    assert "new file mode" in patch and "--- /dev/null" in patch
    blocks = _file_blocks(patch)
    assert len(blocks) == 1
    hunks = _hunk_bodies(blocks[0][1])
    assert len(hunks) == 1
    assert hunks[0][0].group(0).startswith("@@ -0,0 +1,20 @@")

    gold = extract_gold(patch)

    assert gold["gold_files"] == ["faker/providers/doi/__init__.py"]
    assert gold["gold_symbols"] == ["Provider", "doi"]
    assert gold["gold_line_ranges"] == []


def test_rename_with_edit_keeps_post_image_path_and_names_edited_symbol():
    """Real `git diff -M` output; the dataset has no rename (see below).

    @@ -9,5 +9,5 @@ def helper(value):     header names the preceding sibling
       9  blank                 context
      10  def target(value):    context   <- the enclosing definition
      11  if value < 0:         context
      12  raise ValueError(...) REMOVED
    """
    blocks = _file_blocks(RENAME_WITH_EDIT_PATCH)
    assert [path for path, _ in blocks] == ["pkg/new_name.py"]

    gold = extract_gold(RENAME_WITH_EDIT_PATCH)

    assert gold["gold_files"] == ["pkg/new_name.py"]
    assert _ranges(gold, "pkg/new_name.py") == [(12, 12)]
    assert gold["gold_symbols"] == ["target"]


def test_git_quoted_path_is_decoded_to_the_real_repository_path():
    """git C-quotes non-ASCII paths (\\303\\244 == UTF-8 'a-umlaut').

    An undecoded path can never match a path the engine emits, so a corrupted
    decode is a silently unscorable case.
    """
    assert _unquote('"b/pkg/naive f\\303\\244llt.py"') == "b/pkg/naive fällt.py"
    assert _unquote('"b/tab\\ttab.py"') == "b/tab\ttab.py"
    assert _unquote("b/plain.py") == "b/plain.py"

    for patch, expected in (
        (QUOTED_SPACE_UNICODE_PATCH, "pkg/naive fällt.py"),
        (QUOTED_UNICODE_PATCH, "pkg/naive_fällt.py"),
        (SPACE_PATH_PATCH, "pkg/with space.py"),
    ):
        gold = extract_gold(patch)
        assert gold["gold_files"] == [expected], patch.splitlines()[0]
        assert _ranges(gold, expected) == [(12, 12)]
        assert gold["gold_symbols"] == ["edited"]


def test_a_space_in_one_path_cannot_move_hunks_onto_another_file():
    """Block splitting must not merge two files into one gold entry."""
    combined = RENAME_WITH_EDIT_PATCH + SPACE_PATH_PATCH
    blocks = _file_blocks(combined)
    assert [path for path, _ in blocks] == ["pkg/new_name.py", "pkg/with space.py"]

    gold = extract_gold(combined)

    assert gold["gold_files"] == ["pkg/new_name.py", "pkg/with space.py"]
    assert _ranges(gold, "pkg/new_name.py") == [(12, 12)]
    assert _ranges(gold, "pkg/with space.py") == [(12, 12)]


def test_dataset_carries_no_rename_and_no_quoted_path():
    """Why two fixtures above are git-generated rather than dataset rows."""
    rows = _all_rows()
    assert len(rows) == 300
    quoted = [r["instance_id"] for r in rows if re.search(r'^diff --git "', r["patch"], re.M)]
    renamed = [r["instance_id"] for r in rows if re.search(r"^rename from ", r["patch"], re.M)]
    assert quoted == []
    assert renamed == []


# --------------------------------------------------------------------------
# DEFECT 2 -- gold line numbers must address the PRE-fix tree.
# --------------------------------------------------------------------------
def _pre_image_touched_lines(body: str) -> tuple[set[int], set[int]]:
    """Re-derive, straight from the patch text, which PRE-image lines a hunk touches.

    Returns (touched, spanned).  ``touched`` = removed lines plus the pre-image
    lines immediately either side of an insertion point.  Deliberately a
    superset of any single anchoring convention: the assertion below is about
    the coordinate system and locality, not about which side an insertion picks.
    """
    touched: set[int] = set()
    spanned: set[int] = set()
    for hunk, text in _hunk_bodies(body):
        old_start = int(hunk.group("old_start"))
        old_count = int(hunk.group("old_count") if hunk.group("old_count") is not None else 1)
        spanned |= set(range(old_start, old_start + old_count))
        line = old_start
        for raw in text.splitlines():
            if raw.startswith("\\"):
                continue
            if raw.startswith("-"):
                touched.add(line)
                line += 1
            elif raw.startswith("+"):
                touched.add(max(old_start, line - 1))
                touched.add(line)
            else:
                line += 1
    return touched, spanned


@pytest.mark.parametrize(
    "instance_id",
    [
        "amoffat__sh-744",
        "scrapy-plugins__scrapy-splash-324",
        "kozea__weasyprint-2303",
        "geopandas__geopandas-3471",
    ],
)
def test_gold_lines_are_lines_the_fix_actually_touches(instance_id):
    patch = _patch(instance_id)
    gold = extract_gold(patch)
    assert gold["gold_line_ranges"], instance_id  # non-vacuity

    by_path = {path: body for path, body in _file_blocks(patch)}
    checked = 0
    for item in gold["gold_line_ranges"]:
        touched, spanned = _pre_image_touched_lines(by_path[item["file"]])
        for line in range(int(item["start"]), int(item["end"]) + 1):
            checked += 1
            assert line in spanned, (
                f"{instance_id} {item['file']}:{line} is outside every pre-image "
                f"hunk span {sorted(spanned)[:4]}... -- it is a post-image number"
            )
            assert line in touched, (
                f"{instance_id} {item['file']}:{line} is an UNCHANGED pre-image "
                "line: gold points at a line the fix never edits"
            )
    assert checked


def test_whole_corpus_gold_lines_exist_in_the_pre_fix_tree():
    """The same invariant over all 300 rows, not four hand-picked ones."""
    rows = _all_rows()
    cases, _repos, _report = build(rows)
    assert len(cases) > 250  # non-vacuity

    patch_by_id = {row["instance_id"]: row["patch"] for row in rows}
    offenders: list[str] = []
    checked = 0
    for case in cases:
        patch = patch_by_id[case["id"].split("_", 1)[1]]
        by_path = {path: body for path, body in _file_blocks(patch)}
        for item in case["gold_line_ranges"]:
            touched, spanned = _pre_image_touched_lines(by_path[item["file"]])
            for line in range(int(item["start"]), int(item["end"]) + 1):
                checked += 1
                if line not in spanned or line not in touched:
                    offenders.append(f"{case['id']} {item['file']}:{line}")
    assert checked > 2000
    assert offenders[:20] == []


def test_gold_line_coordinate_system_is_declared_on_every_case():
    rows = _all_rows()
    cases, _repos, _report = build(rows)
    assert cases
    assert {case["gold_line_coordinates"] for case in cases} == {"pre_image"}


# --------------------------------------------------------------------------
# DEFECT 3 -- the symbol guards.
# --------------------------------------------------------------------------
def _row(instance_id: str, patch: str, *, repo: str = "acme/widget") -> dict:
    return {
        "instance_id": instance_id,
        "patch": patch,
        "problem_statement": "Something is wrong and should be fixed.",
        "repo": repo,
        "base_commit": "0" * 40,
    }


_TS_CORRUPT_PATCH = (
    "diff --git a/src/a.ts b/src/a.ts\n"
    "--- a/src/a.ts\n"
    "+++ b/src/a.ts\n"
    "@@ -1,3 +1,4 @@\n"
    " const x = 1;\n"
    "+function a(v) {\n"
    "+  if (!v) { throw new Error('v'); }\n"
    "+  return v;\n"
    "+}\n"
    " export default x;\n"
)


def test_single_character_symbol_guard_is_per_language():
    """One language fully corrupted must abort even when diluted by Python."""
    python_rows = [
        _row(f"acme__widget-{index}", _patch("amoffat__sh-744"))
        for index in range(50)
    ]
    corrupt = _row("acme__tsapp-1", _TS_CORRUPT_PATCH)

    clean_cases, _repos, _report = build(python_rows)
    clean_symbols = [s for case in clean_cases for s in case["gold_symbols"]]
    assert clean_symbols  # non-vacuity: the python arm really produced symbols

    # Non-vacuity: the typescript arm really is corrupt (and really is TS).
    corrupt_gold = extract_gold(_TS_CORRUPT_PATCH)
    assert corrupt_gold["gold_files"] == ["src/a.ts"]
    assert corrupt_gold["gold_symbols"] == ["a"]

    # Non-vacuity for the guard itself: globally the corruption is 1 symbol in
    # 51, i.e. under the 2% bar -- exactly the dilution the old guard missed.
    total = len(clean_symbols) + 1
    assert 1 / total <= 0.02

    with pytest.raises(SystemExit) as excinfo:
        build([*python_rows, corrupt])
    assert "typescript" in str(excinfo.value)


def test_symbols_defined_on_added_lines_are_always_reported():
    """The measured failure mode: naming the hunk header's preceding sibling."""
    patch = _patch("sissbruecker__linkding-989")
    hunks = _hunk_bodies(_file_blocks(patch)[0][1])
    assert hunks[0][0].group("hint").strip() == "def __str__(self):"
    assert any(
        line.startswith("+def bookmark_deleted") for line in hunks[0][1].splitlines()
    )

    result = extract_gold_details(patch)

    assert "bookmark_deleted" in result.gold["gold_symbols"]
    assert result.symbol_anomalies == []


def test_preceding_sibling_naming_aborts_the_build(monkeypatch):
    """Fault injection: make the header win, as the module docstring warns."""
    row = _row("acme__widget-1", _patch("sissbruecker__linkding-989"))
    assert build([row])[0][0]["gold_symbols"]  # non-vacuity: builds clean today

    monkeypatch.setattr(
        builder,
        "_enclosing_symbol",
        lambda body_symbol, header_symbol: header_symbol or body_symbol,
    )

    with pytest.raises(SystemExit) as excinfo:
        build([row])
    assert "bookmark_deleted" in str(excinfo.value)


# --------------------------------------------------------------------------
# DEFECT 4 -- the report must count every dropped file.
# --------------------------------------------------------------------------
def test_report_counts_every_skipped_file_with_its_reason():
    rows = _rows(
        "geopandas__geopandas-3471",  # CHANGELOG.md      -> unsupported suffix
        "pylint-dev__pylint-10240",  # doc/whatsnew/...  -> doc path
        "fonttools__fonttools-3726",  # Tests/feaLib/...  -> test path
        "kozea__weasyprint-2303",  # nothing skipped
    )
    ordered = [
        rows["geopandas__geopandas-3471"],
        rows["pylint-dev__pylint-10240"],
        rows["fonttools__fonttools-3726"],
        rows["kozea__weasyprint-2303"],
    ]
    # Non-vacuity: these rows really do carry the paths the reasons refer to.
    blocks = {
        row["instance_id"]: [path for path, _ in _file_blocks(row["patch"])]
        for row in ordered
    }
    assert "CHANGELOG.md" in blocks["geopandas__geopandas-3471"]
    assert any(p.startswith("doc/") for p in blocks["pylint-dev__pylint-10240"])
    assert any(p.startswith("Tests/") for p in blocks["fonttools__fonttools-3726"])

    _cases, _repos, report = build(ordered)

    skipped = report["skipped_files"]
    by_reason = {}
    for entry in skipped:
        by_reason.setdefault(entry["reason"], []).append(entry["path"])
    assert "CHANGELOG.md" in by_reason["unsupported_language"]
    assert any(p.startswith("doc/") for p in by_reason["doc_path"])
    assert any(p.startswith("Tests/") for p in by_reason["test_path"])
    assert all("id" in entry for entry in skipped)

    counts = report["file_dispositions"]
    assert counts["kept"] + sum(
        value for key, value in counts.items() if key != "kept"
    ) == sum(len(_file_blocks(row["patch"])) for row in ordered) + counts[
        "no_post_image_path"
    ]
    assert counts["test_path"] == 2
    assert counts["doc_path"] >= 1
    assert counts["unsupported_language"] >= 1


def test_report_accounts_for_blocks_with_no_post_image_path():
    """A rename-only or deleted file yields no target; it must still be counted."""
    row = _row("acme__widget-1", RENAME_WITH_EDIT_PATCH + RENAME_ONLY_PATCH)
    assert _file_blocks(row["patch"]) and len(_file_blocks(row["patch"])) == 1

    _cases, _repos, report = build([row])

    assert report["file_dispositions"]["no_post_image_path"] == 1
    assert [
        entry["path"] for entry in report["skipped_files"] if entry["reason"] == "no_post_image_path"
    ] == ["pkg/moved_name.py"]


# --------------------------------------------------------------------------
# DEFECT 5 -- `_INPUT_KEYS` must drive the gold strip, not merely describe it.
# Lives here because the shard preparer is the direct consumer of this corpus
# and the existing oss-compare test module is outside this change's scope.
# --------------------------------------------------------------------------
def test_prepared_shard_rows_are_enforced_against_input_keys(monkeypatch):
    import scripts.localization_vnext_oss_compare as compare

    cases = [
        {
            "id": "random_py_a",
            "language": "python",
            "repo": "repo-a",
            "issue_text": "Behavior A should change.",
            "split": "random",
            "gold_files": ["pkg/a.py"],
            "gold_symbols": ["run"],
            "gold_line_ranges": [{"file": "pkg/a.py", "start": 2, "end": 4}],
            "gold_line_coordinates": "pre_image",
            "patch_sha256": "f" * 64,
        }
    ]
    repos = {"repo-a": {"commit": "a" * 40, "url": "https://example.invalid/a"}}

    prepared = compare.prepare_shard(
        cases, repos, language="python", lane_index=0, lane_count=1
    )
    assert len(prepared) == 1  # non-vacuity: the loop body ran
    assert set(prepared[0]) == set(compare._INPUT_KEYS)

    # If the constant were dead, neither direction below would raise.
    monkeypatch.setattr(compare, "_INPUT_KEYS", compare._INPUT_KEYS + ("gold_files",))
    with pytest.raises(ValueError):
        compare.prepare_shard(
            cases, repos, language="python", lane_index=0, lane_count=1
        )

    monkeypatch.setattr(compare, "_INPUT_KEYS", ("id", "issue_text"))
    with pytest.raises(ValueError):
        compare.prepare_shard(
            cases, repos, language="python", lane_index=0, lane_count=1
        )


def test_full_corpus_report_reconciles_every_file_block():
    rows = _all_rows()
    _cases, _repos, report = build(rows)
    counts = report["file_dispositions"]
    assert counts["kept"] == 754  # measured on this dataset
    assert counts["unsupported_language"] == 164
    assert counts["doc_path"] == 89
    assert counts["test_path"] == 2
    assert counts["no_post_image_path"] == 6
    headers = sum(
        len(re.findall(r"^diff --git ", row["patch"], re.M)) for row in rows
    )
    assert sum(counts.values()) == headers
    assert len(report["skipped_files"]) == headers - counts["kept"]
