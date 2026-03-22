"""GT-enabled SWE-bench eval task for Inspect AI.

Identical to the standard swe_bench task from inspect_evals EXCEPT:
- Adds gt_impact, gt_references, gt_check to the agent's tool list
- Adds a short system prompt explaining the GT tools
- Logs all GT tool calls to a JSONL file for observability

Usage:
    inspect eval scripts/swebench/gt_swebench_task.py \
        --model openai/qwen3-coder \
        --max-connections 4 \
        --message-limit 100 \
        --temperature 0.7 \
        --top-p 0.8

    # Or with sample selection:
    inspect eval scripts/swebench/gt_swebench_task.py \
        --model openai/qwen3-coder \
        --sample-id django__django-11333,sympy__sympy-13031
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

# Import GT tools
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
from gt_inspect_tools import gt_impact, gt_references, gt_check


GT_SYSTEM_ADDENDUM = dedent("""\

    You have access to GroundTruth codebase intelligence tools in addition to
    your standard tools. These help you understand code structure before making
    changes:

    - gt_impact(symbol): Shows what methods and sites share state with a symbol.
      Call this BEFORE editing a class to see all obligation sites you must cover.
    - gt_references(symbol): Shows where a symbol is defined and all files that
      use it. Faster than grep for finding all dependents.
    - gt_check(): After making edits, verifies your patch covers all obligation
      sites. Shows which sites you modified and which you missed.

    Use these tools when working on changes that affect class hierarchies,
    shared state, or cross-file dependencies. They are optional but help you
    write more complete patches.
""")


@task
def gt_swe_bench(
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
    """SWE-bench eval with GroundTruth live tools.

    Identical to standard swe_bench except the agent has gt_impact,
    gt_references, and gt_check tools available alongside bash, python,
    and text_editor.
    """
    # Build the base task using the standard swe_bench function
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

    # Replace the solver with our GT-enhanced agent
    PROMPT = dedent("""\
    You are an expert software engineer, and you are interacting with a
    standard Ubuntu machine with bash commands and python tools.
    You will be given an issue to fix.
    Your objective is to modify the code on the file system to fix the issue.
    The repository code is already checked out to the current working directory.
    You do NOT need to change branches or commit the fix.
    Once you are done, use your submit tool.
    """) + GT_SYSTEM_ADDENDUM

    gt_agent = react(
        description="Software engineering agent with GT codebase intelligence tools",
        prompt=PROMPT,
        tools=[
            python(timeout=210),
            bash_session(timeout=210),
            text_editor(timeout=210),
            gt_impact(),
            gt_references(),
            gt_check(),
        ],
    )

    base_task.solver = gt_agent
    return base_task
