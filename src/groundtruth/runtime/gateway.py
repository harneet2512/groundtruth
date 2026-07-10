"""GT Gateway kernel (G1 + G2-pure) — the mini-swe-agent single-surface completer.

One interception point (the tool-observation constructor), one normalized event, one
acquisition-outcome classifier, existing producers, one dedup chain. Every agent
action (search / view / edit / test / submit) is completed with deterministic FACTS
inside the SAME observation the model already reads.

This module is PURE, DETERMINISTIC, and LLM-free. It **replicates** the semantics of
``artifact_deepswe/gt_mini_patch.py`` (post_search parse + the listen lattice) WITHOUT
importing it — importing gt_mini_patch executes runtime monkeypatches. The search-command
parse here is proven byte-equivalent to that module's ``_search_pattern`` by the golden
corpus in ``tests/runtime/test_gateway.py`` (re-derived from ``test_post_search.py``).

Governing laws (all pre-existing; none new):
  * Master flag ``GT_GATEWAY`` — unset/0 => :func:`augment` returns ``[]`` immediately.
  * Producer sub-flags honored (``GT_CHANGE_SURFACE`` for W-A, ``GT_PATCH_DELTA`` for W-C).
  * Correct-or-quiet: wrong evidence is worse than none; every ambiguity abstains.
  * ONE PAYLOAD CONTRACT: every produced fact is an
    ``evidence_envelope.EvidenceEnvelope`` (the canonical contract; the former
    ``Addition`` capsule was REPLACED at reconciliation, option (b), 2026-07-10).
    Field mapping: ``fact_kind`` -> ``evidence_type``, ``body_lines`` -> ``payload``,
    ``evidence`` -> ``provenance``, the F1 symbol -> ``fact_id``. Every envelope
    :func:`augment` ships passes ``evidence_envelope.validate`` (a violating
    envelope is DROPPED fail-closed, never delivered).
  * LEAK LAW: no envelope may carry a test-file path/name in its ``target``,
    ``payload``, OR ``provenance``. The single predicate :func:`_is_leaky`
    (== ``not is_deliverable``) gates the target and every provenance row; a
    belt-and-braces scan in :func:`_mk_add` drops any PAYLOAD line carrying a leaky
    path-shaped token; covering-test bodies are additionally routed through the
    EXISTING identity firewall ``native_render.render_covering_failure_native``
    (Format D — never a second scrubber). On any uncertainty a path is treated as
    a test and dropped. ``validate`` law (a) re-checks target+provenance.
  * Need gate: every envelope carries the CANONICAL deterministic ``dedup_key``
    (``evidence_envelope.derive_dedup_key``: producer, evidence_type, target,
    fact_id=the F1 symbol, content=the shipped payload+provenance) — the symbol
    keeps two DISTINCT facts from colliding on a shared def file, and the content
    component lets the SAME symbol re-fire when its def-set changes (freshness).
    STAMP-AT-SEAL (bounce 2026-07-10): :func:`augment` only READS
    ``state.delivered_keys`` for suppression; the DELIVERY COMMIT (the seam's seal
    step — ``adapters.miniswe.seal_delivery(dedup_chain=...)``) stamps the key when
    the bytes are actually appended. So a repeated event yields ``[]`` only once the
    fact was DELIVERED; a produced-but-dropped fact (arbitration loss / leak-guard /
    law-8 budget) is deferred and re-offered, never destroyed.

Rendering is intentionally NOT this module's job: an envelope is a minimal decision
capsule (target + change context + facts + provenance). No ``<gt-*>`` tags,
no agent-facing string assembly beyond deterministic ``file:line`` row formatting.
"""
from __future__ import annotations

import hashlib
import os
import re
import shlex
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field

from groundtruth.delivery.path_policy import is_deliverable
from groundtruth.pretask.curation_map import DETERMINISTIC_RESOLUTION_METHODS
from groundtruth.runtime.covering_runner import _connect_ro, _edge_columns
from groundtruth.runtime.episode_state import EpisodeState
from groundtruth.runtime.evidence_envelope import (
    ADVISORY,
    EVENT_STEP0,
    HYPOTHESIS,
    INFO,
    VERIFIED,
    WARNING,
    EvidenceEnvelope,
)
from groundtruth.runtime.evidence_envelope import validate as _validate_envelope
from groundtruth.runtime.native_render import render_covering_failure_native

# Producer engines (traces / change_surface / patch_delta) are OPTIONAL module-scope
# imports (W2 ship-closure, 2026-07-10): try-wrapped so `import gateway` never crashes
# in a container that ships gateway WITHOUT the whole pretask stack
# (anchors/stratum/cochange/confidence). Being under `ast.Try` they are EXCLUDED from
# the injection import-closure guard (which only flags UNGUARDED module-scope imports
# — the real container-startup-crash risk), while staying real module attributes so
# tests can monkeypatch them. Unavailable -> None -> the producer's own try/except
# degrades correct-or-quiet (returns []). On host all three resolve normally.
try:
    from groundtruth.pretask.traces import parse_stack_traces
except Exception:  # noqa: BLE001
    parse_stack_traces = None  # type: ignore[assignment]
try:
    from groundtruth.pretask.change_surface import detect_change_surface
except Exception:  # noqa: BLE001
    detect_change_surface = None  # type: ignore[assignment]
try:
    from groundtruth.runtime.patch_delta import analyze_patch_delta
except Exception:  # noqa: BLE001
    analyze_patch_delta = None  # type: ignore[assignment]

__all__ = [
    "ToolEvent",
    "EvidenceEnvelope",  # re-export: the ONE payload contract the Gateway emits
    "CoveringResult",
    "GatewayState",
    "augment",
    "classify_outcome",
    "classify_command",
    "normalize_search",
    "search_pattern",
    # kinds
    "KIND_SEARCH", "KIND_VIEW", "KIND_EDIT", "KIND_TEST", "KIND_SUBMIT", "KIND_OTHER",
    # outcome states
    "EXACT_HIT", "SATISFIED", "AMBIGUOUS_HIT", "FLOOD", "WRONG_SURFACE", "TRACE_HIT",
    "ZERO_NAME", "ZERO_BEHAVIOR", "ZERO_ABSENT",
]

# --------------------------------------------------------------------------- #
# kind + outcome-state constants
# --------------------------------------------------------------------------- #
KIND_SEARCH = "search"
KIND_VIEW = "view"
KIND_EDIT = "edit"
KIND_TEST = "test"
KIND_SUBMIT = "submit"
KIND_OTHER = "other"

