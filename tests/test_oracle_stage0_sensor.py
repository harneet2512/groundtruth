"""Oracle Stage 0 — trajectory sensor + replay harness (red->green).

Per `ORACLE_ARCHITECTURE_PLAN.md` §1.2 + §8 Stage 0.  The sensor is a STATELESS
pure function `sense(turns) -> DerivedState` plus an offline replayer over the
canonical mini-swe trajectory format.  The detection logic that used to live only
inside `gt_mini_patch._augment_output` (with ~12 module globals, un-replayable)
becomes a side-effect-free recompute, which Stage 2's byte-parity replay requires.

These tests are RED before `gt_oracle_sense.py` exists (no standalone sense API)
and GREEN after.  They run against the REAL frozen `tenpack_27307362054`
trajectories (5 languages: go, python, typescript, rust, javascript) — generality
is proven on held-out, multi-language inputs, never asserted (LEG 1).  No task IDs
gate any assertion; the corpus is iterated by glob.

Metric moved (LEG 2 — Core Product Contract observability): sensor RECALL on edit
events.  The contract requires GT to know "did the gold reach the edit / did the
agent act on it"; without a faithful edit/test sensor that is unmeasurable.  We
assert the sensor recovers EVERY genuine source write (heredoc / `cat >` /
`sed -i` / `python -c open(...,'w')` / `writeFileSync`) the corpus contains, plus
the template-write residual the live counter misses.
"""

from __future__ import annotations

import glob
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SENSE_PATH = _ROOT / "artifact_deepswe" / "gt_oracle_sense.py"
_PATCH_PATH = _ROOT / "artifact_deepswe" / "gt_mini_patch.py"
_CORPUS = _ROOT / ".claude" / "reports" / "runs" / "tenpack_27307362054"

_TRAJ_GLOB = str(_CORPUS / "*" / "jobs" / "*" / "*" / "agent" / "mini-swe-agent.trajectory.json")

pytestmark = pytest.mark.skipif(
    not _CORPUS.is_dir(), reason="external frozen trajectory corpus is not installed"
)


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def sense_mod():
    return _load(_SENSE_PATH, "gt_oracle_sense_t")


@pytest.fixture(scope="module")
def patch_mod():
    # GT_BASELINE=1 only WHILE loading (prevents _install patching env classes;
    # the module captures the flag at import); restored so this suite never
    # poisons later suites' env (pre-existing suite-order pollution).
    import os

    prev = os.environ.get("GT_BASELINE")
    os.environ["GT_BASELINE"] = "1"
    try:
        return _load(_PATCH_PATH, "gt_mini_patch_t0")
    finally:
        if prev is None:
            os.environ.pop("GT_BASELINE", None)
        else:
            os.environ["GT_BASELINE"] = prev


def _all_trajectories() -> list[str]:
    return sorted(glob.glob(_TRAJ_GLOB))


def _task_of(path: str) -> str:
    p = path.replace("\\", "/")
    return p.split("tenpack_27307362054/")[1].split("/")[0]


def _raw_commands(messages: list[dict]) -> list[str]:
    out: list[str] = []
    for x in messages:
        if x.get("role") != "assistant":
            continue
        for tc in x.get("tool_calls") or []:
            try:
                out.append(json.loads(tc["function"]["arguments"]).get("command", ""))
            except Exception:  # noqa: BLE001
                pass
    return out


# An INDEPENDENT write-detector (deliberately different code from the sensor) so
# the recall assertion is not circular: it finds every command that writes a
# *source* file by any shape, then we require the sensor to have sensed each.
_SRC_EXT = (
    ".py",
    ".go",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".rs",
    ".java",
    ".rb",
    ".c",
    ".cc",
    ".cpp",
    ".h",
    ".hpp",
    ".cs",
    ".php",
    ".kt",
    ".scala",
    ".swift",
)


def _independent_source_writes(cmd: str) -> set[str]:
    """Source files this command writes AT THIS TURN.  Mirrors the precedence the
    live edit-target uses (redirect target wins over a heredoc body): when the
    command is `cat > /tmp/script.py << EOF ... open('src/x.py','w') ... EOF`, the
    turn writes the SCRATCH file `/tmp/script.py` — the source edit happens only
    when the script is later RUN.  So a heredoc body's open() is counted only when
    the redirect target itself is a source file (no scratch indirection)."""
    writes: set[str] = set()
    scan = cmd.split("<<", 1)[0] if "<<" in cmd else cmd
    redirect_targets: set[str] = set()
    for mm in re.finditer(r">>?\s*([^\s'\"<>|&;]+)", scan):
        redirect_targets.add(mm.group(1).strip("\"'`()"))
    # a scratch redirect target (e.g. /tmp/...) means the heredoc body is a
    # program, not a direct edit — don't peek inside it for source writes.
    scratch_redirect = any(t.startswith("/tmp/") or t.startswith("/dev/") for t in redirect_targets)
    for t in redirect_targets:
        if t.endswith(_SRC_EXT):
            writes.add(t)
    if not scratch_redirect:
        for mm in re.finditer(r"""open\(\s*['"]([^'"]+)['"]\s*,\s*['"][wa]""", cmd):
            writes.add(mm.group(1))
        for mm in re.finditer(r"""(?:writeFileSync|appendFileSync)\(\s*['"]([^'"]+)['"]""", cmd):
            writes.add(mm.group(1))
    return {w for w in writes if w.endswith(_SRC_EXT) and "*" not in w and "$" not in w}


