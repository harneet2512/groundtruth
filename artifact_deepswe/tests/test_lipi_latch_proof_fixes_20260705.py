"""Red->green regression tests for the 2026-07-04/05 LIPI findings.

Each test names the runtime/delivery-wiring bug it guards and FAILS on the pre-fix
code (proven by reverting the corresponding hunk), PASSES after. Findings covered:

  C3 - consensus & contract burned their fire-once latch at PRODUCTION, not at
       DELIVERY. `_graph_contract_block` set `_contract_seen.add(rel)` BEFORE its
       os.path.isfile(db)/empty-rows early-returns; `_consensus_collect` set
       `_consensus_fired` BEFORE querying the graph. A transient empty-graph frame
       (or a Lane-A dedup collision) then burned the latch with NO delivery and the
       re-arm loop only restores on a gate LOSS -> permanent silence. Fix: consume
       `_contract_seen` ONLY on a DELIVERED outcome in `_lane_a_deliver`; consume
       `_consensus_fired` only on a PRODUCTIVE collect (>=1 neighbour).
  C6 - R5 obligation.resurface had the same defect: `_oblig_resurface_fired` was set
       at production; a dedup burned it and it was not in the re-arm set. Fix:
       consume it only on the DELIVERED outcome in `_lane_a_deliver`.
  C1 - the proof marker was asserted only at BUILD/install time and
       `_proof_marker_verdict` was DEAD at runtime; nothing IN-CONTAINER re-checked
       the marker during the paid run. Fix: an in-container RUNTIME fail-closed
       assertion in the agent-startup injection (`_BOOTSTRAP_SNIPPET`) + a host-
       orchestrated post-run in-container marker verdict (`_assert_proof_marker`).
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import sqlite3
import subprocess
import sys
import tempfile

import pytest

_HERE = os.path.dirname(__file__)
_ARTIFACT_DIR = os.path.join(_HERE, "..")
sys.path.insert(0, _ARTIFACT_DIR)

import gt_mini_patch as g  # noqa: E402
import gt_agent as ga  # noqa: E402


# ============================================================================ #
# C3 — contract: production latch removed; transient empty-db must not silence
# ============================================================================ #
def _mk_contract_graph(db: str, filerel: str) -> None:
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
        CREATE TABLE properties (id INTEGER PRIMARY KEY, node_id INTEGER, kind TEXT, value TEXT);
        """
    )
    con.execute(
        "INSERT INTO nodes VALUES (1,'Function','doThing',NULL,?,5,9,"
        "'doThing(x int) int','',1,0,'go',NULL)",
        (filerel,),
    )
    con.commit()
    con.close()


def test_c3_contract_transient_empty_db_does_not_silence(monkeypatch, tmp_path):
    """An empty-db frame (db transiently absent) must NOT consume the contract
    fire-once latch, so the contract still delivers once the graph appears. RED
    pre-fix: `_graph_contract_block` latched `_contract_seen` before the isfile(db)
    check, so the file was silenced forever."""
    rel = "src/foo.go"
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_contract_seen", set())
    monkeypatch.setattr(g, "_root", lambda: str(tmp_path))

    # 1) transient: graph db absent -> empty block, latch must stay armed.
    missing = str(tmp_path / "absent.db")
    monkeypatch.setattr(g, "_db_path", lambda: missing)
    assert g._graph_contract_block(rel) == ""
    assert rel not in g._contract_seen, \
        "an empty-db frame must not consume the contract fire-once latch"

    # 2) graph now present with a real contract -> must still deliver.
    db = str(tmp_path / "graph.db")
    _mk_contract_graph(db, rel)
    monkeypatch.setattr(g, "_db_path", lambda: db)
    block = g._graph_contract_block(rel)
    assert block and "<gt-contract" in block, \
        "contract must deliver even though the earlier frame was empty-db"


