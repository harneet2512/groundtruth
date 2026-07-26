from __future__ import annotations

from groundtruth.pretask.localization_vnext.comparison import (
    _input_digests,
    _legacy_inspection_files,
    _legacy_ranking_priors,
    _matches,
    _shadow_legacy_candidates,
    _shadow_only_ranked_files,
    _shadow_total_latency_samples,
    evaluate_winner,
    score_sealed_case,
)


def _row(
    language: str,
    *,
    old_h1: bool = True,
    new_h1: bool = True,
    old_h8: bool = True,
    new_h8: bool = True,
    old_precision: float = 0.5,
    new_precision: float = 0.6,
    old_tokens: int = 1000,
    new_tokens: int = 500,
    old_latency: float = 100.0,
    new_latency: float = 110.0,
    old_memory: int = 1000,
    new_memory: int = 1100,
) -> dict:
    return {
        "language": language,
        "scorable": True,
        "region_scorable": True,
        "safety": {
            "deterministic": True,
            "leakage_count": 0,
            "legacy_byte_identity": True,
        },
        "old": {
            "hit_at_1": old_h1,
            "hit_at_8": old_h8,
            "file_precision": old_precision,
            "symbol_recall": 1.0,
            "line_recall": 1.0,
            "implied_inspection_tokens": old_tokens,
            "latency_ms": old_latency,
            "peak_memory_bytes": old_memory,
        },
        "new": {
            "hit_at_1": new_h1,
            "hit_at_8": new_h8,
            "file_precision": new_precision,
            "symbol_recall": 1.0,
            "line_recall": 1.0,
            "implied_inspection_tokens": new_tokens,
            "latency_ms": new_latency,
            "peak_memory_bytes": new_memory,
        },
    }


def _corpus(**kwargs) -> list[dict]:
    return [
        _row(language, **kwargs)
        for language in ("python", "go", "javascript", "typescript", "rust")
        for _ in range(3)
    ]


def test_new_wins_only_when_every_recall_safety_efficiency_gate_holds():
    verdict = evaluate_winner(_corpus())
    assert verdict["verdict"] == "NEW_WINS"
    assert verdict["context_reduction_fraction"] >= 0.25


def test_any_safety_or_recall_regression_makes_old_win():
    rows = _corpus()
    rows[0]["safety"]["legacy_byte_identity"] = False
    assert evaluate_winner(rows)["verdict"] == "OLD_WINS"

    rows = _corpus()
    rows[0]["new"]["hit_at_8"] = False
    assert evaluate_winner(rows)["verdict"] == "OLD_WINS"


def test_recall_safe_but_small_context_reduction_is_tie():
    assert (
        evaluate_winner(_corpus(old_tokens=1000, new_tokens=800))["verdict"]
        == "TIE"
    )


def test_thin_region_gold_makes_region_metrics_unmeasured_not_the_whole_verdict():
    """Thin region gold disables REGION metrics, not the retrieval verdict.

    This test previously pinned the opposite - that too few region-scorable
    cases in one language returns INCONCLUSIVE for the entire run. That was the
    defect encoded as the contract: the gate returned before computing hit@1,
    hit@8, precision, latency or memory, so on the real corpus (0/60
    region-scorable) it never judged anything at all, on any run.
    """
    rows = _corpus()
    rows = [
        row
        for index, row in enumerate(rows)
        if not (row["language"] == "rust" and index % 3 == 2)
    ]

    verdict = evaluate_winner(rows)

    assert verdict["verdict"] != "INCONCLUSIVE"
    assert "random_primary_hit_at_1" in verdict
    # the region-level metrics are the ones that go unmeasured
    assert verdict["symbol_recall"]["old"] is None
    assert verdict["region_precision"]["old"] is None


def test_latency_or_memory_over_125x_makes_old_win():
    assert (
        evaluate_winner(_corpus(new_latency=126.0))["verdict"] == "OLD_WINS"
    )
    assert (
        evaluate_winner(_corpus(new_memory=1251))["verdict"] == "OLD_WINS"
    )


