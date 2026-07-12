"""Red->green regression tests for the 2026-07-04 adversarial-review fixes.

Each test names the runtime/delivery-wiring bug it guards and FAILS on the pre-fix
code (proven by reverting the corresponding hunk), PASSES after. Findings covered:

  R10 - proof-mode fail-closed was INVERTED: the `.pth` site-load channel swallows
        an import-time raise, so a GT-off proof run was silently graded GT-on. A
        POSITIVE on-disk sentinel (written only on a full GT-on proof load) + a
        harness assertion replaces the swallowable raise.
  R1  - GT_POST_SEARCH read by the code but never forwarded via --ae (un-enableable
        in prod) + a structural fail-closed parity invariant (reads subset of --ae).
  R3  - the end-of-budget obligation checklist re-armed EVERY turn past 80% (~5.1KB
        flood); now a one-shot latch + a per-emission max_listed cap.
  R4  - <gt-evidence> fired on TEST-file views ("preserve this interface" for test
        code); now gated on the canonical is_test_or_demo predicate.
  R5  - obligation.resurface bypassed the delivery ledger + fire counter (direct
        append); now routed through _lane_a_deliver.
  R6  - _evidence consumed the fire-once latch BEFORE the emptiness check, so one
        transient EMPTY evidence permanently silenced a file; latch now after.
  R9  - semantic_drift guard was a Python-only `if ...:` regex (dead on the ~70%
        of DeepSWE that is Go/Rust/JS/TS); now matches brace-language `if (..) {`.
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import textwrap

import pytest

_HERE = os.path.dirname(__file__)
_ARTIFACT_DIR = os.path.join(_HERE, "..")
sys.path.insert(0, _ARTIFACT_DIR)

import gt_mini_patch as g  # noqa: E402
import gt_agent as ga  # noqa: E402

_REPO_ROOT = os.path.abspath(os.path.join(_ARTIFACT_DIR, ".."))
_WORKFLOW = os.path.join(_REPO_ROOT, ".github", "workflows", "deepswe_full.yml")
_AE_BLOCK = os.path.join(_ARTIFACT_DIR, "gt_integration", "gt_ae_block.sh")
_PATCH_SRC = os.path.join(_ARTIFACT_DIR, "gt_mini_patch.py")


# ============================================================================ #
# R10 — proof-mode fail-closed sentinel (the critical inversion)
# ============================================================================ #
def test_r10_verdict_failclosed_on_missing_marker():
    """The harness verdict fails a GT-ON proof run when the marker is absent, and
    passes it when present / when not a proof run."""
    ok, _ = ga._proof_marker_verdict(proof_required=True, marker_present=False)
    assert ok is False  # GT-on proof + no marker -> NOT graded as GT-on
    ok, _ = ga._proof_marker_verdict(proof_required=True, marker_present=True)
    assert ok is True
    ok, _ = ga._proof_marker_verdict(proof_required=False, marker_present=False)
    assert ok is True  # baseline / non-proof needs no marker


def test_r10_selftest_asserts_marker_in_proof_mode():
    """The in-container harness check (selftest) must ASSERT the positive marker,
    not rely on the swallowed raise."""
    src = ga._SELFTEST_PY
    assert "GT_PROOF_MARKER" in src
    assert "GT_PROOF_MODE" in src
    # a distinct non-zero exit for the proof-marker-absent failure mode
    assert "sys.exit(8)" in src


def test_r10_write_and_clear_marker(monkeypatch, tmp_path):
    marker = str(tmp_path / "sub" / "gt_proof_active")
    # proof mode, GT-on arm -> written
    monkeypatch.setenv("GT_PROOF_MODE", "1")
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    g._write_proof_marker(marker)
    assert os.path.exists(marker)
    # clear removes it
    g._clear_proof_marker(marker)
    assert not os.path.exists(marker)
    # baseline arm -> NOT written (baseline is intentionally GT-off)
    monkeypatch.setattr(g, "_GT_BASELINE", True)
    g._write_proof_marker(marker)
    assert not os.path.exists(marker)
    # non-proof -> NOT written
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.delenv("GT_PROOF_MODE", raising=False)
    g._write_proof_marker(marker)
    assert not os.path.exists(marker)


def _run_pth_load(oracle_route: str, marker: str) -> bool:
    """Reproduce the REAL site.py `.pth` swallow: gt_mini_patch is imported by a
    site directory `.pth` line; site.addsitedir swallows any import-time raise.
    Returns whether the proof marker exists after the (possibly swallowed) load."""
    fake_site = tempfile.mkdtemp()
    art = _ARTIFACT_DIR.replace("\\", "/")
    with open(os.path.join(fake_site, "zz_gt.pth"), "w", encoding="utf-8") as fh:
        fh.write(f'import sys; sys.path.insert(0, r"{art}"); import gt_mini_patch\n')
    driver = textwrap.dedent(f"""
        import site, os
        site.addsitedir(r"{fake_site.replace(chr(92), '/')}")   # swallows a .pth raise
        print("MARKER=" + ("1" if os.path.exists(r"{marker.replace(chr(92), '/')}") else "0"))
    """)
    env = dict(os.environ)
    env["GT_PROOF_MODE"] = "1"
    env["GT_BASELINE"] = "0"
    env["GT_ORACLE_ROUTE"] = oracle_route
    env["GT_PROOF_MARKER"] = marker
    env.pop("PYTHONPATH", None)
    # HOST-LOCALE ROBUSTNESS (2026-07-09): the .pth import loads minisweagent, whose
    # __init__ prints a 👋 emoji. On a cp1252 host (Windows console, piped stdout) that
    # raise UnicodeEncodeError -> minisweagent import fails -> _install patches ZERO env
    # classes -> _write_proof_marker no-ops (its _PATCHED_CLASSES guard) -> the clean-load
    # control writes no marker and the test false-FAILs. Pin utf-8 so the subprocess
    # reproduces the CI (utf-8) load deterministically; no-op where the locale is already
    # utf-8. This tests GT's fail-closed marker, not the host console encoding.
    env["PYTHONIOENCODING"] = "utf-8"
    out = subprocess.run([sys.executable, "-c", driver], capture_output=True,
                         text=True, env=env)
    return "MARKER=1" in out.stdout


def test_r10_swallowed_pth_raise_leaves_no_marker(tmp_path):
    """THE bug: with GT_ORACLE_ROUTE=0 in proof mode gt_mini_patch raises at import;
    site.py swallows it -> module never loads -> GT is OFF. The positive sentinel
    catches this: NO marker is written (harness fails the run). The clean-load
    control (ORACLE_ROUTE=1) DOES write the marker.

    RED on pre-fix code: no _write_proof_marker exists, so even the clean-load
    control writes no marker -> the `present` assertion fails."""
    m1 = str(tmp_path / "clean_marker")
    present = _run_pth_load("1", m1)
    assert present is True, "clean GT-on proof load must write the sentinel"

    m2 = str(tmp_path / "swallowed_marker")
    swallowed = _run_pth_load("0", m2)
    assert swallowed is False, "a swallowed .pth raise must leave NO sentinel"
    # harness verdict on the swallowed case: NOT GT-on
    ok, _ = ga._proof_marker_verdict(proof_required=True, marker_present=swallowed)
    assert ok is False


# ============================================================================ #
# R1 — GT_POST_SEARCH forwarding + fail-closed --ae parity invariant
# ============================================================================ #
_GT_READ_RE = re.compile(
    r"(?:os\.environ\.get|os\.getenv)\(\s*[\"'](GT_[A-Z0-9_]+)[\"']"
    r"|os\.environ\[\s*[\"'](GT_[A-Z0-9_]+)[\"']"
)
_AE_RE = re.compile(r'--ae\s+"?(GT_[A-Z0-9_]+)=')


def _gt_reads() -> set[str]:
    src = open(_PATCH_SRC, encoding="utf-8").read()
    out: set[str] = set()
    for m in _GT_READ_RE.finditer(src):
        out.add(m.group(1) or m.group(2))
    return out


def _ae_forwarded() -> set[str]:
    keys: set[str] = set()
    for path in (_WORKFLOW, _AE_BLOCK):
        keys |= set(_AE_RE.findall(open(path, encoding="utf-8").read()))
    return keys


# Vars intentionally NOT forwarded via --ae: host-provided/derived paths, pure
# defaulted tuning knobs, and debug-only opt-ins. Each has a safe in-code default,
# so it is NOT a channel that becomes un-enableable without --ae. A NEW behavioral
# flag would NOT be here -> the invariant below catches it (fail-closed).
_HOST_LOCAL_EXEMPT = frozenset({
    "GT_ANCHORS_PATH",                       # derived anchors path (from GT_CERT_DIR)
    "GT_ARTIFACTS_DIR",                      # host artifacts dir (defaulted "")
    "GT_C1_CONFIDENCE_FLOOR_MAD_MULTIPLIER", # tuning knob, safe default
    "GT_GATEWAY_MAX_DELTA",                  # W2 tuning knob, safe default "4000"
    "GT_LANE_MAX_DELTA",                     # RL-1 lane budget knob, safe default "16000"
    "GT_GRAPH_DB",                           # legacy pre-substrate graph (GT_HOST_GRAPH_DB is forwarded)
    "GT_HORIZON_CALIBRATION",                # calibration JSON path, shipped default in-repo
    "GT_HOME",                               # container install prefix, default /opt/gt
    "GT_HOOK_TIMEOUT",                       # tuning default "30"
    "GT_INDEX_CACHE",                        # local cache path default /tmp/gt_index.json
    "GT_ISSUE_FILE",                         # optional issue-file override (CERT_DIR/issue.txt primary)
    "GT_MTIME_SCAN_CAP",                     # tuning default "20000"
    "GT_OBLIGATION_STATUS",                  # local status file path default
    "GT_PROOF_MARKER",                       # R10 sentinel path, host-local default
    "GT_RESURF_DEBUG",                       # debug-only opt-in
    "GT_ROOT_FILE",                          # container path default /opt/gt/gt_root.txt
    # GT_XSESSION_DIR GRADUATED off this exempt set (BUG-2, 2026-07-12): it is now
    # --ae-forwarded in gt_ae_block.sh into ${GT_C_OUT}/gt_xsession (a writable, host-mounted
    # dir on every pier caller), so it is covered by FORWARDING — not host-local. Removing
    # that forward re-trips this invariant (the same fail-closed guarantee GT_POST_SEARCH has).
    # The direct-docker mini path forwards it via `docker run -e` (see
    # tests/test_live_lite_xsession_dir_pin_20260712.py).
})


# NAMED SEAM — the pending-forward register for a NEW behavioral flag whose --ae forward +
# profile membership are OWNED by a CONCURRENT coder in the SAME session, so it cannot yet be
# wired from this surface. UNLIKE _HOST_LOCAL_EXEMPT, such a flag is NOT host-local: it MUST be
# forwarded to be enableable in the container. A flag lives here ONLY until its forward lands in
# gt_ae_block.sh (at which point _ae_forwarded() covers it via the preferred forwarding path —
# the same way GT_POST_SEARCH_NATIVE is covered), then it is REMOVED. The mechanism is retained
# (the r1 parity invariant below subtracts this set) so the next cross-coder seam has a home.
#   CURRENTLY EMPTY: GT_SCOPE_NATIVE (Task #63 — the scope-surface FORM A/B) was forwarded in
#   gt_ae_block.sh AND added to rl_profile._PROFILE_1_MEMBERS this session (2026-07-12), so it
#   graduated off the seam onto the preferred forwarding path.
_PENDING_AE_FORWARD_SEAM: frozenset[str] = frozenset()


def test_r1_post_search_is_forwarded():
    assert "GT_POST_SEARCH" in _ae_forwarded(), \
        "GT_POST_SEARCH must be forwarded via --ae or it is un-enableable in prod"


def test_r1_ae_parity_invariant_failclosed():
    """Structural invariant: every GT_* var the code READS (minus documented host-local
    exemptions and the NAMED-SEAM pending-forward set) MUST be forwarded via --ae. A read with
    no --ae entry, no exemption, and no seam fails the run. RED before the GT_POST_SEARCH --ae
    line."""
    reads = _gt_reads()
    forwarded = _ae_forwarded()
    uncovered = sorted(reads - forwarded - _HOST_LOCAL_EXEMPT - _PENDING_AE_FORWARD_SEAM)
    assert uncovered == [], (
        "GT_* env var(s) read by gt_mini_patch.py but neither forwarded via --ae, documented "
        f"host-local, nor a pending-forward NAMED SEAM: {uncovered}")
    # fail-closed demonstration: GT_POST_SEARCH is covered by FORWARDING, not by
    # exemption, so removing it from --ae would re-trip the invariant.
    assert "GT_POST_SEARCH" in reads
    assert "GT_POST_SEARCH" not in _HOST_LOCAL_EXEMPT
    assert "GT_POST_SEARCH" in forwarded


# ============================================================================ #
# R3 — end-of-budget obligation checklist: one-shot latch + max_listed cap
# ============================================================================ #
class _V:
    def __init__(self, idx):
        self.idx = idx
        self.verbatim = "requirement %d" % idx


class _FakeOm:
    OBL_EDITED_UNTESTED = "EU"

    def __init__(self, statuses):
        self._statuses = statuses
        self.render_calls = []

    def load_obligations(self, path):
        return [1] * len(self._statuses)

    def order_unmet(self, statuses):
        return list(statuses)

    def status_vector_hash(self, statuses):
        return "STABLE_H"  # stable vector -> exposes the every-turn re-arm flood

    def composite_severity(self, base, budget_fraction, unmet_ratio):
        return float(base)

    def render_obligation_status_block(self, statuses, covering=None, max_listed=None):
        self.render_calls.append(max_listed)
        rows = statuses if max_listed is None else statuses[:max_listed]
        return "OBLIG_BLOCK n=%d" % len(rows)


class _FakeTracker:
    def update(self, *a):
        pass

    def statuses_tuple(self, edited, tested):
        return _STATUSES


_STATUSES = tuple((_V(i), "EU", (), 1.0) for i in range(10))  # 10 unmet, all edited-untested


@pytest.fixture
def _oblig_env(monkeypatch):
    om = _FakeOm(_STATUSES)
    monkeypatch.setattr(g, "_load_gt_oracle", lambda: om)
    monkeypatch.setattr(g, "_get_obligation_tracker", lambda _om: _FakeTracker())
    monkeypatch.setattr(g, "_persist_obligation_status", lambda _t: None)
    monkeypatch.setattr(g, "_covering_tests_for_symbols", lambda _s: [])
    monkeypatch.setattr(g, "_anchors_path", lambda: "")
    monkeypatch.setattr(g, "_GT_STEP_LIMIT", 100)
    monkeypatch.setattr(g, "_action_count", 90)          # 90% budget > 80%
    monkeypatch.setattr(g, "_oblig_status_emitted", set())
    monkeypatch.setattr(g, "_oblig_final_shot_fired", False)
    monkeypatch.setattr(g, "_oracle_obligation_fired", False)
    monkeypatch.setattr(g, "_oracle_edited_tokens", set())
    monkeypatch.setattr(g, "_oracle_tested_tokens", set())
    return om


def test_r3_end_of_budget_checklist_fires_at_most_once(_oblig_env):
    """Past 80% budget with a STABLE status vector, the buggy re-arm discarded the
    dedup hash every turn -> the checklist re-fired every turn (~5.1KB flood). The
    one-shot latch caps it at a single emission. RED pre-fix: 8 fires."""
    fires = [g._obligation_nudge_block() for _ in range(8)]
    non_none = [f for f in fires if f is not None]
    assert len(non_none) == 1, f"expected ONE end-of-budget shot, got {len(non_none)}"


def test_r3_emission_is_capped(_oblig_env):
    """The single emission renders at most _OBLIG_MAX_LISTED obligations (the cap is
    passed to render_obligation_status_block). RED pre-fix: max_listed=None."""
    fires = [g._obligation_nudge_block() for _ in range(8)]
    non_none = [f for f in fires if f is not None]
    assert non_none, "expected at least one emission"
    assert _oblig_env.render_calls[0] == g._OBLIG_MAX_LISTED
    _sev, payload = non_none[0]
    assert "n=%d" % g._OBLIG_MAX_LISTED in payload  # only the capped rows rendered


# ============================================================================ #
# R4 — <gt-evidence> must NOT fire on test-file views
# ============================================================================ #
def _mk_witness_graph(db, viewed, caller, lang="go"):
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE nodes (id INTEGER PRIMARY KEY, label TEXT, name TEXT,
          qualified_name TEXT, file_path TEXT, start_line INTEGER, end_line INTEGER,
          signature TEXT, return_type TEXT, is_exported INTEGER, is_test INTEGER,
          language TEXT, parent_id INTEGER);
        CREATE TABLE edges (id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,
          type TEXT, source_line INTEGER, source_file TEXT, resolution_method TEXT,
          confidence REAL, metadata TEXT);
        """
    )
    con.execute("INSERT INTO nodes VALUES (1,'Function','getStuff',NULL,?,5,9,'getStuff()','',1,0,?,NULL)", (viewed, lang))
    con.execute("INSERT INTO nodes VALUES (2,'Function','realCaller',NULL,?,10,14,'realCaller()','',1,0,?,NULL)", (caller, lang))
    con.execute("INSERT INTO edges VALUES (NULL,2,1,'CALLS',10,?,'import',1.0,NULL)", (caller,))
    con.commit()
    con.close()


