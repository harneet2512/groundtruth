#!/usr/bin/env python
"""Scenario-stratified analysis of sealed old/new localization artifacts.

The aggregate winner gate answers one question: did the new arm beat the old
one over the whole corpus.  On a multi-file corpus that question is the wrong
grain.  A lexical floor over the 294-case corpus holds any@10 flat near 80%
across every gold shape while strict@10 collapses 81% -> 62% -> 30% -> 6% from
single-file to cross-module, so a single headline number is an average over
populations that behave nothing alike.

This module reports the SAME paired metrics inside each measured scenario
stratum, and refuses to let a stratum too small to detect anything print a
number that looks like a result:

* Strata are derived from the corpus gold itself - shape (single-file,
  co-located, intra-module, cross-module), fan-out (1, 2, 3-5, 6-10, 11+),
  their cross, and the separate stratum of issues that literally name a gold
  path or basename (those measure string matching, not retrieval).
* Every stratum carries its DETECTABILITY: the minimum number of one-way
  discordant pairs an exact two-sided McNemar test needs for p<0.05, and that
  count as a fraction of the bucket.  A stratum needing more than a quarter of
  its cases to flip is labelled ANECDOTE; a stratum smaller than the minimum is
  labelled UNDETECTABLE.  Both labels ride on every line the stratum prints.
* Cases whose frozen embedder reported available while encoding nothing are
  DARK and are excluded, with the exclusion counted per stratum.  Scoring a
  case whose semantic leg silently did not run measures the outage, not the arm.

Scoring reuses `_same_file` / `_matched_gold` from the comparison module on
purpose.  A second, private notion of "same file" in a second scorer is the
exact defect that module documents: one scorer holding two notions of file
identity credits a shallower wrong file as gold and inflates recall, precision
and rank together.
"""
from __future__ import annotations

import argparse
import json
import math
import posixpath
import statistics
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from groundtruth.pretask.localization_vnext.comparison import (  # noqa: E402
    _matched_gold,
    _norm,
    _same_file,
)

SCHEMA = "gt.localization.scenarios.v1"
DEFAULT_K = (1, 3, 5, 8, 10)
ALPHA = 0.05
ANECDOTE_FLIP_FRACTION = 0.25

SHAPE_ORDER = ("single-file", "co-located", "intra-module", "cross-module")
FAN_OUT_ORDER = ("1", "2", "3-5", "6-10", "11+")
TAG_ORDER = ("deep-spread", "same-stem", "package-export", "mixed-language")

# Package/module export files: a gold set that contains one of these is edited
# through its package surface, not only in place.  Language-general on purpose;
# the python-only `__init__.py` rule counts 17 of the 122 multi-file cases in
# `swebench_live_gold_cases.json`, this rule counts 18 (it adds the one
# TypeScript `index.ts` case).
EXPORT_BASENAMES = frozenset(
    {
        "__init__.py",
        "__init__.pyi",
        "index.js",
        "index.jsx",
        "index.ts",
        "index.tsx",
        "mod.rs",
        "lib.rs",
    }
)


# ---------------------------------------------------------------------------
# stratum derivation - from gold, never from an outcome
# ---------------------------------------------------------------------------


def normalized_gold(gold_files: Iterable[str]) -> list[str]:
    """Gold paths, normalized and de-duplicated, order preserved.

    strict@k asks whether EVERY gold file is inside the top k, so a duplicate
    spelling of one gold file would make strict@k unreachable for that case.
    """
    return list(dict.fromkeys(_norm(str(path)) for path in gold_files if str(path)))


def classify_shape(gold_files: Sequence[str]) -> str:
    """Where the gold set sits relative to itself.

    single-file    one gold file
    co-located     several gold files, all in one directory
    intra-module   several directories under one top-level module
    cross-module   the gold set spans top-level modules
    """
    gold = normalized_gold(gold_files)
    if not gold:
        return "no-gold"
    if len(gold) == 1:
        return "single-file"
    directories = {posixpath.dirname(path) for path in gold}
    if len(directories) == 1:
        return "co-located"
    tops = {path.split("/")[0] for path in gold}
    if len(tops) == 1:
        return "intra-module"
    return "cross-module"


def classify_fan_out(gold_files: Sequence[str]) -> str:
    """How many files the fix has to touch, bucketed."""
    count = len(normalized_gold(gold_files))
    if count == 0:
        return "0"
    if count == 1:
        return "1"
    if count == 2:
        return "2"
    if count <= 5:
        return "3-5"
    if count <= 10:
        return "6-10"
    return "11+"


def _path_suffixes(path: str) -> list[str]:
    parts = _norm(path).split("/")
    return ["/".join(parts[index:]) for index in range(len(parts))]


def names_gold_path_literally(issue_text: str, gold_files: Sequence[str]) -> bool:
    """Does the issue text spell out a gold path or basename?

    Case-insensitive substring match of any trailing path suffix of a gold file
    (the basename is the shortest such suffix).  67 of the 294 cases in
    `swebench_live_gold_cases.json` match.  Those cases measure string matching,
    not retrieval, so they are reported as their own stratum and counted as
    contamination inside every other stratum.
    """
    text = (issue_text or "").lower()
    if not text:
        return False
    return any(
        suffix.lower() in text
        for path in normalized_gold(gold_files)
        for suffix in _path_suffixes(path)
    )


