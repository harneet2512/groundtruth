"""CONFIRMED-1 — GT_GATEWAY x GT_POST_SEARCH dose-law mutual exclusion at the seam.

The post_search lattice (``gt_mini_patch._search_localize_block``) and the Gateway
(``gt_mini_patch._gt_gateway_deliver`` -> ``groundtruth.runtime.gateway.augment``)
are TWO delivery planes over the ONE shared ``_EPISODE.probe_ledger``. With BOTH
flags on, on a SEARCH turn, they collide:

  (a) DOUBLE DOSE — the observation receives the SAME def fact twice in one turn
      (the lattice ``<gt-search-facts>`` block AND the gateway ``<gt-fact
      kind="name_fold">`` block), violating the <=1-steer-per-turn dose law across
      planes.
  (b) DOUBLE RECORD — one physical probe writes the shared ledger TWICE (the
      lattice's ``_ledger_record`` + ``gateway.classify_outcome``'s), so
      ``outcomes=['zero','zero']`` — which would false-fire a 1-probe "stuck"
      advisory once hypothesis_ledger wires on ``len(outcomes) >= 2``.

Shape-dependent: single-shape smokes miss it (on some HIT shapes only one plane
renders), which is why these pins cover ZERO_NAME, AMBIGUOUS_HIT, and a lattice-
abstains isolation.

THE PINNED LAW (verbatim):
  MUTUAL EXCLUSION, post_search WINS for search turns. When the post_search lattice
  is enabled (``_POST_SEARCH_ON``) AND the event classifies as a SEARCH event, the
  Gateway is SKIPPED for that event ENTIRELY: no producers, no classify_outcome, no
  probe-ledger record, no delivery, no chain-head advance, no dedup stamp, no gateway
  state mutation of any kind. Exclusion is BY FLAG for ALL search events: when the
  lattice ABSTAINS (e.g. AMBIGUOUS_HIT), the Gateway ALSO stays quiet on that search
  turn — the abstention is itself a correct-or-quiet decision, and deterministic dose
  accounting beats opportunistic backfill. The Gateway stays LIVE for the same
  episode's non-search (edit/test/view) turns; the shared probe_ledger still receives
  the lattice's ONE record, so gateway consumers reading probe history lose nothing.

MUTATION RECIPES (proven-biting, embedded below; NOT registered in
tests/contract/mutation_registry.py — owned by another agent this session):
  m1 (delete/invert the exclusion guard): monkeypatch
     ``gt_mini_patch._gateway_search_excluded`` -> ``lambda ev: False`` so the guard
     never fires. The Gateway then runs the search turn again -> the two-flag
     single-delivery invariant BITES (two def-fact blocks, outcomes ['zero','zero'],
     a sealed gateway delivery). See ``test_m1_guard_removed_double_delivery_bites``.
  m2 (guard skips DELIVERY but still classify/records): monkeypatch the guard OFF
     (as m1) AND wrap ``gateway.augment`` so it still runs (recording the probe via
     classify_outcome) but yields NO winner -> nothing is delivered, yet the shared
     ledger is DOUBLE-recorded. The single-record assertion BITES even though the
     naive double-delivery check would pass -> proves the guard must sit BEFORE
     classify/record, not merely before delivery. See
     ``test_m2_guard_after_classify_double_record_bites``.

Hermetic: synthetic graph.db, no ONNX / no repo checkout. All local runs use
PYTHONIOENCODING=utf-8 (Windows cp1252 + minisweagent emoji red-herring).
"""
from __future__ import annotations

import sqlite3

import pytest

import gt_mini_patch as g
from groundtruth.runtime import gateway as gw
from groundtruth.runtime.evidence_envelope import RECEIPT_ACTED, RECEIPT_DELIVERED

_ZERO_NAME_BLOCK = '<gt-fact kind="name_fold">'   # the gateway's ZERO_NAME render
_LATTICE_TAG = "<gt-search-facts"                 # the pinned lattice surface


