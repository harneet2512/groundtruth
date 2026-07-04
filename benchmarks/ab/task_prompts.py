"""Convert hallucination benchmark cases into coding task prompts for the LLM agent.

Each case becomes an AgentTask: a natural-language task description + file path + language
that an LLM would attempt. Evaluation can check whether the generated code uses
correct_symbol / correct_import (from the case's expected field).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class AgentTask:
    """A single coding task derived from a hallucination case, for agent A/B runs."""

    case_id: str
    category: str
    subcategory: str | None
    language: str
    task_description: str
    file_path: str
    correct_symbol: str | None
    correct_import: str | None
    error_type: str | None

    def to_agent_input(self) -> dict[str, Any]:
        """For use with CodeGenAgent: task, file_path, language."""
        return {
            "task_description": self.task_description,
            "file_path": self.file_path,
            "language": self.language,
        }


def _case_to_task(case_data: dict[str, Any]) -> AgentTask | None:
    """Convert one JSON case to AgentTask. Returns None if missing required fields."""
    case_id = case_data.get("id", "")
    category = case_data.get("category", "")
    subcategory = case_data.get("subcategory")
    language = case_data.get("language", "python")
    inp = case_data.get("input", {})
    exp = case_data.get("expected", {})

    file_path = inp.get("filePath") or inp.get("file_path")
    intent = inp.get("intent")
    description = case_data.get("description", "")

    correct_symbol = exp.get("correctSymbol") or exp.get("correct_symbol")
    correct_import = exp.get("correctImport") or exp.get("correct_import")
    error_type = exp.get("errorType") or exp.get("error_type")

    if not file_path:
        return None
    # Prefer intent as the task; fall back to description or a generic prompt
    if intent:
        task_description = intent
    elif description:
        task_description = description
    else:
        task_description = f"Implement the required behavior in {file_path}"

    return AgentTask(
        case_id=case_id,
        category=category,
        subcategory=subcategory,
        language=language,
        task_description=task_description,
        file_path=file_path,
        correct_symbol=correct_symbol,
        correct_import=correct_import,
        error_type=error_type,
    )


def load_agent_tasks(hallucination_cases_dir: str | Path) -> list[AgentTask]:
    """
    Load all hallucination cases from a directory and convert to AgentTasks.

    Recursively finds all .json files. Skips cases that have no file_path.
    """
    tasks: list[AgentTask] = []
    root = Path(hallucination_cases_dir)
    if not root.is_dir():
        return tasks

    for path in sorted(root.rglob("*.json")):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        task = _case_to_task(data)
        if task is not None:
            tasks.append(task)

    return tasks


def load_agent_tasks_from_runner_cases(cases: list[Any]) -> list[AgentTask]:
    """
    Convert BenchmarkCase instances (from runner.load_cases) to AgentTasks.

    Use when you already have loaded cases from benchmarks.runner.
    """
    tasks: list[AgentTask] = []
    for bc in cases:
        task_desc = bc.intent or bc.description or f"Implement required behavior in {bc.file_path}"
        tasks.append(AgentTask(
            case_id=bc.id,
            category=bc.category,
            subcategory=bc.subcategory,
            language=bc.language,
            task_description=task_desc,
            file_path=bc.file_path,
            correct_symbol=bc.correct_symbol,
            correct_import=bc.correct_import,
            error_type=bc.error_type,
        ))
    return tasks
