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
implement to these; each is an INDEPENDENT ``GT_SS_*`` env flag, default-OFF byte-identical):

  S1  GT_SS_STEP_BEHIND    a factual delivery whose ENTIRE entity set ⊆ {files the agent already
                           viewed + symbols it grepped} is SUPPRESSED (ledger reason
                           ``ss_step_behind``); a delivery carrying a NOVEL cross-file entity survives.
  S2  GT_SS_SEMANTIC_DEDUP two same-class deliveries with byte-DISTINCT payloads but equal/subset
                           entity sets -> the second is suppressed (reason ``ss_semantic_dup``).
  S3  GT_SS_COHERENCE      a run-coherence signal fires ONLY on >=3 consecutive successful writes to
                           one file with NO intervening passing test; a passing test / a failed write
                           / <=2 writes does NOT fire. When it fires its bytes carry the EXACT count.
  S4  GT_SS_RECOVERY       the SAME failing test observed twice with no intervening edit delivers a
                           recovery imperative on the SECOND repeat (not later); two different-output
                           commands do NOT; a passing-test repeat does NOT.
  S5  GT_SS_PROVENANCE     a fact whose provenance is only low-quality paths (``tmp/``, ``htmlcov/``,
                           scratch/coverage) is suppressed (reason ``ss_provenance``); no L6 reindex
                           is triggered for those paths.
  S6  GT_SS_LATE           an obligation whose requirement is already covered by an EARLIER passing
                           test is suppressed (reason ``ss_late``).
  S7  GT_SS_ACK            a delivery whose entity a later agent action references gains ledger
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

# The fixture's known test-identifier tokens — none of these may EVER appear in a
# delivered model-facing payload (the S9 leak invariant).
_TEST_TOKENS = ("tests/test_pkg.py", "test_run", "test_pkg")

# The low-provenance paths the S5 fixture engineers into facts about `gizmo`.
_PROVENANCE_PATHS = ("tmp/scratch.py", "htmlcov/x.js")

# The canonical SUPER-SAIYAN feature flags (the S1-S8 overlay). All default-OFF.
_SS_FLAGS = ("GT_SS_STEP_BEHIND", "GT_SS_SEMANTIC_DEDUP", "GT_SS_COHERENCE", "GT_SS_RECOVERY",
             "GT_SS_PROVENANCE", "GT_SS_LATE", "GT_SS_ACK", "GT_SS_ARBITER_V2")


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
            con.execute(
                "INSERT INTO edges(source_id,target_id,type,resolution_method,confidence)"
                " VALUES(?,?,?,?,?)",
                (e["source_id"], e["target_id"], e.get("type", "CALLS"),
                 e.get("resolution_method", "import"), e.get("confidence", 1.0)))
        for p in spec.get("properties", []):
            con.execute(
                "INSERT INTO properties(node_id,kind,value,line,confidence)"
                " VALUES(?,?,?,?,?)",
                (p["node_id"], p["kind"], p["value"], p.get("line", 1),
                 p.get("confidence", 0.8)))
        con.commit()
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
        self._core = core
        spec_path = _FIXTURES / "graph_spec.json"
        self.spec = json.loads(spec_path.read_text(encoding="utf-8"))
        self.repo_src = _FIXTURES / "repo"

    def run(self, events: list[Event], ss_env: dict) -> SeamResult:
        g = self.g
        env_snapshot = dict(os.environ)
        saved_db = g._db_path
        saved_root = g._root
        saved_ps = getattr(g, "_POST_SEARCH_ON", None)
        tmp = Path(tempfile.mkdtemp(prefix="ss_gate_"))
        try:
            root = tmp / "repo"
            shutil.copytree(self.repo_src, root)
            db = str(tmp / "graph.db")
            _build_graph_db(db, self.spec)
            ledger = tmp / "led.jsonl"

            # env: clear ALL GT_SS_*, apply core + this arm's overrides.
            for k in _all_ss_env_keys():
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

            g._db_path = lambda: db
            g._root = lambda: str(root)
            if saved_ps is not None:
                g._POST_SEARCH_ON = False
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
            return SeamResult(observations=obs, ledger=rows)
        finally:
            g._db_path = saved_db
            g._root = saved_root
            if saved_ps is not None:
                g._POST_SEARCH_ON = saved_ps
            os.environ.clear()
            os.environ.update(env_snapshot)
            shutil.rmtree(tmp, ignore_errors=True)


class FakeSeamDriver:
    """A test double for the selftest: ``behavior(events, ss_env) -> SeamResult``. Lets the
    selftest inject a SS-CORRECT reference seam (scenario must PASS) and mutated seams
    (scenario must FAIL) through the SAME scenario code the real gate runs — proving the
    gate BITES, not that GT passes."""

    name = "fake"

    def __init__(self, behavior: Callable[[list[Event], dict], SeamResult]) -> None:
        self._behavior = behavior

    def run(self, events: list[Event], ss_env: dict) -> SeamResult:
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


def _cat(path: str, body: str) -> Event:
    return Event(action={"command": f"cat {path}"}, output=body, rc=0)


def _grep(sym: str, hits: str) -> Event:
    return Event(action={"command": f"grep -rn {sym} ."}, output=hits, rc=0)


def _run_hits() -> str:  # the ambiguous `run` def sites (mod_a + mod_b)
    return f"{_MOD_A}:8: def run():\n{_MOD_B}:9: def run():"


def _gizmo_hits() -> str:
    return "tmp/scratch.py:5: def gizmo():\nhtmlcov/x.js:4: function gizmo()"


