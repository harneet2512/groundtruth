"""GT-enabled SWE-bench eval task V2 — with call cap and sharper prompts.

V2 changes:
1. Call cap: max 3 GT calls per task (prevents overuse that hurts resolve rate)
2. Pre-indexing: repo indexed on first GT call (not during agent interaction)
3. Sharper system prompt: explicit "call ONCE" guidance
4. Tighter output: 2000 char cap (saves context budget)

Evidence from V1 run (80 tasks):
- 1-3 GT calls: 37.3% resolve rate (beats 35% baseline)
- 5+ GT calls: 13.3% resolve rate (catastrophic)
- Fix: cap at 3, guide agent to use strategically

Usage:
    inspect eval scripts/swebench/gt_swebench_task_v2.py \
        --model openai/gpt-4.1-mini \
        --max-connections 2 \
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

# Import V2 GT tools (with call cap + pre-indexing)
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
from gt_inspect_tools_v2 import gt_impact, gt_references, gt_check


# V2: Much sharper prompt — explicit call-once guidance
GT_SYSTEM_ADDENDUM_V2 = dedent("""\

    You have 3 GroundTruth codebase intelligence tools. They are powerful but
    LIMITED TO 3 TOTAL CALLS per task. Use them strategically:

    WORKFLOW:
    1. Read the issue. Identify the key class/function to modify.
    2. Call gt_impact(symbol) ONCE to see all obligation sites and shared state.
    3. Make your code changes using bash and text_editor.
    4. Call gt_check() ONCE to verify your patch is complete.

    RULES:
    - You have a MAXIMUM of 3 GT tool calls total. After 3, they return guidance
      to proceed without further analysis.
    - Call gt_impact ONCE before editing to understand coupling.
    - Call gt_check ONCE after editing to verify completeness.
    - Use gt_references only if you need to find where a symbol is used.
    - Do NOT call GT tools repeatedly. Use bash grep for follow-up searches.
    - Do NOT call gt_check before making edits — it needs a git diff to analyze.

    These tools index the full codebase and show structural relationships that
    grep cannot find (shared state, obligation sites, class conventions).
""")


@task
def gt_swe_bench_v2(
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
    """SWE-bench eval with GroundTruth V2 — call cap + sharper prompts."""
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
    """) + GT_SYSTEM_ADDENDUM_V2

    gt_agent = react(
        description="Software engineering agent with GT V2 tools (capped at 3 calls)",
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
