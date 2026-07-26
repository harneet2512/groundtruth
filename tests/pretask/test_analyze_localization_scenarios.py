"""Contract tests for the scenario-stratified localization analyzer.

Two kinds of test live here.

1. Definition tests that pin the stratum classifiers against the REAL corpus
   files in the repo.  The stratum sizes are the whole point of the analyzer -
   a silent drift in "what counts as intra-module" would silently re-cut every
   reported result - so the classifiers are checked against counts derived from
   `swebench_live_gold_cases.json` itself, not against a fixture that encodes
   the hypothesis.
2. Scorer tests that pin strict@k, any@k, recall@k, the rank metrics and the
   detectability labelling on constructed inputs where the right answer is
   arithmetic, plus the single-file invariant strict@k == any@k that any correct
   scorer must satisfy on a single-file corpus.

Tests against the real sealed artifact directories are opt-in through
`GT_LOC_PAIRED_DIRS` (os.pathsep separated) so no machine-specific absolute
path is committed.
"""
from __future__ import annotations

import json
import os
from collections import Counter
from pathlib import Path

import pytest

from scripts.analyze_localization_scenarios import (
    ANECDOTE_FLIP_FRACTION,
    DEFAULT_K,
    analyze,
    arm_metrics,
    census,
    classify_fan_out,
    classify_shape,
    embedder_state,
    gold_ranks,
    legacy_order_is_prefix_of_new,
    mcnemar_exact_p,
    min_one_way_discordant,
    multi_file_tags,
    names_gold_path_literally,
    read_corpus,
    read_paired_artifacts,
    render_census,
    render_report,
    stratum_power,
)

LIVE_CORPUS = Path("benchmarks/data/swebench_live_gold_cases.json")
OSS_CORPUS = Path("benchmarks/data/oss_all60_cases.json")


# ---------------------------------------------------------------------------
# stratum definitions, pinned against the real corpus
# ---------------------------------------------------------------------------


def _live_cases() -> list[dict]:
    return read_corpus(LIVE_CORPUS)


def test_shape_and_fan_out_reproduce_the_measured_corpus_strata():
    cases = _live_cases()
    assert len(cases) == 294
    shapes = Counter(classify_shape(case["gold_files"]) for case in cases)
    assert shapes == {
        "single-file": 172,
        "intra-module": 82,
        "co-located": 24,
        "cross-module": 16,
    }
    fan_out = Counter(classify_fan_out(case["gold_files"]) for case in cases)
    assert fan_out == {"1": 172, "2": 54, "3-5": 40, "6-10": 20, "11+": 8}


def test_random_split_strata_match_the_locked_primary_comparison_set():
    """The random split is the population the run's gates are computed on."""
    cases = [case for case in _live_cases() if case["split"] == "random"]
    assert len(cases) == 237
    shapes = Counter(classify_shape(case["gold_files"]) for case in cases)
    assert shapes == {
        "single-file": 138,
        "intra-module": 66,
        "co-located": 21,
        "cross-module": 12,
    }
    fan_out = Counter(classify_fan_out(case["gold_files"]) for case in cases)
    assert fan_out["2"] == 47
    assert fan_out["6-10"] == 15
    assert fan_out["11+"] == 6


def test_literal_path_named_stratum_matches_the_measured_count():
    cases = _live_cases()
    named = [
        case
        for case in cases
        if names_gold_path_literally(case["issue_text"], case["gold_files"])
    ]
    assert len(named) == 67


def test_multi_file_tags_match_the_measured_counts():
    """package-export is language-general here; the quoted 17 is python-only.

    `__init__.py` alone tags 17 of the 122 multi-file cases; adding the other
    ecosystems' export files adds exactly one case (a TypeScript `index.ts`),
    which is the generalizing choice and the documented +1.
    """
    multi = [case for case in _live_cases() if len(case["gold_files"]) > 1]
    assert len(multi) == 122
    tags = Counter(tag for case in multi for tag in multi_file_tags(case["gold_files"]))
    assert tags["deep-spread"] == 25
    assert tags["same-stem"] == 24
    assert tags["mixed-language"] == 7
    assert tags["package-export"] == 18
    python_only = sum(
        1
        for case in multi
        if any(path.endswith("__init__.py") for path in case["gold_files"])
    )
    assert python_only == 17