EXACT_HIT = "EXACT_HIT"           # silence — the agent already has the def
SATISFIED = "SATISFIED"           # silence — nothing to add
AMBIGUOUS_HIT = "AMBIGUOUS_HIT"   # def spans 2-3 files -> def/ref partition
FLOOD = "FLOOD"                   # a very large raw hit set -> cut with the def
WRONG_SURFACE = "WRONG_SURFACE"   # every hit is test/vendored; the def is elsewhere
TRACE_HIT = "TRACE_HIT"           # output carries an in-repo stack trace
ZERO_NAME = "ZERO_NAME"           # empty grep; name exists under a fold variant
ZERO_BEHAVIOR = "ZERO_BEHAVIOR"   # empty grep; concept only in function bodies
ZERO_ABSENT = "ZERO_ABSENT"       # empty grep; name+path+body all miss (truly absent)

# Trust tiers come from the canonical envelope module (imported above): VERIFIED /
# WARNING / INFO / HYPOTHESIS — a single vocabulary, never re-declared here.

# Deterministic tier -> default confidence (tier-honest by construction: VERIFIED
# >= 0.7 with provenance; HYPOTHESIS < 0.7). A producer with a REAL measured
# confidence (patch_delta's FACT edges) passes it explicitly instead.
_TIER_DEFAULT_CONF: dict[str, float] = {
    VERIFIED: 0.9, WARNING: 0.7, INFO: 0.5, HYPOTHESIS: 0.5,
}

# --------------------------------------------------------------------------- #
# dataclasses
# --------------------------------------------------------------------------- #
@dataclass
class ToolEvent:
    """One normalized agent tool action + its observation (the interception unit)."""

    kind: str
    command: str = ""
    output: str = ""
    exit_status: int | None = None
    cwd: str = ""
    changed_files: tuple[str, ...] = ()
    action_index: int = 0
    # Optional seam bridges — NOT part of the raw observation; injected by the seam
    # when it has them (kept off the 7 core fields so the observation stays pure).
    edit_before_after: "Mapping[str, tuple[str | None, str]] | None" = None
    covering: "CoveringResult | None" = None


@dataclass
class CoveringResult:
    """A pre-computed covering-test verdict injected on a test event.

    The Gateway does NOT execute anything; running the covering test is seam work
    (``covering_runner`` + the executor bridge). The kernel only wraps the verdict.
    """

    target: str
    verdict: str = ""
    body_lines: list[str] = field(default_factory=list)
    evidence: list[tuple[str, int]] = field(default_factory=list)
    tier: str = WARNING
    # test files the runner executed — sharpens the firewall's identity match
    test_files: tuple[str, ...] = ()


@dataclass
class GatewayState:
    """Per-run mutable context — a thin facade over the canonical :class:`EpisodeState`.

    The probe-stem ``ledger``, the ``delivered_keys`` dedup chain, and the
    ``edit_events`` record are OWNED by the embedded :class:`EpisodeState` (W1a
    strangler consolidation, 2026-07-10): they were fragmented state (the ledger is
    field-identical to the mini seam's ``_search_seen``; ``delivered_keys`` duplicated
    ``_oracle_delivered_hashes``). They are exposed here as delegating properties so
    every producer's in-place mutation (``state.ledger[stem] = e`` /
    ``state.delivered_keys.add(k)`` / ``state.edit_events.append(...)``) is byte-
    identical, while storage has exactly ONE owner. Construct with a caller-supplied
    ``episode`` (the W2 seam passes the one live episode) or let it create a fresh one."""

    graph_db: str | None = None
    repo_root: str = ""
    issue_text: str = ""
    graph_revision: str = ""
    episode: EpisodeState = field(default_factory=EpisodeState)

    @property
    def ledger(self) -> dict[str, dict[str, object]]:
        """The probe-stem ledger ``{stem: {probed_forms, probe_indices, outcomes}}`` —
        owned by :attr:`episode`.``probe_ledger`` (field-identical shape)."""
        return self.episode.probe_ledger

    @ledger.setter
    def ledger(self, value: dict[str, dict[str, object]]) -> None:
        self.episode.probe_ledger = value

    @property
    def delivered_keys(self) -> set[str]:
        """THE delivered-dedup chain (opaque dedup_key strings) — owned by
        :attr:`episode`.``delivered_dedup``."""
        return self.episode.delivered_dedup

    @delivered_keys.setter
    def delivered_keys(self, value: set[str]) -> None:
        self.episode.delivered_dedup = value

    @property
    def edit_events(self) -> list[dict[str, object]]:
        """The ordered EDIT record ``{"index": action_index, "blob": lowercase text of
        the edit's command + changed paths + after-content}`` — owned by
        :attr:`episode`.``edit_events``. The honest-negative gate mutes only on an
        edit RELATED to the probed stem (F4), never on any edit."""
        return self.episode.edit_events

    @edit_events.setter
    def edit_events(self, value: list[dict[str, object]]) -> None:
        self.episode.edit_events = value


# --------------------------------------------------------------------------- #
# flags
# --------------------------------------------------------------------------- #
def _gateway_on() -> bool:
    return os.environ.get("GT_GATEWAY", "").strip().lower() not in ("", "0", "false", "no", "off")


# --------------------------------------------------------------------------- #
# LEAK LAW — the single chokepoint (== not is_deliverable), fail-closed on error.
# --------------------------------------------------------------------------- #
def _is_leaky(path_or_symbol: str) -> bool:
    """True iff ``path_or_symbol`` must NOT surface as a delivered fact. A path-shaped
    value goes through ``is_deliverable`` (test/demo/docs OR vendored/generated => leaky);
    a bare symbol (no path shape) is not path-leaky. Any error => treat as leaky (drop)."""
    x = (path_or_symbol or "").strip()
    if not x:
        return False
    try:
        return not is_deliverable(x)
    except Exception:  # noqa: BLE001 — unsure => treat as a test => drop
        return True


# --------------------------------------------------------------------------- #
# path helpers (ported: repo-relative keys matching graph.db)
# --------------------------------------------------------------------------- #
_CONTAINER_ROOTS = ("/testbed/", "/home/user/", "/workspace/", "/app/", "/repo/")


def _norm_fp(file_path: str) -> str:
    p = (file_path or "").replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p.lstrip("/")


def _to_repo_rel(f: str, root: str) -> str:
    if not f:
        return f
    nf = f.replace("\\", "/")
    if nf.startswith("/"):
        for cr in _CONTAINER_ROOTS:
            if nf.startswith(cr):
                return nf[len(cr):]
        if root:
            nroot = root.replace("\\", "/").rstrip("/") + "/"
            if nf.startswith(nroot):
                return nf[len(nroot):]
        try:
            return os.path.relpath(f, root) if root else f
        except (ValueError, TypeError):
            return f
    return f


