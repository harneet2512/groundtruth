"""Sealed old/new localization comparison and recall-first winner gate."""
from __future__ import annotations

import hashlib
import json
import os
import re
import statistics
import tempfile
import threading
import time
import tracemalloc
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .engine import localize_vnext
from .model import CandidateAction, LocalizationPolicy, LocalizationRequest
from .shadow import legacy_discoveries_from_projection


_EXPECTED_LANGUAGES = ("python", "go", "javascript", "typescript", "rust")
_LANGUAGE_ALIASES = {
    "py": "python",
    "golang": "go",
    "js": "javascript",
    "jsx": "javascript",
    "ts": "typescript",
    "tsx": "typescript",
}


class _PeakRssSampler:
    """Best-effort process RSS sampler with an explicit fallback method."""

    def __init__(self) -> None:
        self.peak_bytes = 0
        self.method = "unavailable"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        try:
            import psutil

            self._process = psutil.Process()
            self.method = "psutil_process_rss_10ms"
        except (ImportError, OSError):
            self._process = None

    def __enter__(self) -> "_PeakRssSampler":
        if self._process is None:
            return self
        process = self._process

        def sample() -> None:
            while not self._stop.wait(0.01):
                try:
                    self.peak_bytes = max(
                        self.peak_bytes,
                        int(process.memory_info().rss),
                    )
                except (OSError, AttributeError):
                    return

        try:
            self.peak_bytes = int(self._process.memory_info().rss)
        except (OSError, AttributeError):
            self.method = "unavailable"
            return self
        self._thread = threading.Thread(target=sample, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=0.1)
        if self._process is not None:
            try:
                self.peak_bytes = max(
                    self.peak_bytes,
                    int(self._process.memory_info().rss),
                )
            except (OSError, AttributeError):
                pass


def _norm(path: str) -> str:
    normalized = (path or "").replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.lstrip("/")


def _normalize_language(language: str) -> str:
    normalized = (language or "unknown").strip().lower()
    return _LANGUAGE_ALIASES.get(normalized, normalized)


