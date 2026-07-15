"""SS-10 — the FOUR replay-oracle regressions + the chars=0-delivered honesty invariant.

Every test here is a DETERMINISTIC synthetic reproduction derived from a byte-verified
replay-oracle case (tests/fixtures/ss_replay/cases.json) — NOT a task-keyed hack. Each
drives the real seam (or the pure helper) with the recorded EVENT SHAPE and asserts the
fixed behaviour; each fails RED on the pre-fix seam.

  R1 (conan-17123) raw consensus.scope at iter12 was scratch-triggered and invalid. Corrected
     write truth rejects that false edit and delivers new scope bytes only after a real source
     write at the recurring decision boundary; historical acknowledgment does not transfer.
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
from groundtruth.runtime import hypothesis_ledger as hl  # noqa: E402
from groundtruth.runtime.native_render import render_recovery_native  # noqa: E402


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
# R1 — the arbiter_v2 obligations_open RELAXATION must EXCLUDE localization. Conan's
# scratch-triggered historical scope is invalid; corrected scope must wait for a real source
# write and the recurring review boundary, never fire on an off-boundary test event.
# ═════════════════════════════════════════════════════════════════════════════
def test_r2_v2_churn_requires_changed_bytes_after_pass(monkeypatch):
    monkeypatch.setenv("GT_SS_COHERENCE_V2", "1")
    rel = "babel/dates.py"
    cmd = "python -c \"open('babel/dates.py', 'w').write('content')\""

    monkeypatch.setattr(g, "_action_count", 1)
    g._ss_record_edit(rel, cmd, "", returncode=0, bytes_changed=True)
    monkeypatch.setattr(g, "_action_count", 2)
    g._ss_record_test("pytest -q", "1 passed", failed=False, passed=True)

    monkeypatch.setattr(g, "_action_count", 3)
    g._ss_record_edit(rel, cmd, "", returncode=0, bytes_changed=False)
    monkeypatch.setattr(g, "_action_count", 4)
    g._ss_record_edit(rel, cmd, "", returncode=0, bytes_changed=True)
    monkeypatch.setattr(g, "_action_count", 5)
    g._ss_record_edit(rel, cmd, "", returncode=0, bytes_changed=True)

    post_pass = [ok for event_rel, step, ok in g._ss_edit_events
                 if event_rel == rel and step > 2]
    assert post_pass == [False, True, True]
    assert sum(post_pass) == 2
    assert g._ss_coherence_churn(rel) is None

    monkeypatch.setattr(g, "_action_count", 6)
    g._ss_record_edit(rel, cmd, "", returncode=0, bytes_changed=True)
    assert g._ss_coherence_churn(rel) == 3


def test_r1_obl_relax_excludes_localization(monkeypatch):
    monkeypatch.setenv("GT_SS_ARBITER_V2", "1")
    monkeypatch.setenv("GT_GLOBAL_ARBITER", "1")
    monkeypatch.setattr(g, "_ss_obligations_open", lambda: True)  # completeness point still live
    loc = g._ga_make_candidate("steer", "consensus.scope", dedup_key="k1", kkind="post_edit")
    cc = g._ga_make_candidate("steer", "l3.contract", dedup_key="k2", kkind="post_edit")
    assert loc is not None and cc is not None
    # localization is EXCLUDED -> obligations_open stays False -> the seam's repair_support
    # defer-and-refire delivers corrected bytes at the recurring review boundary.
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


# R1 rich-event timing: test observations defer localization until the recurring review boundary.
def _rich_event_steer_candidate(monkeypatch, event, *, kind="consensus.scope", v2=True):
    monkeypatch.setenv("GT_GLOBAL_ARBITER", "1")
    if v2:
        monkeypatch.setenv("GT_SS_ARBITER_V2", "1")
    else:
        monkeypatch.delenv("GT_SS_ARBITER_V2", raising=False)
    monkeypatch.setattr(g, "_last_gate_winner_kind", kind)
    monkeypatch.setattr(g, "_last_gate_winner_hash", "rich-event-proof")
    delivered = []
    monkeypatch.setattr(
        g, "_deliver_gate_winner",
        lambda *_args, **_kwargs: delivered.append(kind),
    )
    pool = []
    g._global_pool_add_steer(
        pool, {"output": "native"}, "pytest -q", "scope payload",
        kkind=None, kf="", krel="", event=event, steer_base="native",
    )
    assert len(pool) == 1
    return pool, pool[0][0], delivered


@pytest.mark.parametrize(("event", "ordinal"), [
    (g.Event.TASK_START, 0),
    (g.Event.REVIEW_TRANSITION, 1),
    (g.Event.POST_VIEW, 2),
    (g.Event.POST_EDIT, 3),
    (g.Event.TEST_RESULT, 4),
    (g.Event.PRE_SUBMIT, 5),
])
def test_r1_v2_steer_uses_rich_event_ordinal(monkeypatch, event, ordinal):
    _pool, candidate, _delivered = _rich_event_steer_candidate(monkeypatch, event)
    assert candidate.boundary_ordinal == 1
    assert candidate.current_ordinal == ordinal


def test_r1_test_result_defers_and_rearms_scope(monkeypatch):
    pool, candidate, delivered = _rich_event_steer_candidate(
        monkeypatch, g.Event.TEST_RESULT)
    assert (candidate.boundary_ordinal, candidate.current_ordinal) == (1, 4)
    monkeypatch.setattr(g, "_oracle_review_fired", True)

    g._global_pool_flush(pool, kkind=None, kf="", krel="")

    assert delivered == []
    assert g._oracle_review_fired is False


def test_r1_review_transition_delivers_scope_once(monkeypatch):
    pool, candidate, delivered = _rich_event_steer_candidate(
        monkeypatch, g.Event.REVIEW_TRANSITION)
    assert (candidate.boundary_ordinal, candidate.current_ordinal) == (1, 1)
    monkeypatch.setattr(g, "_oracle_review_fired", True)

    g._global_pool_flush(pool, kkind=None, kf="", krel="")

    assert delivered == ["consensus.scope"]
    assert g._oracle_review_fired is True


def test_r1_v2_off_keeps_legacy_kkind_ordinal(monkeypatch):
    _pool, candidate, _delivered = _rich_event_steer_candidate(
        monkeypatch, g.Event.TEST_RESULT, v2=False)
    assert candidate.current_ordinal == 0


def test_r1_reactive_test_result_still_delivers(monkeypatch):
    pool, candidate, delivered = _rich_event_steer_candidate(
        monkeypatch, g.Event.TEST_RESULT, kind="recovery")
    assert (candidate.boundary_ordinal, candidate.current_ordinal) == (5, 4)

    g._global_pool_flush(pool, kkind=None, kf="", krel="")

    assert delivered == ["recovery"]


# R1 second path — GT_SS_PROVENANCE's reindex-skip must NOT skip an OUTSIDE-root scratch
# write (a graph no-op), so the corrected later consensus.scope boundary remains reachable.
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
    # so L6 freshness advances identically to the non-provenance arm.
    cache = tmp_path / "gt_index_cache.json"
    cache.write_text("stale", encoding="utf-8")
    monkeypatch.setattr(g, "_GT_INDEX_CACHE", str(cache))
    emits: list = []
    monkeypatch.setattr(g, "_l6_emit", lambda ev, **k: emits.append(ev))
    # UNDER-root generated write: graph-mutating reindex SKIPPED (silent) BUT stale cache invalidated
    g._invalidate_on_edit("htmlcov/cov.js", "/testbed")
    assert emits == []                    # graph-mutating reindex skipped (pollution guard preserved)
    assert not cache.exists()             # freshness ADVANCED (cache invalidated)
    # OUTSIDE-root scratch write: the (no-op) reindex RUNS so freshness is not deferred
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


@pytest.mark.parametrize("target_kind", ["scratch", "outside"])
def test_r3_byte_changed_non_repo_target_never_becomes_an_edit(
        monkeypatch, tmp_path, target_kind):
    """Exact bytes cannot promote scratch or outside-root files into repo edit truth."""
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "mod.py").write_text("value = 1\n", encoding="utf-8")
    if target_kind == "scratch":
        target = root / "tmp" / "stage.py"
        command_target = "tmp/stage.py"
    else:
        target = tmp_path / "outside.py"
        command_target = str(target)
        # Prove root confinement independently of the ambient pytest /tmp path marker.
        monkeypatch.setattr(g, "_ss_is_scratch_path", lambda _path: False)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("value = 1\n", encoding="utf-8")

    monkeypatch.setattr(g, "_root", lambda: str(root))
    monkeypatch.setenv("GT_SS_RECOVERY_V2", "1")
    g._reset_oracle_state()
    g._ss_record_test("pytest pkg/test_mod.py", "1 failed", True, False)
    g._ss_record_test("pytest pkg/test_mod.py", "1 failed", True, False)
    assert g._ss_recovery_eligible() is True

    cmd = f"cat > {command_target} <<'EOF'\nvalue = 2\nEOF"
    assert g._classify(cmd) == ("post_edit", command_target)
    action = {"command": cmd}
    g._ss_capture_write_preimage(action)
    target.write_text("value = 2\n", encoding="utf-8")
    g._augment_output(action, {"output": "", "returncode": 0})

    assert g._ss_edit_events == []
    assert g._oracle_edited_rels == set()
    assert g._ss_recovery_eligible() is True


def test_r3_record_edit_rejects_non_repo_rel_even_with_claimed_byte_truth(monkeypatch):
    """The chronology sink independently rejects poisoned upstream path claims."""
    monkeypatch.setenv("GT_SS_COHERENCE_V2", "1")
    g._ss_record_edit(
        "tmp/stage.py", "cat > tmp/stage.py", "",
        returncode=0, bytes_changed=True)
    g._ss_record_edit(
        "../outside.py", "cat > ../outside.py", "",
        returncode=0, bytes_changed=True)
    assert g._ss_edit_events == []


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

    # A direct external patch script is write-shaped, but V2 cannot call it a
    # successful write without byte proof from the observation seam.
    g._ss_record_edit("pkg/mod.py", "python3 /tmp/patch.py", "")
    assert g._ss_edit_events == [("pkg/mod.py", g._action_count, False)]


def test_r3_compound_revert_then_rewrite_counts_only_landed_byte_changes(monkeypatch, tmp_path):
    """privacyidea replay shape: a scratch plan, one compound edit+revert action, one real
    rewrite, then a pure read. Coherence counts only landed source-byte transitions, excluding
    edit+restore commands, scratch staging, parser-shaped reads, and later views."""
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
    assert len(writes) == 1
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
# R4b — behavioral execution proof must reach the obligation late-drop seam.
_BEHAVIORAL_OBLIGATION = (
    '<gt-nudge reason="test_evidence_gap">\n'
    '[edited, untested] "Support inverse_match behavior"\n'
    '</gt-nudge>'
)

_CHECKED_PROBE = """python3 - <<'PY'
from pkg.policy import inverse_match
print(f'allow: {inverse_match("allow")}')  # True
print(f'deny: {inverse_match("deny")}')  # False
PY"""

_CHECKED_OUTPUT = "allow: True\ndeny: False"


def _record_behavioral_proof(command=_CHECKED_PROBE, output=_CHECKED_OUTPUT,
                             returncode=0):
    """Record replay-faithful product truth from command, return code, and output.

    Assistant acknowledgment is a separate SS receipt grade.  The replay child does not pass
    assistant reasoning to the product seam, so suppression cannot depend on that prose.
    """
    g._ss_record_behavioral_proof(
        command=command,
        output=output,
        returncode=returncode,
    )


def _attempt_obligation_steer(monkeypatch, tmp_path, payload=_BEHAVIORAL_OBLIGATION):
    ledger = tmp_path / "behavioral-proof-ledger.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("GT_SS_LATE_DROP", "1")
    monkeypatch.setenv("GT_GLOBAL_ARBITER", "1")
    monkeypatch.setenv("GT_RUNTIME_LEDGER", str(ledger))
    monkeypatch.setattr(g, "_last_gate_winner_kind", "spec.obligation")
    monkeypatch.setattr(g, "_last_gate_winner_hash", "")
    g._RUNTIME_LEDGER = g._ProductLedger()
    pool = []
    observation = {"output": "native command output"}
    g._global_pool_add_steer(
        pool, observation, "pytest -q tests/test_unrelated.py", payload,
        kkind="post_test", kf="pkg/policy.py", krel="pkg/policy.py",
        event=None, steer_base="native command output",
    )
    rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()
            if line.strip()] if ledger.is_file() else []
    return pool, observation, rows


def test_r4b_checked_probe_then_unrelated_green_suppresses_late_obligation(
        monkeypatch, tmp_path):
    """Checked dynamic proof, then an unrelated green, precede the review boundary."""
    _record_behavioral_proof()
    g._ss_record_test(
        "pytest -q tests/test_unrelated.py", "test_unrelated PASSED\n1 passed",
        failed=False, passed=True,
    )

    pool, observation, rows = _attempt_obligation_steer(monkeypatch, tmp_path)

    suppressed = [row for row in rows if row.get("layer") == "spec.obligation"]
    assert len(suppressed) == 1
    assert suppressed[0]["outcome"] == "suppressed_hidden_only"
    assert suppressed[0]["reason"] == "ss_late"
    assert suppressed[0]["chars_delivered"] == 0
    assert pool == []
    assert observation["output"] == "native command output"
    assert _BEHAVIORAL_OBLIGATION not in observation["output"]


def test_r4b_live_augment_records_native_probe_event(monkeypatch, tmp_path):
    """The live observation seam, not only the direct helper, records proof input."""
    monkeypatch.setattr(g, "_root", lambda: str(tmp_path))
    monkeypatch.setenv("GT_GLOBAL_ARBITER", "0")

    out = {"output": _CHECKED_OUTPUT, "returncode": 0}
    g._augment_output({"command": _CHECKED_PROBE}, out)

    assert g._ss_behavioral_probe_events
    command, output, returncode, turn = g._ss_behavioral_probe_events[-1]
    assert command == _CHECKED_PROBE
    assert output == _CHECKED_OUTPUT
    assert returncode == 0
    assert turn == g._action_count


def test_r4b_unrelated_green_without_behavioral_proof_does_not_suppress(
        monkeypatch, tmp_path):
    g._ss_record_test(
        "pytest -q tests/test_unrelated.py", "test_unrelated PASSED\n1 passed",
        failed=False, passed=True,
    )

    pool, observation, rows = _attempt_obligation_steer(monkeypatch, tmp_path)

    assert not any(row.get("reason") == "ss_late" for row in rows)
    assert len(pool) == 1

    # A source edit after the proof makes the earlier observation stale.
    g._action_count += 1
    g._ss_record_edit(
        "pkg/policy.py", "sed -i 's/inverse_match/inverse_match_v2/' pkg/policy.py", "",
        returncode=0, bytes_changed=True,
    )
    pool, _observation, rows = _attempt_obligation_steer(
        monkeypatch, tmp_path / "stale", _BEHAVIORAL_OBLIGATION)
    assert not any(row.get("reason") == "ss_late" for row in rows)
    assert len(pool) == 1
    assert observation["output"] == "native command output"


@pytest.mark.parametrize(("command", "output", "returncode"), [
    (_CHECKED_PROBE, _CHECKED_OUTPUT, 1),
    ("python3 -c \"print('allow: True'); print('deny: False')\"",
     _CHECKED_OUTPUT, 0),
    ("""python3 - <<'PY'
