r"""W8 (2026-07-13): PRODUCTION-DEFAULT INVERSION — the seam resolves its OWN RL profile
in-process at install time.

Before W8 every Profile-2 member was an opt-in env read defaulting OFF, and production-on
depended on an EXTERNAL exporter (the workflow fanned GT_RL_PROFILE=2 -> 29 member exports).
A launcher that forgot to fan out silently degraded GT to the legacy stack. W8 inverts
ownership: at ``gt_mini_patch._install()`` time the seam resolves the profile itself and
DEFAULTS the members ON, so flags become KILL-SWITCHES and explicit env always wins.

WHY MOST TESTS SPAWN A SUBPROCESS: the seam runs ``_install()`` at IMPORT time, and it does
NOT auto-apply the inversion when ``pytest`` is resident (``_should_auto_invert`` — a
process-global env mutation must never contaminate the shared test interpreter, and the
seam is re-imported mid-suite while proof-mode tests transiently hold GT_PROOF_MODE=1). So
the TRUE import-time production path is exercised in a FRESH python subprocess (``pytest``
genuinely absent) with a crafted env; the PURE resolver logic + the correct-or-quiet path
are exercised in-process by calling the functions directly.

Mutation sentinels (run manually, confirm bite, restore) at the module tail.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

_SRC = os.path.join(r"D:\Groundtruth", "src")
_ART = os.path.join(r"D:\Groundtruth", "artifact_deepswe")

sys.path.insert(0, _ART)
sys.path.insert(0, _SRC)

import gt_mini_patch as g  # noqa: E402
from groundtruth.runtime import rl_profile as rp  # noqa: E402


# ---------------------------------------------------------------------------
# Hermeticity: snapshot + restore ALL GT_* env keys around every test. The
# inversion mutates os.environ via setdefault (NOT monkeypatch-tracked), so an
# in-process test that drives it directly could leak a member into a later test.
# This local fixture makes THIS file self-contained (no shared-conftest change).
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _restore_gt_env():
    saved = {k: v for k, v in os.environ.items() if k.startswith("GT_")}
    saved_source = dict(g._GT_PROFILE_MEMBER_SOURCE)
    yield
    for k in [k for k in os.environ if k.startswith("GT_")]:
        if k not in saved:
            os.environ.pop(k, None)
    for k, v in saved.items():
        os.environ[k] = v
    g._GT_PROFILE_MEMBER_SOURCE = saved_source


# ---------------------------------------------------------------------------
# Subprocess harness: a FRESH python that imports gt_mini_patch (fires _install
# -> auto-invert, since pytest is absent) under a crafted env, then reports the
# resolved GT_ member snapshot, GT_RL_PROFILE, the member_source, the patched
# set, and (if a ledger dir was given) the profile receipt.
# ---------------------------------------------------------------------------
_CHILD = r"""
import json, os, sys
sys.path.insert(0, r"{art}")
sys.path.insert(0, r"{src}")
import gt_mini_patch as g
from groundtruth.runtime import rl_profile as r
members = sorted(r.PROFILE_MEMBERS["1"] | r.PROFILE_MEMBERS["2"])
snap = {{m: os.environ.get(m) for m in members}}
receipt = None
led = os.environ.get("GT_RUNTIME_LEDGER")
if led:
    rpath = os.path.join(os.path.dirname(led) or ".", "gt_profile_receipt.json")
    if os.path.isfile(rpath):
        receipt = json.load(open(rpath, encoding="utf-8"))
