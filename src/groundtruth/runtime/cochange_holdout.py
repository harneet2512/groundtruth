"""cochange_holdout — preregistered cochange-on / cochange-holdout ablation.

Mirrors :mod:`shadow_holdout` but scopes ONLY the cochange influence contribution
on a localization candidate (``cochange_evidence`` / ``components.cochange`` /
``Also changes`` support), never the whole localization FACT.

Default rate ``0`` -> always DELIVER (byte-identical production). Opt-in via
``GT_COCHANGE_HOLDOUT_RATE`` + ``GT_COCHANGE_HOLDOUT_SEED`` for a paid causal cohort.

PURE · DETERMINISTIC · STDLIB-ONLY.
"""
from __future__ import annotations

import hashlib
import os

__all__ = [
    "DELIVER",
    "HOLDOUT",
    "assign",
    "parse_rate",
    "configured_rate",
    "task_seed",
]

DELIVER = "DELIVER"
HOLDOUT = "HOLDOUT"


def parse_rate(raw: object) -> float:
    try:
        rate = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    if rate != rate or rate <= 0.0:  # NaN / non-positive
        return 0.0
    return 1.0 if rate >= 1.0 else rate


def configured_rate() -> float:
    return parse_rate(os.environ.get("GT_COCHANGE_HOLDOUT_RATE", "0"))


def task_seed() -> str:
    """Stable per-task seed for the deterministic holdout draw."""
    seed = os.environ.get("GT_COCHANGE_HOLDOUT_SEED", "")
    task = (
        os.environ.get("GT_TASK_ID")
        or os.environ.get("SWEBENCH_INSTANCE_ID")
        or os.environ.get("INSTANCE_ID")
        or ""
    )
    return f"{seed}|{task}"


def assign(*, task_id: str, candidate_id: str, rate: float | None = None) -> str:
    """Map ``(task, candidate)`` to DELIVER / HOLDOUT via sha256 bucket.

    ``rate <= 0`` (default) always DELIVER. Deterministic: same inputs -> same arm.
    """
    holdout_rate = configured_rate() if rate is None else parse_rate(rate)
    if holdout_rate <= 0.0:
        return DELIVER
    cid = str(candidate_id or "").strip()
    tid = str(task_id or "").strip()
    if not cid:
        return DELIVER
    digest = hashlib.sha256(
        f"{tid}|cochange_prior|{cid}".encode("utf-8")
    ).hexdigest()
    bucket = int(digest[:8], 16) / 0xFFFFFFFF
    return HOLDOUT if bucket < holdout_rate else DELIVER
