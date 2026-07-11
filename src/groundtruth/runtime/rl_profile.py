"""Official RL profile resolver — B-16 / B-17.

Problem (verifyandobserve.md rows B-16/B-17, §4 "Official RL profile"):
the whole RL-adherence stack defaults OFF in the official ``deepswe_full.yml``
harness, and six of its member flags have NO workflow-dispatch input, so they
expand ``unset -> "0"`` and the submit gate + native forms are unreachable.

Fix: ONE versioned toggle ``GT_RL_PROFILE``. When set (e.g. ``=1``) it activates
the coherent member set; an EXPLICITLY-set member flag overrides the profile so
per-flag control still works; if the profile is requested but a member capability
is unavailable the harness ABORTS before model spend (fail-closed); and when the
profile is UNSET the transform is a strict no-op (byte-identical to today).

This module is PURE + deterministic (no I/O, no LLM). The two library verbs are
unit-testable without running the workflow:

  * ``resolve_profile(env) -> dict[str, str]`` — the member flag map to export.
  * ``preflight(env, available) -> list[str]`` — members to abort on (empty = OK).

A thin ``main()`` CLI (``python -m groundtruth.runtime.rl_profile --emit-exports``)
is what ``artifact_deepswe/gt_integration/gt_ae_block.sh`` sources: it prints
``export GT_X=...`` lines on success and exits non-zero (printing
``GT_RL_PREFLIGHT_ABORT: ...``) when the requested profile is partially
unavailable — the shell aborts the run before the paid ``pier run``.
"""

from __future__ import annotations

import os
import shlex
import sys
from typing import Iterable, Mapping, MutableMapping, Optional, Sequence

# ── The versioned profile registry ───────────────────────────────────────────
# ``GT_RL_PROFILE=<version>`` -> the coherent member set that version activates.
# Source of truth: `.claude/reports/verifyandobserve.md` §4. Adding a versioned
# profile here is the ONLY way to extend the toggle; unknown versions fail closed.
PROFILE_MEMBERS: dict[str, frozenset[str]] = {
    "1": frozenset(
        {
            "GT_GATEWAY",
            "GT_GATEWAY_NATIVE",
            "GT_STEER_NATIVE",
            "GT_LANE_ENVELOPE",
            "GT_EDIT_CHECK",
            "GT_VERIFY_EXECUTE",
            "GT_D7_RELATEDNESS",
            "GT_OBLIGATION_FRESHNESS",
            # 2026-07-10 seam flags — the mode-conditioned/bilateral contract (B-19/B-20)
            # and the edit before/after bridges (B-3). Forwarded in gt_ae_block.sh; part
            # of the coherent RL-adherence stack the profile activates.
            "GT_CONTRACT_MODE",
            "GT_CONTRACT_BILATERAL",
            "GT_GATEWAY_EDIT_BRIDGES",
        }
    ),
}

# Values (case-insensitive, stripped) that count as "OFF" for a member.
_OFF_VALUES = {"", "0", "false", "no", "off"}


def _profile_token(env: Mapping[str, str]) -> str:
    """The requested profile version, stripped. Empty string when unset/OFF."""
    return (env.get("GT_RL_PROFILE") or "").strip()


def _profile_is_off(token: str) -> bool:
    return token == "" or token == "0"


def _is_on(value: str) -> bool:
    return value.strip().lower() not in _OFF_VALUES


def profile_members(token: str) -> frozenset[str]:
    """Canonical members for a profile version (empty for unknown/off)."""
    return PROFILE_MEMBERS.get(token, frozenset())


def resolve_profile(env: Mapping[str, str]) -> dict[str, str]:
    """Fan ``GT_RL_PROFILE`` out to its member flag map.

    Rules:
      * profile UNSET / "" / "0" / unknown version  -> ``{}`` (no member touched).
      * else each member -> ``"1"`` UNLESS the env carries an explicit, non-empty
        value for it, in which case that value wins (per-flag override, incl "0").
        An EMPTY string is treated as unset (the workflow's ``|| ''`` default),
        NOT as a chosen "0", so it does not suppress the profile.

    Deterministic: the returned dict is insertion-ordered by sorted member name.
    """
    token = _profile_token(env)
    if _profile_is_off(token):
        return {}
    members = PROFILE_MEMBERS.get(token)
    if members is None:
        return {}
    out: dict[str, str] = {}
    for member in sorted(members):
        explicit = env.get(member)
        if explicit is not None and explicit.strip() != "":
            out[member] = explicit  # explicit override wins (including "0")
        else:
            out[member] = "1"
    return out