def multi_file_tags(gold_files: Sequence[str]) -> tuple[str, ...]:
    """Overlapping descriptors of a multi-file gold set (never a partition)."""
    gold = normalized_gold(gold_files)
    if len(gold) < 2:
        return ()
    tags: list[str] = []
    depths = [path.count("/") for path in gold]
    if max(depths) - min(depths) >= 2:
        tags.append("deep-spread")
    stems = [posixpath.splitext(posixpath.basename(path))[0] for path in gold]
    if len(stems) != len(set(stems)):
        tags.append("same-stem")
    if any(posixpath.basename(path) in EXPORT_BASENAMES for path in gold):
        tags.append("package-export")
    if len({posixpath.splitext(path)[1].lower() for path in gold}) > 1:
        tags.append("mixed-language")
    return tuple(tags)


def strata_for_case(case: Mapping[str, Any]) -> dict[str, Any]:
    """Every stratum key a single corpus case belongs to."""
    gold = list(case.get("gold_files") or ())
    shape = classify_shape(gold)
    fan_out = classify_fan_out(gold)
    literal = names_gold_path_literally(str(case.get("issue_text") or ""), gold)
    return {
        "shape": shape,
        "fan_out": fan_out,
        "shape_x_fan_out": f"{shape}|{fan_out}",
        "literal_path_named": "named" if literal else "not-named",
        "literal": literal,
        "tags": list(multi_file_tags(gold)),
    }


# ---------------------------------------------------------------------------
# detectability - what this bucket size can and cannot show
# ---------------------------------------------------------------------------


def _binomial_cdf_half(successes: int, trials: int) -> float:
    return sum(math.comb(trials, index) for index in range(successes + 1)) / (
        2**trials
    )


def mcnemar_exact_p(old_only: int, new_only: int) -> float:
    """Exact two-sided McNemar p over the discordant pairs only."""
    discordant = old_only + new_only
    if discordant == 0:
        return 1.0
    return min(1.0, 2.0 * _binomial_cdf_half(min(old_only, new_only), discordant))


def min_one_way_discordant(alpha: float = ALPHA, limit: int = 1000) -> int:
    """Fewest all-one-way discordant pairs that can reach p<alpha.

    With every discordant pair pointing the same way the exact two-sided p is
    2 * 0.5**m, so p<0.05 needs m>=6 - and a stratum holding fewer than 6 cases
    cannot produce a significant result no matter how the arms behave.
    """
    for count in range(1, limit + 1):
        if mcnemar_exact_p(count, 0) < alpha:
            return count
    return limit + 1


def stratum_power(
    size: int,
    *,
    alpha: float = ALPHA,
    anecdote_flip_fraction: float = ANECDOTE_FLIP_FRACTION,
) -> dict[str, Any]:
    """What this bucket size can prove, stated before any rate is printed."""
    required = min_one_way_discordant(alpha)
    if size <= 0:
        return {
            "n": size,
            "min_one_way_discordant_for_p05": required,
            "required_flip_fraction": None,
            "label": "EMPTY",
            "anecdote": True,
            "note": "no cases in this stratum",
        }
    fraction = required / size
    if size < required:
        label, note = (
            "UNDETECTABLE",
            f"n={size} < {required} one-way discordant pairs needed for p<{alpha}"
            "; no arrangement of outcomes in this stratum can reach significance",
        )
    elif fraction > anecdote_flip_fraction + 1e-12:
        label, note = (
            "ANECDOTE",
            f"{required}/{size} = {fraction:.1%} of the bucket must flip one way"
            f" for p<{alpha}; rates below are descriptive only",
        )
    else:
        label, note = (
            "POWERED",
            f"{required}/{size} = {fraction:.1%} of the bucket must flip one way"
            f" for p<{alpha}",
        )
    return {
        "n": size,
        "min_one_way_discordant_for_p05": required,
        "required_flip_fraction": fraction,
        "label": label,
        "anecdote": label != "POWERED",
        "note": note,
    }


# ---------------------------------------------------------------------------
# per-case scoring
# ---------------------------------------------------------------------------


def gold_ranks(
    ranked_files: Sequence[str], gold_files: Sequence[str]
) -> dict[str, int | None]:
    """First rank (1-based) at which each gold file appears, or None."""
    ranks: dict[str, int | None] = {}
    for gold in normalized_gold(gold_files):
        ranks[gold] = None
        for index, candidate in enumerate(ranked_files, start=1):
            if _same_file(str(candidate), gold):
                ranks[gold] = index
                break
    return ranks