def test_symbol_or_region_precision_regression_makes_old_win():
    rows = _corpus()
    rows[0]["old"]["symbol_precision"] = 1.0
    rows[0]["new"]["symbol_precision"] = 0.0
    assert evaluate_winner(rows)["verdict"] == "OLD_WINS"

    rows = _corpus()
    rows[0]["old"]["region_precision"] = 1.0
    rows[0]["new"]["region_precision"] = 0.0
    assert evaluate_winner(rows)["verdict"] == "OLD_WINS"


def test_overall_recall_gate_uses_random_primary_split_only():
    rows = _corpus()
    held = _row("python", old_h1=True, new_h1=False)
    held["split"] = "held"
    rows.append(held)
    verdict = evaluate_winner(rows)
    assert verdict["verdict"] == "NEW_WINS"
    assert verdict["random_primary_hit_at_1"] == {"old": 1.0, "new": 1.0}


def test_missing_random_primary_split_is_inconclusive():
    rows = _corpus()
    for row in rows:
        row["split"] = "held"
    verdict = evaluate_winner(rows)
    assert verdict["verdict"] == "INCONCLUSIVE"
    assert verdict["reason"] == "random_primary_comparison_set_unavailable"


def test_legacy_inspection_tokens_use_model_visible_brief_before_reactive_rows():
    selected = _legacy_inspection_files(
        {"candidate_order": []},
        {"focus_set": ["src/v74.py"]},
        {
            "candidate_order": [
                "src/brief.py",
                "src/brief.py",
                "src/second.py",
            ]
        },
    )

    assert selected == ["src/brief.py", "src/second.py"]

    assert _legacy_inspection_files(
        {"candidate_order": ["src/reactive.py"]},
        {"focus_set": ["src/v74.py"]},
        {"candidate_order": []},
    ) == ["src/v74.py"]


def test_legacy_ranking_priors_preserve_exact_model_visible_file_order():
    priors = _legacy_ranking_priors([f"src/legacy_{index}.py" for index in range(1, 9)])

    assert [item["path"] for item in priors] == [f"src/legacy_{index}.py" for index in range(1, 9)]
    assert [item["legacy_rank"] for item in priors] == list(range(1, 9))
    assert all(item["ranking_prior_only"] is True for item in priors)


def test_shadow_seed_prefix_is_exact_model_visible_order_before_other_candidates():
    v74 = object()
    reactive = object()
    seed = _shadow_legacy_candidates(
        ["src/final_brief_first.py", "src/final_brief_second.py"],
        [v74],
        [reactive],
    )

    assert [item["path"] for item in seed[:2]] == [
        "src/final_brief_first.py",
        "src/final_brief_second.py",
    ]
    assert seed[2:] == [v74, reactive]


def test_shadow_latency_samples_keep_measured_cold_cache_cost():
    samples = _shadow_total_latency_samples(
        legacy_latency_ms=10.0,
        shadow_verification_latency_ms=110.0,
        vnext_warm_latencies_ms=[2.0, 3.0, 4.0],
    )

    assert samples == [110.0, 12.0, 13.0, 14.0]


def test_scoring_uses_measured_legacy_byte_identity_instead_of_stamping_pass():
    sealed = {
        "case": {"id": "case", "language": "python", "split": "random"},
        "legacy": {
            "candidate_order": ["src/a.py"],
            "witnesses": [],
            "implied_inspection_tokens": 10,
            "latency_ms": 1.0,
            "peak_memory_bytes": 1,
            "byte_identity": False,
        },
        "vnext": {
            "discoveries": [],
            "admitted_regions": [],
            "metrics": {"leakage_count": 0},
        },
        "comparison": {
            "new_admitted_files": [],
            "ranked_discovery_files": [],
            "deterministic": True,
            "p95_latency_ms": 1.0,
            "shadow_total_p95_latency_ms": 9.0,
            "peak_memory_bytes": 1,
            "shadow_total_peak_memory_bytes": 9,
            "implied_inspection_tokens": 1,
        },
    }

    scored = score_sealed_case(sealed, {"gold_files": ["src/a.py"]})

    assert scored["safety"]["legacy_byte_identity"] is False
    assert scored["new"]["latency_ms"] == 9.0
    assert scored["new"]["peak_memory_bytes"] == 9