def preflight(env: Mapping[str, str], available: Iterable[str]) -> list[str]:
    """Members that block the run: requested-ON but capability absent.

    Returns a sorted list of member names (or an ``GT_RL_PROFILE=<v>`` sentinel
    for an unknown version) that must ABORT the harness before model spend.
    Empty list = clear to run. A member turned OFF by an explicit override does
    NOT require its capability to be available. Profile OFF returns ``[]``.
    """
    token = _profile_token(env)
    if _profile_is_off(token):
        return []
    if token not in PROFILE_MEMBERS:
        return [f"GT_RL_PROFILE={token}"]  # unknown version -> fail closed
    available_set = set(available)
    resolved = resolve_profile(env)
    requested_on = {m for m, v in resolved.items() if _is_on(v)}
    return sorted(requested_on - available_set)


# Brief-F6: each RL-profile member's capability is BACKED by a concrete module in the
# ``groundtruth`` package (the feature's implementation). A member whose backing module
# is ABSENT from the running interpreter's import path (a stale/partial substrate) is
# genuinely unavailable → the preflight must abort before model spend. Members without a
# single clean backing module (GT_D7_RELATEDNESS + the contract flags are spread across
# the in-container mini-seam) are omitted here and treated as available — correct-or-
# quiet: NEVER a FALSE abort on an unmapped member. Only LIGHT ``groundtruth.runtime.*``
# modules are mapped so the probe imports nothing heavy at resolver time.
_MEMBER_CAPABILITY_MODULE: dict[str, str] = {
    "GT_GATEWAY": "groundtruth.runtime.gateway",
    "GT_GATEWAY_EDIT_BRIDGES": "groundtruth.runtime.gateway",
    "GT_GATEWAY_NATIVE": "groundtruth.runtime.native_render",
    "GT_STEER_NATIVE": "groundtruth.runtime.native_render",
    "GT_LANE_ENVELOPE": "groundtruth.runtime.evidence_envelope",
    "GT_EDIT_CHECK": "groundtruth.runtime.edit_check",
    "GT_VERIFY_EXECUTE": "groundtruth.runtime.covering_runner",
    "GT_OBLIGATION_FRESHNESS": "groundtruth.runtime.obligations",
}


def _module_findable(module: str) -> bool:
    """True iff ``module`` is importable-by-path WITHOUT executing it — a side-effect-
    free capability probe (``importlib.util.find_spec``). A definitive 'not found'
    (``find_spec`` → None) means the implementation is ABSENT; any probe-machinery error
    is treated as findable (conservative: the probe itself never triggers a FALSE abort).
    """
    import importlib.util as _ilu

    try:
        return _ilu.find_spec(module) is not None
    except Exception:
        return True


def _available_from_env(env: Mapping[str, str]) -> set[str]:
    """The capability-availability set for preflight — a REAL signal, not self-attestation.

    Precedence:
      1. ``GT_RL_PROFILE_AVAILABLE`` (comma-separated) — an explicit operator/CI stamp
         wins outright (e.g. narrowed from a baked-substrate manifest).
      2. else PROBE: a member is available iff its backing module
         (``_MEMBER_CAPABILITY_MODULE``) is importable-by-path in THIS interpreter;
         a member with no mapped module is assumed available. This makes the preflight
         ABORT reachable on a stale/partial substrate (Brief-F6: the prior default
         returned ALL members unconditionally, so ``preflight()`` was always ``[]`` for a
         known version — the fail-closed abort was structurally dead / self-attesting).

    Pure w.r.t. ``env``; the probe is side-effect-free (never executes member code).
    """
    raw = env.get("GT_RL_PROFILE_AVAILABLE")
    if raw is not None and raw.strip() != "":
        return {x.strip() for x in raw.split(",") if x.strip()}
    out: set[str] = set()
    for member in profile_members(_profile_token(env)):
        module = _MEMBER_CAPABILITY_MODULE.get(member)
        if module is None or _module_findable(module):
            out.add(member)
    return out


def main(
    argv: Optional[Sequence[str]] = None,
    env: Optional[Mapping[str, str]] = None,
    out=None,
    err=None,
) -> int:
    """CLI entry point sourced by ``gt_ae_block.sh``.

    ``--emit-exports``: print ``export GT_X=<shell-quoted-value>`` for each
    resolved member on success (rc 0). On a preflight abort, print
    ``GT_RL_PREFLIGHT_ABORT: ...`` to stderr, emit NO exports, and return 3.
    Profile OFF => rc 0, no output (byte-identical no-op).
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    env = os.environ if env is None else env
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err

    emit = "--emit-exports" in argv

    missing = preflight(env, _available_from_env(env))
    if missing:
        err.write(
            "GT_RL_PREFLIGHT_ABORT: profile=%s unavailable=%s\n"
            % (_profile_token(env) or "<unset>", ",".join(missing))
        )
        return 3

    if emit:
        for key, value in resolve_profile(env).items():
            out.write("export %s=%s\n" % (key, shlex.quote(value)))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