# ---------------------------------------------------------------------------
# Loader correctness
# ---------------------------------------------------------------------------
def test_corpus_present():
    trajs = _all_trajectories()
    assert len(trajs) == 10, f"expected 10 frozen trajectories, found {len(trajs)}"


def test_loader_recovers_actions_and_observations(sense_mod):
    """The replayer must pair each action-bearing assistant message with the
    observation that follows it, across all 10 trajectories."""
    for tj in _all_trajectories():
        turns = sense_mod.load_trajectory(tj)
        assert len(turns) > 5, f"{_task_of(tj)}: too few turns ({len(turns)})"
        # at least the first action is a real bash command
        assert any(t.command for t in turns)
        # every turn with a command should have its (possibly empty) observation
        for t in turns:
            assert isinstance(t.command, str)
            assert isinstance(t.raw_obs, str)


def test_strip_gt_removes_injected_blocks(sense_mod):
    """`strip_gt` returns the agent's raw observation; GT's blocks are gone."""
    obs = "<returncode>0</returncode>\n<output>\nreal output here\n<gt-evidence>\nINJECTED\n</gt-evidence></output>"
    raw = sense_mod.strip_gt(obs)
    assert "real output here" in raw
    assert "INJECTED" not in raw
    assert "gt-evidence" not in raw


# ---------------------------------------------------------------------------
# Edit-event recall — the sensor recovers EVERY source write, every shape, lang.
# ---------------------------------------------------------------------------
def test_edit_recall_all_source_writes(sense_mod):
    """RED without a standalone sensor (no API existed to recompute edits from a
    frozen trajectory).  GREEN: recall == 1.0 of independently-detected source
    writes, across heredoc / cat> / sed -i / python-c / writeFileSync, 5 langs."""
    total = 0
    missed: list[tuple[str, int, set[str]]] = []
    for tj in _all_trajectories():
        task = _task_of(tj)
        data = json.load(open(tj, encoding="utf-8", errors="replace"))
        cmds = _raw_commands(data["messages"])
        turns = sense_mod.load_trajectory(tj)
        st = sense_mod.sense(turns)
        sensed = st.edited_source_files
        for i, c in enumerate(cmds):
            for w in _independent_source_writes(c):
                total += 1
                if w not in sensed:
                    missed.append((task, i, {w}))
    assert total >= 100, f"corpus should have >=100 source writes, got {total}"
    assert not missed, f"sensor missed {len(missed)}/{total} source writes: {missed[:6]}"


def test_heredoc_and_python_c_edits_sensed(sense_mod):
    """The specifically-named shapes (plan §1.2 row 'edit events'): heredoc
    `cat > x << EOF`, `python3 << EOF ... open('x','w')`, and `python -c
    "...open('x','w')"` must each be recovered."""
    cases = [
        ("cd /r && cat > src/new.rs << 'EOF'\nfn x(){}\nEOF", "src/new.rs"),
        ("python3 << 'PY'\nopen('pkg/mod.py','w').write('x')\nPY", "pkg/mod.py"),
        ("python -c \"open('a/b.ts','w').write('q')\"", "a/b.ts"),
        ("sed -i '5a foo' lib/core.go", "lib/core.go"),
        ("node -e \"require('fs').writeFileSync('s/app.js','x')\"", "s/app.js"),
    ]
    for cmd, expect in cases:
        turn = sense_mod.Turn(
            index=0, command=cmd, raw_obs="<returncode>0</returncode>", full_obs=""
        )
        st = sense_mod.sense([turn])
        assert expect in st.edited_source_files, f"{cmd!r} -> {st.edited_source_files}"
        assert st.source_edit_count == 1


def test_template_write_sensed_as_nonsource(sense_mod):
    """The genuine residual the live source-counter misses: `cat > x.html`.  It
    is sensed as a NON-source edit (recorded, but does not inflate the source
    edit count) — aiomonitor cmd 57."""
    cmd = "cd /r && cat > aiomonitor/webui/templates/snapshots.html << 'T'\n<div></div>\nT"
    turn = sense_mod.Turn(index=0, command=cmd, raw_obs="", full_obs="")
    st = sense_mod.sense([turn])
    assert "aiomonitor/webui/templates/snapshots.html" in st.edited_nonsource_files
    assert st.source_edit_count == 0  # not a source edit
    assert "aiomonitor/webui/templates/snapshots.html" in st.edited_files()


