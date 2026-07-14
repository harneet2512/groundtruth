#!/usr/bin/env python3
r"""SS-9 — the AUTOMATED SMOKE VERDICT tool (SS proof-pipeline, coordinator SS-9).

Run:  ``python scripts/swebench/ss_smoke_audit.py --run-dir D:/gt_runs/<run>/art``

When the 2-task held-out SS smoke finishes, this turns its recorded artifacts into the
go/no-go answer in MINUTES instead of a manual §4 audit. It is DETERMINISTIC, ``$0``, and
OFFLINE. It NEVER edits a product file and NEVER drives the live seam — it reads the RECORDED
trajectory + runtime ledger + reward and grades every model-visible GT delivery against the
Super-Saiyan (SS) delivery standard (gt-math §"Super Saiyan (SS) delivery standard").

INPUT (per task):  ``<run-dir>/<task>/``
    mini-swe-agent.trajectory.json   — the agent's chronological messages[]
    gt_runtime_ledger_<task>.jsonl   — the host-side delivery ledger (content_sha256_16 seals)
    reward.txt / report.json         — the evaluation verdict (context only, never a gate)

WHAT IT COMPUTES, per task, CHRONOLOGICALLY (parse messages in order; never grep-to-conclude):

  1  DELIVERY LEDGER JOIN — every ledger *delivered* row byte-joined into the observation
     stream via the proven sliding-window sha256 seal locate (imported read-only from
     ``ss_replay_oracle.reconstruct_task`` → ``locate_seal``: bytes-first, char-window
     fallback). A row that does NOT byte-join is a hard SMOKE_FAIL (delivery unproven).

  2  SS VIOLATIONS (the go/no-go core), per delivered fact:
       (a) STEP_BEHIND — the fact is about a file the agent had ALREADY body-read before the
           delivery (entity ⊆ independently-recomputed acquisition set). ss_step_behind SHOULD
           have suppressed it. Applies to relationship-fact layers (l3b.evidence / l3.contract).
       (b) LATE — an obligation/requirement reminder delivered at/after PASSING test evidence
           for the requirement already existed. Applies to spec.obligation / obligation.resurface
           / verify.horizon.advisory.
       (c) WRONG-INFO — (c1) coherence "rewritten <file> N times" whose N != the independently
           counted write-commands to that file; (c2) provenance: a payload / file_path citing a
           tmp / scratch / generated path (never a repo source); (c3) an unexecuted covering
           assurance (a covering-verdict claim with no execution evidence in the payload).
       (d) LEAK — word-boundary test-identifier scan (pytest node-ids, ::-qualified test ids,
           ^test_\w{3,}, FAIL_TO_PASS/PASS_TO_PASS). Any hit fails the delivery.
       (e) DOSE — > 1 delivered payload homed to the same observation.

  3  ACKNOWLEDGMENT — per delivery: (i) SS-0's ledger ``ack`` field when present + (ii) an
     INDEPENDENT check — does any LATER assistant-authored message reference a delivered entity
     (file / symbol)? Reports the ack-rate per class + the exact (delivery m# → ack m#) pairs.

  4  SS SUPPRESSION HEALTH — counts per ``ss_*`` ledger reason (step_behind / semantic_dup /
     provenance / late) actually present, plus a P-bucket rollup (P1 / P2 / P3 / P3a-ack / P5)
     over the delivered facts.

VERDICT:  SMOKE_PASS iff, across audited tasks, violations(a-e) == 0 AND ack_count > 0 AND every
delivered row byte-joined. Else SMOKE_FAIL, listing the exact failing instances (task, m#,
payload head, reason). Output: a per-task console table + a combined verdict block + a JSON report.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

# ── repo layout ──────────────────────────────────────────────────────────────
_REPO = Path(__file__).resolve().parents[2]
_SWE = _REPO / "scripts" / "swebench"
_SRC = _REPO / "src"
if str(_SWE) not in sys.path:
    sys.path.insert(0, str(_SWE))
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# The DELIVERY LEDGER JOIN is the proven, byte-verified reconstruction from the SS replay
# oracle. We import it READ-ONLY (no seam install, no product mutation) — this is the
# "reuse the proven join approach" the SS-9 mandate calls for.
import ss_replay_oracle as sso  # noqa: E402  (reconstruct_task / locate_seal / _commands_of)
import consumption_ledger as cl  # noqa: E402  (shared sealed receipt classifier)
from groundtruth.runtime.obligations import (  # noqa: E402
    classify_checked_behavioral_proof,
    rendered_obligation_subject_groups,
)

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass

# ══════════════════════════════════════════════════════════════════════════════
# LAYER TAXONOMY (which SS check applies to which producer)
# ══════════════════════════════════════════════════════════════════════════════
# Relationship-fact producers whose payload is a set of caller/callee/contract locations the
# agent could SELF-ACQUIRE by reading the subject file → eligible for the step_behind check.
RELATION_LAYERS = {"l3b.evidence", "l3.contract"}
# Obligation / requirement / verification reminders → eligible for the late check.
OBLIGATION_LAYERS = {"spec.obligation", "obligation.resurface", "verify.horizon.advisory"}
# Coherence "rewritten N times" producers → eligible for the count check.
COHERENCE_LAYERS = {"detect.coherence", "semantic_drift"}
# Executed / companion / recovery classes that, delivered clean + acknowledged, are P5.
P5_CLASSES = {
    "edit.syntax", "verify.horizon.executed", "consensus.scope",
    "recovery", "detect.coherence", "detect.loop",
}

# ══════════════════════════════════════════════════════════════════════════════
# LEAK SCAN — word-boundary test-identifier detector (MUTATION TARGET)
# ══════════════════════════════════════════════════════════════════════════════
# Defined HERE (not merely imported) so the mutation test can swap a broken (boundary-less)
# variant and prove the guard bites. The \b boundaries keep ordinary English ("latest",
# "fastest_run", "greatest", "contest") from tripping the scan; without them, "fastest_run"
# would false-flag on the "test_run" substring.
LEAK_PATTERNS = [
    re.compile(r"\btests?/[^\s:'\"]+\.py\b"),        # tests/foo/test_x.py | test/foo.py
    re.compile(r"::test[A-Za-z0-9_]*\b"),             # ::test_something (node-id tail)
    re.compile(r"\btest_[A-Za-z0-9_]{3,}\b"),         # test_something (>=3 tail chars)
    re.compile(r"\b[A-Za-z0-9]{2,}_test\b"),          # something_test
    re.compile(r"\bFAIL_TO_PASS\b|\bPASS_TO_PASS\b"),  # SWE-bench label leakage
]


def leak_scan(text: str) -> list[str]:
    """Every test-identifier-looking token in ``text`` (manifest-free, word-boundary guarded)."""
    hits: list[str] = []
    if not text:
        return hits
    for pat in LEAK_PATTERNS:
        hits.extend(m.group(0) for m in pat.finditer(text))
    return hits


# ══════════════════════════════════════════════════════════════════════════════
# ENTITY / PATH EXTRACTION
# ══════════════════════════════════════════════════════════════════════════════
_SRC_EXT = r"(?:py|go|js|ts|jsx|tsx|java|rs|rb|c|cc|cpp|h|hpp|kt|scala|php)"
# A cited caller/ref location: ``path/to/file.py:1234``  (the anchor GT evidence always uses).
PATH_LINE = re.compile(r"([A-Za-z_][\w./\-]*\." + _SRC_EXT + r"):\d+")
# Any path-like token (for provenance scanning of file_path + payload).
PATH_TOK = re.compile(r"(?:\.\./)*[A-Za-z_][\w./\-]*\." + _SRC_EXT + r"\b")
# Generated / scratch path segments — never a repo source of truth.
# NOTE: deliberately excludes bare ``build/`` and ``dist/`` — those collide with legitimate
# source packages (e.g. ``conan/tools/build/cppstd.py``). Only unambiguously-generated dirs.
GENERATED_SEG = re.compile(
    r"(?:^|[\s'\"(=/])(?:\.\./)*(?:tmp|htmlcov|__pycache__|node_modules|\.tox|\.eggs|"
    r"\.mypy_cache|\.pytest_cache|site-packages|\.git)/", re.IGNORECASE)
SCRATCH_BASE = re.compile(r"\bpatch_[\w\-]*\.(?:py|txt|diff)\b", re.IGNORECASE)
# Real container/stdlib roots that are NOT scratch even if they nest deep.
_REAL_ROOTS = ("/testbed", "/usr", "/opt", "/gt_artifacts")


def cited_paths(payload: str) -> set[str]:
    """The distinct source files cited as ``path:line`` in a delivery payload."""
    return {p.lstrip("./") for p in PATH_LINE.findall(payload or "")}


def is_scratch_path(tok: str) -> bool:
    """True iff ``tok`` names a tmp / scratch / generated artifact (never a repo source)."""
    t = tok.strip().strip("'\"()")
    if any(t.startswith(r) for r in _REAL_ROOTS):
        return False
    return bool(GENERATED_SEG.search(t) or SCRATCH_BASE.search(t))


def scratch_paths_in(text: str) -> list[str]:
    """Every scratch/generated path token in ``text`` (deduped, order-preserving)."""
    seen: list[str] = []
    for tok in PATH_TOK.findall(text or ""):
        if is_scratch_path(tok) and tok not in seen:
            seen.append(tok)
    # also catch bare 'tmp/patch_x.py' or '(/tmp)' style not caught as a full source token
    for m in re.finditer(r"(?:\.\./)*(?:/)?tmp/[\w./\-]+", text or "", re.IGNORECASE):
        tok = m.group(0)
        if not any(tok.startswith(r) for r in _REAL_ROOTS) and tok not in seen:
            seen.append(tok)
    return seen


# ══════════════════════════════════════════════════════════════════════════════
# INDEPENDENT ACQUISITION LEDGER (recomputed from the trajectory ACTIONS — MUTATION TARGET)
# ══════════════════════════════════════════════════════════════════════════════
# A file is "body-acquired" once the agent has run a command that reads its BODY
# (cat / sed -n / head / tail / nl / less / more / python open()). A `find`/`ls`/`grep`
# that merely LISTS a filename does NOT acquire the file's caller relationships — so those are
# deliberately excluded (that over-broad harvest was the false-positive trap).
_BODYREAD = re.compile(r"\b(?:cat|sed\s+-n|head|tail|nl|less|more)\b")
_PY_OPEN = re.compile(r"open\(\s*['\"]([^'\"]+\." + _SRC_EXT + r")['\"]")


def acquisition_before(msgs: list[dict], home: int) -> set[str]:
    """The set of files whose BODY the agent read (self-acquired) STRICTLY before ``home``."""
    acq: set[str] = set()
    for i, m in enumerate(msgs):
        if i >= home:
            break
        if m.get("role") != "assistant":
            continue
        for c in sso._commands_of(m):
            reads_body = bool(_BODYREAD.search(c))
            for p in _PY_OPEN.findall(c):
                acq.add(p.lstrip("./"))
            if reads_body:
                for p in PATH_TOK.findall(c):
                    acq.add(p.lstrip("./"))
    return acq


# ══════════════════════════════════════════════════════════════════════════════
# WRITE-COMMAND COUNTER (independent recompute for the coherence count check)
# ══════════════════════════════════════════════════════════════════════════════
# A "rewrite" is an actual file MUTATION: sed -i, a redirect (> / >>) into the file, tee into
# it, or a python open(...,'w'/'a') on it. A `cat`/`sed -n`/`grep` READ is NOT a rewrite — the
# coherence producer's known bug conflates reads with rewrites, which this recompute exposes.
_WRITE_FAIL_RE = re.compile(
    r"no such file|cannot (?:open|find|stat|create)|patch failed|does not apply|hunk .*failed|"
    r"malformed|command not found|permission denied|no replacement|did not appear|"
    r"multiple occurrences|fatal:|unexpected eof",
    re.IGNORECASE,
)


def _write_result_ok(content: str) -> bool:
    """Whether the recorded tool result proves a successful write attempt."""
    rc = _RETURNCODE.search(content or "")
    if not rc or int(rc.group(1)) != 0:
        return False
    return not _WRITE_FAIL_RE.search(content or "")


def _writes_to_basename(msgs: list[dict], basename: str, *, before: int | None = None,
                        passing: list[int] | None = None) -> list[int]:
    """Message indices of write-COMMANDS that mutate a REPO file with basename ``basename``.

    A write is: ``sed -i FILE``, a redirect/tee whose TARGET basename == ``basename`` (and is
    not a tmp/scratch backup like ``/tmp/orig_sh.py``), or a write-mode ``open(FILE, 'w'|'a')``.
    Reads (``cat``/``sed -n``/``grep``/``open(FILE)``) are NOT writes — the coherence producer's
    known bug is conflating them, which this recompute exposes.

    Semantics note: this pairs each candidate write with its recorded tool result, rejects
    nonzero exits and known failure/no-op markers, stops at the delivery observation, and when
    given passing-test indices resets the count after the latest prior passing test.
    This independently reproduces the producer's successful-write and post-GREEN semantics.
    """
    if not basename:
        return []
    b = re.escape(basename)
    sed_i = re.compile(r"sed\s+-[a-zA-Z]*i\b\s+[^|;&]*" + b)
    openw = re.compile(r"open\([^)]*" + b + r"[^)]*,\s*['\"][wa]")
    # redirect / tee TARGET tokens; we keep only those whose basename matches and isn't scratch
    redir_tgt = re.compile(r">>?\s*([^\s|;&'\"]+)")
    tee_tgt = re.compile(r"\btee\b\s+(?:-a\s+)?([^\s|;&'\"]+)")
    upper = len(msgs) if before is None else max(0, min(before, len(msgs)))
    prior_green = [i for i in (passing or []) if i < upper]
    lower = max(prior_green) if prior_green else -1
    out: list[int] = []
    for result_msg, (command_msg, command) in _command_events_by_tool_message(msgs).items():
        if result_msg >= upper or command_msg <= lower:
            continue
        wrote = False
        if basename in command:
            if sed_i.search(command) or openw.search(command):
                wrote = True
            else:
                for tgt in redir_tgt.findall(command) + tee_tgt.findall(command):
                    if _basename(tgt) == basename and not is_scratch_path(tgt):
                        wrote = True
                        break
        content = msgs[result_msg].get("content", "")
        result = content if isinstance(content, str) else ""
        if wrote and _write_result_ok(result) and command_msg not in out:
            out.append(command_msg)
    return out


_COHERENCE_CLAIM = re.compile(r"rewritten\s+([^\s]+?)\s+(\d+)\s+times", re.IGNORECASE)


# ══════════════════════════════════════════════════════════════════════════════
# PASSING-TEST DETECTION (for the late check)
# ══════════════════════════════════════════════════════════════════════════════
_TESTCMD = re.compile(r"\b(?:pytest|py\.test|python[0-9.]*\s+-m\s+pytest|unittest|nosetests|tox)\b")
_PROBE_CMD = re.compile(
    r"\b(?:python[0-9.]*\s+(?:-c\b|-\s*<<|<<)|node\s+(?:-e\b|<<)|go\s+run\b)")
_RETURNCODE = re.compile(r"\s*<returncode>(-?\d+)</returncode>")
_POSITIVE_PASS_SUMMARY = re.compile(
    r"^[=\s]*[1-9]\d*\s+passed"
    r"(?:,\s*\d+\s+(?:skipped|deselected|xfailed|xpassed|warnings?))*"
    r"(?:\s+in\s+[0-9.]+s)?[=\s]*$", re.IGNORECASE | re.MULTILINE)
_POSITIVE_FAILURE_COUNT = re.compile(r"\b([1-9]\d*)\s+(?:failed|errors?)\b", re.IGNORECASE)
_OBL_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def passing_test_msgs(msgs: list[dict]) -> list[int]:
    """Tool-observation indices where a test RUN passed (rc==0, 'passed', no failure marker)."""
    out: list[int] = []
    commands = _commands_by_tool_message(msgs)
    for i, m in enumerate(msgs):
        role = m.get("role")
        if role != "tool":
            continue
        cmd = commands.get(i, "")
        content = m.get("content", "") if isinstance(m.get("content"), str) else ""
        if not _TESTCMD.search(cmd):
            continue
        rc = _RETURNCODE.match(content)
        if not rc:
            continue
        rcv = int(rc.group(1))
        # Require a structured positive pass count. Substring checks admit `0 passed`,
        # `bypassed`, and negated prose, all of which falsely reset the coherence boundary.
        if (rcv == 0 and _POSITIVE_PASS_SUMMARY.search(content)
                and not _POSITIVE_FAILURE_COUNT.search(content)):
            out.append(i)
    return out


def _command_events_by_tool_message(msgs: list[dict]) -> dict[int, tuple[int, str]]:
    """Pair each result with ``(assistant index, command)`` without losing call IDs."""
    pending: list[tuple[str | None, int, str]] = []
    paired: dict[int, tuple[int, str]] = {}
    for index, message in enumerate(msgs):
        if message.get("role") == "assistant":
            calls = message.get("tool_calls") if isinstance(message.get("tool_calls"), list) else []
            if calls:
                for call in calls:
                    if not isinstance(call, dict):
                        continue
                    call_id = str(call.get("id")) if call.get("id") else None
                    try:
                        args = json.loads((call.get("function") or {}).get("arguments") or "{}")
                    except (TypeError, json.JSONDecodeError):
                        continue
                    command = args.get("command") if isinstance(args, dict) else None
                    if isinstance(command, str) and command.strip():
                        pending.append((call_id, index, command))
            else:
                pending.extend((None, index, command) for command in sso._commands_of(message))
            continue
        if message.get("role") != "tool" or not pending:
            continue
        tool_id = str(message.get("tool_call_id") or "")
        if tool_id:
            matches = [pos for pos, (call_id, _message_index, _command) in enumerate(pending)
                       if call_id == tool_id]
            if len(matches) == 1:
                _call_id, message_index, command = pending.pop(matches[0])
                paired[index] = (message_index, command)
        elif len(pending) == 1 and pending[0][0] is None:
            _call_id, message_index, command = pending.pop(0)
            paired[index] = (message_index, command)
    return paired


def _commands_by_tool_message(msgs: list[dict]) -> dict[int, str]:
    """Pair actions to results by tool-call id; use FIFO only when unambiguous."""
    return {result: command for result, (_assistant, command)
            in _command_events_by_tool_message(msgs).items()}


def _obligation_term_groups(payload: str) -> list[set[str]]:
    """Canonical term set for every independently rendered obligation row."""
    return [set(group) for group in rendered_obligation_subject_groups(payload)]


def _terms_covered(terms: set[str], command: str, output: str) -> bool:
    if not terms:
        return False
    evidence = {token.lower() for token in _OBL_TOKEN.findall(
        (command or "") + "\n" + (output or ""))}
    for term in terms:
        if term in evidence:
            continue
        if "_" in term and any(term in token for token in evidence):
            continue
        if any(term in re.split(r"[_\d]+", token) for token in evidence if "_" in token):
            continue
        # One-way, bounded inflection: a sufficiently distinctive obligation term may
        # appear in evidence as its ordinary verb form (match -> matching/matches).
        # Never stem evidence back to a shorter token: that made `unit` match `united`.
        if len(term) >= 5 and any(token in {term + "s", term + "es", term + "ed", term + "ing"}
                                  for token in evidence):
            continue
        return False
    return True


def _assistant_accepted_probe(msgs: list[dict], probe_msg: int, home_msg: int) -> bool:
    for message in msgs[probe_msg + 1:home_msg]:
        if message.get("role") == "tool":
            return False
        if message.get("role") != "assistant":
            continue
        prose = str(message.get("content") or "").strip().lower()
        # Receipt must be the assistant's affirmative statement, not a positive phrase
        # embedded under negation (`does not mean all tests passed`, `false that ...`).
        positive = re.compile(
            r"^(?:all (?:tests?|checks?) pass(?:ed)?|"
            r"all (?:looks?|results?) correct|"
            r"(?:it|that|this|they) (?:works?|passes?) correctly|"
            r"results? (?:are|look) correct)\b")
        commands = " ".join(sso._commands_of(message))
        lines = prose.splitlines(keepends=True)
        for line_index, raw_line in enumerate(lines):
            paragraph = raw_line.rstrip("\r\n").lstrip(" -*_`#")
            match = positive.match(paragraph)
            if not match:
                continue
            tail = paragraph[match.end():] + "".join(lines[line_index + 1:])
            tail = tail.lstrip(" \t\r\n*_`")
            if tail.startswith("?"):
                continue
            tail = tail.lstrip(".!: \t")
            if not tail:
                return True
            # A positive receipt may share the turn with a stated next verification
            # action, but only when that same assistant message actually launches a test.
            if (re.fullmatch(
                    r"(?:now\s+)?let me\s+(?:run|check|verify|test)\b[^.\n]*[.!:]?",
                    tail)
                    and _TESTCMD.search(commands)):
                return True
        return False
    return False


def _probe_has_checked_expectations(command: str, output: str, terms: set[str],
                                    *, returncode: int = 0, turn: int = 0) -> bool:
    """Delegate intrinsic execution truth; chronology and acknowledgment stay here."""
    return classify_checked_behavioral_proof(
        command, output, returncode, terms, turn=turn) is not None


def _transport_result(content: str) -> tuple[int, str] | None:
    """Parse the trajectory wrapper while preserving the original command output bytes."""
    match = _RETURNCODE.match(content or "")
    if not match:
        return None
    output = (content or "")[match.end():]
    if output.startswith("\r\n"):
        output = output[2:]
    elif output.startswith("\n"):
        output = output[1:]
    return int(match.group(1)), output


def requirement_evidence_before(payload: str, msgs: list[dict], home_msg: int,
                                passing: list[int]) -> int | None:
    """First prior evidence covering any complete, independently rendered row."""
    groups = _obligation_term_groups(payload)
    if not groups:
        return None
    commands = _commands_by_tool_message(msgs)
    for index, message in enumerate(msgs[:home_msg + 1]):
        if message.get("role") != "tool":
            continue
        command = commands.get(index, "")
        output = str(message.get("content") or "")
        if index in passing and any(
                _terms_covered(terms, command, output) for terms in groups):
            return index
        result = _transport_result(output)
        if (bool(_PROBE_CMD.search(command))
                and result is not None
                and result[0] == 0
                and _assistant_accepted_probe(msgs, index, home_msg)):
            if any(_probe_has_checked_expectations(
                    command, result[1], terms,
                    returncode=result[0], turn=index) for terms in groups):
                return index
    return None


# ══════════════════════════════════════════════════════════════════════════════
# DATA MODEL
# ══════════════════════════════════════════════════════════════════════════════
@dataclass
class Violation:
    kind: str          # step_behind | late | coherence_miscount | provenance | unexecuted_cover | leak | dose | unjoined
    check: str         # a | b | c | d | e
    home_msg: int
    layer: str
    detail: str
    payload_head: str


@dataclass
class DeliveryGrade:
    home_msg: int
    iteration: int
    layer: str
    chars: int
    file_path: str
    payload_head: str
    joined: bool
    violations: list[Violation] = field(default_factory=list)
    ack_ledger: bool = False       # SS-0 ledger ack field present+true
    ack_independent: int = -1      # msg index of the first later assistant reference, or -1
    acted_independent: int = -1    # later entity-targeting assistant command, or -1
    receipt_level: int = 1         # 1 delivered, 2 referenced, 3 relevant action
    pbucket: str = ""

    @property
    def acknowledged(self) -> bool:
        return self.receipt_level >= 2 or self.ack_ledger or self.ack_independent >= 0

    @property
    def clean(self) -> bool:
        return not self.violations and self.joined


@dataclass
class TaskReport:
    task: str
    n_messages: int
    resolved: bool | None
    deliveries: list[DeliveryGrade]
    residual_leaks: list[str]
    ss_reason_counts: dict[str, int]
    shadow_holdout_count: int = 0    # SS-8 shadow-holdout rows (chars=0, NOT deliveries)

    @property
    def violations(self) -> list[Violation]:
        v = [x for d in self.deliveries for x in d.violations]
        # unjoined seals are a task-level delivery failure too
        return v

    @property
    def unjoined(self) -> list[DeliveryGrade]:
        return [d for d in self.deliveries if not d.joined]

    @property
    def ack_count(self) -> int:
        return sum(1 for d in self.deliveries if d.acknowledged)

    @property
    def smoke_pass(self) -> bool:
        return (not self.violations) and (not self.unjoined) and self.ack_count > 0


# ══════════════════════════════════════════════════════════════════════════════
# PER-DELIVERY GRADING
# ══════════════════════════════════════════════════════════════════════════════
def _basename(p: str) -> str:
    return (p or "").rstrip("/").split("/")[-1]


def grade_delivery(d: "sso.Delivery", msgs: list[dict], acq_cache: dict,
                   passing: list[int], ledger_row: dict,
                   receipt_level: int = 1) -> DeliveryGrade:
    payload = d.payload or ""
    head = payload.strip().replace("\n", " ")[:80]
    joined = d.home_msg >= 0
    g = DeliveryGrade(home_msg=d.home_msg, iteration=d.iteration, layer=d.layer,
                      chars=d.chars, file_path=str(d.file_path or ""),
                      payload_head=head, joined=joined,
                      receipt_level=max(1, int(receipt_level or 1)))
    if not joined:
        g.violations.append(Violation("unjoined", "1", d.home_msg, d.layer,
                                      f"seal {d.sha16} ({d.chars}c) not located in observation stream",
                                      head))
        return g

    # ── (a) STEP_BEHIND ────────────────────────────────────────────────────────
    # A scratch-path subject is handled by the provenance check, not step_behind (a tmp file
    # is not a "self-acquired repo file"). Guard against that spurious double-flag.
    if d.layer in RELATION_LAYERS and not is_scratch_path(str(d.file_path or "")):
        subj = str(d.file_path or "").lstrip("./")
        if d.home_msg not in acq_cache:
            acq_cache[d.home_msg] = acquisition_before(msgs, d.home_msg)
        acq = acq_cache[d.home_msg]
        # step_behind iff the fact's SUBJECT file was already body-read by the agent, and its
        # cited entities are not net-new cross-file targets beyond that acquired set.
        ents = cited_paths(payload)
        subj_read = bool(subj) and subj in acq
        all_cited_read = bool(ents) and ents <= acq
        if subj_read or all_cited_read:
            why = ("subject %s already body-read" % subj) if subj_read else \
                  "all cited refs already body-read"
            g.violations.append(Violation("step_behind", "a", d.home_msg, d.layer,
                                          why + " (ss_step_behind should have fired)", head))

    # ── (b) LATE ────────────────────────────────────────────────────────────────
    if d.layer in OBLIGATION_LAYERS:
        evidence_msg = requirement_evidence_before(payload, msgs, d.home_msg, passing)
        if evidence_msg is not None:
            g.violations.append(Violation("late", "b", d.home_msg, d.layer,
                                          "requirement-covering evidence at m%s precedes obligation"
                                          % evidence_msg, head))

    # ── (c1) COHERENCE MISCOUNT ──────────────────────────────────────────────────
    if d.layer in COHERENCE_LAYERS:
        mm = _COHERENCE_CLAIM.search(payload)
        if mm:
            fname, claimed = _basename(mm.group(1)), int(mm.group(2))
            actual = len(_writes_to_basename(
                msgs, fname, before=d.home_msg, passing=passing,
            ))
            if claimed != actual:
                g.violations.append(Violation("coherence_miscount", "c", d.home_msg, d.layer,
                                              f"claims rewritten {fname} {claimed}x; actual "
                                              f"write-commands = {actual}", head))

    # ── (c2) PROVENANCE (tmp / scratch / generated) ─────────────────────────────
    prov = scratch_paths_in(str(d.file_path or "")) + scratch_paths_in(payload)
    prov = list(dict.fromkeys(prov))
    if prov:
        g.violations.append(Violation("provenance", "c", d.home_msg, d.layer,
                                      "scratch/generated provenance: " + ", ".join(prov[:3]), head))

    # ── (c3) UNEXECUTED COVERING ASSURANCE ──────────────────────────────────────
    # Fires ONLY on an asserted covering VERDICT ("fails/passes/breaks a covering test") that
    # carries NO execution evidence (traceback / exit / error / line N / assert). An advisory
    # that merely says a covering test EXISTS ("a covering test covers them — consider running")
    # asserts no verdict and is correct-or-quiet, so it is not flagged; and the EXECUTED form
    # (a real traceback) carries evidence, so it passes too.
    low = payload.lower()
    if re.search(r"(?:fails?|passe[sd]|break[s]?|broke|red|green)\s+(?:a\s+|the\s+)?covering[ -]?test", low) \
       and not re.search(r"traceback|<exit|exit\s+code|assert|error|\bfailed\b|line \d+", low):
        g.violations.append(Violation("unexecuted_cover", "c", d.home_msg, d.layer,
                                      "covering-test VERDICT asserted with no execution evidence", head))

    # ── (d) LEAK ─────────────────────────────────────────────────────────────────
    hits = leak_scan(payload)
    if hits:
        g.violations.append(Violation("leak", "d", d.home_msg, d.layer,
                                      "test-identifier leak: " + ", ".join(sorted(set(hits))[:4]),
                                      head))

    # ── (3) ACKNOWLEDGMENT — SS-0 ledger ack field (when present) ────────────────
    ack_field = ledger_row.get("ack") if isinstance(ledger_row, dict) else None
    if ack_field in (True, 1, "1", "true", "acked", "referenced", "acted"):
        g.ack_ledger = True
        g.receipt_level = max(g.receipt_level, 2)

    # ── (3) ACKNOWLEDGMENT — independent later-reference check ────────────────────
    g.ack_independent = _first_reference(d, msgs)
    if g.ack_independent >= 0:
        g.receipt_level = max(g.receipt_level, 2)
    g.acted_independent = _first_action_reference(d, msgs)
    return g


def _delivery_entities(d: "sso.Delivery") -> set[str]:
    """Non-scratch file entities carried by one delivery."""
    ents: set[str] = set()
    fp = str(d.file_path or "")
    if fp and not is_scratch_path(fp):
        ents.add(_basename(fp))
    # cited entities: both `path:line` caller refs AND bare path citations (consensus.scope
    # names in-scope files without a line, e.g. "subapi/config.py — in GT scope").
    payload = d.payload or ""
    for p in cited_paths(payload):
        if not is_scratch_path(p):
            ents.add(_basename(p))
    for p in PATH_TOK.findall(payload):
        if not is_scratch_path(p):
            ents.add(_basename(p))
    return {e for e in ents if e and len(e) >= 4}


def _first_reference(d: "sso.Delivery", msgs: list[dict]) -> int:
    """First later assistant prose/action reference to a delivered entity, or -1."""
    ents = _delivery_entities(d)
    if not ents:
        return -1
    pats = [re.compile(r"\b" + re.escape(e) + r"\b") for e in ents]
    for i in range(d.home_msg + 1, len(msgs)):
        m = msgs[i]
        if m.get("role") != "assistant":
            continue
        blob = m.get("content", "") if isinstance(m.get("content"), str) else ""
        blob += " " + " ".join(sso._commands_of(m))
        if any(p.search(blob) for p in pats):
            return i
    return -1


def _first_action_reference(d: "sso.Delivery", msgs: list[dict]) -> int:
    """First later assistant command targeting a delivered entity, or -1.

    This is deliberately separate from generic receipt 3. For a scope constraint,
    inspecting a named not-yet-touched file is the relevant scope-validation
    action; for other classes the shared receipt classifier still requires a
    mutation or verification command.
    """
    ents = _delivery_entities(d)
    if not ents:
        return -1
    pats = [re.compile(r"\b" + re.escape(e) + r"\b") for e in ents]
    for i in range(d.home_msg + 1, len(msgs)):
        m = msgs[i]
        if m.get("role") != "assistant":
            continue
        commands = " ".join(sso._commands_of(m))
        if commands and any(p.search(commands) for p in pats):
            return i
    return -1


def apply_dose(grades: list[DeliveryGrade]) -> None:
    """(e) DOSE — mark every delivery beyond the first that homed to the same observation."""
    by_home: dict[int, list[DeliveryGrade]] = {}
    for g in grades:
        if g.joined:
            by_home.setdefault(g.home_msg, []).append(g)
    for home, gs in by_home.items():
        if len(gs) > 1:
            for g in gs[1:]:
                g.violations.append(Violation("dose", "e", home, g.layer,
                                              f"{len(gs)} payloads homed to observation m{home}",
                                              g.payload_head))


def _assign_pbucket(g: DeliveryGrade) -> str:
    """Worst-first P-bucket (SS pipeline rollup). Precedence P1 > P2 > P3 > P5 > P3a-ack.

    P1  bad info reached / attempted at the model (coherence miscount, scratch provenance,
        leak, dose, or an unexecuted covering assurance, or a failed byte-join).
    P2  step_behind (the agent had self-acquired the fact — the SS 'very bad' rung).
    P3  late (delivered after the requirement already had passing evidence).
    P5  clean + acknowledged + an executed/companion/recovery class (the consumed-good rung).
    P3a-ack  every other clean delivery (delivered novel; acknowledged-or-not, non-P5 class).
    """
    kinds = {v.kind for v in g.violations}
    if kinds & {"coherence_miscount", "provenance", "leak", "dose", "unexecuted_cover", "unjoined"}:
        return "P1"
    if "step_behind" in kinds:
        return "P2"
    if "late" in kinds:
        return "P3"
    acted = g.receipt_level >= 3 or (
        g.layer == "consensus.scope" and g.acted_independent >= 0
    )
    if acted and g.layer in P5_CLASSES:
        return "P5"
    return "P3a-ack"


# ══════════════════════════════════════════════════════════════════════════════
# TASK AUDIT
# ══════════════════════════════════════════════════════════════════════════════
def _load_resolved(task_dir: Path, task: str) -> bool | None:
    rj = task_dir / "report.json"
    if rj.is_file():
        try:
            d = json.loads(rj.read_text(encoding="utf-8"))
            node = d.get(task) if isinstance(d, dict) else None
            if isinstance(node, dict) and "resolved" in node:
                return bool(node["resolved"])
        except Exception:  # noqa: BLE001
            pass
    rw = task_dir / "reward.txt"
    if rw.is_file():
        try:
            return rw.read_text(encoding="utf-8").strip().startswith("1")
        except Exception:  # noqa: BLE001
            pass
    return None


def _ledger_rows_by_seal(task_dir: Path, task: str) -> dict[str, dict]:
    """Map content_sha256_16 -> its full ledger row (for the SS-0 ack field join)."""
    out: dict[str, dict] = {}
    p = task_dir / f"gt_runtime_ledger_{task}.jsonl"
    if not p.is_file():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        sha = r.get("content_sha256_16")
        if sha:
            out[str(sha)] = r
    return out


# ── reconstruct memo ─────────────────────────────────────────────────────────
# The proven byte-exact seal join (sso.reconstruct_task) is O(trajectory size) and can take
# ~2 min on a 300-message task. It is deterministic in the recorded artifacts, so we memo its
# output keyed by a hash of the exact (trajectory + ledger) bytes: the join runs ONCE per task,
# and every re-run of the verdict (or a test re-run) is instant. The memo NEVER changes the
# join logic — it stores and replays reconstruct_task's own output. Disable with GT_SSA_NO_CACHE=1.
_MEMO: dict[str, "sso.ReconstructedTask"] = {}


def _reconstruct_cached(task: str, root: Path) -> "sso.ReconstructedTask":
    task_dir = root / task
    traj_p = task_dir / "mini-swe-agent.trajectory.json"
    led_p = task_dir / f"gt_runtime_ledger_{task}.jsonl"
    key = hashlib.sha256(traj_p.read_bytes() + led_p.read_bytes()).hexdigest()[:16]
    memo_key = f"{task}:{key}"
    if memo_key in _MEMO:
        return _MEMO[memo_key]
    disabled = os.environ.get("GT_SSA_NO_CACHE") == "1"
    cache = root / ".ss_smoke_cache" / f"{task}.{key}.json"
    if not disabled and cache.is_file():
        try:
            blob = json.loads(cache.read_text(encoding="utf-8"))
            recon = sso.ReconstructedTask(
                task=task, pairs=[],
                recorded_deliveries=[sso.Delivery(**d) for d in blob["recorded_deliveries"]],
                residual_leaks=list(blob["residual_leaks"]),
                raw_rows=list(blob["raw_rows"]),
                n_messages=int(blob["n_messages"]), rcs=[])
            _MEMO[memo_key] = recon
            return recon
        except Exception:  # noqa: BLE001 — a bad/stale cache is recomputed, never trusted
            pass
    recon = sso.reconstruct_task(task, root)
    if not disabled:
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps({
                "recorded_deliveries": [asdict(d) for d in recon.recorded_deliveries],
                "residual_leaks": recon.residual_leaks,
                "raw_rows": recon.raw_rows,
                "n_messages": recon.n_messages,
            }), encoding="utf-8")
        except Exception:  # noqa: BLE001 — caching is best-effort, never fatal
            pass
    _MEMO[memo_key] = recon
    return recon


def audit_task(task: str, root: Path) -> TaskReport:
    task_dir = root / task
    recon = _reconstruct_cached(task, root)
    traj = json.loads((task_dir / "mini-swe-agent.trajectory.json").read_text(encoding="utf-8"))
    msgs = traj["messages"]
    passing = passing_test_msgs(msgs)
    seal_rows = _ledger_rows_by_seal(task_dir, task)
    receipt_ledger = cl.build_consumption_ledger(
        traj, runtime_ledger_path=str(task_dir / f"gt_runtime_ledger_{task}.jsonl")
    )
    receipt_by_seal: dict[str, int] = {}
    for entry in receipt_ledger.get("entries") or []:
        if not isinstance(entry, dict) or entry.get("joined") is not True:
            continue
        seal = str(entry.get("content_sha256_16") or "")
        if seal:
            receipt_by_seal[seal] = max(
                receipt_by_seal.get(seal, 0), int(entry.get("receipt") or 0)
            )
    acq_cache: dict[int, set[str]] = {}

    grades: list[DeliveryGrade] = []
    for d in recon.recorded_deliveries:
        # SS-8 shadow-holdout rows (outcome="shadow_holdout", chars=0) are NOT model-visible
        # deliveries and NOT violations — never grade them (they carry no sealed bytes, so they
        # must not be counted as a P1 chars-mismatch or a dark delivery). Counted separately.
        if d.chars <= 0 or "delivered" not in d.outcome or "shadow_holdout" in d.outcome:
            continue
        ledger_row = seal_rows.get(d.sha16 or "", {})
        grades.append(grade_delivery(
            d, msgs, acq_cache, passing, ledger_row,
            receipt_level=receipt_by_seal.get(d.sha16 or "", 1),
        ))

    apply_dose(grades)

    for g in grades:
        g.pbucket = _assign_pbucket(g)

    # (4) SS suppression-health reason counts from the raw ledger
    ss_counts: dict[str, int] = {}
    shadow = 0
    for r in recon.raw_rows:
        if "shadow_holdout" in str(r.get("outcome") or ""):
            shadow += 1
        reason = str(r.get("reason") or "")
        for tok in ("ss_step_behind", "ss_semantic_dup", "ss_provenance", "ss_late"):
            if tok in reason:
                ss_counts[tok] = ss_counts.get(tok, 0) + 1

    return TaskReport(task=task, n_messages=recon.n_messages,
                      resolved=_load_resolved(task_dir, task), deliveries=grades,
                      residual_leaks=recon.residual_leaks, ss_reason_counts=ss_counts,
                      shadow_holdout_count=shadow)


# ══════════════════════════════════════════════════════════════════════════════
# RENDER
# ══════════════════════════════════════════════════════════════════════════════
def _pbucket_rollup(reports: list[TaskReport]) -> dict[str, int]:
    roll: dict[str, int] = {"P1": 0, "P2": 0, "P3": 0, "P3a-ack": 0, "P5": 0}
    for r in reports:
        for g in r.deliveries:
            roll[g.pbucket] = roll.get(g.pbucket, 0) + 1
    return roll


def render_console(reports: list[TaskReport]) -> str:
    lines: list[str] = []
    for r in reports:
        acked = r.ack_count
        lines.append("=" * 88)
        res = {True: "RESOLVED", False: "unresolved", None: "verdict?"}[r.resolved]
        lines.append(f"TASK {r.task}  ({r.n_messages} msgs, {res})")
        lines.append(f"  deliveries={len(r.deliveries)}  joined="
                     f"{sum(1 for d in r.deliveries if d.joined)}/{len(r.deliveries)}  "
                     f"acknowledged={acked}  violations={len(r.violations)}  "
                     f"SMOKE={'PASS' if r.smoke_pass else 'FAIL'}")
        lines.append(f"  {'m#':>4} {'iter':>4} {'layer':22} {'ack':>4} {'bucket':8} payload / violations")
        for d in sorted(r.deliveries, key=lambda x: (x.home_msg if x.home_msg >= 0 else 1 << 30)):
            ack = ("L" if d.ack_ledger else "") + (f"m{d.ack_independent}" if d.ack_independent >= 0 else "-")
            lines.append(f"  {d.home_msg:>4} {d.iteration:>4} {d.layer:22} {ack:>4} {d.pbucket:8} "
                         f"{d.payload_head[:52]}")
            for v in d.violations:
                lines.append(f"        !! [{v.check}] {v.kind}: {v.detail}")
        if r.ss_reason_counts:
            lines.append(f"  ss_* reasons: {r.ss_reason_counts}")
        if r.shadow_holdout_count:
            lines.append(f"  shadow_holdout rows (skipped, not deliveries): {r.shadow_holdout_count}")
    # combined verdict
    total_v = sum(len(r.violations) for r in reports)
    total_ack = sum(r.ack_count for r in reports)
    total_unjoined = sum(len(r.unjoined) for r in reports)
    all_pass = all(r.smoke_pass for r in reports) and reports != []
    verdict = "SMOKE_PASS" if (total_v == 0 and total_ack > 0 and total_unjoined == 0 and all_pass) \
        else "SMOKE_FAIL"
    lines.append("=" * 88)
    lines.append(f"COMBINED VERDICT: {verdict}")
    lines.append(f"  tasks={len(reports)}  violations={total_v}  ack_count={total_ack}  "
                 f"unjoined_deliveries={total_unjoined}")
    lines.append(f"  P-bucket rollup: {_pbucket_rollup(reports)}")
    if verdict == "SMOKE_FAIL":
        lines.append("  FAILING INSTANCES:")
        for r in reports:
            for v in r.violations:
                lines.append(f"    - {r.task} m{v.home_msg} [{v.check}] {v.kind}: {v.detail}")
            for d in r.unjoined:
                lines.append(f"    - {r.task} UNJOINED {d.layer} it{d.iteration} ({d.chars}c)")
    return "\n".join(lines)


def build_report(reports: list[TaskReport]) -> dict:
    total_v = sum(len(r.violations) for r in reports)
    total_ack = sum(r.ack_count for r in reports)
    total_unjoined = sum(len(r.unjoined) for r in reports)
    all_pass = all(r.smoke_pass for r in reports) and reports != []
    verdict = "SMOKE_PASS" if (total_v == 0 and total_ack > 0 and total_unjoined == 0 and all_pass) \
        else "SMOKE_FAIL"
    return {
        "schema": "ss.smoke_audit.v1",
        "verdict": verdict,
        "tasks_audited": len(reports),
        "total_violations": total_v,
        "total_ack_count": total_ack,
        "total_unjoined": total_unjoined,
        "pbucket_rollup": _pbucket_rollup(reports),
        "tasks": [
            {
                "task": r.task,
                "n_messages": r.n_messages,
                "resolved": r.resolved,
                "smoke_pass": r.smoke_pass,
                "deliveries": len(r.deliveries),
                "joined": sum(1 for d in r.deliveries if d.joined),
                "ack_count": r.ack_count,
                "shadow_holdout_rows": r.shadow_holdout_count,
                "ss_reason_counts": r.ss_reason_counts,
                "violations": [
                    {"home_msg": v.home_msg, "check": v.check, "kind": v.kind,
                     "layer": v.layer, "detail": v.detail, "payload_head": v.payload_head}
                    for v in r.violations
                ],
                "acks": [
                    {"delivery_m": d.home_msg, "layer": d.layer,
                     "ack_ledger": d.ack_ledger, "ack_independent_m": d.ack_independent}
                    for d in r.deliveries if d.acknowledged
                ],
                "pbuckets": {
                    b: [d.home_msg for d in r.deliveries if d.pbucket == b]
                    for b in ("P1", "P2", "P3", "P3a-ack", "P5")
                },
            }
            for r in reports
        ],
    }


# ══════════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════════
def discover_tasks(root: Path) -> list[str]:
    out: list[str] = []
    for d in sorted(root.iterdir()):
        if d.is_dir() and (d / "mini-swe-agent.trajectory.json").is_file() \
                and (d / f"gt_runtime_ledger_{d.name}.jsonl").is_file():
            out.append(d.name)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="SS-9 automated smoke verdict tool")
    ap.add_argument("--run-dir", default=str(sso._DEFAULT_RECORDED),
                    help="the run's art/ dir (default: the recorded arm-4 dev data)")
    ap.add_argument("--tasks", default="", help="comma-separated task ids (default: all discovered)")
    ap.add_argument("--out", default="", help="write the JSON report here")
    ap.add_argument("--json", action="store_true", help="print the JSON report to stdout")
    args = ap.parse_args(argv)

    root = Path(args.run_dir)
    if not root.is_dir():
        sys.stderr.write(f"[ss-9] run dir not found: {root}\n")
        return 2
    tasks = [t.strip() for t in args.tasks.split(",") if t.strip()] or discover_tasks(root)
    if not tasks:
        sys.stderr.write(f"[ss-9] no auditable tasks under {root}\n")
        return 2

    reports = [audit_task(t, root) for t in tasks]
    report = build_report(reports)

    if args.json:
        print(json.dumps(report, indent=1))
    else:
        print(render_console(reports))
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=1), encoding="utf-8")

    return 0 if report["verdict"] == "SMOKE_PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
