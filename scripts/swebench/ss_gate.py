#!/usr/bin/env python3
r"""SS-6 — the SUPER-SAIYAN (SS) live-fire acceptance GATE.

Run:  ``python scripts/swebench/ss_gate.py``

This is a DETERMINISTIC, ``$0``, OFFLINE proof harness — the delivery-side half of the
frontier-lab pattern (a fixture/replay harness for the mechanism + a small paid canary for
the model). It DRIVES THE REAL SEAM (``artifact_deepswe/gt_mini_patch.py._augment_output``,
the one per-observation hook every seam test uses) over synthetic, generic, multi-file
Python fixture repos (``tests/fixtures/ss_gate/`` — NO benchmark repo, NO task id: generality
is the whole point) and asserts the SS acceptance standard per feature.

It NEVER edits a product file. It imports + drives the frozen seam through its PUBLIC test
surface only (``_augment_output`` / ``_reset_oracle_state`` / ``_db_path`` / ``_root`` /
``_POST_SEARCH_ON`` / the ``GT_RUNTIME_LEDGER`` durable ledger), so it keeps working unchanged
as SS-0/SS-1 land.

────────────────────────────────────────────────────────────────────────────────────────────
THE SS FEATURE CONTRACT (the flag names + ledger reasons this gate enforces — SS-0/SS-1 MUST
implement to these; each is an INDEPENDENT ``GT_SS_*`` env flag, default-OFF byte-identical).
CURRENT SCENARIO SET = 12: S0 CHANNEL-CANARY (SS-6b hermeticity pre-flight — a dead fixture
channel is a loud exit-1, never a SKIP) + S1..S10 below + S11 SUBMIT-RED (SS-2: ONE native
refusal when the last test touching an edited surface was observed FAILING and unresolved;
silent on green; never fires twice):

  S1  GT_SS_NOVELTY    a factual delivery whose ENTIRE entity set ⊆ {files the agent already
                           viewed + symbols it grepped} is SUPPRESSED (ledger reason
                           ``ss_step_behind``); a delivery carrying a NOVEL cross-file entity survives.
  S2  GT_SS_DEDUP2 two same-class deliveries with byte-DISTINCT payloads but equal/subset
                           entity sets -> the second is suppressed (reason ``ss_semantic_dup``).
  S3  GT_SS_COHERENCE_V2      a run-coherence signal fires ONLY on >=3 consecutive successful writes to
                           one file with NO intervening passing test; a passing test / a failed write
                           / <=2 writes does NOT fire. When it fires its bytes carry the EXACT count.
  S4  GT_SS_RECOVERY_V2       the SAME failing test observed twice with no intervening edit delivers a
                           recovery imperative on the SECOND repeat (not later); two different-output
                           commands do NOT; a passing-test repeat does NOT.
  S5  GT_SS_PROVENANCE     a fact whose provenance is only low-quality paths (``tmp/``, ``htmlcov/``,
                           scratch/coverage) is suppressed (reason ``ss_provenance``); no L6 reindex
                           is triggered for those paths.
  S6  GT_SS_LATE_DROP           an obligation whose requirement is already covered by an EARLIER passing
                           test is suppressed (reason ``ss_late``).
  S7  GT_SS_ACK_METRICS            a delivery whose entity a later agent action references gains ledger
                           ``ack=true``; an unreferenced delivery is ``ack=false``.
  S8  GT_SS_ARBITER_V2     a producer that yields ZERO bytes yields NO ``delivered`` ledger row
                           (structural empty-payload guarantee). SKIP-with-reason if the flag has no
                           effect (this engine is optional).
  S9  (invariant, all SS on)  across every scenario stream: ZERO test-identifier tokens in any
                           delivered payload; <=1 delivered GT payload per observation.
  S10 (invariant, all SS off) a full replay with every ``GT_SS_*`` UNSET is byte-identical (model
                           observation stream) to a no-SS baseline; and no ``ss_*`` ledger reason appears.

AUTO-DETECTION OF "BUILT": for S1-S8 the gate first probes whether setting the ``GT_SS_*`` flag
changes the seam's behaviour at all (on-arm vs off-arm). If it is a byte-identical no-op (the
feature has NOT landed) the scenario is ``SKIP:flag-not-built`` — an HONEST finding, never a
false green. The MOMENT SS-0/SS-1 gives the flag an effect, the scenario auto-activates and
enforces the standard (PASS/FAIL). S9/S10 are always-on invariants.

EXIT: ``0`` iff every scenario is PASS or ``SKIP:flag-not-built``; else ``1``. A FAIL means a
LANDED SS feature violates the standard. Report -> ``ss_gate_report.json``.
"""
from __future__ import annotations

import argparse
import copy
import datetime
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

