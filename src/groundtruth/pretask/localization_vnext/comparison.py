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
# A language below this many scorable cases is REPORTED but does not vote in the
# per-language regression gate: at n=1 a single discordant pair is the entire
# signal, and a 41-job-hour verdict decided by one case is not a measurement.
_LANGUAGE_GATE_MIN_CASES = 3
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


def _paired_means(rows: Sequence[Mapping[str, Any]], key: str) -> dict[str, Any]:
    """Mean old/new over exactly the rows where BOTH arms measured ``key``.

    Aggregating each arm over its own rows puts two different populations on
    the two sides of one gate: a row that scores old and not new (or the
    reverse) moves one mean only, so the gate can read "no regression" while
    the new arm is worse on every row the two arms actually share.  With no
    comparable row the metric is UNMEASURED - None, never a measured 0.0.
    """
    paired = [
        row
        for row in rows
        if row["old"].get(key) is not None and row["new"].get(key) is not None
    ]
    if not paired:
        return {"old": None, "new": None, "paired_cases": 0}
    return {
        "old": statistics.fmean(float(row["old"][key]) for row in paired),
        "new": statistics.fmean(float(row["new"][key]) for row in paired),
        "paired_cases": len(paired),
    }


def _regressed(paired: Mapping[str, Any]) -> bool:
    """An UNMEASURED metric can neither show a regression nor clear one."""
    old_value = paired["old"]
    new_value = paired["new"]
    if old_value is None or new_value is None:
        return False
    return float(new_value) + 1e-12 < float(old_value)