def test_r4_source_file_still_emits_evidence(monkeypatch, tmp_path):
    db = str(tmp_path / "src.db")
    _mk_witness_graph(db, "src/lib.go", "src/app.go")
    monkeypatch.setattr(g, "_db_path", lambda: db)
    monkeypatch.setattr(g, "_code_at", lambda root, fp, line: "getStuff()")
    ev = g._evidence_body("post_view", "src/lib.go", "/testbed")
    assert ev and "[WITNESS]" in ev  # a real source subject still gets facts


def test_r4_test_file_emits_no_evidence(monkeypatch, tmp_path):
    """Same graph shape, but the VIEWED file is a test file (caught by the canonical
    is_test_or_demo predicate). No <gt-evidence> may be produced. RED pre-fix: the
    [WITNESS]/[CALLERS] lines leaked for the test file."""
    for viewed, caller in [
        ("pkg/foo_test.go", "pkg/app.go"),        # *_test.* basename
        ("interval/tests/mod.rs", "interval/lib.rs"),  # tests/ dir segment
    ]:
        db = str(tmp_path / (viewed.replace("/", "_") + ".db"))
        _mk_witness_graph(db, viewed, caller, lang="rust")
        monkeypatch.setattr(g, "_db_path", lambda _db=db: _db)
        monkeypatch.setattr(g, "_code_at", lambda root, fp, line: "getStuff()")
        assert g._is_test_or_demo_path(viewed) is True
        ev = g._evidence_body("post_view", viewed, "/testbed")
        assert ev == "", f"test file {viewed} must emit no evidence, got: {ev!r}"


