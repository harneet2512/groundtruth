"""Python-specific runtime introspection trust substrate.

Provides runtime verification of class/member existence via import + dir(),
catching metaclass-injected methods, mixin methods, __getattr__ virtual
attributes, and descriptor protocol methods that AST parsing misses.
"""

from __future__ import annotations

import importlib
import threading
from dataclasses import dataclass, field
from enum import Enum


class TrustLevel(str, Enum):
    """Evidence level for a symbol's existence."""

    GREEN = "green"  # Runtime-confirmed (import + dir() succeeded)
    YELLOW = "yellow"  # AST-only (couldn't import, but AST shows it)
    RED = "red"  # Neither (no evidence at all)


@dataclass
class TrustResult:
    """Result of a trust check on a symbol."""

    level: TrustLevel
    symbol_name: str
    checked_via: str  # "runtime" | "ast" | "none"
    members: list[str] = field(default_factory=list)
    error: str | None = None


class RuntimeIntrospector:
    """Introspect Python classes at runtime to verify member existence.

    Uses importlib + dir() to discover every method/attribute Python actually
    sees, including those injected by metaclasses, mixins, __getattr__, and
    descriptors. Falls back gracefully on any failure.
    """

    def __init__(self, timeout_seconds: float = 2.0) -> None:
        self._timeout = timeout_seconds

    def check_class(self, module_path: str, class_name: str) -> TrustResult:
        """Try to import module and inspect class via dir().

        Returns GREEN with full member list on success.
        Returns YELLOW with error message on any failure.
        Never crashes.
        """
        try:
            module = self._import_with_timeout(module_path)
            cls = getattr(module, class_name)
            members = list(dir(cls))
            return TrustResult(
                level=TrustLevel.GREEN,
                symbol_name=f"{module_path}.{class_name}",
                checked_via="runtime",
                members=members,
            )
        except Exception as exc:  # noqa: BLE001
            return TrustResult(
                level=TrustLevel.YELLOW,
                symbol_name=f"{module_path}.{class_name}",
                checked_via="ast",
                error=f"{type(exc).__name__}: {exc}",
            )

    def check_member(self, module_path: str, class_name: str, member_name: str) -> TrustResult:
        """Check if a specific member exists on a class at runtime.

        GREEN if runtime-confirmed present, RED if runtime-confirmed absent,
        YELLOW if runtime check itself failed.
        """
        class_result = self.check_class(module_path, class_name)

        if class_result.level == TrustLevel.YELLOW:
            return TrustResult(
                level=TrustLevel.YELLOW,
                symbol_name=f"{module_path}.{class_name}.{member_name}",
                checked_via="ast",
                error=class_result.error,
            )

        if member_name in class_result.members:
            return TrustResult(
                level=TrustLevel.GREEN,
                symbol_name=f"{module_path}.{class_name}.{member_name}",
                checked_via="runtime",
                members=class_result.members,
            )

        return TrustResult(
            level=TrustLevel.RED,
            symbol_name=f"{module_path}.{class_name}.{member_name}",
            checked_via="runtime",
            error=f"'{member_name}' not found in dir({class_name})",
        )

    def _import_with_timeout(self, module_path: str) -> object:
        """Import a module with a threading-based timeout.

        Raises TimeoutError if import takes longer than self._timeout.
        Re-raises any import exception from the worker thread.
        """
        result: list[object] = []
        error: list[BaseException] = []

        def _do_import() -> None:
            try:
                result.append(importlib.import_module(module_path))
            except BaseException as exc:  # noqa: BLE001
                error.append(exc)

        t = threading.Thread(target=_do_import, daemon=True)
        t.start()
        t.join(timeout=self._timeout)

        if t.is_alive():
            raise TimeoutError(f"Import of '{module_path}' exceeded {self._timeout}s timeout")
        if error:
            raise error[0]  # noqa: RSE102
        return result[0]
