#!/usr/bin/env python3
"""Run A/B benchmark from a config file; optionally upload results to S3.

Usage:
  python scripts/run_benchmark_with_config.py [--config benchmark_config.example.json]
  python scripts/run_benchmark_with_config.py --config my_config.json --upload
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _login_gate() -> None:
    """Ask user to confirm login/credentials before starting work."""
    env_file = ROOT / ".env"
    has_env = env_file.exists()
    api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if has_env and not api_key:
        try:
            from dotenv import load_dotenv
            load_dotenv(env_file)
            api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
        except ImportError:
            pass
    if not api_key:
        print("No OPENAI_API_KEY or ANTHROPIC_API_KEY found in environment or .env.")
        print("Set one of these to run the benchmark (LLM calls required).")
    print()
    print("Log in / set credentials to start the work.")
    input("Press Enter when ready to continue... ")
    print()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        type=Path,
        default=ROOT / "benchmarks" / "ab" / "benchmark_config.example.json",
        help="Path to benchmark config JSON",
    )
    parser.add_argument(
        "--upload",
        action="store_true",
        help="Upload results to S3 if config has s3_uri",
    )
    parser.add_argument(
        "--no-login",
        action="store_true",
        help="Skip login/credentials prompt (e.g. for CI)",
    )
    args = parser.parse_args()

    if not args.no_login:
        _login_gate()

    if not args.config.is_file():
        print(f"Config not found: {args.config}", file=sys.stderr)
        return 1

    with open(args.config, encoding="utf-8") as f:
        config = json.load(f)

    condition = config.get("condition", "both")
    fixture = config.get("fixture", "python")
    output_dir = config.get("output_dir", "benchmarks/ab/results")
    output_path = ROOT / output_dir
    output_path.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable, "-m", "benchmarks.ab.harness",
        "--condition", condition,
        "--fixture", fixture,
        "--output-dir", str(output_path),
    ]
    if config.get("model"):
        cmd.extend(["--model", config["model"]])
    if config.get("temperature") is not None:
        cmd.extend(["--temperature", str(config["temperature"])])
    if config.get("max_tokens") is not None:
        cmd.extend(["--max-tokens", str(config["max_tokens"])])

    print("Running:", " ".join(cmd))
    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode != 0:
        return result.returncode

    s3_uri = config.get("s3_uri") or os.environ.get("S3_BUCKET")
    if args.upload and s3_uri:
        run_id = str(uuid.uuid4())
        prefix = f"benchmark-runs/{run_id}"
        bucket = s3_uri.replace("s3://", "").split("/")[0]
        prefix_uri = f"s3://{bucket}/{prefix}"
        for name in ("no_mcp.json", "with_groundtruth_mcp.json"):
            p = output_path / name
            if p.exists():
                subprocess.run(
                    ["aws", "s3", "cp", str(p), f"{prefix_uri}/{name}"],
                    cwd=ROOT,
                    check=False,
                )
        print(f"Uploaded to {prefix_uri}")
    elif args.upload and not s3_uri:
        print("Set s3_uri in config or S3_BUCKET env to upload.", file=sys.stderr)

    print("Compare: python -m benchmarks.ab.compare --results-dir", output_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
