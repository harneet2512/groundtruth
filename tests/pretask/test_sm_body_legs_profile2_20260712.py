"""SM (2026-07-12) — the two stratum-B BODY legs fire under GT_RL_PROFILE=2.

The only body-semantic retrieval surfaces are default-OFF:
  * GT_SEM_BODY   (graph_localizer.py — the semantic leg embeds per-symbol BODY passages
                   instead of name+signature)
  * GT_CONTENT_LEG (graph_localizer.py — per-symbol body-BM25 over symbol_content_fts,
                   fills the vacant lexical slot ONLY when the live-grep leg recalled
                   nothing -> mutually exclusive with grep -> never double-counts)

Both are the stratum-B (behavior-described) lever. This wave folds them into the
Super-Mode member set so ``GT_RL_PROFILE=2`` fans them ON. Preserved: (a) byte-identical
OFF (profile unset -> empty fan-out -> both flags OFF, localize byte-identical); (b) the
GT_CONTENT_LEG grep mutual-exclusion (unchanged — the leg's own `not grep_recalled` guard
still holds, so no lexical double-count); (c) leak=0 (the content leg excludes test
symbols at source; the legs surface FILE candidates, never test identity).

RED-first (the ranking fixture): on the pre-fix tree — equivalently under the MUTATION that
drops GT_CONTENT_LEG from ``_SUPER_MODE_MEMBERS`` — ``resolve_profile({"GT_RL_PROFILE":"2"})``
does NOT light the content leg, so the behaviour-described (no code symbol named) gold does
NOT surface. The assertion is a TRUE value assertion (gold present/absent among the
candidates), never a compile/attribute error.

HONESTY (CLAUDE.md): live efficacy of the body legs is MEASUREMENT-GATED (scripts/
measure_brief.py). This fixture proves the flag-flip + the deterministic content-leg
ranking effect; the measured stratum-B win is OWED to measure_brief, not claimed here.
GT_SEM_BODY's ranking effect needs the semantic embedder (measurement-gated), so only its
flag-flip is pinned here — not its rank delta.
"""
from __future__ import annotations

import sqlite3

import pytest

from groundtruth.pretask.graph_localizer import _normalize, localize
from groundtruth.runtime.rl_profile import PROFILE_MEMBERS, resolve_profile


def _fts5_available() -> bool:
    try:
        c = sqlite3.connect(":memory:")
        c.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        c.close()
        return True
    except sqlite3.OperationalError:
        return False


# --------------------------------------------------------------------------- #
# 1. The flag-flip: both body legs are Profile-2 members and fan out to "1".
# --------------------------------------------------------------------------- #
def test_body_legs_are_profile2_members():
    for flag in ("GT_SEM_BODY", "GT_CONTENT_LEG"):
        assert flag in PROFILE_MEMBERS["2"], flag
        # NOT part of Profile-1 (the stratum-B lever is a Super-Mode addition).
        assert flag not in PROFILE_MEMBERS["1"], flag
    got = resolve_profile({"GT_RL_PROFILE": "2"})
    assert got.get("GT_SEM_BODY") == "1"
    assert got.get("GT_CONTENT_LEG") == "1"
    # per-flag override still wins (byte-identical control surface preserved).
    ov = resolve_profile({"GT_RL_PROFILE": "2", "GT_CONTENT_LEG": "0"})
    assert ov.get("GT_CONTENT_LEG") == "0"


def test_profile_off_leaves_body_legs_dark():
    # byte-identical OFF: an unset profile touches neither flag.
    for env in ({}, {"GT_RL_PROFILE": ""}, {"GT_RL_PROFILE": "0"}):
        got = resolve_profile(env)
        assert "GT_SEM_BODY" not in got
        assert "GT_CONTENT_LEG" not in got


# --------------------------------------------------------------------------- #
# 2. The stratum-B ranking fixture: GT_RL_PROFILE=2 lights the content leg so a
#    behaviour-described issue surfaces the gold whose BODY (not name) carries the vocab.
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not _fts5_available(), reason="Python sqlite3 built without FTS5")
def test_profile2_lights_content_leg_for_stratum_b_gold(tiny_graph_db, monkeypatch):
    GOLD = "patroni/net.py"
    conn = sqlite3.connect(tiny_graph_db)
    # A generically-named function whose BODY (indexed content) carries the domain vocab,
    # with an edge so it is a connected component (mirrors a real indexed symbol).
    conn.execute(
        "INSERT INTO nodes (id,label,name,file_path,start_line,end_line,language,is_test) "
        "VALUES (99,'Function','handle_stream','patroni/net.py',1,9,'python',0)"
    )
    conn.execute(
        "INSERT INTO edges (source_id,target_id,type,source_line,source_file,resolution_method,confidence) "
        "VALUES (99,1,'CALLS',3,'patroni/net.py','name_match',0.9)"
    )
    conn.execute("CREATE VIRTUAL TABLE symbol_content_fts USING fts5(content)")
    conn.execute(
        "INSERT INTO symbol_content_fts(rowid,content) "
        "VALUES (99,'websocket upgrade handshake frame opcode payload masking')"
    )
    conn.commit()
    conn.close()

    # Behaviour vocab only — matches NO node name (no anchor); repo-less (no grep leg).
    issue = "websocket upgrade handshake fails: the opcode frame is rejected during masking"

    def _localize_under_profile(token: str):
        # exercise the WHOLE chain: profile -> resolve_profile fan-out -> os.environ ->
        # localize's call-time flag reads. Clear the leg flags first for isolation.
        for f in ("GT_CONTENT_LEG", "GT_SEM_BODY", "GT_RL_PROFILE"):
            monkeypatch.delenv(f, raising=False)
        fan = resolve_profile({"GT_RL_PROFILE": token}) if token else {}
        for k, v in fan.items():
            monkeypatch.setenv(k, v)
        return localize(issue, tiny_graph_db, top_k=8, repo_root=None)

    def _has_gold(res):
        return any(_normalize(c.file_path) == _normalize(GOLD) for c in res.candidates)

    # profile OFF -> legs dark -> the stratum-B gold does NOT surface (early-abstain control).
    off = _localize_under_profile("")
    assert not _has_gold(off), (
        "control: with the profile off the body legs are dark and the stratum-B gold "
        "must NOT surface"
    )

    # profile 2 -> the fan-out lights GT_CONTENT_LEG -> gold surfaces from body vocab.
    on = _localize_under_profile("2")
    assert _has_gold(on), (
        "GT_RL_PROFILE=2 must light the content leg so the stratum-B gold surfaces "
        f"(candidates: {[c.file_path for c in on.candidates]})"
    )
    # leak=0: no test-file candidate surfaced.
    assert not any("test" in _normalize(c.file_path).split("/")[-1] for c in on.candidates)
