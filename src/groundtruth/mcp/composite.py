"""Composite GT pull endpoints + per-task BUDGET_EXHAUSTED counter (v1.0.5).

Three composite tools exposed via the OH MCP surface:

  gt_lookup <symbol>   — fan-in IMPORT (callees) + CALLER + TEST + PRECEDENT + TYPE
                          per gt_intel.compute_evidence; cap=2 calls/task
  gt_impact <target>   — fan-in IMPACT + SIBLING for a symbol; cap=2 calls/task
  gt_check  <file>     — file-level checks: imports/signatures/packages
                          validators + TEST coverage + structural neighbours;
                          cap=3 calls/task; serves as the pre-submit gate.

Per-task counter lives at ``/tmp/gt_calls_<instance_id>.json`` so the host
wrapper, hook, and pre-submit gate can all read the same state. When a cap
is hit, the impl returns a string starting with ``BUDGET_EXHAUSTED:`` with a
redirect to the next-best endpoint — the agent must NOT retry the same tool.

Confidence tiers in rendered output:
  [VERIFIED] — score >= 2 in compute_evidence (high confidence)
  [WARNING]  — score == 1
  [INFO]     — score == 0 or unknown
"""

from __future__ import annotations

import json
import os
import sqlite3
from typing import Any

_DEFAULT_COUNTER_DIR = "/tmp"

_CAPS: dict[str, int] = {
    "gt_lookup": 2,
    "gt_impact": 2,
    "gt_check": 3,
}

_REDIRECTS: dict[str, str] = {
    "gt_lookup": "gt_impact for blast radius / sibling norms on the same symbol",
    "gt_impact": "gt_lookup for caller/callee/test context on the same symbol",
    "gt_check": "you've used your gt_check budget — read the evidence already returned and finish",
}

_TIER_VERIFIED = "[VERIFIED]"
_TIER_WARNING = "[WARNING]"
_TIER_INFO = "[INFO]"


def _counter_path(instance_id: str) -> str:
    safe_id = instance_id.replace("/", "_").replace("..", "_")
    return os.path.join(_DEFAULT_COUNTER_DIR, f"gt_calls_{safe_id}.json")


def _load_counter(instance_id: str) -> dict[str, int]:
    path = _counter_path(instance_id)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return {k: int(v) for k, v in data.items() if isinstance(k, str)}
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return {}


def _save_counter(instance_id: str, counts: dict[str, int]) -> None:
    path = _counter_path(instance_id)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(counts, fh)
        os.replace(tmp, path)
    except OSError:
        pass


def check_and_increment(instance_id: str, endpoint: str) -> tuple[bool, str]:
    """Atomically check+bump the per-task counter for ``endpoint``.

    Returns ``(allowed, message)``. If allowed, message is "" and the caller
    proceeds to compute evidence. If not allowed, message is the
    BUDGET_EXHAUSTED redirect string the caller should return verbatim.
    """
    cap = _CAPS.get(endpoint)
    if cap is None:
        return True, ""
    counts = _load_counter(instance_id)
    used = counts.get(endpoint, 0)
    if used >= cap:
        redirect = _REDIRECTS.get(endpoint, "")
        return (
            False,
            f"BUDGET_EXHAUSTED: {endpoint} has reached its per-task cap of {cap}. Try {redirect}.",
        )
    counts[endpoint] = used + 1
    _save_counter(instance_id, counts)
    return True, ""


def _resolve_instance_id(explicit: str | None) -> str:
    if explicit and explicit.strip():
        return explicit.strip()
    env = os.environ.get("GT_INSTANCE_ID", "").strip()
    return env or "unknown"


def _tier_for_score(score: int) -> str:
    if score >= 2:
        return _TIER_VERIFIED
    if score == 1:
        return _TIER_WARNING
    return _TIER_INFO


