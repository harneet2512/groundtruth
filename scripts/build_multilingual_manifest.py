#!/usr/bin/env python3
"""Build the anti-overfit iteration manifest: artifact_multilingual/repo_manifest.json.

PURPOSE
-------
GroundTruth iterates/validates on SWE-bench Multilingual (go/rust/ts/js) + a Python
arm from SWE-bench Verified, while the DeepSWE 113-task set stays HELD OUT. This script
is the PICKER -- a validation-surface tool. The selection rule lives HERE, never in
src/groundtruth/ (anti-benchmaxx: CLAUDE.md ".claude/CLAUDE.md" pillar 1, "no
benchmark-shape logic, task IDs, or gold labels" in product logic).

WHAT IT EMITS
-------------
artifact_multilingual/repo_manifest.json  -- SAME schema the DeepSWE workflows parse.
artifact_multilingual/SPLIT.md            -- documents the 15 picks + the NAV rationale.

The manifest carries ONLY routing metadata: instance_id / language / docker_image /
repo_url / commit_hash (+ schema-parity fields). NO gold patch, NO FAIL_TO_PASS, NO
test content ever lands in the manifest -- eval data stays in the dataset, fetched at
eval time. The three fields the GHA workflows FAIL-CLOSED on are instance_id, language,
docker_image (verified against .github/workflows/deepswe_{proof_sweep,full}.yml,
cache_deepswe_images.yml as of 2026-06-17).

THE NAV RULE (encoded here, NOT in GT)
--------------------------------------
Keep a task only if ALL hold:
  1. len(gold_files(patch)) >= 2          -- multi-file fix (excludes single-file adds)
  2. it has a non-empty FAIL_TO_PASS       -- a real, gradeable test exists
  3. issue->file is NON-OBVIOUS: no gold file's basename (sans ext) is a substring of
     the issue's first line. If the issue names the file, localization is trivial and
     the task can't witness GT's value.
This selects tasks where finding the right file is the hard part -- exactly where a
context-delivery product must earn its keep.

LANGUAGE DERIVATION
-------------------
SWE-bench_Multilingual has NO 'language' column (probed 2026-06-17: it is None for every
row). Language is derived from the GOLD PATCH file extensions -- the majority extension
among touched files. This is ground truth for what the task actually edits.

STRATIFY + SPLIT
----------------
3 tasks per language for {go, rust, typescript, javascript} (from Multilingual) + 3
python (from Verified) = 15. Within each language, tasks are ordered by sha256(instance_id)
(deterministic, content-addressed -- never hand-picked) and the first 3 are taken. The 3
are assigned to splits TRAIN / VAL / TEST in that sha-order, so every split holds exactly
one task per language (5 langs x 1 = 5 per split). Re-running on the same dataset snapshot
is byte-identical.

USAGE
-----
    python scripts/build_multilingual_manifest.py
Requires `datasets` + HuggingFace network access. If HF is offline the script writes a
schema-correct EMPTY stub manifest + a SPLIT.md telling you to run it on a runner with HF
access -- it NEVER fabricates instance ids.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from collections import Counter, defaultdict

# ---------------------------------------------------------------------------
# Config -- all selection knobs live here (the picker), never in the product.
# ---------------------------------------------------------------------------
ML_DATASET = "SWE-bench/SWE-bench_Multilingual"
PY_DATASET = "princeton-nlp/SWE-bench_Verified"
SPLIT_NAME = "test"

# Multilingual supplies these four; Verified supplies python.
ML_LANGS = ["go", "rust", "typescript", "javascript"]
PY_LANG = "python"
PER_LANG = 3
SPLITS = ["TRAIN", "VAL", "TEST"]  # PER_LANG buckets, sha-ordered

MIN_GOLD_FILES = 2

# Majority-extension -> language. Order within a file is first-match; the per-task
# winner is the most common language across the task's gold files.
EXT_LANG = {
    ".go": "go",
    ".rs": "rust",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".py": "python",
}

# The standard SWE-bench instance image ref, byte-identical to the harness construction
# in swebench/harness/test_spec/test_spec.py::instance_image_key (verified swebench 4.1.0,
# lines 107-110):
#     key = f"sweb.eval.{arch}.{instance_id.lower()}:{tag}"
#     if is_remote_image: key = f"{namespace}/{key}".replace("__", "_1776_")
# For the PUBLIC images namespace=`swebench`, arch=`x86_64`, tag=`latest`. The `_1776_`
# substitution is applied to the WHOLE namespaced string (only the instance_id carries
# `__`, so in practice it rewrites the id's separator). This is the PROBE pattern the
# eval harness resolves. LOCAL-BUILD FALLBACK: if the public image is absent,
# run_evaluation builds it from the dataset's environment setup (base_commit + test_patch)
# -- no manifest change needed; the harness owns that.
_SWEBENCH_NAMESPACE = "swebench"
_SWEBENCH_ARCH = "x86_64"
_SWEBENCH_TAG = "latest"


def swebench_image(instance_id: str) -> str:
    key = f"sweb.eval.{_SWEBENCH_ARCH}.{instance_id.lower()}:{_SWEBENCH_TAG}"
    return f"{_SWEBENCH_NAMESPACE}/{key}".replace("__", "_1776_")


# ---------------------------------------------------------------------------
# NAV primitives
# ---------------------------------------------------------------------------
_DIFF_NEWFILE_RE = re.compile(r"^\+\+\+ b/(.+)$", re.M)


def gold_files(patch: str) -> list[str]:
    """Files touched by the gold patch (the '+++ b/<path>' targets, excluding deletes)."""
    out: list[str] = []
    for m in _DIFF_NEWFILE_RE.finditer(patch or ""):
        f = m.group(1).strip()
        if f and f != "/dev/null":
            out.append(f)
    return out


def derive_language(files: list[str]) -> str | None:
    """Majority source-extension across gold files -> language. None if no known ext."""
    c: Counter[str] = Counter()
    for f in files:
        for ext, lang in EXT_LANG.items():
            if f.endswith(ext):
                c[lang] += 1
                break
    if not c:
        return None
    # Deterministic tie-break: highest count, then lexical lang name.
    return sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def has_fail_to_pass(row: dict) -> bool:
    """F2P is a list in Multilingual but a JSON-encoded string in Verified."""
    f2p = row.get("FAIL_TO_PASS")
    if f2p is None:
        return False
    if isinstance(f2p, str):
        s = f2p.strip()
        if not s or s in ("[]", "null"):
            return False
        try:
            return len(json.loads(s)) > 0
        except (json.JSONDecodeError, TypeError):
            # Non-JSON but non-empty string still indicates a test name.
            return True
    try:
        return len(f2p) > 0
    except TypeError:
        return bool(f2p)


def issue_to_file_obvious(first_line: str, files: list[str]) -> bool:
    """True if any gold file's basename (sans extension) appears in the issue's 1st line."""
    fl = (first_line or "").lower()
    if not fl:
        return False
    for f in files:
        stem = os.path.splitext(os.path.basename(f))[0].lower()
        if stem and stem in fl:
            return True
    return False


def passes_nav(row: dict, files: list[str]) -> bool:
    if len(files) < MIN_GOLD_FILES:
        return False
    if not has_fail_to_pass(row):
        return False
    ps = (row.get("problem_statement") or "").splitlines()
    first = ps[0] if ps else ""
    if issue_to_file_obvious(first, files):
        return False
    return True


def sha_key(instance_id: str) -> str:
    """Content-addressed deterministic order key (anti-benchmaxx: no hand-picking)."""
    return hashlib.sha256(instance_id.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Candidate collection
# ---------------------------------------------------------------------------
def collect_candidates(ds, want_langs: set[str]) -> dict[str, list[dict]]:
    """Return {lang: [candidate-record,...]} for NAV-passing rows in want_langs."""
    by_lang: dict[str, list[dict]] = defaultdict(list)
    for row in ds:
        files = gold_files(row.get("patch", ""))
        lang = derive_language(files)
        if lang not in want_langs:
            continue
        if not passes_nav(row, files):
            continue
        by_lang[lang].append(
            {
                "instance_id": row["instance_id"],
                "repo": row["repo"],
                "base_commit": row["base_commit"],
                "language": lang,
                "gold_file_count": len(files),
                "first_line": ((row.get("problem_statement") or "").splitlines() or [""])[0][
                    :160
                ],
            }
        )
    return by_lang


def pick_for_lang(cands: list[dict], lang: str) -> list[dict]:
    """Deterministic: sha-order, take PER_LANG, assign TRAIN/VAL/TEST in that order."""
    ordered = sorted(cands, key=lambda c: sha_key(c["instance_id"]))
    chosen = ordered[:PER_LANG]
    if len(chosen) < PER_LANG:
        raise SystemExit(
            f"FATAL: language {lang!r} has only {len(chosen)} NAV-passing candidates "
            f"(need {PER_LANG}). Relax NAV or accept fewer for this language."
        )
    for i, c in enumerate(chosen):
        c["split"] = SPLITS[i]
    return chosen


def to_manifest_entry(c: dict) -> dict:
    """Routing-only fields. NO patch / F2P / test content. Schema mirrors DeepSWE."""
    iid = c["instance_id"]
    repo = c["repo"]
    return {
        "instance_id": iid,  # required (fail-closed)
        "repo_url": f"https://github.com/{repo}",
        "language": c["language"],  # required (fail-closed)
        "commit_hash": c["base_commit"],
        "docker_image": swebench_image(iid),  # required (fail-closed)
        "category": "swebench_multilingual",
        "display_title": iid,
        "agent_timeout_sec": 5400.0,
        "instruction_chars": 0,
    }


# ---------------------------------------------------------------------------
# Emit
# ---------------------------------------------------------------------------
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "artifact_multilingual")
OUT_MANIFEST = os.path.join(OUT_DIR, "repo_manifest.json")
OUT_SPLIT = os.path.join(OUT_DIR, "SPLIT.md")

RUN_CMD = "python scripts/build_multilingual_manifest.py"


def write_manifest(tasks: list[dict]) -> None:
    lang_dist: Counter[str] = Counter(t["language"] for t in tasks)
    man = {
        "benchmark": "SWE-bench-Multilingual+Verified(python)",
        "source": (
            "https://huggingface.co/datasets/SWE-bench/SWE-bench_Multilingual + "
            "https://huggingface.co/datasets/princeton-nlp/SWE-bench_Verified"
        ),
        "note": (
            "Anti-overfit iteration/validation surface. DeepSWE (artifact_deepswe) stays "
            "HELD OUT. Selection rule lives in scripts/build_multilingual_manifest.py "
            "(picker), never in src/groundtruth/. Routing metadata only -- no gold/F2P."
        ),
        "total_tasks": len(tasks),
        "total_repos": len({t["repo_url"] for t in tasks}),
        "language_distribution": dict(sorted(lang_dist.items())),
        "tasks": tasks,
    }
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(OUT_MANIFEST, "w", encoding="utf-8") as f:
        json.dump(man, f, indent=2)
        f.write("\n")


def write_split_md(picks: list[dict], stub: bool, reason: str = "") -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    lines: list[str] = []
    lines.append("# SWE-bench Multilingual iteration manifest -- SPLIT")
    lines.append("")
    lines.append(
        "Anti-overfit iteration surface for GroundTruth. DeepSWE (`artifact_deepswe/`) "
        "stays **held out**; GT iterates/validates here."
    )
    lines.append("")
    lines.append("## NAV rule (why these tasks)")
    lines.append("")
    lines.append("A task is kept only if ALL hold (encoded in the picker, never in GT):")
    lines.append("")
    lines.append(f"1. `len(gold_files(patch)) >= {MIN_GOLD_FILES}` -- multi-file fix.")
    lines.append("2. non-empty `FAIL_TO_PASS` -- a real, gradeable test exists.")
    lines.append(
        "3. issue->file **non-obvious**: no gold file basename (sans ext) is a substring "
        "of the issue's first line. The issue does not name the file."
    )
    lines.append("")
    lines.append(
        "Single-file single-symbol feature adds are excluded by (1). Language is derived "
        "from the **gold patch file extensions** (Multilingual has no `language` column)."
    )
    lines.append("")
    lines.append("## Stratification + split")
    lines.append("")
    lines.append(
        f"3 per language for {{go, rust, typescript, javascript}} (Multilingual) + 3 "
        f"python (Verified) = 15. Within a language, tasks are ordered by "
        f"`sha256(instance_id)` and the first 3 taken, then assigned TRAIN/VAL/TEST in "
        f"that order -- so each split holds one task per language (deterministic, "
        f"content-addressed, never hand-picked)."
    )
    lines.append("")
    if stub:
        lines.append("## STATUS: STUB (HuggingFace was offline here)")
        lines.append("")
        lines.append(f"Reason: {reason}")
        lines.append("")
        lines.append(
            "No instance ids were fabricated. The manifest is a schema-correct empty stub. "
            "Run the picker on a GHA runner / Codespace with HF access:"
        )
        lines.append("")
        lines.append("```bash")
        lines.append("pip install datasets")
        lines.append(RUN_CMD)
        lines.append("```")
        lines.append("")
        return _flush(lines)

    lines.append("## The 15 picks")
    lines.append("")
    lines.append(
        "| instance_id | language | split | gold_file_count | why-navigation (issue 1st line) |"
    )
    lines.append("|---|---|---|---:|---|")
    order = {"TRAIN": 0, "VAL": 1, "TEST": 2}
    for p in sorted(picks, key=lambda p: (p["language"], order[p["split"]])):
        fl = p["first_line"].replace("|", "\\|")
        lines.append(
            f"| `{p['instance_id']}` | {p['language']} | {p['split']} | "
            f"{p['gold_file_count']} | {fl} |"
        )
    lines.append("")
    lines.append(
        "Each row passed NAV: >=2 gold files, has FAIL_TO_PASS, and the gold file is not "
        "named in the issue's first line -- so localization is the hard part GT must earn."
    )
    lines.append("")
    lines.append("## Trap (cold-successor note)")
    lines.append("")
    lines.append(
        "TypeScript is the tight bucket: exactly 3 NAV-passing tasks exist in Multilingual "
        "(measured 2026-06-17 -- go 6, javascript 6, rust 14, **typescript 3**). There is "
        "ZERO slack for TS. If a TS docker image fails to pull/build at eval time there is "
        "no substitute under the current NAV rule -- relax MIN_GOLD_FILES or the "
        "obviousness filter for TS, or accept <3 TS, and re-run the picker. Do not hand-edit "
        "the manifest."
    )
    lines.append("")
    _flush(lines)


def _flush(lines: list[str]) -> None:
    with open(OUT_SPLIT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    try:
        from datasets import load_dataset
    except ImportError as e:
        write_manifest([])
        write_split_md([], stub=True, reason=f"`datasets` not importable: {e}")
        print(f"STUB: datasets unavailable ({e}). Wrote schema-correct empty manifest.")
        return 0

    try:
        ml = load_dataset(ML_DATASET, split=SPLIT_NAME)
        py = load_dataset(PY_DATASET, split=SPLIT_NAME)
    except Exception as e:  # network / auth / HF offline
        write_manifest([])
        write_split_md([], stub=True, reason=f"load_dataset failed: {type(e).__name__}: {e}")
        print(f"STUB: HF offline ({type(e).__name__}: {e}). Wrote empty stub manifest.")
        return 0

    ml_cands = collect_candidates(ml, set(ML_LANGS))
    py_cands = collect_candidates(py, {PY_LANG})

    picks: list[dict] = []
    for lang in ML_LANGS:
        picks.extend(pick_for_lang(ml_cands.get(lang, []), lang))
    picks.extend(pick_for_lang(py_cands.get(PY_LANG, []), PY_LANG))

    tasks = [to_manifest_entry(p) for p in picks]
    write_manifest(tasks)
    write_split_md(picks, stub=False)

    print(f"Wrote {len(tasks)} tasks -> {OUT_MANIFEST}")
    dist = Counter(t["language"] for t in tasks)
    print("language_distribution:", dict(sorted(dist.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
