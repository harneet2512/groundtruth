"""Deterministic runtime helpers with side-effect-free package import.

Public names remain backward compatible, but their implementation modules are
loaded only when the name is requested.  Importing a narrow runtime submodule
must not register unrelated proof, embedding, memory, or repository-analysis
surfaces in Mini-SWE's process.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any


_EXPORTS = {
    "GTRuntimeContext": ("groundtruth.runtime.context", "GTRuntimeContext"),
    "GTProofModeError": ("groundtruth.runtime.context", "GTProofModeError"),
    "audit_patch": ("groundtruth.runtime.patch_auditor", "audit_patch"),
    "build_benchmark_report": ("groundtruth.runtime.report", "build_benchmark_report"),
    "build_project_memory": ("groundtruth.runtime.project_memory", "build_project_memory"),
    "decide_control_action": ("groundtruth.runtime.control_policy", "decide_control_action"),
    "detect_repo_profile": ("groundtruth.runtime.repo_adapters", "detect_repo_profile"),
    "evaluate_replan_triggers": ("groundtruth.runtime.replan", "evaluate_replan_triggers"),
    "format_intervention": ("groundtruth.runtime.control_policy", "format_intervention"),
    "select_test_command": ("groundtruth.runtime.test_runner", "select_test_command"),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    try:
        module_name, attribute = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
    value = getattr(import_module(module_name), attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted([*globals(), *__all__])
