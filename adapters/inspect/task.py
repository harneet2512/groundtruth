"""Inspect AI Task definition for SWE-bench evaluation with GroundTruth.

Defines the eval task that loads SWE-bench-Live Lite, configures an agent
with bash + text_editor + GT tools, and runs in a Docker sandbox.
"""

from __future__ import annotations

from inspect_ai import Task, task
from inspect_ai.dataset import hf_dataset, Sample
from inspect_ai.scorer import includes
from inspect_ai.solver import generate, system_message, use_tools
from inspect_ai.tool import bash, text_editor
from inspect_ai.model import GenerateConfig

from adapters.inspect.tools import gt_tools
from adapters.inspect.hooks import on_sample_init, on_sample_end


# ---------------------------------------------------------------------------
# SWE-bench dataset loading
# ---------------------------------------------------------------------------

# The 30 SWE-bench-Live Lite task IDs used for evaluation.
# Composed from the project's existing task pools across workflows.
SWEBENCH_LIVE_LITE_30 = [
    "beancount__beancount-931",
    "beetbox__beets-5495",
    "delgan__loguru-1297",
    "delgan__loguru-1306",
    "flexget__flexget-4306",
    "flexget__flexget-4244",
    "kozea__weasyprint-2300",
    "kozea__weasyprint-2387",
    "kozea__weasyprint-2405",
    "kozea__weasyprint-2398",
    "kozea__weasyprint-2303",
    "pypsa__pypsa-1172",
    "pypsa__pypsa-1112",
    "pypsa__pypsa-1091",
    "pypsa__pypsa-1195",
    "aiogram__aiogram-1594",
    "amoffat__sh-744",
    "arviz-devs__arviz-2413",
    "aws-cloudformation__cfn-lint-3875",
    "aws-cloudformation__cfn-lint-3890",
    "aws-cloudformation__cfn-lint-3855",
    "aws-cloudformation__cfn-lint-4023",
    "dulwich__dulwich-1399",
    "dulwich__dulwich-1423",
    "fal-ai__dbt-fal-842",
    "getmoto__moto-8271",
    "getmoto__moto-8301",
    "graphql-python__graphene-1565",
    "jd__tenacity-482",
    "pre-commit__pre-commit-3584",
]


def _record_to_sample(record: dict) -> Sample:
    """Convert a HuggingFace dataset record to an Inspect Sample."""
    instance_id = record.get("instance_id", "")
    problem_statement = record.get("problem_statement", "")
    repo = record.get("repo", "")
    base_commit = record.get("base_commit", "")

    prompt = (
        f"You are solving a software engineering task.\n\n"
        f"## Repository\n{repo}\n\n"
        f"## Base commit\n{base_commit}\n\n"
        f"## Problem Statement\n{problem_statement}\n\n"
        f"## Instructions\n"
        f"1. Navigate the repository and understand the codebase structure.\n"
        f"2. Use GroundTruth tools (groundtruth_brief, groundtruth_trace, etc.) "
        f"to understand symbol relationships before making changes.\n"
        f"3. Identify the root cause of the issue.\n"
        f"4. Implement a fix that resolves the issue without breaking existing tests.\n"
        f"5. Verify your fix is correct.\n\n"
        f"When done, your changes will be evaluated against the test suite."
    )

    return Sample(
        input=prompt,
        id=instance_id,
        metadata={
            "instance_id": instance_id,
            "repo": repo,
            "base_commit": base_commit,
            "problem_statement": problem_statement,
            "FAIL_TO_PASS": record.get("FAIL_TO_PASS", ""),
            "PASS_TO_PASS": record.get("PASS_TO_PASS", ""),
            "test_patch": record.get("test_patch", ""),
            "patch": record.get("patch", ""),
        },
    )


def load_swebench_dataset(
    task_ids: list[str] | None = None,
    dataset_name: str = "SWE-bench-Live/SWE-bench-Live",
    split: str = "lite",
) -> list[Sample]:
    """Load SWE-bench-Live dataset, optionally filtered to specific task IDs.

    Args:
        task_ids: List of instance IDs to include. None = all.
        dataset_name: HuggingFace dataset name.
        split: Dataset split.

    Returns:
        List of Inspect Samples.
    """
    dataset = hf_dataset(
        dataset_name,
        split=split,
        sample_fields=_record_to_sample,
    )

    if task_ids is not None:
        task_id_set = set(task_ids)
        dataset = [s for s in dataset if s.id in task_id_set]

    return dataset


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

GT_SYSTEM_PROMPT = """\
You are an expert software engineer solving a coding task. You have access to \
bash, a text editor, and GroundTruth codebase intelligence tools.

## GroundTruth Tools
- **groundtruth_brief**: Get a pre-edit briefing for a file. Use BEFORE editing \
to understand contracts, callers, and high-impact symbols.
- **groundtruth_trace**: Trace callers/callees of a symbol through the call graph.
- **groundtruth_validate**: Validate proposed code against the codebase index.
- **groundtruth_hotspots**: Find the most-referenced symbols (biggest blast radius).
- **groundtruth_impact**: Assess blast radius of modifying a symbol.
- **groundtruth_symbols**: List all symbols defined in a file.

## Workflow
1. Read the problem statement carefully.
2. Explore the repo structure with bash (find, grep, cat).
3. Use groundtruth_symbols and groundtruth_brief to understand relevant files.
4. Use groundtruth_trace or groundtruth_impact before modifying high-usage symbols.
5. Make targeted, minimal fixes.
6. Run the relevant tests to verify your fix.
"""


# ---------------------------------------------------------------------------
# Task definitions
# ---------------------------------------------------------------------------


@task
def swebench_gt(
    task_ids: list[str] | None = None,
    max_messages: int = 100,
) -> Task:
    """SWE-bench evaluation task with GroundTruth tools.

    Args:
        task_ids: Specific task IDs to evaluate. None = all 30.
        max_messages: Maximum agent messages before stopping.

    Returns:
        Inspect Task configured for SWE-bench + GT evaluation.
    """
    if task_ids is None:
        task_ids = SWEBENCH_LIVE_LITE_30

    dataset = load_swebench_dataset(task_ids=task_ids)

    return Task(
        dataset=dataset,
        solver=[
            system_message(GT_SYSTEM_PROMPT),
            use_tools([bash(), text_editor()] + gt_tools()),
            generate(),
        ],
        scorer=includes(),
        max_messages=max_messages,
        sandbox="docker",
        config=GenerateConfig(
            max_tokens=65536,
            temperature=1.0,
            top_p=1.0,
        ),
        setup=on_sample_init,
        cleanup=on_sample_end,
    )


@task
def swebench_baseline(
    task_ids: list[str] | None = None,
    max_messages: int = 100,
) -> Task:
    """SWE-bench evaluation task WITHOUT GroundTruth tools (baseline).

    Args:
        task_ids: Specific task IDs to evaluate. None = all 30.
        max_messages: Maximum agent messages before stopping.

    Returns:
        Inspect Task configured for baseline SWE-bench evaluation.
    """
    if task_ids is None:
        task_ids = SWEBENCH_LIVE_LITE_30

    dataset = load_swebench_dataset(task_ids=task_ids)

    return Task(
        dataset=dataset,
        solver=[
            system_message(
                "You are an expert software engineer solving a coding task. "
                "You have access to bash and a text editor."
            ),
            use_tools([bash(), text_editor()]),
            generate(),
        ],
        scorer=includes(),
        max_messages=max_messages,
        sandbox="docker",
        config=GenerateConfig(
            max_tokens=65536,
            temperature=1.0,
            top_p=1.0,
        ),
    )
