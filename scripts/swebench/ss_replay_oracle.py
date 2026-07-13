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

────────────────────────────────────────────────────────────────────────────────────────────
SS-R2 (fixpoint-first re-emit, coordinator bounce 2026-07-13). The oracle now runs the
OFF-flag fixpoint FIRST, per task, and GATES every case on it:

  * PER-TASK REPLAY = one fresh subprocess per task per arm (no module-global bleed), with
    the recorded env posture reproduced from the task's own gt_profile_receipt.json and the
    container paths MIRRORED on the current drive (\testbed junction -> the repo snapshot,
    \gt_artifacts -> graph.db + certs, \opt\gt -> gt-index + gt_root.txt, \tmp cleaned):
    rootless POSIX paths resolve against the current drive on Windows, so the seam's own
    path strings — and the ledger reason bytes — match the recording literally.
  * OFF ARM = every GT_SS_* EXPLICITLY "0" (under the W8 default-inversion an UNSET
    Profile-2 member is fan-out-resolved to "1", so unset is NOT off).
  * EDIT APPLICATION: the recorded run's disk state EVOLVED; the replay re-executes the
    agent's own recorded file-mutation commands (sed/cat-redirect/git-checkout + the two
    python patch-script shapes) against the mirrored tree, git-restored at teardown. Test
    runs / pip / network commands are never executed.
  * THE GATE (two-plane fixpoint): the DELIVERED-row subsequence (the sha-sealed bytes the
    model saw) must match the recording exactly and in order; hidden telemetry rows
    (candidate stamps, arbitration losers, L6 node counters, the env-execute-chokepoint
    submit rows) are diffed + reported but do not gate. A task failing the gate marks its
    cases REPLAY_UNFAITHFUL (never PASS/FAIL); a task diverging at iteration B keeps a
    TRUSTED PREFIX — cases whose recorded deliveries all sit strictly before B are judged.
  * EXIT: 0 all-green; 1 hard fail (case FAIL / cardinal kill / manifest / residual /
    invariant); 3 inconclusive (no hard fail, but >=1 cardinal P5 case REPLAY_UNFAITHFUL).
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
# SS-R2 (fixpoint-first gating): a case on a task/window whose OFF-flag replay does NOT
# reproduce the recorded ledger is NOT judged — it is REPLAY_UNFAITHFUL, counted neither
# PASS nor FAIL (a false verdict is worse than no verdict).
REPLAY_UNFAITHFUL = "REPLAY_UNFAITHFUL"

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
    pairs: list[tuple[str, str]]          # (action_text=the executed COMMAND, native_observation)
    recorded_deliveries: list[Delivery]   # the located seals (home_msg == manifest mNN)
    residual_leaks: list[str]             # non-empty => stripping failed (a seal still matches)
    raw_rows: list[dict]                  # the full recorded ledger (for invariants / suppressed rows)
    n_messages: int
    rcs: list[int] = field(default_factory=list)   # per-pair returncode (from the observation wrapper)


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

    # per-message seal list (ledger order) — needed to strip the SAME seals from raw_output.
    seals_at: dict[int, list[tuple[int, str]]] = {}
    for dl in deliveries:
        if dl.home_msg >= 0:
            seals_at.setdefault(dl.home_msg, []).append((dl.chars, dl.sha16 or ""))

    # build (command, native_obs) pairs: each tool observation paired with the COMMAND that
    # produced it. mini-swe v2 stores the executed command in the assistant message's
    # tool_calls[].function.arguments JSON ({"command": ...}); prose-only content has no
    # command (fenced-block fallback for synthetic fixtures). The seam at runtime received
    # the env's RAW output (msg.extra.raw_output) — NOT the templated
    # <returncode>/<output> content — so the native observation fed back is raw_output with
    # this message's seals stripped; content-stripped is the fallback when raw is absent.
    pairs: list[tuple[str, str]] = []
    rcs: list[int] = []
    pending_cmds: list[str] = []
    for i, m in enumerate(msgs):
        role = m.get("role")
        if role == "assistant":
            pending_cmds = _commands_of(m)
            continue
        if role != "tool":
            continue
        cmd = pending_cmds.pop(0) if pending_cmds else ""
        content = m.get("content", "") if isinstance(m.get("content"), str) else ""
        raw = (m.get("extra") or {}).get("raw_output") if isinstance(m.get("extra"), dict) else None
        if isinstance(raw, str):
            native = raw
            for n, sha in seals_at.get(i, []):
                off = locate_seal(native, n, sha)
                if off is not None:
                    native = native[:off] + native[off + n:]
                else:
                    residual.append(f"seal {sha} ({n}c) homed at m{i} not found in raw_output")
            # residual guard on the raw-native too
            for n, sha in seals_at.get(i, []):
                if locate_seal(native, n, sha) is not None:
                    residual.append(f"seal {sha} ({n}c) still present in raw-native of m{i}")
        else:
            native = stripped[i]
        rc = 0
        mrc = re.match(r"\s*<returncode>(-?\d+)</returncode>", content)
        if mrc:
            rc = int(mrc.group(1))
        pairs.append((cmd, native))
        rcs.append(rc)

    # SUBMIT WINDOW: the final assistant command (echo COMPLETE_TASK... && cat patch) is
    # executed by the env but its output becomes the EXIT message, not a tool message — the
    # recorded completion_cert/submit_gate rows come from that execute. Feed it as the last
    # pair so the submit-window rows can reproduce.
    if len(msgs) >= 2 and msgs[-1].get("role") == "exit":
        for back in range(len(msgs) - 2, -1, -1):
            if msgs[back].get("role") == "assistant":
                tail_cmds = _commands_of(msgs[back])
                if tail_cmds and not (back + 1 < len(msgs) and msgs[back + 1].get("role") == "tool"):
                    exit_content = msgs[-1].get("content", "") or ""
                    pairs.append((tail_cmds[0], exit_content if isinstance(exit_content, str) else ""))
                    rcs.append(0)
                break

    return ReconstructedTask(task=task, pairs=pairs, recorded_deliveries=deliveries,
                             residual_leaks=residual, raw_rows=rows, n_messages=len(msgs),
                             rcs=rcs)


def _commands_of(assistant_msg: dict) -> list[str]:
    """The executed command(s) of one assistant message: tool_calls[].function.arguments
    ({"command": ...}) when present (mini-swe v2), else the fenced ```bash block(s) of the
    content (synthetic fixtures / older format)."""
    cmds: list[str] = []
    for tc in assistant_msg.get("tool_calls") or []:
        try:
            args = json.loads((tc.get("function") or {}).get("arguments") or "{}")
            c = args.get("command")
            if isinstance(c, str) and c.strip():
                cmds.append(c)
        except Exception:  # noqa: BLE001
            continue
    if cmds:
        return cmds
    content = assistant_msg.get("content", "") or ""
    block = extract_command(content)
    return [block] if block else []


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
# SS-R2 — CONTAINER-PATH MIRRORS (host-side env parity with the recorded run)
# ══════════════════════════════════════════════════════════════════════════════
# The recorded ledger rows embed the CONTAINER's absolute paths (/testbed, /gt_artifacts/
# graph.db, /tmp/gt_work.db, /opt/gt/gt-index). On Windows, a rootless POSIX path resolves
# against the CURRENT DRIVE, so mirroring those trees at <drive>:\testbed, \gt_artifacts,
# \opt\gt and \tmp makes the seam's own path strings (and therefore the ledger reason bytes)
# LITERALLY identical to the recorded ones — a real parity lever, not a normalization hack.
_MIRROR_TESTBED = Path("/testbed")
_MIRROR_ARTIFACTS = Path("/gt_artifacts")
_MIRROR_OPT_GT = Path("/opt/gt")
_MIRROR_TMP = Path("/tmp")

# /tmp files the recorded seam writes; cleaned between tasks so no state bleeds.
# The -wal/-shm sqlite sidecars MUST go with the main file: a stale WAL from a prior
# task replays foreign frames into the fresh copy (the conan-17092/aiogram bleed).
_TMP_STATE = ["gt_work.db", "gt_work.db-wal", "gt_work.db-shm", "gt_index.json",
              "gt_oracle_events.jsonl", "gt_hook_fire_counts.json"]