def _noop_ledger(monkeypatch):
    monkeypatch.setattr(g, "_record_hook_fire", lambda *a, **k: None)
    monkeypatch.setattr(g, "_ledger_note_delivery", lambda *a, **k: None)
    monkeypatch.setattr(g, "_runtime_ledger_record", lambda *a, **k: None)


def test_c3_contract_latched_only_on_delivery(monkeypatch):
    """A DELIVERED contract consumes the latch inside `_lane_a_deliver`. RED pre-fix:
    `_lane_a_deliver` set only `_cochange_fired`, never `_contract_seen`, so the
    latch was never consumed on delivery (it was consumed at production instead)."""
    _noop_ledger(monkeypatch)
    monkeypatch.setattr(g, "_oracle_delivered_hashes", set())
    monkeypatch.setattr(g, "_contract_seen", set())
    out = {"output": ""}
    g._lane_a_deliver(
        out, "edit",
        [("l3.contract", "\n<gt-contract>SIG</gt-contract>")],
        krel="src/foo.go", event="post_edit",
    )
    assert "<gt-contract>" in out["output"]
    assert "src/foo.go" in g._contract_seen, \
        "a DELIVERED contract must consume the fire-once latch in _lane_a_deliver"


def test_c3_contract_dedup_skip_does_not_latch(monkeypatch):
    """A DEDUP-skipped contract (byte-identical fact already delivered) must NOT
    burn the latch, so a later distinct contract for the file can still deliver."""
    _noop_ledger(monkeypatch)
    monkeypatch.setattr(g, "_contract_seen", set())
    text = "\n<gt-contract>SIG</gt-contract>"
    hc = "c:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
    monkeypatch.setattr(g, "_oracle_delivered_hashes", {hc})  # pre-seed -> dedup
    out = {"output": ""}
    g._lane_a_deliver(out, "edit", [("l3.contract", text)],
                      krel="src/foo.go", event="post_edit")
    assert out["output"] == ""                       # deduped, not delivered
    assert "src/foo.go" not in g._contract_seen, \
        "a dedup-skipped contract must not consume the latch"


# ============================================================================ #
# C3 — consensus collect: transient empty graph must not freeze scope
# ============================================================================ #
def test_c3_consensus_transient_empty_graph_does_not_silence(monkeypatch):
    """A transient empty first view must NOT freeze consensus scope to that view for
    the whole task. RED pre-fix: `_consensus_collect` set `_consensus_fired` before
    querying, so an empty first view latched with only {rel} and every later file
    was refused scope membership (delivering no <gt-scope> for the working file)."""
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_consensus_fired", False)
    monkeypatch.setattr(g, "_consensus_scope", set())
    monkeypatch.setattr(g, "_seen", set())

    state = {"ready": False}

    def _fake_scope(rel):
        return ["src/neighbor.py"] if state["ready"] else []  # empty until warm

    monkeypatch.setattr(g, "_query_scope", _fake_scope)

    # View file A while the graph frame is transiently empty.
    g._consensus_collect("src/a.py")
    # Graph warms up (LSP ready / db present).
    state["ready"] = True
    # The agent now views the file it actually works in.
    g._consensus_collect("src/work.py")
    block = g._consensus_scope_block("src/work.py")
    assert block and "<gt-scope" in block, \
        "a transient empty first view must not freeze consensus scope for the task"


def test_c3_consensus_isolated_first_view_leaves_latch_armed(monkeypatch):
    """An isolated file (no neighbours) at first view must not burn the collect
    latch — a later connected file must still register in scope."""
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_consensus_fired", False)
    monkeypatch.setattr(g, "_consensus_scope", set())
    monkeypatch.setattr(g, "_seen", set())
    scopes = {"iso.py": [], "hub.py": ["dep.py"]}
    monkeypatch.setattr(g, "_query_scope", lambda rel: scopes.get(rel.split("/")[-1], []))
    g._consensus_collect("pkg/iso.py")     # isolated -> no latch
    assert g._consensus_fired is False
    g._consensus_collect("pkg/hub.py")     # connected -> latches
    assert g._consensus_fired is True
    assert g._norm_rel("pkg/hub.py") in g._consensus_scope