def _paired_means_at_k(
    rows: Sequence[Mapping[str, Any]], key: str
) -> dict[str, Any]:
    """`_paired_means` for the {"k", "value"} precision-at-k shape.

    Rows whose two sides were scored at DIFFERENT k are dropped rather than
    averaged: comparing 1/3 to 1/8 is a list-length comparison wearing a
    precision label.
    """
    paired = []
    for row in rows:
        old_pk = row["old"].get(key) or {}
        new_pk = row["new"].get(key) or {}
        if old_pk.get("value") is None or new_pk.get("value") is None:
            continue
        if int(old_pk.get("k", -1)) != int(new_pk.get("k", -2)):
            continue
        paired.append((float(old_pk["value"]), float(new_pk["value"])))
    if not paired:
        return {"old": None, "new": None, "paired_cases": 0}
    return {
        "old": statistics.fmean(o for o, _ in paired),
        "new": statistics.fmean(n for _, n in paired),
        "paired_cases": len(paired),
    }


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
    # Count the languages the corpus ACTUALLY contains, not a fixed five-tuple.
    # Keying the gate off `_EXPECTED_LANGUAGES` made it unsatisfiable on any
    # corpus missing one of them - permanently so on a monolingual corpus - and
    # it returned before computing a single metric. Verified against the real
    # completed run 30196352388: 0/60 rows are region_scorable, so this gate
    # short-circuited and hit@1, hit@8, precision, latency and memory were never
    # evaluated on ANY run in the corpus's history.
    present_languages = {
        str(row.get("language") or "unknown") for row in scorable
    } or {"unknown"}
    language_counts = {
        language: sum(1 for row in region_scorable if row.get("language") == language)
        for language in sorted(present_languages)
    }
    # Region-level gold is a SEPARATE capability from retrieval. Its absence
    # makes region metrics UNMEASURED - it does not make retrieval unjudgeable.
    #
    # A language below the floor is DROPPED from region scoring; it does not
    # black out the languages that ARE above it. Requiring every present
    # language to clear the floor meant one 1-case language nulled all 3,248
    # line ranges and 1,355 symbols on a 294-case corpus - discarding the only
    # capability that corpus exists to measure.
    region_languages = {
        language
        for language, count in language_counts.items()
        if count >= _LANGUAGE_GATE_MIN_CASES
    }
    region_scorable = [
        row for row in region_scorable if row.get("language") in region_languages
    ]

    # The locked random split is the primary comparison set for aggregate
    # retrieval gates. Held/ext2 rows remain diagnostic and contribute to the
    # per-language, patch-grounded, precision, and operational safety gates.
    old_h1 = _mean_bool(random_scorable, "old", "hit_at_1")
    new_h1 = _mean_bool(random_scorable, "new", "hit_at_1")
    old_h8 = _mean_bool(random_scorable, "old", "hit_at_8")
    new_h8 = _mean_bool(random_scorable, "new", "hit_at_8")
    per_language_h8 = {}
    per_language_regression = False
    # Read the languages the corpus CONTAINS. Iterating the fixed five-tuple
    # reported a measured 0.0/0.0 hit@8 for languages with no rows at all -
    # fabricated numbers a reader cannot distinguish from a real total failure -
    # and it gave every language equal standing regardless of size, so a
    # single-case language could decide a whole run's verdict by itself.
    for language in sorted(present_languages):
        language_rows = [row for row in scorable if row.get("language") == language]
        if not language_rows:
            continue
        old_rate = _mean_bool(language_rows, "old", "hit_at_8")
        new_rate = _mean_bool(language_rows, "new", "hit_at_8")
        per_language_h8[language] = {
            "old": old_rate,
            "new": new_rate,
            "cases": len(language_rows),
        }
        # A language too small to carry a paired decision is reported but does
        # not vote. `_LANGUAGE_GATE_MIN_CASES` cases is the floor at which one
        # discordant pair stops being the entire signal.
        if len(language_rows) >= _LANGUAGE_GATE_MIN_CASES and new_rate + 1e-12 < old_rate:
            per_language_regression = True

    symbol_recall = _paired_means(region_scorable, "symbol_recall")
    line_recall = _paired_means(region_scorable, "line_recall")
    # The GATE reads the equal-k precision. The fixed-`[:8]` `file_precision`
    # stays in the report for continuity but must never gate: legacy's list is
    # a median of 3 entries long, so `[:8]` scored it at k=3 and vnext at k=8,
    # and that denominator gap alone flipped run 30221830560 to OLD_WINS.
    file_precision = _paired_means_at_k(scorable, "file_precision_at_k")
    file_precision_fixed_8 = _paired_means(scorable, "file_precision")
    symbol_precision = _paired_means(scorable, "symbol_precision")
    region_precision = _paired_means(region_scorable, "region_precision")

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
        or _regressed(symbol_recall)
        or _regressed(line_recall)
        or _regressed(file_precision)
        or _regressed(symbol_precision)
        or _regressed(region_precision)
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
        # Each paired metric carries the population it was computed on, so a
        # metric no case could score reads as unmeasured instead of as a tie.
        "symbol_recall": symbol_recall,
        "line_recall": line_recall,
        "file_precision": file_precision,
        "file_precision_fixed_k8_ungated": file_precision_fixed_8,
        "symbol_precision": symbol_precision,
        "region_precision": region_precision,
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
    return {
        "candidate_order": [
            _norm(str(getattr(entry, "path", ""))) for entry in files
        ],
        "scores": [
            float(getattr(entry, "score", 0.0) or 0.0) for entry in files
        ],
        "localization_proof": proof,
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


def _legacy_ranking_priors(old_files: Sequence[str]) -> list[dict[str, Any]]:
    """Carry the exact model-visible legacy file order into shadow ranking.

    These are ranking-only priors, not behavioral facts and not admissible
    source evidence.
    """
    return [
        {
            "path": path,
            "score": 0.5,
            "components": {"lex": 0.5},
            "legacy_rank": rank,
            "ranking_prior_only": True,
        }
        for rank, path in enumerate(
            dict.fromkeys(_norm(path) for path in old_files if _norm(path)),
            start=1,
        )
    ]


def _shadow_legacy_candidates(
    old_files: Sequence[str],
    v74_candidates: Sequence[Any],
    reactive_candidates: Sequence[Any],
) -> list[Any]:
    """Compose the shadow seed with the model-visible order as its prefix."""
    return [
        *_legacy_ranking_priors(old_files),
        *v74_candidates,
        *reactive_candidates,
    ]


def _input_digests(repository_root: str, graph_db: str) -> dict[str, Any]:
    """Hash every input that can move the result, so drift is visible IN the artifact.

    Two runs on identical DECLARED inputs produced different legacy answers
    (Hit@1 30 vs 32, legacy file list differing in 9/60 cases) because the frozen
    embedder silently encoded nothing on 16/60 cases. Nothing in the sealed schema
    could reveal that; it was found only by diffing two runs. Content digests make
    the same drift a one-line comparison. Note the graph digest is over CONTENT,
    not (size, mtime) - a same-size rewrite must change it.
    """
    digest = ""
    size = 0
    try:
        raw = Path(graph_db).read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        size = len(raw)
    except OSError:
        digest = ""
    embedder: dict[str, Any] = {"model_root": os.getenv("GT_MODELS_ROOT", ""), "files": []}
    try:
        root = Path(embedder["model_root"] or (Path(__file__).resolve().parents[4] / "models"))
        if root.is_dir():
            embedder["files"] = sorted(
                {
                    f"{path.name}:{path.stat().st_size}"
                    for path in root.rglob("*.onnx")
                }
            )
    except OSError:
        pass
    return {
        "graph_db_sha256": digest,
        "graph_db_bytes": size,
        "repository_root": _norm(str(repository_root)),
        "embedder": embedder,
    }


def _shadow_only_ranked_files(discoveries: Sequence[Any]) -> list[str]:
    """The shadow engine's own file order, with the legacy floor removed.

    Attribution diagnostic only: the model-visible ranking priors are a
    recall-first floor, so the floored order cannot show whether the shadow
    engine's own ordering moved.  Priors still contribute to class fusion, so
    this is the engine's order without the floor, not a counterfactual run.
    """
    kept = [
        discovery
        for discovery in discoveries
        if discovery.file_path
        and dict(discovery.metadata).get("ranking_prior_only") != "1"
    ]

    def shadow_position(item: tuple[int, Any]) -> tuple[int, int]:
        index, discovery = item
        raw = dict(discovery.metadata).get("shadow_rank", "")
        return (int(raw) if raw.isdigit() else len(kept) + index + 1, index)

    return list(
        dict.fromkeys(
            discovery.file_path
            for _position, discovery in sorted(
                enumerate(kept),
                key=shadow_position,
            )
        )
    )


def _shadow_total_latency_samples(
    legacy_latency_ms: float,
    shadow_verification_latency_ms: float,
    vnext_warm_latencies_ms: Sequence[float],
) -> list[float]:
    """Keep the measured cold shadow call instead of hiding cache warm-up."""
    return [
        float(shadow_verification_latency_ms),
        *[
            float(legacy_latency_ms) + float(latency)
            for latency in vnext_warm_latencies_ms
        ],
    ]


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
    reactive_projection = _reactive_projection(legacy_measurement.localizer)
    v74_projection = _v74_projection(legacy_measurement.v74)
    brief_projection = _brief_projection(legacy_measurement.brief)
    reactive_files = list(reactive_projection["candidate_order"])
    old_files = _legacy_inspection_files(
        reactive_projection,
        v74_projection,
        brief_projection,
    )
    legacy_candidates = _shadow_legacy_candidates(
        old_files,
        v74_candidates,
        reactive_candidates,
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
    ranked_discovery_files_shadow_only = _shadow_only_ranked_files(
        new_result.discoveries
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
    shadow_total_latencies = _shadow_total_latency_samples(
        legacy_measurement.latency_ms,
        legacy_measurement.shadow_verification_latency_ms,
        new_latencies,
    )
    shadow_total_peak_memory = max(
        [
            legacy_measurement.peak_memory_bytes,
            *new_peaks,
        ],
        default=legacy_measurement.peak_memory_bytes,
    )
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
            # Content digests of every input that can move the result, so
            # inter-run drift is a one-line comparison instead of a two-run diff.
            "input_digests": _input_digests(repo, graph),
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
            "ranked_discovery_files_shadow_only": ranked_discovery_files_shadow_only,
            "new_admitted_files": new_files,
            "first_divergence": _first_divergence(
                old_files,
                ranked_discovery_files_shadow_only or ranked_discovery_files,
            ),
            "first_divergence_floored": _first_divergence(
                old_files,
                ranked_discovery_files,
            ),
            "region_contributions": contribution,
            "admitted_decision_trace": admitted_decision_trace,
            "implied_inspection_tokens": sum(
                region.source_tokens for region in new_result.admitted_regions
            ),
            "tokens_saved": _file_tokens(repo, old_files)
            - sum(region.source_tokens for region in new_result.admitted_regions),
            "deterministic_hashes": hashes,
            "deterministic": len(set(hashes)) == 1,
            "cold_latency_ms": max(
                0.0,
                legacy_measurement.shadow_verification_latency_ms
                - legacy_measurement.latency_ms,
            ),
            "cold_latency_measurement": (
                "shadow_total_minus_flag_off_legacy"
            ),
            "warm_latency_ms": statistics.median(new_latencies),
            "p95_latency_ms": _percentile(new_latencies, 0.95),
            "peak_memory_bytes": max(new_peaks, default=0),
            "shadow_total_cold_latency_ms": shadow_total_latencies[0],
            "shadow_total_warm_latency_ms": statistics.median(
                [
                    legacy_measurement.latency_ms + latency
                    for latency in new_latencies
                ]
            ),
            "shadow_total_p95_latency_ms": _percentile(
                shadow_total_latencies,
                0.95,
            ),
            "shadow_total_peak_memory_bytes": shadow_total_peak_memory,
            "memory_measurement_methods": new_memory_methods,
            "ablations": ablations,
            "algorithmic_contribution_evidence": [
                component
                for component, outcome in ablations.items()
                if outcome["changed_output"]
            ],
        },
    }


def _same_file(candidate: str, gold: str) -> bool:
    """The one notion of "same file" every scorer in this module uses.

    A candidate may carry a checkout/worktree prefix the gold path does not
    (``checkout/src/a.py`` for gold ``src/a.py``), so a candidate whose trailing
    path SEGMENTS are exactly the gold path is the same file.  The reverse
    direction is not a match: a shallower candidate (``utils.py``) is a
    DIFFERENT file from a deeper gold path (``src/deep/utils.py``), and the old
    bidirectional rule credited that wrong file as gold - which on multi-file
    gold inflates recall, precision and rank alike.
    """
    left = _norm(candidate)
    right = _norm(gold)
    return left == right or left.endswith("/" + right)


def _matches(path: str, gold: set[str]) -> bool:
    return any(_same_file(path, candidate) for candidate in gold)


def _matched_gold(files: Sequence[str], gold: Iterable[str]) -> set[str]:
    """The GOLD paths a candidate list covers - the recall NUMERATOR.

    Recall is |matched gold files| / |gold files|.  Counting matching
    CANDIDATES instead lets several candidate spellings of one gold file report
    full recall while most of the gold is missed, and can exceed 1.0.
    """
    return {
        gold_path
        for gold_path in gold
        if any(_same_file(path, gold_path) for path in files)
    }


def _rank(files: Sequence[str], gold: set[str]) -> int | None:
    for index, path in enumerate(files, start=1):
        if _matches(path, gold):
            return index
    return None


def _precision_at_k(
    paths: Sequence[str], gold: set[str], k: int
) -> dict[str, Any]:
    """precision over the first ``k`` paths, with ``k`` reported alongside.

    Both arms MUST be scored at the same ``k``.  Returning the value bare let a
    caller compare 1/3 against 1/8 and read it as a quality difference.
    """
    window = list(paths[:k])
    if not window:
        return {"k": int(k), "value": None}
    matched = sum(1 for path in window if _matches(path, gold))
    return {"k": int(k), "value": matched / len(window)}


def score_sealed_case(
    sealed: Mapping[str, Any], gold: Mapping[str, Any]
) -> dict[str, Any]:
    """Load gold only after a sealed result exists and score paired outputs."""
    gold_files = {_norm(path) for path in gold.get("gold_files", ())}
    old_files = list(sealed["legacy"]["candidate_order"])
    new_files = list(sealed["comparison"]["new_admitted_files"])
    new_ranked_files = list(sealed["comparison"].get("ranked_discovery_files") or new_files)
    shadow_only_raw = sealed["comparison"].get("ranked_discovery_files_shadow_only")
    shadow_only_measured = shadow_only_raw is not None
    shadow_only_files = list(shadow_only_raw or ())
    old_rank = _rank(old_files, gold_files)
    new_rank = _rank(new_ranked_files, gold_files)
    shadow_only_rank = _rank(shadow_only_files, gold_files)
    # `new_hits` is the candidate-side set, used only by the candidate-side
    # ratio `admitted_file_precision`.  RECALL counts GOLD files covered - the
    # matching-candidate count is not a recall numerator.
    new_hits = {path for path in new_files if _matches(path, gold_files)}
    # RECALL compares like to like: the RANKED list on both sides.  Scoring
    # legacy over its full candidate_order and vnext over its ADMITTED set put
    # two different objects on the two sides of one metric - on run
    # 30221830560 that reported "file_recall 0.8167 -> 0.5000", a 32-point
    # collapse that does not exist (ranked-vs-ranked the same run is
    # 0.8167 -> 0.9333).  A longer list cannot LOWER recall, so a recall drop
    # beside a hit@k RISE is a population mismatch, never a regression.
    old_gold_hits = _matched_gold(old_files, gold_files)
    new_gold_hits = _matched_gold(new_ranked_files, gold_files)
    # The selection loss is real and stays visible under its OWN name: what the
    # agent RECEIVES.  Legacy delivers what it ranks, so its delivered set is
    # `old_files`; vnext's is the admitted set.
    old_admitted_gold_hits = old_gold_hits
    new_admitted_gold_hits = _matched_gold(new_files, gold_files)
    # precision@k must divide by the SAME k on both sides or it measures list
    # LENGTH, not quality.  Legacy returns a median of 3 candidates and vnext
    # ranks 28.5, so a fixed `[:8]` divided legacy's hits by 3.6 and vnext's by
    # 7.9 - the artifact that decided OLD_WINS on run 30221830560 while the two
    # arms sat within 2pp at every k where both lists were populated.
    equal_k = min(len(old_files), len(new_ranked_files), 8)

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
    # UNSCORABLE, not zero.  `old_symbols` is every identifier-like token
    # scraped out of witness PROSE ("set_fields calls set_parse [CALLS]",
    # "defines Foo (issue symbol)"), so the denominator counted words like
    # "calls", "defines" and "unverified" - a text statistic, not a symbol set.
    # The legacy model-visible surface exposes no parsed symbols to derive a
    # comparable denominator from (`run_v74.ranked_full` rows are file-level),
    # so prose tokens must not be scored against parsed gold symbols at all.
    old_symbol_precision = None
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
    matched_gold_lines = {
        (gold_path, line)
        for gold_path, line in gold_lines
        if any(
            candidate_line == line
            and _matches(candidate_path, {gold_path})
            for candidate_path, candidate_line in new_lines
        )
    }
    matched_new_lines = {
        (candidate_path, line)
        for candidate_path, line in new_lines
        if any(
            gold_line == line
            and _matches(candidate_path, {gold_path})
            for gold_path, gold_line in gold_lines
        )
    }
    new_line_recall = (
        len(matched_gold_lines) / len(gold_lines) if gold_lines else None
    )
    # Legacy full-file inspection necessarily covers every gold line in any
    # admitted gold file, but not lines in a missed file.  Line recall and
    # region recall must decide "the legacy arm delivered this gold file" the
    # same way: exact string membership said no to `checkout/src/a.py` for gold
    # `src/a.py` while the region scorer's suffix rule said yes, so one scorer
    # held two notions of "same file".
    range_files = {path for path, _start, _end in normalized_ranges}
    old_covered_range_files = _matched_gold(old_files, range_files)
    old_line_recall = (
        len({line for line in gold_lines if line[0] in old_covered_range_files})
        / len(gold_lines)
        if gold_lines
        else None
    )
    old_region_recall = (
        sum(
            1
            for path, _start, _end in normalized_ranges
            if path in old_covered_range_files
        )
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
    # Both arms must report the SAME ratio.  The legacy arm delivers whole
    # files, so each delivered file IS one region spanning its file, and that
    # region overlaps a gold range exactly when the file holds one.  The old
    # form counted gold-FILE hits over delivered FILES and put that against the
    # new arm's gold-RANGE overlaps over delivered REGIONS - two different
    # ratios on the two sides of one gate.
    old_region_precision = (
        sum(1 for path in old_files if _matches(path, range_files))
        / len(old_files)
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
        len(matched_new_lines) / len(new_lines)
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
            "file_recall": len(old_gold_hits) / len(gold_files)
            if gold_files
            else None,
            "admitted_file_recall": len(old_admitted_gold_hits) / len(gold_files)
            if gold_files
            else None,
            "file_precision_at_k": _precision_at_k(old_files, gold_files, equal_k),
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
            "file_recall": len(new_gold_hits) / len(gold_files)
            if gold_files
            else None,
            "admitted_file_recall": len(new_admitted_gold_hits) / len(gold_files)
            if gold_files
            else None,
            "file_precision_at_k": _precision_at_k(
                new_ranked_files, gold_files, equal_k
            ),
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
            "latency_ms": float(
                sealed["comparison"].get("shadow_total_p95_latency_ms")
                or sealed["comparison"]["p95_latency_ms"]
            ),
            "peak_memory_bytes": int(
                sealed["comparison"].get("shadow_total_peak_memory_bytes")
                or sealed["comparison"]["peak_memory_bytes"]
            ),
        },
        # Attribution diagnostic, never the reported comparison column: the
        # shadow engine's own order with the model-visible legacy floor removed.
        # An artifact sealed before this column existed is UNMEASURED, never a
        # measured zero - reporting False/0 there fabricates a regression.
        "new_shadow_only": {
            "measured": shadow_only_measured,
            "first_gold_rank": shadow_only_rank if shadow_only_measured else None,
            "hit_at_1": (shadow_only_rank == 1) if shadow_only_measured else None,
            "hit_at_3": (
                shadow_only_rank is not None and shadow_only_rank <= 3
            )
            if shadow_only_measured
            else None,
            "hit_at_8": (
                shadow_only_rank is not None and shadow_only_rank <= 8
            )
            if shadow_only_measured
            else None,
            "ranked_file_count": len(shadow_only_files) if shadow_only_measured else None,
            "file_precision": (
                (
                    sum(1 for path in shadow_only_files[:8] if _matches(path, gold_files))
                    / len(shadow_only_files[:8])
                    if shadow_only_files[:8]
                    else 0.0
                )
                if shadow_only_measured
                else None
            ),
        },
    }


__all__ = [
    "evaluate_winner",
    "run_sealed_case",
    "score_sealed_case",
]