# --------------------------------------------------------------------------- #
# SEARCH normalization (byte-equivalent to gt_mini_patch._search_pattern).
# --------------------------------------------------------------------------- #
_GREP_HEAD_RE = re.compile(r"(?:^|[|&;]\s*)(?:grep|egrep|fgrep|rg)\b")
_BARE_SYMBOL_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{2,}$")
# Flags that CONSUME the next token as a value (so it is never mistaken for the
# pattern operand). -E / -T are DELIBERATELY absent (valueless in GNU grep).
_GREP_VALUE_FLAGS = frozenset({
    "-e", "--regexp", "-f", "--file", "-m", "--max-count",
    "-A", "-B", "-C", "--context", "--after-context", "--before-context", "-d",
    "-t", "--type", "--type-not", "-g", "--glob", "--iglob",
    "--include", "--exclude", "--exclude-dir", "--exclude-from",
    "-M", "--max-columns", "-j", "--threads", "--encoding",
})


@dataclass(frozen=True)
class SearchQuery:
    """The normalized shape of a grep/rg command."""

    pattern: str                    # bare-symbol operand (== search_pattern), else ""
    raw_operand: str                # dequoted operand even when not a bare symbol
    scope: tuple[str, ...]          # path operands
    filters: tuple[str, ...]        # flag tokens (best-effort)


def _search_operand_raw(cmd: str) -> str | None:
    """The DEQUOTED pattern operand of a grep/rg command, or None (no bare gate)."""
    head = (cmd or "").split("\n", 1)[0]
    m = _GREP_HEAD_RE.search(head)
    if not m:
        return None
    try:
        toks = shlex.split(head[m.end():])
    except ValueError:
        return None
    pat: str | None = None
    i = 0
    while i < len(toks):
        t = toks[i]
        if t == "--":
            pat = toks[i + 1] if i + 1 < len(toks) else None
            break
        if t.startswith("-"):
            if "=" in t:
                flag, _, val = t.partition("=")
                if flag in ("-e", "--regexp"):
                    pat = val
                    break
                i += 1
                continue
            if t in ("-e", "--regexp") and i + 1 < len(toks):
                pat = toks[i + 1]
                break
            i += 2 if t in _GREP_VALUE_FLAGS else 1
            continue
        pat = t
        break
    if not pat:
        return None
    return pat.strip().strip("'\"")


def search_pattern(cmd: str) -> str | None:
    """The grep/rg operand, ABSTAINING unless it is a single bare symbol (>=3 chars,
    no regex metachars / path separators). Byte-equivalent to gt_mini_patch._search_pattern."""
    pat = _search_operand_raw(cmd)
    if pat is None:
        return None
    return pat if _BARE_SYMBOL_RE.match(pat) else None


def normalize_search(cmd: str) -> SearchQuery | None:
    """Parse a grep/rg command into (pattern, raw_operand, scope, filters). None when
    the command is not a grep/rg / has no operand."""
    op = _search_operand_raw(cmd)
    if op is None:
        return None
    head = (cmd or "").split("\n", 1)[0]
    m = _GREP_HEAD_RE.search(head)
    try:
        toks = shlex.split(head[m.end():]) if m else []
    except ValueError:
        toks = []
    scope: list[str] = []
    filters: list[str] = []
    i = 0
    seen_pat = False
    while i < len(toks):
        t = toks[i]
        if t == "--":
            i += 1
            continue
        if t.startswith("-"):
            filters.append(t)
            if "=" not in t and t in _GREP_VALUE_FLAGS and i + 1 < len(toks):
                i += 2
                continue
            i += 1
            continue
        if not seen_pat:
            seen_pat = True  # the first bare token is the operand, not scope
        else:
            scope.append(t.strip().strip("'\""))
        i += 1
    bare = op if _BARE_SYMBOL_RE.match(op) else ""
    return SearchQuery(pattern=bare, raw_operand=op, scope=tuple(scope), filters=tuple(filters))


def _search_probe_tokens(cmd: str) -> list[str]:
    """Bare-symbol tokens in the operand (quoted multi-word / `foo|bar` split), for the
    ledger — never rendered."""
    op = _search_operand_raw(cmd)
    if op is None:
        return []
    out: list[str] = []
    for part in re.split(r"[|\s]+", op):
        p = part.strip().strip("\\")
        if p and _BARE_SYMBOL_RE.match(p) and p not in out:
            out.append(p)
    return out


# --------------------------------------------------------------------------- #
# COMMAND-kind classifier (F3, segment-head rule).
#
# The command is split into pipeline/chain SEGMENTS (quote-aware, on `|`, `||`,
# `&&`, `;`, newline — single `&` is NOT a separator so `2>&1` stays intact).
# A segment whose HEAD token is a grep-family binary is SEARCH regardless of its
# OPERAND (so `grep -rn pytest .` never classifies as a test run). The compound
# line's kind is the highest-precedence segment kind:
#   submit > test > edit > search > view > other.
# --------------------------------------------------------------------------- #
_SUBMIT_RE = re.compile(r"COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT|MINI_SWE_AGENT_FINAL_OUTPUT")
_TEST_RE = re.compile(
    r"\b(pytest|py\.test|go\s+test|cargo\s+test|npm\s+(?:run\s+)?test|yarn\s+test|"
    r"jest|mocha|vitest|tox|nosetests|unittest|rspec|phpunit|gradle\s+test|mvn\s+test)\b"
)
_EDIT_KIND_RE = re.compile(
    r"sed\s+-i|>\s*\S+\.\w+|>>\s*\S|tee\s|<<\s*['\"]?(?:EOF|PY|PATCH|TXT)|"
    r"apply_patch|patch\s+-|python\s+-\s*<<|git\s+apply\b|git\s+checkout\s+--(?:\s|$)"
)
_VIEW_SEG_RE = re.compile(r"^\s*(?:cat|less|more|head|tail|nl|sed\s+-n|view|open|bat)\b")

_GREP_BINS = frozenset({"grep", "egrep", "fgrep", "rg", "ag", "ack"})
_FIND_BINS = frozenset({"find", "fd"})
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_KIND_PRECEDENCE = {KIND_SUBMIT: 5, KIND_TEST: 4, KIND_EDIT: 3,
                    KIND_SEARCH: 2, KIND_VIEW: 1, KIND_OTHER: 0}


