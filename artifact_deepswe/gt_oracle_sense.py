"""GT delivery oracle — STAGE 0: stateless trajectory sensor + replay harness.

This is the SENSE layer of the stateless deterministic delivery oracle
(`ORACLE_ARCHITECTURE_PLAN.md` §1.2, §8 Stage 0). It does ONE thing: given the
agent's frozen trajectory (the ordered (action, observation) list exactly as it
appears in a mini-swe `messages[]` array), it RECOMPUTES the derived state the
oracle needs — with ZERO module-global state, ZERO side effects, and ZERO LLM.

Why a separate module (vs the logic already in gt_mini_patch._augment_output):
  - `_augment_output` MUTATES an output dict in the LIVE loop and threads ~12
    module-global counters (`_action_count`, `_source_edit_count`,
    `_blind_test_runs`, `_l5_*_fired`, `_cmd_history`, ...). It cannot be
    replayed offline and it cannot be unit-tested as a pure function.
  - The oracle's defining property (plan §1.2) is *statelessness*: "Every piece
    of state is a deterministic recomputation over T each turn. Same (C,T) ->
    same output, bit-for-bit." That requires a pure `sense(messages) -> state`
    function, which is what this module provides. Stage 2's byte-parity replay
    is impossible without it.

It REUSES gt_mini_patch's parsing primitives (the action classifier, the source
extension set, and the test-runner / pass / fail / env / compile regexes) so the
sensor and the live patch can never disagree on what counts as an edit or a test
run. The only detection this module ADDS over gt_mini_patch._classify is the
template/non-source write (e.g. `cat > x.html`) and the GT-marker delivered-set
scan — neither of which the live counter exposes.

Pure stdlib. Language-agnostic (the primitives are text patterns, not repo/task
logic). No task IDs, no gold labels, no benchmark-shape logic (LEG 1).
"""
from __future__ import annotations

import importlib.util as _ilu
import json
import os as _os
import re
import sys as _sys
from dataclasses import dataclass, field

# Reuse the SAME primitives the live patch uses, so sensor and patch never
# diverge on "is this an edit / a test run".  This module is loaded BY PATH
# (in-container at /opt/gt/, in-repo under artifact_deepswe/) rather than as a
# package, so `from . import` / a bare `import gt_mini_patch` can fail; load the
# sibling gt_mini_patch.py from THIS file's directory directly, which works in
# both deployments.  The inline fallbacks below keep the sensor standalone-safe.
def _load_sibling_gmp():
    for _name in ("gt_mini_patch", "artifact_deepswe.gt_mini_patch"):
        if _name in _sys.modules:
            return _sys.modules[_name]
    try:
        import gt_mini_patch as _m  # type: ignore
        return _m
    except Exception:  # noqa: BLE001
        pass
    sib = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "gt_mini_patch.py")
    if _os.path.isfile(sib):
        # Loading gt_mini_patch runs _install(); GT_BASELINE=1 makes that a no-op
        # so importing the sensor never patches the live environment.
        prev = _os.environ.get("GT_BASELINE")
        _os.environ.setdefault("GT_BASELINE", "1")
        try:
            spec = _ilu.spec_from_file_location("gt_mini_patch_sibling", sib)
            if spec and spec.loader:
                mod = _ilu.module_from_spec(spec)
                _sys.modules.setdefault("gt_mini_patch_sibling", mod)
                spec.loader.exec_module(mod)
                return mod
        except Exception:  # noqa: BLE001
            pass
        finally:
            if prev is None:
                _os.environ.pop("GT_BASELINE", None)
    return None


_gmp = _load_sibling_gmp()