def _import_gt_intel():
    """Import gt_intel from benchmarks.swebench, returning the module or None.

    The OH wrapper bundles the groundtruth package into /tmp/gt_src and the
    benchmarks dir into the same parent path, so both should be importable
    in-container. We probe rather than fail hard so the MCP server can still
    register tools even if the evidence engine isn't reachable.
    """
    try:
        from benchmarks.swebench import gt_intel  # type: ignore[import-not-found]

        return gt_intel
    except ImportError:
        return None


def _format_evidence_lines(
    evidence_nodes: list[Any],
    families_keep: set[str] | None = None,
    max_per_family: int = 3,
) -> list[str]:
    """Render compute_evidence() result list as tier-labeled lines.

    Filters to ``families_keep`` if provided; caps ``max_per_family`` per
    family so a single noisy family can't crowd out the others.
    """
    by_family: dict[str, list[Any]] = {}
    for node in evidence_nodes:
        family = getattr(node, "family", "")
        if families_keep is not None and family not in families_keep:
            continue
        by_family.setdefault(family, []).append(node)

    out: list[str] = []
    for family in sorted(by_family.keys()):
        for node in by_family[family][:max_per_family]:
            score = int(getattr(node, "score", 0))
            tier = _tier_for_score(score)
            name = getattr(node, "name", "?")
            file_path = getattr(node, "file", "?")
            line = getattr(node, "line", 0)
            summary = getattr(node, "summary", "")
            source_code = getattr(node, "source_code", "")
            head = f"{tier} {family} {name} @ {file_path}:{line}"
            if summary:
                head += f" — {summary}"
            out.append(head)
            if source_code:
                code = source_code.strip().splitlines()[0][:160]
                out.append(f"    {code}")
    return out


def _open_target(db_path: str, file_path: str, function_name: str = "") -> tuple[Any, Any] | None:
    """Open graph.db and resolve a target node. Returns (conn, node) or None."""
    if not os.path.exists(db_path):
        return None
    gt_intel = _import_gt_intel()
    if gt_intel is None:
        return None
    try:
        conn = sqlite3.connect(db_path)
    except sqlite3.Error:
        return None
    try:
        target = gt_intel.get_target_node(conn, file_path, function_name)
    except Exception:
        conn.close()
        return None
    if target is None:
        conn.close()
        return None
    return conn, target


def _format_block(title: str, lines: list[str]) -> str:
    if not lines:
        body = "(no admissible evidence — below confidence floor or no graph neighbours)"
    else:
        body = "\n".join(lines)
    return f'<gt-evidence tool="{title}">\n{body}\n</gt-evidence>'


# ---------------------------------------------------------------------------
# Endpoint implementations
# ---------------------------------------------------------------------------

_LOOKUP_FAMILIES = {"IMPORT", "CALLER", "TEST", "PRECEDENT", "TYPE"}
_IMPACT_FAMILIES = {"IMPACT", "SIBLING"}
_CHECK_FAMILIES = {"IMPORT", "TEST"}  # validators add CONTRACT/STRUCTURAL


def gt_lookup_impl(
    symbol: str,
    *,
    db_path: str,
    root_path: str,
    file_path: str = "",
    instance_id: str | None = None,
) -> str:
    """Composite endpoint: callers + callees + tests + precedent + type."""
    import time as _time

    _t0 = _time.monotonic()
    iid = _resolve_instance_id(instance_id)
    allowed, msg = check_and_increment(iid, "gt_lookup")
    if not allowed:
        _emit_endpoint_telemetry(
            iid, "gt_lookup", {"symbol": symbol, "file_path": file_path}, msg, _t0
        )
        return msg
    if not symbol or not symbol.strip():
        return _format_block("gt_lookup", ["[INFO] empty symbol — pass a function or class name"])

    opened = _open_target(db_path, file_path or "", symbol.strip())
    if opened is None:
        return _format_block(
            "gt_lookup",
            [f"[INFO] could not resolve symbol '{symbol}' against {db_path}"],
        )
    conn, target = opened
    try:
        gt_intel = _import_gt_intel()
        if gt_intel is None:
            return _format_block("gt_lookup", ["[INFO] evidence engine unavailable"])
        evidence_nodes = gt_intel.compute_evidence(conn, root_path, target)
    except Exception as exc:
        return _format_block("gt_lookup", [f"[INFO] compute_evidence failed: {exc}"])
    finally:
        try:
            conn.close()
        except Exception:
            pass

    lines = _format_evidence_lines(evidence_nodes, families_keep=_LOOKUP_FAMILIES)
    out = _format_block("gt_lookup", lines)
    _emit_endpoint_telemetry(
        iid, "gt_lookup", {"symbol": symbol, "file_path": file_path}, out, _t0, lines=lines
    )
    return out