from pkg.policy import inverse_match
allow = inverse_match("allow")
allow = True
deny = inverse_match("deny")
deny = False
print(f'allow: {allow}')
print(f'deny: {deny}')
PY""", _CHECKED_OUTPUT, 0),
    ("""python3 - <<'PY'
from pkg.other import unrelated_check
print(f'allow: {unrelated_check("allow")}')  # True
print(f'deny: {unrelated_check("deny")}')  # False
PY""", _CHECKED_OUTPUT, 0),
])
def test_r4b_unproven_or_adversarial_probe_cannot_suppress(
        monkeypatch, tmp_path, command, output, returncode):
    _record_behavioral_proof(command=command, output=output, returncode=returncode)

    pool, _observation, rows = _attempt_obligation_steer(monkeypatch, tmp_path)

    assert not any(row.get("reason") == "ss_late" for row in rows)
    assert len(pool) == 1


def test_r4b_renderer_metadata_is_not_an_obligation_subject():
    subjects = g._ss_code_idents(_BEHAVIORAL_OBLIGATION)
    assert "inverse_match" in subjects
    assert "test_evidence_gap" not in subjects
    assert "gt-nudge" not in subjects


def test_r4b_behavioral_proof_is_obligation_and_event_scoped(monkeypatch, tmp_path):
    _record_behavioral_proof()

    unrelated = _BEHAVIORAL_OBLIGATION.replace("inverse_match", "normalize_path")
    pool, _observation, rows = _attempt_obligation_steer(
        monkeypatch, tmp_path / "unrelated", unrelated)
    assert not any(row.get("reason") == "ss_late" for row in rows)
    assert len(pool) == 1


_BEETS_RESURFACE = (
    "before you submit, the issue requires the following -- re-read it:\n"
    "  - Tekstowo backend does not return lyrics any more\n"
)


def _record_successful_edit(turn: int, command: str) -> None:
    g._action_count = turn
    g._ss_record_edit(
        "beetsplug/lyrics.py", command, "", returncode=0, bytes_changed=True)


def _record_beets_green(turn: int) -> None:
    g._action_count = turn
    g._ss_record_behavioral_proof(
        command="python3 -m pytest test/plugins/test_lyrics.py -k Tekstowo -v",
        output=(
            "test/plugins/test_lyrics.py::TekstowoExtractLyricsTest::test_good_lyrics PASSED\n"
            "3 passed, 2 skipped, 27 deselected in 0.42s\n"
        ),
        returncode=0,
    )


def test_r4c_beets_partial_green_does_not_retire_complete_obligation_group():
    _record_successful_edit(
        10, "python3 - <<'PY'\nclass Tekstowo:\n    def fetch(self): pass\nPY")
    _record_beets_green(20)
    _record_successful_edit(
        30, "sed -i 's/from beets.autotag.hooks import string_dist//' beetsplug/lyrics.py")

    assert g._ss_late_drop_suppresses(
        "obligation.resurface", _BEETS_RESURFACE) is False


def test_r4c_beets_relevant_edit_invalidates_prior_green():
    _record_successful_edit(
        10, "python3 - <<'PY'\nclass Tekstowo:\n    def fetch(self): pass\nPY")
    _record_beets_green(20)
    _record_successful_edit(
        30, "sed -i 's/def fetch/def fetch_v2/' beetsplug/lyrics.py")

    assert g._ss_late_drop_suppresses(
        "obligation.resurface", _BEETS_RESURFACE) is False


def test_r4c_beets_unrelated_or_pre_edit_green_never_suppresses():
    g._action_count = 10
    g._ss_record_behavioral_proof(
        command="python3 -m pytest tests/test_unrelated.py -q",
        output="tests/test_unrelated.py::test_other PASSED\n1 passed in 0.1s\n",
        returncode=0,
    )
    assert g._ss_late_drop_suppresses(
        "obligation.resurface", _BEETS_RESURFACE) is False

    _record_beets_green(20)
    _record_successful_edit(
        30, "sed -i 's/Tekstowo/TekstowoV2/' beetsplug/lyrics.py")
    assert g._ss_late_drop_suppresses(
        "obligation.resurface", _BEETS_RESURFACE) is False


def test_r4d_arviz_partial_exception_probe_does_not_suppress_whole_payload():
    command = """python3 - <<'PY'