# ---------------------------------------------------------------------------
# Primitives — reused from gt_mini_patch when importable, else inlined verbatim.
# ---------------------------------------------------------------------------
if _gmp is not None:
    _classify = _gmp._classify
    _edit_target = _gmp._edit_target
    _view_target = _gmp._view_target
    # F3 structured-action-first: normalize a STRUCTURED editor action (path +
    # file_text/new_str + an editor verb) to the SAME parser-faithful command
    # the live patch uses, so live and replay never disagree on edit detection
    # (the OH/CodeAct editor channel the bash `command` string is blind to).
    _effective_cmd = _gmp._effective_cmd
    _structured_edit = _gmp._structured_edit
    _SRC_EXT = _gmp._SRC_EXT
    _TEST_RUNNER_RE = _gmp._TEST_RUNNER_RE
    _TEST_PASS_RE = _gmp._TEST_PASS_RE
    _TEST_FAIL_RE = _gmp._TEST_FAIL_RE
    _ENV_FAIL_RE = _gmp._ENV_FAIL_RE
    _COMPILE_FAIL_RE = _gmp._COMPILE_FAIL_RE
    # Behavioral-signal primitives (delivery-engine Stage 1): ONE formula,
    # shared with the live patch, so live and replay can never disagree on
    # loop_ratio / new_state_rate / coverage (the LEGITIMACY deduction-1
    # twin-drift mitigation). Research: TIDE arXiv 2602.02196, TRAJEVAL
    # arXiv 2603.24631, arXiv 2604.02547 (see gt_mini_patch for full notes).
    _behavior_state_key = _gmp._behavior_state_key
    _obs_collapse = _gmp._obs_collapse
    compute_loop_ratio = _gmp.compute_loop_ratio
    compute_new_state_rate = _gmp.compute_new_state_rate
    symbol_tested = _gmp.symbol_tested
    edit_coverage_ratio = _gmp.edit_coverage_ratio
    test_coverage_ratio = _gmp.test_coverage_ratio
else:  # pragma: no cover - exercised only if gt_mini_patch is truly absent
    _SRC_EXT = (
        ".py", ".go", ".ts", ".tsx", ".js", ".jsx", ".rs", ".java", ".rb",
        ".c", ".cc", ".cpp", ".h", ".hpp", ".cs", ".php", ".kt", ".scala",
        ".swift",
    )

    def _classify(cmd):  # type: ignore
        return (None, None)

    def _edit_target(cmd):  # type: ignore
        return None

    def _view_target(cmd):  # type: ignore
        return None

    def _structured_edit(action):  # type: ignore
        return None

    def _effective_cmd(action):  # type: ignore
        if isinstance(action, dict):
            c = action.get("command", "")
            return c if isinstance(c, str) else ""
        return str(action) if action is not None else ""

    _TEST_RUNNER_RE = re.compile(r"(?:pytest|go\s+test|cargo\s+test)\b", re.I)
    _TEST_PASS_RE = re.compile(r"(\b\d+ passed\b|test result: ok\b)", re.M)
    _TEST_FAIL_RE = re.compile(r"(\bFAILED\b|\b\d+ failed\b)")
    _ENV_FAIL_RE = re.compile(r"(ModuleNotFoundError|No module named)", re.I)
    _COMPILE_FAIL_RE = re.compile(r"(error\[E\d+\]|\bSyntaxError\b)")

    def _obs_collapse(obs, n=400):  # type: ignore
        return " ".join((obs or "").split())[:n]

    def _behavior_state_key(cmd, raw_obs):  # type: ignore
        import hashlib as _h
        head = (cmd or "").strip().split(None, 1)[0] if (cmd or "").strip() else ""
        oh = _h.sha256(_obs_collapse(raw_obs).encode("utf-8", "replace")).hexdigest()[:8]
        return f"cmd\x00{head}\x00{oh}"

    def compute_loop_ratio(signatures):  # type: ignore
        sigs = list(signatures or ())
        if not sigs:
            return 0.0
        counts: dict = {}
        for s in sigs:
            counts[s] = counts.get(s, 0) + 1
        return sum(c for c in counts.values() if c >= 2) / len(sigs)

    def compute_new_state_rate(state_keys, window=12):  # type: ignore
        keys = list(state_keys or ())
        if not keys:
            return 1.0
        tail = keys[-window:]
        base = len(keys) - len(tail)
        new = 0
        for i, k in enumerate(tail):
            gi = base + i
            if k not in keys[max(0, gi - window):gi]:
                new += 1
        return new / len(tail)

    def symbol_tested(sym, tested_tokens):  # type: ignore
        if not sym:
            return False
        tested = tested_tokens or ()
        if sym in tested:
            return True
        compound = ("_" in sym or "." in sym or any(c.isdigit() for c in sym)
                    or any(c.isupper() for c in sym[1:]))
        return compound and any(sym in t for t in tested)

    def edit_coverage_ratio(obligation_syms, edited_tokens):  # type: ignore
        syms = {s for s in (obligation_syms or ()) if s}
        if not syms:
            return None
        return len({s for s in syms if s in (edited_tokens or ())}) / len(syms)

    def test_coverage_ratio(edited_tokens_by_file, tested_tokens):  # type: ignore
        files = dict(edited_tokens_by_file or {})
        if not files:
            return None
        tested = set(tested_tokens or ())
        covered = 0
        for _f, toks in files.items():
            idents = {t for t in (toks or ()) if len(t) >= 4}
            if idents & tested or any(
                    symbol_tested(t, tested) for t in sorted(idents)[:50]):
                covered += 1
        return covered / len(files)