payload = {{
    "env": snap,
    "gt_rl_profile": os.environ.get("GT_RL_PROFILE"),
    "member_source": dict(g._GT_PROFILE_MEMBER_SOURCE),
    "patched": list(g._PATCHED_CLASSES),
    "pytest_in": "pytest" in sys.modules,
    "receipt": receipt,
    # a representative delivery composer call (pure, env-independent) — byte-identity pin
    "compose": g._join_lane_output("base output", "STEER block"),
}}
print("W8_JSON_BEGIN")
print(json.dumps(payload))
print("W8_JSON_END")
"""


def _run_seam(overrides, tmp_path=None):
    """Import gt_mini_patch in a fresh subprocess under a crafted env; return the payload dict.

    The child env starts as os.environ with EVERY GT_* key (and PYTHONPATH) stripped — a
    clean, deterministic base — then ``overrides`` are applied (value None => keep unset).
    """
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("GT_") and k != "PYTHONPATH"}
    if tmp_path is not None:
        env["GT_RUNTIME_LEDGER"] = str(tmp_path / "led.jsonl")
    for k, v in overrides.items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v
    code = _CHILD.format(art=_ART, src=_SRC)
    proc = subprocess.run([sys.executable, "-c", code],
                          env=env, capture_output=True, text=True, timeout=180)
    out = proc.stdout
    b, e = out.find("W8_JSON_BEGIN"), out.find("W8_JSON_END")
    assert b != -1 and e != -1, (
        f"child produced no payload (rc={proc.returncode})\n"
        f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
    return json.loads(out[b + len("W8_JSON_BEGIN"):e].strip())


_P1 = sorted(rp.PROFILE_MEMBERS["1"])
_P2 = sorted(rp.PROFILE_MEMBERS["2"])
_SUPER_ONLY = sorted(rp.PROFILE_MEMBERS["2"] - rp.PROFILE_MEMBERS["1"])


# ---------------------------------------------------------------------------
# 1. IMPORT-TIME behaviour (subprocess — the true production path)
# ---------------------------------------------------------------------------
def test_unset_profile_defaults_every_profile2_member_on(tmp_path):
    """GT_RL_PROFILE unset + non-baseline -> after install-time resolution EVERY
    PROFILE_MEMBERS['2'] member is "1" (production default). Members enumerated dynamically
    from rl_profile — never hardcoded."""
    r = _run_seam({"GT_RL_PROFILE": None, "GT_BASELINE": None}, tmp_path)
    assert r["pytest_in"] is False, "subprocess must run WITHOUT pytest so auto-invert fires"
    assert r["gt_rl_profile"] == "2", "GT_RL_PROFILE must be setdefault-ed to the resolved token"
    missing = [m for m in _P2 if r["env"].get(m) != "1"]
    assert not missing, f"unset profile must default every Profile-2 member ON; missing={missing}"
    # every one was resolved (none pre-existed in the stripped child env)
    assert all(r["member_source"].get(m) == "resolved" for m in _P2)


def test_explicit_off_resolves_nothing(tmp_path):
    """GT_RL_PROFILE=off -> ZERO members set (explicit legacy/control posture)."""
    r = _run_seam({"GT_RL_PROFILE": "off", "GT_BASELINE": None}, tmp_path)
    on = [m for m in _P2 if r["env"].get(m) is not None]
    assert not on, f"explicit-off must set NO member; got {on}"
    assert r["member_source"] == {}, "explicit-off resolves nothing -> empty member_source"
    assert r["gt_rl_profile"] == "off", "explicit posture is left untouched"


def test_profile_1_sets_exactly_profile1_members(tmp_path):
    """GT_RL_PROFILE=1 -> exactly PROFILE_MEMBERS['1'] set; the Super-Mode-only members stay OFF."""
    r = _run_seam({"GT_RL_PROFILE": "1", "GT_BASELINE": None}, tmp_path)
    missing = [m for m in _P1 if r["env"].get(m) != "1"]
    assert not missing, f"Profile-1 members must all be ON; missing={missing}"
    leaked_super = [m for m in _SUPER_ONLY if r["env"].get(m) is not None]
    assert not leaked_super, f"Super-Mode-only members must stay OFF under Profile-1; got {leaked_super}"
    assert r["gt_rl_profile"] == "1"


def test_baseline_sets_no_members_and_no_receipt(tmp_path):
    """GT_BASELINE=1 -> _install early-returns: zero members, no profile receipt."""
    r = _run_seam({"GT_BASELINE": "1", "GT_RL_PROFILE": None}, tmp_path)
    on = [m for m in _P2 if r["env"].get(m) is not None]
    assert not on, f"the control arm must set NO member; got {on}"
    assert r["member_source"] == {}
    assert r["receipt"] is None, "the baseline arm must not write a profile receipt (no contamination)"


def test_killswitch_explicit_zero_survives(tmp_path):
    """An explicit GT_X=0 in the env BEFORE resolution stays "0" after (setdefault kill-switch),
    while the rest of Profile-2 still defaults ON. Tested with TWO members."""
    r = _run_seam(
        {"GT_RL_PROFILE": None, "GT_BASELINE": None, "GT_GATEWAY": "0", "GT_LANE_ENVELOPE": "0"},
        tmp_path)
    assert r["env"].get("GT_GATEWAY") == "0", "GT_GATEWAY=0 kill-switch must survive the inversion"
    assert r["env"].get("GT_LANE_ENVELOPE") == "0", "GT_LANE_ENVELOPE=0 kill-switch must survive"
    # the kill-switched members are recorded as inherited, not resolved
    assert r["member_source"].get("GT_GATEWAY") == "inherited"
    assert r["member_source"].get("GT_LANE_ENVELOPE") == "inherited"
    # a NON-kill-switched member still defaults ON and is recorded resolved
    other = next(m for m in _P2 if m not in ("GT_GATEWAY", "GT_LANE_ENVELOPE"))
    assert r["env"].get(other) == "1"
    assert r["member_source"].get(other) == "resolved"


def test_receipt_member_source_distinguishes_inherited_vs_resolved(tmp_path):
    """The profile receipt records per-member provenance: a pre-set member reads "inherited",
    a defaulted one reads "resolved". Schema stays gt.profile_receipt.v1 (additive key)."""
    r = _run_seam(
        {"GT_RL_PROFILE": None, "GT_BASELINE": None, "GT_GATEWAY": "0"}, tmp_path)
    rec = r["receipt"]
    assert rec is not None, "a GT-on run must write the profile receipt next to the ledger"
    assert rec["schema"] == "gt.profile_receipt.v1", "schema must remain v1 (member_source is additive)"
    assert rec["gt_rl_profile"] == "2"
    ms = rec["member_source"]
    assert ms.get("GT_GATEWAY") == "inherited", "a pre-set member reads inherited"
    resolved = next(m for m in _P2 if m != "GT_GATEWAY")
    assert ms.get(resolved) == "resolved", "a defaulted member reads resolved"
    # the kill-switched member (value "0") is correctly EXCLUDED from members_on (off to _is_on)
    assert "GT_GATEWAY" not in rec["members_on"]


# ---------------------------------------------------------------------------
# 2. PURE resolver logic (in-process — rl_profile helpers, no os.environ mutation)
# ---------------------------------------------------------------------------
def test_resolve_default_token_production_default_and_explicit_off():
    assert rp.resolve_default_token({}) == "2"                       # unset -> production default
    assert rp.resolve_default_token({"GT_RL_PROFILE": ""}) == "2"    # empty -> production default
    assert rp.resolve_default_token({"GT_RL_PROFILE": "   "}) == "2"  # whitespace -> production default
    for off in ("0", "off", "none", "OFF", " None ", "Off"):
        assert rp.resolve_default_token({"GT_RL_PROFILE": off}) is None, off
    assert rp.resolve_default_token({"GT_RL_PROFILE": "1"}) == "1"
    assert rp.resolve_default_token({"GT_RL_PROFILE": " 2 "}) == "2"
    assert rp.resolve_default_token({"GT_RL_PROFILE": "99"}) == "99"  # unknown token passes through


def test_resolve_profile_defaults_maps_and_explicit_off():
    d2 = rp.resolve_profile_defaults({})
    # THE-17 (2026-07-17): resolution fans PROFILE_MEMBERS *plus* the Profile-2
    # PROFILE_BEHAVIOR_FLAGS (behavior fix-switches deliberately kept OUT of the
    # member set so the CAP inventory count stays exact — rl_profile.py:303-321).
    _p2_resolved = set(_P2) | set(rp.PROFILE_BEHAVIOR_FLAGS.get("2", frozenset()))
    assert set(d2) == _p2_resolved and all(v == "1" for v in d2.values())
    assert rp.resolve_profile_defaults({"GT_RL_PROFILE": "off"}) == {}
    d1 = rp.resolve_profile_defaults({"GT_RL_PROFILE": "1"})
    assert set(d1) == set(_P1) and all(v == "1" for v in d1.values())
    assert rp.resolve_profile_defaults({"GT_RL_PROFILE": "99"}) == {}  # unknown -> nothing (fail-safe)


def test_resolve_profile_defaults_returns_full_set_including_killswitched():
    """The helper does NOT pre-filter an explicit per-member value — it returns the FULL member
    set so the SEAM's setdefault stays the single load-bearing kill-switch (a direct-assignment
    regression must be caught by the seam's kill-switch test, not masked here)."""
    d = rp.resolve_profile_defaults({"GT_GATEWAY": "0"})  # GT_RL_PROFILE unset -> Profile-2
    assert "GT_GATEWAY" in d and d["GT_GATEWAY"] == "1", "helper returns the member; seam's setdefault guards it"


