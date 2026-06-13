"""Single-generation brief cache (A1, 2026-06-13) — generate the v1r brief ONCE per proof.

The substrate proof generated the brief TWICE: gate3b (``foundational_gates``, a
SEPARATE subprocess that runs FIRST) called ``generate_v1r_brief`` to MEASURE the
embedder-consumption metrics, and ``emit_brief`` (``gt_run_proof``, later) called it
AGAIN to WRITE ``brief.txt``. Two generations across a process boundary mean the
gate-certified brief can DIVERGE from the delivered brief (any nondeterminism), and
the most expensive proof stage runs twice.

This module shares one ``V1RBriefResult`` across the process boundary via a small
on-disk artifact (``<out_dir>/brief_result.json``): the FIRST caller (gate3b)
generates + persists; the SECOND (emit_brief) loads it. Result: ONE generation, and
the certified brief text + sha == the delivered ``brief.txt`` by construction.

FAIL-SAFE: any cache miss / read error / write error falls back to generating
(``generated=True``) — it degrades to the prior double-generation, it NEVER breaks
the proof or blocks ``brief.txt``. So this is a pure optimization with a safety net.
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Optional

BRIEF_CACHE_BASENAME = "brief_result.json"
BRIEF_RESULT_SCHEMA = "gt.brief_result.v1"
_METRIC_FIELDS = (
    "effective_w_sem", "semantic_signal_count",
    "rendered_candidate_count", "k_sem_top", "sem_components",
)


def cache_path(out_dir: str) -> str:
    return os.path.join(out_dir or "", BRIEF_CACHE_BASENAME)


def brief_sha256(text: str) -> str:
    """Canonical brief hash — sha256 of the STRIPPED brief text (the exact bytes
    written to brief.txt and consumed by the agent)."""
    return hashlib.sha256((text or "").strip().encode("utf-8")).hexdigest()


def _extract_metrics(obj: Any) -> dict:
    if obj is None:
        return {}

    def _get(k: str):
        return obj.get(k) if isinstance(obj, dict) else getattr(obj, k, None)

    out = {}
    for k in _METRIC_FIELDS:
        v = _get(k)
        # sem_components may be a non-JSON-able object; keep only JSON-safe scalars/lists.
        if k == "sem_components" and v is not None and not isinstance(v, (list, dict, int, float, str, bool)):
            try:
                v = list(v)
            except TypeError:
                v = None
        out[k] = v
    return out


def load_cached_brief(out_dir: str) -> Optional[dict]:
    """Return the persisted ``{schema, brief_text, brief_sha256, metrics}`` dict, or
    None on any miss/error (fail-safe — the caller then regenerates)."""
    p = cache_path(out_dir)
    try:
        if os.path.isfile(p):
            with open(p, encoding="utf-8") as fh:
                d = json.load(fh)
            if isinstance(d, dict) and (d.get("brief_text") or "").strip():
                return d
    except (OSError, ValueError):
        pass
    return None


def persist_brief(out_dir: str, brief_text: str, result_obj: Any = None) -> dict:
    """Best-effort persist of the brief text + sha + metrics for cross-process reuse.
    Returns the dict regardless of whether the write succeeded."""
    text = (brief_text or "").strip()
    d = {
        "schema": BRIEF_RESULT_SCHEMA,
        "brief_text": text,
        "brief_sha256": brief_sha256(text),
        "metrics": _extract_metrics(result_obj),
    }
    try:
        with open(cache_path(out_dir), "w", encoding="utf-8") as fh:
            json.dump(d, fh)
    except (OSError, TypeError):
        pass
    return d


def get_or_generate(out_dir: str, issue_text: str, work: str, graph: str,
                    generator: Any = None) -> dict:
    """Single-generation entry point. Returns
    ``{schema, brief_text, brief_sha256, metrics, generated: bool}``.

    Loads the persisted result if present (``generated=False`` — no second
    generation); otherwise generates via ``generator`` (default the real
    ``generate_v1r_brief``), persists it, and returns (``generated=True``).
    Raising is left to the caller's contract — ``generator`` exceptions propagate so
    the proof can fail closed on a brief that cannot be produced."""
    cached = load_cached_brief(out_dir)
    if cached is not None:
        out = dict(cached)
        out["generated"] = False
        return out
    if generator is None:
        from groundtruth.pretask.v1r_brief import generate_v1r_brief as generator
    res = generator(issue_text=issue_text, repo_root=work, graph_db=graph, bug_id="portable")
    brief_text = (getattr(res, "brief_text", "") or "").strip()
    out = persist_brief(out_dir, brief_text, res)
    out["generated"] = True
    return out
