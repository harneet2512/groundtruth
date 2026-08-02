from __future__ import annotations

import os
import subprocess
import sys
import textwrap


def _run_isolated(source: str) -> None:
    """Run import assertions without invalidating classes collected by pytest."""
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(source)],
        cwd=os.getcwd(),
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_narrow_runtime_import_does_not_load_comparison_control_dependencies() -> None:
    _run_isolated(
        """
        import importlib
        import sys

        importlib.import_module("groundtruth.runtime.observation_compiler")
        assert "groundtruth.runtime.context" not in sys.modules
        assert "groundtruth.runtime.proof" not in sys.modules
        assert "groundtruth.memory.enrich.embed" not in sys.modules
        """
    )


def test_legacy_runtime_exports_remain_lazy_and_compatible() -> None:
    _run_isolated(
        """
        import importlib
        import sys

        runtime = importlib.import_module("groundtruth.runtime")
        assert "groundtruth.runtime.context" not in sys.modules
        assert runtime.GTRuntimeContext.__name__ == "GTRuntimeContext"
        assert "groundtruth.runtime.context" in sys.modules
        """
    )
