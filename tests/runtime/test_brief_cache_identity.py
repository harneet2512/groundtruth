"""Pin: brief_cache must NEVER serve a stale brief from a different task (the
awilix↔geo cross-task contamination, 2026-06-15). A reused out_dir (a codespace
running several tasks through one /tmp/gt) must regenerate per (issue, graph),
not return a prior task's cached brief."""

import tempfile

from groundtruth.runtime import brief_cache as bc


def test_stale_cross_task_brief_is_not_served():
    d = tempfile.mkdtemp()
    id_geo = bc.request_identity("geo: s2 shapeindex serialization", "/tmp/geo.db")
    bc.persist_brief(d, "GEO BRIEF: edit s2/shapeindex.go", None, identity=id_geo)
    # A DIFFERENT task asks against the SAME out_dir -> MUST miss (regenerate).
    id_awilix = bc.request_identity("awilix: container.initialize concurrency", "/tmp/awilix.db")
    assert bc.load_cached_brief(d, expect_identity=id_awilix) is None
    # The ORIGINAL task still gets its own brief back (within-task reuse preserved).
    same = bc.load_cached_brief(d, expect_identity=id_geo)
    assert same is not None and "GEO" in same["brief_text"]


def test_guard_is_load_bearing():
    # Without the identity check (the old code path) the stale brief WOULD be served.
    d = tempfile.mkdtemp()
    bc.persist_brief(d, "STALE", None, identity=bc.request_identity("a", "/g"))
    assert bc.load_cached_brief(d) is not None  # no expect_identity -> returns it (the bug)
    assert bc.load_cached_brief(d, expect_identity="other") is None  # guarded -> rejected


def test_get_or_generate_regenerates_for_a_new_issue():
    d = tempfile.mkdtemp()
    calls = []

    def gen(issue_text, repo_root, graph_db, bug_id):
        calls.append(issue_text)
        r = type("R", (), {})()
        r.brief_text = "BRIEF::" + issue_text[:12]
        return r

    bc.get_or_generate(d, "geo issue", "/w", "/tmp/g1.db", generator=gen)
    bc.get_or_generate(d, "awilix issue", "/w", "/tmp/g2.db", generator=gen)
    assert len(calls) == 2  # regenerated for the second issue, did NOT serve geo's brief
