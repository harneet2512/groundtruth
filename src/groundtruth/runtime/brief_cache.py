"""Gate-to-emit brief cache plus independent acquisition determinism proof.

The foundational-gate subprocess persists the authoritative ``V1RBriefResult``.
The later emit stage loads those exact bytes, executes the same acquisition once
independently, and attaches a repeat witness only when their canonical identities
match. The second execution never replaces the model-visible gate artifact.
"""
from __future__ import annotations

import hashlib
import json
import os
import copy
import tempfile
from dataclasses import dataclass
from typing import Any, Optional

BRIEF_CACHE_BASENAME = "brief_result.json"
BRIEF_DETERMINISM_DIAGNOSTIC_BASENAME = "brief_determinism_mismatch.json"
BRIEF_RESULT_SCHEMA = "gt.brief_result.v1"
_METRIC_FIELDS = (
    "effective_w_sem", "semantic_signal_count",
    "rendered_candidate_count", "k_sem_top", "sem_components",
    "localization_proof", "acquisition_proof",
    # ACQ phase-A: V1R already computes these witnesses. Keep them across the
    # gate->emit boundary so the sealed cache retains acquisition provenance.
    # Sidecar-only: none participates in ``brief_text``.
    "graph_edge_count", "structural_signal_count", "fts5_signal_count",
    # C15 (2026-07-27): the four fields above are DELIVERY counts whose unqualified names
    # read as ACQUISITION. Carry both families explicitly so a downstream reader never has
    # to guess which fact it is holding. ``delivered_*`` may be None == NOT_EVALUABLE.
    "acquired_graph_edge_count", "acquired_semantic_signal_count",
    "acquired_structural_signal_count", "acquired_fts5_signal_count",
    "delivered_graph_edge_count", "delivered_semantic_signal_count",
    "delivered_structural_signal_count", "delivered_fts5_signal_count",
    "delivered_candidate_count",
    "block_receipts", "control_participation", "tokenizer_used", "budget_suppressed",
    # Cluster-2b: the build-time obligations extraction record (issue source identity +
    # extracted obligations digest/count) bound to the delivered obligations block seal,
    # so the runner can seal a joinable obligations producer attestation. Sidecar only.
    "obligations_record",
    # B-4: persist the localization confidence tier so the L5b verify-horizon reader
    # (gt_mini_patch._brief_confidence_tier) can consume it. Previously omitted -> the
    # tier never reached the agent -> the LOW-tier verify advisory shipped DEAD.
    "confidence_tier",
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
        # Some metric payloads may carry dataclass-ish or iterator values; keep only
        # JSON-safe scalars/lists/dicts so proof persistence cannot affect briefing.
        if k in {"sem_components", "localization_proof", "acquisition_proof"} and v is not None and not isinstance(v, (list, dict, int, float, str, bool)):
            try:
                v = list(v)
            except TypeError:
                v = None
        out[k] = v
    return out


def _canonical_acquisition_payload(payload: Any) -> dict[str, Any]:
    """Return exact brief text + persisted ACQ data, excluding this witness."""
    if isinstance(payload, dict) and isinstance(payload.get("metrics"), dict):
        brief_text = payload.get("brief_text")
        metrics = copy.deepcopy(payload["metrics"])
    else:
        brief_text = getattr(payload, "brief_text", None)
        metrics = copy.deepcopy(_extract_metrics(payload))
    if not isinstance(brief_text, str) or not brief_text.strip():
        raise ValueError("brief determinism: missing brief text")
    proofs = metrics.get("localization_proof")
    if not isinstance(proofs, list):
        raise ValueError("brief determinism: localization proof is not a list")
    acquisition_proofs = metrics.get("acquisition_proof")
    if acquisition_proofs is None:
        acquisition_proofs = []
        metrics["acquisition_proof"] = acquisition_proofs
    elif not isinstance(acquisition_proofs, list):
        raise ValueError("brief determinism: acquisition proof is not a list")
    metrics.pop("determinism_witness", None)
    for proof in proofs:
        if not isinstance(proof, dict):
            raise ValueError("brief determinism: malformed localization proof")
        sources = proof.get("acquisition_sources")
        if isinstance(sources, dict):
            sources.pop("determinism", None)
    if any(not isinstance(proof, dict) for proof in acquisition_proofs):
        raise ValueError("brief determinism: malformed acquisition proof")
    return {"brief_text": brief_text.strip(), "metrics": metrics}


def _canonical_payload_sha256(payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _canonical_acquisition_sha256(payload: Any) -> str:
    """Hash exact brief text and persisted ACQ data, excluding this witness."""
    return _canonical_payload_sha256(_canonical_acquisition_payload(payload))


_MISSING = object()


def _value_fingerprint(value: Any) -> dict[str, str]:
    if value is _MISSING:
        return {"type": "missing", "sha256_16": ""}
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return {
        "type": type(value).__name__,
        "sha256_16": hashlib.sha256(encoded).hexdigest()[:16],
    }


def _bounded_payload_diff(
    primary: Any, repeat: Any, *, limit: int = 32,
) -> tuple[list[dict[str, Any]], bool]:
    """Return stable hash-only JSON-pointer differences, bounded for diagnostics."""
    differences: list[dict[str, Any]] = []
    truncated = False

    def walk(left: Any, right: Any, pointer: str) -> None:
        nonlocal truncated
        if len(differences) >= limit:
            truncated = True
            return
        if isinstance(left, dict) and isinstance(right, dict):
            for key in sorted(set(left) | set(right)):
                escaped = str(key).replace("~", "~0").replace("/", "~1")
                walk(left.get(key, _MISSING), right.get(key, _MISSING),
                     f"{pointer}/{escaped}")
            return
        if isinstance(left, list) and isinstance(right, list):
            for index in range(max(len(left), len(right))):
                walk(
                    left[index] if index < len(left) else _MISSING,
                    right[index] if index < len(right) else _MISSING,
                    f"{pointer}/{index}",
                )
            return
        if left != right or type(left) is not type(right):
            differences.append({
                "json_pointer": pointer or "/",
                "primary": _value_fingerprint(left),
                "repeat": _value_fingerprint(right),
            })

    walk(primary, repeat, "")
    return differences, truncated


def _atomic_write_payload(path: str, payload: dict[str, Any]) -> None:
    """Atomically replace one JSON cache artifact in its existing directory."""
    parent = os.path.dirname(path) or "."
    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=parent, delete=False,
        ) as handle:
            temp_path = handle.name
            json.dump(payload, handle, sort_keys=True)
        # Publish with the cross-runner handoff mode already in place. Applying
        # chmod after replace would expose a transient destination inherited from
        # NamedTemporaryFile's private default mode.
        os.chmod(temp_path, 0o644)
        os.replace(temp_path, path)
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass


def verify_independent_generation(
    out_dir: str,
    primary: dict[str, Any],
    generate_again: Any,
    *,
    expect_identity: str,
) -> dict[str, Any]:
    """Execute a second same-input acquisition and persist equal identities.

    Cache reuse is not an execution. The primary model-visible bytes remain the
    delivery authority; the second result is used only as a repeat witness.
    """
    if not isinstance(primary, dict) or primary.get("identity") != expect_identity:
        raise ValueError("brief determinism: primary request identity mismatch")
    first_payload = _canonical_acquisition_payload(primary)
    second_payload = _canonical_acquisition_payload(generate_again())
    first_sha = _canonical_payload_sha256(first_payload)
    second_sha = _canonical_payload_sha256(second_payload)
    identities = [first_sha, second_sha]
    verdict = {"matched": first_sha == second_sha, "canonical_sha256": identities}
    if not verdict["matched"]:
        differences, truncated = _bounded_payload_diff(first_payload, second_payload)
        diagnostic = {
            "schema": "gt.brief_determinism_mismatch.v1",
            "canonical_sha256": identities,
            "difference_count": len(differences),
            "differences_truncated": truncated,
            "differences": differences,
        }
        _atomic_write_payload(
            os.path.join(out_dir, BRIEF_DETERMINISM_DIAGNOSTIC_BASENAME),
            diagnostic,
        )
        return verdict

    diagnostic_path = os.path.join(out_dir, BRIEF_DETERMINISM_DIAGNOSTIC_BASENAME)
    try:
        os.unlink(diagnostic_path)
    except FileNotFoundError:
        pass

    current = load_cached_brief(out_dir, expect_identity=expect_identity)
    if current is None or _canonical_acquisition_sha256(current) != first_sha:
        raise ValueError("brief determinism: primary cache changed before witness join")
    metrics = current.get("metrics")
    proofs = metrics.get("localization_proof") if isinstance(metrics, dict) else None
    if not isinstance(proofs, list):
        raise ValueError("brief determinism: localization proof is not a list")
    witness = {
        "kind": "repeat_identity",
        "runs": 2,
        "canonical_sha256": identities,
    }
    # A deterministic acquisition may honestly produce a non-empty brief with no
    # localized file candidate (for example a new-file task). Repeat identity is
    # still a run-level fact; candidate-local copies exist only when candidates do.
    for proof in proofs:
        if not isinstance(proof, dict):
            raise ValueError("brief determinism: malformed candidate proof")
        sources = proof.setdefault("acquisition_sources", {})
        if not isinstance(sources, dict):
            raise ValueError("brief determinism: malformed acquisition sources")
        sources["determinism"] = dict(witness)
    metrics["determinism_witness"] = dict(witness)
    _atomic_write_payload(cache_path(out_dir), current)
    return verdict


def _graph_state_sha256(graph: str) -> str:
    """Hash the SQLite database and its committed WAL carrier, if present."""
    state = hashlib.sha256()
    found = False
    for suffix in ("", "-wal"):
        path = f"{graph}{suffix}"
        try:
            size = os.path.getsize(path)
            state.update(suffix.encode("ascii"))
            state.update(str(size).encode("ascii"))
            state.update(b"\x00")
            with open(path, "rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    state.update(chunk)
            found = True
        except OSError:
            if suffix == "":
                state.update(b"nostat")
    return state.hexdigest() if found else "nostat"


@dataclass(frozen=True)
class RequestIdentityHandoff:
    """One immutable graph snapshot identity shared across an emit operation."""

    value: str
    issue_text: str
    graph: str
    graph_state_sha256: str


def capture_request_identity(issue_text: str, graph: str) -> RequestIdentityHandoff:
    graph_state = _graph_state_sha256(graph)
    h = hashlib.sha256()
    h.update((issue_text or "").encode("utf-8", "replace"))
    h.update(b"\x00")
    h.update(os.path.abspath(graph).encode("utf-8", "replace"))
    h.update(b"\x00")
    h.update(graph_state.encode("ascii"))
    return RequestIdentityHandoff(
        value=h.hexdigest(), issue_text=issue_text or "", graph=graph,
        graph_state_sha256=graph_state,
    )


def validate_request_identity(handoff: RequestIdentityHandoff) -> None:
    """Fail closed if the graph (including WAL state) changed during the handoff."""
    if _graph_state_sha256(handoff.graph) != handoff.graph_state_sha256:
        raise ValueError("brief determinism: graph changed during identity handoff")


def request_identity(issue_text: str, graph: str) -> str:
    """Identity of a brief request, bound to issue text and exact graph bytes.

    A different issue or any rebuilt graph, including a same-size replacement, must
    miss and regenerate. This
    is the guard against cross-task contamination: a reused ``out_dir`` (e.g. a codespace
    that runs several tasks through one /tmp/gt) must NOT serve a prior task's brief."""
    return capture_request_identity(issue_text, graph).value


def load_cached_brief(out_dir: str, expect_identity: Optional[str] = None) -> Optional[dict]:
    """Return the persisted ``{schema, brief_text, brief_sha256, identity, metrics}`` dict,
    or None on any miss/error (fail-safe — the caller then regenerates). FAIL CLOSED: when
    ``expect_identity`` is given, a cached brief whose stored ``identity`` does not match is
    treated as a MISS — a stale brief from a DIFFERENT task is NEVER served (the awilix↔geo
    contamination, 2026-06-15)."""
    p = cache_path(out_dir)
    try:
        if os.path.isfile(p):
            with open(p, encoding="utf-8") as fh:
                d = json.load(fh)
            if isinstance(d, dict) and (d.get("brief_text") or "").strip():
                if expect_identity is not None and d.get("identity") != expect_identity:
                    return None  # stale: cached brief is for a different issue/graph
                return d
    except (OSError, ValueError):
        pass
    return None


def persist_brief(out_dir: str, brief_text: str, result_obj: Any = None,
                  identity: str = "") -> dict:
    """Best-effort persist of the brief text + sha + request identity + metrics for
    cross-process reuse. Returns the dict regardless of whether the write succeeded."""
    text = (brief_text or "").strip()
    d = {
        "schema": BRIEF_RESULT_SCHEMA,
        "brief_text": text,
        "brief_sha256": brief_sha256(text),
        "identity": identity,
        "metrics": _extract_metrics(result_obj),
    }
    try:
        _atomic_write_payload(cache_path(out_dir), d)
    except (OSError, TypeError):
        pass
    return d


def get_or_generate(out_dir: str, issue_text: str, work: str, graph: str,
                    generator: Any = None,
                    identity_handoff: RequestIdentityHandoff | None = None) -> dict:
    """Single-generation entry point. Returns
    ``{schema, brief_text, brief_sha256, metrics, generated: bool}``.

    Loads the persisted result if present (``generated=False`` — no second
    generation); otherwise generates via ``generator`` (default the real
    ``generate_v1r_brief``), persists it, and returns (``generated=True``).
    Raising is left to the caller's contract — ``generator`` exceptions propagate so
    the proof can fail closed on a brief that cannot be produced."""
    if identity_handoff is not None:
        if identity_handoff.issue_text != (issue_text or "") or identity_handoff.graph != graph:
            raise ValueError("brief cache: identity handoff request mismatch")
        ident = identity_handoff.value
    else:
        ident = request_identity(issue_text, graph)
    cached = load_cached_brief(out_dir, expect_identity=ident)
    if cached is not None:
        out = dict(cached)
        out["generated"] = False
        return out
    if generator is None:
        from groundtruth.pretask.v1r_brief import generate_v1r_brief as generator
    res = generator(issue_text=issue_text, repo_root=work, graph_db=graph, bug_id="portable")
    brief_text = (getattr(res, "brief_text", "") or "").strip()
    out = persist_brief(out_dir, brief_text, res, identity=ident)
    out["generated"] = True
    return out