def test_shape_classifier_on_constructed_gold_sets():
    assert classify_shape(["src/a.py"]) == "single-file"
    assert classify_shape(["src/a.py", "src/b.py"]) == "co-located"
    assert classify_shape(["src/api/a.py", "src/core/b.py"]) == "intra-module"
    assert classify_shape(["src/a.py", "tests/b.py"]) == "cross-module"
    assert classify_shape([]) == "no-gold"
    # A repeated gold spelling is one file, not a multi-file shape.
    assert classify_shape(["src/a.py", "./src/a.py"]) == "single-file"


def test_fan_out_buckets():
    assert classify_fan_out(["a.py"]) == "1"
    assert classify_fan_out(["a.py", "b.py"]) == "2"
    assert classify_fan_out([f"f{index}.py" for index in range(5)]) == "3-5"
    assert classify_fan_out([f"f{index}.py" for index in range(10)]) == "6-10"
    assert classify_fan_out([f"f{index}.py" for index in range(11)]) == "11+"


def test_literal_path_named_is_case_insensitive_and_suffix_aware():
    assert names_gold_path_literally("fix aiogram/fsm/context.py", ["aiogram/fsm/context.py"])
    assert names_gold_path_literally("crash in Context.PY", ["aiogram/fsm/context.py"])
    assert names_gold_path_literally("see fsm/context.py", ["aiogram/fsm/context.py"])
    assert not names_gold_path_literally(
        "FSMContext should expose a single-value getter", ["aiogram/fsm/context.py"]
    )
    assert not names_gold_path_literally("", ["a.py"])


# ---------------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------------


def test_gold_rank_uses_the_unidirectional_same_file_rule():
    """A shallower candidate is a DIFFERENT file, not a gold hit.

    This is the exact defect present in the artifacts of run 30188004122, which
    scored `balancer.go` at rank 1 as gold for `balancer/balancer.go`.
    """
    ranked = ["balancer.go", "balancer/balancer.go"]
    assert gold_ranks(ranked, ["balancer/balancer.go"]) == {"balancer/balancer.go": 2}
    # a checkout prefix on the candidate side is still the same file
    assert gold_ranks(["checkout/src/a.py"], ["src/a.py"]) == {"src/a.py": 1}
    assert gold_ranks(["other.py"], ["src/a.py"]) == {"src/a.py": None}


def test_strict_and_any_diverge_only_when_gold_is_multi_file():
    gold = ["src/a.py", "src/b.py"]
    ranked = ["src/a.py", "noise.py", "noise2.py", "src/b.py"]
    metrics = arm_metrics(ranked, ranked, gold)
    assert metrics["any_at_1"] is True
    assert metrics["strict_at_1"] is False
    assert metrics["recall_at_1"] == 0.5
    assert metrics["any_at_5"] is True
    assert metrics["strict_at_5"] is True
    assert metrics["recall_at_5"] == 1.0
    assert metrics["first_gold_rank"] == 1
    assert metrics["all_gold_by_rank"] == 4

    single = arm_metrics(ranked, ranked, ["src/a.py"])
    for k in DEFAULT_K:
        assert single[f"strict_at_{k}"] == single[f"any_at_{k}"]


def test_all_gold_by_rank_is_none_until_every_gold_file_is_seen():
    metrics = arm_metrics(["src/a.py"], ["src/a.py"], ["src/a.py", "src/b.py"])
    assert metrics["first_gold_rank"] == 1
    assert metrics["all_gold_by_rank"] is None
    assert metrics["gold_found"] is True
    assert metrics["recall_at_10"] == 0.5


def test_gold_admitted_is_scored_on_the_admitted_set_not_the_ranking():
    """Ranking the gold file is not delivering it; the admitted set decides."""
    metrics = arm_metrics(
        ["src/a.py", "src/b.py"], ["src/b.py"], ["src/a.py", "src/b.py"]
    )
    assert metrics["any_at_10"] is True
    assert metrics["strict_at_10"] is True
    assert metrics["gold_admitted_any"] is True
    assert metrics["gold_admitted_all"] is False
    assert metrics["gold_admitted_recall"] == 0.5


