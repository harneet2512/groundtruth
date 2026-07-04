"""MCP proof dataclass, validation logic, and validity rules for SWE-bench runs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .mcp_bridge import MCPProof, SUBSTANTIVE_TOOLS

__all__ = [
    "MCPProof",
    "SUBSTANTIVE_TOOLS",
    "validate_proof_from_dir",
    "is_valid_proof",
    "verify_gt_usage_passive",
]


def verify_gt_usage_passive(gt_report: dict[str, Any]) -> bool:
    """Verify that passive GT integration was active during a V2 run.

    Validity criteria:
    - gt_available is True
    - context_tokens_injected > 0
    - index_symbols > 0
    """
    instr = gt_report.get("instrumentation", {})
    if not instr.get("gt_available", False):
        return False
    if int(instr.get("context_tokens_injected", 0)) <= 0:
        return False
    if int(instr.get("index_symbols", 0)) <= 0:
        return False
    return True


def is_valid_proof(proof: MCPProof) -> bool:
    """Apply validity rules: connection_ok and at least one substantive tool call."""
    if not proof.connection_ok:
        return False
    if proof.substantive_tool_count < 1:
        return False
    return True


def validate_proof_from_dir(proof_dir: Path) -> tuple[bool, str, MCPProof | None]:
    """
    Read mcp_usage.json from proof_dir (or proof_dir/proof/<instance_id>/) and validate.

    Returns:
        (valid, message, proof_or_none)
    """
    usage_file = proof_dir / "mcp_usage.json"
    if not usage_file.exists():
        # Check for proof/<instance_id>/mcp_usage.json layout
        proof_sub = proof_dir / "proof"
        if proof_sub.is_dir():
            subdirs = [d for d in proof_sub.iterdir() if d.is_dir()]
            if not subdirs:
                return False, "proof/ dir empty", None
            valid_count = 0
            invalid_reasons: list[str] = []
            for d in subdirs:
                uf = d / "mcp_usage.json"
                if uf.exists():
                    v, msg, _ = validate_proof_from_dir(d)
                    if v:
                        valid_count += 1
                    else:
                        invalid_reasons.append(f"{d.name}: {msg}")
                else:
                    invalid_reasons.append(f"{d.name}: no mcp_usage.json")
            total = len(subdirs)
            if valid_count >= total * 0.9:
                msg = f"{valid_count}/{total} task proofs valid ({len(invalid_reasons)} invalid)"
                return True, msg, None
            return False, f"only {valid_count}/{total} valid; " + "; ".join(invalid_reasons[:5]), None
        # Single subdir (e.g. direct instance_id dir)
        subdirs = [d for d in proof_dir.iterdir() if d.is_dir()]
        if len(subdirs) == 1:
            usage_file = subdirs[0] / "mcp_usage.json"
        elif subdirs:
            valid_count = 0
            invalid_reasons = []
            for d in subdirs:
                v, msg, _ = validate_proof_from_dir(d)
                if v:
                    valid_count += 1
                else:
                    invalid_reasons.append(f"{d.name}: {msg}")
            total = len(subdirs)
            if valid_count >= total * 0.9:
                msg = f"{valid_count}/{total} task proofs valid ({len(invalid_reasons)} invalid)"
                return True, msg, None
            return False, f"only {valid_count}/{total} valid; " + "; ".join(invalid_reasons[:5]), None
        else:
            return False, "no mcp_usage.json or proof subdirs found", None

    try:
        data = json.loads(usage_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        return False, f"failed to read mcp_usage.json: {e}", None

    proof = _proof_from_dict(data)
    valid = is_valid_proof(proof)
    if valid:
        return True, "valid", proof
    reason = proof.invalid_run_reason or "connection_ok=False or no substantive tool calls"
    return False, reason, proof


def _proof_from_dict(data: dict[str, Any]) -> MCPProof:
    """Build MCPProof from mcp_usage.json dict."""
    tool_calls = data.get("tool_calls", [])
    if isinstance(tool_calls, list) and tool_calls and isinstance(tool_calls[0], dict):
        pass
    else:
        tools_called = data.get("tools_called", [])
        tool_calls = [{"name": n, "success": True} for n in tools_called]

    successful = data.get("successful_tool_calls", 0)
    failed = data.get("failed_tool_calls", 0)
    substantive = data.get("substantive_tool_count", 0)
    if substantive == 0 and tool_calls:
        substantive = sum(1 for t in tool_calls if t.get("name") in SUBSTANTIVE_TOOLS and t.get("success"))

    return MCPProof(
        mcp_enabled=data.get("mcp_enabled", True),
        connection_ok=data.get("connection_ok", False),
        tools_discovered=data.get("tools_discovered", []),
        tool_calls=tool_calls,
        successful_tool_calls=successful,
        failed_tool_calls=failed,
        substantive_tool_count=substantive,
        valid=data.get("valid", False),
        invalid_run_reason=data.get("invalid_run_reason"),
        mcp_server_command=data.get("mcp_server_command", ""),
        mcp_server_root=data.get("mcp_server_root", ""),
        worker_id=data.get("worker_id", 0),
        shard_id=data.get("shard_id", 0),
        model_name_exact=data.get("model_name_exact", ""),
    )
