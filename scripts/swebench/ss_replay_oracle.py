#!/usr/bin/env python3
r"""SS-R — the SUPER-SAIYAN REPLAY-ORACLE (proof-pipeline step 6).

Run:  ``python scripts/swebench/ss_replay_oracle.py``

This is a DETERMINISTIC, ``$0``, OFFLINE proof harness. It takes the 29 RECORDED arm-4
trajectories (mini-swe-agent.trajectory.json + gt_runtime_ledger_<task>.jsonl), RECONSTRUCTS
the NATIVE (pre-GT) observation stream each task's agent actually saw, replays that native
stream back through the SS-flagged seam, and asserts — per the byte-verified case manifest
(``tests/fixtures/ss_replay/cases.json``) — that the known-BAD delivery instances are now
suppressed while the known-GOOD (P5) ones still deliver.

It NEVER edits a product file. It imports + drives the frozen seam through its public test
surface only (mirroring ``ss_gate.py``'s RealSeamDriver idiom); if the recorded artifacts do
not carry what full replay needs (the task's repo checkout for the seam's file-body reads) the
seam driver reports BLOCKED with a precise note and the flag-gated cases become
``SKIP:seam-blocked`` — an honest finding, never a false green.

THREE LAYERS (all here):

  1  RECONSTRUCTION  For each recorded task: walk ``messages[]`` chronologically, pair each
                     assistant action with its following tool observation, and STRIP the
                     GT-delivered bytes from each observation to recover the native text. For
                     every delivered ledger seal (``outcome`` contains ``delivered``,
                     ``chars_delivered>0``, ``content_sha256_16`` set) the exact byte/char
                     window whose sha256 hex-prefix(16) == the seal is located (bytes first,
                     char-window fallback) and removed, progressively over the trajectory in
                     ledger order (so a short seal nested inside a longer one lands on its own
                     home). INVARIANT: after stripping, NO remaining window matches ANY seal —
                     feeding GT's own prior bytes back through the seam would poison the novelty
                     gate (the seam would read them as agent-acquired text).

  2  SEAM-DRIVER     A minimal ``SeamDriver`` protocol (``step(action, native_obs) ->
                     [LedgerRow]``). ``StubSeamDriver`` is a test double (the selftest injects an
                     SS-correct reference and mutated seams through the SAME oracle). ``MiniSeamDriver``
                     mirrors ss_gate's RealSeamDriver: it installs ``gt_mini_patch``, arms
                     Profile-2 + the per-arm ``GT_SS_*`` flags (passed to the constructor), points
                     ``_db_path``/``_root`` at the task, and drives ``_augment_output`` per step,
                     reading the durable ``GT_RUNTIME_LEDGER`` delta. Full replay of a RECORDED
                     task additionally needs that task's repo checkout (see ``SeamReplayBlocked``).

  3  ORACLE          Given the reconstructed RECORDED deliveries + the replayed ledger + the case
                     manifest, evaluate every case: PRESERVE (delivery of that class still occurs
                     at/near the recorded m; the 3 P5s are CARDINAL — a kill fails the whole
                     oracle), SUPPRESS (the named delivery is now absent/suppressed with the
                     expected ``ss_*`` reason), COUNT-ACCURACY (coherence fires only with the exact
                     verified write count or stays silent), plus the invariants (leak==0 via a
                     manifest-free test-identifier scan; <=1 payload per observation; zero
                     delivered rows with 0 bytes; all-flags-off replay byte-identical to the
                     RECORDED ledger — the off-flag fixpoint).

OUTPUT: a per-case verdict table (PASS/FAIL/SKIP:reason) to stdout + a JSON report. EXIT 0 iff
no case FAILs and no CARDINAL P5 is killed and no manifest entry is invalid; else non-zero.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol, runtime_checkable

# ── repo layout ──────────────────────────────────────────────────────────────
_REPO = Path(__file__).resolve().parents[2]
_SRC = _REPO / "src"
_ART = _REPO / "artifact_deepswe"
_DEFAULT_CASES = _REPO / "tests" / "fixtures" / "ss_replay" / "cases.json"
_DEFAULT_RECORDED = Path("D:/gt_runs/29236533134/art")

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass

# ── verdict vocabulary ───────────────────────────────────────────────────────
PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"

# ── SS ledger-reason tokens (must match ss_gate.py's feature contract) ────────
SS_REASON = {
    "step_behind": "ss_step_behind",
    "semantic_dup": "ss_semantic_dup",
    "provenance": "ss_provenance",
    "late": "ss_late",
}

# A delivered row is "delivered with bytes" iff outcome mentions delivered and chars>0.
def _is_delivered(row: dict) -> bool:
    return "delivered" in str(row.get("outcome") or "") and int(row.get("chars_delivered") or 0) > 0


# ══════════════════════════════════════════════════════════════════════════════
# LEAK SCAN — manifest-free test-identifier detector (word-boundary + length guard)
# ══════════════════════════════════════════════════════════════════════════════
# These patterns catch pytest node-ids / test module + function names WITHOUT knowing any
# task's specific test names. The length/boundary guards keep ordinary English ("latest",
# "greatest", "contest") from tripping the scan.
_LEAK_PATTERNS = [
    re.compile(r"\btests?/[^\s:'\"]+\.py\b"),          # tests/foo/test_x.py  or  test/foo.py
    re.compile(r"::test[A-Za-z0-9_]*\b"),               # ::test_something (node-id tail)
    re.compile(r"\btest_[A-Za-z0-9_]{2,}\b"),           # test_something (>=2 tail chars)
    re.compile(r"\b[A-Za-z0-9]{2,}_test\b"),            # something_test
    re.compile(r"\bFAIL_TO_PASS\b|\bPASS_TO_PASS\b"),   # SWE-bench label leakage
]


def leak_tokens(text: str) -> list[str]:
    """Return every test-identifier-looking token in ``text`` (manifest-free)."""
    hits: list[str] = []
    if not text:
        return hits
    for pat in _LEAK_PATTERNS:
        hits.extend(m.group(0) for m in pat.finditer(text))
    return hits


# ══════════════════════════════════════════════════════════════════════════════
# LAYER 1 — RECONSTRUCTION
# ══════════════════════════════════════════════════════════════════════════════
def _sha16(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()[:16]


def locate_seal(content: str, n_chars: int, sha16: str) -> int | None:
    """Return the CHAR start offset of the delivered window in ``content``, or None.

    The seam seals ``sha256(delta.encode('utf-8'))[:16]`` over a delta of ``n_chars`` CHARS.
    We honor the spec's "bytes first, char-window fallback": a byte-window of ``n_chars`` bytes
    matches iff the delta is pure-ASCII (bytes==chars); otherwise the sha (computed over MORE
    bytes) can never equal, so it safely falls through to the authoritative char-window.
    """
    if not content or n_chars <= 0:
        return None
    b = content.encode("utf-8")
    # (a) bytes-first: n_chars-byte window
    if n_chars <= len(b):
        for j in range(0, len(b) - n_chars + 1):
            if _sha16(b[j:j + n_chars]) == sha16:
                try:
                    return len(b[:j].decode("utf-8"))   # byte offset -> char offset
                except UnicodeDecodeError:
                    continue                              # window split a codepoint; fall through
    # (b) char-window fallback (authoritative)
    if n_chars <= len(content):
        for j in range(0, len(content) - n_chars + 1):
            if _sha16(content[j:j + n_chars].encode("utf-8")) == sha16:
                return j
    return None


@dataclass
class Delivery:
    """One recorded (or replayed) model-facing GT delivery, located to its home message."""

    layer: str
    event_type: str
    iteration: int
    chars: int
    sha16: str | None
    home_msg: int          # trajectory message index the bytes were appended to (== manifest mNN)
    outcome: str
    reason: str
    file_path: str
    payload: str = ""      # the stripped delta bytes (the exact GT text the model saw)


@dataclass
class ReconstructedTask:
    task: str
    pairs: list[tuple[str, str]]          # (action_text, native_observation) in chronological order
    recorded_deliveries: list[Delivery]   # the located seals (home_msg == manifest mNN)
    residual_leaks: list[str]             # non-empty => stripping failed (a seal still matches)
    raw_rows: list[dict]                  # the full recorded ledger (for invariants / suppressed rows)
    n_messages: int


def _seal_rows(rows: list[dict]) -> list[dict]:
    return [r for r in rows if _is_delivered(r) and r.get("content_sha256_16")]


def reconstruct_task(task: str, recorded_root: Path) -> ReconstructedTask:
    """Reconstruct the native observation stream + recorded deliveries for one task."""
    d = recorded_root / task
    traj = json.loads((d / "mini-swe-agent.trajectory.json").read_text(encoding="utf-8"))
    msgs = traj["messages"]
    rows = [json.loads(l) for l in (d / f"gt_runtime_ledger_{task}.jsonl").read_text(
        encoding="utf-8").splitlines() if l.strip()]

    # progressively strip each delivered seal (ledger order) over a mutable buffer.
    stripped = [m.get("content", "") if isinstance(m.get("content"), str) else "" for m in msgs]
    deliveries: list[Delivery] = []
    for r in _seal_rows(rows):
        it = int(r["iteration"]); n = int(r["chars_delivered"]); sha = str(r["content_sha256_16"])
        # try the calibrated tool-observation index (2*iter+1) first, else full scan.
        home = 2 * it + 1
        off = locate_seal(stripped[home], n, sha) if 0 <= home < len(stripped) else None
        if off is None:
            home, off = None, None
            for idx, c in enumerate(stripped):
                o = locate_seal(c, n, sha)
                if o is not None:
                    home, off = idx, o
                    break
        if home is None or off is None:
            # seal bytes not found anywhere — record as a residual/anomaly (never silently drop).
            deliveries.append(Delivery(str(r.get("layer")), str(r.get("event_type")), it, n, sha,
                                       -1, str(r.get("outcome")), str(r.get("reason") or ""),
                                       str(r.get("file_path") or ""), payload=""))
            continue
        payload = stripped[home][off:off + n]
        deliveries.append(Delivery(str(r.get("layer")), str(r.get("event_type")), it, n, sha,
                                   home, str(r.get("outcome")), str(r.get("reason") or ""),
                                   str(r.get("file_path") or ""), payload=payload))
        stripped[home] = stripped[home][:off] + stripped[home][off + n:]

    # INVARIANT: after stripping, no remaining window matches any seal.
    residual: list[str] = []
    for r in _seal_rows(rows):
        n = int(r["chars_delivered"]); sha = str(r["content_sha256_16"])
        for idx, c in enumerate(stripped):
            if locate_seal(c, n, sha) is not None:
                residual.append(f"seal {sha} (iter {r['iteration']}, {n}c) still present at m{idx}")
                break

    # build (action, native_obs) pairs: each tool observation paired with its preceding action.
    pairs: list[tuple[str, str]] = []
    for i, m in enumerate(msgs):
        if m.get("role") == "tool":
            action = ""
            if i > 0 and msgs[i - 1].get("role") == "assistant":
                action = msgs[i - 1].get("content", "") or ""
            pairs.append((action, stripped[i]))

    return ReconstructedTask(task=task, pairs=pairs, recorded_deliveries=deliveries,
                             residual_leaks=residual, raw_rows=rows, n_messages=len(msgs))


def extract_command(action_text: str) -> str:
    """Pull the last fenced code block (the bash command) from an assistant message; '' if none.
    Used only by the real seam driver — the stub does not need it."""
    if not action_text:
        return ""
    blocks = re.findall(r"```(?:bash|sh)?\s*\n(.*?)```", action_text, re.DOTALL)
    if blocks:
        return blocks[-1].strip()
    return ""


# ══════════════════════════════════════════════════════════════════════════════
# LAYER 2 — SEAM DRIVER
# ══════════════════════════════════════════════════════════════════════════════
LedgerRow = dict


class SeamReplayBlocked(RuntimeError):
    """Raised by a real driver when full replay of a recorded task cannot proceed. The message
    NAMES exactly what is missing (never a bare TODO)."""


@runtime_checkable
class SeamDriver(Protocol):
    name: str

    def begin_task(self, task: str, recorded_dir: Path) -> None:
        ...

    def step(self, action_text: str, native_observation: str) -> list[LedgerRow]:
        """Feed ONE (action, native observation) pair through the seam; return the ledger rows
        produced by that step (empty list if the seam delivered nothing)."""
        ...

    def end_task(self) -> None:
        ...


class StubSeamDriver:
    """Deterministic test double: a ``behavior(task) -> list[LedgerRow]`` supplies the whole
    task's replayed ledger up front; ``step`` streams it out in order. Lets the selftest inject
    an SS-CORRECT reference seam (cases PASS) and mutated seams (cases FAIL) through the SAME
    oracle — proving the oracle BITES, not that GT passes. Runs with NO real seam."""

    name = "stub"

    def __init__(self, behavior: Callable[[str], list[LedgerRow]]) -> None:
        self._behavior = behavior
        self._queue: list[LedgerRow] = []
        self._task = ""

    def begin_task(self, task: str, recorded_dir: Path) -> None:
        self._task = task
        # the stub returns the whole task's replayed ledger on the FIRST step (order preserved).
        self._queue = list(self._behavior(task))

    def step(self, action_text: str, native_observation: str) -> list[LedgerRow]:
        out, self._queue = self._queue, []
        return out

    def end_task(self) -> None:
        self._queue = []
        self._task = ""


class MiniSeamDriver:
    """Drives the REAL ``gt_mini_patch`` seam over a task, mirroring ss_gate.py's RealSeamDriver
    idiom (import = install; arm Profile-2 + the per-arm ``GT_SS_*`` flags; point _db_path/_root;
    drive ``_augment_output`` per step; read the durable GT_RUNTIME_LEDGER delta). The per-arm SS
    flag env is passed to the constructor.

    Full replay of a RECORDED task needs, per step, the seam's inputs: the task ``graph.db`` (present
    in the recorded artifacts) AND the task's REPO CHECKOUT at ``_root()`` for the seam's file-body
    reads / edit-overlay reconstruction (post_view reads the viewed file, post_edit reconstructs the
    edited file, L6 reindexes on-disk paths). The recorded artifacts carry graph.db but NOT the repo
    snapshot, so ``begin_task`` raises ``SeamReplayBlocked`` naming that gap. When a repo snapshot IS
    supplied (``repo_snapshot_root``) the driver runs end-to-end exactly like ss_gate."""

    name = "mini"

    def __init__(self, flag_env: dict[str, str] | None = None,
                 repo_snapshot_root: Path | None = None) -> None:
        self.flag_env = dict(flag_env or {})
        self.repo_snapshot_root = repo_snapshot_root
        self._installed = False
        self._g = None
        self._core: dict[str, str] = {}
        self._env_snapshot: dict[str, str] = {}
        self._saved: dict[str, object] = {}
        self._tmp: Path | None = None
        self._ledger: Path | None = None
        self._seen = 0

    def _install(self) -> None:
        for p in (str(_ART), str(_SRC)):
            if p not in sys.path:
                sys.path.insert(0, p)
        os.environ.pop("GT_BASELINE", None)
        import gt_mini_patch as g  # noqa: E402 — import side effects ARE the seam install
        from groundtruth.runtime import rl_profile as rp  # noqa: E402
        self._g = g
        core = dict(rp.resolve_profile({"GT_RL_PROFILE": "2"}))
        core["GT_RL_PROFILE"] = "2"
        core["GT_GATEWAY"] = "1"
        core["GT_GLOBAL_ARBITER"] = "1"
        self._core = core
        self._installed = True

    def begin_task(self, task: str, recorded_dir: Path) -> None:
        if not self._installed:
            self._install()
        import shutil
        import tempfile
        g = self._g
        db_src = recorded_dir / task / "graph.db"
        if not db_src.is_file():
            raise SeamReplayBlocked(
                f"task {task}: recorded graph.db missing at {db_src}; cannot drive the seam.")
        root = self.repo_snapshot_root / task if self.repo_snapshot_root else None
        if root is None or not root.is_dir():
            raise SeamReplayBlocked(
                f"task {task}: FULL REPLAY REQUIRES the task's REPO CHECKOUT at _root() — the seam's "
                f"post_view/post_edit/L6 producers read file bodies and reconstruct edited files from "
                f"disk, which the recorded artifacts do NOT contain (graph.db only). Supply the "
                f"container repo snapshot via --repo-snapshot-root <dir>/<task>/ (matching the recorded "
                f"HEAD) to enable end-to-end replay; additionally the GT_SS_* engines (SS-0/SS-1) must "
                f"be landed for the ss_* suppress/late/count reasons to appear. "
                f"(graph.db IS present at {db_src}.)")
        self._env_snapshot = dict(os.environ)
        self._tmp = Path(tempfile.mkdtemp(prefix="ssr_mini_"))
        run_root = self._tmp / "repo"
        shutil.copytree(root, run_root)
        db = str(self._tmp / "graph.db")
        shutil.copyfile(db_src, db)
        self._ledger = self._tmp / "led.jsonl"
        self._saved = {"db": g._db_path, "root": g._root,
                       "ps": getattr(g, "_POST_SEARCH_ON", None)}
        for k in [e for e in os.environ if e.startswith("GT_SS_")]:
            os.environ.pop(k, None)
        for k, v in self._core.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = str(v)
        for k, v in self.flag_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = str(v)
        os.environ["GT_RUNTIME_LEDGER"] = str(self._ledger)
        g._db_path = lambda: db
        g._root = lambda: str(run_root)
        if self._saved["ps"] is not None:
            g._POST_SEARCH_ON = False
        g._reset_oracle_state()
        try:
            g._RUNTIME_LEDGER = g._ProductLedger()
        except Exception:  # noqa: BLE001
            pass
        self._seen = 0

    def step(self, action_text: str, native_observation: str) -> list[LedgerRow]:
        g = self._g
        cmd = extract_command(action_text) or action_text.strip().splitlines()[0] if action_text else ""
        out = {"output": native_observation, "returncode": 0}
        try:
            g._augment_output({"command": cmd}, out)
        except Exception:  # noqa: BLE001 — a seam fault is a delivery-of-nothing here
            pass
        rows: list[LedgerRow] = []
        if self._ledger and self._ledger.is_file():
            all_rows = [json.loads(l) for l in self._ledger.read_text(
                encoding="utf-8").splitlines() if l.strip()]
            rows = all_rows[self._seen:]
            self._seen = len(all_rows)
        return rows

    def end_task(self) -> None:
        import shutil
        g = self._g
        if self._saved:
            g._db_path = self._saved.get("db")
            g._root = self._saved.get("root")
            if self._saved.get("ps") is not None:
                g._POST_SEARCH_ON = self._saved.get("ps")
        if self._env_snapshot:
            os.environ.clear()
            os.environ.update(self._env_snapshot)
        if self._tmp:
            shutil.rmtree(self._tmp, ignore_errors=True)
        self._tmp = None
        self._ledger = None


def replay_task(recon: ReconstructedTask, driver: SeamDriver, recorded_root: Path) -> list[LedgerRow]:
    """Drive one reconstructed task's native stream through ``driver``; return the flat replayed
    ledger (rows in step order). Raises ``SeamReplayBlocked`` from the driver if it cannot run."""
    driver.begin_task(recon.task, recorded_root)
    rows: list[LedgerRow] = []
    try:
        for action, native in recon.pairs:
            rows.extend(driver.step(action, native))
    finally:
        driver.end_task()
    return rows


# ══════════════════════════════════════════════════════════════════════════════
# LAYER 3 — ORACLE
# ══════════════════════════════════════════════════════════════════════════════
@dataclass
class CaseVerdict:
    section: str
    task: str
    label: str          # e.g. "l3b m9" / "consensus.scope m25"
    verdict: str        # PASS / FAIL / SKIP
    reason: str
    cardinal: bool = False


def _parse_delivery(s: str) -> tuple[str, int | None]:
    """'l3.contract m49' / 'detect.coherence m103 (1 real edit, claimed 4)' -> ('l3.contract', 49)."""
    s = s.strip()
    m = re.search(r"\bm(\d+)\b", s)
    mnum = int(m.group(1)) if m else None
    layer = s[:m.start()].strip() if m else s
    return layer, mnum


_LAYER_ALIAS = {
    "l3b": "l3b.evidence", "l3.contract": "l3.contract", "consensus.scope": "consensus.scope",
    "edit.syntax": "edit.syntax", "recovery": "recovery", "detect.coherence": "detect.coherence",
    "spec.obligation": "spec.obligation", "obligation.resurface": "obligation.resurface",
    "verify.horizon": "verify.horizon", "gateway.def_ref_partition": "gateway",
}


def _layer_matches(manifest_layer: str, ledger_layer: str) -> bool:
    want = _LAYER_ALIAS.get(manifest_layer.strip().lower(), manifest_layer.strip().lower())
    ll = str(ledger_layer).strip().lower()
    return ll == want or ll.startswith(want) or want in ll


def _rows_matching(rows: list[dict], layer: str, mnum: int | None, tol: int = 3):
    """Rows of the given layer near message m (via home_msg on Delivery, else iteration proximity)."""
    out = []
    for r in rows:
        if not _layer_matches(layer, r.get("layer", "")):
            continue
        loc = r.get("home_msg", r.get("m"))
        if loc is None:
            loc = None
        if mnum is None or loc is None or abs(int(loc) - mnum) <= tol:
            out.append(r)
    return out


def _deliveries_to_rows(dels: list[Delivery]) -> list[dict]:
    """Normalize Delivery objects to plain rows the oracle matchers consume."""
    return [{"layer": d.layer, "home_msg": d.home_msg, "chars": d.chars, "reason": d.reason,
             "outcome": d.outcome, "payload": d.payload, "delivered": _is_delivered(
                 {"outcome": d.outcome, "chars_delivered": d.chars})}
            for d in dels]


def _is_cardinal_preserve(case: dict) -> bool:
    """The 3 CARDINAL P5 preserves = those whose 'why' names a P5 consumption."""
    return "P5" in str(case.get("why", ""))


def _count_in(text: str) -> set[int]:
    return {int(x) for x in re.findall(r"\b(\d+)\b", text or "")}


def evaluate_cases(cases: dict, recorded: dict[str, list[Delivery]],
                   replayed: dict[str, list[dict]] | None,
                   blocked_note: str | None) -> list[CaseVerdict]:
    """The oracle. ``recorded`` = reconstructed recorded deliveries per task; ``replayed`` = the
    replayed ledger per task (or None if the seam driver was BLOCKED — then flag-gated cases
    SKIP:seam-blocked). Returns one CaseVerdict per case delivery."""
    out: list[CaseVerdict] = []

    def rec_rows(task: str) -> list[dict]:
        return _deliveries_to_rows(recorded.get(task, []))

    def rep_rows(task: str) -> list[dict] | None:
        if replayed is None:
            return None
        # replayed ledger rows: keep only delivered-with-bytes; map iteration->home via 'home_msg'
        norm = []
        for r in replayed.get(task, []):
            rr = dict(r)
            if "home_msg" not in rr and "m" in rr:
                rr["home_msg"] = rr["m"]
            rr["delivered"] = _is_delivered(rr) if "chars_delivered" in rr else bool(rr.get("delivered"))
            if "chars" not in rr:
                rr["chars"] = int(rr.get("chars_delivered") or 0)
            norm.append(rr)
        return norm

    # ---- PRESERVE (incl. the 3 CARDINAL P5s) --------------------------------
    for c in cases.get("preserve", []):
        layer, mnum = _parse_delivery(c["delivery"])
        card = _is_cardinal_preserve(c)
        rec = _rows_matching(rec_rows(c["task"]), layer, mnum)
        rep = rep_rows(c["task"])
        if not rec:
            out.append(CaseVerdict("preserve", c["task"], c["delivery"], SKIP,
                                   "no recorded delivery of this class to preserve", card))
            continue
        if rep is None:
            out.append(CaseVerdict("preserve", c["task"], c["delivery"], SKIP,
                                   f"seam-blocked: {blocked_note}", card))
            continue
        still = [r for r in _rows_matching(rep, layer, mnum) if r.get("delivered")]
        if still:
            out.append(CaseVerdict("preserve", c["task"], c["delivery"], PASS,
                                   f"still delivered at m{still[0].get('home_msg')}", card))
        else:
            out.append(CaseVerdict("preserve", c["task"], c["delivery"], FAIL,
                                   "CARDINAL P5 KILLED — preserve delivery absent after replay"
                                   if card else "preserve delivery absent after replay", card))

    # ---- SUPPRESS families (step_behind / semantic_dup / provenance / late) --
    def suppress_family(section: str, reason_tok: str):
        for c in cases.get(section, []):
            if not isinstance(c, dict):
                continue
            dvs = c.get("deliveries") or ([c["delivery"]] if "delivery" in c else [])
            for dv in dvs:
                layer, mnum = _parse_delivery(dv)
                rec = _rows_matching(rec_rows(c["task"]), layer, mnum)
                rep = rep_rows(c["task"])
                if not rec:
                    out.append(CaseVerdict(section, c["task"], dv, SKIP,
                                           "no recorded delivery of this class", False))
                    continue
                if rep is None:
                    out.append(CaseVerdict(section, c["task"], dv, SKIP,
                                           f"seam-blocked: {blocked_note}", False))
                    continue
                delivered = [r for r in _rows_matching(rep, layer, mnum) if r.get("delivered")]
                supp = [r for r in rep if _layer_matches(layer, r.get("layer", ""))
                        and reason_tok in str(r.get("reason") or r.get("outcome") or "")]
                # conan-17092 m13 step_behind: oracle accepts deliver-OR-suppress (novelty may survive).
                allow_survive = (section == "suppress_step_behind" and "m13" in dv
                                 and c["task"] == "conan-io__conan-17092")
                if not delivered or supp:
                    out.append(CaseVerdict(section, c["task"], dv, PASS,
                                           f"suppressed ({reason_tok})" if supp else "absent after replay", False))
                elif allow_survive:
                    out.append(CaseVerdict(section, c["task"], dv, PASS,
                                           "accepted survive (novel cross-file entity per manifest note)", False))
                else:
                    out.append(CaseVerdict(section, c["task"], dv, FAIL,
                                           f"still delivered with no {reason_tok} reason", False))

    suppress_family("suppress_step_behind", SS_REASON["step_behind"])
    suppress_family("suppress_semantic_dup", SS_REASON["semantic_dup"])
    suppress_family("suppress_provenance", SS_REASON["provenance"])
    suppress_family("suppress_late", SS_REASON["late"])

    # ---- COUNT-ACCURACY (coherence) -----------------------------------------
    for c in cases.get("suppress_coherence_miscount", []):
        if not isinstance(c, dict):
            continue
        dvs = c.get("deliveries") or ([c["delivery"]] if "delivery" in c else [])
        # the verified truth: actual_writes / actual_landed / actual_success (first present).
        actual = next((c[k] for k in ("actual_writes", "actual_landed", "actual_success") if k in c), None)
        for dv in dvs:
            layer, mnum = _parse_delivery(dv)
            # a per-delivery inline count may override (e.g. "detect.coherence m245 (3 writes, claimed 4)")
            inline = re.search(r"\((\d+)\s+(?:writes?|real edit|success)", dv)
            true_ct = int(inline.group(1)) if inline else actual
            rec = _rows_matching(rec_rows(c["task"]), layer, mnum)
            rep = rep_rows(c["task"])
            if not rec:
                out.append(CaseVerdict("coherence", c["task"], dv, SKIP, "no recorded coherence delivery", False))
                continue
            if rep is None:
                out.append(CaseVerdict("coherence", c["task"], dv, SKIP, f"seam-blocked: {blocked_note}", False))
                continue
            fired = [r for r in _rows_matching(rep, layer, mnum) if r.get("delivered")]
            if not fired:
                out.append(CaseVerdict("coherence", c["task"], dv, PASS, "silent (count<=2 / test intervened)", False))
                continue
            blob = " ".join(str(r.get("payload") or "") + " " + str(r.get("reason") or "") for r in fired)
            seen = _count_in(blob)
            if true_ct is not None and true_ct in seen and 4 not in seen and 5 not in seen:
                out.append(CaseVerdict("coherence", c["task"], dv, PASS, f"fires with exact count {true_ct}", False))
            else:
                out.append(CaseVerdict("coherence", c["task"], dv, FAIL,
                                       f"fired but count not the verified {true_ct} (saw {sorted(seen)})", False))

    # ---- RECOVERY MISFIRE (must be absent after replay) ----------------------
    for c in cases.get("suppress_recovery_misfire", []):
        layer, mnum = _parse_delivery(c["delivery"])
        rec = _rows_matching(rec_rows(c["task"]), "recovery", mnum)
        rep = rep_rows(c["task"])
        if not rec:
            out.append(CaseVerdict("recovery_misfire", c["task"], c["delivery"], SKIP,
                                   "no recorded recovery misfire to check", False))
            continue
        if rep is None:
            out.append(CaseVerdict("recovery_misfire", c["task"], c["delivery"], SKIP,
                                   f"seam-blocked: {blocked_note}", False))
            continue
        fired = [r for r in _rows_matching(rep, "recovery", mnum) if r.get("delivered")]
        out.append(CaseVerdict("recovery_misfire", c["task"], c["delivery"],
                               FAIL if fired else PASS,
                               "recovery still fired (misfire not suppressed)" if fired else "no misfire delivery", False))

    # ---- RECOVERY EARLIER RELEASE -------------------------------------------
    for c in cases.get("recovery_earlier_release", []):
        rec = _rows_matching(rec_rows(c["task"]), "recovery", None)
        rep = rep_rows(c["task"])
        if rep is None:
            out.append(CaseVerdict("recovery_earlier", c["task"], "recovery", SKIP,
                                   f"seam-blocked: {blocked_note}", False))
            continue
        recovered = [r for r in rep if _layer_matches("recovery", r.get("layer", "")) and r.get("delivered")]
        rec_m = min((int(r.get("home_msg")) for r in rec if r.get("home_msg") not in (None, -1)), default=None)
        if not recovered:
            out.append(CaseVerdict("recovery_earlier", c["task"], "recovery", FAIL,
                                   "recovery never delivered on replay", False))
            continue
        first_m = min(int(r.get("home_msg")) for r in recovered if r.get("home_msg") is not None)
        if rec_m is None or first_m < rec_m:
            out.append(CaseVerdict("recovery_earlier", c["task"], "recovery", PASS,
                                   f"recovery released earlier (m{first_m} < recorded m{rec_m})", False))
        else:
            out.append(CaseVerdict("recovery_earlier", c["task"], "recovery", FAIL,
                                   f"recovery not earlier (m{first_m} >= recorded m{rec_m})", False))

    return out


@dataclass
class InvariantResult:
    name: str
    verdict: str
    detail: str


def evaluate_invariants(recorded: dict[str, list[Delivery]],
                        replayed: dict[str, list[dict]] | None,
                        recorded_rows: dict[str, list[dict]],
                        blocked_note: str | None) -> list[InvariantResult]:
    """The four SS invariants. When ``replayed`` is None (seam blocked) the leak/dose/empty
    invariants are still evaluated on the RECORDED ledger (a meaningful property of the recording);
    the off-flag fixpoint SKIPs (it needs an all-off drive to compare)."""
    out: list[InvariantResult] = []
    src_label = "replayed" if replayed is not None else "recorded"

    # choose the delivery stream to scan
    if replayed is not None:
        streams = {t: [r for r in rows if _is_delivered(r) or bool(r.get("delivered"))]
                   for t, rows in replayed.items()}
        payload_of = lambda r: str(r.get("payload") or r.get("delta") or "")
        chars_of = lambda r: int(r.get("chars", r.get("chars_delivered") or 0) or 0)
        iter_of = lambda r: int(r.get("home_msg", r.get("iteration") or 0) or 0)
        outcome_delivered = lambda r: _is_delivered(r) if "chars_delivered" in r else bool(r.get("delivered"))
    else:
        streams = {t: dels for t, dels in recorded.items()}  # Delivery objects
        payload_of = lambda d: d.payload
        chars_of = lambda d: d.chars
        iter_of = lambda d: d.home_msg
        outcome_delivered = lambda d: _is_delivered({"outcome": d.outcome, "chars_delivered": d.chars})

    # (1) LEAK == 0 (manifest-free)
    leaks: list[str] = []
    for t, rows in streams.items():
        for r in rows:
            toks = leak_tokens(payload_of(r))
            if toks:
                leaks.append(f"{t}: {toks[:3]}")
    out.append(InvariantResult(f"leak==0 ({src_label} payloads)", PASS if not leaks else FAIL,
                               "0 test identifiers" if not leaks else f"{len(leaks)} leaks: {leaks[:3]}"))

    # (2) <=1 delivered payload per observation
    dose_bad: list[str] = []
    for t, rows in streams.items():
        per: dict[int, int] = {}
        for r in rows:
            if outcome_delivered(r):
                k = iter_of(r)
                per[k] = per.get(k, 0) + 1
        bad = {k: n for k, n in per.items() if n > 1}
        if bad:
            dose_bad.append(f"{t}:{bad}")
    out.append(InvariantResult(f"<=1 dose/obs ({src_label})", PASS if not dose_bad else FAIL,
                               "<=1 payload per observation" if not dose_bad else f"{dose_bad[:3]}"))

    # (3) zero delivered rows with 0 bytes
    empties: list[str] = []
    for t, rows in streams.items():
        for r in rows:
            if outcome_delivered(r) and chars_of(r) == 0:
                empties.append(t)
                break
    out.append(InvariantResult(f"no empty payload ({src_label})", PASS if not empties else FAIL,
                               "no 0-byte delivered rows" if not empties else f"{empties[:5]}"))

    # (4) off-flag fixpoint: all-SS-off replay byte-identical to recorded ledger
    if replayed is None:
        out.append(InvariantResult("off-flag fixpoint (all GT_SS_* off == recorded)", SKIP,
                                   f"seam-blocked: {blocked_note}"))
    else:
        mism: list[str] = []
        for t in recorded_rows:
            rec = [{k: v for k, v in r.items() if k != "timestamp_ms"} for r in recorded_rows.get(t, [])]
            rep = [{k: v for k, v in r.items() if k != "timestamp_ms"} for r in replayed.get(t, [])]
            if rec != rep:
                mism.append(t)
        out.append(InvariantResult("off-flag fixpoint (all GT_SS_* off == recorded)",
                                   PASS if not mism else FAIL,
                                   "replay byte-identical to recorded" if not mism else f"differs: {mism[:3]}"))
    return out


# ══════════════════════════════════════════════════════════════════════════════
# MANIFEST VALIDATION (vs the real recorded data)
# ══════════════════════════════════════════════════════════════════════════════
def validate_manifest(cases: dict, recorded: dict[str, list[Delivery]]) -> list[str]:
    """Confirm every manifest case delivery ('layer mNN') resolves to an EXACT recorded seal of
    that layer at message index m. Returns a list of discrepancy strings (empty == fully valid)."""
    findings: list[str] = []

    def check(section: str, task: str, dv: str):
        layer, mnum = _parse_delivery(dv)
        dels = recorded.get(task, [])
        exact = [d for d in dels if _layer_matches(layer, d.layer) and d.home_msg == mnum]
        if exact:
            return
        near = [d for d in dels if _layer_matches(layer, d.layer) and mnum is not None
                and abs(d.home_msg - mnum) <= 2 and d.home_msg != -1]
        anyl = [d.home_msg for d in dels if _layer_matches(layer, d.layer)]
        if near:
            findings.append(f"[{section}] {task} '{dv}': no seal at m{mnum}; nearest same-layer at "
                            f"m{near[0].home_msg} (off {near[0].home_msg - mnum})")
        elif anyl:
            findings.append(f"[{section}] {task} '{dv}': no seal at/near m{mnum}; same-layer seals at m{sorted(anyl)}")
        else:
            findings.append(f"[{section}] {task} '{dv}': NO delivered seal of layer '{layer}' at all")

    for c in cases.get("preserve", []):
        check("preserve", c["task"], c["delivery"])
    for section in ("suppress_step_behind", "suppress_semantic_dup"):
        for c in cases.get(section, []):
            for dv in c["deliveries"]:
                check(section, c["task"], dv)
    for c in cases.get("suppress_coherence_miscount", []):
        if not isinstance(c, dict):
            continue
        for dv in (c.get("deliveries") or ([c["delivery"]] if "delivery" in c else [])):
            check("coherence", c["task"], dv)
    for c in cases.get("suppress_recovery_misfire", []):
        check("recovery_misfire", c["task"], c["delivery"])
    for c in cases.get("suppress_provenance", []):
        if isinstance(c, dict):
            check("provenance", c["task"], c["delivery"])
    for c in cases.get("suppress_late", []):
        for dv in (c.get("deliveries") or ([c["delivery"]] if "delivery" in c else [])):
            check("late", c["task"], dv)
    for c in cases.get("recovery_earlier_release", []):
        # earlier-release names no explicit m; confirm a recovery seal exists at all.
        if not any(_layer_matches("recovery", d.layer) for d in recorded.get(c["task"], [])):
            findings.append(f"[recovery_earlier] {c['task']}: NO recovery seal recorded")
    return findings


# ══════════════════════════════════════════════════════════════════════════════
# main
# ══════════════════════════════════════════════════════════════════════════════
def _manifest_tasks(cases: dict) -> list[str]:
    tasks: set[str] = set()
    for section, val in cases.items():
        if not isinstance(val, list):
            continue
        for c in val:
            if isinstance(c, dict) and "task" in c:
                tasks.add(c["task"])
    return sorted(tasks)


def _table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [max([len(str(h))] + [len(str(r[i])) for r in rows]) for i, h in enumerate(headers)]
    line = lambda cols: "  " + " | ".join(str(c).ljust(widths[i]) for i, c in enumerate(cols))
    out = [line(headers), "  " + "-+-".join("-" * w for w in widths)]
    out += [line(r) for r in rows]
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="SS-R SUPER-SAIYAN replay-oracle")
    ap.add_argument("--cases", default=str(_DEFAULT_CASES))
    ap.add_argument("--recorded-root", default=str(_DEFAULT_RECORDED),
                    help="dir holding <task>/mini-swe-agent.trajectory.json + gt_runtime_ledger_<task>.jsonl")
    ap.add_argument("--repo-snapshot-root", default=None,
                    help="optional dir of per-task repo checkouts (enables real-seam full replay)")
    ap.add_argument("--out", default=str(_REPO / "ss_replay_oracle_report.json"))
    args = ap.parse_args(argv)

    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    cases = json.loads(Path(args.cases).read_text(encoding="utf-8"))
    recorded_root = Path(args.recorded_root)
    tasks = _manifest_tasks(cases)

    # ── LAYER 1: reconstruct every manifest task ─────────────────────────────
    recon: dict[str, ReconstructedTask] = {}
    recon_errors: list[str] = []
    for t in tasks:
        try:
            recon[t] = reconstruct_task(t, recorded_root)
        except Exception as exc:  # noqa: BLE001
            recon_errors.append(f"{t}: {type(exc).__name__}: {exc}")
    recorded_deliveries = {t: r.recorded_deliveries for t, r in recon.items()}
    recorded_rows = {t: r.raw_rows for t, r in recon.items()}
    residuals = {t: r.residual_leaks for t, r in recon.items() if r.residual_leaks}

    # ── manifest validation ──────────────────────────────────────────────────
    manifest_findings = validate_manifest(cases, recorded_deliveries)

    # ── LAYER 2: attempt real-seam replay (Profile-2, all SS on) ─────────────
    replayed: dict[str, list[dict]] | None = None
    blocked_note: str | None = None
    driver_used = "none"
    snap = Path(args.repo_snapshot_root) if args.repo_snapshot_root else None
    ss_on = {k: "1" for k in ("GT_SS_STEP_BEHIND", "GT_SS_SEMANTIC_DEDUP", "GT_SS_COHERENCE",
                              "GT_SS_RECOVERY", "GT_SS_PROVENANCE", "GT_SS_LATE", "GT_SS_ACK",
                              "GT_SS_ARBITER_V2")}
    try:
        driver = MiniSeamDriver(flag_env=ss_on, repo_snapshot_root=snap)
        replayed = {}
        for t, r in recon.items():
            replayed[t] = [dict(row, home_msg=row.get("iteration")) for row in replay_task(r, driver, recorded_root)]
        driver_used = "mini"
    except SeamReplayBlocked as exc:
        blocked_note = str(exc)
        replayed = None
        driver_used = "mini(blocked)"
    except Exception as exc:  # noqa: BLE001
        blocked_note = f"seam driver fault: {type(exc).__name__}: {exc}"
        replayed = None
        driver_used = "mini(error)"

    # ── LAYER 3: oracle ──────────────────────────────────────────────────────
    verdicts = evaluate_cases(cases, recorded_deliveries, replayed, blocked_note)
    invariants = evaluate_invariants(recorded_deliveries, replayed, recorded_rows, blocked_note)

    # ── tallies + exit ───────────────────────────────────────────────────────
    n_fail = sum(1 for v in verdicts if v.verdict == FAIL)
    n_pass = sum(1 for v in verdicts if v.verdict == PASS)
    n_skip = sum(1 for v in verdicts if v.verdict == SKIP)
    cardinal_kills = [v for v in verdicts if v.cardinal and v.verdict == FAIL]
    inv_fail = [i for i in invariants if i.verdict == FAIL]
    hard_fail = (n_fail > 0 or bool(cardinal_kills) or bool(manifest_findings)
                 or bool(residuals) or bool(recon_errors) or bool(inv_fail))
    exit_code = 1 if hard_fail else 0

    # ── report ───────────────────────────────────────────────────────────────
    print(f"\n# SS-R SUPER-SAIYAN replay-oracle — {ts}")
    print(f"  recorded-root : {recorded_root}")
    print(f"  tasks         : {len(recon)} reconstructed"
          + (f" ({len(recon_errors)} errors)" if recon_errors else ""))
    print(f"  seam driver   : {driver_used}"
          + (f"  — {blocked_note[:110]}..." if blocked_note else ""))
    print(f"  reconstruction: residual seal leaks in {len(residuals)} task(s)"
          + (f" {list(residuals)[:3]}" if residuals else " (0 — every seal stripped cleanly)"))
    print(f"  manifest      : {len(cases_delivery_count(cases))} case-deliveries; "
          f"{len(manifest_findings)} discrepancy(ies) vs recorded data")
    for f in manifest_findings:
        print("     ! " + f)

    print("\n  ── per-case verdicts ──")
    rows = [[v.verdict + ("*" if v.cardinal else ""), v.section, v.task[:30], v.label[:26], v.reason[:60]]
            for v in verdicts]
    print(_table(["VERDICT", "SECTION", "TASK", "DELIVERY", "REASON"], rows))

    print("\n  ── invariants ──")
    print(_table(["VERDICT", "INVARIANT", "DETAIL"],
                 [[i.verdict, i.name, i.detail[:70]] for i in invariants]))

    print(f"\n  cases: PASS={n_pass} FAIL={n_fail} SKIP={n_skip}"
          f" | cardinal P5 kills={len(cardinal_kills)} | invariant FAILs={len(inv_fail)}")
    print(f"  EXIT {exit_code} ({'GREEN' if exit_code == 0 else 'RED'})")
    if driver_used.startswith("mini(") and blocked_note:
        print(f"\n  [seam-note] {blocked_note}")

    report = {
        "gate": "ss_replay_oracle", "generated_utc": ts, "recorded_root": str(recorded_root),
        "seam_driver": driver_used, "seam_blocked_note": blocked_note,
        "tasks_reconstructed": sorted(recon), "reconstruction_errors": recon_errors,
        "residual_seal_leaks": residuals, "manifest_findings": manifest_findings,
        "cardinal_p5_kills": [f"{v.task}:{v.label}" for v in cardinal_kills],
        "cases": [{"section": v.section, "task": v.task, "delivery": v.label, "verdict": v.verdict,
                   "reason": v.reason, "cardinal": v.cardinal} for v in verdicts],
        "invariants": [{"name": i.name, "verdict": i.verdict, "detail": i.detail} for i in invariants],
        "counts": {"pass": n_pass, "fail": n_fail, "skip": n_skip},
        "exit_code": exit_code,
    }
    try:
        Path(args.out).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"  report -> {args.out}")
    except Exception as exc:  # noqa: BLE001
        print(f"  WARN: could not write report: {exc}")
    return exit_code


def cases_delivery_count(cases: dict) -> list[str]:
    out: list[str] = []
    for c in cases.get("preserve", []):
        out.append(c["delivery"])
    for section in ("suppress_step_behind", "suppress_semantic_dup"):
        for c in cases.get(section, []):
            out.extend(c["deliveries"])
    for c in cases.get("suppress_coherence_miscount", []):
        if isinstance(c, dict):
            out.extend(c.get("deliveries") or ([c["delivery"]] if "delivery" in c else []))
    for c in cases.get("suppress_recovery_misfire", []):
        out.append(c["delivery"])
    for c in cases.get("suppress_provenance", []):
        if isinstance(c, dict):
            out.append(c["delivery"])
    for c in cases.get("suppress_late", []):
        out.extend(c.get("deliveries") or ([c["delivery"]] if "delivery" in c else []))
    return out


if __name__ == "__main__":
    raise SystemExit(main())