import arviz as az
import numpy as np
x = np.array(['A', 'B', 'C'])
y = np.ones((1, 3, 3))
try:
    az.plot_hdi(x=x, y=y, smooth=True)
    print('ERROR: No exception raised!')
except TypeError as exc:
    print(f'OK: TypeError raised: {exc}')
except Exception as exc:
    print(f'Other exception: {type(exc).__name__}: {exc}')
x2 = np.arange(3)
try:
    az.plot_hdi(x=x2, y=y, smooth=True)
    print('OK: No exception')
except Exception as exc:
    print(f'Exception: {type(exc).__name__}: {exc}')
PY"""
    payload = (
        '[edited, untested] "Given the documentation, I\'d expect the behavior to '
        'mirror `bambi.interpret`"\n'
        '[edited, untested] "I think a TypeError informing the user of the lack of '
        'support for categorical values would be helpful."\n'
    )
    g._action_count = 20
    g._ss_record_behavioral_proof(
        command=command,
        output=(
            "OK: TypeError raised: categorical x values are not supported\n"
            "OK: No exception\n"
        ),
        returncode=0,
    )

    assert g._ss_late_drop_suppresses("spec.obligation", payload) is False


def test_r4d_structured_miss_does_not_fall_back_to_legacy_pass_tokens(monkeypatch):
    payload = '[edited, untested] "Preserve normalize_path behavior for Windows paths"'
    monkeypatch.setattr(g, "_ss_pass_tokens", {
        "preserve", "normalize_path", "behavior", "windows", "paths"
    })

    assert g._ss_late_drop_suppresses("spec.obligation", payload) is False


def test_r4d_structured_classifier_exception_fails_closed_without_legacy_fallback(
        monkeypatch):
    payload = '[edited, untested] "Preserve normalize_path behavior for Windows paths"'
    monkeypatch.setattr(g, "_ss_pass_tokens", {
        "preserve", "normalize_path", "behavior", "windows", "paths"
    })
    import groundtruth.runtime.obligations as obligations
    monkeypatch.setattr(
        obligations, "classify_checked_behavioral_proof",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("structured failure")),
    )
    g._ss_behavioral_probe_events.append(("probe", "output", 0, 1))

    assert g._ss_late_drop_suppresses("spec.obligation", payload) is False


# R5 — recovery repeat identity is the observed failing test, not the spelling of
# the runner command. A file-level first run followed by a nodeid retry is the same
# unresolved failure when no edit intervenes (Dynaconf's faithful iter144/145 shape).
def test_r5_recovery_v2_releases_on_same_failure_across_runner_scope():
    first_cmd = "python -m pytest tests/test_cli.py -x -v"
    first_out = "tests/test_cli.py::test_inspect_no_args FAILED [ 89%]\n1 failed"
    retry_cmd = (
        "python -m pytest tests/test_cli.py::test_inspect_no_args -x -v"
    )
    retry_out = (
        "FAILED tests/test_cli.py::test_inspect_no_args - assert False\n1 failed"
    )

    g._ss_record_test(first_cmd, first_out, failed=True, passed=False)
    assert g._ss_recovery_eligible() is False
    g._ss_record_test(retry_cmd, retry_out, failed=True, passed=False)
    assert g._ss_recovery_eligible() is True


def test_r5_recovery_v2_builds_generic_candidate_without_legacy_selection(monkeypatch):
    """The canonical test identity, not the legacy prose fingerprint, releases Dynaconf."""
    monkeypatch.setenv("GT_HYPOTHESIS", "1")
    monkeypatch.setenv("GT_SS_RECOVERY_V2", "1")
    monkeypatch.setattr(g, "_gt_hypothesis_recovery", None)
    first_cmd = "python -m pytest tests/test_cli.py -x -v"
    first_out = "tests/test_cli.py::test_inspect_no_args FAILED [ 89%]\n1 failed"
    retry_cmd = "python -m pytest tests/test_cli.py::test_inspect_no_args -x -v"
    retry_out = "FAILED tests/test_cli.py::test_inspect_no_args - assert False\n1 failed"

    g._ss_record_test(first_cmd, first_out, failed=True, passed=False)
    assert g._recovery_candidate() is None

    g._ss_record_test(retry_cmd, retry_out, failed=True, passed=False)
    assert g._gt_hypothesis_recovery is None  # classifier selected no legacy fingerprint path
    expected = render_recovery_native(
        hl.D_REQUEST_NEW_HYPOTHESIS,
        g._hypothesis_imperative_map()[hl.D_REQUEST_NEW_HYPOTHESIS],
    )
    assert g._recovery_candidate() == (
        float(g._SEV_RECOVERY),
        "recovery",
        expected,
        True,
    )

    # The streak remains unresolved, but fallback selection is tied to the qualifying
    # failure event. A later unrelated turn or pass must stay quiet.
    monkeypatch.setattr(g, "_action_count", g._action_count + 1)
    assert g._ss_recovery_eligible() is True
    assert g._recovery_candidate() is None
    g._ss_record_test("pytest tests/test_other.py -q", "1 passed", False, True)
    assert g._ss_recovery_eligible() is True
    assert g._recovery_candidate() is None

    monkeypatch.setattr(g, "_action_count", g._action_count + 1)
    g._ss_record_test(
        "pytest tests/test_other.py::test_other -x",
        "FAILED tests/test_other.py::test_other - assert False\n1 failed",
        True,
        False,
    )
    assert g._ss_recovery_eligible() is True  # the earlier identity remains unresolved
    assert g._recovery_candidate() is None    # this exact identity has failed only once


def _r5_falsified_selection() -> tuple[str, str]:
    return (
        hl.D_HYPOTHESIS_FALSIFIED,
        g._hypothesis_imperative_map()[hl.D_HYPOTHESIS_FALSIFIED],
    )


def test_r5_recovery_v2_releases_typed_falsification_on_current_failure(monkeypatch):
    """A repeated failure after an edit is typed falsification, not a fresh one-off."""
    monkeypatch.setenv("GT_HYPOTHESIS", "1")
    monkeypatch.setenv("GT_SS_RECOVERY_V2", "1")
    cmd = "pytest tests/test_cli.py::test_inspect_no_args -x"
    out = "FAILED tests/test_cli.py::test_inspect_no_args - assert False"

    monkeypatch.setattr(g, "_action_count", 1)
    g._ss_record_test(cmd, out, failed=True, passed=False)
    monkeypatch.setattr(g, "_action_count", 2)
    g._ss_record_edit(
        "pkg/policy.py", "sed -i 's/old/new/' pkg/policy.py", "",
        returncode=0, bytes_changed=True,
    )
    assert g._ss_test_fail_counts == {}

    monkeypatch.setattr(g, "_action_count", 3)
    g._ss_record_test(cmd, out, failed=True, passed=False)
    selection = _r5_falsified_selection()
    monkeypatch.setattr(g, "_gt_hypothesis_recovery", selection)
    expected = render_recovery_native(*selection)

    assert g._ss_recovery_eligible() is False  # edit reset leaves one canonical failure
    assert g._recovery_candidate() == (
        float(g._SEV_RECOVERY), "recovery", expected, True)


def test_r5_recovery_v2_typed_falsification_is_current_event_bound(monkeypatch):
    """A valid selection cannot leak onto a later read/pass like PrivacyIdea's misfires."""
    monkeypatch.setenv("GT_HYPOTHESIS", "1")
    monkeypatch.setenv("GT_SS_RECOVERY_V2", "1")
    monkeypatch.setattr(g, "_action_count", 7)
    g._ss_record_test(
        "pytest tests/test_cli.py::test_inspect_no_args -x",
        "FAILED tests/test_cli.py::test_inspect_no_args - assert False",
        failed=True,
        passed=False,
    )
    monkeypatch.setattr(g, "_gt_hypothesis_recovery", _r5_falsified_selection())

    monkeypatch.setattr(g, "_action_count", 8)  # a later non-test observation

    assert g._recovery_candidate() is None