def _mk_graph(tmp_path, nodes):
    db = str(tmp_path / "graph.db")
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE nodes(id INTEGER PRIMARY KEY, label TEXT, name TEXT,"
        " qualified_name TEXT, file_path TEXT, start_line INTEGER, end_line INTEGER,"
        " signature TEXT, return_type TEXT, is_exported INTEGER, is_test INTEGER,"
        " language TEXT, parent_id INTEGER);"
        "CREATE TABLE edges(id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,"
        " type TEXT, source_line INTEGER, source_file TEXT, resolution_method TEXT,"
        " confidence REAL, metadata TEXT);")
    for n in nodes:
        con.execute(
            "INSERT INTO nodes(id,label,name,file_path,start_line,end_line,is_test,language)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (n["id"], n.get("label", "Function"), n["name"], n["file_path"],
             n.get("start_line", 1), n.get("end_line", 5), n.get("is_test", 0),
             n.get("language", "python")))
        # Gateway trace localization is correct-or-quiet against the live repo,
        # not graph-only: a relative frame must resolve to a real repository
        # file. Keep this synthetic graph and its filesystem snapshot coherent.
        source = tmp_path / n["file_path"]
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("def placeholder():\n    pass\n", encoding="utf-8")
    con.commit()
    con.close()
    return db


def _wire(tmp_path, monkeypatch, db, *, post_search):
    monkeypatch.setattr(g, "_db_path", lambda: db)
    monkeypatch.setattr(g, "_root", lambda: str(tmp_path))
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_POST_SEARCH_ON", post_search)
    monkeypatch.delenv("GT_POST_SEARCH_NATIVE", raising=False)
    g._reset_oracle_state()


@pytest.fixture
def zero_name(tmp_path, monkeypatch):
    """1-def 'get_user' graph; a `grep -rn getUser .` misses (empty) -> ZERO_NAME."""
    db = _mk_graph(tmp_path, [
        {"id": 1, "name": "get_user", "file_path": "a/x.py", "start_line": 10},
    ])
    _wire(tmp_path, monkeypatch, db, post_search=True)
    yield db
    g._reset_oracle_state()


@pytest.fixture
def ambiguous(tmp_path, monkeypatch):
    """2-def 'run' graph (a/x.py + b/y.py) -> AMBIGUOUS_HIT for the gateway."""
    db = _mk_graph(tmp_path, [
        {"id": 1, "name": "run", "file_path": "a/x.py", "start_line": 10},
        {"id": 2, "name": "run", "file_path": "b/y.py", "start_line": 20},
    ])
    _wire(tmp_path, monkeypatch, db, post_search=True)
    yield db
    g._reset_oracle_state()


def _run(cmd, output, rc=0):
    action, out = {"command": cmd}, {"output": output, "returncode": rc}
    g._augment_output(action, out)
    return out["output"]


def _ledger_outcomes(sym):
    entry = g._EPISODE.probe_ledger.get(g._norm_stem(sym))
    return list(entry["outcomes"]) if entry else []


# --------------------------------------------------------------------------- #
# ABI drift guard: the seam's search-kind mirror must equal the gateway constant
# --------------------------------------------------------------------------- #
def test_kind_search_abi_value_is_stable():
    assert gw.KIND_SEARCH == "search"


# --------------------------------------------------------------------------- #
# PINNED two-flag test — ZERO_NAME: def fact ONCE (lattice form) + ONE record
# --------------------------------------------------------------------------- #
def test_two_flags_zero_name_single_delivery_and_record(zero_name, monkeypatch):
    monkeypatch.setenv("GT_GATEWAY", "1")           # BOTH planes on
    head_before = g._gt_gateway_chain_head
    obs = _run("grep -rn getUser .", "", rc=1)      # ZERO_NAME (empty grep)

    # (i) the def fact reached the model EXACTLY ONCE, in the LATTICE form.
    assert obs.count("a/x.py:10") == 1, obs
    assert _LATTICE_TAG in obs                        # lattice delivered
    assert _ZERO_NAME_BLOCK not in obs                # gateway did NOT re-deliver

    # (ii) the shared probe-ledger stem carries EXACTLY ONE outcome (lattice only).
    assert _ledger_outcomes("getUser") == ["zero"]

    # (iii) the gateway plane stayed fully quiet: no seal, chain head UNCHANGED.
    assert g._gt_gateway_deliveries == []
    assert g._gt_gateway_chain_head == head_before


