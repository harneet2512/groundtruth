"""Fable-bounce fixes on the RL-profile fan-out path — Brief-F4, F5, F6, F10.

These guard the container-boundary + workflow-ABI + fail-closed correctness of the
official RL profile (rl_profile.py / gt_ae_block.sh / deepswe_full.yml):

  F4 — the fan-out lost 3/11 members at the pier container boundary (GT_GATEWAY,
       GT_EDIT_CHECK, GT_VERIFY_EXECUTE had NO --ae entry in GT_AE_ARGS, so the
       codespace path — which splices ONLY that array — activated 8/11).
  F5 — the workflow input ABI mangled '0': input default '0' + `|| ''` forwarded the
       truthy string '0', which the resolver reads as an explicit OFF override, so the
       profile could never activate edit_check/gateway from a UI dispatch.
  F6 — preflight() was self-attesting (default availability == the requested members),
       so the fail-closed abort was structurally unreachable.
  F10 — the gt_ae_block.sh header falsely claimed deepswe_full.yml does not source it.

Run with PYTHONIOENCODING=utf-8.
"""

from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess
import sys

import pytest
import yaml

import groundtruth
from groundtruth.runtime import rl_profile
from groundtruth.runtime.rl_profile import (
    PROFILE_MEMBERS,
    _available_from_env,
    resolve_profile,
)

_REPO = pathlib.Path(__file__).resolve().parents[2]
_BLOCK = _REPO / "artifact_deepswe" / "gt_integration" / "gt_ae_block.sh"
_WORKFLOW = _REPO / ".github" / "workflows" / "deepswe_full.yml"
_SRC = pathlib.Path(os.path.dirname(os.path.dirname(groundtruth.__file__)))
_MEMBERS = set(PROFILE_MEMBERS["1"])


# ═══════════════════════════════════════════════════════════════════════════════
# Brief-F4 — GT_AE_ARGS carries ALL 11 members (was missing the gateway/edit/verify trio)
# ═══════════════════════════════════════════════════════════════════════════════
# Mutation: delete the three `--ae "GT_GATEWAY=..."` / `GT_EDIT_CHECK` / `GT_VERIFY_EXECUTE`
# lines from gt_ae_block.sh -> test_f4_all_members_forwarded bites (3 MISSing).

_SENTINEL = "___AE_ARGS_BEGIN___"