def test_r5_recovery_v2_one_off_non_falsified_selection_stays_quiet(monkeypatch):
    """Only typed edit-falsification may complement the canonical repeat counter."""
    monkeypatch.setenv("GT_HYPOTHESIS", "1")
    monkeypatch.setenv("GT_SS_RECOVERY_V2", "1")
    monkeypatch.setattr(g, "_action_count", 11)
    g._ss_record_test(
        "pytest tests/test_cli.py::test_inspect_no_args -x",
        "FAILED tests/test_cli.py::test_inspect_no_args - assert False",
        failed=True,
        passed=False,
    )
    selection = (
        hl.D_REQUEST_NEW_HYPOTHESIS,
        g._hypothesis_imperative_map()[hl.D_REQUEST_NEW_HYPOTHESIS],
    )
    monkeypatch.setattr(g, "_gt_hypothesis_recovery", selection)

    assert g._recovery_candidate() is None


def test_r5_recovery_v2_keeps_distinct_failures_separate():
    g._ss_record_test(
        "pytest tests/test_cli.py -x", "tests/test_cli.py::test_one FAILED [50%]",
        failed=True, passed=False,
    )
    g._ss_record_test(
        "pytest tests/test_cli.py::test_two -x", "FAILED tests/test_cli.py::test_two",
        failed=True, passed=False,
    )

    assert g._ss_recovery_eligible() is False