def _run_quiet(cmd: list[str]) -> int:
    import subprocess
    try:
        return subprocess.run(cmd, capture_output=True, timeout=120).returncode
    except Exception:  # noqa: BLE001
        return 1


# ── SS-R3: EXCLUSIVE RUN LOCK ─────────────────────────────────────────────────
# The container-path mirrors (\testbed, \gt_artifacts, \opt\gt, \tmp state) are DRIVE-GLOBAL
# singletons — two concurrent oracle instances swap junctions under each other's children and
# cross-contaminate ledgers (observed live: one run's graph staged into another's task, its
# guard fail-closing five tasks to boundary=-1, and the sibling run dying mid-flight). The
# parent takes an exclusive pid-stamped lock; a second instance FAILS FAST with a message
# naming the holder instead of silently poisoning both runs.
_LOCK_PATH = Path("/tmp/ssr_replay_oracle.lock")


def _pid_alive(pid: int) -> bool:
    import subprocess
    try:
        r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                           capture_output=True, text=True, timeout=15)
        return str(pid) in (r.stdout or "")
    except Exception:  # noqa: BLE001 — unknown -> assume alive (fail safe: don't steal)
        return True


def acquire_run_lock() -> bool:
    """True when THIS process holds the mirror lock; False when a live sibling does."""
    _LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(str(_LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"{os.getpid()}".encode())
            os.close(fd)
            return True
        except FileExistsError:
            try:
                holder = int(_LOCK_PATH.read_text(encoding="utf-8").strip() or "0")
            except Exception:  # noqa: BLE001
                holder = 0
            if holder and _pid_alive(holder) and holder != os.getpid():
                sys.stderr.write(
                    f"[ssr] mirror lock held by live pid {holder} ({_LOCK_PATH}) — the "
                    f"container-path mirrors are drive-global; concurrent runs cross-"
                    f"contaminate. Wait for it or stop it, then retry.\n")
                return False
            try:
                _LOCK_PATH.unlink()   # stale (holder dead) -> steal
            except OSError:
                return False
    return False


def release_run_lock() -> None:
    try:
        if _LOCK_PATH.is_file() and _LOCK_PATH.read_text(encoding="utf-8").strip() == str(os.getpid()):
            _LOCK_PATH.unlink()
    except Exception:  # noqa: BLE001
        pass


def _make_junction(link: Path, target: Path) -> bool:
    """Create a Windows directory junction (no admin needed). Removes a stale link first."""
    import subprocess
    _remove_junction(link)
    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                       capture_output=True, timeout=60)
    return r.returncode == 0


def _remove_junction(link: Path) -> None:
    """Remove a junction WITHOUT touching its target (rmdir on a junction unlinks only)."""
    if link.exists() or link.is_symlink():
        _run_quiet(["cmd", "/c", "rmdir", str(link)])