def test_patch_grounded_scoring_records_symbol_region_and_line_precision():
    sealed = {
        "case": {"id": "case", "language": "python", "split": "random"},
        "legacy": {
            "candidate_order": ["src/a.py"],
            "witnesses": ["GoldSymbol"],
            "implied_inspection_tokens": 10,
            "latency_ms": 1.0,
            "peak_memory_bytes": 1,
            "byte_identity": True,
        },
        "vnext": {
            "discoveries": [
                {"symbol": "GoldSymbol"},
                {"symbol": "NoiseSymbol"},
            ],
            "admitted_regions": [
                {
                    "file_path": "src/a.py",
                    "start_line": 10,
                    "end_line": 12,
                },
                {
                    "file_path": "src/b.py",
                    "start_line": 30,
                    "end_line": 31,
                },
            ],
            "metrics": {"leakage_count": 0},
        },
        "comparison": {
            "new_admitted_files": ["src/a.py", "src/b.py"],
            "ranked_discovery_files": ["src/a.py", "src/b.py"],
            "deterministic": True,
            "p95_latency_ms": 1.0,
            "peak_memory_bytes": 1,
            "implied_inspection_tokens": 1,
        },
    }
    gold = {
        "gold_files": ["src/a.py"],
        "gold_symbols": ["GoldSymbol"],
        "gold_line_ranges": [
            {"file": "src/a.py", "start": 11, "end": 12},
        ],
    }

    scored = score_sealed_case(sealed, gold)

    assert scored["new"]["symbol_recall"] == 1.0
    assert scored["new"]["symbol_precision"] == 0.5
    assert scored["new"]["region_recall"] == 1.0
    assert scored["new"]["region_precision"] == 0.5
    assert scored["new"]["line_recall"] == 1.0
    assert scored["new"]["line_precision"] == 0.4


def test_line_scoring_accepts_the_same_suffix_path_match_as_file_scoring():
    sealed = {
        "case": {"id": "case", "language": "python", "split": "random"},
        "legacy": {
            "candidate_order": [],
            "witnesses": [],
            "implied_inspection_tokens": 0,
            "latency_ms": 1.0,
            "peak_memory_bytes": 1,
            "byte_identity": True,
        },
        "vnext": {
            "discoveries": [],
            "admitted_regions": [
                {
                    "file_path": "checkout/src/a.py",
                    "start_line": 10,
                    "end_line": 12,
                }
            ],
            "metrics": {"leakage_count": 0},
        },
        "comparison": {
            "new_admitted_files": ["checkout/src/a.py"],
            "ranked_discovery_files": ["checkout/src/a.py"],
            "deterministic": True,
            "p95_latency_ms": 1.0,
            "peak_memory_bytes": 1,
            "implied_inspection_tokens": 1,
        },
    }

    scored = score_sealed_case(
        sealed,
        {
            "gold_files": ["src/a.py"],
            "gold_line_ranges": [
                {"file": "src/a.py", "start": 11, "end": 12},
            ],
        },
    )

    assert scored["new"]["line_recall"] == 1.0
    assert scored["new"]["line_precision"] == 2 / 3


def test_shadow_only_ranked_files_drop_model_visible_prior_rows():
    class _Unit:
        def __init__(self, file_path, prior):
            self.file_path = file_path
            self.metadata = (("ranking_prior_only", "1"),) if prior else ()

    files = _shadow_only_ranked_files(
        [
            _Unit("src/legacy_first.py", True),
            _Unit("src/shadow_found.py", False),
            _Unit("src/legacy_second.py", True),
            _Unit("src/shadow_found.py", False),
            _Unit("", False),
        ]
    )

    assert files == ["src/shadow_found.py"]