def test_r5_recovery_v2_matching_pass_clears_canonical_streak_only():
    first_cmd = "python -m pytest tests/test_cli.py -x -v"
    retry_cmd = "python -m pytest tests/test_cli.py::test_inspect_no_args -x -v"
    failure = "FAILED tests/test_cli.py::test_inspect_no_args - assert False\n1 failed"

    g._ss_record_test(first_cmd, failure, failed=True, passed=False)
    g._ss_record_test(retry_cmd, failure, failed=True, passed=False)
    assert g._ss_recovery_eligible() is True

    g._ss_record_test(
        "pytest tests/test_other.py -q", "1 passed", failed=False, passed=True)
    assert g._ss_recovery_eligible() is True

    g._ss_record_test(
        retry_cmd,
        "tests/test_cli.py::test_inspect_no_args PASSED [100%]\n1 passed",
        failed=False,
        passed=True,
    )
    assert g._ss_recovery_eligible() is False

    g._ss_record_test(
        "pytest tests/test_other.py::test_other -x",
        "FAILED tests/test_other.py::test_other - assert False\n1 failed",
        failed=True,
        passed=False,
    )
    assert g._ss_recovery_eligible() is False


def test_r5_recovery_v2_edit_resets_failure_identity_streak():
    cmd = "pytest tests/test_cli.py::test_inspect_no_args -x"
    out = "FAILED tests/test_cli.py::test_inspect_no_args - assert False"
    g._ss_record_test(cmd, out, failed=True, passed=False)
    g._ss_record_edit(
        "pkg/policy.py", "sed -i 's/old/new/' pkg/policy.py", "",
        returncode=0, bytes_changed=True,
    )
    assert g._ss_failure_keys_by_test_cmd == {}
    g._ss_record_test(cmd, out, failed=True, passed=False)

    assert g._ss_recovery_eligible() is False


