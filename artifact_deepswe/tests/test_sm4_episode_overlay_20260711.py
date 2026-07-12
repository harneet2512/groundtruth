"""SM-4 (2026-07-11) — the LIVE episode-overlay freshness enforcement.

`certified base graph + incremental changed-file facts + edit-overlay = current episode
graph VIEW` — never mutate the certified base. SM-4 makes SM-0's freshness enforcement REAL:
after the agent structurally edits a file F, a Gateway fact READ from the (frozen, ro-mounted)
base graph.db about F's OLD structure is DROPPED as stale, not shipped. A fact about an
UNEDITED file is unaffected. All gated behind ``GT_EDIT_OVERLAY`` (default OFF ⇒ byte-identical).

PINNED HERE (RED-first; ≥2 biting mutations named per block):
  * edit_overlay.build_episode_overlay_entry — the O(file) reparse-and-compare that marks a
    file ``changed`` when a recorded def signature no longer appears in the CURRENT content
    (mark_stale_envelopes verdict); UNKNOWN (no base nodes) -> fail-quiet (not stale).
  * edit_overlay.compose_episode_revision — ONE current episode revision (base + per-file rev).
  * gateway.route_delivery — the SM-4 stale-drop, target-scoped, edit-turn-exempt, flag-gated.
  * gateway.augment — a base-read def fact about an EDITED file is dropped; an UNEDITED file
    still ships (the model-facing behavior).
  * gt_mini_patch — the seam builds + stores the overlay per structured edit.

MUTATIONS bitten:
  M1 (drop the stale-drop): remove ``return ROUTE_STALE`` in the SM-4 block -> a stale fact
     ships -> `test_route_delivery_drops_stale_edited_file` + `test_augment_drops_...` bite.
  M2 (ignore the flag): make the drop apply regardless of GT_EDIT_OVERLAY -> the flag-OFF
     path changes -> `test_flag_off_is_byte_identical` + `test_route_delivery_flag_off` bite.
  M3 (reparse mutates the base): any write to graph.db in the reparse -> the immutability
     asserts in `test_build_entry_*` bite (graph.db bytes must be unchanged).

Deterministic: real in-memory-on-disk sqlite graph.db, no Go toolchain, no network, no task IDs.
"""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import gt_mini_patch as g  # noqa: E402

import groundtruth.runtime.gateway as gw  # noqa: E402
from groundtruth.runtime import edit_overlay as eo  # noqa: E402
from groundtruth.runtime.evidence_envelope import EvidenceEnvelope  # noqa: E402


# --------------------------------------------------------------------------- #
# graph.db builders (a REAL sqlite base graph — never mutated by the overlay).
# --------------------------------------------------------------------------- #
_SEQ = [0]


def _mk_graph(tmp_path, nodes, *, name=None):
    _SEQ[0] += 1
    db = str(tmp_path / (name or f"graph{_SEQ[0]}.db"))
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE nodes(id INTEGER PRIMARY KEY, label TEXT, name TEXT,"
        " qualified_name TEXT, file_path TEXT, start_line INTEGER, end_line INTEGER,"
        " signature TEXT, return_type TEXT, is_exported INTEGER, is_test INTEGER,"
        " language TEXT, parent_id INTEGER);"
        "CREATE TABLE edges(id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,"
        " type TEXT, source_line INTEGER, source_file TEXT, resolution_method TEXT,"
        " confidence REAL, metadata TEXT);"
        "CREATE TABLE project_meta(key TEXT PRIMARY KEY, value TEXT);"
    )
    for n in nodes:
        con.execute(
            "INSERT INTO nodes(id,label,name,file_path,start_line,signature,is_test,language)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (n["id"], n.get("label", "Function"), n["name"], n["file_path"],
             n.get("start_line", 1), n.get("signature", ""), n.get("is_test", 0), "python"))
    con.commit()
    con.close()
    return db