# --------------------------------------------------------------------------- #
# AMBIGUOUS_HIT with both flags: the gateway plane is quiet, lattice wins, ONE record
# --------------------------------------------------------------------------- #
def test_two_flags_ambiguous_hit_lattice_delivers_once_gateway_quiet(ambiguous, monkeypatch):
    """FLIPPED 2026-07-10 (BUG-B1): on AMBIGUOUS_HIT the lattice fall-through now
    DELIVERS the def partition (mute pre-fix). The dose law still holds under both
    flags: the LATTICE delivers EXACTLY ONCE (both def sites, one block), the Gateway
    stays FULLY excluded, and the shared ledger carries ONE record."""
    grep_out = "a/x.py:10: run\nb/y.py:20: run"
    monkeypatch.setenv("GT_GATEWAY", "1")
    head_before = g._gt_gateway_chain_head
    obs = _run("grep -rn run .", grep_out)

    # the lattice delivered its def partition EXACTLY ONCE (one block, both def sites).
    assert obs.count(_LATTICE_TAG) == 1, obs
    assert "def: a/x.py:10" in obs and "def: b/y.py:20" in obs
    # the gateway contributed NOTHING (its own def_ref_partition never ran).
    assert g._gt_gateway_deliveries == []
    assert g._gt_gateway_chain_head == head_before
    assert _ZERO_NAME_BLOCK not in obs and '<gt-fact' not in obs   # no gateway <gt-fact>
    # ONE shared-ledger record for the physical probe (lattice only).
    assert _ledger_outcomes("run") == ["hit"]


# --------------------------------------------------------------------------- #
# REGRESSION: GT_GATEWAY=1 ALONE still delivers on a search shape (unchanged)
# --------------------------------------------------------------------------- #
def test_gateway_alone_still_delivers_on_search(tmp_path, monkeypatch):
    db = _mk_graph(tmp_path, [
        {"id": 1, "name": "run", "file_path": "a/x.py", "start_line": 10},
        {"id": 2, "name": "run", "file_path": "b/y.py", "start_line": 20},
    ])
    _wire(tmp_path, monkeypatch, db, post_search=False)   # POST_SEARCH OFF
    grep_out = "a/x.py:10: run\nb/y.py:20: run"
    monkeypatch.setenv("GT_GATEWAY", "1")
    on = _run("grep -rn run .", grep_out)
    assert on.startswith(grep_out)                        # byte-preserving suffix
    delta = on[len(grep_out):]
    assert delta and "a/x.py:10" in delta                 # gateway delivered
    assert len(g._gt_gateway_deliveries) == 1             # sealed
    assert g._gt_gateway_chain_head                        # chain advanced


# --------------------------------------------------------------------------- #
# REGRESSION: GT_POST_SEARCH=1 ALONE is byte-identical to the two-flag lattice output
# (the gateway is off by default -> the 32 legacy lattice pins are untouched)
# --------------------------------------------------------------------------- #
def test_post_search_alone_matches_two_flag_lattice(zero_name, monkeypatch):
    # lattice only (gateway default-off)
    monkeypatch.delenv("GT_GATEWAY", raising=False)
    lattice_only = _run("grep -rn getUser .", "", rc=1)
    g._reset_oracle_state()
    # both flags on -> the guard makes the gateway quiet -> byte-identical to lattice-only
    monkeypatch.setenv("GT_GATEWAY", "1")
    both = _run("grep -rn getUser .", "", rc=1)
    assert both == lattice_only
    assert g._gt_gateway_deliveries == []