def test_r5_recovery_v2_attempt_reset_clears_failure_aliases():
    g._ss_record_test(
        "pytest tests/test_cli.py -x",
        "FAILED tests/test_cli.py::test_inspect_no_args - assert False\n1 failed",
        failed=True,
        passed=False,
    )
    assert g._ss_failure_keys_by_test_cmd

    g._ss_reset()

    assert g._ss_test_fail_counts == {}
    assert g._ss_failure_keys_by_test_cmd == {}


def test_r5_nodeid_pass_clears_file_command_failure_identity_without_prior_alias():
    file_cmd = "pytest tests/test_cli.py -x"
    failure = "FAILED tests/test_cli.py::test_inspect_no_args - assert False\n1 failed"
    g._ss_record_test(file_cmd, failure, failed=True, passed=False)
    g._ss_record_test(file_cmd, failure, failed=True, passed=False)
    assert g._ss_recovery_eligible() is True

    # This exact node-id command never failed before. Its passing identity, not command
    # string equality, must retire the earlier file-level alias.
    g._ss_record_test(
        "pytest tests/test_cli.py::test_inspect_no_args -x",
        "1 passed",
        failed=False,
        passed=True,
    )

    assert g._ss_recovery_eligible() is False
    assert g._ss_test_fail_counts == {}
    assert g._ss_failure_keys_by_test_cmd == {}