# =========================================================================== #
# 1. edit_overlay.build_episode_overlay_entry — the O(file) reparse-and-compare.
# =========================================================================== #
def test_build_entry_signature_change_is_changed(tmp_path):
    """A recorded def signature that no longer appears in the CURRENT content -> changed=True
    (the mark_stale_envelopes verdict). M3: assert graph.db bytes are UNCHANGED (the base is
    ro; the reparse must never write it)."""
    db = _mk_graph(tmp_path, [
        {"id": 1, "name": "get_user", "file_path": "mod.py",
         "signature": "def get_user(id):"},
    ])
    before_bytes = open(db, "rb").read()
    # agent changed the signature: the OLD "def get_user(id):" no longer occurs.
    entry = eo.build_episode_overlay_entry(
        db, "mod.py", "def get_user(id, extra):\n    return db.get(id)\n")
    assert entry["state"] == "available"
    assert entry["changed"] is True
    assert entry["n_defs"] == 1
    # M3 (immutability): the certified base was NOT mutated by the reparse.
    assert open(db, "rb").read() == before_bytes


def test_build_entry_body_only_edit_is_fresh(tmp_path):
    """A body-only / comment-only edit that PRESERVES the recorded signature -> changed=False
    (the refinement: only a structural change stales, so a fresh fact is not over-dropped)."""
    db = _mk_graph(tmp_path, [
        {"id": 1, "name": "get_user", "file_path": "mod.py",
         "signature": "def get_user(id):"},
    ])
    entry = eo.build_episode_overlay_entry(
        db, "mod.py", "def  get_user(id):\n    return db.get(id) + 1  # reworked body\n")
    assert entry["state"] == "available"
    assert entry["changed"] is False  # signature intact (whitespace-insensitive match)


def test_build_entry_unknown_when_no_base_nodes_fails_quiet(tmp_path):
    """No recorded def structure for the file (new file / unindexed) -> UNKNOWN, changed=False
    (deliverable 4: fail QUIET — never a fabricated stale on an unprovable comparison)."""
    db = _mk_graph(tmp_path, [
        {"id": 1, "name": "other", "file_path": "elsewhere.py", "signature": "def other():"},
    ])
    entry = eo.build_episode_overlay_entry(db, "mod.py", "def brand_new():\n    pass\n")
    assert entry["state"] == "unknown"
    assert entry["changed"] is False
    # a missing db is also fail-quiet (never raises, never stale).
    e2 = eo.build_episode_overlay_entry(str(tmp_path / "nope.db"), "mod.py", "x = 1\n")
    assert e2["state"] == "unknown" and e2["changed"] is False


def test_graph_def_signatures_are_file_scoped(tmp_path):
    """F1 (LIPI): the per-file reparse must be O(changed file), NOT O(repo). The SQL is an
    INDEXED EQUALITY on file_path, so ONLY the target file's def signatures are consulted —
    a DIFFERENT file's defs are NEVER materialized. Two files share the def name ``alpha`` with
    DIFFERENT signatures; querying file A must return A's signature ONLY (B's excluded).

    BITING MUTATION: revert to the unfiltered ``SELECT ... WHERE is_test=0 AND label IN (...)``
    (drop ``AND file_path = ?``) -> B's ``def alpha(x, y):`` leaks into A's set and this assert
    fails; the changed-verdict assert below also flips (present ⊂ graph_defs -> changed=True)."""
    db = _mk_graph(tmp_path, [
        {"id": 1, "name": "alpha", "file_path": "mod_a.py", "signature": "def alpha(x):"},
        {"id": 2, "name": "alpha", "file_path": "mod_b.py", "signature": "def alpha(x, y):"},
    ])
    # only A's signature is consulted for A (B's differently-signatured alpha is NOT).
    assert eo._graph_def_signatures(db, "mod_a.py") == ("def alpha(x):",)
    assert eo._graph_def_signatures(db, "mod_b.py") == ("def alpha(x, y):",)
    # end-to-end scoping consequence: A's current content still holds A's own signature -> FRESH.
    # (Under the unfiltered scan, B's "def alpha(x, y):" — absent from A's content — would drag
    # the verdict to changed=True; scoping keeps it correctly FRESH.)
    entry = eo.build_episode_overlay_entry(db, "mod_a.py", "def alpha(x):\n    return x\n")
    assert entry["state"] == "available"
    assert entry["n_defs"] == 1          # ONLY A's one def, not A+B
    assert entry["changed"] is False


