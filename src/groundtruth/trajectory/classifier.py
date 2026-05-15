"""Observation classifier — detects test runs, failures, and command kinds."""

from __future__ import annotations

import enum
import os
import re
from dataclasses import dataclass


class VerificationTarget(str, enum.Enum):
    TARGETED_TO_EDITED_SYMBOL = "targeted_to_edited_symbol"
    TARGETED_TO_EDITED_FILE = "targeted_to_edited_file"
    TARGETED_TO_RELATED_TEST = "targeted_to_related_test"
    BROAD_PROJECT_VERIFICATION = "broad_project_verification"
    IRRELEVANT_VERIFICATION = "irrelevant_verification"
    UNKNOWN = "unknown"

    def is_targeted(self) -> bool:
        return self in (
            VerificationTarget.TARGETED_TO_EDITED_SYMBOL,
            VerificationTarget.TARGETED_TO_EDITED_FILE,
            VerificationTarget.TARGETED_TO_RELATED_TEST,
        )


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


_BROAD_PATTERNS = [
    re.compile(r"^pytest\s*$"),
    re.compile(r"^python\s+-m\s+pytest\s*$"),
    re.compile(r"pytest\s+(?:tests?/?|test/?)\s*$"),
    re.compile(r"pytest\s+\.\s*$"),
    re.compile(r"^npm\s+test\s*$"),
    re.compile(r"^yarn\s+test\s*$"),
    re.compile(r"^pnpm\s+test\s*$"),
    re.compile(r"^go\s+test\s+\./\.\.\.\s*$"),
    re.compile(r"^cargo\s+test\s*$"),
    re.compile(r"^mvn\s+test\s*$"),
    re.compile(r"^gradle\s+test\s*$"),
    re.compile(r"^tox\s*$"),
    re.compile(r"^nox\s*$"),
    re.compile(r"^rspec\s*$"),
    re.compile(r"^make\s+test\s*$"),
]


def _strip_cd_prefix(command: str) -> str:
    """Remove 'cd ... &&' prefix to get the actual test command."""
    stripped = command.strip().rstrip(";").strip()
    if "&&" in stripped:
        parts = stripped.split("&&")
        stripped = parts[-1].strip()
    return stripped


def classify_verification_targeting(
    command: str,
    edited_files: list[str],
    *,
    related_test_files: list[str] | None = None,
) -> VerificationTarget:
    """Classify how targeted a verification command is to edited files.

    Returns a VerificationTarget indicating specificity level. Only
    TARGETED_TO_EDITED_SYMBOL, TARGETED_TO_EDITED_FILE, and
    TARGETED_TO_RELATED_TEST mark a patch as verified. Broad passing
    tests never verify a patch.
    """
    if not is_verification_command(command):
        return VerificationTarget.UNKNOWN

    cmd_stripped = _strip_cd_prefix(command)

    for bp in _BROAD_PATTERNS:
        if bp.search(cmd_stripped):
            return VerificationTarget.BROAD_PROJECT_VERIFICATION

    cmd_lower = cmd_stripped.lower()

    for ef in edited_files:
        stem = os.path.splitext(os.path.basename(ef))[0].lower()
        module = stem.replace("test_", "").replace("_test", "")
        if re.search(r"\s-k\s+['\"]?" + re.escape(module), cmd_lower):
            return VerificationTarget.TARGETED_TO_EDITED_SYMBOL
        test_stem = f"test_{module}"
        if test_stem in cmd_lower or f"{module}_test" in cmd_lower:
            return VerificationTarget.TARGETED_TO_EDITED_FILE
        if module and len(module) > 2 and module in cmd_lower:
            return VerificationTarget.TARGETED_TO_EDITED_FILE

    if related_test_files:
        for rtf in related_test_files:
            rtf_base = os.path.basename(rtf).lower()
            rtf_stem = os.path.splitext(rtf_base)[0].lower()
            if rtf_base in cmd_lower or rtf_stem in cmd_lower:
                return VerificationTarget.TARGETED_TO_RELATED_TEST

    if re.search(r"test\w*\.(?:py|js|ts|go|rs)\b", cmd_stripped):
        return VerificationTarget.IRRELEVANT_VERIFICATION

    if re.search(r"\s-k\s", cmd_stripped):
        return VerificationTarget.IRRELEVANT_VERIFICATION

    return VerificationTarget.BROAD_PROJECT_VERIFICATION