def _percentile(values: Sequence[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * q
    lower = int(position)
    upper = min(len(ordered) - 1, lower + 1)
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _mean_bool(rows: Sequence[Mapping[str, Any]], side: str, key: str) -> float:
    values = [1.0 if bool(row[side].get(key)) else 0.0 for row in rows]
    return statistics.fmean(values) if values else 0.0


def _mean_numeric(rows: Sequence[Mapping[str, Any]], side: str, key: str) -> float:
    values = [
        float(row[side][key])
        for row in rows
        if row[side].get(key) is not None
    ]
    return statistics.fmean(values) if values else 0.0


def evaluate_winner(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Apply the user-pinned recall-first Pareto rule to paired task rows."""
    safety_failures = [
        index
        for index, row in enumerate(rows)
        if not bool((row.get("safety") or {}).get("deterministic"))
        or int((row.get("safety") or {}).get("leakage_count") or 0) != 0
        or not bool((row.get("safety") or {}).get("legacy_byte_identity"))
    ]
    if safety_failures:
        return {
            "verdict": "OLD_WINS",
            "reason": "safety_or_legacy_byte_failure",
            "failed_rows": safety_failures,
        }

    scorable = [row for row in rows if bool(row.get("scorable"))]
    region_scorable = [row for row in scorable if bool(row.get("region_scorable"))]
    random_scorable = [
        row
        for row in scorable
        if str(row.get("split") or "random").lower() == "random"
    ]
    if not random_scorable:
        return {
            "verdict": "INCONCLUSIVE",
            "reason": "random_primary_comparison_set_unavailable",
        }
    language_counts = {
        language: sum(1 for row in region_scorable if row.get("language") == language)
        for language in _EXPECTED_LANGUAGES
    }
    if any(count < 3 for count in language_counts.values()):
        return {
            "verdict": "INCONCLUSIVE",
            "reason": "fewer_than_three_region_scorable_cases",
            "language_counts": language_counts,
        }

    # The locked random split is the primary comparison set for aggregate
    # retrieval gates. Held/ext2 rows remain diagnostic and contribute to the
    # per-language, patch-grounded, precision, and operational safety gates.
    old_h1 = _mean_bool(random_scorable, "old", "hit_at_1")
    new_h1 = _mean_bool(random_scorable, "new", "hit_at_1")
    old_h8 = _mean_bool(random_scorable, "old", "hit_at_8")
    new_h8 = _mean_bool(random_scorable, "new", "hit_at_8")
    per_language_h8 = {}
    per_language_regression = False
    for language in _EXPECTED_LANGUAGES:
        language_rows = [row for row in scorable if row.get("language") == language]
        old_rate = _mean_bool(language_rows, "old", "hit_at_8")
        new_rate = _mean_bool(language_rows, "new", "hit_at_8")
        per_language_h8[language] = {"old": old_rate, "new": new_rate}
        if new_rate + 1e-12 < old_rate:
            per_language_regression = True

    old_symbol = _mean_numeric(region_scorable, "old", "symbol_recall")
    new_symbol = _mean_numeric(region_scorable, "new", "symbol_recall")
    old_line = _mean_numeric(region_scorable, "old", "line_recall")
    new_line = _mean_numeric(region_scorable, "new", "line_recall")
    old_precision = _mean_numeric(scorable, "old", "file_precision")
    new_precision = _mean_numeric(scorable, "new", "file_precision")
    old_symbol_precision = _mean_numeric(scorable, "old", "symbol_precision")
    new_symbol_precision = _mean_numeric(scorable, "new", "symbol_precision")
    old_region_precision = _mean_numeric(
        region_scorable, "old", "region_precision"
    )
    new_region_precision = _mean_numeric(
        region_scorable, "new", "region_precision"
    )

    old_latency = _percentile(
        [float(row["old"]["latency_ms"]) for row in scorable], 0.95
    )
    new_latency = _percentile(
        [float(row["new"]["latency_ms"]) for row in scorable], 0.95
    )
    old_memory = _percentile(
        [float(row["old"]["peak_memory_bytes"]) for row in scorable], 0.95
    )
    new_memory = _percentile(
        [float(row["new"]["peak_memory_bytes"]) for row in scorable], 0.95
    )
    latency_ratio = new_latency / old_latency if old_latency > 0 else float("inf")
    memory_ratio = new_memory / old_memory if old_memory > 0 else float("inf")

    recall_or_cost_regression = (
        new_h1 + 1e-12 < old_h1
        or new_h8 + 1e-12 < old_h8
        or per_language_regression
        or new_symbol + 1e-12 < old_symbol
        or new_line + 1e-12 < old_line
        or new_precision + 1e-12 < old_precision
        or new_symbol_precision + 1e-12 < old_symbol_precision
        or new_region_precision + 1e-12 < old_region_precision
        or latency_ratio > 1.25 + 1e-12
        or memory_ratio > 1.25 + 1e-12
    )
    old_token_median = statistics.median(
        float(row["old"]["implied_inspection_tokens"]) for row in scorable
    )
    new_token_median = statistics.median(
        float(row["new"]["implied_inspection_tokens"]) for row in scorable
    )
    context_reduction = (
        1.0 - (new_token_median / old_token_median)
        if old_token_median > 0
        else 0.0
    )
    metrics = {
        "overall_hit_at_1": {"old": old_h1, "new": new_h1},
        "overall_hit_at_8": {"old": old_h8, "new": new_h8},
        "random_primary_hit_at_1": {"old": old_h1, "new": new_h1},
        "random_primary_hit_at_8": {"old": old_h8, "new": new_h8},
        "per_language_hit_at_8": per_language_h8,
        "symbol_recall": {"old": old_symbol, "new": new_symbol},
        "line_recall": {"old": old_line, "new": new_line},
        "file_precision": {"old": old_precision, "new": new_precision},
        "symbol_precision": {
            "old": old_symbol_precision,
            "new": new_symbol_precision,
        },
        "region_precision": {
            "old": old_region_precision,
            "new": new_region_precision,
        },
        "p95_latency_ratio": latency_ratio,
        "p95_memory_ratio": memory_ratio,
        "old_median_implied_inspection_tokens": old_token_median,
        "new_median_implied_inspection_tokens": new_token_median,
        "context_reduction_fraction": context_reduction,
    }
    if recall_or_cost_regression:
        return {
            "verdict": "OLD_WINS",
            "reason": "recall_precision_latency_or_memory_regression",
            **metrics,
        }
    if context_reduction >= 0.25 - 1e-12:
        return {"verdict": "NEW_WINS", "reason": "pareto_gate_passed", **metrics}
    return {
        "verdict": "TIE",
        "reason": "recall_safe_but_context_reduction_below_25_percent",
        **metrics,
    }


def _file_tokens(repository_root: str, files: Iterable[str]) -> int:
    root = Path(repository_root)
    total = 0
    seen: set[str] = set()
    for raw_path in files:
        path = _norm(raw_path)
        if path in seen:
            continue
        seen.add(path)
        try:
            target = (root / path).resolve()
            target.relative_to(root.resolve())
            total += (len(target.read_bytes()) + 3) // 4
        except (OSError, ValueError):
            continue
    return total


def _primitive(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _primitive(asdict(value))
    if isinstance(value, Mapping):
        return {
            str(key): _primitive(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_primitive(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            _primitive(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()


def _v74_projection(result: Any) -> dict[str, Any]:
    if result is None:
        return {
            "candidate_order": [],
            "scores": [],
            "ranked_full": [],
            "focus_set": [],
            "hyperparameters": {},
        }
    ranked = list(getattr(result, "ranked_full", ()) or ())
    return {
        "candidate_order": [str(row.get("path", "")) for row in ranked],
        "scores": [float(row.get("score", 0.0) or 0.0) for row in ranked],
        "ranked_full": _primitive(ranked),
        "focus_set": list(getattr(result, "focus_set", ()) or ()),
        "hyperparameters": _primitive(getattr(result, "hyperparameters", {}) or {}),
    }


def _reactive_projection(result: Any) -> dict[str, Any]:
    candidates = list(getattr(result, "candidates", ()) or ())
    return {
        "candidate_order": [
            _norm(str(getattr(candidate, "file_path", "")))
            for candidate in candidates
        ],
        "scores": [
            float(getattr(candidate, "score", 0.0) or 0.0)
            for candidate in candidates
        ],
        "witnesses": [
            str(candidate.render_witness()) for candidate in candidates
        ],
        "confidence": float(getattr(result, "confidence", 0.0) or 0.0),
        "confident": bool(getattr(result, "confident", False)),
        "localization_proof": str(getattr(result, "gate_reason", "")),
    }


def _brief_projection(result: Any) -> dict[str, Any]:
    files = list(getattr(result, "files", ()) or ())
    text = str(getattr(result, "brief_text", "") or "")
    proof = _primitive(getattr(result, "localization_proof", ()) or ())
    acquisition_proof = _primitive(
        getattr(result, "acquisition_proof", ()) or ()
    )
    return {
        "candidate_order": [
            _norm(str(getattr(entry, "path", ""))) for entry in files
        ],
        "scores": [
            float(getattr(entry, "score", 0.0) or 0.0) for entry in files
        ],
        "localization_proof": proof,
        "acquisition_proof": acquisition_proof,
        "brief_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "brief_chars": len(text),
        "brief_tokens": int(getattr(result, "token_estimate", 0) or 0),
    }


@dataclass(frozen=True)
class LegacyMeasurement:
    localizer: Any
    v74: Any
    brief: Any
    latency_ms: float
    peak_memory_bytes: int
    shadow_verification_latency_ms: float
    identity: dict[str, bool]
    memory_measurement_method: str


def _legacy_measure(
    issue_text: str,
    repository_root: str,
    graph_db: str,
) -> LegacyMeasurement:
    # Capture the production reactive projection from inside the actual brief
    # generator.  This obtains run_v74, localize, and final candidate selection
    # from one chronological legacy orchestration rather than three unrelated
    # calls whose caches/timings could diverge.
    from groundtruth.pretask import v1r_brief as brief_module

    previous_shadow = os.environ.pop("GT_LOC_VNEXT_SHADOW", None)
    previous_sidecars = os.environ.get("GT_LOC_VNEXT_SIDECAR_DIR")
    original_localize = brief_module.localize
    captured: list[Any] = []

    def capture_localize(*args: Any, **kwargs: Any) -> Any:
        result = original_localize(*args, **kwargs)
        captured.append(result)
        return result

    brief_module.localize = capture_localize
    try:
        tracemalloc.start()
        started = time.perf_counter()
        rss_sampler = _PeakRssSampler()
        with rss_sampler:
            legacy_brief = brief_module.generate_v1r_brief(
                issue_text,
                repository_root,
                graph_db,
            )
        elapsed = (time.perf_counter() - started) * 1000.0
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        if not captured:
            # The production generator skips reactive localization only on a
            # no-candidate path. Preserve an explicit empty/unavailable state;
            # do not manufacture a second baseline execution.
            legacy_localizer = None
        else:
            legacy_localizer = captured[0]

        with tempfile.TemporaryDirectory(prefix="gt-loc-vnext-byte-lock-") as sidecars:
            os.environ["GT_LOC_VNEXT_SHADOW"] = "1"
            os.environ["GT_LOC_VNEXT_SIDECAR_DIR"] = sidecars
            shadow_started = time.perf_counter()
            shadow_brief = brief_module.generate_v1r_brief(
                issue_text,
                repository_root,
                graph_db,
            )
            shadow_elapsed = (time.perf_counter() - shadow_started) * 1000.0

        shadow_localizer = captured[1] if len(captured) > 1 else None
        legacy_v74 = getattr(legacy_brief, "v74_result", None)
        shadow_v74 = getattr(shadow_brief, "v74_result", None)
        identity = {
            "brief_text": (
                str(getattr(legacy_brief, "brief_text", "")).encode("utf-8")
                == str(getattr(shadow_brief, "brief_text", "")).encode("utf-8")
            ),
            "final_candidates": (
                _brief_projection(legacy_brief) == _brief_projection(shadow_brief)
            ),
            "localization_proof": (
                _primitive(getattr(legacy_brief, "localization_proof", ()) or ())
                == _primitive(getattr(shadow_brief, "localization_proof", ()) or ())
            ),
            "acquisition_proof": (
                _primitive(getattr(legacy_brief, "acquisition_proof", ()) or ())
                == _primitive(getattr(shadow_brief, "acquisition_proof", ()) or ())
            ),
            "run_v74": (
                _v74_projection(legacy_v74) == _v74_projection(shadow_v74)
            ),
            "reactive_top_five": (
                _reactive_projection(legacy_localizer)
                == _reactive_projection(shadow_localizer)
            ),
        }
        return LegacyMeasurement(
            localizer=legacy_localizer,
            v74=legacy_v74,
            brief=legacy_brief,
            latency_ms=elapsed,
            peak_memory_bytes=rss_sampler.peak_bytes or peak,
            shadow_verification_latency_ms=shadow_elapsed,
            identity=identity,
            memory_measurement_method=(
                rss_sampler.method
                if rss_sampler.peak_bytes
                else "tracemalloc_python_allocations"
            ),
        )
    finally:
        if tracemalloc.is_tracing():
            tracemalloc.stop()
        brief_module.localize = original_localize
        if previous_shadow is None:
            os.environ.pop("GT_LOC_VNEXT_SHADOW", None)
        else:
            os.environ["GT_LOC_VNEXT_SHADOW"] = previous_shadow
        if previous_sidecars is None:
            os.environ.pop("GT_LOC_VNEXT_SIDECAR_DIR", None)
        else:
            os.environ["GT_LOC_VNEXT_SIDECAR_DIR"] = previous_sidecars


def _first_divergence(old_files: Sequence[str], new_files: Sequence[str]) -> dict[str, Any]:
    limit = max(len(old_files), len(new_files))
    for index in range(limit):
        old = old_files[index] if index < len(old_files) else None
        new = new_files[index] if index < len(new_files) else None
        if old != new:
            return {"rank": index + 1, "old": old, "new": new}
    return {"rank": None, "old": None, "new": None}


def _legacy_inspection_files(
    reactive_projection: Mapping[str, Any],
    v74_projection: Mapping[str, Any],
    brief_projection: Mapping[str, Any],
) -> list[str]:
    """Select the files the legacy model-visible surface actually exposes."""
    for candidates in (
        brief_projection.get("candidate_order") or (),
        v74_projection.get("focus_set") or (),
        reactive_projection.get("candidate_order") or (),
    ):
        normalized = [
            _norm(str(path))
            for path in candidates
            if _norm(str(path))
        ]
        if normalized:
            return list(dict.fromkeys(normalized))
    return []


def run_sealed_case(
    case_input: Mapping[str, Any],
    *,
    repeats: int = 3,
) -> dict[str, Any]:
    """Run old/new without accepting or reading any gold fields."""
    allowed = {
        "id",
        "issue_text",
        "repository_root",
        "repo_root",
        "graph_db",
        "revision_identity",
        "language",
        "split",
    }
    unexpected = set(case_input) - allowed
    if unexpected:
        raise ValueError(f"unsealed or unsupported input keys: {sorted(unexpected)}")
    case_id = str(case_input["id"])
    issue = str(case_input["issue_text"])
    repo = str(case_input.get("repository_root") or case_input.get("repo_root") or "")
    graph = str(case_input["graph_db"])
    revision = str(case_input.get("revision_identity") or "unknown")

    legacy_measurement = _legacy_measure(issue, repo, graph)
    reactive_candidates = list(
        legacy_discoveries_from_projection(
            legacy_measurement.localizer,
            "localize",
        )
    )
    v74_candidates = list(
        getattr(legacy_measurement.v74, "ranked_full", ()) or ()
    )
    legacy_candidates = [*v74_candidates, *reactive_candidates]
    reactive_projection = _reactive_projection(legacy_measurement.localizer)
    v74_projection = _v74_projection(legacy_measurement.v74)
    brief_projection = _brief_projection(legacy_measurement.brief)
    reactive_files = list(reactive_projection["candidate_order"])
    old_files = _legacy_inspection_files(
        reactive_projection,
        v74_projection,
        brief_projection,
    )
    legacy_projection = {
        **reactive_projection,
        "candidate_order": old_files,
        "reactive_candidate_order": reactive_files,
        "reactive_top_five": reactive_files[:5],
        "run_v74": v74_projection,
        "final_brief": brief_projection,
        "byte_identity": all(legacy_measurement.identity.values()),
        "byte_identity_checks": dict(legacy_measurement.identity),
        "projection_sha256": _sha256_json(
            {
                "reactive": reactive_projection,
                "run_v74": v74_projection,
                "final_brief": brief_projection,
            }
        ),
    }

    request = LocalizationRequest(
        issue_text=issue,
        repository_root=repo,
        graph_db=graph,
        revision_identity=revision,
    )
    results = []
    new_peaks = []
    new_memory_methods = []
    for _repeat in range(max(3, repeats)):
        rss_sampler = _PeakRssSampler()
        with rss_sampler:
            repeated = localize_vnext(
                request,
                legacy_discoveries=legacy_candidates,
            )
        results.append(repeated)
        new_peaks.append(
            rss_sampler.peak_bytes
            or int(repeated.metrics.get("peak_memory_bytes") or 0)
        )
        new_memory_methods.append(
            rss_sampler.method
            if rss_sampler.peak_bytes
            else "tracemalloc_python_allocations"
        )
    new_result = results[0]
    language = _normalize_language(str(case_input.get("language") or "unknown"))
    if language in {"", "unknown", "none"}:
        graph_languages = list(new_result.capabilities.details.get("languages") or ())
        if graph_languages:
            language = _normalize_language(str(graph_languages[0]))
    hashes = [result.deterministic_hash for result in results]
    ranked_discovery_files = list(
        dict.fromkeys(
            discovery.file_path
            for discovery in new_result.discoveries
            if discovery.file_path
        )
    )
    new_files = list(dict.fromkeys(region.file_path for region in new_result.admitted_regions))
    discovery_by_id = {
        discovery.evidence_id: discovery for discovery in new_result.discoveries
    }
    admitted_decision_trace = [
        {
            "evidence_id": decision.evidence_id,
            "file": discovery_by_id[decision.evidence_id].file_path,
            "symbol": discovery_by_id[decision.evidence_id].symbol,
            "span": [
                discovery_by_id[decision.evidence_id].start_line,
                discovery_by_id[decision.evidence_id].end_line,
            ],
            "roles_added": list(decision.newly_covered_roles),
            "reason_codes": [reason.value for reason in decision.reason_codes],
        }
        for decision in new_result.decisions
        if decision.action is CandidateAction.ADMIT
        and decision.evidence_id in discovery_by_id
    ]
    contribution = [
        {
            "file": region.file_path,
            "span": [region.start_line, region.end_line],
            "roles": list(region.roles),
            "tokens": region.source_tokens,
            "selection_reason": region.selection_reason,
        }
        for region in new_result.admitted_regions
    ]
    new_latencies = [
        float(result.metrics.get("latency_ms") or 0.0) for result in results
    ]
    ablations = {}
    for component in (
        "behavioral_facets",
        "structured_semantics",
        "relation_policy",
        "marginal_coverage",
        "history",
        "source_regions",
    ):
        policy = LocalizationPolicy(disabled_components=frozenset({component}))
        ablated = localize_vnext(
            LocalizationRequest(
                issue_text=issue,
                repository_root=repo,
                graph_db=graph,
                revision_identity=revision,
                policy=policy,
            ),
            legacy_discoveries=legacy_candidates,
        )
        ablations[component] = {
            "deterministic_hash": ablated.deterministic_hash,
            "admitted_files": list(
                dict.fromkeys(region.file_path for region in ablated.admitted_regions)
            ),
            "admitted_source_tokens": int(
                ablated.metrics.get("admitted_source_tokens") or 0
            ),
            "changed_output": ablated.deterministic_hash
            != new_result.deterministic_hash,
            "source_token_delta_from_full": int(
                ablated.metrics.get("admitted_source_tokens") or 0
            )
            - int(new_result.metrics.get("admitted_source_tokens") or 0),
        }

    return {
        "schema": "gt.localization.vnext.comparison.sealed.v1",
        "case": {
            "id": case_id,
            "language": language,
            "split": str(case_input.get("split") or "unknown"),
            "issue_sha256": hashlib.sha256(issue.encode("utf-8")).hexdigest(),
            "revision_identity": revision,
        },
        "legacy": {
            **legacy_projection,
            "latency_ms": legacy_measurement.latency_ms,
            "peak_memory_bytes": legacy_measurement.peak_memory_bytes,
            "memory_measurement_method": (
                legacy_measurement.memory_measurement_method
            ),
            "shadow_verification_latency_ms": (
                legacy_measurement.shadow_verification_latency_ms
            ),
            "implied_inspection_tokens": _file_tokens(repo, old_files),
        },
        "vnext": new_result.to_dict(),
        "comparison": {
            "ranked_discovery_files": ranked_discovery_files,
            "new_admitted_files": new_files,
            "first_divergence": _first_divergence(old_files, ranked_discovery_files),
            "region_contributions": contribution,
            "admitted_decision_trace": admitted_decision_trace,
            "implied_inspection_tokens": sum(
                region.source_tokens for region in new_result.admitted_regions
            ),
            "tokens_saved": _file_tokens(repo, old_files)
            - sum(region.source_tokens for region in new_result.admitted_regions),
            "deterministic_hashes": hashes,
            "deterministic": len(set(hashes)) == 1,
            "cold_latency_ms": new_latencies[0],
            "warm_latency_ms": statistics.median(new_latencies[1:]),
            "p95_latency_ms": _percentile(new_latencies, 0.95),
            "peak_memory_bytes": max(new_peaks, default=0),
            "memory_measurement_methods": new_memory_methods,
            "ablations": ablations,
            "algorithmic_contribution_evidence": [
                component
                for component, outcome in ablations.items()
                if outcome["changed_output"]
            ],
        },
    }


def _matches(path: str, gold: set[str]) -> bool:
    normalized = _norm(path)
    return normalized in gold or any(
        normalized.endswith("/" + candidate) or candidate.endswith("/" + normalized)
        for candidate in gold
    )


def _rank(files: Sequence[str], gold: set[str]) -> int | None:
    for index, path in enumerate(files, start=1):
        if _matches(path, gold):
            return index
    return None


def score_sealed_case(
    sealed: Mapping[str, Any], gold: Mapping[str, Any]
) -> dict[str, Any]:
    """Load gold only after a sealed result exists and score paired outputs."""
    gold_files = {_norm(path) for path in gold.get("gold_files", ())}
    old_files = list(sealed["legacy"]["candidate_order"])
    new_files = list(sealed["comparison"]["new_admitted_files"])
    new_ranked_files = list(sealed["comparison"].get("ranked_discovery_files") or new_files)
    old_rank = _rank(old_files, gold_files)
    new_rank = _rank(new_ranked_files, gold_files)
    old_hits = {path for path in old_files if _matches(path, gold_files)}
    new_hits = {path for path in new_files if _matches(path, gold_files)}

    gold_symbols = {
        str(symbol) for symbol in gold.get("gold_symbols", ()) if str(symbol)
    }
    new_symbols = {
        str(discovery.get("symbol") or "")
        for discovery in sealed["vnext"].get("discoveries", ())
        if discovery.get("symbol")
    }
    old_symbols = {
        symbol
        for witness in sealed["legacy"].get("witnesses", ())
        for symbol in re.findall(r"[A-Za-z_][A-Za-z0-9_.]*", str(witness))
    }
    old_symbol_recall = (
        len(old_symbols & gold_symbols) / len(gold_symbols) if gold_symbols else None
    )
    new_symbol_recall = (
        len(new_symbols & gold_symbols) / len(gold_symbols) if gold_symbols else None
    )
    old_symbol_precision = (
        len(old_symbols & gold_symbols) / len(old_symbols)
        if gold_symbols and old_symbols
        else None
    )
    new_symbol_precision = (
        len(new_symbols & gold_symbols) / len(new_symbols)
        if gold_symbols and new_symbols
        else None
    )

    line_ranges = list(gold.get("gold_line_ranges", ()) or ())
    gold_lines: set[tuple[str, int]] = set()
    normalized_ranges: list[tuple[str, int, int]] = []
    for item in line_ranges:
        path = _norm(str(item["file"]))
        start = int(item["start"])
        end = int(item["end"])
        normalized_ranges.append((path, start, end))
        for line in range(start, end + 1):
            gold_lines.add((path, line))
    new_regions = list(sealed["vnext"].get("admitted_regions", ()))
    new_lines = {
        (_norm(str(region["file_path"])), line)
        for region in new_regions
        for line in range(int(region["start_line"]), int(region["end_line"]) + 1)
    }
    new_line_recall = (
        len(new_lines & gold_lines) / len(gold_lines) if gold_lines else None
    )
    # Legacy full-file inspection necessarily covers every gold line in any
    # admitted gold file, but not lines in a missed file.
    old_line_recall = (
        len({line for line in gold_lines if line[0] in old_hits}) / len(gold_lines)
        if gold_lines
        else None
    )
    old_region_recall = (
        sum(1 for path, _start, _end in normalized_ranges if _matches(path, old_hits))
        / len(normalized_ranges)
        if normalized_ranges
        else None
    )
    new_region_recall = (
        sum(
            1
            for path, start, end in normalized_ranges
            if any(
                _matches(str(region["file_path"]), {path})
                and int(region["start_line"]) <= end
                and int(region["end_line"]) >= start
                for region in new_regions
            )
        )
        / len(normalized_ranges)
        if normalized_ranges
        else None
    )
    old_region_precision = (
        len(old_hits) / len(old_files)
        if normalized_ranges and old_files
        else None
    )
    new_region_precision = (
        sum(
            1
            for region in new_regions
            if any(
                _matches(str(region["file_path"]), {path})
                and int(region["start_line"]) <= end
                and int(region["end_line"]) >= start
                for path, start, end in normalized_ranges
            )
        )
        / len(new_regions)
        if normalized_ranges and new_regions
        else None
    )
    new_line_precision = (
        len(new_lines & gold_lines) / len(new_lines)
        if gold_lines and new_lines
        else None
    )
    return {
        "schema": "gt.localization.vnext.comparison.scored.v1",
        "case_id": sealed["case"]["id"],
        "language": sealed["case"]["language"],
        "split": sealed["case"]["split"],
        "scorable": bool(gold_files),
        "region_scorable": bool(gold_lines),
        "safety": {
            "deterministic": bool(sealed["comparison"]["deterministic"]),
            "leakage_count": int(sealed["vnext"]["metrics"].get("leakage_count", 0)),
            "legacy_byte_identity": bool(
                sealed["legacy"].get("byte_identity", False)
            ),
        },
        "old": {
            "first_gold_rank": old_rank,
            "hit_at_1": old_rank == 1,
            "hit_at_3": old_rank is not None and old_rank <= 3,
            "hit_at_8": old_rank is not None and old_rank <= 8,
            "file_recall": len({_norm(path) for path in old_hits}) / len(gold_files)
            if gold_files
            else None,
            "file_precision": (
                sum(1 for path in old_files[:8] if _matches(path, gold_files))
                / len(old_files[:8])
                if old_files[:8]
                else 0.0
            ),
            "symbol_recall": old_symbol_recall,
            "symbol_precision": old_symbol_precision,
            "region_recall": old_region_recall,
            "region_precision": old_region_precision,
            "line_recall": old_line_recall,
            "line_precision": None,
            "implied_inspection_tokens": int(
                sealed["legacy"]["implied_inspection_tokens"]
            ),
            "latency_ms": float(sealed["legacy"]["latency_ms"]),
            "peak_memory_bytes": int(sealed["legacy"]["peak_memory_bytes"]),
        },
        "new": {
            "first_gold_rank": new_rank,
            "hit_at_1": new_rank == 1,
            "hit_at_3": new_rank is not None and new_rank <= 3,
            "hit_at_8": new_rank is not None and new_rank <= 8,
            "file_recall": len({_norm(path) for path in new_hits}) / len(gold_files)
            if gold_files
            else None,
            "file_precision": (
                sum(1 for path in new_ranked_files[:8] if _matches(path, gold_files))
                / len(new_ranked_files[:8])
                if new_ranked_files[:8]
                else 0.0
            ),
            "admitted_file_precision": len(new_hits) / len(new_files) if new_files else 0.0,
            "symbol_recall": new_symbol_recall,
            "symbol_precision": new_symbol_precision,
            "region_recall": new_region_recall,
            "region_precision": new_region_precision,
            "line_recall": new_line_recall,
            "line_precision": new_line_precision,
            "implied_inspection_tokens": int(
                sealed["comparison"]["implied_inspection_tokens"]
            ),
            "latency_ms": float(sealed["comparison"]["p95_latency_ms"]),
            "peak_memory_bytes": int(sealed["comparison"]["peak_memory_bytes"]),
        },
    }


__all__ = [
    "evaluate_winner",
    "run_sealed_case",
    "score_sealed_case",
]