def _split_segments(cmd: str) -> list[str]:
    """Quote-aware split on `|`/`||`/`&&`/`;`/newline. Single `&` (as in ``2>&1``)
    is NOT a separator. Unbalanced quotes swallow the rest into one segment
    (fail-safe: never splits inside what the shell would treat as one word)."""
    segs: list[str] = []
    cur: list[str] = []
    quote: str | None = None
    i, n = 0, len(cmd or "")
    while i < n:
        c = cmd[i]
        if quote is not None:
            cur.append(c)
            if c == "\\" and quote == '"' and i + 1 < n:
                cur.append(cmd[i + 1])
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c in ("'", '"'):
            quote = c
            cur.append(c)
            i += 1
            continue
        if c in (";", "\n"):
            segs.append("".join(cur))
            cur = []
            i += 1
            continue
        if c == "|":
            segs.append("".join(cur))
            cur = []
            i += 2 if i + 1 < n and cmd[i + 1] == "|" else 1
            continue
        if c == "&" and i + 1 < n and cmd[i + 1] == "&":
            segs.append("".join(cur))
            cur = []
            i += 2
            continue
        cur.append(c)
        i += 1
    segs.append("".join(cur))
    return [s.strip() for s in segs if s.strip()]


def _seg_head(seg: str) -> str:
    """The segment's head BINARY (basename, lowercased), skipping VAR=val prefixes."""
    try:
        toks = shlex.split(seg)
    except ValueError:
        toks = seg.split()
    for t in toks:
        if _ENV_ASSIGN_RE.match(t):
            continue
        return t.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return ""


def _classify_segment(seg: str) -> str:
    head = _seg_head(seg)
    if head in _GREP_BINS:
        return KIND_SEARCH  # HEAD RULE: a grep is a search regardless of operand
    if _SUBMIT_RE.search(seg):
        return KIND_SUBMIT
    if _TEST_RE.search(seg):
        return KIND_TEST
    if _EDIT_KIND_RE.search(seg):
        return KIND_EDIT
    if head in _FIND_BINS:
        return KIND_SEARCH
    if _VIEW_SEG_RE.match(seg):
        return KIND_VIEW
    return KIND_OTHER


def classify_command(cmd: str) -> str:
    kinds = [_classify_segment(s) for s in _split_segments(cmd or "")]
    if not kinds:
        return KIND_OTHER
    return max(kinds, key=lambda k: _KIND_PRECEDENCE[k])


# --------------------------------------------------------------------------- #
# grep RESULT parsing (emptiness + hit paths) — the lattice's result half.
# --------------------------------------------------------------------------- #
_GREP_PIPE_SPLIT_RE = re.compile(r"(?<!\|)\|(?!\|)")
_GREP_STAGE_HEAD_RE = re.compile(r"^\s*(?:grep|egrep|fgrep|rg)\b")
_HIT_PATH_EXT_RE = re.compile(r"\.\w+$")


def _grep_is_final_stage(head: str) -> bool:
    seg = _GREP_PIPE_SPLIT_RE.split(head)[-1]
    return bool(_GREP_STAGE_HEAD_RE.match(seg))


def _grep_is_count(seg: str) -> bool:
    for t in seg.split():
        if t == "--count":
            return True
        if t.startswith("-") and not t.startswith("--") and "c" in t[1:]:
            return True
    return False


def _grep_result_empty(cmd: str, out: str) -> bool:
    head = (cmd or "").split("\n", 1)[0]
    if not _grep_is_final_stage(head):
        return False
    s = (out or "").strip()
    if s == "":
        return True
    seg = _GREP_PIPE_SPLIT_RE.split(head)[-1]
    if _grep_is_count(seg):
        return all(ln.strip() == "0" or ln.strip().endswith(":0")
                   for ln in s.splitlines() if ln.strip())
    return False


def _grep_hit_paths(out: str, root: str) -> set[str]:
    paths: set[str] = set()
    for ln in (out or "").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        cand = ln.split(":", 1)[0].strip()
        if cand.startswith("./"):
            cand = cand[2:]
        if not cand or " " in cand:
            continue
        if "/" in cand or _HIT_PATH_EXT_RE.search(cand):
            paths.add(_norm_fp(_to_repo_rel(cand, root)))
    return paths


# --------------------------------------------------------------------------- #
# morphology (name-fold) — ported from graph_localizer._split_camel_subtokens.
# --------------------------------------------------------------------------- #
def _split_camel_subtokens(s: str) -> list[str]:
    if not s:
        return []
    out: list[str] = []
    start = 0
    for i in range(1, len(s)):
        prev, cur = s[i - 1], s[i]
        nxt = s[i + 1] if i + 1 < len(s) else ""
        lower_to_upper = ("a" <= prev <= "z") and ("A" <= cur <= "Z")
        acronym_end = ("A" <= cur <= "Z") and ("a" <= nxt <= "z") and ("A" <= prev <= "Z")
        if lower_to_upper or acronym_end:
            out.append(s[start:i])
            start = i
    out.append(s[start:])
    return out


def _stem_subtokens(sym: str) -> list[str]:
    parts: list[str] = []
    for seg in (sym or "").split("_"):
        for sub in _split_camel_subtokens(seg):
            if sub:
                parts.append(sub.lower())
    return parts


def _norm_stem(sym: str) -> str:
    return "".join(_stem_subtokens(sym))


def _fold_variants(sym: str) -> list[str]:
    subs = _stem_subtokens(sym)
    variants: list[str] = []

    def add(v: str) -> None:
        if v and v not in variants:
            variants.append(v)

    add(sym)
    add(sym.lower())
    add(sym.upper())
    if subs:
        add("_".join(subs))
        add("".join(subs))
        add(subs[0] + "".join(w.capitalize() for w in subs[1:]))
        add("".join(w.capitalize() for w in subs))
        add("_".join(w.upper() for w in subs))
    return variants


# --------------------------------------------------------------------------- #
# ledger (probe history for the ZERO_ABSENT repeat gate) + dedup + graph_revision.
# --------------------------------------------------------------------------- #
def _ledger_entry(state: GatewayState, stem: str) -> dict:
    e = state.ledger.get(stem)
    if e is None:
        e = {"probed_forms": set(), "probe_indices": [], "outcomes": []}
        state.ledger[stem] = e
    return e


def _ledger_record(state: GatewayState, sym: str, idx: int, outcome: str) -> None:
    e = _ledger_entry(state, _norm_stem(sym))
    e["probed_forms"].add(sym)
    e["probe_indices"].append(int(idx))
    e["outcomes"].append(outcome)


# NOTE: the dedup key is no longer derived here — EvidenceEnvelope.build derives the
# CANONICAL key (evidence_envelope.derive_dedup_key: producer, evidence_type, target,
# fact_id=the F1 symbol, content=the shipped payload+provenance). One derivation, one owner.


# Cache keyed on (path, mtime_ns, size) so a graph rebuilt/updated mid-run gets a
# fresh revision hash instead of a stale cached one.
_GRAPH_REV_CACHE: dict[tuple[str, int, int], str] = {}


