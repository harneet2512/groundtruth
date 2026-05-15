from __future__ import annotations
from dataclasses import dataclass
from typing import Optional
import os
import re

@dataclass
class TestTarget:
    file: Optional[str] = None
    class_name: Optional[str] = None
    function: Optional[str] = None
    is_broad: bool = False

_BROAD_COMMANDS = [
    re.compile(r'^pytest\s*$'),
    re.compile(r'^python\s+-m\s+pytest\s*$'),
    re.compile(r'^pytest\s+(?:tests?/?|test/?)\s*$'),
    re.compile(r'^npm\s+test\s*$'),
    re.compile(r'^yarn\s+test\s*$'),
    re.compile(r'^go\s+test\s+\./\.\.\.\s*$'),
    re.compile(r'^cargo\s+test\s*$'),
    re.compile(r'^make\s+test\s*$'),
    re.compile(r'^tox\s*$'),
    re.compile(r'^nox\s*$'),
]

def parse_test_command(command: str) -> Optional[TestTarget]:
    """Parse test command into target components."""
    cmd = command.strip()
    if '&&' in cmd:
        cmd = cmd.split('&&')[-1].strip()

    for bp in _BROAD_COMMANDS:
        if bp.search(cmd):
            return TestTarget(is_broad=True)

    # pytest path::Class::method
    m = re.search(r'pytest\s+(\S+\.py)(?:::(\w+))?(?:::(\w+))?', cmd)
    if not m:
        m = re.search(r'python\s+-m\s+pytest\s+(\S+\.py)(?:::(\w+))?(?:::(\w+))?', cmd)
    if m:
        return TestTarget(file=m.group(1), class_name=m.group(2), function=m.group(3))

    # pytest -k name
    m = re.search(r'pytest.*-k\s+["\']?(\w+)', cmd)
    if m:
        return TestTarget(function=m.group(1))

    if not is_test_command(cmd):
        return None

    return TestTarget(is_broad=True)

def classify_test_command(command: str, edited_files: set[str], edited_symbols: set[str], file_to_tests: dict | None = None) -> str:
    target = parse_test_command(command)
    if target is None:
        return "not_a_test"
    if target.is_broad:
        return "broad_project_verification"
    if target.function and target.function in edited_symbols:
        return "targeted_to_edited_symbol"
    if target.file:
        for ef in edited_files:
            stem = os.path.splitext(os.path.basename(ef))[0].replace("test_", "").replace("_test", "")
            if stem and len(stem) > 2:
                test_stem = f"test_{stem}"
                if test_stem in target.file or f"{stem}_test" in target.file:
                    return "targeted_to_edited_file"
        if file_to_tests:
            for ef in edited_files:
                if target.file in file_to_tests.get(ef, []):
                    return "targeted_to_related_test"
        return "irrelevant_verification"
    return "unknown"

def is_test_command(command: str) -> bool:
    patterns = ['pytest', 'python -m pytest', 'python -m unittest', 'npm test', 'yarn test', 'go test', 'cargo test', 'make test', 'tox', 'nox', 'rspec', 'manage.py test']
    cmd_lower = command.lower()
    return any(p in cmd_lower for p in patterns)