def arm_metrics(
    ranked_files: Sequence[str],
    admitted_files: Sequence[str],
    gold_files: Sequence[str],
    *,
    ks: Sequence[int] = DEFAULT_K,
) -> dict[str, Any]:
    """Paired-comparable metrics for one arm on one case.

    any@k    at least one gold file inside the top k
    strict@k EVERY gold file inside the top k - the metric single-file corpora
             cannot distinguish from any@k, and the one that collapses with
             fan-out
    recall@k share of gold files inside the top k
    """
    gold = normalized_gold(gold_files)
    if not gold:
        return {}
    ranks = gold_ranks(ranked_files, gold)
    found = [rank for rank in ranks.values() if rank is not None]
    metrics: dict[str, Any] = {}
    for k in ks:
        within = [rank for rank in found if rank <= k]
        metrics[f"any_at_{k}"] = bool(within)
        metrics[f"strict_at_{k}"] = len(within) == len(gold)
        metrics[f"recall_at_{k}"] = len(within) / len(gold)
    metrics["gold_found"] = bool(found)
    metrics["first_gold_rank"] = min(found) if found else None
    # The depth the agent must read before it has seen the WHOLE gold set.
    metrics["all_gold_by_rank"] = max(found) if len(found) == len(gold) else None
    matched = _matched_gold([str(path) for path in admitted_files], gold)
    metrics["gold_admitted_recall"] = len(matched) / len(gold)
    metrics["gold_admitted_all"] = len(matched) == len(gold)
    metrics["gold_admitted_any"] = bool(matched)
    metrics["admitted_file_count"] = len(admitted_files)
    metrics["ranked_file_count"] = len(ranked_files)
    return metrics


def binary_metric_names(ks: Sequence[int] = DEFAULT_K) -> list[str]:
    names = []
    for k in ks:
        names.append(f"any_at_{k}")
        names.append(f"strict_at_{k}")
    names.extend(["gold_found", "gold_admitted_any", "gold_admitted_all"])
    return names


def rate_metric_names(ks: Sequence[int] = DEFAULT_K) -> list[str]:
    return [f"recall_at_{k}" for k in ks] + ["gold_admitted_recall"]


RANK_METRIC_NAMES = ("first_gold_rank", "all_gold_by_rank")


# ---------------------------------------------------------------------------
# artifact reading
# ---------------------------------------------------------------------------


def embedder_state(artifact: Mapping[str, Any]) -> str:
    """dark | live | unavailable | unknown.

    DARK is the failure this exclusion exists for: the capability census
    reported `frozen_semantic` AVAILABLE while the run encoded zero structured
    semantic items, so the semantic leg contributed nothing while the artifact
    claims it was present.  A sealed artifact that never wrote the counter is
    UNKNOWN - it is not evidence of an outage and is not excluded.
    """
    explanation = artifact.get("explanation") or {}
    available = (explanation.get("capabilities") or {}).get("available") or {}
    if not bool(available.get("frozen_semantic")):
        return "unavailable"
    metrics = explanation.get("operational_metrics") or {}
    if "structured_semantic_encoded_count" not in metrics:
        return "unknown"
    try:
        encoded = int(metrics["structured_semantic_encoded_count"])
    except (TypeError, ValueError):
        return "unknown"
    return "dark" if encoded == 0 else "live"


def arm_file_lists(artifact: Mapping[str, Any]) -> dict[str, list[str]]:
    """The two arms' model-visible file lists, straight out of the seal.

    old  `legacy_ranked_files`  - the legacy model-visible order.  The legacy
         arm has no curation stage, so its ADMITTED set is the same list; that
         asymmetry is reported, not hidden.
    new  `vnext_ranked_files`   - the ranked discovery order actually scored by
         the sealed comparison, and `regions` - the admitted region files.
    """
    explanation = artifact.get("explanation") or {}
    old_ranked = [str(path) for path in explanation.get("legacy_ranked_files") or ()]
    new_ranked = [str(path) for path in explanation.get("vnext_ranked_files") or ()]
    admitted = list(
        dict.fromkeys(
            str(region.get("file_path") or "")
            for region in explanation.get("regions") or ()
            if region.get("file_path")
        )
    )
    return {
        "old_ranked": old_ranked,
        "old_admitted": list(old_ranked),
        "new_ranked": new_ranked,
        "new_admitted": admitted,
    }


def legacy_order_is_prefix_of_new(lists: Mapping[str, Sequence[str]]) -> bool:
    """Is the new arm's ranking the legacy order with a tail appended?

    The shadow engine seeds itself with the model-visible legacy order as a
    recall-first floor.  Where that floor survives into the ranking, the new arm
    CANNOT rank gold worse than the old one and can only add hits below the
    floor - so a rank or hit@k delta on the floored arm is structurally
    one-directional, not evidence that the engine reordered anything.  Measured
    per case, never assumed: it holds on 59/60 artifacts of run 30196352388 and
    on 2/60 of run 30188004122.
    """
    old = [_norm(str(path)) for path in lists["old_ranked"]]
    new = [_norm(str(path)) for path in lists["new_ranked"]]
    return bool(old) and new[: len(old)] == old


