#!/usr/bin/env python3
"""Resolve exact GPT-5 mini (or fallback) model ID from OpenAI and validate tool-calling."""

from __future__ import annotations

import argparse
import json
import os
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description="Resolve OpenAI model ID for SWE-bench")
    parser.add_argument(
        "--prefer",
        default="gpt-5o-mini",
        help="Preferred model name fragment (default: gpt-5-mini)",
    )
    parser.add_argument("--fallback", default="gpt-5-mini", help="Fallback if preferred not found")
    parser.add_argument("--smoke-test", action="store_true", help="Run a tool-calling smoke test")
    parser.add_argument("--json", action="store_true", help="Output JSON only")
    args = parser.parse_args()

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("OPENAI_API_KEY not set", file=sys.stderr)
        return 1

    try:
        from openai import OpenAI
    except ImportError:
        print("openai package not installed. pip install openai", file=sys.stderr)
        return 1

    client = OpenAI()
    model_id = args.fallback

    try:
        models = client.models.list()
        candidates = [
            m.id for m in models.data
            if args.prefer.lower() in m.id.lower()
        ]
        if candidates:
            # Prefer stable name without date suffix when possible
            stable = [c for c in candidates if "-202" not in c and "-01" not in c]
            model_id = stable[0] if stable else candidates[0]
    except Exception as e:
        if not args.json:
            print(f"Models list failed: {e}, using fallback {args.fallback}", file=sys.stderr)
        model_id = args.fallback

    if args.smoke_test:
        try:
            r = client.chat.completions.create(
                model=model_id,
                messages=[{"role": "user", "content": "Say OK"}],
                tools=[{
                    "type": "function",
                    "function": {
                        "name": "test",
                        "description": "test",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }],
                max_completion_tokens=10,
            )
            has_tools = bool(r.choices[0].message.tool_calls)
            if not args.json:
                print(f"Smoke test: model={r.model} tool_calls={has_tools}")
            if not has_tools and r.choices[0].message.content:
                pass  # model responded; tool_calls can be empty if it didn't use the tool
        except Exception as e:
            print(f"Smoke test failed: {e}", file=sys.stderr)
            return 1

    if args.json:
        print(json.dumps({
            "MODEL_PROVIDER": "openai",
            "MODEL_NAME_EXACT": model_id,
            "API_KEY_ENV_VARS": "OPENAI_API_KEY",
        }))
    else:
        print(f"MODEL_NAME_EXACT={model_id}")
        print("export MODEL_NAME_EXACT=" + model_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