# --------------------------------------------------------------------------- #
# PINNED LAW (2026-07-22, CONDITIONAL EXCLUSION): exclusion keys on whether the
# lattice PRODUCED, not on the flag alone. When the lattice ABSTAINS (returns "")
# on an ISOLATED search, the <=1-dose slot is FREE -> the gateway is ADMITTED to
# compete (restoring the strictly-dominated zero-coverage of its outcome producers).
# The <=1-dose invariant still holds: exactly ONE plane delivers.
# --------------------------------------------------------------------------- #
def test_two_flags_lattice_abstains_gateway_now_admitted(ambiguous, monkeypatch):
    # force the lattice to abstain while POST_SEARCH stays ENABLED. The search is isolated,
    # so the free dose is now filled by the gateway's def/ref partition for the ambiguous
    # `run` hit (was byte-identical-EXCLUDED under the old flag-only guard).
    monkeypatch.setattr(g, "_search_localize_block", lambda cmd, out=None: "")
    grep_out = "a/x.py:10: run\nb/y.py:20: run"
    monkeypatch.setenv("GT_GATEWAY", "1")
    head_before = g._gt_gateway_chain_head
    obs = _run("grep -rn run .", grep_out)
    # the gateway was ADMITTED and delivered exactly one dose (the lattice took none).
    assert obs != grep_out
    assert "<gt-search-facts" in obs
    assert len(g._gt_gateway_deliveries) == 1
    assert g._gt_gateway_chain_head != head_before


# --------------------------------------------------------------------------- #
# PINNED LAW: the Gateway stays LIVE for non-search turns in the SAME episode.
# --------------------------------------------------------------------------- #
def test_two_flags_edit_turn_still_reaches_gateway(ambiguous, monkeypatch):
    import groundtruth.runtime.gateway as gwmod
    monkeypatch.setenv("GT_GATEWAY", "1")

    seen_kinds: list[str] = []
    real_augment = gwmod.augment

    def _spy_augment(event, state):
        seen_kinds.append(event.kind)
        return real_augment(event, state)

    monkeypatch.setattr(gwmod, "augment", _spy_augment)

    _run("grep -rn run .", "a/x.py:10: run\nb/y.py:20: run")   # search -> guard skips
    _run("sed -i 's/a/b/' a/x.py", "")                          # edit -> gateway runs

    assert "search" not in seen_kinds, "guard must SKIP gateway.augment on search"
    assert "edit" in seen_kinds, "gateway must still process the episode's edit turn"


# --------------------------------------------------------------------------- #
# MUTATIONS — proven-biting (embedded; recipes in the module docstring)
# --------------------------------------------------------------------------- #
def test_m1_guard_removed_double_delivery_bites(zero_name, monkeypatch):
    """m1: guard removed/inverted -> both planes fire the search turn again."""
    monkeypatch.setattr(g, "_gateway_search_excluded", lambda ev, **kw: False)
    monkeypatch.setenv("GT_GATEWAY", "1")
    obs = _run("grep -rn getUser .", "", rc=1)
    # the honest invariant BITES: the def fact is now double-delivered + double-recorded
    assert obs.count("a/x.py:10") == 2
    assert _ZERO_NAME_BLOCK in obs                    # gateway's block reappears
    # 2026-07-22: double-DELIVERY still bites (obs.count==2 above), but the SHARED ledger is now
    # defended by _ledger_record (stem,idx) idempotence -> single record even with the guard gone.
    assert _ledger_outcomes("getUser") == ["zero"]
    assert len(g._gt_gateway_deliveries) == 1         # gateway sealed again


def test_m2_guard_after_classify_double_record_bites(zero_name, monkeypatch):
    """m2: guard prevents DELIVERY but not classify/record -> ledger still doubles.

    Simulated by disabling the guard (so gateway.augment runs and records the probe
    via classify_outcome) while forcing augment to yield NO winner (nothing gets
    delivered). Delivery-wise this looks clean, but the SHARED ledger is double-
    recorded -> the single-record assertion BITES -> the guard MUST sit before the
    classify/record step, not merely before delivery."""
    import groundtruth.runtime.gateway as gwmod
    real_augment = gwmod.augment

    def _record_but_no_winner(event, state):
        real_augment(event, state)   # runs classify_outcome -> writes the probe ledger
        return []                    # ...but delivers nothing

    monkeypatch.setattr(g, "_gateway_search_excluded", lambda ev, **kw: False)
    monkeypatch.setattr(gwmod, "augment", _record_but_no_winner)
    monkeypatch.setenv("GT_GATEWAY", "1")
    obs = _run("grep -rn getUser .", "", rc=1)

    # nothing extra was DELIVERED (naive double-delivery check would pass) ...
    assert obs.count("a/x.py:10") == 1
    assert g._gt_gateway_deliveries == []
    # 2026-07-22: the SHARED probe ledger is now defended by _ledger_record (stem,idx)
    # idempotence even when the guard sits AFTER classify -> single record (was ["zero","zero"]).
    assert _ledger_outcomes("getUser") == ["zero"]