# ============================================================================ #
# R5 — obligation.resurface must route through _lane_a_deliver (ledger + cap)
# ============================================================================ #
def test_r5_resurface_routes_through_ledger(monkeypatch, tmp_path):
    """The resurface delivery must go through _lane_a_deliver so it is recorded in
    the delivery ledger + fire counter (not an unaccounted direct append). RED
    pre-fix: delivered but the content hash never lands in _oracle_delivered_hashes
    and the fire counter never sees 'obligation.resurface'."""
    monkeypatch.setenv("GT_HOOK_FIRE_COUNTS", str(tmp_path / "fc.json"))
    monkeypatch.setenv("GT_RUNTIME_LEDGER", str(tmp_path / "led.jsonl"))
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_ORACLE_ROUTE", True)
    monkeypatch.setattr(g, "_oracle_delivered_hashes", set())
    # HERMETIC (2026-07-09): this pins the V1 resurface path the docstring documents
    # ("Artifact absent -> byte-identical v1 path"). Without this, a stale
    # $GT_CERT_DIR/gt_obligations_v2.json (default /tmp, written by any prior
    # obligations_v2 run this session) makes _load_obligations_v2() non-None -> the
    # code takes the V2 branch (_unexercised_clause_candidate) and IGNORES the
    # monkeypatched v1 candidate -> RESURFACE_BODY never delivered. Same stale-artifact
    # fragility class as the E4 gt_issue_anchors.json poisoning.
    monkeypatch.setattr(g, "_load_obligations_v2", lambda: None)
    monkeypatch.setattr(g, "_HOOK_FIRE_COUNTS", {})
    monkeypatch.setattr(g, "_seen", set())
    monkeypatch.setattr(g, "_action_count", 0)
    monkeypatch.setattr(g, "_classify", lambda cmd: ("post_edit", "src/foo.py"))
    monkeypatch.setattr(g, "_effective_cmd", lambda action: "sed -i s/a/b/ src/foo.py")
    monkeypatch.setattr(g, "_detect_phase", lambda: g.Phase.SUBMIT)
    resurf = '\n<gt-nudge reason="obligation_resurface">RESURFACE_BODY</gt-nudge>'
    monkeypatch.setattr(g, "_obligation_resurface_candidate", lambda: (5.0, resurf))

    out = {"output": ""}
    g._augment_output({"command": "sed -i s/a/b/ src/foo.py"}, out)

    assert "RESURFACE_BODY" in (out.get("output") or "")   # delivered
    hc = "c:" + hashlib.sha256(resurf.encode("utf-8")).hexdigest()[:16]  # D-2: 16 hex
    assert hc in g._oracle_delivered_hashes, "resurface not recorded in the delivery ledger"
    assert g._HOOK_FIRE_COUNTS.get("obligation.resurface") == 1, "resurface fire not counted"