# ============================================================================ #
# C6 — obligation.resurface: production latch removed; delivered-only
# ============================================================================ #
def test_c6_resurface_candidate_does_not_burn_latch(monkeypatch, tmp_path):
    """Producing a resurface candidate must NOT set `_oblig_resurface_fired` — the
    latch is consumed only on the DELIVERED outcome. RED pre-fix:
    `_obligation_resurface_candidate` set the latch at production (before delivery),
    so a dedup'd first resurface permanently silenced a later distinct one."""
    monkeypatch.setattr(g, "_GT_BASELINE", False)
    monkeypatch.setattr(g, "_oblig_resurface_fired", False)
    monkeypatch.setattr(g, "_load_gt_oracle", lambda: None)   # no structured obligations
    monkeypatch.setattr(g, "_anchors_path", lambda: "")
    monkeypatch.setenv("GT_CERT_DIR", str(tmp_path))          # no issue.txt here
    issue = tmp_path / "issue.txt_alt"
    issue.write_text("The parser must handle empty input without crashing.\n",
                     encoding="utf-8")
    monkeypatch.setenv("GT_ISSUE_FILE", str(issue))

    res = g._obligation_resurface_candidate()
    assert res is not None, "a resurface candidate should be produced from the issue"
    assert g._oblig_resurface_fired is False, \
        "producing a resurface candidate must NOT burn the fire-once latch"


def test_c6_resurface_latched_only_on_delivery(monkeypatch):
    """A DELIVERED resurface consumes the latch inside `_lane_a_deliver`. RED
    pre-fix: `_lane_a_deliver` never set `_oblig_resurface_fired`."""
    _noop_ledger(monkeypatch)
    monkeypatch.setattr(g, "_oracle_delivered_hashes", set())
    monkeypatch.setattr(g, "_oblig_resurface_fired", False)
    out = {"output": ""}
    g._lane_a_deliver(
        out, "edit",
        [("obligation.resurface", "\n<gt-nudge>REQ</gt-nudge>")],
        krel="src/foo.py", event="post_edit",
    )
    assert "REQ" in out["output"]
    assert g._oblig_resurface_fired is True, \
        "a DELIVERED resurface must consume the fire-once latch in _lane_a_deliver"


# ============================================================================ #
# C1 — in-container RUNTIME proof-marker fail-closed
# ============================================================================ #
def test_c1_bootstrap_snippet_has_runtime_failclose():
    """Structural: the agent-startup injection carries an in-container runtime
    fail-closed check mirroring _proof_marker_verdict (proof gate + baseline gate +
    marker existence + raise). RED pre-fix: the snippet was import-only."""
    src = ga._BOOTSTRAP_SNIPPET
    assert "GT_PROOF_MODE" in src
    assert "GT_BASELINE" in src
    assert "GT_PROOF_MARKER" in src or "gt_proof_active" in src
    assert "raise" in src


def _run_bootstrap(gt_mini_body: str, env_extra: dict) -> int:
    """Execute the injected `_BOOTSTRAP_SNIPPET` as default.py-appended code would,
    with a controllable fake gt_mini_patch on the GT dir, and return the exit code.
    A swallowed load in proof mode with no marker must abort (non-zero)."""
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "gt_mini_patch.py"), "w", encoding="utf-8") as fh:
        fh.write(gt_mini_body)
    snippet = ga._BOOTSTRAP_SNIPPET.replace(ga._GT_DIR, d.replace("\\", "/"))
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.update(env_extra)
    r = subprocess.run([sys.executable, "-c", snippet],
                       capture_output=True, text=True, env=env)
    return r.returncode