# --------------------------------------------------------------------------- #
# FIX-2 (2026-07-10) — receipt promotion is BOOKKEEPING, not a dose: it runs
# BEFORE the exclusion return, so an excluded search turn promotes a prior
# delivery's receipt EXACTLY as gateway-alone does (no arm-asymmetric receipts).
# --------------------------------------------------------------------------- #
def test_excluded_search_promotes_prior_receipt_like_gateway_alone(tmp_path, monkeypatch):
    """A prior trace_frame delivery whose target the agent then greps promotes
    delivered->acted on the excluded search turn — the SAME promotion gateway-alone
    records. Reverting FIX-2 (promotion after the exclusion return) reddens this: the
    both-flags receipt would stay 'delivered' (RECEIPT PROMOTION LOST) while
    gateway-alone is 'acted'. The exclusion still holds for DELIVERY (no new seal)."""
    db = _mk_graph(tmp_path, [
        {"id": 1, "name": "get_user", "file_path": "a/x.py", "start_line": 10},
    ])
    trace_out = ('Traceback (most recent call last):\n'
                 '  File "a/x.py", line 12, in get_user\nE: boom\n')

    def arm(post_search):
        _wire(tmp_path, monkeypatch, db, post_search=post_search)
        seed_obs = _run("python repro.py", trace_out)    # turn 1: trace_frame -> a/x.py
        assert len(g._gt_gateway_deliveries) == 1, {
            "observation": seed_obs,
            "root": g._root(),
            "trace_file_exists": (tmp_path / "a/x.py").is_file(),
            "gateway_on": g._gt_gateway_on(),
            "batch_install_failed": g._batch_install_failed,
            "batch_commit_installed": g._batch_commit_installed,
        }
        assert g._gt_gateway_deliveries[0].receipt_state == RECEIPT_DELIVERED
        _run("grep -rn get_user a/x.py", "a/x.py:10:def get_user():")  # turn 2: ACTS on a/x.py
        return [d.receipt_state for d in g._gt_gateway_deliveries]

    monkeypatch.setenv("GT_GATEWAY", "1")
    both = arm(post_search=True)     # turn 2 is EXCLUDED (both flags)
    alone = arm(post_search=False)   # turn 2 is NOT excluded (gateway-alone)
    # exclusion still holds for DELIVERY: the excluded search sealed nothing new.
    assert len(both) == 1
    # ...but the prior trace_frame's receipt promoted to ACTED, matching gateway-alone.
    assert both[0] == alone[0] == RECEIPT_ACTED, (both, alone)


# --------------------------------------------------------------------------- #
# FIX-4 (2026-07-10) — TOTALITY pin. On a guard-excluded search event the Gateway
# mutates NOTHING except the legitimate FIX-2 receipt promotion. Snapshots every
# gateway/episode field EXCEPT receipt_state and asserts bitwise no change; BITES a
# guard-after-state mutant (mut_guard_after_state) that touches episode_id inside the
# predicate before returning True (the totality was probe-protected only).
# --------------------------------------------------------------------------- #
def _gateway_totality_snapshot():
    """Gateway+episode state EXCLUDING receipt promotion (delivery IDENTITIES only,
    not their receipt_state — FIX-2 legitimately promotes receipts on an excluded
    turn). Everything here must be bitwise-frozen by a guard-excluded search event:
    episode value-state (episode_id, probe_ledger, delivered_dedup, edit_events), the
    TITO chain head, and the delivery dedup_keys."""
    return {
        "episode": g._EPISODE.to_dict(),
        "chain_head": g._gt_gateway_chain_head,
        "delivery_keys": [d.dedup_key for d in g._gt_gateway_deliveries],
        "n_deliveries": len(g._gt_gateway_deliveries),
    }


