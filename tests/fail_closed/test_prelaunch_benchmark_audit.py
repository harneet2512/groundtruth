import subprocess
import sys


SCRIPT = "scripts/ci/prelaunch_benchmark_audit.py"
DIGEST = "ghcr.io/hbali-stack/gt-substrate@sha256:55f18a1c99431e334ce637e30c7f6147078c4a9dc8a1a8c92ceb5fd268ea05f9"


def _run(*args):
    return subprocess.run(
        [sys.executable, SCRIPT, *args],
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_pro_on_harneet_requires_explicit_substrate_digest():
    result = _run(
        "--surface", "pro",
        "--repo", "harneet2512/groundtruth",
        "--ref", "codex/prevent-benchmark-harness-regressions",
        "--mode", "full",
        "--shard", "1/3",
        "--require-pinned-substrate", "1",
    )

    assert result.returncode != 0
    assert "PRELAUNCH_GT_SUBSTRATE_DIGEST_MISSING" in result.stderr
    assert "pass --gt-substrate-digest explicitly" in result.stderr


def test_mutable_substrate_reference_is_rejected():
    result = _run(
        "--surface", "deepswe",
        "--repo", "hbali-stack/groundtruth",
        "--ref", "codex/prevent-benchmark-harness-regressions",
        "--gt-substrate-digest", "ghcr.io/hbali-stack/gt-substrate:latest",
        "--require-pinned-substrate", "1",
    )

    assert result.returncode != 0
    assert "PRELAUNCH_GT_SUBSTRATE_DIGEST_INVALID" in result.stderr


def test_pro_full_requires_shard_before_dispatch():
    result = _run(
        "--surface", "pro",
        "--repo", "harneet2512/groundtruth",
        "--ref", "codex/prevent-benchmark-harness-regressions",
        "--gt-substrate-digest", DIGEST,
        "--mode", "full",
        "--require-pinned-substrate", "1",
    )

    assert result.returncode != 0
    assert "PRELAUNCH_PRO_FULL_SHARD_MISSING" in result.stderr


def test_expected_head_sha_rejects_wrong_explicit_ref():
    result = _run(
        "--surface", "deepswe",
        "--repo", "hbali-stack/groundtruth",
        "--ref", "0" * 40,
        "--expected-head-sha", "1" * 40,
        "--gt-substrate-digest", DIGEST,
        "--require-pinned-substrate", "1",
    )

    assert result.returncode != 0
    assert "PRELAUNCH_REF_SHA_MISMATCH" in result.stderr


def test_expected_head_sha_allows_matching_explicit_ref():
    sha = "1" * 40
    result = _run(
        "--surface", "deepswe",
        "--repo", "hbali-stack/groundtruth",
        "--ref", sha,
        "--expected-head-sha", sha,
        "--gt-substrate-digest", DIGEST,
        "--max-parallel", "20",
        "--require-pinned-substrate", "1",
    )

    assert result.returncode == 0, result.stderr
    assert "PRELAUNCH_AUDIT_PASS" in result.stdout


def test_current_pro_dispatch_plan_passes_prelaunch_audit():
    result = _run(
        "--surface", "pro",
        "--repo", "harneet2512/groundtruth",
        "--ref", "codex/prevent-benchmark-harness-regressions",
        "--gt-substrate-digest", DIGEST,
        "--mode", "full",
        "--shard", "1/3",
        "--max-parallel", "20",
        "--require-pinned-substrate", "1",
    )

    assert result.returncode == 0, result.stderr
    assert "PRELAUNCH_AUDIT_PASS" in result.stdout


def test_current_deepswe_dispatch_plan_passes_prelaunch_audit():
    result = _run(
        "--surface", "deepswe",
        "--repo", "hbali-stack/groundtruth",
        "--ref", "codex/prevent-benchmark-harness-regressions",
        "--gt-substrate-digest", DIGEST,
        "--max-parallel", "20",
        "--require-pinned-substrate", "1",
    )

    assert result.returncode == 0, result.stderr
    assert "PRELAUNCH_AUDIT_PASS" in result.stdout