# ---------------------------------------------------------------------------
# Test-evidence sensing — reuses the live regexes; decisions on GT-stripped obs.
# ---------------------------------------------------------------------------
def test_test_evidence_and_blind_runs(sense_mod):
    """A real runner with a pass/fail result latches test_evidence_seen; a real
    runner with no result and no env/compile error counts as a blind run."""
    turns = [
        sense_mod.Turn(
            0, "cargo test", "<output>\n    Blocking waiting for file lock\n</output>", ""
        ),
        sense_mod.Turn(1, "cargo test", "<output>\nBuilding...\n</output>", ""),
    ]
    st = sense_mod.sense(turns)
    assert st.blind_test_runs == 2
    assert not st.test_evidence_seen

    turns2 = [sense_mod.Turn(0, "pytest tests/", "<output>\n5 passed in 0.1s\n</output>", "")]
    st2 = sense_mod.sense(turns2)
    assert st2.test_evidence_seen
    assert st2.blind_test_runs == 0


def test_boa_blind_runs_recovered_from_real_trajectory(sense_mod):
    """boa ran six `timeout N cargo test` SIGKILLed mid-build.  The decision-point
    state that makes no_test_evidence fire (Stage 2) is: at the turn where the 2nd
    blind run lands with >=1 source edit, test_evidence_seen is still False (boa
    only first observed a result two turns LATER).  We assert that PREFIX state, the
    statelessly-recomputed condition the governor reads — not a whole-trajectory
    claim (evidence does appear by the end, which is exactly why the governor must
    fire at the decision point, before it)."""
    tj = glob.glob(
        str(_CORPUS / "boa-*" / "jobs" / "*" / "*" / "agent" / "mini-swe-agent.trajectory.json")
    )[0]
    turns = sense_mod.load_trajectory(tj)
    fire_state = None
    for k in range(len(turns)):
        s = sense_mod.sense(turns[: k + 1])
        if s.blind_test_runs >= 2 and s.source_edit_count >= 1 and not s.test_evidence_seen:
            fire_state = s
            break
    assert fire_state is not None, "no_test_evidence decision point never reached"
    assert fire_state.blind_test_runs >= 2
    assert not fire_state.test_evidence_seen  # blind at the decision point
    assert fire_state.source_edit_count >= 1


# ---------------------------------------------------------------------------
# Delivered-set (GT-marker scan) — the stateless dedup anchor.
# ---------------------------------------------------------------------------
def test_delivered_set_recovered_from_gt_markers(sense_mod):
    """GT's own emissions appear in the trajectory; the sensor recovers them as
    the delivered-set with zero side-channel ledger (plan §1.2 row 1)."""
    found_any = False
    for tj in _all_trajectories():
        turns = sense_mod.load_trajectory(tj)
        st = sense_mod.sense(turns)
        if st.delivered_markers:
            found_any = True
            tags = {t for _, t, _ in st.delivered_markers}
            assert tags <= {"gt-evidence", "gt-scope", "gt-contract", "gt-nudge"}, tags
    assert found_any, "no GT markers recovered from any trajectory"


def test_nudge_reasons_recovered_match_recorded(sense_mod):
    """The recorded <gt-nudge reason=...> fires are recovered exactly — this is
    the Stage-2 byte-parity ground truth, sensed statelessly."""
    expected = {
        "abs-module-cache-flags": {"scaffold_trap", "failure_persisted"},
        "boa-hierarchical-evaluation-cancellation": {"scaffold_trap", "no_test_evidence"},
        "csstree-shorthand-expansion-compression": {"scaffold_trap"},
        "aiomonitor-task-snapshots-diff": set(),
    }
    for tj in _all_trajectories():
        task = _task_of(tj)
        if task not in expected:
            continue
        turns = sense_mod.load_trajectory(tj)
        st = sense_mod.sense(turns)
        assert st.delivered_nudge_reasons == expected[task], (
            f"{task}: sensed {st.delivered_nudge_reasons}, expected {expected[task]}"
        )


# ---------------------------------------------------------------------------
# Statelessness — sense(turns[:k]) is a pure prefix recompute, no memory.
# ---------------------------------------------------------------------------
def test_sense_is_stateless_prefix_pure(sense_mod):
    """sense(turns[:k]) twice gives identical state; and the streamed states are
    monotone in action_count (recompute, never accumulate side effects)."""
    tj = _all_trajectories()[0]
    turns = sense_mod.load_trajectory(tj)
    k = min(20, len(turns))
    a = sense_mod.sense(turns[:k])
    b = sense_mod.sense(turns[:k])
    assert a.action_count == b.action_count == k
    assert a.edited_source_files == b.edited_source_files
    assert a.blind_test_runs == b.blind_test_runs
    stream = sense_mod.stream_states(turns[:k])
    counts = [s.action_count for s in stream]
    assert counts == list(range(1, k + 1))


def test_phase_progression(sense_mod):
    """Phase advances ORIENT -> IMPLEMENT (no benchmark/task logic, pure shape)."""
    orient = sense_mod.sense([sense_mod.Turn(0, "ls", "<output>files</output>", "")])
    assert orient.phase == "ORIENT"
    impl = sense_mod.sense(
        [
            sense_mod.Turn(0, "ls", "<output>files</output>", ""),
            sense_mod.Turn(1, "sed -i '1a x' src/a.py", "<output></output>", ""),
        ]
    )
    assert impl.phase in ("IMPLEMENT", "VERIFY", "REVIEW")
    assert impl.source_edit_count == 1
