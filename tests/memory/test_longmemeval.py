"""Phase 5 tests: LongMemEval harness validation.

Tests the evaluation infrastructure, not the benchmark results.
Runs on a synthetic mini-dataset to verify the pipeline works end-to-end.
"""

from __future__ import annotations

import json
import os
import pytest

from groundtruth.memory.config import MemoryConfig
from groundtruth.memory.db.store import MemoryStore
from groundtruth.utils.result import Ok


# ---- Prompts ----

def test_prompts_cover_all_categories() -> None:
    from evals.longmemeval.prompts import CATEGORY_PROMPTS, get_prompt

    expected = [
        "single-session-user",
        "single-session-assistant",
        "single-session-preference",
        "multi-session",
        "knowledge-update",
        "temporal-reasoning",
    ]
    for cat in expected:
        assert cat in CATEGORY_PROMPTS, f"Missing prompt for {cat}"
        prompt = get_prompt(cat, "test context", "test question")
        assert "test context" in prompt
        assert "test question" in prompt


# ---- Batch runner ----

@pytest.mark.asyncio
async def test_batch_runner_processes_synthetic_question() -> None:
    """Run the batch runner on a single synthetic question."""
    from evals.longmemeval.batch_runner import run_question, RunConfig

    # Synthetic question matching LongMemEval structure
    item = {
        "question_id": "test001",
        "question_type": "single-session-user",
        "question": "What programming language did I mention?",
        "answer": "Python",
        "question_date": "2023/05/30 (Tue) 23:40",
        "haystack_dates": ["2023/05/01", "2023/05/15"],
        "haystack_session_ids": ["s1", "s2"],
        "haystack_sessions": [
            [
                {"role": "user", "content": "I've been learning Python for data science."},
                {"role": "assistant", "content": "Python is a great choice for data science!"},
            ],
            [
                {"role": "user", "content": "Can you help me with a math problem?"},
                {"role": "assistant", "content": "Of course! What's the problem?"},
            ],
        ],
        "answer_session_ids": ["s1"],
    }

    config = RunConfig(
        db_path=":memory:",
        llm_api_key="",  # No LLM — hypothesis will be context
        max_questions=1,
    )

    result = await run_question(item, config)

    assert result.question_id == "test001"
    assert result.question_type == "single-session-user"
    assert result.sessions_ingested == 2
    assert result.context_items >= 0
    assert result.ingest_time_ms >= 0
    assert result.total_time_ms >= 0


# ---- Report ----

def test_report_print_summary() -> None:
    """print_summary should handle result dicts without crashing."""
    from evals.longmemeval.report import print_summary

    results = [
        {
            "question_id": "q1",
            "question_type": "single-session-user",
            "context_items": 3,
            "total_time_ms": 100,
        },
        {
            "question_id": "q2",
            "question_type": "knowledge-update",
            "context_items": 5,
            "total_time_ms": 200,
        },
    ]
    # Should not raise
    print_summary(results)


# ---- Dataset exists ----

def test_longmemeval_dataset_downloaded() -> None:
    """LongMemEval_S dataset should be present."""
    path = os.path.join(os.path.dirname(__file__), "..", "..", "evals", "longmemeval", "data", "longmemeval_s")
    assert os.path.exists(path), f"Dataset not found at {path}. Run the download step."

    # Verify file exists and has reasonable size (don't load full 278MB in test)
    size = os.path.getsize(path)
    assert size > 1_000_000, f"Dataset too small: {size} bytes"

    # Read just the first few bytes to verify it's valid JSON array
    with open(path, encoding="utf-8") as f:
        header = f.read(100)
    assert header.startswith("["), "Dataset should be a JSON array"
    assert '"question_id"' in header or '"question_type"' in header


# ---- Ablation configs ----

def test_ablation_configs_work() -> None:
    """All three ablation modes should produce valid MemoryConfig."""
    modes = [
        ("ITEM_ONLY", False, False),
        ("CLAIM_NO_SUPERSESSION", True, False),
        ("CLAIM_WITH_SUPERSESSION", True, True),
    ]
    for mode, claims, supersession in modes:
        cfg = MemoryConfig(
            retrieval_mode=mode,
            enable_claims=claims,
            enable_supersession=supersession,
        )
        assert cfg.retrieval_mode == mode
        assert cfg.enable_claims == claims
        assert cfg.enable_supersession == supersession
