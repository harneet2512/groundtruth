"""Register the Vertex MaaS Qwen3-Coder model with litellm cost tables.

This must be importable so Track D's smoke runner can call it before
SWE-agent starts. SWE-agent's `LiteLLMModel.__init__` will *also* call
`litellm.register_model` if `agent.model.litellm_model_registry` points
at a JSON file (see `gt_track4.yaml`). This module is the Python-side
equivalent and the canonical source of truth for the cost table.

Numbers per CLAUDE.md (verified 2026-05-05):
  input  $0.45 / 1M tokens
  output $1.80 / 1M tokens
  context window 262144 in / 65536 out (per Track 4 plan, max_iterations=100)
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict

# Vertex MaaS publisher slug used both in the OpenAI-style endpoint and
# as the litellm model key.
MODEL_NAME = "openai/qwen3-coder-480b-a35b-instruct-maas"

COST_TABLE: Dict[str, Dict[str, Any]] = {
    MODEL_NAME: {
        "input_cost_per_token": 0.45e-6,
        "output_cost_per_token": 1.80e-6,
        "max_input_tokens": 262144,
        "max_output_tokens": 65536,
        "litellm_provider": "openai",
        "mode": "chat",
    }
}


def register() -> None:
    """Register the cost table with litellm in-process.

    Idempotent — safe to call repeatedly. If litellm is not importable,
    this raises so the caller surfaces the issue rather than silently
    running with $0/token (which would defeat per_instance_cost_limit).
    """
    import litellm  # noqa: F401  (local import — keep optional at module load)
    litellm.register_model(COST_TABLE)


def write_registry_json(path: str | os.PathLike[str]) -> Path:
    """Write the cost table to a JSON file that SWE-agent's
    `litellm_model_registry` field can consume.

    Returns the resolved Path.
    """
    out = Path(path).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(COST_TABLE, indent=2), encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in {"--write-json", "-w"}:
        target = argv[1] if len(argv) > 1 else "config/gt_track4_litellm_registry.json"
        out = write_registry_json(target)
        print(f"wrote {out}")
        return 0
    register()
    print(f"registered {MODEL_NAME} with litellm")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
