"""Official RL profile resolver (rl_profile.py) — B-16 / B-17, Stage-1.

TTD, red-first: these tests are written against the (initially MISSING) module
``groundtruth.runtime.rl_profile`` and are watched fail before it exists.

The module makes the official RL-adherence stack EXECUTABLE via ONE versioned
toggle ``GT_RL_PROFILE`` instead of eight independent, input-less flags. It is a
pure/deterministic env-map transform (no I/O, no LLM) plus a tiny CLI the
workflow shell (`gt_ae_block.sh`) sources.

Laws proven here:

1. ``resolve_profile(env)`` fans ``GT_RL_PROFILE=1`` out to the 8 coherent member
   flags, all "1" — the coherent set from verifyandobserve.md §4.
2. an EXPLICITLY-set member value (incl. "0") OVERRIDES the profile — per-flag
   control still works; an EMPTY value is NOT an override (the workflow's
   ``|| ''`` default must not suppress the profile).
3. profile UNSET / "" / "0" ⇒ empty map ⇒ NO member touched ⇒ byte-identical OFF.
4. ``preflight(env, available)`` returns the members that are requested-ON but
   whose capability is absent (⇒ the harness ABORTS before model spend,
   fail-closed); an unknown profile version also aborts; a member turned OFF by
   an explicit override does NOT require its capability.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from groundtruth.runtime import rl_profile
from groundtruth.runtime.rl_profile import (
    PROFILE_MEMBERS,
    preflight,
    resolve_profile,
)

# The coherent member set (verifyandobserve.md §4 "Official RL profile").
MEMBERS = {
    "GT_GATEWAY",
    "GT_GATEWAY_NATIVE",
    "GT_STEER_NATIVE",
    "GT_LANE_ENVELOPE",
    "GT_EDIT_CHECK",
    "GT_VERIFY_EXECUTE",
    "GT_D7_RELATEDNESS",
    "GT_OBLIGATION_FRESHNESS",
    # 2026-07-10 seam flags (B-19/B-20/B-3) added to the coherent stack.
    "GT_CONTRACT_MODE",
    "GT_CONTRACT_BILATERAL",
    "GT_GATEWAY_EDIT_BRIDGES",
}


# ── (completeness — the biting-mutation target) ──────────────────────────────
def test_profile_v1_is_exactly_the_members():
    # Dropping (or adding) a member in PROFILE_MEMBERS["1"] makes THIS bite.
    assert PROFILE_MEMBERS["1"] == MEMBERS


# ── (a) profile=1 sets all 8 members ─────────────────────────────────────────
def test_profile_1_activates_all_members_to_one():
    got = resolve_profile({"GT_RL_PROFILE": "1"})
    assert set(got) == MEMBERS
    assert all(v == "1" for v in got.values()), got


# ── (b) an explicit member override is honored ───────────────────────────────
def test_explicit_member_override_off_is_honored():
    got = resolve_profile({"GT_RL_PROFILE": "1", "GT_VERIFY_EXECUTE": "0"})
    assert got["GT_VERIFY_EXECUTE"] == "0"
    assert all(got[m] == "1" for m in MEMBERS if m != "GT_VERIFY_EXECUTE")


def test_explicit_group_x_override_off_is_honored():
    # GT_GATEWAY has an existing per-flag dispatch input; an operator "0" must win.
    got = resolve_profile({"GT_RL_PROFILE": "1", "GT_GATEWAY": "0"})
    assert got["GT_GATEWAY"] == "0"


def test_explicit_on_is_a_noop_still_on():
    got = resolve_profile({"GT_RL_PROFILE": "1", "GT_GATEWAY": "1"})
    assert got["GT_GATEWAY"] == "1"


def test_empty_member_value_is_not_an_override():
    # The workflow's `|| ''` default forwards an EMPTY string; that is "unset",
    # NOT a chosen "0" — the profile must still activate the member.
    got = resolve_profile({"GT_RL_PROFILE": "1", "GT_GATEWAY": "", "GT_EDIT_CHECK": "  "})
    assert got["GT_GATEWAY"] == "1"
    assert got["GT_EDIT_CHECK"] == "1"


# ── (c) profile UNSET ⇒ empty / no-op (byte-identical off) ────────────────────
@pytest.mark.parametrize("env", [
    {},
    {"GT_RL_PROFILE": ""},
    {"GT_RL_PROFILE": "   "},
    {"GT_RL_PROFILE": "0"},
])
def test_profile_off_is_empty_noop(env):
    assert resolve_profile(env) == {}


def test_profile_off_never_touches_member_env():
    # Even with member vars present, an OFF profile resolves nothing (it never
    # rewrites the operator's own explicit member settings).
    env = {"GT_EDIT_CHECK": "1", "GT_VERIFY_EXECUTE": "0"}
    assert resolve_profile(env) == {}


def test_unknown_profile_resolves_nothing():
    # resolve is quiet on an unknown version; preflight is the one that aborts.
    assert resolve_profile({"GT_RL_PROFILE": "99"}) == {}


# ── (d) preflight: missing capability ⇒ abort list ───────────────────────────
def test_preflight_all_available_is_clean():
    assert preflight({"GT_RL_PROFILE": "1"}, set(MEMBERS)) == []


def test_preflight_reports_missing_member():
    avail = set(MEMBERS) - {"GT_STEER_NATIVE"}
    assert preflight({"GT_RL_PROFILE": "1"}, avail) == ["GT_STEER_NATIVE"]


def test_preflight_missing_members_sorted():
    avail = set(MEMBERS) - {"GT_STEER_NATIVE", "GT_D7_RELATEDNESS"}
    assert preflight({"GT_RL_PROFILE": "1"}, avail) == [
        "GT_D7_RELATEDNESS",
        "GT_STEER_NATIVE",
    ]


def test_preflight_off_never_aborts():
    assert preflight({}, set()) == []
    assert preflight({"GT_RL_PROFILE": "0"}, set()) == []


def test_preflight_ignores_capability_of_a_disabled_member():
    # A member explicitly turned OFF must NOT require its capability to be present.
    avail = set(MEMBERS) - {"GT_VERIFY_EXECUTE"}
    env = {"GT_RL_PROFILE": "1", "GT_VERIFY_EXECUTE": "0"}
    assert preflight(env, avail) == []


def test_preflight_unknown_profile_aborts():
    # Fail-closed: a profile version the resolver does not know must abort.
    missing = preflight({"GT_RL_PROFILE": "99"}, set(MEMBERS))
    assert missing  # non-empty ⇒ abort


# ── CLI: main() emits shell exports + fail-closed exit code ───────────────────
class _Buf:
    def __init__(self):
        self.s = ""

    def write(self, x):
        self.s += x
        return len(x)


def test_main_emit_exports_success():
    out, err = _Buf(), _Buf()
    rc = rl_profile.main(["--emit-exports"], env={"GT_RL_PROFILE": "1"}, out=out, err=err)
    assert rc == 0
    # every member gets an `export GT_X=...` line
    for m in MEMBERS:
        assert f"export {m}=" in out.s, (m, out.s)


def test_main_off_emits_nothing():
    out, err = _Buf(), _Buf()
    rc = rl_profile.main(["--emit-exports"], env={}, out=out, err=err)
    assert rc == 0
    assert out.s == ""


def test_main_aborts_when_capability_unavailable():
    out, err = _Buf(), _Buf()
    rc = rl_profile.main(
        ["--emit-exports"],
        env={"GT_RL_PROFILE": "1", "GT_RL_PROFILE_AVAILABLE": "GT_GATEWAY"},
        out=out,
        err=err,
    )
    assert rc == 3
    assert out.s == ""  # no exports on abort
    assert "GT_RL_PREFLIGHT_ABORT" in err.s


def test_main_available_override_can_pass():
    # If the operator asserts availability of exactly the requested-ON set, pass.
    out, err = _Buf(), _Buf()
    avail = ",".join(sorted(MEMBERS))
    rc = rl_profile.main(
        ["--emit-exports"],
        env={"GT_RL_PROFILE": "1", "GT_RL_PROFILE_AVAILABLE": avail},
        out=out,
        err=err,
    )
    assert rc == 0


# ── subprocess smoke: `python -m groundtruth.runtime.rl_profile` shell path ───
def _subproc(env_extra):
    import groundtruth

    src_dir = os.path.dirname(os.path.dirname(groundtruth.__file__))
    env = {k: v for k, v in os.environ.items() if not k.startswith("GT_")}
    env["PYTHONPATH"] = src_dir + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "groundtruth.runtime.rl_profile", "--emit-exports"],
        capture_output=True,
        text=True,
        env=env,
    )


def test_cli_subprocess_emits_exports_and_aborts():
    ok = _subproc({"GT_RL_PROFILE": "1"})
    assert ok.returncode == 0, ok.stderr
    assert "export GT_GATEWAY=1" in ok.stdout
    assert "export GT_VERIFY_EXECUTE=1" in ok.stdout

    off = _subproc({})
    assert off.returncode == 0
    assert off.stdout.strip() == ""

    abort = _subproc({"GT_RL_PROFILE": "1", "GT_RL_PROFILE_AVAILABLE": "GT_GATEWAY"})
    assert abort.returncode == 3
    assert abort.stdout.strip() == ""
    assert "GT_RL_PREFLIGHT_ABORT" in abort.stderr