def test_c1_bootstrap_failcloses_on_missing_marker(tmp_path):
    """Behavioral: proof-required + swallowed GT load + marker absent -> the agent
    process aborts (fail-closed). Baseline / non-proof / clean-load -> no abort.
    RED pre-fix: the import-only snippet swallowed the raise and exited 0."""
    raising = "raise RuntimeError('simulated GT_ORACLE_ROUTE=0 proof-mode raise')\n"
    writing = ("import os\n"
               "open(os.environ['GT_PROOF_MARKER'], 'w', encoding='utf-8').write('x')\n")

    m1 = str(tmp_path / "m1")
    rc = _run_bootstrap(raising, {"GT_PROOF_MODE": "1", "GT_BASELINE": "0",
                                  "GT_PROOF_MARKER": m1})
    assert rc != 0, "a swallowed GT load in proof mode (no marker) must abort the agent"

    m2 = str(tmp_path / "m2")
    rc = _run_bootstrap(writing, {"GT_PROOF_MODE": "1", "GT_BASELINE": "0",
                                  "GT_PROOF_MARKER": m2})
    assert rc == 0, "a clean GT-on proof load (marker written) must not abort"

    m3 = str(tmp_path / "m3")
    rc = _run_bootstrap(raising, {"GT_PROOF_MODE": "1", "GT_BASELINE": "1",
                                  "GT_PROOF_MARKER": m3})
    assert rc == 0, "baseline (GT-off) needs no marker -> must not abort"

    m4 = str(tmp_path / "m4")
    env_np = dict(GT_PROOF_MARKER=m4)
    env_np["GT_PROOF_MODE"] = "0"           # explicitly non-proof
    env_np["GT_BASELINE"] = "0"
    rc = _run_bootstrap(raising, env_np)
    assert rc == 0, "a non-proof run needs no marker -> must not abort"


class _FakeExecRes:
    def __init__(self, rc: int, stdout: str = "", stderr: str = ""):
        self.return_code = rc
        self.stdout = stdout
        self.stderr = stderr


class _FakeEnv:
    def __init__(self, res: _FakeExecRes):
        self._res = res

    async def exec(self, command, timeout_sec=None):  # noqa: ARG002
        return self._res


def test_c1_assert_proof_marker_hardstops_when_absent(monkeypatch):
    """Host-side leg: after the agent runs, a CONFIRMED absent container marker in
    proof mode HARD-STOPS via _proof_marker_verdict/_adapter_fail. RED pre-fix:
    `_assert_proof_marker` did not exist and `_proof_marker_verdict` was dead."""
    monkeypatch.setattr(ga, "_GT_BASELINE", False)
    monkeypatch.setenv("GT_PROOF_MODE", "1")
    agent = ga.GTMiniSweAgent.__new__(ga.GTMiniSweAgent)

    absent = _FakeEnv(_FakeExecRes(1, ""))           # test -f -> non-zero -> absent
    with pytest.raises(ga.DeepSweAdapterError):
        asyncio.run(agent._assert_proof_marker(absent))

    present = _FakeEnv(_FakeExecRes(0, "GT_MARKER_PRESENT"))
    asyncio.run(agent._assert_proof_marker(present))  # marker present -> no raise


def test_c1_assert_proof_marker_noop_on_baseline_and_nonproof(monkeypatch):
    """The host-side leg must NOT fire on the baseline arm or outside proof mode
    (those need no marker) — never a false infra failure."""
    agent = ga.GTMiniSweAgent.__new__(ga.GTMiniSweAgent)
    absent = _FakeEnv(_FakeExecRes(1, ""))

    monkeypatch.setattr(ga, "_GT_BASELINE", True)
    monkeypatch.setenv("GT_PROOF_MODE", "1")
    asyncio.run(agent._assert_proof_marker(absent))   # baseline -> no raise

    monkeypatch.setattr(ga, "_GT_BASELINE", False)
    monkeypatch.setenv("GT_PROOF_MODE", "0")
    asyncio.run(agent._assert_proof_marker(absent))   # non-proof -> no raise
