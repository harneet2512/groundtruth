"""WIDE-11: empty profile environment must use the Profile-2 preflight set."""

from groundtruth.runtime import rl_profile as rp


def test_empty_profile_preflight_matches_explicit_profile2(monkeypatch):
    monkeypatch.setattr(rp, "_manifest_preflight", lambda env, token: [])
    available = set(rp.PROFILE_MEMBERS["2"])
    explicit = rp.preflight({"GT_RL_PROFILE": "2"}, available)
    assert rp.preflight({"GT_RL_PROFILE": ""}, available) == explicit == []


def test_empty_profile_reports_same_missing_member_as_explicit(monkeypatch):
    monkeypatch.setattr(rp, "_manifest_preflight", lambda env, token: [])
    available = set(rp.PROFILE_MEMBERS["2"])
    missing = sorted(available)[0]
    available.remove(missing)
    explicit = rp.preflight({"GT_RL_PROFILE": "2"}, available)
    assert explicit == [missing]
    assert rp.preflight({"GT_RL_PROFILE": ""}, available) == explicit
