"""Observation classifier — detects test runs, failures, and command kinds."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


class CommandKind:
    TEST = "test"
    TYPECHECK = "typecheck"
    LINT = "lint"
    BUILD = "build"
    INSTALL = "install"
    RUN = "run"
    UNKNOWN = "unknown"


class FailureKind:
    ASSERTION = "assertion"
    EXCEPTION = "exception"
    COMPILE_ERROR = "compile_error"
    TYPE_ERROR = "type_error"
    LINT_ERROR = "lint_error"
    DEPENDENCY_ERROR = "dependency_error"
    ENV_ERROR = "env_error"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"


_TEST_PATTERNS = [
    re.compile(r"\bpytest\b"),
    re.compile(r"python\s+-m\s+pytest\b"),
    re.compile(r"python\s+-m\s+unittest\b"),
    re.compile(r"\bnpm\s+test\b"),
    re.compile(r"\bpnpm\s+test\b"),
    re.compile(r"\byarn\s+test\b"),
    re.compile(r"\bjest\b"),
    re.compile(r"\bvitest\b"),
    re.compile(r"\bgo\s+test\b"),
    re.compile(r"\bcargo\s+test\b"),
    re.compile(r"\bmvn\s+test\b"),
    re.compile(r"\bgradle\s+test\b"),
    re.compile(r"\brspec\b"),
    re.compile(r"\btox\b"),
    re.compile(r"\bnox\b"),
]

_TYPECHECK_PATTERNS = [
    re.compile(r"\btsc\b(?!\s+--build)"),
    re.compile(r"\bmypy\b"),
    re.compile(r"\bpyright\b"),
    re.compile(r"\bgo\s+vet\b"),
    re.compile(r"\bcargo\s+check\b"),
]

_LINT_PATTERNS = [
    re.compile(r"\beslint\b"),
    re.compile(r"\bruff\b(?:\s+check)?"),
    re.compile(r"\bflake8\b"),
    re.compile(r"\bpylint\b"),
    re.compile(r"\bgolangci-lint\b"),
    re.compile(r"\bcargo\s+clippy\b"),
]

_BUILD_PATTERNS = [
    re.compile(r"\bnpm\s+(?:run\s+)?build\b"),
    re.compile(r"\bpnpm\s+(?:run\s+)?build\b"),
    re.compile(r"\byarn\s+(?:run\s+)?build\b"),
    re.compile(r"\bmake\b"),
    re.compile(r"\bdocker\s+build\b"),
    re.compile(r"\bcargo\s+build\b"),
    re.compile(r"\bgradle\s+build\b"),
    re.compile(r"\bmvn\s+(?:package|compile)\b"),
]

_INSTALL_PATTERNS = [
    re.compile(r"\bpip\s+install\b"),
    re.compile(r"\bnpm\s+install\b"),
    re.compile(r"\bconda\s+install\b"),
    re.compile(r"\bapt\s+(?:install|get)\b"),
]

_ENV_FAILURE_PATTERNS = [
    re.compile(r"ModuleNotFoundError.*pip install", re.IGNORECASE),
    re.compile(r"command not found", re.IGNORECASE),
    re.compile(r"No such file or directory", re.IGNORECASE),
    re.compile(r"ConnectionError|ConnectionRefused|ConnectionReset", re.IGNORECASE),
    re.compile(r"PermissionError|Permission denied", re.IGNORECASE),
    re.compile(r"Could not resolve host", re.IGNORECASE),
]


def classify_command(command: str) -> str:
    for p in _INSTALL_PATTERNS:
        if p.search(command):
            return CommandKind.INSTALL
    for p in _TEST_PATTERNS:
        if p.search(command):
            return CommandKind.TEST
    for p in _TYPECHECK_PATTERNS:
        if p.search(command):
            return CommandKind.TYPECHECK
    for p in _LINT_PATTERNS:
        if p.search(command):
            return CommandKind.LINT
    for p in _BUILD_PATTERNS:
        if p.search(command):
            return CommandKind.BUILD
    return CommandKind.UNKNOWN


def is_verification_command(command: str) -> bool:
    kind = classify_command(command)
    return kind in (CommandKind.TEST, CommandKind.TYPECHECK, CommandKind.LINT)


def is_env_failure(observation_text: str) -> bool:
    for p in _ENV_FAILURE_PATTERNS:
        if p.search(observation_text[:2000]):
            return True
    return False


def extract_exit_code(observation_text: str) -> int | None:
    m = re.search(r"exit\s+code[:\s]+(\d+)", observation_text[-500:], re.IGNORECASE)
    if m:
        return int(m.group(1))
    m = re.search(r"__EXIT__(\d+)", observation_text[-200:])
    if m:
        return int(m.group(1))
    return None


@dataclass
class ObservationClassification:
    command_kind: str = CommandKind.UNKNOWN
    is_verification: bool = False
    is_failure: bool = False
    is_env_failure: bool = False
    exit_code: int | None = None
    observation_capped: str = ""


def classify_observation(
    command: str,
    observation_text: str,
) -> ObservationClassification:
    cmd_kind = classify_command(command)
    is_verif = is_verification_command(command)
    exit_code = extract_exit_code(observation_text)
    env_fail = is_env_failure(observation_text)
    is_fail = (exit_code is not None and exit_code != 0) and not env_fail

    return ObservationClassification(
        command_kind=cmd_kind,
        is_verification=is_verif,
        is_failure=is_fail,
        is_env_failure=env_fail,
        exit_code=exit_code,
        observation_capped=observation_text[:3000],
    )