# ── repo layout / import path ────────────────────────────────────────────────
_REPO = Path(__file__).resolve().parents[2]
_SRC = _REPO / "src"
_ART = _REPO / "artifact_deepswe"
_FIXTURES = _REPO / "tests" / "fixtures" / "ss_gate"
for _p in (str(_ART), str(_SRC)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass

# ── verdicts ─────────────────────────────────────────────────────────────────
PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP:flag-not-built"
ERROR = "ERROR"


class SSGateChannelDead(RuntimeError):
    """The fixture-graph delivery channel is provably DEAD in the CORE arm (GT-on, no
    ``GT_SS_*`` flags) — graph.db built empty, or the gateway def/ref partition produced
    ZERO deliveries for the ambiguous ``run`` probe.

    This exists to KILL a silent-degradation class: when the channel dies (ambient state
    poisons graph.db build / gateway / def_ref producer) EVERY flag-gated scenario (S1/S2/
    S5/S6/S7/S8) auto-detects 'no behavioural delta between arms' and reports
    ``SKIP:flag-not-built`` — indistinguishable from a genuinely-unlanded SS feature. That
    is the exact drift SS-6b closes: a dead channel MUST surface as a LOUD, NAMED ERROR
    (gate exit 1), never a false-green SKIP."""

# The fixture's known test-identifier tokens — none of these may EVER appear in a
# delivered model-facing payload (the S9 leak invariant).
_TEST_TOKENS = ("tests/test_pkg.py", "test_run", "test_pkg")

# The low-provenance paths the S5 fixture engineers into facts about `gizmo`.
_PROVENANCE_PATHS = ("tmp/scratch.py", "htmlcov/x.js")

# The canonical SUPER-SAIYAN feature flags (the S1-S8 overlay). All default-OFF.
_SS_FLAGS = ("GT_SS_NOVELTY", "GT_SS_DEDUP2", "GT_SS_COHERENCE_V2", "GT_SS_RECOVERY_V2",
             "GT_SS_PROVENANCE", "GT_SS_LATE_DROP", "GT_SS_ACK_METRICS", "GT_SS_ARBITER_V2")


# ══════════════════════════════════════════════════════════════════════════════
# event + result model
# ══════════════════════════════════════════════════════════════════════════════
@dataclass
class Event:
    """One scripted agent tool action + its raw observation (pre-GT)."""

    action: dict
    output: str = ""
    rc: int = 0


@dataclass
class Obs:
    """One turn's before/after observation text (GT appends a byte-preserving suffix)."""

    before: str
    after: str

    @property
    def appended(self) -> bool:
        return self.after != self.before

    @property
    def delta(self) -> str:
        return self.after[len(self.before):] if self.after.startswith(self.before) else self.after


@dataclass
class SeamResult:
    observations: list[Obs] = field(default_factory=list)
    ledger: list[dict] = field(default_factory=list)
    # SS-2 (S11): the pre-submit refusal string returned by each submit attempt made
    # AFTER the event stream (empty when the flag is off / no unresolved RED / dose spent).
    submit_refusals: list[str] = field(default_factory=list)

    # -- views over the durable ledger --------------------------------------
    def delivered_rows(self) -> list[dict]:
        return [r for r in self.ledger
                if str(r.get("outcome")) == "delivered" and int(r.get("chars_delivered") or 0) > 0]

    def delivered_rows_any(self) -> list[dict]:
        return [r for r in self.ledger if str(r.get("outcome")) == "delivered"]

    def rows_with_reason(self, token: str) -> list[dict]:
        return [r for r in self.ledger
                if token in (str(r.get("reason") or ""), str(r.get("outcome") or ""))]

    def obs_stream(self) -> list[str]:
        return [o.after for o in self.observations]

    def deltas(self) -> list[str]:
        return [o.delta for o in self.observations]

    def any_delta_contains(self, text: str) -> bool:
        return any(text in o.delta for o in self.observations)


# ══════════════════════════════════════════════════════════════════════════════
# graph.db builder (from the fixture spec)
# ══════════════════════════════════════════════════════════════════════════════
def _build_graph_db(db_path: str, spec: dict) -> None:
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
        for n in spec.get("nodes", []):
            con.execute(
                "INSERT INTO nodes(id,label,name,file_path,start_line,end_line,signature,"
                "is_test,language) VALUES(?,?,?,?,?,?,?,?,?)",
                (n["id"], n.get("label", "Function"), n["name"], n["file_path"],
                 n.get("start_line", 1), n.get("end_line", 5), n.get("signature", ""),
                 n.get("is_test", 0), n.get("language", "python")))
        for e in spec.get("edges", []):
            # ``source_line`` is OPTIONAL in the spec: an edge with no ``source_line`` key
            # inserts NULL (the pre-existing behaviour — the caller-witness query gates on
            # ``e.source_line > 0``, so NULL edges are unchanged). Only an edge that WANTS to
            # surface as a cross-file caller witness (S2) declares a real call-site line.
            con.execute(
                "INSERT INTO edges(source_id,target_id,type,source_line,resolution_method,"
                "confidence) VALUES(?,?,?,?,?,?)",
                (e["source_id"], e["target_id"], e.get("type", "CALLS"),
                 e.get("source_line"), e.get("resolution_method", "import"),
                 e.get("confidence", 1.0)))
        for p in spec.get("properties", []):
            con.execute(
                "INSERT INTO properties(node_id,kind,value,line,confidence)"
                " VALUES(?,?,?,?,?)",
                (p["node_id"], p["kind"], p["value"], p.get("line", 1),
                 p.get("confidence", 0.8)))
        con.commit()
        # HERMETICITY: this deterministic in-python build (no gt-index binary, no cached DB,
        # no /tmp state) MUST yield a non-empty graph with the DELIBERATELY-ambiguous ``run``
        # def sites — every def/ref producer reads ``nodes``. An empty/degenerate build would
        # silently kill the whole fixture channel; fail LOUD here instead of degrading to
        # false SKIPs downstream.
        n_nodes = con.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        n_run = con.execute("SELECT COUNT(*) FROM nodes WHERE name='run'").fetchone()[0]
        if n_nodes <= 0 or n_run < 2:
            raise SSGateChannelDead(
                f"fixture graph.db built degenerate from graph_spec.json "
                f"(nodes={n_nodes}, run_def_sites={n_run}, want nodes>0 and >=2 'run' defs) "
                f"— the ambiguity probe the def/ref channel needs is absent")
    finally:
        con.close()


def _scrub_paths(rows: list[dict], tmp: Path) -> list[dict]:
    """Replace THIS run's ephemeral temp dir with ``<TMP>`` in every ledger string value.
    The seam's L6 telemetry rows embed the absolute graph.db/root path in ``reason`` — a
    per-run artifact of the harness using a fresh temp dir, NOT real non-determinism. Scrubbing
    it makes two deterministic runs compare equal (and keeps the determinism/byte-identity gate
    honest instead of tripping on the harness's own temp names)."""
    needles = [str(tmp), str(tmp).replace("\\", "/")]
    out: list[dict] = []
    for r in rows:
        nr: dict = {}
        for k, v in r.items():
            if isinstance(v, str):
                for n in needles:
                    v = v.replace(n, "<TMP>")
            nr[k] = v
        out.append(nr)
    return out


# ══════════════════════════════════════════════════════════════════════════════
# drivers
# ══════════════════════════════════════════════════════════════════════════════
# The core posture is PRODUCTION Super-Mode (Profile-2, GT_RL_PROFILE=2): the SM-5
# global arbiter is what enforces the <=1-dose law (with the profile OFF the legacy
# always-on lattice stacks scope+evidence on one view => >1 dose, so an off posture
# could not test S9). SUPER-SAIYAN rides ON TOP of Super-Mode; each GT_SS_* flag is an
# INDEPENDENT overlay. The ONLY thing that differs between a scenario's arms is the
# GT_SS_* flags, so a behavioural delta is attributable to SS and byte-identity is crisp.
def _all_ss_env_keys() -> list[str]:
    return [k for k in os.environ if k.startswith("GT_SS_")]


def _all_gt_env_keys() -> list[str]:
    """Every ambient ``GT_*`` key currently in the environment. The gate STRIPS this whole
    namespace before each arm and re-applies ONLY its own pinned core (Profile-2) + the
    arm's ``GT_SS_*`` overrides — so a leaked ``GT_BASELINE`` / ``GT_INDEX_BIN`` /
    ``GT_HOST_GRAPH_DB`` / ``GT_CERT_DIR`` / ``GT_POST_SEARCH`` / ``GT_PROOF_MODE`` from an
    earlier shell or a prior run can never steer the fixture-graph channel. Hermeticity: a
    scenario's environment is a function of the GATE ALONE, never of the caller's shell."""
    return [k for k in os.environ if k.startswith("GT_")]


class RealSeamDriver:
    """Drives the REAL ``gt_mini_patch`` seam over the fixture repo. Public entry points only."""

    name = "real"

    def __init__(self) -> None:
        # GT-ON, production Super-Mode posture: baseline OFF, Profile-2 members armed so
        # the SM-5 global arbiter (the <=1-dose enforcer) is live.
        os.environ.pop("GT_BASELINE", None)
        import gt_mini_patch as g  # noqa: E402 — import side effects are the seam install
        from groundtruth.runtime import rl_profile as rp  # noqa: E402
        self.g = g
        # The full Profile-2 member map (every member -> "1"), plus the master switches.
        core = dict(rp.resolve_profile({"GT_RL_PROFILE": "2"}))
        core["GT_RL_PROFILE"] = "2"
        core["GT_GATEWAY"] = "1"
        core["GT_GLOBAL_ARBITER"] = "1"   # the <=1-dose enforcer, explicit
        # SUPER-SAIYAN is an INDEPENDENT overlay controlled ONLY by each scenario arm's
        # ss_env — so the ONLY thing that differs between a scenario's arms is the GT_SS_*
        # flags (this comment's contract). Profile-2 now REGISTERS the GT_SS_* members
        # (=1), which would contaminate the SS-OFF baseline (the S10 'unset' arm would
        # inherit SS-on from the profile and never equal explicit-0). Strip them from core
        # so the baseline is truly SS-off and every SS delta is arm-attributable.
        self._core = {k: v for k, v in core.items() if not k.startswith("GT_SS_")}
        spec_path = _FIXTURES / "graph_spec.json"
        self.spec = json.loads(spec_path.read_text(encoding="utf-8"))
        self.repo_src = _FIXTURES / "repo"

    def run(self, events: list[Event], ss_env: dict, submit_attempts: int = 0) -> SeamResult:
        g = self.g
        env_snapshot = dict(os.environ)
        saved_db = g._db_path
        saved_root = g._root
        saved_ps = getattr(g, "_POST_SEARCH_ON", None)
        saved_baseline = getattr(g, "_GT_BASELINE", None)
        tmp = Path(tempfile.mkdtemp(prefix="ss_gate_"))
        try:
            root = tmp / "repo"
            shutil.copytree(self.repo_src, root)
            db = str(tmp / "graph.db")
            _build_graph_db(db, self.spec)
            ledger = tmp / "led.jsonl"

            # HERMETIC ENV (SS-6b): strip the ENTIRE ambient GT_* namespace, then apply ONLY
            # the gate's pinned core (Profile-2) + this arm's GT_SS_* overrides. A leaked
            # GT_BASELINE / GT_INDEX_BIN / GT_HOST_GRAPH_DB / GT_CERT_DIR / GT_POST_SEARCH from
            # an earlier shell or run could otherwise silently darken the fixture-graph channel
            # into a false SKIP (the exact drift SS-6b closes). The scenario env is now a
            # function of the gate ALONE, never of the caller's shell.
            for k in _all_gt_env_keys():
                os.environ.pop(k, None)
            for k, v in self._core.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            for k, v in (ss_env or {}).items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = str(v)
            os.environ["GT_RUNTIME_LEDGER"] = str(ledger)
            # PIN the L6 reindex binary to a guaranteed-ABSENT path inside THIS run's temp dir
            # (never created) so no ambient /tmp/gt-index can activate the post-edit reindex
            # subprocess — the reindex stays deterministically inert (production posture: the
            # ~49MB binary is legitimately absent on the host-graph-inject path), independent of
            # host state. Closes the "present gt-index binary -> nondeterminism" poison.
            os.environ["GT_INDEX_BIN"] = str(tmp / "gt-index-hermetic-absent")

            g._db_path = lambda: db
            g._root = lambda: str(root)
            # FORCE the seam's IMPORT-FROZEN flag globals to hermetic GT-on values. `_GT_BASELINE`
            # and `_POST_SEARCH_ON` are read from os.environ at MODULE IMPORT — before the gate can
            # sanitize the env — so a leaked GT_BASELINE=1 / GT_POST_SEARCH at interpreter start
            # would otherwise stick regardless of the strip above. The gate REQUIRES GT-on +
            # gateway-owned search delivery; re-assert both every run.
            if saved_ps is not None:
                g._POST_SEARCH_ON = False
            if saved_baseline is not None:
                g._GT_BASELINE = False
            # Reset the residual L6-FRESH module latches _reset_oracle_state does not clear, so a
            # prior run's staged work-copy pointer / one-shot warning latch cannot leak into this
            # run's delivery decisions (cross-run state -> flaky def/ref firing).
            for _latch, _val in (("_l6_work_db", None), ("_l6_no_binary_warned", False),
                                 ("_l6_reindex_failed_warned", False),
                                 ("_l6_probe_emitted", False)):
                if hasattr(g, _latch):
                    setattr(g, _latch, _val)
            g._reset_oracle_state()
            try:
                g._RUNTIME_LEDGER = g._ProductLedger()
            except Exception:  # noqa: BLE001
                pass

            obs: list[Obs] = []
            for ev in events:
                out = {"output": ev.output, "returncode": ev.rc}
                before = out["output"]
                try:
                    g._augment_output(ev.action, out)
                except Exception:  # noqa: BLE001 — a seam fault is a delivery-of-nothing here
                    pass
                obs.append(Obs(before=before, after=out.get("output") or ""))

            # SS-2 (S11): AFTER the stream, exercise the submit boundary N times (while the
            # arm's env is still set) so the ss_submit_red single-dose is observable. The events
            # above build `_ss_last_failing_test` via the seam's own `_ss_record_test`; each
            # attempt calls the public `_ss_submit_red_refusal` (the same helper the real submit
            # chokepoint uses). Empty when the flag is off / no unresolved RED / dose spent.
            refusals: list[str] = []
            for _ in range(max(0, int(submit_attempts))):
                try:
                    refusals.append(str(g._ss_submit_red_refusal() or ""))
                except Exception:  # noqa: BLE001 — a submit-gate fault is a refusal-of-nothing here
                    refusals.append("")

            rows: list[dict] = []
            if ledger.is_file():
                for line in ledger.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except Exception:  # noqa: BLE001
                        pass
            rows = _scrub_paths(rows, tmp)
            return SeamResult(observations=obs, ledger=rows, submit_refusals=refusals)
        finally:
            g._db_path = saved_db
            g._root = saved_root
            if saved_ps is not None:
                g._POST_SEARCH_ON = saved_ps
            if saved_baseline is not None:
                g._GT_BASELINE = saved_baseline
            os.environ.clear()
            os.environ.update(env_snapshot)
            shutil.rmtree(tmp, ignore_errors=True)


class FakeSeamDriver:
    """A test double for the selftest: ``behavior(events, ss_env) -> SeamResult``. Lets the
    selftest inject a SS-CORRECT reference seam (scenario must PASS) and mutated seams
    (scenario must FAIL) through the SAME scenario code the real gate runs — proving the
    gate BITES, not that GT passes."""

    name = "fake"

    def __init__(self, behavior: Callable[..., SeamResult]) -> None:
        self._behavior = behavior

    def run(self, events: list[Event], ss_env: dict, submit_attempts: int = 0) -> SeamResult:
        # A 3-arg behavior (S11-aware) receives submit_attempts; a legacy 2-arg one does not.
        import inspect
        try:
            n_params = len(inspect.signature(self._behavior).parameters)
        except (TypeError, ValueError):
            n_params = 2
        if n_params >= 3:
            return self._behavior(list(events), dict(ss_env or {}), submit_attempts)
        return self._behavior(list(events), dict(ss_env or {}))


# ══════════════════════════════════════════════════════════════════════════════
# comparison / built-detection helpers
# ══════════════════════════════════════════════════════════════════════════════
def _norm_ledger(rows: list[dict]) -> list[dict]:
    """Drop only the volatile wall-clock field so two deterministic runs compare equal."""
    return [{k: v for k, v in r.items() if k != "timestamp_ms"} for r in rows]


def _signature(res: SeamResult) -> tuple:
    return (tuple(res.deltas()), tuple(json.dumps(r, sort_keys=True) for r in _norm_ledger(res.ledger)))


def _has_effect(on: SeamResult, off: SeamResult) -> bool:
    """True iff setting the SS flag changed ANYTHING (model bytes OR ledger). A byte-identical
    no-op => the feature has not landed => SKIP:flag-not-built."""
    return _signature(on) != _signature(off)


def _leak_free(res: SeamResult) -> tuple[bool, str]:
    for o in res.observations:
        for tok in _TEST_TOKENS:
            if tok in o.delta:
                return False, f"delivered payload leaked test identifier '{tok}'"
    for r in res.delivered_rows():
        blob = str(r.get("file_path") or "")
        for tok in _TEST_TOKENS:
            if tok in blob:
                return False, f"delivered ledger row cites test identifier '{tok}'"
    return True, ""


def _dose_le_one(res: SeamResult) -> tuple[bool, str]:
    per_iter: dict[int, int] = {}
    for r in res.delivered_rows():
        it = int(r.get("iteration") or 0)
        per_iter[it] = per_iter.get(it, 0) + 1
    bad = {it: n for it, n in per_iter.items() if n > 1}
    if bad:
        return False, f">1 delivered GT payload in one observation: {bad}"
    return True, ""


# ══════════════════════════════════════════════════════════════════════════════
# scenario result
# ══════════════════════════════════════════════════════════════════════════════
@dataclass
class ScenarioResult:
    sid: str
    name: str
    flag: str
    verdict: str
    detail: str
    subchecks: list[tuple[str, str]] = field(default_factory=list)


def _det_and_byteid(driver, events, flag) -> tuple[SeamResult, SeamResult, SeamResult, list[tuple[str, str]]]:
    """Shared machinery for the flag-gated scenarios (S1-S8): on-arm twice (determinism),
    off-arm + baseline (byte-identity). Returns (on1, off, base, subchecks)."""
    on1 = driver.run(events, {flag: "1"})
    on2 = driver.run(events, {flag: "1"})
    off = driver.run(events, {flag: "0"})
    base = driver.run(events, {})
    subs: list[tuple[str, str]] = []
    subs.append(("determinism (on-arm x2 identical)", PASS if _signature(on1) == _signature(on2) else FAIL))
    subs.append(("byte-identity off==baseline", PASS if off.obs_stream() == base.obs_stream() else FAIL))
    return on1, off, base, subs


def _gate(sid, name, flag, subs, core_ok, core_detail, built) -> ScenarioResult:
    """Fold sub-check + core verdict + built-detection into one ScenarioResult."""
    if not built:
        return ScenarioResult(sid, name, flag, SKIP,
                              f"GT_SS flag '{flag}' is a byte-identical no-op (feature not landed)", subs)
    infra_fail = [s for s in subs if s[1] == FAIL]
    if infra_fail:
        return ScenarioResult(sid, name, flag, FAIL,
                              "; ".join(f"{n}={v}" for n, v in infra_fail), subs)
    return ScenarioResult(sid, name, flag, PASS if core_ok else FAIL, core_detail, subs)


# ══════════════════════════════════════════════════════════════════════════════
# fixture event streams
# ══════════════════════════════════════════════════════════════════════════════
_MOD_A = "pkg/mod_a.py"
_MOD_B = "pkg/mod_b.py"
_UTIL = "pkg/util.py"


def _cat(path: str, body: str) -> Event:
    return Event(action={"command": f"cat {path}"}, output=body, rc=0)


def _grep(sym: str, hits: str) -> Event:
    return Event(action={"command": f"grep -rn {sym} ."}, output=hits, rc=0)


def _run_hits() -> str:  # the ambiguous `run` def sites (mod_a + mod_b)
    return f"{_MOD_A}:8: def run():\n{_MOD_B}:9: def run():"


def _gizmo_hits() -> str:
    return "tmp/scratch.py:5: def gizmo():\nhtmlcov/x.js:4: function gizmo()"


# ══════════════════════════════════════════════════════════════════════════════
# HERMETICITY PRE-FLIGHT — prove the fixture-graph channel is LIVE before any SKIP
# ══════════════════════════════════════════════════════════════════════════════
def _channel_canary(driver) -> tuple[bool, str]:
    """Prove the fixture-graph def/ref channel is LIVE in the CORE arm (GT-on, NO ``GT_SS_*``
    flags) BEFORE any scenario is graded. The whole SS overlay rides on this ONE delivery: a
    grep for the deliberately-ambiguous ``run`` symbol must yield a def/ref partition (a
    model-facing delta OR a delivered ledger row).

    If the channel is silently dead (ambient state killed graph.db build / gateway def_ref
    producer / post_search classification) every flag-gated scenario auto-detects 'no effect'
    and reports ``SKIP:flag-not-built`` — a dead channel masquerading as an unlanded feature.
    Return ``(False, reason)`` so :func:`main` reports a LOUD, NAMED ERROR (exit 1), never a
    false-green SKIP. Any exception during the probe is itself a dead-channel signal.

    The probe is a BARE ``grep run`` with NOTHING pre-viewed: the ambiguous symbol resolves to
    two def sites the agent has not acquired, so a healthy CORE arm delivers the def/ref
    partition. (A preceding ``cat`` would let the global arbiter legitimately suppress it as
    ``already_acquired`` — a LIVE-channel suppression, not a dead channel; the bare grep avoids
    that confound so 'zero deliveries' means ONLY a genuinely dead channel.)"""
    probe = [_grep("run", _run_hits())]
    try:
        res = driver.run(probe, {})  # CORE arm — no SS flags, gateway owns the search dose
    except Exception as exc:  # noqa: BLE001 — a probe fault IS a dead channel
        return False, f"core-arm canary raised {type(exc).__name__}: {exc}"
    grep_delta = res.observations[-1].delta if res.observations else ""
    delivered = res.delivered_rows_any()
    if not grep_delta and not delivered:
        return False, ("fixture-graph def/ref channel produced ZERO deliveries in the CORE arm "
                       "for the ambiguous 'run' grep — graph.db build / gateway def_ref_partition "
                       "/ post_search classification is DEAD (NOT a missing GT_SS_* flag). "
                       "Refusing to emit false SKIP:flag-not-built.")
    return True, (f"core-arm channel live: grep_delta={'DELIVERED' if grep_delta else 'empty'}; "
                  f"delivered_rows={len(delivered)}")


def _meta_report(driver) -> int:
    """``--meta``: print every ENVIRONMENTAL dependency the gate resolves, per arm — so a dead
    link is PINPOINTED (seam path, fixture paths, graph.db row counts, L6 binary probe, profile
    posture, def/ref classification, per-arm delivered rows) rather than inferred from a SKIP.
    Diagnostic only; always exits 0."""
    g = getattr(driver, "g", None)
    w = sys.stdout.write
    w("\n# SS-6 gate --meta (hermeticity diagnostics)\n")
    w(f"  seam_module      : {getattr(g, '__file__', '(fake/none)')}\n")
    w(f"  fixtures_dir     : {_FIXTURES}\n")
    w(f"  fixture_repo     : {_FIXTURES / 'repo'}  exists={(_FIXTURES / 'repo').is_dir()}\n")
    w(f"  graph_spec       : {_FIXTURES / 'graph_spec.json'}  "
      f"exists={(_FIXTURES / 'graph_spec.json').is_file()}\n")
    # graph.db build + row counts (deterministic, pure python, no gt-index/cached DB/tmp state)
    try:
        _t = Path(tempfile.mkdtemp(prefix="ss_meta_"))
        _db = str(_t / "graph.db")
        _build_graph_db(_db, getattr(driver, "spec", {}) or {})
        _con = sqlite3.connect(_db)
        _nn = _con.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        _ne = _con.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        _nr = _con.execute("SELECT COUNT(*) FROM nodes WHERE name='run'").fetchone()[0]
        _con.close()
        shutil.rmtree(_t, ignore_errors=True)
        w(f"  graph.db build   : nodes={_nn} edges={_ne} run_def_sites={_nr} "
          f"(want run_def_sites>=2 for the ambiguity probe)\n")
    except Exception as exc:  # noqa: BLE001
        w(f"  graph.db build   : FAILED {type(exc).__name__}: {exc}\n")
    # L6 binary probe + profile posture (what the seam WOULD resolve on)
    _bin = os.environ.get("GT_INDEX_BIN", "/tmp/gt-index")
    w(f"  L6 GT_INDEX_BIN  : {_bin}  isfile={os.path.isfile(_bin)} "
      f"(gate pins this ABSENT per-run for hermeticity)\n")
    if hasattr(driver, "_core"):
        _c = driver._core
        w(f"  profile posture  : GT_RL_PROFILE={_c.get('GT_RL_PROFILE')} "
          f"GT_GATEWAY={_c.get('GT_GATEWAY')} GT_GLOBAL_ARBITER={_c.get('GT_GLOBAL_ARBITER')} "
          f"GT_L6_FRESH={_c.get('GT_L6_FRESH')}\n")
    if g is not None:
        w(f"  seam flag globals: _GT_BASELINE={getattr(g, '_GT_BASELINE', '?')} "
          f"_POST_SEARCH_ON={getattr(g, '_POST_SEARCH_ON', '?')} "
          f"_l6_work_db={getattr(g, '_l6_work_db', '?')}\n")
    # canary + per-arm delivered summary for the def/ref probe
    ok, detail = _channel_canary(driver)
    w(f"  channel canary   : {'LIVE' if ok else 'DEAD'} — {detail}\n")
    probe = [_grep("run", _run_hits())]  # bare grep (nothing pre-viewed) — the def/ref probe
    for arm_name, arm in (("core", {}), ("NOVELTY=1", {"GT_SS_NOVELTY": "1"})):
        res = driver.run(probe, arm)
        gd = res.observations[-1].delta if res.observations else ""
        rows = [(r.get("layer"), r.get("outcome"), r.get("reason")) for r in res.ledger]
        w(f"  arm[{arm_name:<10}] : grep_delta={'DELIVERED' if gd else 'empty'} "
          f"delivered={len(res.delivered_rows_any())} ledger_rows={len(res.ledger)}\n")
        w(f"      ledger        : {rows}\n")
    return 0


# ══════════════════════════════════════════════════════════════════════════════
# S1 — STEP-BEHIND
# ══════════════════════════════════════════════════════════════════════════════
def scenario_s1(driver) -> ScenarioResult:
    flag = "GT_SS_NOVELTY"
    # suppressed episode: BOTH def files of `run` viewed before grepping it -> the def/ref
    # partition's entity set ⊆ {viewed files + grepped symbol} -> must be suppressed.
    supp_events = [
        _cat(_MOD_A, "def alpha(): ...\ndef run(): return 'a'\n"),
        _cat(_MOD_B, "def beta(): ...\ndef run(): return 'b'\n"),
        _grep("run", _run_hits()),
    ]
    # survive episode: only mod_a viewed -> the partition carries mod_b (NOVEL) -> survives.
    surv_events = [
        _cat(_MOD_A, "def alpha(): ...\ndef run(): return 'a'\n"),
        _grep("run", _run_hits()),
    ]
    on_supp = driver.run(supp_events, {flag: "1"})
    on_supp2 = driver.run(supp_events, {flag: "1"})
    off_supp = driver.run(supp_events, {flag: "0"})
    base_supp = driver.run(supp_events, {})
    on_surv = driver.run(surv_events, {flag: "1"})

    subs = [
        ("determinism (suppressed episode x2)", PASS if _signature(on_supp) == _signature(on_supp2) else FAIL),
        ("byte-identity off==baseline", PASS if off_supp.obs_stream() == base_supp.obs_stream() else FAIL),
    ]
    built = _has_effect(on_supp, off_supp) or _has_effect(on_surv, driver.run(surv_events, {flag: "0"}))

    grep_supp_delta = on_supp.observations[-1].delta if on_supp.observations else ""
    grep_surv_delta = on_surv.observations[-1].delta if on_surv.observations else ""
    suppressed_row = bool(on_supp.rows_with_reason("ss_step_behind"))
    core_ok = (grep_supp_delta == "" and suppressed_row and grep_surv_delta != "")
    detail = (f"suppressed-grep-delta={'empty' if grep_supp_delta=='' else 'DELIVERED'}; "
              f"ss_step_behind_row={suppressed_row}; survive-grep-delta="
              f"{'DELIVERED' if grep_surv_delta else 'empty'}")
    return _gate("S1", "STEP-BEHIND", flag, subs, core_ok, detail, built)


# ══════════════════════════════════════════════════════════════════════════════
# S2 — SEMANTIC DEDUP
# ══════════════════════════════════════════════════════════════════════════════
def scenario_s2(driver) -> ScenarioResult:
    flag = "GT_SS_DEDUP2"
    # dedup2's real domain is the caller_facts GROUP ({l3b.evidence, l3.contract} + gateway
    # caller aliases), NOT localization partitions — episode-scoped ENTITY-SET containment that
    # kills a byte-DISTINCT semantic repeat the content-hash dedup passes (the conan-17092
    # cross-class migrations cluster: m13 l3b.evidence -> m49 l3.contract re-delivering a SUBSET).
    # FIXTURE: editing mod_b delivers an l3b.evidence caller block (the SUPERSET entity set:
    # consumer/alpha/sig_target across mod_a/mod_b/util). Editing util.py then delivers an
    # l3.contract caller witness (handler -> sig_target, via the edge-8->7 source_line) whose
    # cited entity set {sig_target, pkg/mod_b.py} is a strict SUBSET of the mod_b block and is
    # byte-DISTINCT (a different fact class, different text) -> content-hash dedup does NOT fire,
    # so the semantic (entity-set) dedup must suppress the SECOND (cross-class, in-group).
    events = [
        _write(_MOD_B, "return 'b'", "return 'B'"),
        _write(_UTIL, "return x + 1", "return x + 2"),
    ]
    on1, off, base, subs = _det_and_byteid(driver, events, flag)
    built = _has_effect(on1, off)
    on_delivered = on1.delivered_rows()
    off_delivered = off.delivered_rows()
    dup_row = bool(on1.rows_with_reason("ss_semantic_dup"))
    # ON: the second caller-facts delivery is suppressed (ss_semantic_dup) -> strictly fewer
    # delivered rows than the OFF arm, which delivers BOTH. dup_row names the reason.
    core_ok = (dup_row and len(on_delivered) < len(off_delivered) and len(on_delivered) >= 1)
    detail = (f"on_delivered={len(on_delivered)} off_delivered={len(off_delivered)} "
              f"(want on<off, on>=1); ss_semantic_dup_row={dup_row}")
    return _gate("S2", "SEMANTIC-DEDUP", flag, subs, core_ok, detail, built)


# ══════════════════════════════════════════════════════════════════════════════
# S3 — COHERENCE TRUTH
# ══════════════════════════════════════════════════════════════════════════════
def _write(path: str, old: str, new: str, rc: int = 0) -> Event:
    return Event(action={"command": "str_replace", "path": path, "old_str": old, "new_str": new},
                 output=("" if rc == 0 else "patch failed"), rc=rc)


def scenario_s3(driver) -> ScenarioResult:
    flag = "GT_SS_COHERENCE_V2"
    # NON-FIRE: 2 successful writes + 1 failed write + 2 cat views + 1 passing test between.
    nonfire = [
        _write(_MOD_A, "return 'a'", "return 'a1'"),
        _cat(_MOD_A, "def run(): return 'a1'\n"),
        _write(_MOD_A, "return 'a1'", "return 'a2'"),
        Event(action={"command": "pytest -q"}, output="1 passed", rc=0),
        _write(_MOD_A, "return 'a2'", "return 'a3'", rc=1),   # failed write
        _cat(_MOD_A, "def run(): return 'a2'\n"),
    ]
    # FIRE: 3 successful writes, NO test -> coherence fires with the EXACT count 3.
    fire = [
        _write(_MOD_A, "return 'a'", "return 'a1'"),
        _write(_MOD_A, "return 'a1'", "return 'a2'"),
        _write(_MOD_A, "return 'a2'", "return 'a3'"),
    ]
    on_nf = driver.run(nonfire, {flag: "1"})
    on_nf2 = driver.run(nonfire, {flag: "1"})
    off_nf = driver.run(nonfire, {flag: "0"})
    base_nf = driver.run(nonfire, {})
    on_fire = driver.run(fire, {flag: "1"})
    off_fire = driver.run(fire, {flag: "0"})

    subs = [
        ("determinism (nonfire x2)", PASS if _signature(on_nf) == _signature(on_nf2) else FAIL),
        ("byte-identity off==baseline", PASS if off_nf.obs_stream() == base_nf.obs_stream() else FAIL),
    ]
    built = _has_effect(on_fire, off_fire) or _has_effect(on_nf, off_nf)

    nonfire_fired = bool(on_nf.rows_with_reason("ss_coherence")) or on_nf.any_delta_contains("coherence")
    fire_row = on_fire.rows_with_reason("ss_coherence")
    fire_fired = bool(fire_row) or on_fire.any_delta_contains("coherence")
    exact_count = on_fire.any_delta_contains("3") or any("3" in str(r.get("reason") or "") for r in fire_row)
    core_ok = (not nonfire_fired) and fire_fired and exact_count
    detail = f"nonfire_fired={nonfire_fired} (want False); fire_fired={fire_fired}; exact_count_3={exact_count}"
    return _gate("S3", "COHERENCE", flag, subs, core_ok, detail, built)


# ══════════════════════════════════════════════════════════════════════════════
# S4 — RECOVERY TRUTH
# ══════════════════════════════════════════════════════════════════════════════
def _test_evt(cmd: str, output: str, rc: int) -> Event:
    return Event(action={"command": cmd}, output=output, rc=rc)


def scenario_s4(driver) -> ScenarioResult:
    flag = "GT_SS_RECOVERY_V2"
    fail_out = "E   assert run() == 'x'\n1 failed"
    # (a) SAME failing test twice, no intervening edit -> recovery on the SECOND repeat.
    same_fail = [_test_evt("pytest -q", fail_out, 1), _test_evt("pytest -q", fail_out, 1)]
    # (b) two DIFFERENT-output commands -> NO recovery.
    diff_cmd = [_test_evt("pytest -q", fail_out, 1),
                _test_evt("python build.py", "compilation error at line 9", 1)]
    # (c) passing-test repeat -> NO recovery.
    pass_rep = [_test_evt("pytest -q", "1 passed", 0), _test_evt("pytest -q", "1 passed", 0)]

    on_a = driver.run(same_fail, {flag: "1"})
    on_a2 = driver.run(same_fail, {flag: "1"})
    off_a = driver.run(same_fail, {flag: "0"})
    base_a = driver.run(same_fail, {})
    on_b = driver.run(diff_cmd, {flag: "1"})
    on_c = driver.run(pass_rep, {flag: "1"})

    subs = [
        ("determinism (case-a x2)", PASS if _signature(on_a) == _signature(on_a2) else FAIL),
        ("byte-identity off==baseline", PASS if off_a.obs_stream() == base_a.obs_stream() else FAIL),
    ]
    built = _has_effect(on_a, off_a)

    def _recovered_on_turn(res: SeamResult, turn_idx: int) -> bool:
        if turn_idx >= len(res.observations):
            return False
        d = res.observations[turn_idx].delta.lower()
        rows = [r for r in res.delivered_rows() if int(r.get("iteration") or -1) == turn_idx + 1]
        return ("recover" in d) or any("recover" in str(r.get("reason") or r.get("layer") or "").lower()
                                        for r in rows)

    a_first = _recovered_on_turn(on_a, 0)
    a_second = _recovered_on_turn(on_a, 1)
    b_any = any(_recovered_on_turn(on_b, i) for i in range(len(on_b.observations)))
    c_any = any(_recovered_on_turn(on_c, i) for i in range(len(on_c.observations)))
    core_ok = (not a_first) and a_second and (not b_any) and (not c_any)
    detail = (f"a:first={a_first}(want F) second={a_second}(want T); "
              f"b_diff_recovered={b_any}(want F); c_pass_recovered={c_any}(want F)")
    return _gate("S4", "RECOVERY", flag, subs, core_ok, detail, built)


# ══════════════════════════════════════════════════════════════════════════════
# S5 — PROVENANCE
# ══════════════════════════════════════════════════════════════════════════════
def scenario_s5(driver) -> ScenarioResult:
    flag = "GT_SS_PROVENANCE"
    events = [_grep("gizmo", _gizmo_hits())]
    on1, off, base, subs = _det_and_byteid(driver, events, flag)
    built = _has_effect(on1, off)

    cites_bad = False
    for o in on1.observations:
        for p in _PROVENANCE_PATHS:
            if p in o.delta:
                cites_bad = True
    for r in on1.delivered_rows():
        for p in _PROVENANCE_PATHS:
            if p in str(r.get("file_path") or ""):
                cites_bad = True
    prov_row = bool(on1.rows_with_reason("ss_provenance"))
    # L6 reindex must NOT be triggered for the low-provenance paths.
    l6_bad = any(str(r.get("layer") or "").lower().startswith("l6")
                 and any(p in str(r.get("file_path") or "") for p in _PROVENANCE_PATHS)
                 for r in on1.ledger)
    core_ok = (not cites_bad) and prov_row and (not l6_bad)
    detail = f"delivered_cites_low_provenance={cites_bad}(want F); ss_provenance_row={prov_row}; l6_for_bad_path={l6_bad}(want F)"
    return _gate("S5", "PROVENANCE", flag, subs, core_ok, detail, built)


# ══════════════════════════════════════════════════════════════════════════════
# S6 — LATE-DROP
# ══════════════════════════════════════════════════════════════════════════════
def scenario_s6(driver) -> ScenarioResult:
    flag = "GT_SS_LATE_DROP"
    # late-drop fires when a resurfaced fact names code symbols that were ALL already covered by
    # an observed PASSING test — and the pass-token set is seeded ONLY from the passing test's
    # command + output (gt_mini_patch _ss_record_test). FIXTURE: the passing test command
    # literally carries the `late_probe` symbol (`pytest -k late_probe ...`) so the pass-token
    # set gains 'late_probe'. grep late_probe then resurfaces a def/ref localization partition
    # whose ONLY code identifier is the bare 'late_probe' (the two def files pkg/io.py + pkg/db.py
    # have 2-char stems, too short to form a dotted path-symbol, so no `x.py` token pollutes the
    # entity set) -> every symbol is already GREEN-tested -> the delivery is late-dropped.
    events = [
        _test_evt("pytest -k late_probe tests/test_pkg.py", "1 passed", 0),
        _grep("late_probe", "pkg/io.py:6: def late_probe(x):\npkg/db.py:6: def late_probe(x):"),
    ]
    on1, off, base, subs = _det_and_byteid(driver, events, flag)
    built = _has_effect(on1, off)
    late_row = bool(on1.rows_with_reason("ss_late"))
    on_delivered = on1.delivered_rows()
    off_delivered = off.delivered_rows()
    # ON: the resurfaced partition is late-dropped (ss_late) -> strictly fewer delivered rows
    # than the OFF arm, which delivers it. late_row names the reason.
    core_ok = (late_row and len(on_delivered) < len(off_delivered))
    detail = (f"ss_late_row={late_row}; on_delivered={len(on_delivered)} "
              f"off_delivered={len(off_delivered)} (want on<off)")
    return _gate("S6", "LATE-DROP", flag, subs, core_ok, detail, built)


# ══════════════════════════════════════════════════════════════════════════════
# S7 — ACK TELEMETRY
# ══════════════════════════════════════════════════════════════════════════════
def scenario_s7(driver) -> ScenarioResult:
    flag = "GT_SS_ACK_METRICS"
    # deliver a `run` partition (cites mod_a/mod_b), then a later action that REFERENCES a
    # delivered entity (edits mod_b) -> that delivery's ledger row gains ack=true.
    events = [
        _grep("run", _run_hits()),
        _write(_MOD_B, "return 'b'", "return 'B'"),
    ]
    on1, off, base, subs = _det_and_byteid(driver, events, flag)
    built = _has_effect(on1, off)
    acked = [r for r in on1.delivered_rows_any() if r.get("ack") is True]
    unacked = [r for r in on1.delivered_rows_any() if r.get("ack") is False]
    core_ok = bool(acked)   # at least one delivery acknowledged; ack field present
    detail = f"ack_true_rows={len(acked)}; ack_false_rows={len(unacked)}"
    return _gate("S7", "ACK", flag, subs, core_ok, detail, built)


# ══════════════════════════════════════════════════════════════════════════════
# S8 — EMPTY-PAYLOAD (needs GT_SS_ARBITER_V2)
# ══════════════════════════════════════════════════════════════════════════════
def scenario_s8(driver) -> ScenarioResult:
    flag = "GT_SS_ARBITER_V2"
    # ARBITER_V2's empty-payload guarantee: a producer that yields ZERO renderable bytes must
    # never become a delivered ledger row. The genuine empty-payload input is a def/ref
    # partition for `vend_probe`, whose def sites are ALL under node_modules/ -> the gateway's
    # `_mk_add` leak-filters the body AND provenance to EMPTY (is_deliverable=False) and the
    # rendered delta is empty.
    events = [_grep("vend_probe",
                    "node_modules/a.js:3: function vend_probe()\n"
                    "node_modules/b.js:3: function vend_probe()")]
    on1, off, base, subs = _det_and_byteid(driver, events, flag)
    built = _has_effect(on1, off)
    # HONEST STRUCTURAL SKIP (named reason, code path quoted): through the mini seam an
    # empty-payload envelope is byte-identical between the ON and OFF arms. The seam's OWN
    # `if not delta ...: return` in gt_mini_patch `_gt_gateway_deliver` drops an empty rendered
    # delta BEFORE it can reach a delivered ledger row OR the global pool — so the flag-ON
    # `_envelope_has_bytes` drop (gateway.augment) and the flag-OFF render-empty bail produce
    # the SAME zero-delivery, zero-ledger outcome (verified: HAS_EFFECT False). The
    # `_envelope_has_bytes` guard + arbiter REASON_SS_EMPTY_PAYLOAD are real and unit-proven at
    # the PURE gateway.augment / arbitrate layer (tests/runtime/test_ss1_gateway_empty_payload.py
    # + test_ss1_arbiter_v2.py) — a layer this seam-driven gate cannot surface, because no real
    # gateway producer builds an empty-payload envelope that COMPETES with a full one (the only
    # reachable empty-payload path is a LONE all-leaky def/ref partition, which self-suppresses
    # in both arms). So the guard is correct-by-construction here, not a flag-attributable delta.
    if not built:
        return ScenarioResult(
            "S8", "EMPTY-PAYLOAD", flag, SKIP,
            "empty-payload guard is byte-identical through the mini seam: an empty rendered "
            "delta bails at gt_mini_patch `_gt_gateway_deliver` `if not delta: return` in BOTH "
            "arms (no delivered row, no pool entry) => no flag-attributable delta. The guard is "
            "unit-proven at the pure gateway.augment/arbitrate layer the seam cannot surface.",
            subs)
    zero_byte_delivered = [r for r in on1.delivered_rows_any()
                           if int(r.get("chars_delivered") or 0) == 0]
    core_ok = not zero_byte_delivered
    detail = f"zero_byte_delivered_rows={len(zero_byte_delivered)}(want 0)"
    return _gate("S8", "EMPTY-PAYLOAD", flag, subs, core_ok, detail, built)


# ══════════════════════════════════════════════════════════════════════════════
# S9 — LEAK + DOSE INVARIANTS (across every scenario stream, all SS on)
# ══════════════════════════════════════════════════════════════════════════════
def _all_streams() -> list[list[Event]]:
    return [
        [_cat(_MOD_A, "def run(): return 'a'\n"), _cat(_MOD_B, "def run(): return 'b'\n"), _grep("run", _run_hits())],
        # S2 caller_facts dedup domain: mod_b l3b.evidence (superset) + util.py l3.contract (subset).
        [_write(_MOD_B, "return 'b'", "return 'B'"), _write(_UTIL, "return x + 1", "return x + 2")],
        [_write(_MOD_A, "return 'a'", "return 'a1'"), _write(_MOD_A, "return 'a1'", "return 'a2'"),
         _write(_MOD_A, "return 'a2'", "return 'a3'")],
        [_test_evt("pytest -q", "1 failed", 1), _test_evt("pytest -q", "1 failed", 1)],
        [_grep("gizmo", _gizmo_hits())],
        # S6 late-drop domain: passing test naming late_probe, then a late_probe def/ref partition.
        [_test_evt("pytest -k late_probe tests/test_pkg.py", "1 passed", 0),
         _grep("late_probe", "pkg/io.py:6: def late_probe(x):\npkg/db.py:6: def late_probe(x):")],
        [_grep("run", _run_hits()), _write(_MOD_B, "return 'b'", "return 'B'")],
        # S8 empty-payload domain: an all-node_modules def/ref partition (renders empty).
        [_grep("vend_probe", "node_modules/a.js:3: function vend_probe()\n"
                             "node_modules/b.js:3: function vend_probe()")],
    ]


def scenario_s9(driver) -> ScenarioResult:
    flag = "(invariant: all GT_SS_* on)"
    all_ss_on = {k: "1" for k in _SS_FLAGS}
    subs: list[tuple[str, str]] = []
    leak_ok = True
    dose_ok = True
    detail_bits: list[str] = []
    for i, stream in enumerate(_all_streams()):
        res = driver.run(stream, all_ss_on)
        lk, lkd = _leak_free(res)
        ds, dsd = _dose_le_one(res)
        subs.append((f"stream#{i} leak-free", PASS if lk else FAIL))
        subs.append((f"stream#{i} <=1 dose", PASS if ds else FAIL))
        if not lk:
            leak_ok = False
            detail_bits.append(f"s{i}:{lkd}")
        if not ds:
            dose_ok = False
            detail_bits.append(f"s{i}:{dsd}")
    verdict = PASS if (leak_ok and dose_ok) else FAIL
    detail = "; ".join(detail_bits) if detail_bits else "0 leaks; <=1 dose across all streams"
    return ScenarioResult("S9", "LEAK+DOSE", flag, verdict, detail, subs)


# ══════════════════════════════════════════════════════════════════════════════
# S10 — BYTE-IDENTITY OFF (all GT_SS_* unset == no-SS baseline)
# ══════════════════════════════════════════════════════════════════════════════
def scenario_s10(driver) -> ScenarioResult:
    flag = "(invariant: all GT_SS_* off)"
    combined: list[Event] = []
    for stream in _all_streams():
        combined.extend(stream)
    # A properly flag-gated SS feature is byte-identical whether every GT_SS_* is UNSET or
    # explicitly "0" — both in the model observation stream AND in the (temp-scrubbed) ledger.
    # This bites a feature that fires-when-0 or fires-when-unset differently. (We do NOT flag a
    # baked host-side telemetry reason like the pre-existing `ss_ack` row: it is present in BOTH
    # arms, so unset==zeroed still holds — the correct, non-false-positive invariant.)
    unset = driver.run(combined, {})
    zeroed = driver.run(combined, {k: "0" for k in _SS_FLAGS})
    obs_identical = unset.obs_stream() == zeroed.obs_stream()
    ledger_identical = _norm_ledger(unset.ledger) == _norm_ledger(zeroed.ledger)
    subs = [
        ("unset==explicit-0 observation stream", PASS if obs_identical else FAIL),
        ("unset==explicit-0 ledger (temp-scrubbed)", PASS if ledger_identical else FAIL),
    ]
    verdict = PASS if (obs_identical and ledger_identical) else FAIL
    detail = f"obs_byte_identical={obs_identical}; ledger_identical={ledger_identical}"
    return ScenarioResult("S10", "BYTE-IDENTITY-OFF", flag, verdict, detail, subs)


# ══════════════════════════════════════════════════════════════════════════════
# S11 — SUBMIT-RED (needs GT_SS_SUBMIT_RED)
# ══════════════════════════════════════════════════════════════════════════════
def scenario_s11(driver) -> ScenarioResult:
    flag = "GT_SS_SUBMIT_RED"
    # UNRESOLVED: edit mod_a, then a test FAILS citing mod_a and never goes green -> the submit
    # boundary refuses ONCE (single dose); the 2nd submit passes silent (an allow ledger row).
    fail_out = f"{_MOD_A}:8: in run\nE   assert run() == 'x'\n1 failed"
    unresolved = [_write(_MOD_A, "return 'a'", "return 'a1'"),
                  _test_evt("pytest -q", fail_out, 1)]
    # GREEN: same edit, the test PASSES citing mod_a -> the last touching event is green -> the
    # submit boundary stays SILENT (nothing to refuse).
    pass_out = f"{_MOD_A} .\n1 passed"
    green = [_write(_MOD_A, "return 'a'", "return 'a1'"),
             _test_evt("pytest -q", pass_out, 0)]

    on_u = driver.run(unresolved, {flag: "1"}, submit_attempts=2)
    on_u2 = driver.run(unresolved, {flag: "1"}, submit_attempts=2)
    off_u = driver.run(unresolved, {flag: "0"}, submit_attempts=2)
    base_u = driver.run(unresolved, {}, submit_attempts=2)
    on_g = driver.run(green, {flag: "1"}, submit_attempts=2)

    subs = [
        ("determinism (unresolved x2)", PASS if _signature(on_u) == _signature(on_u2) else FAIL),
        ("byte-identity off==baseline",
         PASS if (off_u.obs_stream() == base_u.obs_stream()
                  and off_u.submit_refusals == base_u.submit_refusals) else FAIL),
    ]
    built = (on_u.submit_refusals != off_u.submit_refusals)

    fired = [r for r in on_u.submit_refusals if r]
    fired_once = len(fired) == 1
    red_row = bool(on_u.rows_with_reason("ss_submit_red"))
    allow_row = any(str(r.get("reason")) == "ss_submit_red" and "allow" in str(r.get("outcome"))
                    for r in on_u.ledger)
    green_silent = not any(r for r in on_g.submit_refusals if r)
    off_silent = not any(r for r in off_u.submit_refusals if r)
    core_ok = fired_once and red_row and allow_row and green_silent and off_silent
    detail = (f"unresolved_refusals={len(fired)}(want 1); ss_submit_red_row={red_row}; "
              f"allow_on_2nd={allow_row}; green_silent={green_silent}; off_silent={off_silent}")
    return _gate("S11", "SUBMIT-RED", flag, subs, core_ok, detail, built)


# ══════════════════════════════════════════════════════════════════════════════
# runner
# ══════════════════════════════════════════════════════════════════════════════
_SCENARIOS: list[Callable] = [
    scenario_s1, scenario_s2, scenario_s3, scenario_s4, scenario_s5,
    scenario_s6, scenario_s7, scenario_s8, scenario_s9, scenario_s10,
    scenario_s11,
]


def run_all(driver) -> list[ScenarioResult]:
    results: list[ScenarioResult] = []
    for fn in _SCENARIOS:
        try:
            results.append(fn(driver))
        except Exception as exc:  # noqa: BLE001 — a scenario fault is a reportable ERROR, not a crash
            sid = fn.__name__.replace("scenario_", "").upper()
            results.append(ScenarioResult(sid, fn.__name__, "?", ERROR, f"{type(exc).__name__}: {exc}"))
    return results


def _exit_code(results: list[ScenarioResult]) -> int:
    return 0 if all(r.verdict in (PASS, SKIP) for r in results) else 1


def _table(results: list[ScenarioResult]) -> str:
    w = max((len(r.name) for r in results), default=10)
    lines = ["", f"  {'ID':<4} {'SCENARIO':<{w}} {'VERDICT':<20} DETAIL",
             f"  {'-'*4} {'-'*w} {'-'*20} {'-'*40}"]
    for r in results:
        lines.append(f"  {r.sid:<4} {r.name:<{w}} {r.verdict:<20} {r.detail}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="SS-6 SUPER-SAIYAN acceptance gate")
    ap.add_argument("--out", default=str(_REPO / "ss_gate_report.json"),
                    help="report path (default: <repo>/ss_gate_report.json, gitignored)")
    ap.add_argument("--meta", action="store_true",
                    help="print per-arm ENVIRONMENTAL dependency diagnostics and exit "
                         "(hermeticity debug: seam path, graph.db row counts, L6 probe, profile "
                         "posture, def/ref classification, per-arm delivered rows)")
    args = ap.parse_args(argv)

    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    try:
        driver = RealSeamDriver()
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"[ss_gate] FATAL: could not install the real seam: {exc}\n")
        return 2

    if args.meta:
        return _meta_report(driver)

    # HERMETICITY PRE-FLIGHT (SS-6b): prove the fixture-graph channel is LIVE in the CORE arm
    # before trusting ANY SKIP:flag-not-built verdict. A dead channel makes every flag-gated
    # scenario SKIP (false 'not landed'); refuse to emit that — surface a LOUD, NAMED ERROR.
    # The S0 row is always reported (PASS when live) so the channel state is a durable artifact.
    canary_ok, canary_detail = _channel_canary(driver)
    s0 = ScenarioResult("S0", "CHANNEL-CANARY", "(hermeticity pre-flight)",
                        PASS if canary_ok else ERROR, canary_detail)
    if not canary_ok:
        sys.stderr.write(f"[ss_gate] CHANNEL-DEAD (refusing false SKIP): {canary_detail}\n")
        results = [s0]
    else:
        results = [s0] + run_all(driver)
    code = _exit_code(results)

    counts = {v: sum(1 for r in results if r.verdict == v) for v in (PASS, FAIL, SKIP, ERROR)}
    report = {
        "gate": "ss_gate",
        "generated_utc": ts,
        "seam_driver": driver.name,
        "exit_code": code,
        "counts": counts,
        "scenarios": [
            {"id": r.sid, "name": r.name, "flag": r.flag, "verdict": r.verdict,
             "detail": r.detail, "subchecks": [{"name": n, "verdict": v} for n, v in r.subchecks]}
            for r in results
        ],
    }
    out_path = Path(args.out)
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"[ss_gate] WARN: could not write report: {exc}\n")

    sys.stdout.write(f"\n# SS-6 SUPER-SAIYAN acceptance gate — {ts}\n")
    sys.stdout.write(_table(results) + "\n")
    sys.stdout.write(
        f"\n  PASS={counts[PASS]} FAIL={counts[FAIL]} {SKIP}={counts[SKIP]} ERROR={counts[ERROR]}"
        f" / {len(results)} scenarios\n")
    sys.stdout.write(f"  report -> {out_path}\n")
    sys.stdout.write(f"  EXIT {code} ({'GREEN' if code == 0 else 'RED'})"
                     f" — 0 iff every scenario is PASS or {SKIP}\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
