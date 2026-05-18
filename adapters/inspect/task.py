"""Inspect AI Task definitions for SWE-bench evaluation with GroundTruth.

Uses the official inspect_evals.swe_bench implementation for Docker sandbox
management, dataset loading, and evaluation. Adds GT tools on top.

Baseline: `inspect eval adapters/inspect/task.py@swebench_gt_baseline`
GT:       `inspect eval adapters/inspect/task.py@swebench_gt`
"""

from __future__ import annotations

import json

from inspect_ai import Task, task
from inspect_ai.agent import react
from inspect_ai.model import GenerateConfig
from inspect_ai.scorer import Score, Target, accuracy, scorer
from inspect_ai.solver import TaskState
from inspect_ai.tool import bash_session, python, text_editor
from inspect_evals.swe_bench import swe_bench as _official_swe_bench


@scorer(metrics=[accuracy()])
def _passthrough_scorer():
    """Score by checking if the agent produced a non-empty patch.

    SWE-bench-Live repos are NOT in inspect_evals' MAP_REPO_VERSION_TO_SPECS,
    so the built-in swe_bench_scorer() throws KeyError. This scorer bypasses
    the spec lookup and just records whether a patch was created.
    """

    async def score(state: TaskState, target: Target) -> Score:
        from inspect_ai.util import sandbox

        try:
            stat_result = await sandbox().exec(
                ["git", "diff", "--stat"], cwd="/testbed", timeout=30
            )
            diff_stat = stat_result.stdout.strip()
            has_patch = len(diff_stat) > 0

            full_diff = ""
            if has_patch:
                diff_result = await sandbox().exec(
                    ["git", "diff"], cwd="/testbed", timeout=30
                )
                full_diff = diff_result.stdout.strip()

            return Score(
                value="C" if has_patch else "I",
                answer=full_diff[:50000] if has_patch else "no changes",
                explanation=diff_stat if has_patch else "no patch",
                metadata={"full_diff": full_diff, "diff_stat": diff_stat},
            )
        except Exception as exc:
            return Score(value="I", answer="", explanation=f"scorer error: {exc}")

    return score


def _patched_swe_bench(**kwargs):
    """Wrap official swe_bench to handle SWE-bench-Live's pre-parsed list fields."""
    _orig_json_loads = json.loads

    def _safe_json_loads(s, *args, **kw):
        if isinstance(s, list):
            return s
        return _orig_json_loads(s, *args, **kw)

    json.loads = _safe_json_loads
    try:
        return _official_swe_bench(**kwargs)
    finally:
        json.loads = _orig_json_loads


def _filter_dataset(result: Task, task_ids: str) -> Task:
    """Filter a Task's dataset to only include specified sample IDs."""
    if not task_ids:
        return result
    ids = json.loads(task_ids) if task_ids.startswith("[") else [task_ids]
    id_set = set(ids)
    result.dataset = [s for s in result.dataset if s.id in id_set]
    return result


def _starryzhang_image(instance_id: str, arch: str = "x86_64") -> str:
    """Convert instance_id to starryzhang DockerHub image name.

    beancount__beancount-931 -> starryzhang/sweb.eval.x86_64.beancount_1776_beancount-931
    """
    parts = instance_id.split("__", 1)
    if len(parts) == 2:
        return f"docker.io/starryzhang/sweb.eval.{arch}.{parts[0]}_1776_{parts[1]}:latest"
    return f"docker.io/starryzhang/sweb.eval.{arch}.{instance_id}:latest"


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


_TOOL_TIMEOUT = 210

GT_SYSTEM_PROMPT = """\
You have access to 4 GroundTruth codebase intelligence tools that query a \
pre-built code graph. They are instant and free (no LLM calls).

BEFORE editing any file, call groundtruth_brief on it to understand its \
callers, callees, contracts, and high-impact symbols.
Use groundtruth_trace to find callers/callees before changing function signatures.
Use groundtruth_impact to assess blast radius before modifying high-impact functions.
Use groundtruth_validate after making changes to check for broken imports or caller-blind edits."""


@task
def swebench_gt_baseline(
    task_ids: str = "",
    max_messages: int = 100,
) -> Task:
    """SWE-bench baseline WITHOUT GroundTruth tools.

    Uses official inspect_evals swe_bench with starryzhang DockerHub images.
    """
    result = _patched_swe_bench(
        dataset="SWE-bench-Live/SWE-bench-Live",
        split="lite",
        revision="main",
        image_name_template="docker.io/starryzhang/sweb.eval.{arch}.{org}_1776_{repo}-{issue}:latest",
        config=GenerateConfig(
            max_tokens=65536,
            temperature=1.0,
            top_p=1.0,
            extra_body={"thinking": {"type": "disabled"}},
        ),
        scorer=_passthrough_scorer(),
    )
    return _filter_dataset(result, task_ids)


@task
def swebench_gt(
    task_ids: str = "",
    max_messages: int = 100,
) -> Task:
    """SWE-bench evaluation WITH GroundTruth tools.

    Replaces the default solver with a react agent that includes 6 GT tools
    alongside the standard python/bash/text_editor tools, plus a system prompt
    instructing the agent to call GT tools before editing.
    """
    from adapters.inspect.tools import gt_tools

    result = _patched_swe_bench(
        dataset="SWE-bench-Live/SWE-bench-Live",
        split="lite",
        revision="main",
        image_name_template="docker.io/starryzhang/sweb.eval.{arch}.{org}_1776_{repo}-{issue}:latest",
        config=GenerateConfig(
            max_tokens=65536,
            temperature=1.0,
            top_p=1.0,
            extra_body={"thinking": {"type": "disabled"}},
        ),
        scorer=_passthrough_scorer(),
    )
    result.solver = react(
        prompt=GT_SYSTEM_PROMPT,
        tools=[
            python(timeout=_TOOL_TIMEOUT),
            bash_session(timeout=_TOOL_TIMEOUT),
            text_editor(timeout=_TOOL_TIMEOUT),
            *gt_tools(),
        ],
    )
    return _filter_dataset(result, task_ids)