def test_arm_metrics_on_empty_gold_is_empty_not_zero():
    assert arm_metrics(["a.py"], ["a.py"], []) == {}


# ---------------------------------------------------------------------------
# detectability
# ---------------------------------------------------------------------------


def test_exact_mcnemar_p_values():
    assert mcnemar_exact_p(0, 0) == 1.0
    assert mcnemar_exact_p(3, 3) == 1.0
    assert mcnemar_exact_p(5, 0) == pytest.approx(0.0625)
    assert mcnemar_exact_p(6, 0) == pytest.approx(0.03125)
    assert mcnemar_exact_p(0, 6) == pytest.approx(0.03125)
    assert mcnemar_exact_p(8, 0) == pytest.approx(0.0078125)


def test_six_one_way_discordant_pairs_is_the_floor_for_significance():
    assert min_one_way_discordant(0.05) == 6
    assert mcnemar_exact_p(5, 0) >= 0.05
    assert mcnemar_exact_p(6, 0) < 0.05


@pytest.mark.parametrize(
    "size,label",
    [
        (138, "POWERED"),
        (66, "POWERED"),
        (47, "POWERED"),
        (24, "POWERED"),  # 6/24 == 25% exactly, the inclusive boundary
        (23, "ANECDOTE"),
        (21, "ANECDOTE"),
        (15, "ANECDOTE"),
        (12, "ANECDOTE"),
        (6, "ANECDOTE"),
        (5, "UNDETECTABLE"),
        (1, "UNDETECTABLE"),
        (0, "EMPTY"),
    ],
)
def test_stratum_power_labels_the_buckets_of_the_294_case_run(size, label):
    power = stratum_power(size)
    assert power["label"] == label
    assert power["min_one_way_discordant_for_p05"] == 6
    assert power["anecdote"] is (label != "POWERED")
    if size:
        assert power["required_flip_fraction"] == pytest.approx(6 / size)
        if label == "ANECDOTE":
            assert power["required_flip_fraction"] > ANECDOTE_FLIP_FRACTION


def test_a_stratum_smaller_than_the_floor_cannot_reach_significance():
    """Every arrangement of 5 cases is non-significant - so say UNDETECTABLE."""
    for old_only in range(6):
        assert mcnemar_exact_p(old_only, 5 - old_only) >= 0.05
    assert stratum_power(5)["label"] == "UNDETECTABLE"


def test_census_of_the_random_split_labels_the_small_buckets():
    report = census(_live_cases(), splits=["random"])
    shape = report["strata"]["shape"]
    assert shape["single-file"]["power"]["label"] == "POWERED"
    assert shape["intra-module"]["power"]["label"] == "POWERED"
    assert shape["co-located"]["power"]["label"] == "ANECDOTE"
    assert shape["cross-module"]["power"]["label"] == "ANECDOTE"
    fan_out = report["strata"]["fan_out"]
    assert fan_out["2"]["power"]["label"] == "POWERED"
    assert fan_out["6-10"]["power"]["label"] == "ANECDOTE"
    assert fan_out["11+"]["power"]["label"] == "ANECDOTE"
    assert "ANECDOTE" in render_census(report)


# ---------------------------------------------------------------------------
# artifact handling
# ---------------------------------------------------------------------------


