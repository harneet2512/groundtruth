"""Pre-launch gate for the 20-task baseline calibration.

Validates the manifest before ANY shard is launched:
  1. Manifest file exists and parses.
  2. selected has exactly manifest["n"] entries, no duplicates.
  3. Current dataset pool still contains every selected id (catches dataset drift).
  4. CAL_A config file hash matches `--expected-config-sha256` if supplied.

Exit 0 on pass, non-zero on any failure. Launcher must `set -e` and bail.

Usage:
  python scripts/cal_manifest_check.py benchmarks/swebench/cal20_live_lite.manifest.json \
      [--config configs/cal_a_gemini_3_1_pro_live_lite.yaml] \
      [--expected-config-sha256 HEX] \
      [--skip-dataset-check]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _fail(msg: str) -> None:
    sys.stderr.write(f"FAIL: {msg}\n")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("manifest", help="Path to cal20_live_lite.manifest.json")
    p.add_argument("--config", default=None, help="Path to CAL_A config YAML (for hash check)")
    p.add_argument("--expected-config-sha256", default=None)
    p.add_argument(
        "--skip-dataset-check",
        action="store_true",
        help="Skip live dataset re-load (offline/CI).",
    )
    args = p.parse_args()

    errors: list[str] = []

    manifest_path = Path(args.manifest)
    if not manifest_path.exists():
        _fail(f"manifest not found: {manifest_path}")
        return 2

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        _fail(f"manifest is not valid JSON: {e}")
        return 2

    selected = manifest.get("selected") or []
    n = manifest.get("n")
    if n is None or n != len(selected):
        errors.append(f"manifest.n ({n}) != len(selected) ({len(selected)})")
    if len(set(selected)) != len(selected):
        dups = {i for i in selected if selected.count(i) > 1}
        errors.append(f"duplicate instance_ids in selected: {sorted(dups)}")

    if not args.skip_dataset_check:
        try:
            from datasets import load_dataset
        except ImportError:
            errors.append("`datasets` package not installed; cannot verify dataset drift")
        else:
            dataset = manifest.get("dataset")
            split = manifest.get("split")
            if not dataset or not split:
                errors.append("manifest missing dataset/split keys")
            else:
                try:
                    ds = load_dataset(dataset, split=split)
                    pool = {row["instance_id"] for row in ds}
                    missing = [iid for iid in selected if iid not in pool]
                    if missing:
                        errors.append(
                            f"{len(missing)} selected ids no longer in dataset pool: "
                            f"{missing[:5]}{'...' if len(missing) > 5 else ''}"
                        )
                    pool_hash = hashlib.sha256(
                        "\n".join(sorted(pool)).encode()
                    ).hexdigest()[:16]
                    expected_pool_hash = manifest.get("pool_hash")
                    if expected_pool_hash and pool_hash != expected_pool_hash:
                        # drift is informational, not blocking, as long as selected ids still exist
                        sys.stderr.write(
                            f"WARN: dataset pool_hash drift "
                            f"(manifest={expected_pool_hash}, now={pool_hash})\n"
                        )
                except Exception as e:  # noqa: BLE001
                    errors.append(f"dataset load failed: {e}")

    if args.config:
        cfg_path = Path(args.config)
        if not cfg_path.exists():
            errors.append(f"config not found: {cfg_path}")
        elif args.expected_config_sha256:
            actual = _sha256_file(cfg_path)
            if actual != args.expected_config_sha256:
                errors.append(
                    f"config sha256 mismatch: expected {args.expected_config_sha256}, got {actual}"
                )
        else:
            print(f"config sha256: {_sha256_file(cfg_path)}  (pin this for later runs)")

    if errors:
        for e in errors:
            _fail(e)
        return 1

    print(f"OK: manifest has {len(selected)} unique ids, dataset reachable, config present.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