def test_scoring_reports_the_shadow_only_column_beside_the_floored_column():
    sealed = {
        "case": {"id": "case", "language": "python", "split": "random"},
        "legacy": {
            "candidate_order": ["src/legacy.py"],
            "witnesses": [],
            "implied_inspection_tokens": 10,
            "latency_ms": 1.0,
            "peak_memory_bytes": 1,
            "byte_identity": True,
        },
        "vnext": {
            "discoveries": [],
            "admitted_regions": [],
            "metrics": {"leakage_count": 0},
        },
        "comparison": {
            "new_admitted_files": [],
            "ranked_discovery_files": ["src/legacy.py", "src/gold.py"],
            "ranked_discovery_files_shadow_only": ["src/gold.py"],
            "deterministic": True,
            "p95_latency_ms": 1.0,
            "peak_memory_bytes": 1,
            "implied_inspection_tokens": 1,
        },
    }

    scored = score_sealed_case(sealed, {"gold_files": ["src/gold.py"]})

    assert scored["new"]["first_gold_rank"] == 2
    assert scored["new"]["hit_at_1"] is False
    assert scored["new_shadow_only"]["measured"] is True
    assert scored["new_shadow_only"]["first_gold_rank"] == 1
    assert scored["new_shadow_only"]["hit_at_1"] is True
    assert scored["new_shadow_only"]["hit_at_8"] is True


def test_shadow_only_column_is_unmeasured_not_zero_on_older_sealed_artifacts():
    """A pre-column artifact must never score as a measured shadow-only miss."""
    sealed = {
        "case": {"id": "case", "language": "python", "split": "random"},
        "legacy": {
            "candidate_order": ["src/gold.py"],
            "witnesses": [],
            "implied_inspection_tokens": 10,
            "latency_ms": 1.0,
            "peak_memory_bytes": 1,
            "byte_identity": True,
        },
        "vnext": {"discoveries": [], "admitted_regions": [], "metrics": {"leakage_count": 0}},
        "comparison": {
            "new_admitted_files": [],
            "ranked_discovery_files": ["src/gold.py"],
            "deterministic": True,
            "p95_latency_ms": 1.0,
            "peak_memory_bytes": 1,
            "implied_inspection_tokens": 1,
        },
    }

    scored = score_sealed_case(sealed, {"gold_files": ["src/gold.py"]})

    assert scored["new"]["hit_at_1"] is True
    assert scored["new_shadow_only"]["measured"] is False
    assert scored["new_shadow_only"]["hit_at_1"] is None
    assert scored["new_shadow_only"]["first_gold_rank"] is None
    assert scored["new_shadow_only"]["file_precision"] is None


def test_shadow_only_order_uses_the_engine_rank_not_the_floored_order():
    class _Unit:
        def __init__(self, file_path, prior, shadow_rank):
            self.file_path = file_path
            md = [("shadow_rank", str(shadow_rank))]
            if prior:
                md.append(("ranking_prior_only", "1"))
            self.metadata = tuple(md)

    files = _shadow_only_ranked_files(
        [
            _Unit("src/legacy_first.py", True, 9),
            _Unit("src/second_by_engine.py", False, 5),
            _Unit("src/first_by_engine.py", False, 2),
        ]
    )

    assert files == ["src/first_by_engine.py", "src/second_by_engine.py"]


# ---------------------------------------------------------------------------
# Multi-file gold scoring.  The old corpus carried 0 gold_symbols and 0
# gold_line_ranges on all 60 cases, so every symbol/region/line path below ran
# for the first time against the 294-case swebench-live corpus (122 multi-file,
# 3169 line ranges, 1355 symbols).  ONE hand-computed case backs these tests:
#
#   gold files (3)   : src/alpha.py, src/beta.py, src/deep/utils.py
#   gold ranges (5)  : alpha 10-12, alpha 40-41, beta 5-7, beta 60, utils 100-102
#   gold lines (12)  : alpha {10,11,12,40,41} + beta {5,6,7,60} + utils {100,101,102}
#
#   OLD delivers 4 files: checkout/src/alpha.py, build/src/alpha.py (the SAME
#   gold file twice, under two prefixes), utils.py (a DIFFERENT file from
#   src/deep/utils.py) and src/unrelated.py.
#   NEW delivers 5 regions: alpha 11-13, alpha 50-51, beta 6, noise 1-2 and
#   checkout/src/beta.py 60 -> 4 admitted files, 9 admitted lines.
#
# Every expected value is computed by hand in the assertions, never read back
# out of the scorer.
# ---------------------------------------------------------------------------

