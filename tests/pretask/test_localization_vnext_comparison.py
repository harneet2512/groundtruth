from __future__ import annotations

from groundtruth.pretask.localization_vnext.comparison import (
    _legacy_inspection_files,
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


def test_fewer_than_three_region_scorable_cases_in_a_language_is_inconclusive():
    rows = _corpus()
    rows = [
        row
        for index, row in enumerate(rows)
        if not (row["language"] == "rust" and index % 3 == 2)
    ]
    assert evaluate_winner(rows)["verdict"] == "INCONCLUSIVE"


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