def _artifact(
    case_id: str,
    *,
    legacy: list[str],
    vnext: list[str],
    regions: list[str],
    encoded: int | None = 12,
    frozen: bool = True,
    sealed_old_rank: object = "absent",
    sealed_new_rank: object = "absent",
    split: str = "random",
) -> dict:
    """A minimal sealed paired artifact.

    A sealed rank left at "absent" omits the key entirely, which is how a
    fixture says "do not cross-check this arm" - writing a wrong rank there
    would make every fixture look like a scorer disagreement.
    """
    metrics: dict = {"leakage_count": 0}
    if encoded is not None:
        metrics["structured_semantic_encoded_count"] = encoded
    return {
        "case_id": case_id,
        "split": split,
        "language": "python",
        "old": {} if sealed_old_rank == "absent" else {"first_gold_rank": sealed_old_rank},
        "new": {} if sealed_new_rank == "absent" else {"first_gold_rank": sealed_new_rank},
        "explanation": {
            "capabilities": {"available": {"frozen_semantic": frozen}},
            "operational_metrics": metrics,
            "legacy_ranked_files": legacy,
            "vnext_ranked_files": vnext,
            "regions": [{"file_path": path} for path in regions],
        },
    }


def test_embedder_state_classification():
    assert embedder_state(_artifact("a", legacy=[], vnext=[], regions=[])) == "live"
    assert (
        embedder_state(_artifact("a", legacy=[], vnext=[], regions=[], encoded=0))
        == "dark"
    )
    assert (
        embedder_state(_artifact("a", legacy=[], vnext=[], regions=[], encoded=None))
        == "unknown"
    )
    assert (
        embedder_state(
            _artifact("a", legacy=[], vnext=[], regions=[], encoded=0, frozen=False)
        )
        == "unavailable"
    )


def test_dark_embedder_cases_are_excluded_and_counted_per_stratum():
    corpus = [
        {"id": "dark1", "issue_text": "x", "gold_files": ["src/a.py"], "split": "random"},
        {"id": "dark2", "issue_text": "x", "gold_files": ["src/a.py"], "split": "random"},
        {
            "id": "live1",
            "issue_text": "x",
            "gold_files": ["src/a.py", "src/b.py"],
            "split": "random",
        },
    ]
    artifacts = {
        "dark1": _artifact(
            "dark1", legacy=["src/a.py"], vnext=["src/a.py"], regions=["src/a.py"],
            encoded=0,
        ),
        "dark2": _artifact(
            "dark2", legacy=["src/a.py"], vnext=["src/a.py"], regions=["src/a.py"],
            encoded=0,
        ),
        "live1": _artifact(
            "live1",
            legacy=["src/a.py", "src/b.py"],
            vnext=["src/a.py", "src/b.py"],
            regions=["src/a.py"],
        ),
    }
    report = analyze(artifacts, corpus)
    assert report["exclusions"]["dark_embedder"] == 2
    assert report["embedder_states"] == {"dark": 2, "live": 1}
    single = report["strata"]["shape"]["single-file"]
    assert single["n"] == 0
    assert single["excluded_dark_embedder"] == 2
    assert single["dark_embedder_in_stratum"] == 2
    assert single["power"]["label"] == "EMPTY"
    co_located = report["strata"]["shape"]["co-located"]
    assert co_located["n"] == 1
    assert co_located["excluded_dark_embedder"] == 0

    included = analyze(artifacts, corpus, include_dark=True)
    assert included["strata"]["shape"]["single-file"]["n"] == 2
    assert included["exclusions"]["dark_embedder"] == 0
    # the outage stays visible even when the cases are scored
    assert included["embedder_states"]["dark"] == 2
    assert (
        included["strata"]["shape"]["single-file"]["dark_embedder_in_stratum"] == 2
    )


def test_unknown_embedder_state_is_reported_but_never_excluded():
    corpus = [{"id": "u", "issue_text": "x", "gold_files": ["src/a.py"]}]
    artifacts = {
        "u": _artifact(
            "u", legacy=["src/a.py"], vnext=["src/a.py"], regions=["src/a.py"],
            encoded=None,
        )
    }
    report = analyze(artifacts, corpus)
    assert report["exclusions"]["unknown_embedder"] == 1
    assert report["exclusions"]["dark_embedder"] == 0
    assert report["strata"]["all"]["all"]["n"] == 1
    # an artifact that carries no sealed rank is not comparable, not a mismatch
    agreement = report["scorer_agreement_with_sealed_first_gold_rank"]
    assert agreement["arm_ranks_not_comparable"] == 2
    assert agreement["agrees"] is True


