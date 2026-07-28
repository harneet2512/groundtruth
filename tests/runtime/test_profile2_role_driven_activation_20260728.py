"""Profile-2 activates role-driven behavior without corrupting CAP inventory."""

from __future__ import annotations

from groundtruth.runtime import rl_profile


FLAG = "GT_ROLE_DRIVEN_COALITION"


def test_role_driven_coalition_is_profile2_behavior_not_inventory() -> None:
    assert FLAG not in rl_profile.PROFILE_MEMBERS["1"]
    assert FLAG not in rl_profile.PROFILE_MEMBERS["2"]
    assert FLAG in rl_profile.PROFILE_BEHAVIOR_FLAGS["2"]


def test_profile2_resolves_role_driven_on_but_explicit_off_remains_a_kill_switch() -> None:
    defaults = rl_profile.resolve_profile_defaults({"GT_RL_PROFILE": "2"})
    assert defaults[FLAG] == "1"

    # This is the seam's load-bearing operation: resolve the full default map,
    # then apply it with setdefault so an explicit "0" survives.
    env = {"GT_RL_PROFILE": "2", FLAG: "0"}
    for name, value in rl_profile.resolve_profile_defaults(env).items():
        env.setdefault(name, value)
    assert env[FLAG] == "0"