def _graph_revision(state: GatewayState) -> str:
    if state.graph_revision:
        return state.graph_revision
    db = state.graph_db or ""
    if not db:
        return ""
    try:
        st = os.stat(db)
        key = (db, st.st_mtime_ns, st.st_size)
    except OSError:
        return ""
    if key in _GRAPH_REV_CACHE:
        return _GRAPH_REV_CACHE[key]
    rev = ""
    try:
        with open(db, "rb") as fh:
            h = hashlib.sha256()
            h.update(fh.read())
        rev = h.hexdigest()[:12]
    except OSError:
        rev = ""
    _GRAPH_REV_CACHE[key] = rev
    return rev


# --------------------------------------------------------------------------- #
# graph queries (read-only) — def/ref partition, name-fold, body FTS.
# --------------------------------------------------------------------------- #
_DEF_LABELS = ("Function", "Method", "Class", "Interface", "Struct", "Enum",
               "Trait", "Constructor")
_FACT_CONF_FLOOR = 0.7
_FLOOD_HITS = 20  # distinct raw hit paths above which a search is a FLOOD


def _open(state: GatewayState):
    db = state.graph_db
    if not db or not os.path.isfile(db):
        return None
    return _connect_ro(db)


def _resolve_symbol_defs(con, symbol: str, root: str) -> dict | None:
    """def-sites (1-3 non-leaky files) + FACT-tier caller provenance + test-ref COUNT.
    None when the symbol resolves to no deliverable def or spans >3 files (ambiguous)."""
    labels_sql = ",".join("?" * len(_DEF_LABELS))
    try:
        rows = con.execute(
            f"SELECT id, file_path, start_line FROM nodes "
            f"WHERE name=? AND COALESCE(is_test,0)=0 AND COALESCE(start_line,0)>0 "
            f"AND label IN ({labels_sql}) ORDER BY file_path, start_line",
            (symbol, *_DEF_LABELS)).fetchall()
    except sqlite3.Error:
        return None
    rows = [r for r in rows if not _is_leaky(_to_repo_rel(r[1] or "", root))]
    if not rows:
        return None
    if len({r[1] for r in rows}) > 3:
        return None  # ambiguous common / stdlib-shadow name -> not a fact
    def_ids = [r[0] for r in rows]
    def_sites = [(_to_repo_rel(r[1] or "", root), int(r[2] or 0)) for r in rows]
    callers, receiver_types = _fact_callers(con, def_ids)
    test_ref_count = _test_ref_count(con, def_ids)
    return {
        "def_sites": def_sites,
        "n_def_files": len({fp for fp, _ in def_sites}),
        "callers": callers,
        "receiver_types": receiver_types,
        "test_ref_count": test_ref_count,
    }


def _det_in_sql() -> str:
    return "','".join(sorted(DETERMINISTIC_RESOLUTION_METHODS))


_RECEIVER_RE = re.compile(r"(?:^|;)\s*receiver_type=([A-Za-z_][A-Za-z0-9_.]*)")


def _fact_callers(con, def_ids: list[int]) -> tuple[list[dict], list[str]]:
    """FACT-tier (deterministic method AND conf>=0.7), NON-test callers of the def ids,
    with receiver_type provenance parsed from edges.metadata when present (W-B). Leak-safe:
    is_test=0 on both ends; caller file leak-filtered by the consumer."""
    cols = _edge_columns(con)
    if not {"resolution_method"}.issubset(cols):
        return [], []
    has_conf = "confidence" in cols
    has_meta = "metadata" in cols
    conf_gate = f"AND COALESCE(e.confidence,0) >= {_FACT_CONF_FLOOR} " if has_conf else ""
    meta_sel = "e.metadata" if has_meta else "NULL"
    qmarks = ",".join("?" * len(def_ids))
    try:
        rows = con.execute(
            f"SELECT ns.name, ns.file_path, e.source_line, {meta_sel} "
            f"FROM edges e JOIN nodes ns ON ns.id=e.source_id "
            f"WHERE e.target_id IN ({qmarks}) AND e.type='CALLS' "
            f"AND COALESCE(ns.is_test,0)=0 "
            f"AND LOWER(TRIM(e.resolution_method)) IN ('{_det_in_sql()}') {conf_gate}",
            def_ids).fetchall()
    except sqlite3.Error:
        return [], []
    callers: list[dict] = []
    receivers: set[str] = set()
    for name, fp, line, meta in rows:
        callers.append({"name": name or "", "file": fp or "", "line": int(line or 0)})
        if meta:
            m = _RECEIVER_RE.search(str(meta))
            if m:
                receivers.add(m.group(1))
    callers.sort(key=lambda c: (c["file"], c["line"], c["name"]))
    return callers, sorted(receivers)


def _test_ref_count(con, def_ids: list[int]) -> int:
    """COUNT of DISTINCT is_test callers over FACT-tier edges only (never a name)."""
    qmarks = ",".join("?" * len(def_ids))
    try:
        row = con.execute(
            f"SELECT COUNT(DISTINCT e.source_id) FROM edges e "
            f"JOIN nodes sn ON sn.id=e.source_id "
            f"WHERE e.target_id IN ({qmarks}) AND e.type='CALLS' "
            f"AND LOWER(TRIM(e.resolution_method)) IN ('{_det_in_sql()}') "
            f"AND COALESCE(sn.is_test,0)=1", def_ids).fetchone()
        return int(row[0] or 0)
    except sqlite3.Error:
        return 0


def _name_or_path_matches(con, sym: str) -> bool:
    try:
        for variant in _fold_variants(sym):
            if con.execute("SELECT 1 FROM nodes WHERE name=? LIMIT 1", (variant,)).fetchone():
                return True
        low = sym.lower()
        if con.execute("SELECT 1 FROM nodes WHERE LOWER(file_path) LIKE '%'||?||'%' LIMIT 1",
                       (low,)).fetchone():
            return True
    except sqlite3.Error:
        return True  # fail toward suppression (no false absence)
    return False


def _has_content_fts(con) -> bool:
    try:
        return con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='symbol_content_fts' LIMIT 1").fetchone() is not None
    except sqlite3.Error:
        return False


def _body_rows(con, sym: str, root: str) -> list[tuple]:
    try:
        raw = con.execute(
            'SELECT n.file_path, n.start_line, n.name, n.label '
            'FROM symbol_content_fts f JOIN nodes n ON n.id=f.rowid '
            'WHERE symbol_content_fts MATCH ? AND COALESCE(n.is_test,0)=0 '
            'ORDER BY n.file_path, n.start_line LIMIT 26',
            ('"' + sym.replace('"', "") + '"',)).fetchall()
    except sqlite3.Error:
        return []
    out = []
    for fp, ln, name, label in raw:
        rel = _to_repo_rel(fp or "", root)
        if _is_leaky(rel):
            continue
        out.append((rel, int(ln or 0), name or "", (label or "").lower()))
    return out