def test_join_reports_both_unmatched_directions():
    corpus = [
        {"id": "both", "issue_text": "x", "gold_files": ["src/a.py"]},
        {"id": "gold_only", "issue_text": "x", "gold_files": ["src/a.py"]},
    ]
    artifacts = {
        "both": _artifact(
            "both", legacy=["src/a.py"], vnext=["src/a.py"], regions=["src/a.py"]
        ),
        "artifact_only": _artifact(
            "artifact_only", legacy=[], vnext=[], regions=[]
        ),
    }
    report = analyze(artifacts, corpus)
    assert report["join"]["matched"] == 1
    assert report["join"]["gold_without_artifact"] == ["gold_only"]
    assert report["join"]["artifact_without_gold"] == ["artifact_only"]


def test_split_filter_selects_the_primary_comparison_population():
    corpus = [
        {"id": "r", "issue_text": "x", "gold_files": ["src/a.py"], "split": "random"},
        {"id": "h", "issue_text": "x", "gold_files": ["src/a.py"], "split": "held"},
    ]
    artifacts = {
        "r": _artifact("r", legacy=["src/a.py"], vnext=["src/a.py"], regions=[]),
        "h": _artifact(
            "h", legacy=["src/a.py"], vnext=["src/a.py"], regions=[], split="held"
        ),
    }
    report = analyze(artifacts, corpus, splits=["random"])
    assert report["join"]["matched"] == 1
    assert report["join"]["split_filtered_out"] == 1


def test_disagreement_with_the_sealed_rank_is_surfaced_not_silently_overridden():
    corpus = [{"id": "c", "issue_text": "x", "gold_files": ["balancer/balancer.go"]}]
    artifacts = {
        "c": _artifact(
            "c",
            legacy=["balancer/balancer.go"],
            vnext=["balancer.go", "balancer/balancer.go"],
            regions=["balancer/balancer.go"],
            sealed_old_rank=1,
            sealed_new_rank=1,  # what the old bidirectional matcher recorded
        )
    }
    report = analyze(artifacts, corpus)
    agreement = report["scorer_agreement_with_sealed_first_gold_rank"]
    assert agreement["agrees"] is False
    assert agreement["arm_ranks_not_comparable"] == 0
    assert agreement["old_mismatches"] == []
    assert agreement["new_mismatches"] == [
        {"case_id": "c", "sealed": 1, "recomputed": 2}
    ]


def test_legacy_floor_prefix_is_measured_per_case():
    assert legacy_order_is_prefix_of_new(
        {"old_ranked": ["a.py", "b.py"], "new_ranked": ["a.py", "b.py", "c.py"]}
    )
    assert not legacy_order_is_prefix_of_new(
        {"old_ranked": ["a.py", "b.py"], "new_ranked": ["b.py", "a.py"]}
    )
    assert not legacy_order_is_prefix_of_new({"old_ranked": [], "new_ranked": ["a.py"]})


def test_mcnemar_and_power_are_reported_together_per_stratum():
    """Seven cases, all discordant one way: significant, but the bucket is small."""
    corpus = [
        {
            "id": f"c{index}",
            "issue_text": "x",
            "gold_files": ["src/a.py", "src/b.py"],
            "split": "random",
        }
        for index in range(7)
    ]
    artifacts = {
        f"c{index}": _artifact(
            f"c{index}",
            legacy=["noise.py"],
            vnext=["src/a.py", "src/b.py"],
            regions=["src/a.py", "src/b.py"],
        )
        for index in range(7)
    }
    report = analyze(artifacts, corpus)
    stratum = report["strata"]["shape"]["co-located"]
    assert stratum["n"] == 7
    metric = stratum["metrics"]["strict_at_5"]
    assert metric["old"] == 0.0
    assert metric["new"] == 1.0
    assert metric["discordant_old_only"] == 0
    assert metric["discordant_new_only"] == 7
    assert metric["significant_at_0_05"] is True
    # ...and the stratum still says a result here rests on 7 cases
    assert stratum["power"]["label"] == "ANECDOTE"
    rendered = render_report(report)
    assert "[ANECDOTE]" in rendered