_MULTI_FILE_GOLD = {
    "gold_files": ["src/alpha.py", "src/beta.py", "src/deep/utils.py"],
    "gold_symbols": ["parse_alpha", "render_beta"],
    "gold_line_ranges": [
        {"file": "src/alpha.py", "start": 10, "end": 12},
        {"file": "src/alpha.py", "start": 40, "end": 41},
        {"file": "src/beta.py", "start": 5, "end": 7},
        {"file": "src/beta.py", "start": 60, "end": 60},
        {"file": "src/deep/utils.py", "start": 100, "end": 102},
    ],
}


def _multi_file_sealed() -> dict:
    return {
        "case": {"id": "multi", "language": "python", "split": "random"},
        "legacy": {
            "candidate_order": [
                "checkout/src/alpha.py",
                "build/src/alpha.py",
                "utils.py",
                "src/unrelated.py",
            ],
            "witnesses": ["parse_alpha calls render_beta [CALLS]"],
            "implied_inspection_tokens": 400,
            "latency_ms": 1.0,
            "peak_memory_bytes": 1,
            "byte_identity": True,
        },
        "vnext": {
            "discoveries": [{"symbol": "parse_alpha"}, {"symbol": "NoiseSym"}],
            "admitted_regions": [
                {"file_path": "src/alpha.py", "start_line": 11, "end_line": 13},
                {"file_path": "src/alpha.py", "start_line": 50, "end_line": 51},
                {"file_path": "src/beta.py", "start_line": 6, "end_line": 6},
                {"file_path": "src/noise.py", "start_line": 1, "end_line": 2},
                {
                    "file_path": "checkout/src/beta.py",
                    "start_line": 60,
                    "end_line": 60,
                },
            ],
            "metrics": {"leakage_count": 0},
        },
        "comparison": {
            "new_admitted_files": [
                "src/alpha.py",
                "src/beta.py",
                "src/noise.py",
                "checkout/src/beta.py",
            ],
            "ranked_discovery_files": [
                "src/alpha.py",
                "src/beta.py",
                "src/noise.py",
                "checkout/src/beta.py",
            ],
            "deterministic": True,
            "p95_latency_ms": 1.0,
            "peak_memory_bytes": 1,
            "implied_inspection_tokens": 40,
        },
    }


def test_matches_does_not_credit_a_shallower_path_as_a_deeper_gold_file():
    """A candidate may carry a checkout prefix; it may not drop path segments."""
    gold = {"src/deep/utils.py"}

    # the direction that is real: the candidate carries an extra prefix
    assert _matches("src/deep/utils.py", gold) is True
    assert _matches("checkout/src/deep/utils.py", gold) is True
    # the direction that fabricates hits: a root-level utils.py is a DIFFERENT
    # file, and with multi-file gold it was credited as the gold file
    assert _matches("utils.py", gold) is False
    assert _matches("src/utils.py", gold) is False
    # the segment boundary itself must still hold in the surviving direction
    assert _matches("checkout/src/deep/myutils.py", gold) is False