# Template / non-`_SRC_EXT` write shapes the live edit-counter does not treat as
# an edit (aiomonitor cmd 57: `cat > .../snapshots.html`).  These are real file
# writes; the sensor records them as edits-to-NON-SOURCE so the oracle can reason
# about them without polluting the source-edit count.
_NONSRC_EXT = (
    ".html", ".htm", ".jinja", ".jinja2", ".j2", ".mustache", ".hbs",
    ".css", ".scss", ".sass", ".less", ".vue", ".svelte",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".md", ".rst", ".txt",
    ".xml", ".proto", ".graphql", ".sql",
)

# GT's own emissions, as they appear in the agent's observations (plan §1.2:
# "GT's own emissions appear IN the trajectory; scan T for GT markers").  These
# are the stateless delivered-set anchors — no side-channel ledger.
_GT_MARKER_RE = re.compile(r"<(gt-[a-z]+)(?:\s+reason=\"([^\"]+)\")?")
# Anything from the first GT marker to end is GT-injected; the agent's RAW
# observation (for a faithful decision replay) is the text before it.
_GT_FIRST_MARKER_RE = re.compile(r"<gt-[a-z]+")


# ---------------------------------------------------------------------------
# Trajectory loading — the canonical mini-swe format.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Turn:
    """One (action, observation) step recovered from a trajectory.

    `command`  — the bash command the agent ran (or "" for a no-action turn).
    `raw_obs`  — the observation with GT's own injected blocks STRIPPED (the text
                 the command itself produced — what a faithful replay decides on).
    `full_obs` — the observation exactly as recorded (incl. any GT blocks).
    `index`    — the assistant-message index in the original messages array.
    """
    index: int
    command: str
    raw_obs: str
    full_obs: str


def _command_from_assistant(msg: dict) -> str | None:
    """Back-compat single-command accessor — returns the FIRST command of an
    assistant turn (or None).  The replay path uses `_actions_from_assistant`
    instead, which preserves EVERY tool_call (a turn may batch N commands)."""
    acts = _actions_from_assistant(msg)
    return acts[0][1] if acts else None