def _namefold_hit(con, sym: str, root: str) -> tuple[str, dict] | None:
    """First fold variant that resolves to a deliverable def -> (variant, info)."""
    for variant in _fold_variants(sym):
        try:
            hit = con.execute("SELECT 1 FROM nodes WHERE name=? AND COALESCE(is_test,0)=0 LIMIT 1",
                              (variant,)).fetchone()
        except sqlite3.Error:
            return None
        if not hit:
            continue
        info = _resolve_symbol_defs(con, variant, root)
        if info and info["def_sites"]:
            return variant, info
    return None


# --------------------------------------------------------------------------- #
# CLASSIFIER
# --------------------------------------------------------------------------- #
def classify_outcome(event: ToolEvent, state: GatewayState) -> str:
    """The acquisition-outcome classifier. Deterministic; may open graph.db read-only
    to distinguish the zero-classes and the hit-classes. Records search probes into the
    ledger (the ZERO_ABSENT repeat gate depends on probe history)."""
    # 1) An in-repo, non-test stack trace in the output is a strong localizer for ANY
    #    kind (a failing test / error observation). Highest precedence.
    if _has_repo_trace(event, state):
        return TRACE_HIT

    if event.kind != KIND_SEARCH:
        return SATISFIED  # edit/test/submit/view enriched by kind-dispatch, not outcome

    sym = search_pattern(event.command)
    empty = _grep_result_empty(event.command, event.output or "")
    idx = event.action_index
    for tok in _search_probe_tokens(event.command):
        _ledger_record(state, tok, idx, "zero" if empty else "hit")

    if not sym:
        return SATISFIED  # non-bare / regex / path grep -> nothing to complete

    con = _open(state)
    if con is None:
        return SATISFIED
    root = state.repo_root
    try:
        if empty:
            if _namefold_hit(con, sym, root) is not None:
                return ZERO_NAME
            if _has_content_fts(con) and not _name_or_path_matches(con, sym) \
                    and _body_rows(con, sym, root):
                return ZERO_BEHAVIOR
            return ZERO_ABSENT
        # non-empty (hits)
        hit_paths = _grep_hit_paths(event.output or "", root)
        info = _resolve_symbol_defs(con, sym, root)
        if hit_paths and all(_is_leaky(p) for p in hit_paths):
            # every hit is test/vendored — is there a real def elsewhere?
            if info and any(_norm_fp(fp) not in hit_paths for fp, _ in info["def_sites"]):
                return WRONG_SURFACE
        if len(hit_paths) > _FLOOD_HITS:
            return FLOOD
        if info:
            if info["n_def_files"] >= 2:
                return AMBIGUOUS_HIT
            if info["n_def_files"] == 1:
                only = _norm_fp(info["def_sites"][0][0])
                # def IS among the hits -> the agent already has it (silence); def NOT in
                # the hits -> the grep surfaced refs but not the def -> nothing ambiguous
                # to disambiguate here, so stay quiet (correct-or-quiet).
                return EXACT_HIT if only in hit_paths else SATISFIED
        return SATISFIED
    finally:
        con.close()


def _has_repo_trace(event: ToolEvent, state: GatewayState) -> bool:
    if event.kind == KIND_SEARCH:
        return False
    try:
        frames = parse_stack_traces(event.output or "", state.repo_root or ".")
    except Exception:  # noqa: BLE001
        return False
    return any(not _is_leaky(_to_repo_rel(f.file, state.repo_root)) for f in frames)


# --------------------------------------------------------------------------- #
# PRODUCERS (each correct-or-quiet; each Addition provenance-carrying + hashed).
# --------------------------------------------------------------------------- #
# Path-shaped tokens inside a BODY line (belt-and-braces leak scan, F2):
# slash-joined tokens (`pkg/util.py`, `tests/helpers`) and bare source-file
# basenames (`conftest.py`). Each extracted token goes through _is_leaky.
_PATH_TOKEN_RE = re.compile(
    r"[\w.+\-]+(?:[/\\][\w.+\-]+)+"
    r"|\b[\w+\-]+\.(?:py|pyi|go|rs|js|jsx|mjs|cjs|ts|tsx|rb|java|kt|kts|cs|php"
    r"|swift|scala|c|h|cc|hh|cpp|hpp)\b"
)


def _body_line_leaky(line: str) -> bool:
    """True iff a body line carries ANY leaky (test/vendored) path-shaped token.
    Fail-closed direction: a leaky token drops the LINE, never ships redacted-ish."""
    for tok in _PATH_TOKEN_RE.findall(line or ""):
        if _is_leaky(tok):
            return True
    return False


def _event_pref(event: ToolEvent) -> str:
    """The envelope's preferred_event from the triggering kind (``other`` -> step0)."""
    k = (event.kind or "").strip().lower()
    return k if k in (KIND_SEARCH, KIND_VIEW, KIND_EDIT, KIND_TEST, KIND_SUBMIT) \
        else EVENT_STEP0


def _mk_add(state: GatewayState, event: ToolEvent, *, fact_kind: str, target: str,
            body_lines: list[str], evidence: list[tuple[str, int]], tier: str,
            producer: str, symbol: str = "",
            confidence: float | None = None) -> EvidenceEnvelope:
    """Build the CANONICAL EvidenceEnvelope for one fact. Leak filtering happens
    HERE, before the build, so the derived dedup key hashes exactly the shipped
    payload+provenance (the F1 content discipline). The symbol rides ``fact_id``."""
    body = [ln for ln in body_lines if not _body_line_leaky(ln)]
    ev = [(f, ln) for (f, ln) in evidence if not _is_leaky(f)]
    conf = _TIER_DEFAULT_CONF.get(tier, 0.5) if confidence is None else float(confidence)
    return EvidenceEnvelope.build(
        producer=producer,
        fact_id=symbol,
        target=target,
        evidence_type=fact_kind,
        payload=tuple(body),
        provenance=tuple(ev),
        confidence=conf,
        tier=tier,
        graph_revision=_graph_revision(state),
        preferred_event=_event_pref(event),
        blocking_eligibility=ADVISORY,
        estimated_cost_tokens=(len("\n".join(body)) + 3) // 4,
        measured=False,
    )


def _def_partition_body(info: dict) -> list[str]:
    lines = [f"def: {fp}:{ln}" for fp, ln in info["def_sites"][:3]]
    if info["callers"]:
        lines.append(f"fact-tier callers: {len(info['callers'])}")
    if info["receiver_types"]:
        lines.append("resolved callers via receiver type(s): " + ", ".join(info["receiver_types"]))
    if info["test_ref_count"]:
        lines.append(f"test refs: {info['test_ref_count']}")
    return lines


