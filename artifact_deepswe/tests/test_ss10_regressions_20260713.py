"""SS-10 — the FOUR replay-oracle regressions + the chars=0-delivered honesty invariant.

Every test here is a DETERMINISTIC synthetic reproduction derived from a byte-verified
replay-oracle case (tests/fixtures/ss_replay/cases.json) — NOT a task-keyed hack. Each
drives the real seam (or the pure helper) with the recorded EVENT SHAPE and asserts the
fixed behaviour; each fails RED on the pre-fix seam.

  R1 (CARDINAL, conan-17123) consensus.scope moved iter12/review_transition -> a later
     test_result event under SS-on. A preserved correct-time delivery must NOT be deferred.
  R2 (babel-1179) the ACCURATE coherence firing was KILLED because an EARLIER passing test
     (m74/m80) fell between the first and last write, and _ss_coherence_churn spanned it.
     Fix: count the CURRENT blind-overwrite streak = writes SINCE the last passing test.
  R3 (conan-17092 / matplotlib / pi …) coherence OVER-counts: cat READS, grep VIEWs, /tmp
     scratch-creates and moves were counted as verified writes. Fix: a write-classifier that
     counts ONLY genuine real-source writes; heredoc-redirect targets (`cat <<EOF > file`)
     ARE writes.
  R4 (loguru / dvc / haystack …) ss_late + ss_provenance never fired on the lane plane
     (Lane-A / steer pool-add) — only the gateway chokepoint screened. Wire the SAME screen
     into the lane/steer pool-add.
  CHARS0 (hydra-3005 ga.trace_frame; coherence/recovery V2 measurement; ss_ack) a row with
     outcome="delivered" and chars<=0 is a lie. No row on any plane may claim it.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

import gt_mini_patch as g  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# fixtures / harness
# ─────────────────────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    for k in list(os.environ):
        if k.startswith("GT_SS_"):
            monkeypatch.delenv(k, raising=False)
    g._reset_oracle_state()
    yield


def _build_graph(db_path: str) -> None:
    con = sqlite3.connect(db_path)
    try:
        con.executescript(
            "CREATE TABLE nodes(id INTEGER PRIMARY KEY, label TEXT, name TEXT,"
            " qualified_name TEXT, file_path TEXT, start_line INTEGER, end_line INTEGER,"
            " signature TEXT, return_type TEXT, is_exported INTEGER, is_test INTEGER,"
            " language TEXT, parent_id INTEGER);"
            "CREATE TABLE edges(id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,"
            " type TEXT, source_line INTEGER, source_file TEXT, resolution_method TEXT,"
            " confidence REAL, metadata TEXT);"
            "CREATE TABLE properties(id INTEGER PRIMARY KEY, node_id INTEGER, kind TEXT,"
            " value TEXT, line INTEGER, confidence REAL);")
        con.execute("INSERT INTO nodes(id,label,name,file_path,start_line,end_line,language)"
                    " VALUES(1,'Function','run','pkg/mod.py',2,4,'python')")
        con.commit()
    finally:
        con.close()


class _Driver:
    """Drive the REAL seam over a scripted stream (ss_gate RealSeamDriver idiom), production
    Super-Mode posture (Profile-2 + arbiter), with the given GT_SS_* overlay. Returns the
    durable ledger rows."""

    def __init__(self, monkeypatch):
        self.mp = monkeypatch
        from groundtruth.runtime import rl_profile as rp
        core = dict(rp.resolve_profile({"GT_RL_PROFILE": "2"}))
        core["GT_RL_PROFILE"] = "2"
        core["GT_GATEWAY"] = "1"
        core["GT_GLOBAL_ARBITER"] = "1"
        self.core = {k: v for k, v in core.items() if not k.startswith("GT_SS_")}

    def run(self, events, ss_env):
        mp = self.mp
        tmp = Path(tempfile.mkdtemp(prefix="ss10_"))
        try:
            root = tmp / "repo"
            (root / "pkg").mkdir(parents=True)
            (root / "pkg" / "mod.py").write_text("def run():\n    return 1\n")
            db = str(tmp / "graph.db")
            _build_graph(db)
            led = tmp / "led.jsonl"
            for k in [e for e in os.environ if e.startswith("GT_")]:
                mp.delenv(k, raising=False)
            for k, v in self.core.items():
                mp.setenv(k, v if isinstance(v, str) else str(v))
            for k, v in (ss_env or {}).items():
                mp.setenv(k, str(v))
            mp.setenv("GT_RUNTIME_LEDGER", str(led))
            mp.setenv("GT_INDEX_BIN", str(tmp / "absent"))
            mp.setenv("GT_L6_FRESH", "0")
            mp.setenv("HF_HUB_OFFLINE", "1")
            mp.setenv("TRANSFORMERS_OFFLINE", "1")
            mp.setattr(g, "_db_path", lambda: db)
            mp.setattr(g, "_root", lambda: str(root))
            if getattr(g, "_POST_SEARCH_ON", None) is not None:
                mp.setattr(g, "_POST_SEARCH_ON", False)
            g._reset_oracle_state()
            try:
                g._RUNTIME_LEDGER = g._ProductLedger()
            except Exception:
                pass
            for cmd, out_text, rc in events:
                out = {"output": out_text, "returncode": rc}
                try:
                    g._augment_output({"command": cmd}, out)
                except Exception:
                    pass
            rows = []
            if led.is_file():
                for line in led.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        rows.append(json.loads(line))
            return rows
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def _delivered(rows):
    return [r for r in rows if "delivered" in str(r.get("outcome") or "")
            and int(r.get("chars_delivered") or 0) > 0]


# ═════════════════════════════════════════════════════════════════════════════
# R2 — coherence streak (an EARLIER passing test must NOT kill a later streak)
# ═════════════════════════════════════════════════════════════════════════════
def test_r2_coherence_counts_streak_since_last_pass():
    # babel shape: write, PASS, PASS, write, write, write — the 3-write streak AFTER the last
    # pass is a genuine blind-overwrite; the earlier passes do NOT span-kill it.
    g._ss_edit_events.extend([("a/x.py", 1, True), ("a/x.py", 5, True),
                              ("a/x.py", 7, True), ("a/x.py", 9, True)])
    g._ss_test_events.extend([(2, True), (4, True)])   # passing tests BEFORE the streak
    assert g._ss_coherence_churn("a/x.py") == 3        # writes 5,7,9 (since last pass @4)


def test_r2_passing_test_inside_streak_resets():
    # a pass WITHIN the writes resets the streak to the writes after it (over-count guard).
    g._ss_edit_events.extend([("a/x.py", 1, True), ("a/x.py", 3, True),
                              ("a/x.py", 7, True), ("a/x.py", 9, True)])
    g._ss_test_events.append((5, True))                # pass between write-2 and write-3
    assert g._ss_coherence_churn("a/x.py") is None     # only writes 7,9 remain -> <=2 -> silent


# ═════════════════════════════════════════════════════════════════════════════
# R1 — the arbiter_v2 obligations_open RELAXATION must EXCLUDE localization (cardinal
# conan-17123 consensus.scope m25 displacement: a LATE localization steer must DEFER to its
# recurring on-time review boundary, never deliver early on an off-boundary test event).
# ═════════════════════════════════════════════════════════════════════════════
def test_r1_obl_relax_excludes_localization(monkeypatch):
    monkeypatch.setenv("GT_SS_ARBITER_V2", "1")
    monkeypatch.setenv("GT_GLOBAL_ARBITER", "1")
    monkeypatch.setattr(g, "_ss_obligations_open", lambda: True)  # completeness point still live
    loc = g._ga_make_candidate("steer", "consensus.scope", dedup_key="k1", kkind="post_edit")
    cc = g._ga_make_candidate("steer", "l3.contract", dedup_key="k2", kkind="post_edit")
    assert loc is not None and cc is not None
    # localization is EXCLUDED -> obligations_open stays False -> the seam's repair_support
    # defer-and-refire delivers it ON-TIME at the recurring review boundary (reg-1 fix).
    assert loc.obligations_open is False
    # an EDIT-BOUND preventive class KEEPS the relaxation (its edit boundary may not recur).
    assert cc.obligations_open is True


def test_r1_flag_off_localization_relax_inert(monkeypatch):
    # byte-identical off: with GT_SS_ARBITER_V2 unset, NO candidate carries obligations_open.
    monkeypatch.delenv("GT_SS_ARBITER_V2", raising=False)
    monkeypatch.setenv("GT_GLOBAL_ARBITER", "1")
    monkeypatch.setattr(g, "_ss_obligations_open", lambda: True)
    cc = g._ga_make_candidate("steer", "l3.contract", dedup_key="k", kkind="post_edit")
    assert cc is not None and cc.obligations_open is False


# R1 second displacer — GT_SS_PROVENANCE's reindex-skip must NOT skip an OUTSIDE-root scratch
# write (a graph no-op), which DEFERS the cardinal P5 consensus.scope (conan-17123 iter12->20).
def test_r1_provenance_reindex_targets_root():
    # outside-root scratch: gt-index adds 0 nodes -> NOT a pollution target -> reindex must RUN
    assert g._ss_reindex_targets_root("/tmp/patch_code.py", "/testbed") is False
    assert g._ss_reindex_targets_root("../escape/x.py", "/testbed") is False
    assert g._ss_reindex_targets_root("/other/abs.py", "/testbed") is False
    # under-root generated / source: gt-index WOULD add nodes -> a real pollution target -> skip
    assert g._ss_reindex_targets_root("htmlcov/cov.js", "/testbed") is True
    assert g._ss_reindex_targets_root("conan/internal/config.py", "/testbed") is True
    assert g._ss_reindex_targets_root("/testbed/pkg/mod.py", "/testbed") is True


def test_r1_provenance_skip_only_under_root_reindex(monkeypatch, tmp_path):
    monkeypatch.setenv("GT_SS_PROVENANCE", "1")
    monkeypatch.setenv("GT_L6_FRESH", "1")                 # bypass the substrate read-only guard
    monkeypatch.setenv("GT_INDEX_BIN", str(tmp_path / "absent-gt-index"))  # absent -> NO_BINARY if it PROCEEDS
    monkeypatch.setattr(g, "_substrate_active", lambda: False)
    monkeypatch.setattr(g, "_l6_probe_emitted", True)      # silence the one-time PROBE
    # a STALE cache present: the reg-1 fix must invalidate it even for a SKIPPED bad-path write,
    # so L6 freshness advances identically to the non-provenance arm (the cardinal-P5 deferral cause).
    cache = tmp_path / "gt_index_cache.json"
    cache.write_text("stale", encoding="utf-8")
    monkeypatch.setattr(g, "_GT_INDEX_CACHE", str(cache))
    emits: list = []
    monkeypatch.setattr(g, "_l6_emit", lambda ev, **k: emits.append(ev))
    # UNDER-root generated write: graph-mutating reindex SKIPPED (silent) BUT stale cache invalidated
    g._invalidate_on_edit("htmlcov/cov.js", "/testbed")
    assert emits == []                    # graph-mutating reindex skipped (pollution guard preserved)
    assert not cache.exists()             # freshness ADVANCED (cache invalidated) -> P5 not deferred
    # OUTSIDE-root scratch write: the (no-op) reindex RUNS so freshness is not deferred (the 2nd displacer)
    emits.clear()
    g._invalidate_on_edit("/tmp/patch_code.py", "/testbed")
    assert emits != []                    # proceeded to reindex (NO_BINARY, absent bin) -> not skipped


# ═════════════════════════════════════════════════════════════════════════════
# R3 — verified-write classifier (reads/views/moves/scratch excluded; heredoc-redirect in)
# ═════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("cmd,want", [
    ("cat pkg/mod.py", False),                                   # pure read
    ("grep -rn run pkg/mod.py", False),                          # grep VIEW (haystack m59)
    ("head -50 pkg/mod.py", False),
    ("cp a.py b.py", False),                                     # move (manifest: reads/moves)
    ("mv a.py b.py", False),
    ("cat > /tmp/patch.py << 'EOF'\nopen('pkg/mod.py','w')\nEOF", False),  # scratch-create
    ("sed -i 's/a/b/' pkg/mod.py", True),                        # in-place edit
    ("cat <<EOF > pkg/mod.py\nx=1\nEOF", True),                  # heredoc-REDIRECT (the R2 hole)
    ("python - << 'PY' > pkg/mod.py\nprint(1)\nPY", True),       # heredoc-redirect via interp
    ("python3 -c \"open('pkg/mod.py','w').write('x')\"", True),  # inline interpreter write
    ("python3 /tmp/patch.py", True),                             # scratch EXECUTE = the real write
    ("python3 '/tmp/patch.py'", True),                           # quoted direct script
    ("python3 -c \"from pkg.mod import run; print(run)\"", False),  # import-only probe
])
def test_r3_write_classifier(cmd, want):
    assert g._ss_cmd_writes_source(cmd) is want


def test_r3_record_edit_skips_non_writes(monkeypatch):
    monkeypatch.setenv("GT_SS_COHERENCE_V2", "1")
    # a read/scratch classified post_edit (mtime-fabricated) must NOT become a verified write.
    g._ss_record_edit("pkg/mod.py", "cat pkg/mod.py", "")
    g._ss_record_edit("pkg/mod.py", "cat > /tmp/p.py << 'EOF'\nopen('pkg/mod.py','w')\nEOF", "")
    assert [e for e in g._ss_edit_events if e[0] == "pkg/mod.py"] == []
    # genuine writes ARE recorded
    g._ss_record_edit("pkg/mod.py", "sed -i s/a/b/ pkg/mod.py", "")
    g._ss_record_edit("pkg/mod.py", "python3 -c \"open('pkg/mod.py','w')\"", "")
    assert len([e for e in g._ss_edit_events if e[0] == "pkg/mod.py"]) == 2


def test_r3_record_edit_non_write_does_not_reset_recovery_streak(monkeypatch):
    monkeypatch.setenv("GT_SS_RECOVERY_V2", "1")
    g._ss_record_test("pytest x", "1 failed", True, False)
    g._ss_record_test("pytest x", "1 failed", True, False)
    assert g._ss_recovery_eligible() is True
    g._ss_record_edit("pkg/mod.py", "cat pkg/mod.py", "")   # a READ is not an intervening edit
    assert g._ss_recovery_eligible() is True


def test_r3_record_edit_classifier_is_v2_gated(monkeypatch):
    probe = "python3 -c \"from pkg.mod import run; print(run)\""

    # OFF is the replay fixpoint contract: the legacy post-edit sensor already established
    # that the file changed, so its event is retained byte-for-byte even for an import probe.
    monkeypatch.setenv("GT_SS_COHERENCE_V2", "0")
    monkeypatch.setenv("GT_SS_RECOVERY_V2", "0")
    g._ss_record_edit("pkg/mod.py", probe, "Import successful")
    assert g._ss_edit_events == [("pkg/mod.py", g._action_count, True)]

    # Production V2 applies the corrected semantic classifier and rejects the same probe.
    g._ss_edit_events.clear()
    monkeypatch.setenv("GT_SS_COHERENCE_V2", "1")
    g._ss_record_edit("pkg/mod.py", probe, "Import successful")
    assert g._ss_edit_events == []

    # A direct external patch script remains conservative write evidence under V2.
    g._ss_record_edit("pkg/mod.py", "python3 /tmp/patch.py", "")
    assert g._ss_edit_events == [("pkg/mod.py", g._action_count, True)]


def test_r3_compound_revert_then_rewrite_then_read_counts_two(monkeypatch, tmp_path):
    """privacyidea replay shape: a scratch plan, one compound edit+revert action, one real
    rewrite, then a pure read.  Coherence counts source-writing ACTIONS, not scratch staging,
    mtime fallback views, or individual subcommands hidden inside one shell action."""
    root = tmp_path / "repo"
    target = root / "pkg" / "mod.py"
    target.parent.mkdir(parents=True)
    original = "def run():\n    return 1\n"
    target.write_text(original, encoding="utf-8")
    monkeypatch.setattr(g, "_root", lambda: str(root))
    monkeypatch.setenv("GT_SS_COHERENCE_V2", "1")
    g._reset_oracle_state()

    def observe(cmd, output=""):
        g._augment_output({"command": cmd}, {"output": output, "returncode": 0})

    # Scratch-only heredoc: its body mentions the source but is DATA, never executed here.
    observe("cd /testbed && cat > /tmp/fix.py <<'PY'\nopen('pkg/mod.py','w')\nPY")
    # One shell action performs a temporary edit and restores it before observation.
    target.write_text("# probe\n" + original, encoding="utf-8")
    target.write_text(original, encoding="utf-8")
    observe("cd /testbed && sed -i '1i# probe' pkg/mod.py && git checkout -- pkg/mod.py")
    # Replay mtime fallback attributed the restored file's delayed mtime to this next command;
    # the semantic write verifier must reject the import-only interpreter probe.
    g._ss_record_edit(
        "pkg/mod.py",
        "python3 -c \"from pkg.mod import run; print(run)\"",
        "Import successful",
    )
    # One lasting rewrite action.
    target.write_text(original.replace("1", "2"), encoding="utf-8")
    observe("cd /testbed && python3 - <<'PY'\nopen('pkg/mod.py','w').write('x')\nPY")
    # Pure read must not turn the prior mtime change into a third verified write.
    observe("cd /testbed && cat pkg/mod.py", target.read_text(encoding="utf-8"))

    writes = [e for e in g._ss_edit_events if e[0] == "pkg/mod.py" and e[2]]
    assert len(writes) == 2
    assert g._ss_coherence_churn("pkg/mod.py") is None


# ═════════════════════════════════════════════════════════════════════════════
# CHARS0 — no row may carry outcome="delivered" with chars<=0 (any kind/plane)
# ═════════════════════════════════════════════════════════════════════════════
def test_chars0_ledger_record_downgrades_zero_byte_delivered(monkeypatch):
    tmp = Path(tempfile.mkdtemp(prefix="ss10c_"))
    try:
        led = tmp / "led.jsonl"
        monkeypatch.setenv("GT_RUNTIME_LEDGER", str(led))
        try:
            g._RUNTIME_LEDGER = g._ProductLedger()
        except Exception:
            pass
        # the hydra ga.trace_frame / repair_support shape: DELIVERED + chars=0.
        g._runtime_ledger_record(kind="ga.trace_frame",
                                 outcome=g._ProductSignalOutcome.DELIVERED,
                                 reason="repair_support", chars=0)
        # a real delivery is untouched.
        g._runtime_ledger_record(kind="l3.contract",
                                 outcome=g._ProductSignalOutcome.DELIVERED,
                                 chars=42, content="body")
        rows = [json.loads(x) for x in led.read_text(encoding="utf-8").splitlines() if x.strip()]
        by_kind = {r["layer"]: r for r in rows}
        assert by_kind["ga.trace_frame"]["outcome"] != "delivered"    # downgraded (honest)
        assert by_kind["ga.trace_frame"]["reason"] == "repair_support"  # semantics preserved
        assert by_kind["l3.contract"]["outcome"] == "delivered"       # real delivery intact
        assert by_kind["l3.contract"]["chars_delivered"] == 42
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_chars0_in_process_ledger_object_also_downgraded(monkeypatch):
    """The IN-PROCESS _RUNTIME_LEDGER object (feeds the in-process summary) must ALSO record the
    honest outcome — the _runtime_ledger_record guard downgrades BEFORE the object record, so a
    chars=0 delivered never reaches the in-process summary either (not only the durable file)."""
    tmp = Path(tempfile.mkdtemp(prefix="ss10ci_"))
    try:
        monkeypatch.setenv("GT_RUNTIME_LEDGER", str(tmp / "led.jsonl"))
        led = g._ProductLedger()
        g._RUNTIME_LEDGER = led
        g._runtime_ledger_record(kind="ga.trace_frame",
                                 outcome=g._ProductSignalOutcome.DELIVERED,
                                 reason="repair_support", chars=0)
        obj_rows = [json.loads(x) for x in (led.to_jsonl() or "").splitlines() if x.strip()]
        assert obj_rows, "in-process object recorded nothing"
        assert all(r.get("outcome") != "delivered" for r in obj_rows), \
            f"in-process object kept a chars=0 delivered: {obj_rows}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_chars0_no_zero_byte_delivered_in_driven_stream(monkeypatch):
    """End-to-end: a full SS-on stream that fires coherence/recovery/ack must leave ZERO
    ledger rows with outcome='delivered' and chars<=0."""
    drv = _Driver(monkeypatch)
    ss = {"GT_SS_COHERENCE_V2": "1", "GT_SS_RECOVERY_V2": "1", "GT_SS_ACK_METRICS": "1",
          "GT_SS_ARBITER_V2": "1"}
    events = [
        ("sed -i s/1/2/ pkg/mod.py", "", 0),
        ("sed -i s/2/3/ pkg/mod.py", "", 0),
        ("sed -i s/3/4/ pkg/mod.py", "", 0),
        ("pytest -q", "E assert\n1 failed", 1),
        ("pytest -q", "E assert\n1 failed", 1),
        ("grep -rn run pkg/mod.py", "pkg/mod.py:2: def run():", 0),
    ]
    rows = drv.run(events, ss)
    liars = [r for r in rows if str(r.get("outcome") or "") == "delivered"
             and int(r.get("chars_delivered") or 0) <= 0]
    assert liars == [], f"delivered rows with chars<=0: {[(r['layer'], r.get('reason')) for r in liars]}"


# ═════════════════════════════════════════════════════════════════════════════
# R4 — the lane plane (Lane-A + steer pool-add) must run the SS provenance/late screen
# ═════════════════════════════════════════════════════════════════════════════
def test_r4_lane_a_pool_add_screens_provenance(monkeypatch):
    monkeypatch.setenv("GT_SS_PROVENANCE", "1")
    monkeypatch.setenv("GT_GLOBAL_ARBITER", "1")
    monkeypatch.setenv("GT_RL_PROFILE", "2")
    tmp = Path(tempfile.mkdtemp(prefix="ss10r4_"))
    try:
        led = tmp / "led.jsonl"
        monkeypatch.setenv("GT_RUNTIME_LEDGER", str(led))
        monkeypatch.setattr(g, "_root", lambda: str(tmp))
        try:
            g._RUNTIME_LEDGER = g._ProductLedger()
        except Exception:
            pass
        pool = []
        # a Lane-A fact whose ONLY provenance is a scratch /tmp path -> must be provenance-dropped
        # BEFORE it competes, recording reason=ss_provenance, and NOT pooled.
        bad = [("l3.contract", "<gt-contract>\ncaller at /tmp/patch_fix.py:5\n</gt-contract>")]
        g._global_pool_add_lane_a(pool, {"output": ""}, "sed -i s/a/b/ x.py", bad,
                                   krel="x.py", event=None, kkind="post_edit")
        rows = [json.loads(x) for x in led.read_text(encoding="utf-8").splitlines() if x.strip()] \
            if led.is_file() else []
        assert any("ss_provenance" in str(r.get("reason") or "") for r in rows), \
            f"no ss_provenance row on the lane plane; rows={[(r.get('layer'), r.get('reason')) for r in rows]}"
        assert pool == []   # suppressed pre-competition (never pooled)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_r4_steer_pool_add_screens_provenance(monkeypatch):
    monkeypatch.setenv("GT_SS_PROVENANCE", "1")
    monkeypatch.setenv("GT_GLOBAL_ARBITER", "1")
    monkeypatch.setenv("GT_RL_PROFILE", "2")
    tmp = Path(tempfile.mkdtemp(prefix="ss10r4s_"))
    try:
        led = tmp / "led.jsonl"
        monkeypatch.setenv("GT_RUNTIME_LEDGER", str(led))
        monkeypatch.setattr(g, "_root", lambda: str(tmp))
        monkeypatch.setattr(g, "_last_gate_winner_kind", "edit.syntax")
        monkeypatch.setattr(g, "_last_gate_winner_hash", "")
        try:
            g._RUNTIME_LEDGER = g._ProductLedger()
        except Exception:
            pass
        pool = []
        # an edit.syntax steer whose diagnostic content cites ONLY a scratch /tmp path (dvc m31
        # shape) — provenance drops the sole content line, leaving no payload -> whole-suppress.
        win = ('<gt-nudge reason="edit_syntax">\n'
               '  /tmp/patch_remote.py:3: SyntaxError: invalid syntax\n'
               '</gt-nudge>')
        g._global_pool_add_steer(pool, {"output": ""}, "python3 /tmp/patch_remote.py", win,
                                  kkind="post_edit", kf="x.py", krel="x.py", event=None,
                                  steer_base="")
        rows = [json.loads(x) for x in led.read_text(encoding="utf-8").splitlines() if x.strip()] \
            if led.is_file() else []
        assert any("ss_provenance" in str(r.get("reason") or "") for r in rows), \
            f"no ss_provenance row on the steer plane; rows={[(r.get('layer'), r.get('reason')) for r in rows]}"
        assert pool == []
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ═════════════════════════════════════════════════════════════════════════════
# byte-identity: flag-OFF must not record any ss_* reason (all fixes flag-gated)
# ═════════════════════════════════════════════════════════════════════════════
def test_flag_off_no_ss_reasons(monkeypatch):
    drv = _Driver(monkeypatch)
    events = [
        ("sed -i s/1/2/ pkg/mod.py", "", 0),
        ("sed -i s/2/3/ pkg/mod.py", "", 0),
        ("sed -i s/3/4/ pkg/mod.py", "", 0),
        ("grep -rn run pkg/mod.py", "pkg/mod.py:2: def run():", 0),
    ]
    rows = drv.run(events, {k: "0" for k in (
        "GT_SS_NOVELTY", "GT_SS_DEDUP2", "GT_SS_COHERENCE_V2", "GT_SS_RECOVERY_V2",
        "GT_SS_PROVENANCE", "GT_SS_LATE_DROP", "GT_SS_ACK_METRICS", "GT_SS_ARBITER_V2")})
    ss_rows = [r for r in rows if str(r.get("reason") or "").startswith("ss_")]
    assert ss_rows == [], f"flag-off emitted ss_* rows: {[(r['layer'], r.get('reason')) for r in ss_rows]}"