def _actions_from_assistant(msg: dict) -> list[tuple[str | None, str]]:
    """Every (tool_call_id, command) the assistant turn issued, in order.

    A mini-swe assistant turn may carry MULTIPLE tool_calls (a batched action),
    each producing its own `tool` observation linked by `tool_call_id`.  Missing
    that produced orphaned observations (and dropped GT nudges) in the first
    loader.  Primary source: `tool_calls[].function.arguments` (JSON
    {"command": ...}); fallback: `extra.actions[].command`."""
    out: list[tuple[str | None, str]] = []
    for tc in (msg.get("tool_calls") or []):
        tcid = (tc or {}).get("id")
        fn = (tc or {}).get("function") or {}
        args = fn.get("arguments")
        argd = None
        if isinstance(args, str):
            try:
                argd = json.loads(args)
            except Exception:  # noqa: BLE001
                argd = None
        elif isinstance(args, dict):
            argd = args
        # F3 parity: route the FULL args dict through _effective_cmd so a
        # STRUCTURED editor action (editor verb + path + file_text/new_str —
        # blind to the bare `command` string) is normalized to the SAME
        # parser-faithful command the live patch sees. A bash action -> its
        # `command` string unchanged. (Live/replay can never disagree.)
        cmd = None
        if isinstance(argd, dict):
            if _structured_edit(argd) is not None:
                cmd = _effective_cmd(argd)
            else:
                cmd = argd.get("command")
        if isinstance(cmd, str) and cmd:
            out.append((tcid, cmd))
    if out:
        return out
    extra = msg.get("extra") or {}
    for act in (extra.get("actions") or []):
        ad = act or {}
        if isinstance(ad, dict) and _structured_edit(ad) is not None:
            cmd = _effective_cmd(ad)
        else:
            cmd = ad.get("command")
        if isinstance(cmd, str) and cmd:
            out.append((ad.get("tool_call_id"), cmd))
    return out


def _content_text(msg: dict) -> str:
    c = msg.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):  # some providers chunk content
        parts = []
        for p in c:
            if isinstance(p, dict):
                parts.append(p.get("text") or p.get("content") or "")
            else:
                parts.append(str(p))
        return "".join(parts)
    return ""


def strip_gt(observation: str) -> str:
    """Return the observation with GT's appended blocks removed.

    GT injects its blocks INSIDE the command's `<output>...</output>` (nudge just
    before `</output>`; evidence/contract/scope after the real output).  The
    agent's RAW observation is everything up to the first GT marker, with the
    `</output>`/closing tags that GT displaced restored is unnecessary for
    decisioning — we only need the real command text for the regexes."""
    if not observation:
        return ""
    m = _GT_FIRST_MARKER_RE.search(observation)
    if not m:
        return observation
    return observation[: m.start()]


def messages_to_turns(messages: list[dict]) -> list[Turn]:
    """Pair each action with its observation.  A mini-swe assistant turn may
    batch MULTIPLE tool_calls; each gets its own observation, matched by
    `tool_call_id` when present, else by position among the consecutive
    `tool`/`user` messages that follow.  One Turn is emitted per action so a GT
    block appended to ANY observation (incl. a 2nd batched command's output) is
    captured — the earlier loader dropped these (the abs scaffold_trap nudge sat
    in the 2nd tool message of a 2-call turn)."""
    # index tool messages by tool_call_id for O(1) linkage.
    by_id: dict[str, dict] = {}
    for m in messages:
        if m.get("role") in ("tool", "user"):
            tcid = m.get("tool_call_id")
            if tcid:
                by_id[tcid] = m

    turns: list[Turn] = []
    n = len(messages)
    for i, msg in enumerate(messages):
        if msg.get("role") != "assistant":
            continue
        actions = _actions_from_assistant(msg)
        if not actions:
            continue
        # the consecutive observation messages following this assistant turn.
        following: list[dict] = []
        j = i + 1
        while j < n and messages[j].get("role") in ("tool", "user"):
            following.append(messages[j])
            j += 1
        for pos, (tcid, cmd) in enumerate(actions):
            obs_msg = None
            if tcid and tcid in by_id:
                obs_msg = by_id[tcid]
            elif pos < len(following):
                obs_msg = following[pos]
            full_obs = _content_text(obs_msg) if obs_msg else ""
            turns.append(Turn(index=i, command=cmd,
                              raw_obs=strip_gt(full_obs), full_obs=full_obs))
    return turns