def test_resolve_profile_unchanged_for_existing_callers():
    """The pre-W8 workflow verb resolve_profile keeps its behaviour (fan-out stays valid,
    merely redundant): UNSET is still a no-op there (only the SEAM inverts the default)."""
    assert rp.resolve_profile({}) == {}
    assert rp.resolve_profile({"GT_RL_PROFILE": "0"}) == {}
    fan = rp.resolve_profile({"GT_RL_PROFILE": "2"})
    assert set(fan) == set(_P2) and all(v == "1" for v in fan.values())


# ---------------------------------------------------------------------------
# 3. FAILURE-SAFETY — correct-or-quiet (in-process)
# ---------------------------------------------------------------------------
def test_rl_profile_import_failure_is_quiet_and_receipt_still_written(tmp_path, monkeypatch):
    """rl_profile UNIMPORTABLE -> _resolve_production_profile sets NO member, does NOT raise,
    and _install's receipt is still written. Correct-or-quiet: never brick the import-time seam
    (which would blank the fail-closed proof marker)."""
    import groundtruth.runtime as _gr
    monkeypatch.setenv("GT_RUNTIME_LEDGER", str(tmp_path / "led.jsonl"))
    monkeypatch.delenv("GT_RL_PROFILE", raising=False)
    canary = "GT_GATEWAY"
    monkeypatch.delenv(canary, raising=False)
    # Force `from groundtruth.runtime import rl_profile` to RAISE inside the function: the
    # bound package attribute is read first (so sys.modules alone is not enough), so drop the
    # attribute AND poison the sys.modules entry -> the submodule re-import halts (None) -> ImportError.
    monkeypatch.delattr(_gr, "rl_profile", raising=False)
    monkeypatch.setitem(sys.modules, "groundtruth.runtime.rl_profile", None)
    g._GT_PROFILE_MEMBER_SOURCE = {}

    g._resolve_production_profile()  # must NOT raise
    assert canary not in os.environ, "a resolution fault must leave the env untouched (no member set)"
    assert g._GT_PROFILE_MEMBER_SOURCE == {}, "no provenance recorded on the failure path"

    g._write_profile_receipt()  # must still write despite the failed resolution
    rec = json.loads((tmp_path / "gt_profile_receipt.json").read_text(encoding="utf-8"))
    assert rec["schema"] == "gt.profile_receipt.v1"
    assert rec["member_source"] == {}, "member_source is present-but-empty when the inversion did not run"