def test_report_renders_without_a_stratum_that_has_no_scored_cases():
    corpus = [{"id": "c", "issue_text": "x", "gold_files": ["src/a.py"]}]
    artifacts = {
        "c": _artifact(
            "c", legacy=["src/a.py"], vnext=["src/a.py"], regions=[], encoded=0
        )
    }
    report = analyze(artifacts, corpus)
    rendered = render_report(report)
    assert "no scored cases in this stratum" in rendered
    assert "excluded 1 of 1 dark" in rendered


# ---------------------------------------------------------------------------
# the single-file invariant
# ---------------------------------------------------------------------------


def test_oss_60_corpus_is_single_file_only():
    cases = read_corpus(OSS_CORPUS)
    assert len(cases) == 60
    assert {len(case["gold_files"]) for case in cases} == {1}
    assert Counter(classify_shape(case["gold_files"]) for case in cases) == {
        "single-file": 60
    }


def test_strict_equals_any_holds_on_a_single_file_corpus_and_fails_on_multi():
    corpus = [
        {"id": "s", "issue_text": "x", "gold_files": ["src/a.py"]},
        {"id": "m", "issue_text": "x", "gold_files": ["src/a.py", "src/b.py"]},
    ]
    artifacts = {
        "s": _artifact(
            "s",
            legacy=["noise.py", "src/a.py"],
            vnext=["src/a.py"],
            regions=["src/a.py"],
        ),
        "m": _artifact(
            "m",
            legacy=["src/a.py"] + [f"n{index}.py" for index in range(9)] + ["src/b.py"],
            vnext=["src/a.py", "src/b.py"],
            regions=["src/a.py"],
        ),
    }
    single_only = analyze(artifacts, corpus[:1])
    assert single_only["strict_equals_any_on_every_case"]["holds_on_every_case"]
    assert single_only["strict_equals_any_on_every_case"]["multi_file_cases"] == 0

    both = analyze(artifacts, corpus)
    check = both["strict_equals_any_on_every_case"]
    # the multi-file case ranks src/b.py at 11 in the old arm, so strict@10 is
    # False where any@10 is True - the divergence a single-file corpus can never
    # show, and the reason the invariant is a scorer check, not a product claim
    assert check["holds_on_every_case"] is False
    assert {violation["case_id"] for violation in check["violations"]} == {"m"}


def _paired_dirs() -> list[Path]:
    raw = os.environ.get("GT_LOC_PAIRED_DIRS", "")
    return [Path(part) for part in raw.split(os.pathsep) if part.strip()]


@pytest.mark.skipif(
    not _paired_dirs(), reason="set GT_LOC_PAIRED_DIRS to sealed paired artifact dirs"
)
@pytest.mark.parametrize("paired_dir", _paired_dirs(), ids=lambda path: path.parent.name)
def test_real_oss60_artifacts_keep_strict_equal_to_any(paired_dir: Path):
    """The validation the analyzer must survive on real sealed artifacts.

    oss-60 is single-file only, so strict@k must equal any@k on every case in
    both arms.  A single violation means the scorer is wrong.
    """
    artifacts = read_paired_artifacts(paired_dir)
    report = analyze(artifacts, read_corpus(OSS_CORPUS))
    assert report["join"]["matched"] == 60
    assert report["join"]["artifact_without_gold"] == []
    assert report["join"]["gold_without_artifact"] == []
    check = report["strict_equals_any_on_every_case"]
    assert check["multi_file_cases"] == 0
    assert check["violation_count"] == 0
    assert check["holds_on_every_case"] is True
    assert report["strata"]["shape"]["single-file"]["n"] == check["scored_cases"]


def test_json_report_is_serializable():
    corpus = [{"id": "c", "issue_text": "x", "gold_files": ["src/a.py"]}]
    artifacts = {
        "c": _artifact(
            "c", legacy=["src/a.py"], vnext=["src/a.py"], regions=["src/a.py"]
        )
    }
    payload = json.dumps(analyze(artifacts, corpus))
    assert "gt.localization.scenarios.v1" in payload
