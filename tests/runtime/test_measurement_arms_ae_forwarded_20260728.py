"""The two measurement arms must CROSS the pier boundary, or they are dark in-container.

`pier` DROPS the host `os.environ`; only `--ae KEY=VALUE` reaches the task container. So a knob
read in-container but never forwarded is silently OFF no matter what the host sets — and the
failure is invisible in the worst possible way: an operator sets `GT_SS_SHADOW_RATE=0.5`,
dispatches, sees ZERO holdouts, and reads that as "no holdouts happened" when the truth is "the
arm never ran".

That exact class already has a pin for Profile-2 MEMBERS
(`test_ae_forward_profile2_completeness_20260712.py`). These three are MEASUREMENT knobs, not
capability members, so that pin does not cover them — and the R1 parity invariant only scans
`gt_mini_patch.py`, while two of these are read in `miniswe_provider_boundary.py`. Both existing
guards were green while the knobs were dark. This closes that hole.

Forwarding is BYTE-IDENTICAL: every one defaults to `0`, and in-container an explicit "0" and an
absent value are both OFF. What changes is only that setting them on the host now WORKS.
"""

from __future__ import annotations

import re
from pathlib import Path


_BLOCK = (
    Path(__file__).parents[2]
    / "artifact_deepswe"
    / "gt_integration"
    / "gt_ae_block.sh"
)

# Same NAME extractor the other --ae pins use (default-value agnostic).
_AE_RE = re.compile(r'--ae\s+"?(GT_[A-Z0-9_]+)=')

_MEASUREMENT_KNOBS = (
    "GT_SS_SHADOW",       # shadow-holdout arm on/off        (#30)
    "GT_SS_SHADOW_RATE",  # holdout draw rate                (#30)
    "GT_L2_PROBE_RATE",   # same-state counterfactual rate   (#31)
)


def _forwarded() -> set[str]:
    return set(_AE_RE.findall(_BLOCK.read_text(encoding="utf-8")))


def test_every_measurement_knob_crosses_the_ae_boundary() -> None:
    missing = sorted(k for k in _MEASUREMENT_KNOBS if k not in _forwarded())
    assert missing == [], (
        "measurement knob(s) read in-container but NOT --ae-forwarded "
        f"(pier drops host env => the arm is DARK and zero rows read as 'no "
        f"effect' instead of 'never ran'): {missing}"
    )


def test_each_knob_defaults_to_off_so_forwarding_is_byte_identical() -> None:
    """Forwarding must not TURN ANYTHING ON.

    These arms withhold evidence or spend extra tokens; a default that is anything but off
    would make a measurement run happen by accident.
    """
    text = _BLOCK.read_text(encoding="utf-8")
    for knob in _MEASUREMENT_KNOBS:
        match = re.search(
            rf'--ae\s+"?{knob}=\$\{{{knob}:-([^}}"]*)\}}', text
        )
        assert match is not None, f"{knob} is not forwarded with a default"
        assert match.group(1).strip() == "0", (
            f"{knob} defaults to {match.group(1)!r}, not '0' — forwarding it would "
            "enable a measurement arm by accident"
        )