def test_build_entry_ignores_test_nodes(tmp_path):
    """is_test nodes are excluded from the recorded structure (leak/scope discipline): a file
    whose ONLY nodes are test nodes yields UNKNOWN, not a spurious comparison."""
    db = _mk_graph(tmp_path, [
        {"id": 1, "name": "test_thing", "file_path": "mod.py",
         "signature": "def test_thing():", "is_test": 1},
    ])
    entry = eo.build_episode_overlay_entry(db, "mod.py", "def test_thing(): pass\n")
    assert entry["state"] == "unknown"


# =========================================================================== #
# 2. compose_episode_revision — ONE current episode revision, deterministic.
# =========================================================================== #
def test_compose_episode_revision():
    # empty overlay -> the base revision UNCHANGED (byte-identical to no-overlay).
    assert eo.compose_episode_revision("BASE", {}) == "BASE"
    assert eo.compose_episode_revision("BASE", None) == "BASE"
    ov1 = {"a.py": {"overlay_rev": "r1"}, "b.py": {"overlay_rev": "r2"}}
    r = eo.compose_episode_revision("BASE", ov1)
    assert r and r != "BASE"
    # deterministic: same inputs -> same revision (path order does not matter).
    ov1_reordered = {"b.py": {"overlay_rev": "r2"}, "a.py": {"overlay_rev": "r1"}}
    assert eo.compose_episode_revision("BASE", ov1_reordered) == r
    # advances when a file's per-file rev changes (a new edit to a.py).
    ov2 = {"a.py": {"overlay_rev": "r1_NEW"}, "b.py": {"overlay_rev": "r2"}}
    assert eo.compose_episode_revision("BASE", ov2) != r


def test_overlay_target_stale_predicate():
    ov = {"a.py": {"file": "a.py", "changed": True},
          "b.py": {"file": "b.py", "changed": False}}
    assert eo.overlay_target_stale(ov, "a.py") is True     # edited + changed
    assert eo.overlay_target_stale(ov, "b.py") is False    # edited but body-only
    assert eo.overlay_target_stale(ov, "c.py") is False     # UNEDITED -> unaffected
    assert eo.overlay_target_stale(ov, "./a.py") is True    # path normalization
    assert eo.overlay_target_stale({}, "a.py") is False
    assert eo.overlay_target_stale(None, "a.py") is False


# =========================================================================== #
# 3. gateway.route_delivery — the SM-4 stale-drop (unit).
# =========================================================================== #
def _env(target: str, evidence_type: str = "def_ref_partition",
         preferred_event: str = "search") -> EvidenceEnvelope:
    return EvidenceEnvelope.build(
        producer="p", fact_id="x", target=target, evidence_type=evidence_type,
        payload=(f"def: {target}:1",), provenance=((target, 1),), confidence=0.9,
        tier="VERIFIED", graph_revision="", valid_until="", preferred_event=preferred_event,
    )


def test_route_delivery_drops_stale_edited_file(monkeypatch):
    """A base-read fact whose TARGET file was structurally edited (overlay changed) is dropped
    STALE under the flag. M1 (bites): remove ``return ROUTE_STALE`` -> DELIVER, this fails."""
    monkeypatch.setenv("GT_EDIT_OVERLAY", "1")
    st = gw.GatewayState(episode_overlay={"a.py": {"file": "a.py", "changed": True}})
    ev = gw.ToolEvent(kind="search")  # not the edit turn -> no changed_files exemption
    assert gw.route_delivery(_env("a.py"), ev, st) == gw.ROUTE_STALE


def test_route_delivery_unedited_file_unaffected(monkeypatch):
    """A fact about a DIFFERENT, unedited file is delivered — the drop is target-scoped."""
    monkeypatch.setenv("GT_EDIT_OVERLAY", "1")
    st = gw.GatewayState(episode_overlay={"a.py": {"file": "a.py", "changed": True}})
    ev = gw.ToolEvent(kind="search")
    assert gw.route_delivery(_env("b.py"), ev, st) == gw.ROUTE_DELIVER