# ---------------------------------------------------------------------------
# 4. BYTE-IDENTITY — explicit-off == the pre-W8 unset posture
# ---------------------------------------------------------------------------
def test_explicit_off_is_byte_identical_pre_w8_posture(tmp_path):
    """GT_RL_PROFILE=off must reproduce the pre-W8 UNSET posture: NO member set, and a
    representative delivery composer call yields the exact frozen bytes (the composition is
    env-independent, so the inversion changes ZERO delivered bytes on the control posture)."""
    off = _run_seam({"GT_RL_PROFILE": "off", "GT_BASELINE": None}, tmp_path)
    # 1) env posture identical to a bare pre-W8 process: no Profile-2 member present
    assert all(off["env"].get(m) is None for m in _P2)
    # 2) the composer bytes match the frozen pin (same value the seam's own byte-identity test asserts)
    assert off["compose"] == "base output\nSTEER block"
    # 3) and the in-process pure resolver agrees: off resolves nothing
    assert rp.resolve_profile_defaults({"GT_RL_PROFILE": "off"}) == {}
    assert g._join_lane_output("base output", "STEER block") == off["compose"]


# ---------------------------------------------------------------------------
# MUTATION SENTINELS (run manually, confirm bite, restore):
#   (a) setdefault -> direct assignment (overrides an explicit "0"):
#       in gt_mini_patch._resolve_production_profile change
#         os.environ.setdefault(member, defaults[member])
#       -> os.environ[member] = defaults[member]
#       => test_killswitch_explicit_zero_survives FAILS (GT_GATEWAY becomes "1").
#   (b) drop the production default:
#       in rl_profile change `_PRODUCTION_DEFAULT_PROFILE = "2"` -> `= ""`
#       => test_unset_profile_defaults_every_profile2_member_on FAILS (no member set on unset).
# ---------------------------------------------------------------------------