def load_trajectory(path: str) -> list[Turn]:
    """Load a mini-swe `*.trajectory.json` (or a bare messages list) -> Turns."""
    with open(path, encoding="utf-8", errors="replace") as f:
        data = json.load(f)
    if isinstance(data, dict):
        messages = data.get("messages") or data.get("history") or []
    else:
        messages = data
    return messages_to_turns(messages)


# ---------------------------------------------------------------------------
# Derived state — the stateless recompute (plan §1.2 table).
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class EditEvent:
    turn_index: int
    file: str
    is_source: bool   # True iff the file ends in _SRC_EXT (counts as a source edit)


@dataclass(frozen=True)
class TestRun:
    turn_index: int
    command: str
    has_result: bool   # observed pass OR fail marker
    has_pass: bool
    has_fail: bool
    is_env_fail: bool
    is_compile_fail: bool
    is_blind: bool     # real runner, no result, no env/compile error


@dataclass
class DerivedState:
    """All oracle state, recomputed from the trajectory prefix.  No memory
    between turns — every field is a function of the turns fed to `sense`."""
    n_turns: int = 0
    action_count: int = 0
    edit_events: list[EditEvent] = field(default_factory=list)
    edited_source_files: set[str] = field(default_factory=set)
    edited_nonsource_files: set[str] = field(default_factory=set)
    source_edit_count: int = 0
    test_runs: list[TestRun] = field(default_factory=list)
    blind_test_runs: int = 0
    test_evidence_seen: bool = False
    tested_tokens: set[str] = field(default_factory=set)   # tokens in observed test output
    edited_tokens: set[str] = field(default_factory=set)   # tokens in edit-event commands
    #   (the edit EVIDENCE surface: an edit command carries the code it writes —
    #    sed pattern, heredoc body, editor payload — so obligation symbols that
    #    intersect it were demonstrably touched; plan §5.2 "edited?")
    delivered_markers: list[tuple[int, str, str]] = field(default_factory=list)  # (turn, tag, reason)
    delivered_nudge_reasons: set[str] = field(default_factory=set)
    phase: str = "ORIENT"
    # ── delivery-engine Stage-1 behavioral signals (2026-06-11) ──────────────
    # TIDE state graph (arXiv 2602.02196): per-action node identities + full
    # no-new-state signatures, and the two derived structure metrics.
    state_keys: list[str] = field(default_factory=list)
    loop_signatures: list[str] = field(default_factory=list)
    loop_ratio: float = 0.0
    new_state_rate: float = 1.0
    # TRAJEVAL Coherence Collapse (arXiv 2603.24631): per-source-file re-edit
    # count with NO observed passing test between (reset on every observed
    # pass) — the `Pr` re-patch symbol of arXiv 2604.02547.
    edit_churn: dict[str, int] = field(default_factory=dict)
    # per-file edit-evidence tokens — the test_coverage_ratio denominator.
    edited_tokens_by_file: dict[str, set[str]] = field(default_factory=dict)

    # convenience for replay/telemetry
    def edited_files(self) -> set[str]:
        return set(self.edited_source_files) | set(self.edited_nonsource_files)