# ============================================================================ #
# R6 — _evidence must not latch the fire-once _seen on an EMPTY evidence
# ============================================================================ #
def test_r6_empty_evidence_does_not_silence_file(monkeypatch):
    """A first (transient) EMPTY evidence for a file must NOT permanently silence a
    later NON-empty evidence for the SAME file. RED pre-fix: _seen.add ran before
    the emptiness check, so the second call returned '' from the latch."""
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_seen", set())
    monkeypatch.setattr(g, "_classify", lambda cmd: ("post_view", "src/lib.py"))
    monkeypatch.setattr(g, "_to_repo_rel", lambda f, root: f)
    monkeypatch.setattr(g, "_root", lambda: "/testbed")
    monkeypatch.setattr(g, "_translate_to_action", lambda ev, ph: ev)
    monkeypatch.setattr(g, "_budget_trim", lambda ev: ev)
    monkeypatch.setattr(g, "_detect_phase", lambda: None)

    calls = {"n": 0}

    def _body(kind, rel, root, cmd=""):  # B4: _evidence_body now takes cmd
        calls["n"] += 1
        return "" if calls["n"] == 1 else "[WITNESS] real fact"

    monkeypatch.setattr(g, "_evidence_body", _body)

    first = g._evidence("cat src/lib.py")
    assert first == ""                       # transient empty
    second = g._evidence("cat src/lib.py")
    assert "[WITNESS] real fact" in second   # later non-empty still delivers