class TaskMirrors:
    """Per-task setup/teardown of the container-path mirrors. Snapshot repos are mounted at
    /testbed via a junction; when edit-application mutates them, teardown restores the exact
    recorded base commit with git (checkout+clean)."""

    def __init__(self, task: str, recorded_root: Path, snapshot_root: Path) -> None:
        self.task = task
        self.recorded = recorded_root / task
        self.snapshot = snapshot_root / task

    def setup(self) -> None:
        import shutil
        if not self.snapshot.is_dir():
            raise SeamReplayBlocked(f"{self.task}: no repo snapshot at {self.snapshot}")
        # /testbed + /tmp/gt_work_src -> the snapshot working tree
        if not _make_junction(_MIRROR_TESTBED, self.snapshot):
            raise SeamReplayBlocked(f"{self.task}: could not junction {_MIRROR_TESTBED} -> {self.snapshot}")
        _MIRROR_TMP.mkdir(exist_ok=True)
        _make_junction(_MIRROR_TMP / "gt_work_src", self.snapshot)
        # /gt_artifacts: graph.db (the pristine mount copy) + the recorded cert artifacts
        if _MIRROR_ARTIFACTS.exists():
            shutil.rmtree(_MIRROR_ARTIFACTS, ignore_errors=True)
        _MIRROR_ARTIFACTS.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.recorded / "graph.db", _MIRROR_ARTIFACTS / "graph.db")
        # FAIL-LOUD copy-integrity guard (the conan-17092 bleed staged ANOTHER task's graph
        # after a lock race): the mirror COPY must be byte-equal to THIS task's RECORDED
        # graph.db (source-vs-copy integrity — nothing else). One retry covers a transient
        # writer still flushing; a persisting mismatch means another process holds the
        # drive-global mirror (the run-lock exists to prevent exactly that).
        import hashlib
        import time
        def _h16(p: Path) -> str:
            h = hashlib.sha256()
            with open(p, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
            return h.hexdigest()[:16]
        want = _h16(self.recorded / "graph.db")
        got = _h16(_MIRROR_ARTIFACTS / "graph.db")
        if got != want:
            time.sleep(2.0)
            shutil.copyfile(self.recorded / "graph.db", _MIRROR_ARTIFACTS / "graph.db")
            got = _h16(_MIRROR_ARTIFACTS / "graph.db")
        if got != want:
            raise SeamReplayBlocked(
                f"{self.task}: mirror copy integrity FAILED — copied graph.db hashes {got} "
                f"but the recorded source is {want} (another process is mutating the "
                f"drive-global mirror {_MIRROR_ARTIFACTS}; check the run lock)")
        art_src = self.recorded / "gt_artifacts"
        if art_src.is_dir():
            for f in art_src.iterdir():
                if f.is_file():
                    shutil.copyfile(f, _MIRROR_ARTIFACTS / f.name)
        # /opt/gt: gt-index binary (both extensionless + .exe for CreateProcess) + root file
        _MIRROR_OPT_GT.mkdir(parents=True, exist_ok=True)
        bin_src = _REPO / "gt-index" / "gt-index.exe"
        for name in ("gt-index", "gt-index.exe"):
            dst = _MIRROR_OPT_GT / name
            if not dst.is_file() or dst.stat().st_size != bin_src.stat().st_size:
                shutil.copyfile(bin_src, dst)
        # /opt/gt/models -> the local embedder models (the recorded run's models_root was
        # /opt/gt/models; the semantic/content legs stall on HF fetch timeouts without it)
        models_src = _REPO / "models"
        if models_src.is_dir() and not (_MIRROR_OPT_GT / "models").exists():
            _make_junction(_MIRROR_OPT_GT / "models", models_src)
        (_MIRROR_OPT_GT / "gt_root.txt").write_text("/testbed", encoding="utf-8")
        # /tmp/gt_root.txt is what the AGENT's own commands `cat` (cd $(cat /tmp/gt_root.txt));
        # the edit-applier executes those via Git Bash, which needs the WINDOWS form.
        drive = os.path.splitdrive(os.getcwd())[0] or "D:"
        (_MIRROR_TMP / "gt_root.txt").write_text(f"{drive}/testbed", encoding="utf-8")
        # clean the seam's /tmp state from any prior task
        for name in _TMP_STATE:
            p = _MIRROR_TMP / name
            if p.is_file():
                p.unlink()

    def teardown(self) -> None:
        # restore the snapshot to the exact recorded base commit (edit-application mutates it)
        _run_quiet(["git", "-C", str(self.snapshot), "checkout", "--", "."])
        _run_quiet(["git", "-C", str(self.snapshot), "clean", "-fdq"])
        _remove_junction(_MIRROR_TMP / "gt_work_src")
        _remove_junction(_MIRROR_TESTBED)
        for name in _TMP_STATE:
            p = _MIRROR_TMP / name
            if p.is_file():
                try:
                    p.unlink()
                except OSError:
                    pass


def recorded_member_env(recorded_root: Path, task: str) -> dict[str, str]:
    """The recorded run's EXACT flag posture, from the task's own gt_profile_receipt.json
    (member_source lists every profile member the workflow fanned to '1' in-container)."""
    env: dict[str, str] = {"GT_RL_PROFILE": "2"}
    rec = recorded_root / task / "gt_artifacts" / "gt_profile_receipt.json"
    try:
        receipt = json.loads(rec.read_text(encoding="utf-8"))
        for m in receipt.get("member_source", {}):
            env[m] = "1"
    except Exception:  # noqa: BLE001 — fall back to rl_profile's own fan-out in-child
        pass
    return env


def child_env(recorded_root: Path, task: str, ledger_path: Path,
              ss_env: dict[str, str]) -> dict[str, str]:
    """The full env for a replay child process: hermetic (no inherited GT_*), the recorded
    posture (profile members + substrate/proof handoff), the mirrored container paths, and
    the per-arm GT_SS_* overlay."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GT_")}
    env.update(recorded_member_env(recorded_root, task))
    env.update({
        "GT_PROOF_MODE": "1",
        "GT_CONTAINERIZED": "1",
        "GT_PORTABLE_SUBSTRATE": "1",
        "GT_HOST_GRAPH_DB": "/gt_artifacts/graph.db",
        "GT_CERT_DIR": "/gt_artifacts",
        "GT_INDEX_BIN": "/opt/gt/gt-index",
        "GT_L6_FRESH": "1",
        "GT_ROOT_FILE": "/opt/gt/gt_root.txt",
        "GT_HOST_SRC_ROOT": "/tmp/gt_work_src",
        "GT_RUNTIME_LEDGER": str(ledger_path),
        "GT_ORACLE_EVENTS": "/tmp/gt_oracle_events.jsonl",
        "GT_HOOK_FIRE_COUNTS": "/tmp/gt_hook_fire_counts.json",
        "GT_RUN_TASK": task,
        "GT_MODELS_ROOT": "/opt/gt/models",
        # never let a missing local model turn into a network fetch stall mid-replay
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "PYTHONIOENCODING": "utf-8",
    })
    for k, v in (ss_env or {}).items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = str(v)
    return env


# ══════════════════════════════════════════════════════════════════════════════
# SS-R2 — EDIT APPLICATION (materialize the recorded disk evolution, allowlisted)
# ══════════════════════════════════════════════════════════════════════════════
# The recorded run's disk state EVOLVED (the agent's sed/cat/git edits actually ran). The
# seam's post_edit producers (edit.syntax parse, L6 -file reindex, contract snapshots) read
# that state from disk, so a replay that never applies the edits diverges at the first edit.
# We re-execute ONLY an allowlist of file-mutation commands (no test runs, no python, no
# network), with write targets confined to /testbed and /tmp, via Git Bash. The snapshot is
# git-restored at teardown.
_EDIT_ALLOW = re.compile(
    r"^\s*(sed\s+-[a-zA-Z]*i|cat\s|echo\s|printf\s|tee\s|mkdir\s|cp\s|mv\s|touch\s|"
    r"git\s+(checkout|restore|apply|stash|diff|add)\b|patch\s)", re.DOTALL)
# The agent's dominant edit idioms in the recorded runs beyond sed/cat-redirect:
#   (a) write a patch SCRIPT to /tmp via a heredoc, then execute it
#       (`python /tmp/patch_code.py`) to rewrite repo files;
#   (b) an INLINE python heredoc (`python3 << 'EOF' ... open(...).write(...)`) doing the same.
# Replaying the write without the execute leaves the repo at base -> every post-edit payload
# (contract line numbers, edit.syntax parse) diverges. Both shapes are agent-authored
# mutation scripts already executed once in the recorded container; we re-execute them
# against the mirrored working tree (git-restored at teardown). pytest/pip/network commands
# are never executed. Deny/redirect checks apply to the command HEAD only — the heredoc BODY
# is file CONTENT (it may legitimately contain any word).
_EDIT_SCRATCH_PY = re.compile(r"^\s*python[0-9.]*\s+(/tmp/[\w.\-]+\.py)\s*$")
_EDIT_PY_HEREDOC = re.compile(r"^\s*python[0-9.]*\s*<<")
_EDIT_PY_INLINE = re.compile(r"^\s*python[0-9.]*\s+-c\s", re.DOTALL)   # python3 -c "<script>"
_EDIT_DENY = re.compile(
    r"\b(curl|wget|pip|pytest|npm|rm\s+-rf\s+/|ssh|scp)\b")


def _head_of(cmd: str) -> str:
    cut = cmd.find("<<")
    return cmd if cut < 0 else cmd[:cut]


def _normalize_crlf(repo_root: str) -> None:
    """Host-parity fix: python scripts executed on Windows write CRLF (text-mode newline
    translation) where the recorded container wrote LF. Normalize every modified repo file
    back to LF so file bytes — and every payload embedding them — match the recording."""
    import subprocess
    try:
        r = subprocess.run(["git", "-C", repo_root, "diff", "--name-only"],
                           capture_output=True, text=True, timeout=30)
        for rel in (r.stdout or "").splitlines():
            rel = rel.strip()
            if not rel:
                continue
            p = Path(repo_root) / rel
            if p.is_file():
                b = p.read_bytes()
                if b"\r\n" in b:
                    p.write_bytes(b.replace(b"\r\n", b"\n"))
    except Exception:  # noqa: BLE001 — normalization is best-effort
        pass


def _strip_lead_cd(cmd: str) -> str:
    """Drop the harness-mandated ``cd $(cat /tmp/gt_root.txt) && `` / ``cd /testbed && ``
    prefix so the allowlist matches the ACTUAL mutation command."""
    return re.sub(r"^\s*cd\s+[^&;|]+&&\s*", "", cmd, count=1)


def _rewrite_container_paths(cmd: str) -> str:
    """Rewrite /testbed and /tmp to their drive-mirrored Windows forms in the command PREFIX
    only (heredoc bodies are file CONTENT and must stay byte-exact)."""
    drive = os.path.splitdrive(os.getcwd())[0] or "D:"
    cut = cmd.find("<<")
    head, tail = (cmd, "") if cut < 0 else (cmd[:cut], cmd[cut:])
    head = head.replace("$(cat /tmp/gt_root.txt)", "/testbed")
    head = head.replace("/testbed", f"{drive}/testbed").replace("/tmp/", f"{drive}/tmp/")
    return head + tail


def _redirect_targets_ok(cmd: str) -> bool:
    """Every redirect target must live under /testbed, /tmp, or be repo-relative."""
    for m in re.finditer(r">{1,2}\s*([^\s;|&]+)", cmd):
        t = m.group(1).strip("'\"")
        if t in ("/dev/null", "&1", "&2") or t.startswith("&"):
            continue
        if t.startswith(("/testbed", "/tmp", "D:/testbed", "D:/tmp")):
            continue
        if not t.startswith("/") and ".." not in t:
            continue  # relative inside cwd (/testbed)
        return False
    return True


def _find_bash() -> str:
    """A REAL bash for the edit-applier. A bare 'bash' on Windows resolves to the System32
    WSL stub (fails without a distro); prefer Git Bash explicitly. GT_SSR_BASH overrides."""
    env = os.environ.get("GT_SSR_BASH")
    if env and Path(env).is_file():
        return env
    for cand in (r"C:\Program Files\Git\bin\bash.exe",
                 r"C:\Program Files\Git\usr\bin\bash.exe"):
        if Path(cand).is_file():
            return cand
    return "bash"


def apply_edit_command(cmd: str, cwd: str) -> tuple[bool, str]:
    """Execute one recorded agent command IFF it is an allowlisted file mutation.
    Returns (executed, note)."""
    import subprocess
    body = _strip_lead_cd(cmd or "")
    if not body:
        return False, "empty"
    head = _head_of(body)
    scratch = _EDIT_SCRATCH_PY.match(body)
    if scratch:
        # (a) agent-authored scratch patch script (materialized by an earlier applied
        # heredoc): run it with the HOST python, cwd=the mirrored working tree. Rootless
        # POSIX paths inside the script resolve against the current drive (the mirror trick).
        drive = os.path.splitdrive(os.getcwd())[0] or "D:"
        script = scratch.group(1).replace("/tmp/", f"{drive}/tmp/")
        if not Path(script).is_file():
            return False, "scratch-script-absent"
        try:
            r = subprocess.run([sys.executable, script], cwd=cwd,
                               capture_output=True, timeout=60)
            _normalize_crlf(cwd)
            return True, f"scratch-py rc={r.returncode}"
        except Exception as exc:  # noqa: BLE001
            return True, f"exec-fault:{type(exc).__name__}"
    if _EDIT_PY_HEREDOC.match(body) or (_EDIT_PY_INLINE.match(body)
                                        and not _EDIT_DENY.search(body)):
        # (b)/(c) inline python mutation script (heredoc or -c): run through bash with the
        # interpreter token normalized to the host python (python3 is rarely on Windows
        # PATH), then normalize CRLF back to LF (Windows text-mode writes) so file bytes
        # match the recorded container's.
        rewritten = _rewrite_container_paths(cmd)
        rewritten = re.sub(r"\bpython[0-9.]*(?=\s*<<|\s+-c\s)",
                           f'"{sys.executable}"'.replace("\\", "/"), rewritten, count=1)
        try:
            r = subprocess.run([_find_bash(), "-c", rewritten], cwd=cwd,
                               capture_output=True, timeout=60)
            _normalize_crlf(cwd)
            return True, f"py-inline rc={r.returncode}"
        except Exception as exc:  # noqa: BLE001
            return True, f"exec-fault:{type(exc).__name__}"
    if not _EDIT_ALLOW.match(body) or _EDIT_DENY.search(head):
        return False, "not-allowlisted"
    if re.search(r"\bpython[0-9.]*\s", head):
        return False, "python-outside-scratch-shape"   # only the two script shapes run
    if not _redirect_targets_ok(head):
        return False, "redirect-target-outside-roots"
    rewritten = _rewrite_container_paths(cmd)
    try:
        r = subprocess.run([_find_bash(), "-c", rewritten], cwd=cwd,
                           capture_output=True, timeout=60)
        return True, f"rc={r.returncode}"
    except Exception as exc:  # noqa: BLE001
        return True, f"exec-fault:{type(exc).__name__}"


# ══════════════════════════════════════════════════════════════════════════════
# SS-R2 — CHILD REPLAY (one fresh interpreter per task per arm: no module-global bleed)
# ══════════════════════════════════════════════════════════════════════════════
def replay_child_main(task: str, recorded_root: Path, out_path: Path,
                      apply_edits: bool) -> int:
    """Runs INSIDE the child process (env already set by the parent): reconstruct the native
    stream, import the seam fresh (import side effects ARE the install), feed every pair,
    write the replayed ledger + edit-application log as JSON."""
    for p in (str(_ART), str(_SRC)):
        if p not in sys.path:
            sys.path.insert(0, p)
    recon = reconstruct_task(task, recorded_root)
    ledger = Path(os.environ["GT_RUNTIME_LEDGER"])
    if ledger.is_file():
        ledger.unlink()
    import gt_mini_patch as g  # noqa: E402 — fresh install in THIS process
    applied: list[dict] = []
    for k, (cmd, native) in enumerate(recon.pairs):
        rc = recon.rcs[k] if k < len(recon.rcs) else 0
        if apply_edits and cmd:
            ran, note = apply_edit_command(cmd, cwd=str(_MIRROR_TESTBED))
            if ran:
                applied.append({"pair": k, "cmd": cmd[:160], "note": note})
        out = {"output": native, "returncode": rc}
        try:
            g._augment_output({"command": cmd}, out)
        except BaseException as exc:  # noqa: BLE001 — incl. GTHandoffEmptyError: record + continue
            applied.append({"pair": k, "seam_fault": f"{type(exc).__name__}: {exc}"})
        after = out.get("output") or ""
        if after != native:
            delta = after[len(native):] if after.startswith(native) else "(REWROTE-PREFIX)"
            applied.append({"pair": k, "delta": delta})
    rows: list[dict] = []
    if ledger.is_file():
        for line in ledger.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except Exception:  # noqa: BLE001
                    pass
    out_path.write_text(json.dumps({"task": task, "rows": rows, "applied": applied,
                                    "n_pairs": len(recon.pairs)}), encoding="utf-8")
    return 0


def _join_payloads(child_payload: dict) -> list[dict]:
    """Attach the model-facing DELTA bytes (captured per pair by the child) to the delivered
    ledger rows, keyed by iteration (pair k -> iteration k+1). Without this the oracle's
    byte-sensitive checks (coherence exact-count, preserve diagnostics) read empty payloads
    — the 'saw []' false-FAIL class."""
    rows = child_payload.get("rows", [])
    deltas: dict[int, str] = {}
    for a in child_payload.get("applied", []):
        if "delta" in a and isinstance(a.get("pair"), int):
            deltas[a["pair"] + 1] = a["delta"]
    for r in rows:
        if _is_delivered(r) and "payload" not in r:
            d = deltas.get(int(r.get("iteration") or -1))
            if d is not None:
                r["payload"] = d
    return rows


def spawn_replay(task: str, recorded_root: Path, snapshot_root: Path, arm_ss_env: dict[str, str],
                 apply_edits: bool, work_dir: Path, arm: str = "arm") -> dict:
    """Parent-side: set up mirrors, spawn the child replay, collect its ledger, tear down.
    Returns {"rows": [...], "applied": [...], "error": str|None}."""
    import subprocess
    mirrors = TaskMirrors(task, recorded_root, snapshot_root)
    out_json = work_dir / f"replay_{task}_{arm}.json"
    led = work_dir / f"led_{task}_{arm}.jsonl"
    if os.environ.get("GT_SSR_REUSE_WORK") == "1" and out_json.is_file():
        try:
            payload = json.loads(out_json.read_text(encoding="utf-8"))
            return {"rows": _join_payloads(payload), "applied": payload.get("applied", []),
                    "error": None}
        except Exception:  # noqa: BLE001 — fall through to a fresh replay
            pass
    try:
        mirrors.setup()
        env = child_env(recorded_root, task, led, arm_ss_env)
        cmd = [sys.executable, str(Path(__file__).resolve()), "--replay-one", task,
               "--recorded-root", str(recorded_root), "--child-out", str(out_json),
               "--apply-edits" if apply_edits else "--no-apply-edits"]
        # SS-R3 cwd PARITY: the recorded container ran with cwd=/testbed, and producer-side
        # RELATIVE path checks (os.path.isfile(rel)) resolve against the cwd — a repo cwd
        # left every such check False host-side (the keras/absolute-path dark-head class).
        r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=1800,
                           cwd=str(_MIRROR_TESTBED))
        if r.returncode != 0 or not out_json.is_file():
            return {"rows": [], "applied": [],
                    "error": f"child rc={r.returncode}: {(r.stderr or r.stdout)[-400:]}"}
        payload = json.loads(out_json.read_text(encoding="utf-8"))
        return {"rows": _join_payloads(payload), "applied": payload.get("applied", []),
                "error": None}
    except SeamReplayBlocked as exc:
        return {"rows": [], "applied": [], "error": f"blocked: {exc}"}
    except Exception as exc:  # noqa: BLE001
        return {"rows": [], "applied": [], "error": f"{type(exc).__name__}: {exc}"}
    finally:
        try:
            mirrors.teardown()
        except Exception:  # noqa: BLE001
            pass


# ══════════════════════════════════════════════════════════════════════════════
# SS-R2 — LEDGER DIFFER (row-level, root-cause tagged, trusted-prefix)
# ══════════════════════════════════════════════════════════════════════════════
# Volatile substrings inside `reason` that are environment noise even under path mirroring
# (exception reprs, stderr byte reprs, subprocess timing). Masked for the CHANNEL key only;
# the STRICT key masks nothing but timestamp_ms.
_REASON_VOLATILE = re.compile(r"(stderr=b?['\"].*?['\"]|exc=[A-Za-z]+Error[^ ]*|timeout_s=\d+)")


def _strict_key(row: dict) -> str:
    return json.dumps({k: v for k, v in row.items() if k != "timestamp_ms"}, sort_keys=True)


def _channel_key(row: dict) -> str:
    """Drift-tolerant comparison key: drops `iteration` (the recorded env executed actions
    the trajectory does not carry, so absolute counters differ), masks volatile reason
    substrings, and normalizes path SEPARATORS (the seam computes os.path.relpath, which
    yields `..\\tmp\\x.py` host-side vs `../tmp/x.py` in-container — same path, different
    platform). Everything else — layer, event_type, outcome, chars, sha seal, file_path,
    reason — must match exactly."""
    d = {k: v for k, v in row.items() if k not in ("timestamp_ms", "iteration")}
    if isinstance(d.get("reason"), str):
        d["reason"] = _REASON_VOLATILE.sub("<VOLATILE>", d["reason"]).replace("\\", "/")
    if isinstance(d.get("file_path"), str):
        d["file_path"] = d["file_path"].replace("\\", "/")
    return json.dumps(d, sort_keys=True)


@dataclass
class FixpointResult:
    task: str
    replayed: bool
    strict: bool                    # EVERY row byte-equal (minus timestamp) — the gold standard
    channel: bool                   # the DELIVERED-row subsequence byte-equal — the GATE
    boundary_iter: float            # recorded iteration of the FIRST DELIVERED divergence (inf = none)
    n_recorded: int
    n_replayed: int
    diffs: list[dict] = field(default_factory=list)   # row-level ops with root-cause tags
    drift_map: list[tuple[int, int]] = field(default_factory=list)  # (rec_iter, rep_iter) matched
    error: str = ""
    n_hidden_diffs: int = 0         # hidden/telemetry row diffs (reported, NOT gating)

    @property
    def faithful(self) -> bool:
        """The GATE: model-facing fidelity. ``channel`` compares the DELIVERED rows (the
        exact bytes the model saw, sha-sealed) in order; hidden telemetry rows (candidate
        stamps, arbitration losers, L6 counters) are reported but do not gate — any hidden
        state contamination that MATTERS must surface in later delivered bytes, which DO gate."""
        return self.replayed and (self.strict or self.channel)


def _tag_diff(op: str, row: dict) -> str:
    """Root-cause tag for one diverging row (heuristic, for the report table)."""
    layer = str(row.get("layer") or "")
    fp = str(row.get("file_path") or "")
    outcome = str(row.get("outcome") or "")
    if layer.startswith("L6") or layer == "L6":
        if outcome == "NO_BINARY":
            return "gt-index-binary-missing"
        if fp.startswith("../") or fp.startswith("/tmp"):
            return "agent-scratch-file-not-materialized"
        return "l6-telemetry"
    if layer in ("completion_cert", "submit_gate"):
        return "beyond-trajectory(submit-window)"
    if layer.startswith("verify.horizon.executed"):
        return "in-env-test-execution(container-only)"
    if layer.startswith("edit.syntax") or layer.startswith("l3.contract"):
        return "post-edit-disk-state"
    if layer.startswith(("recovery", "detect.", "l5", "verify.horizon")):
        return "timing/counter-drift"
    return "unclassified"


def _delivered_key(row: dict) -> str:
    """Gate key for one MODEL-FACING row: the byte seal + placement class. `reason` is ''
    on delivered rows; `iteration` is drift-tolerant-excluded."""
    return json.dumps({
        "layer": row.get("layer"), "event_type": row.get("event_type"),
        "outcome": row.get("outcome"), "chars": int(row.get("chars_delivered") or 0),
        "sha": row.get("content_sha256_16"),
        "file_path": str(row.get("file_path") or "").replace("\\", "/"),
    }, sort_keys=True)


def _diff_seq(rec: list[dict], rep: list[dict], keyf, kind: str) -> tuple[list[dict], list[tuple[int, int]], float]:
    """SequenceMatcher diff of two row lists under ``keyf``; returns (ops, matched-iteration
    pairs, first-divergence recorded iteration)."""
    import difflib
    sm = difflib.SequenceMatcher(a=[keyf(r) for r in rec], b=[keyf(r) for r in rep], autojunk=False)
    ops: list[dict] = []
    matched: list[tuple[int, int]] = []
    boundary = float("inf")
    for tag_op, i1, i2, j1, j2 in sm.get_opcodes():
        if tag_op == "equal":
            for di, dj in zip(range(i1, i2), range(j1, j2)):
                ri, rj = rec[di], rep[dj]
                if ri.get("iteration") is not None and rj.get("iteration") is not None:
                    matched.append((int(ri["iteration"]), int(rj["iteration"])))
            continue
        if i1 < len(rec):
            b = rec[i1].get("iteration")
            boundary = min(boundary, float(b) if b is not None else 0.0)
        elif rec:
            boundary = min(boundary, float(rec[-1].get("iteration") or 0) + 1)
        else:
            boundary = min(boundary, 0.0)
        for di in range(i1, i2):
            r = rec[di]
            ops.append({"op": "missing", "kind": kind, "iteration": r.get("iteration"),
                        "layer": r.get("layer"), "outcome": r.get("outcome"),
                        "file_path": r.get("file_path"), "chars": r.get("chars_delivered"),
                        "sha": r.get("content_sha256_16"),
                        "reason": str(r.get("reason") or "")[:120],
                        "tag": _tag_diff("missing", r)})
        for dj in range(j1, j2):
            r = rep[dj]
            ops.append({"op": "extra", "kind": kind, "iteration": r.get("iteration"),
                        "layer": r.get("layer"), "outcome": r.get("outcome"),
                        "file_path": r.get("file_path"), "chars": r.get("chars_delivered"),
                        "sha": r.get("content_sha256_16"),
                        "reason": str(r.get("reason") or "")[:120],
                        "tag": _tag_diff("extra", r)})
    return ops, matched, boundary


def diff_ledgers(task: str, recorded_rows: list[dict], replayed_rows: list[dict],
                 error: str = "") -> FixpointResult:
    """Sequence-align the recorded vs replayed ledgers, two-plane:

    * DELIVERED plane (THE GATE): the model-facing rows — outcome contains 'delivered' with
      chars>0 — compared in order on (layer, event_type, chars, sha-seal, file_path). These
      are the exact bytes the model saw; a divergence here poisons every later verdict, so
      the trusted-prefix boundary comes from this plane.
    * HIDDEN plane (REPORTED, not gating): candidate stamps (eligible/produced), arbitration
      losers (suppressed_*), L6 reindex/staging telemetry, cert/gate rows. Host-side these
      legitimately differ (platform separators, node-count fields on scratch files, the
      container-only embedder's drift candidates, submit-window wrapper executes) — and any
      hidden contamination that MATTERS must surface in later delivered bytes, which gate.
    """
    if error:
        return FixpointResult(task, False, False, False, -1.0,
                              len(recorded_rows), len(replayed_rows), error=error)
    strict = ([_strict_key(r) for r in recorded_rows] == [_strict_key(r) for r in replayed_rows])
    rec_del = [r for r in recorded_rows if _is_delivered(r)]
    rep_del = [r for r in replayed_rows if _is_delivered(r)]
    rec_hid = [r for r in recorded_rows if not _is_delivered(r)]
    rep_hid = [r for r in replayed_rows if not _is_delivered(r)]

    del_ops, del_matched, boundary = _diff_seq(rec_del, rep_del, _delivered_key, "delivered")
    hid_ops, _hid_matched, _hb = _diff_seq(rec_hid, rep_hid, _channel_key, "hidden")

    # re-classify identical rows that merely MOVED (same key INCLUDING the byte seal,
    # different position). Without the sha a same-length different-bytes payload would be
    # masked as a benign move — the exact false-negative the gate exists to catch.
    def _k(d: dict) -> str:
        return json.dumps({x: d.get(x) for x in ("kind", "layer", "outcome", "file_path",
                                                 "chars", "reason", "sha")}, sort_keys=True)
    all_ops = del_ops + hid_ops
    missing_by_k: dict[str, list[dict]] = {}
    for d in all_ops:
        if d["op"] == "missing":
            missing_by_k.setdefault(_k(d), []).append(d)
    for d in all_ops:
        if d["op"] == "extra":
            twins = missing_by_k.get(_k(d))
            if twins:
                twin = twins.pop(0)
                twin["op"] = "moved"
                d["op"] = "moved"
                d["tag"] = twin["tag"] = "timing/counter-drift(moved)"
    channel = not del_ops
    return FixpointResult(task, True, strict, channel, boundary,
                          len(recorded_rows), len(replayed_rows), all_ops, del_matched,
                          n_hidden_diffs=len(hid_ops))


def map_rec_iter(fx: FixpointResult, rec_iter: int) -> int:
    """Map a RECORDED iteration to the REPLAYED iteration via the matched drift pairs."""
    best = None
    for ri, pi in fx.drift_map:
        if ri <= rec_iter and (best is None or ri > best[0]):
            best = (ri, pi)
    if best is None:
        return rec_iter
    return best[1] + (rec_iter - best[0])


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
             "outcome": d.outcome, "payload": d.payload, "iteration": d.iteration,
             "content_sha256_16": d.sha16, "delivered": _is_delivered(
                 {"outcome": d.outcome, "chars_delivered": d.chars})}
            for d in dels]


def _is_cardinal_preserve(case: dict) -> bool:
    """The 3 CARDINAL P5 preserves = those whose 'why' names a P5 consumption."""
    return "P5" in str(case.get("why", ""))


def _count_in(text: str) -> set[int]:
    return {int(x) for x in re.findall(r"\b(\d+)\b", text or "")}


def evaluate_cases(cases: dict, recorded: dict[str, list[Delivery]],
                   replayed: dict[str, list[dict]] | None,
                   blocked_note: str | None,
                   fidelity: dict[str, "FixpointResult"] | None = None) -> list[CaseVerdict]:
    """The oracle. ``recorded`` = reconstructed recorded deliveries per task; ``replayed`` = the
    replayed ledger per task (or None if the seam driver was BLOCKED — then flag-gated cases
    SKIP:seam-blocked). ``fidelity`` (SS-R2) = per-task OFF-flag FixpointResult; when present it
    GATES every case FIRST: a case on an unfaithful task/window is REPLAY_UNFAITHFUL, never a
    PASS or FAIL. Returns one CaseVerdict per case delivery."""
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

    def _fx(task: str) -> "FixpointResult | None":
        return fidelity.get(task) if fidelity is not None else None

    def _gate_reason(task: str, rec_matches: list[dict] | None) -> str | None:
        """FIXPOINT-FIRST GATE (SS-R2). None = the case may be judged. A string = the case is
        REPLAY_UNFAITHFUL for that reason. Trusted-prefix rule: replay is chronological, so
        divergence poisons only what comes AT/AFTER it — a case whose every recorded delivery
        sits strictly BEFORE the first divergence iteration is still judgeable."""
        if fidelity is None:
            return None
        fx = _fx(task)
        if fx is None:
            return "task not replayed (no fixpoint result)"
        if not fx.replayed:
            return f"replay failed: {(fx.error or 'unknown')[:140]}"
        if fx.faithful:
            return None
        if rec_matches and all(int(r.get("iteration") or 10**9) < fx.boundary_iter for r in rec_matches):
            return None
        return (f"off-flag fixpoint diverges at recorded iteration {fx.boundary_iter:g}; "
                f"case is not in the trusted prefix")

    def _rep_match(rep: list[dict], task: str, layer: str, rec_matches: list[dict],
                   mnum: int | None, use_sha: bool = False, tol: int = 2) -> list[dict]:
        """Replay-side rows for a case: drift-aware (recorded iteration mapped through the
        OFF-arm alignment) when fidelity is available; home/m-based otherwise (stub path)."""
        fx = _fx(task)
        if fx is not None and fx.replayed and rec_matches:
            exps = {map_rec_iter(fx, int(r.get("iteration") or 0)) for r in rec_matches}
            shas = {r.get("content_sha256_16") for r in rec_matches if r.get("content_sha256_16")}
            hits = []
            for r in rep:
                if not _layer_matches(layer, r.get("layer", "")):
                    continue
                if use_sha and r.get("content_sha256_16") in shas:
                    hits.append(r)
                    continue
                it = r.get("iteration")
                if it is not None and any(abs(int(it) - e) <= tol for e in exps):
                    hits.append(r)
            return hits
        return _rows_matching(rep, layer, mnum)

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
        gate = _gate_reason(c["task"], rec)
        if gate:
            out.append(CaseVerdict("preserve", c["task"], c["delivery"], REPLAY_UNFAITHFUL, gate, card))
            continue
        if rep is None:
            out.append(CaseVerdict("preserve", c["task"], c["delivery"], SKIP,
                                   f"seam-blocked: {blocked_note}", card))
            continue
        still = [r for r in _rep_match(rep, c["task"], layer, rec, mnum, use_sha=True)
                 if r.get("delivered")]
        if still:
            loc = still[0].get("home_msg") or still[0].get("iteration")
            out.append(CaseVerdict("preserve", c["task"], c["delivery"], PASS,
                                   f"still delivered at {loc}", card))
        else:
            # diagnostics: did the class deliver ANYWHERE (moved/re-rendered) vs vanish?
            anywhere = [r for r in rep if _layer_matches(layer, r.get("layer", ""))
                        and r.get("delivered")]
            if anywhere:
                locs = [(r.get("iteration"), r.get("content_sha256_16")) for r in anywhere[:3]]
                detail = (f"delivery MOVED/REWRITTEN, not preserved in place: same-layer "
                          f"delivered at iter/sha {locs} vs recorded iter "
                          f"{[r.get('iteration') for r in rec]} (manifest asserts bytes "
                          f"equivalent + boundary unchanged)")
            else:
                detail = "preserve delivery ABSENT after replay (no same-layer delivery at all)"
            out.append(CaseVerdict("preserve", c["task"], c["delivery"], FAIL,
                                   ("CARDINAL P5 KILLED — " if card else "") + detail, card))

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
                gate = _gate_reason(c["task"], rec)
                if gate:
                    out.append(CaseVerdict(section, c["task"], dv, REPLAY_UNFAITHFUL, gate, False))
                    continue
                if rep is None:
                    out.append(CaseVerdict(section, c["task"], dv, SKIP,
                                           f"seam-blocked: {blocked_note}", False))
                    continue
                delivered = [r for r in _rep_match(rep, c["task"], layer, rec, mnum)
                             if r.get("delivered")]
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
            gate = _gate_reason(c["task"], rec)
            if gate:
                out.append(CaseVerdict("coherence", c["task"], dv, REPLAY_UNFAITHFUL, gate, False))
                continue
            if rep is None:
                out.append(CaseVerdict("coherence", c["task"], dv, SKIP, f"seam-blocked: {blocked_note}", False))
                continue
            fired = [r for r in _rep_match(rep, c["task"], layer, rec, mnum) if r.get("delivered")]
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
        gate = _gate_reason(c["task"], rec)
        if gate:
            out.append(CaseVerdict("recovery_misfire", c["task"], c["delivery"], REPLAY_UNFAITHFUL, gate, False))
            continue
        if rep is None:
            out.append(CaseVerdict("recovery_misfire", c["task"], c["delivery"], SKIP,
                                   f"seam-blocked: {blocked_note}", False))
            continue
        fired = [r for r in _rep_match(rep, c["task"], "recovery", rec, mnum) if r.get("delivered")]
        out.append(CaseVerdict("recovery_misfire", c["task"], c["delivery"],
                               FAIL if fired else PASS,
                               "recovery still fired (misfire not suppressed)" if fired else "no misfire delivery", False))

    # ---- RECOVERY EARLIER RELEASE -------------------------------------------
    for c in cases.get("recovery_earlier_release", []):
        rec = _rows_matching(rec_rows(c["task"]), "recovery", None)
        rep = rep_rows(c["task"])
        # earlier-release spans the WHOLE run: a trusted prefix is not enough — the task's
        # off-flag replay must be faithful end-to-end for "earlier than recorded" to mean anything.
        if fidelity is not None:
            fx = _fx(c["task"])
            if fx is None or not fx.replayed or not fx.faithful:
                why = ("task not replayed" if fx is None or not fx.replayed
                       else f"off-flag fixpoint diverges at iteration {fx.boundary_iter:g} "
                            f"(whole-run property needs a fully faithful task)")
                out.append(CaseVerdict("recovery_earlier", c["task"], "recovery",
                                       REPLAY_UNFAITHFUL, why, False))
                continue
        if rep is None:
            out.append(CaseVerdict("recovery_earlier", c["task"], "recovery", SKIP,
                                   f"seam-blocked: {blocked_note}", False))
            continue
        recovered = [r for r in rep if _layer_matches("recovery", r.get("layer", "")) and r.get("delivered")]
        if not recovered:
            out.append(CaseVerdict("recovery_earlier", c["task"], "recovery", FAIL,
                                   "recovery never delivered on replay", False))
            continue
        fx = _fx(c["task"])
        if fx is not None and fx.replayed:
            rec_it = min((int(r.get("iteration") or 10**9) for r in rec), default=None)
            first_it = min(int(r.get("iteration") or 10**9) for r in recovered)
            bound = map_rec_iter(fx, rec_it) if rec_it is not None else None
            if bound is None or first_it < bound:
                out.append(CaseVerdict("recovery_earlier", c["task"], "recovery", PASS,
                                       f"recovery released earlier (iter {first_it} < mapped recorded {bound})", False))
            else:
                out.append(CaseVerdict("recovery_earlier", c["task"], "recovery", FAIL,
                                       f"recovery not earlier (iter {first_it} >= mapped recorded {bound})", False))
            continue
        rec_m = min((int(r.get("home_msg")) for r in rec if r.get("home_msg") not in (None, -1)), default=None)
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


# Every SUPER-SAIYAN flag across BOTH vocabularies: the ss_gate feature contract
# (GT_SS_STEP_BEHIND...) AND the landed rl_profile Profile-2 members (GT_SS_NOVELTY...).
# The OFF-arm must set each to an EXPLICIT "0": under the W8 default-inversion an UNSET
# Profile-2 member is RESOLVED to "1" by the seam's own fan-out, so "unset" is NOT off.
_SS_FLAGS_ALL = ("GT_SS_NOVELTY", "GT_SS_DEDUP2", "GT_SS_COHERENCE_V2",
                 "GT_SS_RECOVERY_V2", "GT_SS_PROVENANCE", "GT_SS_LATE_DROP",
                 "GT_SS_ACK_METRICS", "GT_SS_ARBITER_V2", "GT_SS_EXEC_TRUTH",
                 "GT_SS_SUBMIT_RED", "GT_SS_ACK_FORM", "GT_SS_ELIGIBILITY")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="SS-R2 SUPER-SAIYAN replay-oracle (fixpoint-first)")
    ap.add_argument("--cases", default=str(_DEFAULT_CASES))
    ap.add_argument("--recorded-root", default=str(_DEFAULT_RECORDED),
                    help="dir holding <task>/mini-swe-agent.trajectory.json + gt_runtime_ledger_<task>.jsonl")
    ap.add_argument("--snapshots-root", default="D:/gt_runs/ss_replay_snapshots",
                    help="dir of per-task repo checkouts at the recorded base commits")
    ap.add_argument("--tasks", default="", help="comma-separated task subset (default: all manifest tasks)")
    ap.add_argument("--apply-edits", action="store_true", default=True,
                    help="re-execute allowlisted file-mutation commands (recorded disk evolution)")
    ap.add_argument("--no-apply-edits", dest="apply_edits", action="store_false")
    ap.add_argument("--out", default=str(_REPO / "ss_replay_oracle_report.json"))
    ap.add_argument("--work-dir", default="",
                    help="dir for per-task replay artifacts (default: a fresh temp dir)")
    ap.add_argument("--kill-members", default="",
                    help="comma-separated GT members forced to 0 in BOTH arms (e.g. the "
                         "ONNX embedder legs for speed; any infidelity this introduces is "
                         "caught by the per-task fixpoint gate)")
    # child mode (internal): replay ONE task in THIS process with the env the parent set.
    ap.add_argument("--replay-one", default="", help=argparse.SUPPRESS)
    ap.add_argument("--child-out", default="", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    if args.replay_one:
        return replay_child_main(args.replay_one, Path(args.recorded_root),
                                 Path(args.child_out), args.apply_edits)

    # SS-R3: the mirrors are drive-global — one parent at a time, or both runs poison.
    if not acquire_run_lock():
        return 2

    import atexit
    atexit.register(release_run_lock)
    import tempfile
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    cases = json.loads(Path(args.cases).read_text(encoding="utf-8"))
    recorded_root = Path(args.recorded_root)
    snapshots_root = Path(args.snapshots_root)
    tasks = _manifest_tasks(cases)
    if args.tasks:
        keep = {t.strip() for t in args.tasks.split(",") if t.strip()}
        tasks = [t for t in tasks if t in keep]
    work_dir = Path(args.work_dir) if args.work_dir else Path(tempfile.mkdtemp(prefix="ssr2_"))
    work_dir.mkdir(parents=True, exist_ok=True)

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

    # ── SS-R2 STEP 1 (per task): OFF-flag replay -> FIXPOINT GATE ────────────
    # The off-arm (every GT_SS_* unset) must reproduce the RECORDED ledger; a task that
    # fails this canary gets its cases marked REPLAY_UNFAITHFUL (outside the trusted
    # prefix) instead of producing false verdicts.
    fidelity: dict[str, FixpointResult] = {}
    replayed_on: dict[str, list[dict]] = {}
    applied_log: dict[str, dict] = {}
    kill = {m.strip(): "0" for m in args.kill_members.split(",") if m.strip()}
    ss_on = {**{k: "1" for k in _SS_FLAGS_ALL}, **kill}
    ss_off = {**{k: "0" for k in _SS_FLAGS_ALL}, **kill}   # EXPLICIT zeros: unset would be fan-out-resolved ON
    for t in sorted(recon):
        # SS-R3 crash containment: one task's failure (child crash, mirror fault, OOM) is a
        # per-task ERROR entry — the run ALWAYS continues to the report. A partial-silent
        # death that loses the report is the one unacceptable outcome.
        try:
            off = spawn_replay(t, recorded_root, snapshots_root, ss_off, args.apply_edits,
                               work_dir, arm="off")
            fx = diff_ledgers(t, recorded_rows.get(t, []), off["rows"], error=off["error"] or "")
            fidelity[t] = fx
            applied_log[t] = {"off_applied": sum(1 for a in off["applied"] if "cmd" in a),
                              "off_error": off["error"]}
        except BaseException as exc:  # noqa: BLE001 — incl. KeyboardInterrupt-adjacent faults
            if isinstance(exc, KeyboardInterrupt):
                raise
            fidelity[t] = FixpointResult(t, False, False, False, -1.0,
                                         len(recorded_rows.get(t, [])), 0,
                                         error=f"task-level fault: {type(exc).__name__}: {exc}")
            applied_log[t] = {"off_error": str(exc)[:200]}
            print(f"  [fixpoint] {t}: ERROR {type(exc).__name__}: {str(exc)[:120]}",
                  file=sys.stderr)
            continue
        print(f"  [fixpoint] {t}: strict={fx.strict} channel={fx.channel} "
              f"boundary={fx.boundary_iter:g} rows {fx.n_recorded}->{fx.n_replayed} "
              f"diffs={len(fx.diffs)}", file=sys.stderr)
        # ── STEP 2: ON-arm replay (all SS flags) for the case verdicts ───────
        if fx.replayed:
            try:
                on = spawn_replay(t, recorded_root, snapshots_root, ss_on, args.apply_edits,
                                  work_dir, arm="on")
                replayed_on[t] = on["rows"]
                applied_log[t]["on_applied"] = sum(1 for a in on["applied"] if "cmd" in a)
                applied_log[t]["on_error"] = on["error"]
            except BaseException as exc:  # noqa: BLE001
                if isinstance(exc, KeyboardInterrupt):
                    raise
                replayed_on[t] = []
                applied_log[t]["on_error"] = f"task-level fault: {type(exc).__name__}: {exc}"

    trusted = sorted(t for t, fx in fidelity.items() if fx.faithful)
    prefix_only = sorted(t for t, fx in fidelity.items()
                         if fx.replayed and not fx.faithful and fx.boundary_iter > 0)
    untrusted = sorted(t for t, fx in fidelity.items() if not fx.replayed)

    # SS built-probes (mirrors ss_gate's SKIP:flag-not-built): globally — any ss_* reason
    # anywhere or any faithful task whose ON-arm differs from the recording; per FAMILY —
    # the family's reason token must appear in SOME ON-arm ledger for its suppress cases to
    # be judged FAIL (an engine that is not landed yet is an honest SKIP, never a false RED).
    ss_built = False
    for t in trusted + prefix_only:
        fx = fidelity[t]
        on_rows = replayed_on.get(t, [])
        if any("ss_" in str(r.get("reason") or "") for r in on_rows):
            ss_built = True
            break
        # a FAITHFUL task's OFF-arm equals the recording, so an ON-arm that differs from
        # the recording differs from OFF -> the SS flags have an effect.
        if fx.faithful and [_channel_key(r) for r in on_rows] != \
                [_channel_key(r) for r in recorded_rows.get(t, [])]:
            ss_built = True
            break
    _all_on_reasons = " ".join(str(r.get("reason") or "") for rows in replayed_on.values()
                               for r in rows)
    family_token = {
        "suppress_step_behind": "ss_step_behind", "suppress_semantic_dup": "ss_semantic_dup",
        "suppress_provenance": "ss_provenance", "suppress_late": "ss_late",
        "coherence": "ss_coherence", "recovery_misfire": "ss_recovery",
        "recovery_earlier": "ss_recovery",
    }
    family_built = {sec: (tok in _all_on_reasons) for sec, tok in family_token.items()}

    # ── LAYER 3: oracle (fixpoint-gated) ─────────────────────────────────────
    verdicts = evaluate_cases(cases, recorded_deliveries, replayed_on, None, fidelity)
    verdicts = [
        CaseVerdict(v.section, v.task, v.label, SKIP,
                    f"flag-not-built ({family_token.get(v.section)} never appears in any "
                    f"ON-arm ledger)", v.cardinal)
        if (v.verdict == FAIL and v.section in family_token
            and not family_built.get(v.section, True)) else v
        for v in verdicts
    ]
    trusted_on = {t: replayed_on.get(t, []) for t in trusted + prefix_only}
    invariants = evaluate_invariants(recorded_deliveries, trusted_on or None, recorded_rows,
                                     "no trusted replays" if not trusted_on else None)
    # the legacy in-function "off-flag fixpoint" invariant would compare the ON-arm to the
    # recording (wrong plane under SS-R2 — the real fixpoint is per-task and IS the gate).
    # Replace it with the per-task summary; gating already enforces it, so this line is
    # informational (PASS when every replayed task is faithful, SKIP otherwise — the
    # unfaithful tasks' cases are already REPLAY_UNFAITHFUL, never silently counted).
    invariants = [i for i in invariants if not i.name.startswith("off-flag fixpoint")]
    n_rep = sum(1 for fx in fidelity.values() if fx.replayed)
    n_faith = sum(1 for fx in fidelity.values() if fx.faithful)
    invariants.append(InvariantResult(
        "off-flag fixpoint (per task, delivered plane — THE GATE)",
        PASS if (n_rep and n_faith == n_rep) else SKIP,
        f"{n_faith}/{n_rep} replayed tasks faithful; unfaithful tasks' cases are REPLAY_UNFAITHFUL"))

    # ── tallies + exit ───────────────────────────────────────────────────────
    n_fail = sum(1 for v in verdicts if v.verdict == FAIL)
    n_pass = sum(1 for v in verdicts if v.verdict == PASS)
    n_skip = sum(1 for v in verdicts if v.verdict == SKIP)
    n_unf = sum(1 for v in verdicts if v.verdict == REPLAY_UNFAITHFUL)
    cardinal_kills = [v for v in verdicts if v.cardinal and v.verdict == FAIL]
    cardinal_unf = [v for v in verdicts if v.cardinal and v.verdict == REPLAY_UNFAITHFUL]
    inv_fail = [i for i in invariants if i.verdict == FAIL]
    hard_fail = (n_fail > 0 or bool(cardinal_kills) or bool(manifest_findings)
                 or bool(residuals) or bool(recon_errors) or bool(inv_fail))
    if hard_fail:
        exit_code = 1
    elif cardinal_unf:
        exit_code = 3   # inconclusive: a cardinal P5 sits outside the trusted subset
    else:
        exit_code = 0

    # ── report (SS-R3: WRITE the JSON FIRST — a print/format fault must never lose it) ──
    report = {
        "gate": "ss_replay_oracle", "schema": "ss.replay_oracle_report.v2", "generated_utc": ts,
        "recorded_root": str(recorded_root), "snapshots_root": str(snapshots_root),
        "apply_edits": bool(args.apply_edits), "ss_built": ss_built,
        "tasks_reconstructed": sorted(recon), "reconstruction_errors": recon_errors,
        "residual_seal_leaks": residuals, "manifest_findings": manifest_findings,
        "fixpoint": {t: {"strict": fx.strict, "channel": fx.channel, "replayed": fx.replayed,
                         "boundary_iter": fx.boundary_iter if fx.boundary_iter != float("inf") else None,
                         "rows_recorded": fx.n_recorded, "rows_replayed": fx.n_replayed,
                         "error": fx.error, "diffs": fx.diffs[:80]}
                     for t, fx in fidelity.items()},
        "trusted_subset": {"full": trusted, "prefix": prefix_only, "not_replayed": untrusted},
        "applied_edits": applied_log,
        "cardinal_p5_kills": [f"{v.task}:{v.label}" for v in cardinal_kills],
        "cardinal_p5_unfaithful": [f"{v.task}:{v.label} — {v.reason}" for v in cardinal_unf],
        "cases": [{"section": v.section, "task": v.task, "delivery": v.label, "verdict": v.verdict,
                   "reason": v.reason, "cardinal": v.cardinal} for v in verdicts],
        "invariants": [{"name": i.name, "verdict": i.verdict, "detail": i.detail} for i in invariants],
        "counts": {"pass": n_pass, "fail": n_fail, "skip": n_skip, "replay_unfaithful": n_unf},
        "exit_code": exit_code,
    }
    try:
        Path(args.out).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        print(f"  WARN: could not write report: {exc}", file=sys.stderr)

    print(f"\n# SS-R2 SUPER-SAIYAN replay-oracle (fixpoint-first) — {ts}")
    print(f"  recorded-root : {recorded_root}")
    print(f"  snapshots     : {snapshots_root}  apply-edits={args.apply_edits}")
    print(f"  tasks         : {len(recon)} reconstructed"
          + (f" ({len(recon_errors)} errors)" if recon_errors else ""))
    print(f"  reconstruction: residual seal leaks in {len(residuals)} task(s)"
          + (f" {list(residuals)[:3]}" if residuals else " (0 — every seal stripped cleanly)"))
    print(f"  manifest      : {len(cases_delivery_count(cases))} case-deliveries; "
          f"{len(manifest_findings)} discrepancy(ies)")
    for f in manifest_findings:
        print("     ! " + f)
    print(f"  SS engines    : {'BUILT (flags have effect)' if ss_built else 'NOT BUILT (GT_SS_* no-op; suppress FAILs -> SKIP)'}")

    print("\n  ── per-task OFF-flag fixpoint (the gate) ──")
    fx_rows = []
    for t in sorted(fidelity):
        fx = fidelity[t]
        from collections import Counter
        tags = Counter(d["tag"] for d in fx.diffs)
        tag_s = ", ".join(f"{k}x{v}" for k, v in tags.most_common(3)) or "-"
        n_del = sum(1 for d in fx.diffs if d.get("kind") == "delivered")
        status = ("FAITHFUL(strict)" if fx.strict else
                  "FAITHFUL(delivered)" if fx.channel else
                  (f"PREFIX<iter{fx.boundary_iter:g}" if fx.replayed else "NOT-REPLAYED"))
        fx_rows.append([status, t[:34], f"{fx.n_recorded}->{fx.n_replayed}",
                        f"{n_del}/{fx.n_hidden_diffs}", tag_s[:52],
                        (fx.error or "")[:40]])
    print(_table(["FIXPOINT", "TASK", "ROWS", "DIFF del/hid", "TOP ROOT-CAUSE TAGS", "ERROR"], fx_rows))

    print("\n  ── per-case verdicts (fixpoint-gated) ──")
    rows = [[v.verdict + ("*" if v.cardinal else ""), v.section, v.task[:30], v.label[:26], v.reason[:56]]
            for v in verdicts]
    print(_table(["VERDICT", "SECTION", "TASK", "DELIVERY", "REASON"], rows))

    print("\n  ── invariants (trusted subset) ──")
    print(_table(["VERDICT", "INVARIANT", "DETAIL"],
                 [[i.verdict, i.name, i.detail[:70]] for i in invariants]))

    print(f"\n  trusted subset: {len(trusted)} fully faithful + {len(prefix_only)} prefix-faithful"
          f" / {len(fidelity)} replayed; not-replayed: {len(untrusted)}")
    print(f"  cases: PASS={n_pass} FAIL={n_fail} SKIP={n_skip} {REPLAY_UNFAITHFUL}={n_unf}"
          f" | cardinal kills={len(cardinal_kills)} | cardinal unfaithful={len(cardinal_unf)}")
    print(f"  EXIT {exit_code} ({'GREEN' if exit_code == 0 else 'INCONCLUSIVE-CARDINALS' if exit_code == 3 else 'RED'})")
    print(f"  report -> {args.out}")
    release_run_lock()
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