def _produce_def_ref_partition(event: ToolEvent, state: GatewayState, *, note: str = "") -> list[EvidenceEnvelope]:
    sym = search_pattern(event.command)
    con = _open(state)
    if not sym or con is None:
        return []
    try:
        info = _resolve_symbol_defs(con, sym, state.repo_root)
    finally:
        con.close()
    if not info or not info["def_sites"]:
        return []
    body = ([note] if note else []) + _def_partition_body(info)
    target = info["def_sites"][0][0]
    return [_mk_add(state, event, fact_kind="def_ref_partition", target=target,
                    body_lines=body, evidence=info["def_sites"], tier=VERIFIED,
                    producer="def_ref_partition", symbol=sym)]


def _produce_wrong_surface(event: ToolEvent, state: GatewayState) -> list[EvidenceEnvelope]:
    sym = search_pattern(event.command)
    con = _open(state)
    if not sym or con is None:
        return []
    try:
        info = _resolve_symbol_defs(con, sym, state.repo_root)
    finally:
        con.close()
    if not info or not info["def_sites"]:
        return []
    hit_paths = _grep_hit_paths(event.output or "", state.repo_root)
    novel = [(fp, ln) for fp, ln in info["def_sites"] if _norm_fp(fp) not in hit_paths]
    if not novel:
        return []
    body = ['your hits are all test/vendored copies; the definition is here']
    body += [f"def: {fp}:{ln}" for fp, ln in novel[:3]]
    return [_mk_add(state, event, fact_kind="wrong_surface", target=novel[0][0],
                    body_lines=body, evidence=novel, tier=VERIFIED,
                    producer="wrong_surface", symbol=sym)]


def _produce_name_fold(event: ToolEvent, state: GatewayState) -> list[EvidenceEnvelope]:
    sym = search_pattern(event.command)
    con = _open(state)
    if not sym or con is None:
        return []
    try:
        hit = _namefold_hit(con, sym, state.repo_root)
    finally:
        con.close()
    if hit is None:
        return []
    variant, info = hit
    if variant == sym:
        note = f'no grep hits for "{sym}", but it IS indexed (check your path/filetype filter)'
    else:
        note = f'"{sym}" not found; indexed as "{variant}"'
    body = [note] + _def_partition_body(info)
    return [_mk_add(state, event, fact_kind="name_fold", target=info["def_sites"][0][0],
                    body_lines=body, evidence=info["def_sites"], tier=VERIFIED,
                    producer="name_fold", symbol=sym)]


def _produce_body(event: ToolEvent, state: GatewayState) -> list[EvidenceEnvelope]:
    sym = search_pattern(event.command)
    con = _open(state)
    if not sym or con is None:
        return []
    try:
        rows = _body_rows(con, sym, state.repo_root)
    finally:
        con.close()
    if not rows:
        return []
    body = [f'{len(rows)} function bodies mention "{sym}" (no name or path match)']
    body += [f"in {label} {name} - {fp}:{ln}" for fp, ln, name, label in rows[:8]]
    evidence = [(fp, ln) for fp, ln, _n, _l in rows[:8]]
    return [_mk_add(state, event, fact_kind="body_concept", target=rows[0][0],
                    body_lines=body, evidence=evidence, tier=INFO,
                    producer="body_concept", symbol=sym)]


def _edit_related_to_stem(edit_blob: str, sym: str) -> bool:
    """True iff a recorded edit plausibly touches the probed symbol: any fold
    variant of ``sym`` appears in the edit's command / changed paths / after-content
    (all lowercased). Conservative in the honest direction: only a RELATED edit
    mutes the honest-negative; an unrelated edit never resets the gate (F4)."""
    if not edit_blob:
        return False
    for v in _fold_variants(sym):
        if v.lower() in edit_blob:
            return True
    return False


def _zero_absent_repeat_ok(event: ToolEvent, state: GatewayState) -> bool:
    """The honest-negative repeat gate: fire only on a REPEAT of an already-failed stem
    (or fold variant) — never on the first, intentional probe — and stay silent when
    an intervening edit RELATED to the probed stem occurred (the agent may have just
    created it). Unrelated edits do not reset the gate (F4)."""
    for tok in _search_probe_tokens(event.command) or [search_pattern(event.command) or ""]:
        if not tok:
            continue
        e = state.ledger.get(_norm_stem(tok))
        if not e:
            continue
        prior_zero = [i for i, o in zip(e["probe_indices"], e["outcomes"])
                      if o == "zero" and i < event.action_index]
        if not prior_zero:
            continue
        prev = max(prior_zero)
        if any(prev < ee["index"] < event.action_index
               and _edit_related_to_stem(ee["blob"], tok)
               for ee in state.edit_events):
            continue  # a RELATED edit intervened -> the agent may have created it
        return True
    return False


def _produce_change_surface(event: ToolEvent, state: GatewayState) -> list[EvidenceEnvelope]:
    if not _zero_absent_repeat_ok(event, state):
        return []  # protect the intentional first probe
    try:
        res = detect_change_surface(state.issue_text, state.repo_root, state.graph_db)
    except Exception:  # noqa: BLE001
        return []
    if res.abstained:
        return []
    out: list[EvidenceEnvelope] = []
    for d in res.destinations:
        if _is_leaky(d.suggested_path):
            continue
        # F2(a): template / registration / evidence rows go through the SAME leak
        # predicate as targets — a leaky row is dropped, the addition survives when
        # its core (suggested_path) is clean. _mk_add's belt re-checks every line.
        body = [f"new file: {d.suggested_path}"]
        if d.template_file and not _is_leaky(d.template_file):
            body.append(f"template: {d.template_file}")
        if d.registration_file and not _is_leaky(d.registration_file):
            body.append(f"integrate at: {d.registration_file}")
        body += [ev for ev in d.evidence[:4] if not _body_line_leaky(ev)]
        out.append(_mk_add(state, event, fact_kind="new_file_destination", target=d.suggested_path,
                           body_lines=body, evidence=[], tier=HYPOTHESIS,
                           producer="change_surface", symbol=d.entity))
    for m in res.missing_roles:
        tgt = m.registration_file or (m.sibling_files[0] if m.sibling_files else m.entity)
        if _is_leaky(tgt):
            continue
        ev_rows = [(m.registration_file, ln) for ln, _ in m.registration_lines] if m.registration_file else []
        body = [ev for ev in m.evidence[:4] if not _body_line_leaky(ev)]
        out.append(_mk_add(state, event, fact_kind=f"missing_role:{m.role}", target=tgt,
                           body_lines=body, evidence=ev_rows,
                           tier=HYPOTHESIS, producer="change_surface", symbol=m.entity))
    return out


