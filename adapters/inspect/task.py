"""Inspect AI Task definitions for SWE-bench evaluation with GroundTruth.

Uses the official inspect_evals.swe_bench implementation for Docker sandbox
management, dataset loading, and evaluation. Adds GT tools on top.

Baseline: `inspect eval adapters/inspect/task.py@swebench_gt_baseline`
GT:       `inspect eval adapters/inspect/task.py@swebench_gt`
"""

from __future__ import annotations

import json
import re

from inspect_ai import Task, task
from inspect_ai.model import GenerateConfig
from inspect_evals.swe_bench import swe_bench as _official_swe_bench


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


@task
def swebench_gt_baseline(
    task_ids: str = "",
    max_messages: int = 100,
) -> Task:
    """SWE-bench baseline WITHOUT GroundTruth tools.

    Uses official inspect_evals swe_bench with starryzhang DockerHub images.
    """
    return _patched_swe_bench(
        dataset="SWE-bench-Live/SWE-bench-Live",
        split="lite",
        revision="main",
        image_name_template="docker.io/starryzhang/sweb.eval.{arch}.{org}_1776_{repo}-{issue}:latest",
        config=GenerateConfig(
            max_tokens=65536,
            temperature=1.0,
            top_p=1.0,
        ),
    )


@task
def swebench_gt(
    task_ids: str = "",
    max_messages: int = 100,
) -> Task:
    """SWE-bench evaluation WITH GroundTruth tools."""
    from adapters.inspect.tools import gt_tools

    return _patched_swe_bench(
        dataset="SWE-bench-Live/SWE-bench-Live",
        split="lite",
        revision="main",
        image_name_template="docker.io/starryzhang/sweb.eval.{arch}.{org}_1776_{repo}-{issue}:latest",
        config=GenerateConfig(
            max_tokens=65536,
            temperature=1.0,
            top_p=1.0,
        ),
        tools=gt_tools(),
    )