def test_r5_partial_nodeid_pass_does_not_clear_multi_failure_identity():
    file_cmd = "pytest tests/test_cli.py -x"
    failure = (
        "FAILED tests/test_cli.py::test_one - assert False\n"
        "FAILED tests/test_cli.py::test_two - assert False\n2 failed"
    )
    g._ss_record_test(file_cmd, failure, failed=True, passed=False)
    g._ss_record_test(file_cmd, failure, failed=True, passed=False)
    assert g._ss_recovery_eligible() is True

    g._ss_record_test(
        "pytest tests/test_cli.py::test_one -x", "1 passed",
        failed=False, passed=True)

    assert g._ss_recovery_eligible() is True
    assert len(g._ss_test_fail_counts) == 1


def test_r5_stash_counterfactual_does_not_advance_agent_state_failure(monkeypatch):
    """A test against stashed source is evidence about the repo baseline, not the
    agent's current edited state.  It must not release recovery or replace submit-RED."""
    monkeypatch.setenv("GT_SS_RECOVERY_V2", "1")
    monkeypatch.setattr(g, "_oracle_edited_rels", {"pkg/policy.py"})
    monkeypatch.setattr(g, "_action_count", 1)
    failure = "pkg/policy.py:8: assertion failed\n1 failed"
    live_cmd = "pytest tests/test_policy.py -q"
    g._ss_record_test(live_cmd, failure, failed=True, passed=False)
    prior_submit_red = dict(g._ss_last_failing_test or {})

    monkeypatch.setattr(g, "_action_count", 2)
    baseline_cmd = "git stash && pytest tests/test_policy.py -q ; git stash pop"
    g._ss_record_test(baseline_cmd, failure, failed=True, passed=False)

    assert g._ss_test_fail_counts == {live_cmd: 1}
    assert g._ss_current_failure_event is None
    assert g._ss_last_failing_test == prior_submit_red
    assert g._ss_test_events == [(1, False)]
    assert g._ss_recovery_eligible() is False


