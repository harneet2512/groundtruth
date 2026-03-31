"""GT check-only SWE-bench eval task for Inspect AI.

Identical to standard swe_bench EXCEPT:
- Adds ONLY gt_check to the agent's tool list (no gt_impact, no gt_references)
- gt_check has v2 fixes: live re-indexing, noise suppression, hard blocker format
- Short system prompt addendum explaining gt_check's purpose
- No call caps — model self-regulates

Usage:
    inspect eval scripts/swebench/gt_check_only_task.py \
        --model openai/gpt-4.1-mini \
        --max-connections 4 \
        --message-limit 100 \
        --temperature 0.7 \
        --top-p 0.8
"""

from textwrap import dedent

from inspect_ai import task, Task
from inspect_ai.agent import react
from inspect_ai.scorer import Scorer
from inspect_ai.tool import bash_session, python, text_editor

from inspect_evals.swe_bench import swe_bench_scorer
from inspect_evals.swe_bench.swe_bench import (
    DEFAULT_IMAGE_TEMPLATE,
    DEFAULT_INPUT_PROMPT,
    SWE_BENCH_VERIFIED_REVISION,
    swe_bench,
)

# Import only gt_check from GT tools
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
from gt_inspect_tools import gt_check


GT_CHECK_ADDENDUM = dedent("""\

    You have access to gt_check, a structural patch validator. After making edits,
    call gt_check to verify your patch for structural completeness — it detects
    missing coupled changes, broken contracts, and unupdated callers that you cannot
    find by reading individual files. If gt_check reports STRUCTURAL VIOLATIONS, you
    must fix them before submitting. You may call gt_check multiple times as you
    make additional changes.
""")


@task
def gt_check_only(
    dataset: str = "princeton-nlp/SWE-bench_Verified",
    split: str = "test",
    input_prompt: str = DEFAULT_INPUT_PROMPT,
    scorer: Scorer | list[Scorer] | None = None,
    sandbox_type: str = "docker",
    image_name_template: str = DEFAULT_IMAGE_TEMPLATE,
    arch: str | None = None,
    sandbox_config: object | None = None,
    allow_internet: bool = False,
    revision: str = SWE_BENCH_VERIFIED_REVISION,
    **kwargs,
) -> Task:
    """SWE-bench eval with gt_check as the only additional tool.

    Baseline tools (bash, python, text_editor) plus gt_check with v2 fixes.
    No gt_impact, no gt_references.
    """
    base_task = swe_bench(
        dataset=dataset,
        split=split,
        input_prompt=input_prompt,
        scorer=scorer,
        sandbox_type=sandbox_type,
        image_name_template=image_name_template,
        arch=arch,
        sandbox_config=sandbox_config,
        allow_internet=allow_internet,
        revision=revision,
        **kwargs,
    )

    PROMPT = dedent("""\
    You are an expert software engineer, and you are interacting with a
    standard Ubuntu machine with bash commands and python tools.
    You will be given an issue to fix.
    Your objective is to modify the code on the file system to fix the issue.
    The repository code is already checked out to the current working directory.
    You do NOT need to change branches or commit the fix.
    Once you are done, use your submit tool.
    """) + GT_CHECK_ADDENDUM

    gt_agent = react(
        description="Software engineering agent with gt_check structural validator",
        prompt=PROMPT,
        tools=[
            python(timeout=210),
            bash_session(timeout=210),
            text_editor(timeout=210),
            gt_check(),
        ],
    )

    base_task.solver = gt_agent
    return base_task