def test_route_delivery_body_only_change_not_dropped(monkeypatch):
    """An edited file whose entry is changed=False (body-only edit) is NOT dropped."""
    monkeypatch.setenv("GT_EDIT_OVERLAY", "1")
    st = gw.GatewayState(episode_overlay={"a.py": {"file": "a.py", "changed": False}})
    assert gw.route_delivery(_env("a.py"), gw.ToolEvent(kind="search"), st) == gw.ROUTE_DELIVER


def test_route_delivery_edit_turn_exemption(monkeypatch):
    """A fact about a file edited in THIS observation (changed_files) is EXEMPT — the edit-turn
    edit-derived fact is fresh, not a stale base read (protects patch_delta on the edit turn)."""
    monkeypatch.setenv("GT_EDIT_OVERLAY", "1")
    st = gw.GatewayState(episode_overlay={"a.py": {"file": "a.py", "changed": True}})
    ev = gw.ToolEvent(kind="edit", changed_files=("a.py",))
    # an EDIT-boundaried envelope (patch_delta) about the just-edited a.py: the SM-4 stale-drop
    # is EXEMPT (edit-turn fresh fact) and the timing check delivers (edit==edit).
    env = _env("a.py", evidence_type="signature_mismatch", preferred_event="edit")
    assert gw.route_delivery(env, ev, st) == gw.ROUTE_DELIVER


def test_route_delivery_flag_off(monkeypatch):
    """Flag OFF -> the overlay is never consulted (byte-identical). M2 (bites): make the drop
    ignore GT_EDIT_OVERLAY -> flag-off DROPS, this DELIVER assert fails."""
    monkeypatch.delenv("GT_EDIT_OVERLAY", raising=False)
    st = gw.GatewayState(episode_overlay={"a.py": {"file": "a.py", "changed": True}})
    assert gw.route_delivery(_env("a.py"), gw.ToolEvent(kind="search"), st) == gw.ROUTE_DELIVER


# =========================================================================== #
# 4. gateway.augment — the MODEL-FACING behavior (a stale fact is DROPPED end-to-end).
# =========================================================================== #
def _ambiguous_search(tmp_path):
    # foo defined in TWO files -> AMBIGUOUS_HIT -> _produce_def_ref_partition, target = "a.py".
    db = _mk_graph(tmp_path, [
        {"id": 1, "name": "foo", "file_path": "a.py", "signature": "def foo():"},
        {"id": 2, "name": "bar", "file_path": "b.py", "signature": "def bar():"},
        {"id": 3, "name": "foo", "file_path": "c.py", "signature": "def foo():"},
    ])
    ev = gw.ToolEvent(kind="search", command="grep -rn foo .",
                      output="a.py:1:def foo\nc.py:1:def foo", action_index=0)
    return db, ev


def test_augment_ships_the_def_fact_without_overlay(tmp_path, monkeypatch):
    """Baseline (pre-SM-4 world): with NO overlay the base-read def fact about a.py SHIPS."""
    monkeypatch.setenv("GT_GATEWAY", "1")
    monkeypatch.setenv("GT_EDIT_OVERLAY", "1")  # flag on but overlay empty -> no drop
    db, ev = _ambiguous_search(tmp_path)
    st = gw.GatewayState(graph_db=db, repo_root=str(tmp_path))
    envs = gw.augment(ev, st)
    assert any(e.evidence_type == "def_ref_partition" and e.target == "a.py" for e in envs)


def test_augment_drops_stale_fact_about_edited_file(tmp_path, monkeypatch):
    """THE model-facing SM-4 behavior: the agent edited a.py (overlay changed) -> the base-read
    def fact about a.py is DROPPED. Same input -> same drop (deterministic). M1 bites here."""
    monkeypatch.setenv("GT_GATEWAY", "1")
    monkeypatch.setenv("GT_EDIT_OVERLAY", "1")
    db, ev = _ambiguous_search(tmp_path)
    st = gw.GatewayState(graph_db=db, repo_root=str(tmp_path),
                         episode_overlay={"a.py": {"file": "a.py", "changed": True}})
    envs = gw.augment(ev, st)
    assert not any(e.target == "a.py" for e in envs)  # the stale a.py fact is gone
    # SUPPRESSION only — a drop adds NO model-facing bytes (leak=0 by construction: the
    # dropped fact simply never reaches a renderer). Deterministic re-run -> same drop.
    db2, ev2 = _ambiguous_search(tmp_path)
    st2 = gw.GatewayState(graph_db=db2, repo_root=str(tmp_path),
                          episode_overlay={"a.py": {"file": "a.py", "changed": True}})
    assert not any(e.target == "a.py" for e in gw.augment(ev2, st2))


