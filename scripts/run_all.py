"""Master orchestrator for v7.4 holdout evaluation pipeline.

Runs all stages in the correct dependency order, with parallelism at every
stage where dependencies allow. A single command to run everything:

    python scripts/run_all.py [--work-dir D:/Groundtruth/.tmp_holdout] [--workers 8]

Stage dependency order:
  Stage 1 (parallel): enumerate_prs  +  v7.3 baseline smoke on tranche
  Stage 2 (parallel): mine_holdout (5 repo workers)  +  streaming baseline
  Stage 3 (fires at bug 20): calibrate_weights
  Stage 4 (calibration done + all 60 mined): run_v74_holdout --phase ablation
  Stage 5 (ablation done): run_v74_holdout --phase gates
  Stage 6 (gates pass): run_v74_holdout --phase step3

Each stage is launched as a subprocess. Stage transitions are detected by
polling for the existence of output files (5-second intervals).
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent
PYTHON = sys.executable


def _run(cmd: list[str], label: str) -> int:
    print(f"\n[{label}] $ {' '.join(cmd)}")
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )
    assert proc.stdout
    for line in proc.stdout:
        print(f"  [{label}] {line}", end="")
    proc.wait()
    print(f"  [{label}] exit={proc.returncode}")
    return proc.returncode


def _line_count(path: Path) -> int:
    if not path.exists():
        return 0
    count = 0
    with open(path) as f:
        for line in f:
            if line.strip():
                count += 1
    return count


def _wait_for_line_count(path: Path, target: int, label: str, poll_s: float = 10.0, timeout_s: float = 14400.0) -> bool:
    """Poll until JSONL file has at least `target` non-empty lines."""
    elapsed = 0.0
    while _line_count(path) < target:
        current = _line_count(path)
        print(f"  [{label}] waiting for {target} lines in {path.name} (have {current})...")
        time.sleep(poll_s)
        elapsed += poll_s
        if elapsed >= timeout_s:
            print(f"[{label}] TIMEOUT at {current}/{target} lines", file=sys.stderr)
            return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--work-dir", default="D:/Groundtruth/.tmp_holdout")
    parser.add_argument("--prs-out", default="holdout_prs.jsonl")
    parser.add_argument("--holdout-out", default="holdout_v1.jsonl")
    parser.add_argument("--baseline-out", default="results/baseline_holdout.csv")
    parser.add_argument("--coeff-out", default="results/coefficients_v1.json")
    parser.add_argument("--ablation-out", default="results/holdout_ablation.jsonl")
    parser.add_argument("--gate-out", default="results/gate_verdict.json")
    parser.add_argument("--step3-manifest", default="15tasks.jsonl")
    parser.add_argument("--step3-out", default="results/step3_results.csv")
    parser.add_argument("--gt-index-bin", default="")
    parser.add_argument("--workers", type=int, default=min(os.cpu_count() or 4, 8))
    parser.add_argument("--calibration-n", type=int, default=20)
    parser.add_argument("--skip-to", default="",
                        help="Skip to a specific stage: prs|mine|baseline|calibrate|ablation|gates|step3")
    args = parser.parse_args()

    prs_path = Path(args.prs_out)
    holdout_path = Path(args.holdout_out)
    baseline_path = Path(args.baseline_out)
    coeff_path = Path(args.coeff_out)
    ablation_path = Path(args.ablation_out)
    gate_path = Path(args.gate_out)
    step3_path = Path(args.step3_out)
    tranche_path = Path(".tmp_tranche/holdout_feasibility.jsonl")

    skip_to = args.skip_to.lower()

    # -------------------------------------------------------------------------
    # Stage 1: enumerate PRs + smoke baseline on tranche (parallel)
    # -------------------------------------------------------------------------
    if skip_to not in ("mine", "baseline", "calibrate", "ablation", "gates", "step3"):
        print("\n" + "="*60)
        print("Stage 1: enumerate PRs + smoke v7.3 baseline on tranche")
        print("="*60)

        import threading

        enum_rc = [0]
        base_rc = [0]

        def _enumerate() -> None:
            enum_rc[0] = _run(
                [PYTHON, str(HERE / "enumerate_prs.py"), "--out", str(prs_path)],
                "enum"
            )

        def _smoke_baseline() -> None:
            if not tranche_path.exists():
                print("  [smoke] tranche JSONL not found — skipping smoke baseline")
                return
            out = baseline_path.with_stem(baseline_path.stem + "_tranche_smoke")
            base_rc[0] = _run(
                [PYTHON, str(HERE / "run_baseline_v73.py"),
                 "--input", str(tranche_path),
                 "--out", str(out),
                 "--workers", str(args.workers)],
                "smoke_v73"
            )

        t_enum = threading.Thread(target=_enumerate)
        t_smoke = threading.Thread(target=_smoke_baseline)
        t_enum.start(); t_smoke.start()
        t_enum.join(); t_smoke.join()

        if enum_rc[0] != 0:
            print("Stage 1 FAILED: enumerate_prs returned non-zero", file=sys.stderr)
            return 1

    # -------------------------------------------------------------------------
    # Stages 2-4: mine + baseline + calibration + ablation — fully parallel
    #
    # Timeline:
    #   T=0           mine starts (5 repo workers)
    #   T=5 bugs      baseline streaming starts (concurrent with mine)
    #   T=20 bugs     calibration starts (concurrent with mine + baseline)
    #   T=cal_done    ablation starts on available eval bugs (concurrent with remaining mine)
    #   T=mine_done   baseline_finish mops up any rows not yet processed
    #   T=abl_done    gates run (sequential — needs full ablation)
    # -------------------------------------------------------------------------
    if skip_to not in ("baseline", "calibrate", "ablation", "gates", "step3"):
        print("\n" + "="*60)
        print("Stages 2-4: mine + baseline + calibration + ablation (fully parallel)")
        print("="*60)

        if not prs_path.exists():
            print(f"ERROR: {prs_path} not found — run enumerate_prs.py first", file=sys.stderr)
            return 1

        import threading

        mine_rc = [0]
        cal_rc  = [0]
        abl_rc  = [0]

        def _mine() -> None:
            mine_rc[0] = _run(
                [PYTHON, str(HERE / "mine_holdout.py"),
                 "--prs", str(prs_path),
                 "--out", str(holdout_path),
                 "--work-dir", args.work_dir,
                 *(["--gt-index-bin", args.gt_index_bin] if args.gt_index_bin else [])],
                "mine"
            )

        def _calibrate_when_ready() -> None:
            if not _wait_for_line_count(holdout_path, args.calibration_n,
                                         "calibrate", poll_s=15.0, timeout_s=7200.0):
                print("Stage 3 FAILED: not enough bugs mined", file=sys.stderr)
                cal_rc[0] = 1
                return
            cal_rc[0] = _run(
                [PYTHON, str(HERE / "calibrate_weights.py"),
                 "--input", str(holdout_path),
                 "--first-n", str(args.calibration_n),
                 "--out-json", str(coeff_path),
                 "--workers", str(args.workers)],
                "calibrate"
            )

        def _ablate_when_ready() -> None:
            # Wait for calibration to finish (coeff file appears)
            elapsed = 0.0
            while not coeff_path.exists():
                time.sleep(10)
                elapsed += 10
                if elapsed > 7200:
                    print("Stage 4 FAILED: calibration never completed", file=sys.stderr)
                    abl_rc[0] = 1
                    return
            # Start ablation immediately on whatever eval bugs are available;
            # run_v74_holdout supports --resume so we can call it once more later
            # if bugs continue to arrive after it exits.
            abl_rc[0] = _run(
                [PYTHON, str(HERE / "run_v74_holdout.py"),
                 "--phase", "ablation",
                 "--holdout", str(holdout_path),
                 "--coefficients", str(coeff_path),
                 "--ablation-out", str(ablation_path),
                 "--workers", str(args.workers),
                 "--calibration-n", str(args.calibration_n)],
                "ablation"
            )

        # Launch all four concurrent threads
        t_mine = threading.Thread(target=_mine, name="mine")
        t_cal  = threading.Thread(target=_calibrate_when_ready, name="calibrate")
        t_abl  = threading.Thread(target=_ablate_when_ready, name="ablate")
        t_mine.start()
        t_cal.start()
        t_abl.start()

        # Baseline stream: start once 5 bugs are available, run in main thread
        if _wait_for_line_count(holdout_path, 5, "baseline", poll_s=15.0, timeout_s=3600.0):
            _run(
                [PYTHON, str(HERE / "run_baseline_v73.py"),
                 "--input", str(holdout_path),
                 "--out", str(baseline_path),
                 "--workers", str(args.workers),
                 "--resume"],
                "baseline_stream"
            )

        # Wait for mining to finish, then mop up any remaining baseline rows
        t_mine.join()
        if mine_rc[0] != 0:
            print("WARNING: mine_holdout returned non-zero", file=sys.stderr)
        _run(
            [PYTHON, str(HERE / "run_baseline_v73.py"),
             "--input", str(holdout_path),
             "--out", str(baseline_path),
             "--workers", str(args.workers),
             "--resume"],
            "baseline_finish"
        )

        # Wait for calibration + ablation to finish
        t_cal.join()
        if cal_rc[0] != 0:
            print("Stage 3 FAILED: calibrate_weights returned non-zero", file=sys.stderr)
            return 1
        t_abl.join()

        # Second ablation pass to pick up any eval bugs that arrived after
        # the first pass exited (mining may have still been running)
        _run(
            [PYTHON, str(HERE / "run_v74_holdout.py"),
             "--phase", "ablation",
             "--holdout", str(holdout_path),
             "--coefficients", str(coeff_path),
             "--ablation-out", str(ablation_path),
             "--workers", str(args.workers),
             "--calibration-n", str(args.calibration_n)],
            "ablation_finish"
        )
        if abl_rc[0] != 0:
            print("Stage 4 FAILED: ablation returned non-zero", file=sys.stderr)
            return 1

    # -------------------------------------------------------------------------
    # Stage 5: gate check
    # -------------------------------------------------------------------------
    if skip_to != "step3":
        print("\n" + "="*60)
        print("Stage 5: gate check")
        print("="*60)

        if not baseline_path.exists():
            print(f"ERROR: baseline not found at {baseline_path}", file=sys.stderr)
            return 1

        rc = _run(
            [PYTHON, str(HERE / "run_v74_holdout.py"),
             "--phase", "gates",
             "--holdout", str(holdout_path),
             "--baseline", str(baseline_path),
             "--ablation-out", str(ablation_path),
             "--gate-out", str(gate_path),
             "--calibration-n", str(args.calibration_n)],
            "gates"
        )

    # -------------------------------------------------------------------------
    # Stage 6: step 3 (only if gates passed)
    # -------------------------------------------------------------------------
    print("\n" + "="*60)
    print("Stage 6: step 3 on original 15 tasks")
    print("="*60)

    rc = _run(
        [PYTHON, str(HERE / "run_v74_holdout.py"),
         "--phase", "step3",
         "--holdout", str(holdout_path),
         "--baseline", str(baseline_path),
         "--coefficients", str(coeff_path),
         "--gate-out", str(gate_path),
         "--ablation-out", str(ablation_path),
         "--step3-manifest", args.step3_manifest,
         "--step3-out", str(step3_path),
         "--calibration-n", str(args.calibration_n)],
        "step3"
    )

    print("\n" + "="*60)
    print("Pipeline complete.")
    for label, path in [
        ("PR list", prs_path),
        ("Holdout", holdout_path),
        ("Baseline", baseline_path),
        ("Coefficients", coeff_path),
        ("Ablation", ablation_path),
        ("Gates", gate_path),
        ("Step 3", step3_path),
    ]:
        exists = "✅" if path.exists() else "❌ missing"
        print(f"  {exists}  {label}: {path}")
    print("="*60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