def read_paired_artifacts(paired_dir: Path) -> dict[str, dict[str, Any]]:
    artifacts: dict[str, dict[str, Any]] = {}
    for path in sorted(paired_dir.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        case_id = str(payload.get("case_id") or path.stem)
        artifacts[case_id] = payload
    return artifacts


def read_corpus(corpus_path: Path) -> list[dict[str, Any]]:
    payload = json.loads(corpus_path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        return list(payload)
    return list(payload.get("cases") or payload.get("rows") or [])


def _artifact_split(artifact: Mapping[str, Any], case: Mapping[str, Any]) -> str:
    return str(artifact.get("split") or case.get("split") or "unknown")


# ---------------------------------------------------------------------------
# aggregation
# ---------------------------------------------------------------------------


def _fmean(values: Sequence[float]) -> float | None:
    return statistics.fmean(values) if values else None


def summarize_stratum(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Paired old/new summary of one stratum, detectability stated first."""
    scored = [row for row in rows if row.get("scored")]
    size = len(scored)
    summary: dict[str, Any] = {
        "n": size,
        "n_joined": len(rows),
        "excluded_dark_embedder": sum(
            1 for row in rows if row.get("exclusion") == "dark_embedder"
        ),
        # Dark cases present in this stratum whether or not they were excluded,
        # so `--include-dark` cannot make the outage invisible.
        "dark_embedder_in_stratum": sum(
            1 for row in rows if row.get("embedder_state") == "dark"
        ),
        "excluded_other": {
            reason: sum(1 for row in rows if row.get("exclusion") == reason)
            for reason in sorted(
                {
                    str(row["exclusion"])
                    for row in rows
                    if row.get("exclusion") and row["exclusion"] != "dark_embedder"
                }
            )
        },
        "literal_path_named_in_stratum": sum(
            1 for row in scored if row["strata"]["literal"]
        ),
        "power": stratum_power(size),
        "metrics": {},
    }
    if not scored:
        return summary

    ks = tuple(scored[0]["ks"])
    for name in binary_metric_names(ks):
        old_hits = [bool(row["old"][name]) for row in scored]
        new_hits = [bool(row["new"][name]) for row in scored]
        old_only = sum(1 for o, n in zip(old_hits, new_hits) if o and not n)
        new_only = sum(1 for o, n in zip(old_hits, new_hits) if n and not o)
        p_value = mcnemar_exact_p(old_only, new_only)
        summary["metrics"][name] = {
            "old": _fmean([1.0 if hit else 0.0 for hit in old_hits]),
            "new": _fmean([1.0 if hit else 0.0 for hit in new_hits]),
            "discordant_old_only": old_only,
            "discordant_new_only": new_only,
            "discordant_total": old_only + new_only,
            "mcnemar_exact_p": p_value,
            "significant_at_0_05": p_value < ALPHA,
        }
    for name in rate_metric_names(ks):
        old_values = [float(row["old"][name]) for row in scored]
        new_values = [float(row["new"][name]) for row in scored]
        pairs = list(zip(old_values, new_values))
        summary["metrics"][name] = {
            "old": _fmean(old_values),
            "new": _fmean(new_values),
            "delta": _fmean([n - o for o, n in pairs]),
            "new_better": sum(1 for o, n in pairs if n > o),
            "new_worse": sum(1 for o, n in pairs if n < o),
            "tie": sum(1 for o, n in pairs if n == o),
        }
    for name in RANK_METRIC_NAMES:
        old_ranks = [row["old"][name] for row in scored]
        new_ranks = [row["new"][name] for row in scored]
        both = [
            (int(old), int(new))
            for old, new in zip(old_ranks, new_ranks)
            if old is not None and new is not None
        ]
        summary["metrics"][name] = {
            # A rank exists only when the arm produced the required hit, so the
            # medians are over pairs where BOTH arms did; the censored counts
            # carry the rest instead of a fabricated rank.
            "old_measured": sum(1 for rank in old_ranks if rank is not None),
            "new_measured": sum(1 for rank in new_ranks if rank is not None),
            "paired_measured": len(both),
            "old_median_when_paired": (
                statistics.median(old for old, _ in both) if both else None
            ),
            "new_median_when_paired": (
                statistics.median(new for _, new in both) if both else None
            ),
            "new_better": sum(1 for old, new in both if new < old),
            "new_worse": sum(1 for old, new in both if new > old),
            "tie": sum(1 for old, new in both if new == old),
            "old_only_measured": sum(
                1
                for old, new in zip(old_ranks, new_ranks)
                if old is not None and new is None
            ),
            "new_only_measured": sum(
                1
                for old, new in zip(old_ranks, new_ranks)
                if new is not None and old is None
            ),
        }
    return summary


def _stratum_families(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, list[Mapping[str, Any]]]]:
    families: dict[str, dict[str, list[Mapping[str, Any]]]] = {
        "all": {"all": list(rows)},
        "shape": {},
        "fan_out": {},
        "shape_x_fan_out": {},
        "literal_path_named": {},
        "tags": {},
    }
    for row in rows:
        strata = row["strata"]
        for family in ("shape", "fan_out", "shape_x_fan_out", "literal_path_named"):
            families[family].setdefault(strata[family], []).append(row)
        for tag in strata["tags"]:
            families["tags"].setdefault(tag, []).append(row)
    return families


def _sorted_keys(family: str, keys: Iterable[str]) -> list[str]:
    keys = list(keys)
    if family == "shape":
        order = {name: index for index, name in enumerate(SHAPE_ORDER)}
    elif family == "fan_out":
        order = {name: index for index, name in enumerate(FAN_OUT_ORDER)}
    elif family == "tags":
        order = {name: index for index, name in enumerate(TAG_ORDER)}
    elif family == "shape_x_fan_out":
        shape_order = {name: index for index, name in enumerate(SHAPE_ORDER)}
        fan_order = {name: index for index, name in enumerate(FAN_OUT_ORDER)}
        return sorted(
            keys,
            key=lambda key: (
                shape_order.get(key.split("|")[0], 99),
                fan_order.get(key.split("|")[-1], 99),
                key,
            ),
        )
    else:
        order = {"named": 0, "not-named": 1}
    return sorted(keys, key=lambda key: (order.get(key, 99), key))


def analyze(
    artifacts: Mapping[str, Mapping[str, Any]],
    corpus: Sequence[Mapping[str, Any]],
    *,
    ks: Sequence[int] = DEFAULT_K,
    splits: Sequence[str] = (),
    include_dark: bool = False,
) -> dict[str, Any]:
    """Join sealed artifacts to corpus gold and report per stratum."""
    gold_by_id = {str(case["id"]): case for case in corpus}
    split_filter = {str(split).lower() for split in splits}

    rows: list[dict[str, Any]] = []
    join = {
        "corpus_cases": len(gold_by_id),
        "artifacts": len(artifacts),
        "matched": 0,
        "artifact_without_gold": sorted(set(artifacts) - set(gold_by_id)),
        "gold_without_artifact": sorted(set(gold_by_id) - set(artifacts)),
        "split_filtered_out": 0,
    }
    exclusions = {
        "dark_embedder": 0,
        "unknown_embedder": 0,
        "no_gold": 0,
        "missing_explanation": 0,
    }
    embedder_states: dict[str, int] = {}
    agreement = {
        "checked": 0,
        "not_comparable": 0,
        "old_mismatch": [],
        "new_mismatch": [],
    }
    shadow_only_present = 0
    legacy_floored = 0

    for case_id, artifact in sorted(artifacts.items()):
        case = gold_by_id.get(case_id)
        if case is None:
            continue
        split = _artifact_split(artifact, case)
        if split_filter and split.lower() not in split_filter:
            join["split_filtered_out"] += 1
            continue
        join["matched"] += 1
        strata = strata_for_case(case)
        state = embedder_state(artifact)
        embedder_states[state] = embedder_states.get(state, 0) + 1
        row: dict[str, Any] = {
            "case_id": case_id,
            "split": split,
            "language": str(artifact.get("language") or case.get("language") or "unknown"),
            "strata": strata,
            "embedder_state": state,
            "ks": tuple(ks),
            "scored": False,
            "exclusion": None,
        }
        if not (artifact.get("explanation") or {}):
            row["exclusion"] = "missing_explanation"
            exclusions["missing_explanation"] += 1
            rows.append(row)
            continue
        gold = normalized_gold(case.get("gold_files") or ())
        if not gold:
            row["exclusion"] = "no_gold"
            exclusions["no_gold"] += 1
            rows.append(row)
            continue
        if state == "unknown":
            exclusions["unknown_embedder"] += 1
        if state == "dark" and not include_dark:
            row["exclusion"] = "dark_embedder"
            exclusions["dark_embedder"] += 1
            rows.append(row)
            continue

        lists = arm_file_lists(artifact)
        if (artifact.get("explanation") or {}).get("vnext_ranked_files_shadow_only"):
            shadow_only_present += 1
        row["legacy_order_is_prefix_of_new"] = legacy_order_is_prefix_of_new(lists)
        legacy_floored += 1 if row["legacy_order_is_prefix_of_new"] else 0
        row["old"] = arm_metrics(
            lists["old_ranked"], lists["old_admitted"], gold, ks=ks
        )
        row["new"] = arm_metrics(
            lists["new_ranked"], lists["new_admitted"], gold, ks=ks
        )
        row["gold_file_count"] = len(gold)
        row["scored"] = True

        # Cross-check against the rank the sealed run scored for itself.  A
        # mismatch means the corpus gold joined here is not the gold the run was
        # scored against - a join defect, not a metric.
        agreement["checked"] += 1
        for arm, sealed_key in (("old", "old"), ("new", "new")):
            sealed_arm = artifact.get(sealed_key) or {}
            if "first_gold_rank" not in sealed_arm:
                # Nothing to compare against; an absent key is not a mismatch.
                agreement["not_comparable"] += 1
                continue
            sealed_rank = sealed_arm["first_gold_rank"]
            if sealed_rank != row[arm]["first_gold_rank"]:
                agreement[f"{arm}_mismatch"].append(
                    {
                        "case_id": case_id,
                        "sealed": sealed_rank,
                        "recomputed": row[arm]["first_gold_rank"],
                    }
                )
        rows.append(row)

    families = _stratum_families(rows)
    strata_report: dict[str, dict[str, Any]] = {}
    for family, buckets in families.items():
        strata_report[family] = {
            key: summarize_stratum(buckets[key])
            for key in _sorted_keys(family, buckets)
        }
    return {
        "schema": SCHEMA,
        "k_values": list(ks),
        "alpha": ALPHA,
        "anecdote_flip_fraction": ANECDOTE_FLIP_FRACTION,
        "arms": {
            "old": "explanation.legacy_ranked_files (ranked == admitted; the "
            "legacy arm delivers whole files and has no curation stage)",
            "new": "explanation.vnext_ranked_files (ranked) / explanation.regions "
            "(admitted)",
        },
        "join": join,
        "exclusions": exclusions,
        "embedder_states": dict(sorted(embedder_states.items())),
        "shadow_only_column_present": shadow_only_present,
        "legacy_floor": {
            "scored": sum(1 for row in rows if row.get("scored")),
            "new_ranking_starts_with_legacy_order": legacy_floored,
            "note": "where the legacy order prefixes the new ranking, the new arm"
            " cannot rank gold worse than the old one; hit@k and rank deltas on"
            " that population are structurally one-directional",
        },
        "scorer_agreement_with_sealed_first_gold_rank": {
            "checked": agreement["checked"],
            "arm_ranks_not_comparable": agreement["not_comparable"],
            "old_mismatches": agreement["old_mismatch"],
            "new_mismatches": agreement["new_mismatch"],
            "agrees": not agreement["old_mismatch"] and not agreement["new_mismatch"],
        },
        "strict_equals_any_on_every_case": strict_equals_any_check(rows),
        "strata": strata_report,
        "cases": [
            {
                "case_id": row["case_id"],
                "split": row["split"],
                "shape": row["strata"]["shape"],
                "fan_out": row["strata"]["fan_out"],
                "literal_path_named": row["strata"]["literal"],
                "embedder_state": row["embedder_state"],
                "exclusion": row["exclusion"],
                "gold_file_count": row.get("gold_file_count"),
                "legacy_order_is_prefix_of_new": row.get(
                    "legacy_order_is_prefix_of_new"
                ),
                "old": row.get("old"),
                "new": row.get("new"),
            }
            for row in rows
        ],
    }


def strict_equals_any_check(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """strict@k == any@k on every scored case, per arm and k.

    On a single-file corpus this must hold by construction: with one gold file
    "at least one gold file in the top k" and "every gold file in the top k" are
    the same statement.  A single violation means the scorer is wrong.
    """
    scored = [row for row in rows if row.get("scored")]
    violations = []
    for row in scored:
        for k in row["ks"]:
            for arm in ("old", "new"):
                if row[arm][f"strict_at_{k}"] != row[arm][f"any_at_{k}"]:
                    violations.append(
                        {
                            "case_id": row["case_id"],
                            "arm": arm,
                            "k": k,
                            "gold_file_count": row["gold_file_count"],
                        }
                    )
    single_file = [row for row in scored if row["gold_file_count"] == 1]
    return {
        "scored_cases": len(scored),
        "single_file_cases": len(single_file),
        "multi_file_cases": len(scored) - len(single_file),
        "holds_on_every_case": not violations,
        "violations": violations[:20],
        "violation_count": len(violations),
    }


def census(
    corpus: Sequence[Mapping[str, Any]], *, splits: Sequence[str] = ()
) -> dict[str, Any]:
    """Stratum sizes and detectability from the corpus alone, before any run."""
    split_filter = {str(split).lower() for split in splits}
    rows = []
    for case in corpus:
        split = str(case.get("split") or "unknown")
        if split_filter and split.lower() not in split_filter:
            continue
        rows.append(
            {
                "case_id": str(case.get("id") or ""),
                "split": split,
                "strata": strata_for_case(case),
                "scored": True,
                "exclusion": None,
                "ks": tuple(DEFAULT_K),
            }
        )
    families = _stratum_families(rows)
    return {
        "schema": "gt.localization.scenarios.census.v1",
        "cases": len(rows),
        "strata": {
            family: {
                key: {
                    "n": len(buckets[key]),
                    "literal_path_named": sum(
                        1 for row in buckets[key] if row["strata"]["literal"]
                    ),
                    "power": stratum_power(len(buckets[key])),
                }
                for key in _sorted_keys(family, buckets)
            }
            for family, buckets in families.items()
        },
    }


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def _rate(value: Any) -> str:
    if value is None:
        return "  n/a"
    return f"{float(value):5.3f}"


def _optional(value: Any) -> str:
    return "n/a" if value is None else str(value)


def render_report(report: Mapping[str, Any], *, summary_only: bool = False) -> str:
    lines: list[str] = []
    join = report["join"]
    lines.append("=" * 78)
    lines.append("LOCALIZATION SCENARIO ANALYSIS")
    lines.append("=" * 78)
    lines.append(f"corpus cases          : {join['corpus_cases']}")
    lines.append(f"paired artifacts      : {join['artifacts']}")
    lines.append(f"joined                : {join['matched']}")
    lines.append(f"artifact without gold : {len(join['artifact_without_gold'])}")
    lines.append(f"gold without artifact : {len(join['gold_without_artifact'])}")
    if join["split_filtered_out"]:
        lines.append(f"filtered by --split   : {join['split_filtered_out']}")
    exclusions = report["exclusions"]
    lines.append(
        "dark embedder         : "
        f"excluded {exclusions['dark_embedder']} of "
        f"{report['embedder_states'].get('dark', 0)} dark"
        "  (frozen_semantic available, structured_semantic_encoded_count == 0)"
    )
    if exclusions["unknown_embedder"]:
        lines.append(
            f"embedder state unknown: {exclusions['unknown_embedder']}"
            "  (counter absent from the seal; NOT excluded)"
        )
    if exclusions["no_gold"]:
        lines.append(f"excluded no gold      : {exclusions['no_gold']}")
    if exclusions["missing_explanation"]:
        lines.append(
            f"excluded no explanation: {exclusions['missing_explanation']}"
        )
    lines.append(f"embedder states       : {report['embedder_states']}")
    lines.append(
        "shadow-only column    : "
        f"{report['shadow_only_column_present']} artifacts carry it"
    )
    floor = report["legacy_floor"]
    lines.append(
        "legacy floor          : "
        f"{floor['new_ranking_starts_with_legacy_order']}/{floor['scored']}"
        " new rankings start with the legacy order"
        + (
            "  <- rank/hit deltas below are structurally one-directional"
            if floor["scored"]
            and floor["new_ranking_starts_with_legacy_order"] * 2 > floor["scored"]
            else ""
        )
    )
    lines.append(f"arm old               : {report['arms']['old']}")
    lines.append(f"arm new               : {report['arms']['new']}")
    agreement = report["scorer_agreement_with_sealed_first_gold_rank"]
    lines.append(
        "scorer vs sealed rank : "
        f"{'AGREES' if agreement['agrees'] else 'MISMATCH'} on "
        f"{agreement['checked']} cases "
        f"(old {len(agreement['old_mismatches'])} / "
        f"new {len(agreement['new_mismatches'])} mismatched)"
    )
    for arm in ("old", "new"):
        for mismatch in agreement[f"{arm}_mismatches"][:10]:
            lines.append(
                f"  {arm} {mismatch['case_id']}: sealed rank "
                f"{_optional(mismatch['sealed'])} vs recomputed "
                f"{_optional(mismatch['recomputed'])}"
            )
    strict_check = report["strict_equals_any_on_every_case"]
    lines.append(
        "strict@k == any@k     : "
        f"{'HOLDS' if strict_check['holds_on_every_case'] else 'VIOLATED'} on "
        f"{strict_check['scored_cases']} scored cases "
        f"({strict_check['single_file_cases']} single-file, "
        f"{strict_check['multi_file_cases']} multi-file); "
        f"{strict_check['violation_count']} violations"
    )
    lines.append(
        f"significance          : exact two-sided McNemar, alpha={report['alpha']};"
        f" a stratum needing >{report['anecdote_flip_fraction']:.0%} of its bucket"
        " to flip is ANECDOTE"
    )
    lines.append("")

    lines.append("-" * 78)
    lines.append("STRATUM SUMMARY (paired; old -> new)")
    lines.append("-" * 78)
    header = (
        f"{'stratum':<34}{'n':>4}{'dark':>5}{'lit':>4}  "
        f"{'any@8':>13} {'strict@8':>13} {'power':<13}"
    )
    lines.append(header)
    for family, buckets in report["strata"].items():
        for key, summary in buckets.items():
            metrics = summary["metrics"]
            any8 = metrics.get("any_at_8", {})
            strict8 = metrics.get("strict_at_8", {})
            label = summary["power"]["label"]
            lines.append(
                f"{family + '=' + key:<34}"
                f"{summary['n']:>4}"
                f"{summary['excluded_dark_embedder']:>5}"
                f"{summary['literal_path_named_in_stratum']:>4}  "
                f"{_rate(any8.get('old'))}->{_rate(any8.get('new'))} "
                f"{_rate(strict8.get('old'))}->{_rate(strict8.get('new'))} "
                f"{label:<13}"
            )
    lines.append("")

    if summary_only:
        return "\n".join(lines)

    for family, buckets in report["strata"].items():
        for key, summary in buckets.items():
            lines.extend(_render_stratum(family, key, summary, report["k_values"]))
    return "\n".join(lines)


def _render_stratum(
    family: str, key: str, summary: Mapping[str, Any], ks: Sequence[int]
) -> list[str]:
    power = summary["power"]
    tag = "" if power["label"] == "POWERED" else f"  [{power['label']}]"
    lines = ["=" * 78, f"STRATUM {family}={key}   n={summary['n']}{tag}", "-" * 78]
    lines.append(f"power       : {power['label']} - {power['note']}")
    lines.append(
        f"joined {summary['n_joined']}  scored {summary['n']}  "
        f"dark-embedder {summary['dark_embedder_in_stratum']} "
        f"(excluded {summary['excluded_dark_embedder']})  "
        f"literal-path-named inside this stratum "
        f"{summary['literal_path_named_in_stratum']}"
    )
    if not summary["metrics"]:
        lines.append("no scored cases in this stratum")
        lines.append("")
        return lines
    suffix = "" if power["label"] == "POWERED" else f" [{power['label']}]"
    lines.append(
        f"{'metric':<22}{'old':>7}{'new':>8}{'delta':>8}"
        f"{'b(old)':>8}{'c(new)':>8}{'p':>9}"
    )
    for name in binary_metric_names(ks):
        metric = summary["metrics"][name]
        delta = (
            None
            if metric["old"] is None or metric["new"] is None
            else metric["new"] - metric["old"]
        )
        lines.append(
            f"{name:<22}{_rate(metric['old']):>7}{_rate(metric['new']):>8}"
            f"{_rate(delta):>8}{metric['discordant_old_only']:>8}"
            f"{metric['discordant_new_only']:>8}"
            f"{metric['mcnemar_exact_p']:>9.4f}"
            f"{'' if not metric['significant_at_0_05'] else '  SIG'}"
            f"{suffix}"
        )
    lines.append(
        f"{'metric':<22}{'old':>7}{'new':>8}{'delta':>8}"
        f"{'better':>8}{'worse':>8}{'tie':>9}"
    )
    for name in rate_metric_names(ks):
        metric = summary["metrics"][name]
        lines.append(
            f"{name:<22}{_rate(metric['old']):>7}{_rate(metric['new']):>8}"
            f"{_rate(metric['delta']):>8}{metric['new_better']:>8}"
            f"{metric['new_worse']:>8}{metric['tie']:>9}{suffix}"
        )
    for name in RANK_METRIC_NAMES:
        metric = summary["metrics"][name]
        lines.append(
            f"{name:<22}"
            f"measured old {metric['old_measured']} / new {metric['new_measured']}"
            f" / paired {metric['paired_measured']};"
            f" median {_optional(metric['old_median_when_paired'])}"
            f" -> {_optional(metric['new_median_when_paired'])};"
            f" better {metric['new_better']} worse {metric['new_worse']}"
            f" tie {metric['tie']}"
            f"{suffix}"
        )
    lines.append("")
    return lines


def render_census(report: Mapping[str, Any]) -> str:
    lines = ["=" * 78, f"CORPUS STRATUM CENSUS  cases={report['cases']}", "=" * 78]
    lines.append(f"{'stratum':<40}{'n':>5}{'lit':>5}  {'flips':>6}  power")
    for family, buckets in report["strata"].items():
        for key, entry in buckets.items():
            power = entry["power"]
            fraction = power["required_flip_fraction"]
            lines.append(
                f"{family + '=' + key:<40}{entry['n']:>5}"
                f"{entry['literal_path_named']:>5}  "
                f"{power['min_one_way_discordant_for_p05']:>6}  "
                f"{power['label']}"
                f"{'' if fraction is None else f' ({fraction:.1%} of bucket)'}"
            )
    return "\n".join(lines)


def _json_ready(value: Any) -> Any:
    if isinstance(value, float):
        return f"{value:.8f}"
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    return value


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Scenario-stratified paired analysis of localization artifacts"
    )
    parser.add_argument("--paired", help="directory of sealed paired/*.json artifacts")
    parser.add_argument("--corpus", required=True, help="gold corpus JSON")
    parser.add_argument("--out", help="write the full report as JSON here")
    parser.add_argument(
        "--split", action="append", default=[], help="restrict to these splits"
    )
    parser.add_argument(
        "--k",
        default=",".join(str(k) for k in DEFAULT_K),
        help="comma-separated k values",
    )
    parser.add_argument(
        "--census-only",
        action="store_true",
        help="stratum sizes and detectability from the corpus alone",
    )
    parser.add_argument(
        "--include-dark",
        action="store_true",
        help="score dark-embedder cases instead of excluding them",
    )
    parser.add_argument("--summary-only", action="store_true")
    args = parser.parse_args(argv)

    corpus = read_corpus(Path(args.corpus))
    if args.census_only:
        report = census(corpus, splits=args.split)
        print(render_census(report))
    else:
        if not args.paired:
            parser.error("--paired is required unless --census-only is set")
        artifacts = read_paired_artifacts(Path(args.paired))
        ks = tuple(int(part) for part in str(args.k).split(",") if part.strip())
        report = analyze(
            artifacts,
            corpus,
            ks=ks,
            splits=args.split,
            include_dark=args.include_dark,
        )
        print(render_report(report, summary_only=args.summary_only))
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(_json_ready(report), indent=2, sort_keys=True),
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