def _nonsource_write_target(cmd: str) -> str | None:
    """A WRITE to a non-`_SRC_EXT` file (template/config/doc) the live source
    classifier does not flag.  Mirrors gt_mini_patch._edit_target's write shapes
    (redirect, cat>, heredoc target, python/node open-write) but for _NONSRC_EXT.
    Correct-or-quiet: only literal, glob-free, var-free targets."""
    if not cmd:
        return None
    nohd = cmd.split("<<", 1)[0] if "<<" in cmd else cmd
    # redirect / cat > FILE (also the `cat > FILE << HEREDOC` form: target is
    # before the `<<`, so it survives the heredoc strip)
    for mm in re.finditer(r">>?\s*([^\s'\"<>|&;]+)", nohd):
        t = mm.group(1).strip("\"'`()")
        if t.endswith(_NONSRC_EXT) and "*" not in t and "$" not in t:
            return t
    # python/node open-write to a non-source file (scan full cmd incl. heredoc)
    for mm in re.finditer(r"""open\(\s*['"]([^'"]+)['"]\s*,\s*['"][wa]""", cmd):
        t = mm.group(1)
        if t.endswith(_NONSRC_EXT) and "*" not in t:
            return t
    for mm in re.finditer(
        r"""(?:writeFileSync|appendFileSync|writeFile)\(\s*['"]([^'"]+)['"]""", cmd
    ):
        t = mm.group(1)
        if t.endswith(_NONSRC_EXT) and "*" not in t:
            return t
    return None


_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _classify_test_run(cmd: str, raw_obs: str) -> TestRun | None:
    """Classify a command+observation as a test run, reusing the live regexes.
    Returns None when the command is not a real test-runner invocation."""
    if not _TEST_RUNNER_RE.search(cmd or ""):
        return None
    text = raw_obs or ""
    has_pass = bool(_TEST_PASS_RE.search(text))
    has_fail = bool(_TEST_FAIL_RE.search(text))
    is_env = bool(_ENV_FAIL_RE.search(text))
    is_compile = bool(_COMPILE_FAIL_RE.search(text))
    has_result = has_pass or has_fail
    is_blind = (not has_result) and (not is_env) and (not is_compile)
    return TestRun(
        turn_index=-1, command=cmd, has_result=has_result,
        has_pass=has_pass, has_fail=has_fail,
        is_env_fail=is_env, is_compile_fail=is_compile, is_blind=is_blind,
    )


def _derive_phase(state: DerivedState) -> str:
    """Deterministic phase from the trajectory shape (plan §1.2):
      ORIENT     — no source edit yet
      IMPLEMENT  — >=1 source edit, but not yet in review/verify shape
      VERIFY     — a test run occurred after the first source edit
      REVIEW     — >=1 source edit followed by >=3 consecutive non-edit actions
                   (the live GT_VERIFY transition predicate)
    REVIEW takes precedence over VERIFY when both hold (review is the later,
    completeness moment)."""
    if state.source_edit_count == 0:
        return "ORIENT"
    return state.phase  # set during the streaming recompute below