def _source_block(extra_env: dict) -> dict:
    """Source gt_ae_block.sh in a clean bash env and return the KEY=VALUE map that
    GT_AE_ARGS forwards via --ae."""
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash not available for shell-sourcing test")
    env = {k: v for k, v in os.environ.items() if not k.startswith("GT_")}
    env["PYTHONPATH"] = str(_SRC) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    env.update(extra_env)
    script = (
        f'source "{_BLOCK.as_posix()}"\n'
        f'echo "{_SENTINEL}"\n'
        'printf "%s\\n" "${GT_AE_ARGS[@]}"\n'
    )
    proc = subprocess.run(
        [bash, "-c", script], cwd=str(_REPO), env=env,
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, f"block sourcing failed: {proc.stderr}"
    tail = proc.stdout.split(_SENTINEL, 1)[1]
    out: dict[str, str] = {}
    for line in tail.splitlines():
        m = re.match(r"^(GT_[A-Z0-9_]+)=(.*)$", line.strip())
        if m:
            out[m.group(1)] = m.group(2)
    return out


def test_f4_all_members_forwarded_when_profile_on():
    ae = _source_block({"GT_RL_PROFILE": "1"})
    missing = sorted(m for m in _MEMBERS if m not in ae)
    assert not missing, f"members lost at the --ae boundary: {missing}"
    # and the once-lost trio resolves to the profile's activated value
    assert ae["GT_GATEWAY"] == "1"
    assert ae["GT_EDIT_CHECK"] == "1"
    assert ae["GT_VERIFY_EXECUTE"] == "1"


def test_f4_trio_present_and_off_when_profile_unset():
    # byte-identical-off: the trio is always forwarded (additive) but resolves to 0,
    # which the in-container reads treat as OFF (== not passing it).
    ae = _source_block({})
    assert ae.get("GT_GATEWAY") == "0"
    assert ae.get("GT_EDIT_CHECK") == "0"
    assert ae.get("GT_VERIFY_EXECUTE") == "0"


def test_f4_explicit_host_override_survives_the_boundary():
    # per-flag control: an explicit GT_GATEWAY=0 with the profile on stays 0 in the --ae.
    ae = _source_block({"GT_RL_PROFILE": "1", "GT_GATEWAY": "0"})
    assert ae["GT_GATEWAY"] == "0"
    assert ae["GT_EDIT_CHECK"] == "1"  # the others still activate


# ═══════════════════════════════════════════════════════════════════════════════
# Brief-F5 — the workflow input ABI must not mangle '0'
# ═══════════════════════════════════════════════════════════════════════════════
# The fix is in deepswe_full.yml (input default "" + raw passthrough). These pin the
# resolver semantics the fix targets AND the YAML shape statically.
# Mutation: restore `default: "0"` or `|| ''` in the YAML -> the YAML asserts bite.

def test_f5_resolver_treats_zero_as_explicit_off():
    # This is WHY forwarding '0' (the old ABI) is a bug: '0' suppresses the member.
    got = resolve_profile({"GT_RL_PROFILE": "1", "GT_EDIT_CHECK": "0", "GT_GATEWAY": "0"})
    assert got["GT_EDIT_CHECK"] == "0"
    assert got["GT_GATEWAY"] == "0"


def test_f5_resolver_treats_empty_as_unset_and_activates():
    # The fixed ABI forwards '' (unset) -> the profile activates the member.
    got = resolve_profile({"GT_RL_PROFILE": "1", "GT_EDIT_CHECK": "", "GT_GATEWAY": ""})
    assert got["GT_EDIT_CHECK"] == "1"
    assert got["GT_GATEWAY"] == "1"


def _workflow():
    d = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    # YAML 1.1 parses a bare `on:` key to True
    return d, d.get("on", d.get(True))


def test_f5_inputs_default_empty_not_zero():
    _d, trig = _workflow()
    inputs = trig["workflow_dispatch"]["inputs"]
    assert inputs["edit_check"]["default"] == "", inputs["edit_check"]["default"]
    assert inputs["gateway"]["default"] == "", inputs["gateway"]["default"]


def test_f5_env_passthrough_is_raw_no_or_default():
    d, _trig = _workflow()
    env = d["env"]
    assert env["GT_EDIT_CHECK"] == "${{ github.event.inputs.edit_check }}"
    assert env["GT_GATEWAY"] == "${{ github.event.inputs.gateway }}"
    assert "||" not in env["GT_EDIT_CHECK"]
    assert "||" not in env["GT_GATEWAY"]


def test_f5_downstream_reads_are_defaulted_not_bare():
    # blank '' must not break any reader: the only functional refs are ${GT_X:-0}.
    text = _WORKFLOW.read_text(encoding="utf-8")
    # no `-n`/`-z` conditional readers of these vars (a blank default would flip them)
    assert not re.search(r"-[nz]\s+\"?\$\{?GT_(EDIT_CHECK|GATEWAY)\b", text)


# ═══════════════════════════════════════════════════════════════════════════════
# Brief-F6 — the fail-closed abort is REACHABLE via a real capability signal
# ═══════════════════════════════════════════════════════════════════════════════
# Mutation: revert _available_from_env to `return set(profile_members(...))`
# -> test_f6_missing_backing_module_aborts bites (rc 0, no abort).

class _Buf:
    def __init__(self):
        self.s = ""

    def write(self, x):
        self.s += x
        return len(x)


def test_f6_happy_path_all_available_no_regression(monkeypatch):
    # every backing module present on the host -> all members available -> clean run.
    avail = _available_from_env({"GT_RL_PROFILE": "1"})
    assert avail == _MEMBERS, sorted(_MEMBERS - avail)
    out, err = _Buf(), _Buf()
    assert rl_profile.main(["--emit-exports"], env={"GT_RL_PROFILE": "1"}, out=out, err=err) == 0


def test_f6_missing_backing_module_narrows_availability(monkeypatch):
    monkeypatch.setattr(
        rl_profile, "_module_findable",
        lambda m: False if m == "groundtruth.runtime.covering_runner" else True,
    )
    avail = _available_from_env({"GT_RL_PROFILE": "1"})
    assert "GT_VERIFY_EXECUTE" not in avail, "a genuinely-absent module stayed 'available'"


def test_f6_missing_backing_module_aborts(monkeypatch):
    # THE reachability proof: a missing backing module -> preflight non-empty -> rc 3.
    monkeypatch.setattr(
        rl_profile, "_module_findable",
        lambda m: False if m == "groundtruth.runtime.covering_runner" else True,
    )
    out, err = _Buf(), _Buf()
    rc = rl_profile.main(["--emit-exports"], env={"GT_RL_PROFILE": "1"}, out=out, err=err)
    assert rc == 3, "fail-closed abort did not fire on a missing member capability"
    assert out.s == ""  # no exports on abort
    assert "GT_RL_PREFLIGHT_ABORT" in err.s


def test_f6_explicit_available_override_still_wins(monkeypatch):
    # the operator/CI stamp takes precedence over the probe.
    monkeypatch.setattr(rl_profile, "_module_findable", lambda m: False)  # probe says nothing
    avail = _available_from_env(
        {"GT_RL_PROFILE": "1", "GT_RL_PROFILE_AVAILABLE": "GT_GATEWAY,GT_EDIT_CHECK"}
    )
    assert avail == {"GT_GATEWAY", "GT_EDIT_CHECK"}


def test_f6_unmapped_member_never_false_aborts(monkeypatch):
    # GT_D7_RELATEDNESS has no mapped backing module -> always available (correct-or-quiet).
    monkeypatch.setattr(rl_profile, "_module_findable", lambda m: False)
    avail = _available_from_env({"GT_RL_PROFILE": "1"})
    assert "GT_D7_RELATEDNESS" in avail


# ═══════════════════════════════════════════════════════════════════════════════
# Brief-F10 — the gt_ae_block.sh header matches reality (full.yml DOES source it)
# ═══════════════════════════════════════════════════════════════════════════════

def test_f10_header_no_longer_claims_one_caller():
    header = _BLOCK.read_text(encoding="utf-8")
    assert "sourced by ONE caller" not in header
    # the corrected header names deepswe_full.yml as a sourcing caller
    assert "deepswe_full.yml" in header and "sources it" in header


def test_f10_deepswe_full_actually_sources_the_block():
    wf = _WORKFLOW.read_text(encoding="utf-8")
    assert "source artifact_deepswe/gt_integration/gt_ae_block.sh" in wf
    assert 'GT_AE_ARM+=("${GT_AE_ARGS[@]}")' in wf