def gt_impact_impl(
    target: str,
    *,
    db_path: str,
    root_path: str,
    file_path: str = "",
    instance_id: str | None = None,
) -> str:
    """Composite endpoint: blast radius (caller count + critical path) + sibling norms."""
    import time as _time

    _t0 = _time.monotonic()
    iid = _resolve_instance_id(instance_id)
    allowed, msg = check_and_increment(iid, "gt_impact")
    if not allowed:
        _emit_endpoint_telemetry(
            iid, "gt_impact", {"target": target, "file_path": file_path}, msg, _t0
        )
        return msg
    if not target or not target.strip():
        return _format_block("gt_impact", ["[INFO] empty target — pass a symbol or file"])

    opened = _open_target(db_path, file_path or "", target.strip())
    if opened is None:
        return _format_block(
            "gt_impact",
            [f"[INFO] could not resolve '{target}' against {db_path}"],
        )
    conn, node = opened
    try:
        gt_intel = _import_gt_intel()
        if gt_intel is None:
            return _format_block("gt_impact", ["[INFO] evidence engine unavailable"])
        evidence_nodes = gt_intel.compute_evidence(conn, root_path, node)
        try:
            total_callers, unique_files = gt_intel.get_all_callers_count(conn, node.id)
        except Exception:
            total_callers, unique_files = 0, 0
        critical = False
        try:
            critical = bool(gt_intel.is_critical_path(node.file_path))
        except Exception:
            pass
    except Exception as exc:
        return _format_block("gt_impact", [f"[INFO] compute_evidence failed: {exc}"])
    finally:
        try:
            conn.close()
        except Exception:
            pass

    lines: list[str] = []
    head_tier = _TIER_VERIFIED if total_callers >= 5 or critical else _TIER_WARNING
    lines.append(
        f"{head_tier} BLAST_RADIUS callers={total_callers} files={unique_files} "
        f"critical_path={'yes' if critical else 'no'}"
    )
    lines.extend(_format_evidence_lines(evidence_nodes, families_keep=_IMPACT_FAMILIES))
    out = _format_block("gt_impact", lines)
    _emit_endpoint_telemetry(
        iid, "gt_impact", {"target": target, "file_path": file_path}, out, _t0, lines=lines
    )
    return out