def test_r5_open_stash_window_test_does_not_advance_agent_state_failure(monkeypatch):
    """The same rule applies when stash push/test/pop occupy separate turns."""
    monkeypatch.setenv("GT_SS_RECOVERY_V2", "1")
    failure = "FAILED tests/test_policy.py::test_rule - assert False\n1 failed"
    cmd = "pytest tests/test_policy.py::test_rule -q"

    monkeypatch.setattr(g, "_action_count", 1)
    g._ss_record_test(cmd, failure, failed=True, passed=False)
    assert g._l5_failure_nudge("git stash", "") == ""

    monkeypatch.setattr(g, "_action_count", 2)
    g._ss_record_test(cmd, failure, failed=True, passed=False)
    assert g._ss_test_fail_counts == {
        "test_identity:tests/test_policy.py::test_rule": 1,
    }
    assert g._ss_current_failure_event is None
    assert g._ss_recovery_eligible() is False

    assert g._l5_failure_nudge("git stash pop", "") == ""
    monkeypatch.setattr(g, "_action_count", 3)
    g._ss_record_test(cmd, failure, failed=True, passed=False)
    assert g._ss_recovery_eligible() is True


def test_r5_stash_counterfactual_does_not_replace_recent_outcome_or_classify(monkeypatch):
    """The live caller must not feed a baseline probe to recovery recency/classification."""
    monkeypatch.setenv("GT_HYPOTHESIS", "1")
    monkeypatch.setenv("GT_SS_RECOVERY_V2", "1")
    monkeypatch.setattr(g, "_last_test_outcome_failed", False)
    monkeypatch.setattr(g, "_source_edit_count", 1)
    monkeypatch.setattr(g, "_oracle_edited_rels", {"pkg/policy.py"})
    classified: list[tuple[str, str]] = []
    monkeypatch.setattr(
        g, "_gt_hypothesis_classify_turn",
        lambda cmd, out: classified.append((cmd, out)),
    )

    out = {
        "output": "FAILED tests/test_policy.py::test_rule - assert False\n1 failed",
        "returncode": 1,
    }
    g._augment_output(
        {"command": "git stash && pytest tests/test_policy.py::test_rule -q ; git stash pop"},
        out,
    )

    assert g._last_test_outcome_failed is False
    assert g._ss_current_failure_event is None
    assert classified == []


def test_r5_attempt_reset_clears_stash_window_and_baseline_failure_memory(monkeypatch):
    """A partial stash turn or prior baseline signature cannot leak to a new attempt."""
    monkeypatch.setattr(g, "_stash_depth", 1)
    monkeypatch.setattr(g, "_baseline_fail_sigs", {"old failure"})

    g._reset_oracle_state()

    assert g._stash_depth == 0
    assert g._baseline_fail_sigs == set()


def test_r5_combined_stash_probe_closes_depth_in_same_turn(monkeypatch):
    monkeypatch.setattr(g, "_stash_depth", 0)
    cmd = "git stash && pytest tests/test_policy.py -q ; git stash pop"

    assert g._stash_baseline_probe(cmd) is True
    assert g._l5_failure_nudge(cmd, "1 failed") == ""
    assert g._stash_depth == 0


def test_r5_ss_flags_off_preserve_legacy_test_state(monkeypatch):
    """With every consuming SS feature disabled, host state is byte-compatible."""
    for key in ("GT_SS_COHERENCE_V2", "GT_SS_RECOVERY_V2", "GT_SS_SUBMIT_RED"):
        monkeypatch.delenv(key, raising=False)
    cmd = "git stash && pytest tests/test_policy.py::test_rule -q ; git stash pop"
    failure = "FAILED tests/test_policy.py::test_rule - assert False\n1 failed"

    g._ss_record_test(cmd, failure, failed=True, passed=False)

    assert g._ss_test_fail_counts == {
        "test_identity:tests/test_policy.py::test_rule": 1,
    }
    assert g._ss_current_failure_event == (g._action_count, next(iter(g._ss_test_fail_counts)))


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
