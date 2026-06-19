"""Agent state tracker — canonical view of the agent's trajectory.

Pure observer. No agent-visible output. Feeds metrics to router decisions.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class AgentTrajectoryState:
    """Tracks what the agent has done so far in this task."""
    action_count: int = 0
    max_iter: int = 100

    viewed_files: list[str] = field(default_factory=list)
    edited_files: list[str] = field(default_factory=list)
    source_edit_iterations: list[int] = field(default_factory=list)
    test_iterations: list[int] = field(default_factory=list)
    search_count_since_edit: int = 0
    scaffold_files_created: int = 0

    brief_candidates: set[str] = field(default_factory=set)
    candidates_confirmed: set[str] = field(default_factory=set)

    diff_ever_nonzero: bool = False
    diff_collapsed_count: int = 0

    @property
    def ratio(self) -> float:
        if self.max_iter <= 0:
            return 0.0
        return self.action_count / self.max_iter

    @property
    def band(self) -> str:
        r = self.ratio
        if r < 0.25:
            return "early"
        if r < 0.50:
            return "mid"
        if r < 0.75:
            return "late"
        return "final"

    @property
    def has_source_edit(self) -> bool:
        return len(self.source_edit_iterations) > 0

    @property
    def has_verification(self) -> bool:
        return len(self.test_iterations) > 0

    def record_view(self, file_path: str) -> None:
        self.viewed_files.append(file_path)

    def record_edit(self, file_path: str, is_source: bool) -> None:
        self.edited_files.append(file_path)
        if is_source:
            self.source_edit_iterations.append(self.action_count)
            self.search_count_since_edit = 0

    def record_search(self) -> None:
        self.search_count_since_edit += 1

    def record_test(self) -> None:
        self.test_iterations.append(self.action_count)

    def record_scaffold(self) -> None:
        self.scaffold_files_created += 1

    def file_already_viewed(self, file_path: str) -> bool:
        return file_path in self.viewed_files

    def to_dict(self) -> dict:
        return {
            "action_count": self.action_count,
            "max_iter": self.max_iter,
            "ratio": round(self.ratio, 3),
            "band": self.band,
            "viewed_count": len(self.viewed_files),
            "edited_count": len(self.edited_files),
            "source_edits": len(self.source_edit_iterations),
            "tests_run": len(self.test_iterations),
            "searches_since_edit": self.search_count_since_edit,
            "scaffolds": self.scaffold_files_created,
            "candidates_confirmed": len(self.candidates_confirmed),
            "diff_ever_nonzero": self.diff_ever_nonzero,
            "diff_collapsed": self.diff_collapsed_count,
            "has_source_edit": self.has_source_edit,
            "has_verification": self.has_verification,
        }