def test_file_recall_counts_matched_gold_files_not_matching_candidates():
    scored = score_sealed_case(_multi_file_sealed(), _MULTI_FILE_GOLD)

    # preconditions: gold really is multi-file and matching really ran
    assert scored["scorable"] is True
    assert len(_MULTI_FILE_GOLD["gold_files"]) == 3
    assert scored["old"]["first_gold_rank"] == 1
    assert scored["new"]["first_gold_rank"] == 1

    # OLD names src/alpha.py twice (two prefixes) plus a bogus root utils.py;
    # exactly ONE of the three gold files is actually found.
    assert scored["old"]["file_recall"] == 1 / 3
    # NEW admits src/alpha.py and src/beta.py (the latter twice, once
    # prefixed); exactly TWO of the three gold files are found.
    assert scored["new"]["file_recall"] == 2 / 3
    # 2 of 4 legacy candidates are a gold file, 3 of 4 ranked new files are
    assert scored["old"]["file_precision"] == 2 / 4
    assert scored["new"]["file_precision"] == 3 / 4


def test_old_line_recall_and_region_recall_use_one_notion_of_same_file():
    scored = score_sealed_case(_multi_file_sealed(), _MULTI_FILE_GOLD)

    # preconditions: the region/line paths really are exercised
    assert scored["region_scorable"] is True
    assert len(_MULTI_FILE_GOLD["gold_line_ranges"]) == 5

    # legacy covers only src/alpha.py, under a prefix, on both scorers:
    # 5 of the 12 gold lines, 2 of the 5 gold ranges.
    assert scored["old"]["line_recall"] == 5 / 12
    assert scored["old"]["region_recall"] == 2 / 5


def test_region_precision_is_the_same_ratio_on_both_arms():
    scored = score_sealed_case(_multi_file_sealed(), _MULTI_FILE_GOLD)

    assert scored["region_scorable"] is True
    # OLD delivers 4 whole files = 4 regions; the 2 alpha forms hold a gold
    # range, utils.py and src/unrelated.py do not.
    assert scored["old"]["region_precision"] == 2 / 4
    # NEW delivers 5 regions; alpha 11-13, beta 6 and checkout/beta 60 overlap
    # a gold range, alpha 50-51 and noise 1-2 do not.
    assert scored["new"]["region_precision"] == 3 / 5

    # and the rest of the hand-computed region/line grid
    assert scored["new"]["region_recall"] == 3 / 5
    assert scored["new"]["line_recall"] == 4 / 12
    assert scored["new"]["line_precision"] == 4 / 9


def test_old_region_precision_scores_gold_ranges_not_gold_files():
    """The new arm is scored on range OVERLAP, so the old arm must be too."""
    sealed = _multi_file_sealed()
    sealed["legacy"]["candidate_order"] = ["src/alpha.py", "src/beta.py"]
    gold = dict(_MULTI_FILE_GOLD)
    gold["gold_line_ranges"] = [{"file": "src/alpha.py", "start": 10, "end": 12}]

    scored = score_sealed_case(sealed, gold)

    # precondition: both delivered files ARE gold files
    assert scored["old"]["file_recall"] == 2 / 3
    # but only src/alpha.py holds a gold line range
    assert scored["old"]["region_precision"] == 1 / 2


def test_legacy_symbol_precision_is_unscorable_not_a_prose_token_ratio():
    scored = score_sealed_case(_multi_file_sealed(), _MULTI_FILE_GOLD)

    # preconditions: the witness prose really carries both gold symbols, and
    # the new arm's parsed symbols really were scored
    assert scored["old"]["symbol_recall"] == 1.0
    assert scored["new"]["symbol_recall"] == 1 / 2
    assert scored["new"]["symbol_precision"] == 1 / 2

    # The legacy denominator was every identifier-like token scraped out of the
    # witness prose - {parse_alpha, calls, render_beta, CALLS} -> 0.5 - which
    # is text density, not symbol precision.  The legacy surface exposes no
    # parsed symbol set, so this arm is UNSCORABLE, never a comparable number.
    assert scored["old"]["symbol_precision"] is None