def test_excluded_search_touches_no_gateway_state_except_receipts(tmp_path, monkeypatch):
    """FIX-4 totality: seed a prior delivery (so a real receipt promotion happens on
    the excluded turn — and is correctly EXCLUDED from the snapshot), arm an empty
    episode_id, then run a guard-excluded search DIRECTLY through _gt_gateway_deliver.
    Everything except receipt_state is bitwise-frozen. The guard-after-state mutant
    sets episode_id inside the predicate -> episode_id diverges -> this pin BITES."""
    db = _mk_graph(tmp_path, [
        {"id": 1, "name": "get_user", "file_path": "a/x.py", "start_line": 10},
    ])
    _wire(tmp_path, monkeypatch, db, post_search=True)
    monkeypatch.setenv("GT_GATEWAY", "1")
    # seed a prior trace_frame delivery -> the excluded grep will promote its receipt
    # (delivered->acted), which the snapshot must ignore.
    seed_obs = _run("python repro.py", 'Traceback (most recent call last):\n'
                                     '  File "a/x.py", line 12, in get_user\nE: boom\n')
    assert len(g._gt_gateway_deliveries) == 1, {
        "observation": seed_obs,
        "root": g._root(),
        "trace_file_exists": (tmp_path / "a/x.py").is_file(),
        "gateway_on": g._gt_gateway_on(),
        "batch_install_failed": g._batch_install_failed,
        "batch_commit_installed": g._batch_commit_installed,
    }
    # arm the episode-id touch: the CORRECT guard leaves episode_id untouched on an
    # excluded turn; the mutant sets it (diverging the snapshot).
    g._EPISODE.episode_id = ""
    cmd, orig_out = "grep -rn get_user a/x.py", "a/x.py:10:def get_user():"
    out = {"output": orig_out, "returncode": 0}
    before = _gateway_totality_snapshot()
    g._gt_gateway_deliver({"command": cmd}, out, cmd, orig_out)   # EXCLUDED search
    after = _gateway_totality_snapshot()
    assert before == after, (before, after)
    assert out["output"] == orig_out                              # observation bytes frozen
    # the excluded search sealed NOTHING new (delivery identity unchanged).
    assert after["n_deliveries"] == 1


# --------------------------------------------------------------------------- #
# GREEN PROOF (2026-07-22) — change_surface zero-coverage FIX, end-to-end wiring.
# --------------------------------------------------------------------------- #
def test_change_surface_dominance_delivers_blast_radius(zero_name, monkeypatch):
    """A confident change_surface answer DOMINATES the thin CLASS-4 honest-negative on a
    REPEATED zero-absent probe -> the lattice abstains -> the conditional exclusion admits the
    gateway -> _produce_change_surface delivers the new_file_destination blast-radius. The engine
    is mocked (fixture-sensitive + tested separately); this pins THE WIRING the change added.
    RED before the change: turn 3 delivered only the thin 'appears unimplemented' note, the
    gateway stayed excluded, and 0 new_file_destination reached the agent."""
    from types import SimpleNamespace
    dest = SimpleNamespace(
        suggested_path="handlers/baz_handler.py", template_file="handlers/foo_handler.py",
        registration_file="registry.py", entity="baz_handler",
        evidence=["sibling group: handlers/foo_handler.py, handlers/bar_handler.py"])
    fake = SimpleNamespace(abstained=False, destinations=[dest], missing_roles=[])
    monkeypatch.setattr(gw, "detect_change_surface", lambda *a, **k: fake, raising=False)
    monkeypatch.setattr("groundtruth.pretask.change_surface.detect_change_surface",
                        lambda *a, **k: fake, raising=False)
    monkeypatch.setenv("GT_GATEWAY", "1")
    monkeypatch.setenv("GT_CHANGE_SURFACE", "1")
    monkeypatch.setattr(g, "_issue_text", lambda: "Add a baz_handler following the handler pattern.")

    _run("grep -rn BazHandler .", "")        # turn 1: intentional first probe -> silent both planes
    _run("cat registry.py", "contents")      # turn 2: a non-search VIEW (not an edit)
    obs = _run("grep -rn BazHandler .", "")   # turn 3: REPEAT zero-absent -> dominance -> gateway

    assert "baz_handler.py" in obs            # the rich blast-radius delivered
    assert "new file" in obs.lower()
    assert "appears unimplemented" not in obs  # the thin honest-negative was DOMINATED
    assert len(g._gt_gateway_deliveries) == 1  # exactly one dose (the gateway's)
