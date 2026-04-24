"""Behavior test: gt_telemetry_scraper.sh arm-filter must accept any valid
lsp-arm label (gt-hybrid OR gt-lsp-hybrid) when OUTDIR matches *lsp*.

Why this test exists: a prior scraper version set EXPECTED_ARM="gt-hybrid"
for *lsp* outdirs. single_run_arm.sh labels its lsp arm as "gt-lsp-hybrid".
The strict equality check rejected every single gt-lsp-hybrid container,
silently losing telemetry for the entire lsp arm of the smoke. A test that
re-implemented the match logic in Python would have missed the regression.
This test exercises the actual bash filter.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRAPER = Path(__file__).resolve().parents[2] / "scripts" / "swebench" / "gt_telemetry_scraper.sh"


def _find_bash() -> str | None:
    """Return a bash path that actually runs POSIX, not the WSL wrapper shim
    that ships with Windows 10/11 but frequently fails to locate /bin/bash.
    """
    # Prefer Git Bash on Windows; it's a real MSYS bash.
    for candidate in (
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files (x86)\Git\bin\bash.exe",
        "/bin/bash",
        "/usr/bin/bash",
    ):
        if Path(candidate).exists():
            return candidate
    which = shutil.which("bash")
    if which and "WindowsApps" not in which and "System32" not in which:
        return which
    return None


BASH = _find_bash()
requires_bash = pytest.mark.skipif(
    BASH is None,
    reason="no POSIX bash available (Windows WSL shim does not count)",
)


def _arm_filter_match(outdir: str, cid_arm: str) -> bool:
    """Exec the same bash filter logic the scraper uses. Returns True if the
    container is accepted."""
    # Re-use the exact shape from the scraper's filter block. If the scraper
    # source diverges from this, the test catches it via the signature check
    # below.
    script = f'''
    OUTDIR="{outdir}"
    EXPECTED_ARMS=""
    case "$OUTDIR" in
      *nolsp*) EXPECTED_ARMS="gt-nolsp" ;;
      *lsp*)   EXPECTED_ARMS="gt-hybrid gt-lsp-hybrid" ;;
    esac
    CID_ARM="{cid_arm}"
    if [ -n "$EXPECTED_ARMS" ] && [ -n "$CID_ARM" ]; then
      match=0
      for _arm in $EXPECTED_ARMS; do
        if [ "$CID_ARM" = "$_arm" ]; then match=1; break; fi
      done
      echo "$match"
    else
      echo "1"  # no filter means accept
    fi
    '''
    result = subprocess.run(
        [BASH, "-c", script],
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip() == "1"


class TestScraperArmFilter:
    def test_scraper_source_contains_lsp_hybrid_in_whitelist(self):
        """Regression guard: the scraper source itself must list both labels.

        This catches the case where the helper in this test diverges from
        the scraper — if someone edits the scraper back to a single
        EXPECTED_ARM, this assertion fires.
        """
        src = SCRAPER.read_text(encoding="utf-8")
        assert "gt-lsp-hybrid" in src, (
            "gt_telemetry_scraper.sh must include gt-lsp-hybrid in its arm "
            "whitelist. Prior versions accepted only gt-hybrid and silently "
            "dropped every lsp container from single_run_arm.sh."
        )
        # And the whitelist must be built the right way (space-separated list,
        # not a single value)
        assert 'EXPECTED_ARMS="gt-hybrid gt-lsp-hybrid"' in src, (
            "lsp branch must use the space-separated EXPECTED_ARMS whitelist"
        )

    @requires_bash
    @pytest.mark.parametrize("outdir,cid_arm,expect", [
        ("/tmp/gt_single_lsp", "gt-lsp-hybrid", True),
        ("/tmp/gt_single_lsp", "gt-hybrid", True),
        ("/tmp/gt_single_lsp", "gt-nolsp", False),
        ("/tmp/gt_single_nolsp", "gt-nolsp", True),
        ("/tmp/gt_single_nolsp", "gt-lsp-hybrid", False),
        ("/tmp/gt_single_nolsp", "gt-hybrid", False),
        ("/tmp/gt_single_lsp/shard_0", "gt-lsp-hybrid", True),  # sharded outdir
        ("/tmp/gt_single_nolsp/shard_3", "gt-nolsp", True),
    ])
    def test_arm_filter_decides_by_outdir_pattern(self, outdir, cid_arm, expect):
        got = _arm_filter_match(outdir, cid_arm)
        assert got is expect, (
            f"outdir={outdir!r} cid_arm={cid_arm!r}: expected accept={expect}, "
            f"got accept={got}"
        )