def test_symbol_precision_gate_aggregates_the_same_rows_on_both_arms():
    control = evaluate_winner(_corpus())
    assert control["verdict"] == "NEW_WINS"

    rows = _corpus()
    # the ONLY row where both arms measured symbol precision: new is far worse
    rows[0]["old"]["symbol_precision"] = 0.9
    rows[0]["new"]["symbol_precision"] = 0.1
    # a new-only row lifts the new mean; an old-only row drags the old mean
    # down.  Unpaired: old mean 0.5 vs new mean 0.55 -> "no regression".
    rows[1]["new"]["symbol_precision"] = 1.0
    rows[2]["old"]["symbol_precision"] = 0.1

    verdict = evaluate_winner(rows)

    # precondition: exactly one row is comparable at all
    assert verdict["symbol_precision"]["paired_cases"] == 1
    assert verdict["symbol_precision"] == {
        "old": 0.9,
        "new": 0.1,
        "paired_cases": 1,
    }
    assert verdict["verdict"] == "OLD_WINS"


def test_an_unmeasured_paired_metric_is_none_never_a_measured_zero():
    verdict = evaluate_winner(_corpus())

    assert verdict["verdict"] == "NEW_WINS"
    # no row measures symbol or region precision on either arm
    assert verdict["symbol_precision"] == {
        "old": None,
        "new": None,
        "paired_cases": 0,
    }
    assert verdict["region_precision"] == {
        "old": None,
        "new": None,
        "paired_cases": 0,
    }
    # the metrics that ARE measured still report the population they used
    assert verdict["symbol_recall"]["paired_cases"] == 15
    assert verdict["file_precision"]["paired_cases"] == 15


def test_sealed_input_digests_make_inter_run_drift_detectable(tmp_path):
    """Every input that can change the result must be hashed into the artifact.

    The legacy control arm moved between two runs on identical declared inputs
    (Hit@1 30 vs 32, legacy file list differing in 9/60 cases) because the frozen
    embedder went dark on 16/60 cases. Nothing in the sealed schema recorded it:
    the drift was only visible by diffing two runs against each other. An artifact
    that cannot reveal its own input drift cannot support a comparative claim.
    """
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    graph = tmp_path / "graph.db"
    graph.write_bytes(b"sqlite-ish-bytes")

    first = _input_digests(str(repo), str(graph))

    assert first["graph_db_sha256"], "graph.db is not hashed"
    assert first["graph_db_bytes"] == len(b"sqlite-ish-bytes")
    assert "embedder" in first

    # A content change that PRESERVES size must still change the digest - the
    # existing cache key uses (size, mtime) only, which coarse filesystem
    # timestamps can defeat.
    graph.write_bytes(b"sqlite-ish-BYTES")
    second = _input_digests(str(repo), str(graph))

    assert second["graph_db_sha256"] != first["graph_db_sha256"]
    assert second["graph_db_bytes"] == first["graph_db_bytes"]


def test_winner_gate_still_judges_retrieval_when_region_gold_is_absent():
    """A corpus without region gold must still get a retrieval verdict.

    `evaluate_winner` keyed a hard gate off a FIXED five-language tuple counted
    over region-scorable rows, and returned INCONCLUSIVE before computing
    anything. Verified against the real completed run 30196352388: 0/60 rows are
    region_scorable, so the gate short-circuited and never evaluated hit@1,
    hit@8, precision, latency or memory - on ANY run in the corpus's history. It
    is also structurally unsatisfiable on a monolingual corpus.

    Absent region gold, region metrics are UNMEASURED. Retrieval and safety are
    still perfectly measurable and must still be judged.
    """
    rows = _corpus()          # scorable, but region_scorable is False on every row
    for row in rows:
        row["region_scorable"] = False
        for side in ("old", "new"):
            row[side]["symbol_recall"] = None
            row[side]["line_recall"] = None

    verdict = evaluate_winner(rows)

    assert verdict["verdict"] != "INCONCLUSIVE", (
        "the gate refused to judge retrieval because region gold was absent; "
        f"reason={verdict.get('reason')}"
    )
    assert "random_primary_hit_at_1" in verdict, "hit@1 was never computed"
    # ... and the region metrics must read UNMEASURED, never a measured tie.
    assert verdict["symbol_recall"]["old"] is None
    assert verdict["symbol_recall"]["new"] is None