def sense(turns: list[Turn]) -> DerivedState:
    """Recompute the full derived state from an ordered list of turns.

    PURE: depends only on `turns`.  Calling `sense(turns[:k])` for increasing k
    gives the per-turn state stream the oracle consumes — no hidden memory.
    """
    st = DerivedState()
    st.n_turns = len(turns)
    # phase streaming: count consecutive non-source-edit actions after the first
    # source edit to detect the REVIEW transition (>=3) — the live F4 predicate.
    seen_source_edit = False
    nonedit_streak = 0
    phase = "ORIENT"
    for ti, turn in enumerate(turns):
        cmd = turn.command
        raw = turn.raw_obs
        st.action_count += 1

        # ── Stage-1 behavioral state graph (TIDE): one node key per action,
        # one full no-new-state signature per non-empty command (matching the
        # live loop window's append discipline). ──────────────────────────────
        st.state_keys.append(_behavior_state_key(cmd, raw))
        if (cmd or "").strip():
            st.loop_signatures.append(
                (cmd or "").strip() + "\x00" + _obs_collapse(raw))

        kind, fpath = _classify(cmd)
        is_source_edit = (kind == "post_edit" and bool(fpath))
        if is_source_edit:
            st.edit_events.append(EditEvent(ti, fpath, True))
            st.edited_source_files.add(fpath)
            st.source_edit_count += 1
            seen_source_edit = True
            nonedit_streak = 0
            phase = "IMPLEMENT"
            # TRAJEVAL coherence: one more re-edit of this target with no
            # passing test observed since the last reset.
            st.edit_churn[fpath] = st.edit_churn.get(fpath, 0) + 1
            # edit EVIDENCE tokens: the command text carries what was written
            # (sed/heredoc/editor payload) — plan §5.2 "edited?" intersection set.
            ftoks = st.edited_tokens_by_file.setdefault(fpath, set())
            for w in _WORD_RE.findall(cmd or ""):
                if len(w) >= 3:
                    st.edited_tokens.add(w)
                    ftoks.add(w)
        else:
            # non-source write (template/config) — recorded but not a source edit
            ns = _nonsource_write_target(cmd)
            if ns:
                st.edit_events.append(EditEvent(ti, ns, False))
                st.edited_nonsource_files.add(ns)
                for w in _WORD_RE.findall(cmd or ""):
                    if len(w) >= 3:
                        st.edited_tokens.add(w)
            if seen_source_edit:
                nonedit_streak += 1

        # test runs (reuse live regexes; decisions on RAW obs, GT-stripped)
        tr = _classify_test_run(cmd, raw)
        if tr is not None:
            tr = TestRun(
                turn_index=ti, command=tr.command, has_result=tr.has_result,
                has_pass=tr.has_pass, has_fail=tr.has_fail,
                is_env_fail=tr.is_env_fail, is_compile_fail=tr.is_compile_fail,
                is_blind=tr.is_blind,
            )
            st.test_runs.append(tr)
            if tr.has_result:
                st.test_evidence_seen = True
                phase = "VERIFY" if phase != "REVIEW" else "REVIEW"
                # TRAJEVAL coherence reset: an observed PASSING result clears
                # the per-target churn (re-edits after this are a new cycle).
                if tr.has_pass:
                    st.edit_churn = {}
                # tokens in the observed test output AND the test command
                # itself (plan §5.2 "tested?" = obligation symbols ∩ observed
                # test command/output text) -> tested_tokens
                for w in _WORD_RE.findall(raw):
                    if len(w) >= 3:
                        st.tested_tokens.add(w)
                for w in _WORD_RE.findall(cmd or ""):
                    if len(w) >= 3:
                        st.tested_tokens.add(w)
            elif tr.is_blind:
                st.blind_test_runs += 1

        # REVIEW transition: source edit followed by >=3 non-edit actions
        if seen_source_edit and nonedit_streak >= 3:
            phase = "REVIEW"

        # GT delivered-set: scan the FULL observation for GT's own markers
        for mm in _GT_MARKER_RE.finditer(turn.full_obs or ""):
            tag = mm.group(1)
            reason = mm.group(2) or ""
            st.delivered_markers.append((ti, tag, reason))
            if tag == "gt-nudge" and reason:
                st.delivered_nudge_reasons.add(reason)

    st.phase = phase if seen_source_edit else "ORIENT"
    # ── Stage-1 derived structure metrics (pure functions of the streams) ────
    st.loop_ratio = compute_loop_ratio(st.loop_signatures)
    st.new_state_rate = compute_new_state_rate(st.state_keys)
    return st


def stream_states(turns: list[Turn]) -> list[DerivedState]:
    """The per-turn state stream: states[k] = sense(turns[:k+1]).  This is how the
    oracle (Stage 2) walks the trajectory — recomputing, never accumulating."""
    return [sense(turns[: k + 1]) for k in range(len(turns))]


__all__ = [
    "Turn", "EditEvent", "TestRun", "DerivedState",
    "load_trajectory", "messages_to_turns", "sense", "stream_states", "strip_gt",
    # Stage-1 behavioral signals (shared formulas — bound from gt_mini_patch)
    "compute_loop_ratio", "compute_new_state_rate", "symbol_tested",
    "edit_coverage_ratio", "test_coverage_ratio",
]