def gt_check_impl(
    file_path: str,
    *,
    db_path: str,
    root_path: str,
    instance_id: str | None = None,
) -> str:
    """File-level pre-submit check: validators + TEST coverage + import shape."""
    import time as _time

    _t0 = _time.monotonic()
    iid = _resolve_instance_id(instance_id)
    allowed, msg = check_and_increment(iid, "gt_check")
    if not allowed:
        _emit_endpoint_telemetry(iid, "gt_check", {"file_path": file_path}, msg, _t0)
        return msg
    if not file_path or not file_path.strip():
        return _format_block("gt_check", ["[INFO] empty path — pass a source file"])

    file_path = file_path.strip()
    abs_path = file_path if os.path.isabs(file_path) else os.path.join(root_path, file_path)
    if not os.path.exists(abs_path):
        return _format_block("gt_check", [f"[WARNING] file not found: {file_path}"])

    # Record gt_check coverage so the pre-submit gate can verify it.
    _record_check_coverage(iid, file_path)

    lines: list[str] = []

    # gt_check is intentionally graph-based (compute_evidence's IMPORT + TEST
    # families) rather than LSP-validator-based: ImportValidator,
    # SignatureValidator and PackageValidator all consume LSP diagnostics, and
    # running an LSP server in each SWE-Bench task container costs too much for
    # the v1.0.5 per-task budget. Keep this surface deterministic + cheap.
    try:
        import groundtruth.validators.orchestrator  # noqa: F401

        lines.append(
            f"{_TIER_INFO} VALIDATORS graph-based (LSP path skipped by design — "
            "see compute_evidence IMPORT/TEST output below)"
        )
    except ImportError:
        lines.append(f"{_TIER_INFO} VALIDATORS module unavailable")

    # TEST coverage + IMPORT shape via compute_evidence on each function in file
    if os.path.exists(db_path):
        try:
            conn = sqlite3.connect(db_path)
        except sqlite3.Error:
            conn = None
        if conn is not None:
            try:
                gt_intel = _import_gt_intel()
                rows = conn.execute(
                    "SELECT name, start_line FROM nodes "
                    "WHERE file_path = ? AND label IN ('Function','Method') "
                    "ORDER BY start_line LIMIT 5",
                    (file_path,),
                ).fetchall()
                for name, start_line in rows:
                    try:
                        target = (
                            gt_intel.get_target_node(conn, file_path, name) if gt_intel else None
                        )
                    except Exception:
                        target = None
                    if target is None:
                        continue
                    try:
                        ev_nodes = gt_intel.compute_evidence(conn, root_path, target)
                    except Exception:
                        continue
                    sub_lines = _format_evidence_lines(
                        ev_nodes, families_keep=_CHECK_FAMILIES, max_per_family=2
                    )
                    if sub_lines:
                        lines.append(f"  ── {name} @ {file_path}:{start_line} ──")
                        lines.extend(sub_lines)
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
    else:
        lines.append(f"{_TIER_INFO} graph.db missing at {db_path}")

    out = _format_block("gt_check", lines)
    _emit_endpoint_telemetry(iid, "gt_check", {"file_path": file_path}, out, _t0, lines=lines)
    return out


def _emit_endpoint_telemetry(
    instance_id: str,
    endpoint: str,
    args: dict,
    output: str,
    t0_monotonic: float,
    lines: list[str] | None = None,
) -> None:
    """Best-effort layer4 endpoint telemetry. Never raises."""
    try:
        import time as _time
        from groundtruth.runtime.v105_telemetry import log_endpoint

        latency_ms = (_time.monotonic() - t0_monotonic) * 1000.0
        tiers = {"verified": 0, "warning": 0, "info": 0}
        for ln in lines or []:
            if "[VERIFIED]" in ln:
                tiers["verified"] += 1
            elif "[WARNING]" in ln:
                tiers["warning"] += 1
            elif "[INFO]" in ln:
                tiers["info"] += 1
        counts = _load_counter(instance_id)
        cap = _CAPS.get(endpoint, 0)
        used = counts.get(endpoint, 0)
        log_endpoint(
            instance_id=instance_id,
            endpoint=endpoint,
            args=args,
            output=output,
            tier_distribution=tiers,
            budget_remaining=max(0, cap - used),
            latency_ms=latency_ms,
        )
    except Exception:
        pass


def _record_check_coverage(instance_id: str, file_path: str) -> None:
    """Record that gt_check ran against ``file_path`` for the pre-submit gate."""
    log_path = "/tmp/gt_check_log.jsonl"
    try:
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"instance_id": instance_id, "file": file_path}) + "\n")
    except OSError:
        pass