# ============================================================================ #
# R9 — semantic_drift guard must be language-agnostic (brace langs too)
# ============================================================================ #
def test_r9_brace_language_if_guard_is_extracted():
    """A Go/JS/Rust `if (cond) { return }` guard is extracted the same as a Python
    `if cond: return`. RED pre-fix: the `\\bif\\s+(.+?):` regex matched only the
    Python colon form -> the brace-language guard set was empty."""
    py = "def f(x):\n    if x is None:\n        return 0\n    return x\n"
    js = "function f(x) {\n    if (x == null) {\n        return 0;\n    }\n    return x;\n}\n"
    go = "func f(x int) int {\n    if x < 0 {\n        return 0\n    }\n    return x\n}\n"

    py_guards, _ = g._sem_extract(py)
    js_guards, _ = g._sem_extract(js)
    go_guards, _ = g._sem_extract(go)

    assert py_guards, "python guard must be extracted (baseline)"
    assert js_guards, "JS `if (cond) {` guard must be extracted (R9)"
    assert go_guards, "Go `if cond {` guard must be extracted (R9)"


def test_r9_lost_brace_guard_triggers_drift(monkeypatch, tmp_path):
    """A deleted brace-language guard clause fires the drift nudge across snapshots
    (the same behavior a deleted Python guard already had)."""
    monkeypatch.setattr(g, "_sem_cache", {})
    monkeypatch.setattr(g, "_root", lambda: str(tmp_path))
    f = tmp_path / "handler.ts"
    f.write_text(
        "function handle(req) {\n"
        "    if (req == null) {\n"
        "        throw new Error('bad');\n"
        "    }\n"
        "    return process(req);\n"
        "}\n", encoding="utf-8")
    # first sight = baseline snapshot (never fires)
    assert g._semantic_drift_candidate("handler.ts") is None
    # delete the guard clause
    f.write_text(
        "function handle(req) {\n"
        "    return process(req);\n"
        "}\n", encoding="utf-8")
    res = g._semantic_drift_candidate("handler.ts")
    assert res is not None, "a deleted brace-language guard must fire drift"
    _sev, payload = res
    assert "semantic_drift" in payload