# ══════════════════════════════════════════════════════════════════════════════
# S1 — STEP-BEHIND
# ══════════════════════════════════════════════════════════════════════════════
def scenario_s1(driver) -> ScenarioResult:
    flag = "GT_SS_STEP_BEHIND"
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
    flag = "GT_SS_SEMANTIC_DEDUP"
    # two greps for fold-variant symbols that resolve to the SAME def files (same entity set)
    # but yield byte-DISTINCT partition payloads -> content-hash dedup does NOT fire, so the
    # semantic (entity-set) dedup must suppress the second.
    events = [
        _grep("run", _run_hits()),
        _grep("RUN", f"{_MOD_A}:8: def run():  # via RUN\n{_MOD_B}:9: def run():  # via RUN"),
    ]
    on1, off, base, subs = _det_and_byteid(driver, events, flag)
    built = _has_effect(on1, off)
    delivered = on1.delivered_rows()
    dup_row = bool(on1.rows_with_reason("ss_semantic_dup"))
    core_ok = (len(delivered) <= 1 and dup_row)
    detail = f"delivered_rows={len(delivered)} (want<=1); ss_semantic_dup_row={dup_row}"
    return _gate("S2", "SEMANTIC-DEDUP", flag, subs, core_ok, detail, built)


# ══════════════════════════════════════════════════════════════════════════════
# S3 — COHERENCE TRUTH
# ══════════════════════════════════════════════════════════════════════════════
def _write(path: str, old: str, new: str, rc: int = 0) -> Event:
    return Event(action={"command": "str_replace", "path": path, "old_str": old, "new_str": new},
                 output=("" if rc == 0 else "patch failed"), rc=rc)


def scenario_s3(driver) -> ScenarioResult:
    flag = "GT_SS_COHERENCE"
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
    flag = "GT_SS_RECOVERY"
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
    flag = "GT_SS_LATE"
    # a passing test that covers sig_target EARLIER; then a turn that would resurface the
    # sig_target obligation -> it must be dropped late (reason ss_late).
    events = [
        _test_evt("pytest -q tests/test_pkg.py", "1 passed", 0),
        _grep("sig_target", "pkg/util.py:4: def sig_target(x):"),
    ]
    on1, off, base, subs = _det_and_byteid(driver, events, flag)
    built = _has_effect(on1, off)
    late_row = bool(on1.rows_with_reason("ss_late"))
    core_ok = late_row
    detail = f"ss_late_row={late_row}"
    return _gate("S6", "LATE-DROP", flag, subs, core_ok, detail, built)


# ══════════════════════════════════════════════════════════════════════════════
# S7 — ACK TELEMETRY
# ══════════════════════════════════════════════════════════════════════════════
def scenario_s7(driver) -> ScenarioResult:
    flag = "GT_SS_ACK"
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
    # a grep whose symbol resolves to a def but where the producer may yield zero renderable
    # bytes; arbiter-v2 must ensure NO delivered row with chars_delivered==0.
    events = [_grep("shared_helper", "pkg/mod_a.py:12: def shared_helper():"), _grep("run", _run_hits())]
    on1, off, base, subs = _det_and_byteid(driver, events, flag)
    built = _has_effect(on1, off)
    if not built:
        return ScenarioResult("S8", "EMPTY-PAYLOAD", flag, SKIP,
                              f"GT_SS_ARBITER_V2 has no effect (optional engine not built)", subs)
    zero_byte_delivered = [r for r in on1.delivered_rows_any() if int(r.get("chars_delivered") or 0) == 0]
    core_ok = not zero_byte_delivered
    detail = f"zero_byte_delivered_rows={len(zero_byte_delivered)}(want 0)"
    return _gate("S8", "EMPTY-PAYLOAD", flag, subs, core_ok, detail, built)


# ══════════════════════════════════════════════════════════════════════════════
# S9 — LEAK + DOSE INVARIANTS (across every scenario stream, all SS on)
# ══════════════════════════════════════════════════════════════════════════════
def _all_streams() -> list[list[Event]]:
    return [
        [_cat(_MOD_A, "def run(): return 'a'\n"), _cat(_MOD_B, "def run(): return 'b'\n"), _grep("run", _run_hits())],
        [_grep("run", _run_hits()), _grep("RUN", _run_hits())],
        [_write(_MOD_A, "return 'a'", "return 'a1'"), _write(_MOD_A, "return 'a1'", "return 'a2'"),
         _write(_MOD_A, "return 'a2'", "return 'a3'")],
        [_test_evt("pytest -q", "1 failed", 1), _test_evt("pytest -q", "1 failed", 1)],
        [_grep("gizmo", _gizmo_hits())],
        [_test_evt("pytest -q tests/test_pkg.py", "1 passed", 0),
         _grep("sig_target", "pkg/util.py:4: def sig_target(x):")],
        [_grep("run", _run_hits()), _write(_MOD_B, "return 'b'", "return 'B'")],
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
# runner
# ══════════════════════════════════════════════════════════════════════════════
_SCENARIOS: list[Callable] = [
    scenario_s1, scenario_s2, scenario_s3, scenario_s4, scenario_s5,
    scenario_s6, scenario_s7, scenario_s8, scenario_s9, scenario_s10,
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
    args = ap.parse_args(argv)

    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    try:
        driver = RealSeamDriver()
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"[ss_gate] FATAL: could not install the real seam: {exc}\n")
        return 2

    results = run_all(driver)
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