def test_flag_off_is_byte_identical(tmp_path, monkeypatch):
    """The delivered set with GT_EDIT_OVERLAY OFF is IDENTICAL whether or not the overlay marks
    a.py changed — off is byte-identical. M2 bites here."""
    monkeypatch.setenv("GT_GATEWAY", "1")
    monkeypatch.delenv("GT_EDIT_OVERLAY", raising=False)

    def _run(with_overlay):
        db, ev = _ambiguous_search(tmp_path)
        overlay = {"a.py": {"file": "a.py", "changed": True}} if with_overlay else {}
        st = gw.GatewayState(graph_db=db, repo_root=str(tmp_path), episode_overlay=overlay)
        return [(e.evidence_type, e.target) for e in gw.augment(ev, st)]

    base = _run(with_overlay=False)
    assert ("def_ref_partition", "a.py") in base       # the fact is produced
    assert _run(with_overlay=True) == base             # overlay is inert with the flag OFF


# =========================================================================== #
# 5. gt_mini_patch seam — the overlay is BUILT + STORED per structured edit.
# =========================================================================== #
@pytest.fixture
def seam_repo(monkeypatch, tmp_path):
    # foo.py already carries the agent's applied edit (a signature change: added param b).
    (tmp_path / "foo.py").write_text("def f(a, b):\n    return a\n", encoding="utf-8")
    db = _mk_graph(tmp_path, [
        {"id": 1, "name": "f", "file_path": "foo.py", "signature": "def f(a):"},
    ], name="seam_graph.db")
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_root", lambda: str(tmp_path))
    monkeypatch.setattr(g, "_db_path", lambda: db)
    monkeypatch.setattr(g, "_gt_gateway_deliveries", [], raising=False)
    g._gt_overlay_last = None
    g._gt_episode_overlay.clear()
    return tmp_path, db


def _sig_change_action():
    return {"command": "str_replace", "path": "foo.py",
            "old_str": "def f(a):", "new_str": "def f(a, b):"}


def test_seam_builds_and_stores_changed_overlay(seam_repo, monkeypatch):
    """The seam builds the episode overlay per structured edit + stores it in
    _gt_episode_overlay with changed=True (the recorded ``def f(a):`` is gone from the edited
    file). The certified base graph.db is NOT mutated (M3 parity at the seam)."""
    _tmp, db = seam_repo
    monkeypatch.setenv("GT_EDIT_OVERLAY", "1")
    before_bytes = open(db, "rb").read()

    g._gt_edit_overlay_transaction(_sig_change_action())

    assert "foo.py" in g._gt_episode_overlay
    entry = g._gt_episode_overlay["foo.py"]
    assert entry["state"] == "available"
    assert entry["changed"] is True
    assert g._gt_overlay_last is not None
    assert g._gt_overlay_last["overlay_changed"] is True
    assert g._gt_overlay_last["episode_rev"]  # composed episode revision is non-empty
    # the ro-mounted base was never written by the reparse.
    assert open(db, "rb").read() == before_bytes


def test_seam_flag_off_populates_no_overlay(seam_repo, monkeypatch):
    """Flag OFF -> the seam is a byte-identical no-op: the overlay stays empty."""
    _tmp, _db = seam_repo
    monkeypatch.delenv("GT_EDIT_OVERLAY", raising=False)
    g._gt_edit_overlay_transaction(_sig_change_action())
    assert g._gt_episode_overlay == {}
    assert g._gt_overlay_last is None


def test_reset_clears_episode_overlay(seam_repo, monkeypatch):
    """_reset_oracle_state clears the per-attempt overlay (a retry starts fresh)."""
    _tmp, _db = seam_repo
    monkeypatch.setenv("GT_EDIT_OVERLAY", "1")
    g._gt_edit_overlay_transaction(_sig_change_action())
    assert g._gt_episode_overlay  # populated
    g._reset_oracle_state()
    assert g._gt_episode_overlay == {}
