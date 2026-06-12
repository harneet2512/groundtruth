"""Product-owned mini-swe trajectory state controller."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .context_policy import Event, Phase


SOURCE_EXTS = {
    ".py", ".go", ".rs", ".js", ".ts", ".tsx", ".jsx", ".java", ".rb",
    ".c", ".cpp", ".h", ".hpp", ".php", ".kt", ".scala", ".cs",
}


@dataclass(frozen=True)
class Turn:
    command: str = ""
    observation: str = ""
    full_observation: str = ""


@dataclass
class TrajectoryState:
    action_count: int = 0
    step_limit: int | None = None
    viewed_files: set[str] = field(default_factory=set)
    edited_files: set[str] = field(default_factory=set)
    source_edit_count: int = 0
    nonedit_streak: int = 0
    test_count: int = 0
    blind_test_count: int = 0
    test_evidence_seen: bool = False
    last_test_failed: bool = False
    edited_tokens: set[str] = field(default_factory=set)
    tested_tokens: set[str] = field(default_factory=set)
    delivered_markers: list[tuple[int, str, str]] = field(default_factory=list)

    @property
    def budget_fraction(self) -> float:
        if not self.step_limit:
            return 0.0
        return self.action_count / self.step_limit


_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_FILE_RE = re.compile(r"([\w./-]+\.(?:py|go|rs|js|ts|tsx|jsx|java|rb|c|cpp|h|hpp|php|kt|scala|cs))")
_GT_MARKER_RE = re.compile(r"<(gt-[a-z-]+)(?:\s+reason=['\"]([^'\"]+)['\"])?", re.I)
_TEST_RUNNER_RE = re.compile(
    r"(?:python[\d.]*\s+-m\s+(?:pytest|unittest|nose2?|tox)\b"
    r"|pytest\b|py\.test\b|tox\b|nose2?\b"
    r"|go\s+test\b|cargo\s+test\b"
    r"|npm\s+(?:run\s+)?test\b|yarn\s+(?:run\s+)?test\b|pnpm\s+(?:run\s+)?test\b"
    r"|jest\b|mocha\b|vitest\b|rspec\b|mvn\b.*\btest\b|gradlew?\b.*\btest\b)",
    re.I,
)
_TEST_PASS_RE = re.compile(r"(test result: ok\b|\b\d+ passed\b|\b\d+ passing\b|^OK\b|^PASS\b)", re.I | re.M)
_TEST_FAIL_RE = re.compile(r"(\bFAILED\b|\bAssertionError\b|\b[1-9]\d* failed\b|--- FAIL:|test result: FAILED)", re.I)
_ENV_FAIL_RE = re.compile(r"(ModuleNotFoundError|No module named|ImportError|command not found|No matching distribution)", re.I)


def is_source_path(path: str) -> bool:
    return any(path.endswith(ext) for ext in SOURCE_EXTS)


def extract_files(text: str) -> set[str]:
    return {m.group(1).replace("\\", "/") for m in _FILE_RE.finditer(text or "")}


def command_event(command: str, observation: str = "") -> Event | None:
    files = extract_files(command)
    if _TEST_RUNNER_RE.search(command or ""):
        return Event.TEST_RESULT
    if files:
        if re.search(r"(cat|sed|grep|rg|less|head|tail|Get-Content|Select-String)", command or "", re.I):
            return Event.POST_VIEW
        if re.search(r"(apply_patch|python[\d.]*\s+-c|cat\s*>|>>?|writeFile|open\()", command or "", re.I):
            return Event.POST_EDIT
    return None


def update_state(state: TrajectoryState, turn: Turn) -> TrajectoryState:
    state.action_count += 1
    cmd = turn.command or ""
    obs = turn.observation or ""
    files = extract_files(cmd)
    event = command_event(cmd, obs)

    if event == Event.POST_VIEW:
        state.viewed_files.update(files)
    source_edited = False
    if event == Event.POST_EDIT:
        for f in files:
            state.edited_files.add(f)
            if is_source_path(f):
                source_edited = True
        if source_edited:
            state.source_edit_count += 1
            state.nonedit_streak = 0
            for w in _WORD_RE.findall(cmd):
                if len(w) >= 3:
                    state.edited_tokens.add(w)
    else:
        if state.source_edit_count:
            state.nonedit_streak += 1

    if event == Event.TEST_RESULT:
        state.test_count += 1
        has_pass = bool(_TEST_PASS_RE.search(obs))
        has_fail = bool(_TEST_FAIL_RE.search(obs))
        state.last_test_failed = has_fail and not has_pass
        if has_pass or has_fail:
            state.test_evidence_seen = True
            for w in _WORD_RE.findall(cmd + "\n" + obs):
                if len(w) >= 3:
                    state.tested_tokens.add(w)
        elif not _ENV_FAIL_RE.search(obs):
            state.blind_test_count += 1

    full = turn.full_observation or obs
    for m in _GT_MARKER_RE.finditer(full):
        state.delivered_markers.append((state.action_count, m.group(1), m.group(2) or ""))
    return state


def derive_state(turns: list[Turn], *, step_limit: int | None = None) -> TrajectoryState:
    state = TrajectoryState(step_limit=step_limit)
    for turn in turns:
        update_state(state, turn)
    return state


def derive_phase(state: TrajectoryState) -> Phase:
    if state.action_count <= 5 and not state.edited_files:
        return Phase.ORIENT
    if not state.edited_files:
        return Phase.VIEW
    if state.step_limit and state.budget_fraction > 0.90:
        return Phase.SUBMIT
    if state.nonedit_streak >= 3 or state.test_count:
        return Phase.VERIFY
    return Phase.EDIT


def detect_events(prev: TrajectoryState, cur: TrajectoryState) -> set[Event]:
    events: set[Event] = set()
    if prev.action_count == 0 and cur.action_count > 0:
        events.add(Event.TASK_START)
    if cur.viewed_files - prev.viewed_files:
        events.add(Event.POST_VIEW)
    if cur.edited_files - prev.edited_files or cur.source_edit_count > prev.source_edit_count:
        events.add(Event.POST_EDIT)
    if cur.test_count > prev.test_count:
        events.add(Event.TEST_RESULT)
    if prev.nonedit_streak < 3 <= cur.nonedit_streak and cur.source_edit_count:
        events.add(Event.REVIEW_TRANSITION)
    if derive_phase(cur) == Phase.SUBMIT:
        events.add(Event.PRE_SUBMIT)
    return events