def _produce_trace(event: ToolEvent, state: GatewayState) -> list[EvidenceEnvelope]:
    try:
        frames = parse_stack_traces(event.output or "", state.repo_root or ".")
    except Exception:  # noqa: BLE001
        return []
    for fr in frames:  # deepest in-repo first (parse_stack_traces ordering)
        rel = _to_repo_rel(fr.file, state.repo_root)
        if _is_leaky(rel):
            continue
        loc = f"{rel}:{fr.line}" + (f" in {fr.func}" if fr.func else "")
        return [_mk_add(state, event, fact_kind="trace_frame", target=rel,
                        body_lines=[f"deepest in-repo frame: {loc}"],
                        evidence=[(rel, fr.line)], tier=WARNING, producer="trace",
                        symbol=fr.func or rel)]
    return []


def _produce_patch_delta(event: ToolEvent, state: GatewayState) -> list[EvidenceEnvelope]:
    if not event.edit_before_after:
        return []
    try:
        res = analyze_patch_delta(dict(event.edit_before_after), state.repo_root, state.graph_db or "")
    except Exception:  # noqa: BLE001
        return []
    out: list[EvidenceEnvelope] = []
    for sm in res.signature_mismatches:
        if _is_leaky(sm.caller_file):
            continue
        body = [f"{sm.caller}() call passes {sm.positional_args} positional arg(s); "
                f"{sm.symbol}() now takes {sm.new_min_params}-{sm.new_max_params}",
                sm.call_site_text]
        out.append(_mk_add(state, event, fact_kind="signature_mismatch", target=sm.caller_file,
                           body_lines=body, evidence=[(sm.caller_file, sm.caller_line)],
                           tier=WARNING, producer="patch_delta", symbol=sm.symbol,
                           confidence=max(0.0, min(1.0, sm.confidence))))
    for cs in res.companion_surfaces:
        if _is_leaky(cs.file):
            continue
        body = [f"registers siblings {', '.join(cs.siblings)} but not '{cs.symbol}'"]
        # referencing_lines are (line_no, text) — the FILE is cs.file itself
        ev_rows = [(cs.file, int(ln)) for ln, _txt in cs.referencing_lines]
        out.append(_mk_add(state, event, fact_kind="companion_surface", target=cs.file,
                           body_lines=body, evidence=ev_rows,
                           tier=WARNING, producer="patch_delta", symbol=cs.symbol))
    for cp in res.cochange_partners:
        if _is_leaky(cp.file):
            continue
        out.append(_mk_add(state, event, fact_kind="cochange_partner", target=cp.file,
                           body_lines=[f"co-changed with the edit in {cp.count} commits"],
                           evidence=[], tier=INFO, producer="patch_delta", symbol=cp.file))
    return out


def _produce_covering(event: ToolEvent, state: GatewayState) -> list[EvidenceEnvelope]:
    """Wrap an injected covering verdict — the body goes through the EXISTING
    identity firewall (``native_render`` Format D), never raw runner stdout (F2).
    Correct-or-quiet: when nothing signal-bearing survives the firewall, abstain."""
    cov = event.covering
    if cov is None or not cov.target:
        return []
    if _is_leaky(cov.target):
        return []
    try:
        rendered = render_covering_failure_native(
            {"stdout_tail": "\n".join(cov.body_lines)},
            test_files=list(cov.test_files) or None,
        )
    except Exception:  # noqa: BLE001 — firewall failure => nothing ships
        return []
    if not rendered:
        return []
    return [_mk_add(state, event, fact_kind="covering_verdict", target=cov.target,
                    body_lines=rendered.splitlines(), evidence=list(cov.evidence),
                    tier=cov.tier or WARNING, producer="covering",
                    symbol=cov.verdict or cov.target)]


# --------------------------------------------------------------------------- #
# ENTRY POINT
# --------------------------------------------------------------------------- #
def augment(event: ToolEvent, state: GatewayState) -> list[EvidenceEnvelope]:
    """Complete one tool observation with deterministic facts. ``[]`` when the master
    flag is off, when the outcome needs nothing, or when every producer abstains."""
    if not _gateway_on():
        return []

    # Record edit events (index + a lowercase blob of command/paths/after-content)
    # for the ZERO_ABSENT ordering predicate — the honest-negative gate mutes only
    # on an edit RELATED to the probed stem (F4).
    if event.kind == KIND_EDIT:
        parts = [event.command or ""]
        parts += list(event.changed_files or ())
        if event.edit_before_after:
            for _f, (_bef, aft) in sorted(event.edit_before_after.items()):
                parts.append(aft or "")
        state.edit_events.append({
            "index": event.action_index,
            "blob": "\n".join(parts).lower(),
        })

    outcome = classify_outcome(event, state)

    additions: list[EvidenceEnvelope] = []
    # kind-dispatched producers (independent of the search-outcome lattice)
    if event.kind == KIND_EDIT:
        additions += _produce_patch_delta(event, state)
    elif event.kind == KIND_TEST:
        additions += _produce_covering(event, state)

    # outcome-dispatched producers
    if outcome == TRACE_HIT:
        additions += _produce_trace(event, state)
    elif outcome == ZERO_ABSENT:
        additions += _produce_change_surface(event, state)
    elif outcome == ZERO_NAME:
        additions += _produce_name_fold(event, state)
    elif outcome == ZERO_BEHAVIOR:
        additions += _produce_body(event, state)
    elif outcome in (AMBIGUOUS_HIT, FLOOD):
        additions += _produce_def_ref_partition(event, state)
    elif outcome == WRONG_SURFACE:
        additions += _produce_wrong_surface(event, state)
    # EXACT_HIT / SATISFIED -> silence

    # leak-law (target) + envelope-law validation + need gate (dedup), in
    # deterministic order. The validate() drop is fail-closed BY CONSTRUCTION:
    # a law-violating envelope (leak / tier dishonesty / unmeasured-enforcing /
    # tampered dedup key) is never delivered — correct-or-quiet.
    #
    # F1 (bounce 2026-07-10): production READS the dedup chain, it NEVER stamps it.
    # Stamping here destroyed produced-but-undelivered facts (an arbitration loss,
    # leak-guard drop, or law-8 budget drop downstream muted the fact for the whole
    # episode) — contradicting the seam's own deferral law ("produced-but-not-emitted
    # is DEFERRED, not destroyed"). The DELIVERY COMMIT stamps instead: the seal step
    # (adapters.miniswe.seal_delivery with dedup_chain=the episode chain) adds the
    # winner's key the moment its bytes are actually appended. A local intra-call
    # set still dedups duplicates WITHIN one production batch.
    out: list[EvidenceEnvelope] = []
    seen_this_call: set[str] = set()
    for a in additions:
        if _is_leaky(a.target):
            continue
        if _validate_envelope(a):
            continue
        if a.dedup_key in state.delivered_keys or a.dedup_key in seen_this_call:
            continue
        seen_this_call.add(a.dedup_key)
        out.append(a)
    return out
