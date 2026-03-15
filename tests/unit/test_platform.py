"""Tests for cross-platform utilities."""

from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

from groundtruth.utils.platform import (
    is_macos,
    is_windows,
    normalize_path,
    path_to_uri,
    paths_equal,
    resolve_command,
    uri_to_path,
)


class TestResolveCommand:
    """Tests for resolve_command()."""

    def test_resolves_existing_command(self) -> None:
        """Should resolve an existing executable via shutil.which."""
        # Python itself should always be resolvable
        result = resolve_command(["python", "--version"])
        assert len(result) == 2
        assert result[1] == "--version"
        # The resolved path should be an absolute path or at least non-empty
        assert result[0]

    def test_returns_original_for_missing_command(self) -> None:
        """Should return the original list if command not found."""
        result = resolve_command(["nonexistent_command_xyz_123", "--foo"])
        assert result == ["nonexistent_command_xyz_123", "--foo"]

    def test_empty_list(self) -> None:
        """Should return empty list for empty input."""
        assert resolve_command([]) == []

    def test_cmd_wrapper_prepends_cmd_exe_on_windows(self) -> None:
        """On Windows, .cmd files should be wrapped with cmd.exe /c."""
        with patch("groundtruth.utils.platform.is_windows", return_value=True), \
             patch("shutil.which", return_value="C:\\Program Files\\node\\pyright.cmd"):
            result = resolve_command(["pyright-langserver", "--stdio"])
            assert result[0] == "cmd.exe"
            assert result[1] == "/c"
            assert result[2] == "C:\\Program Files\\node\\pyright.cmd"
            assert result[3] == "--stdio"

    def test_bat_wrapper_prepends_cmd_exe_on_windows(self) -> None:
        """On Windows, .bat files should be wrapped with cmd.exe /c."""
        with patch("groundtruth.utils.platform.is_windows", return_value=True), \
             patch("shutil.which", return_value="C:\\tools\\server.BAT"):
            result = resolve_command(["server", "--arg"])
            assert result[0] == "cmd.exe"
            assert result[1] == "/c"
            assert result[2] == "C:\\tools\\server.BAT"
            assert result[3] == "--arg"

    def test_exe_not_wrapped_on_windows(self) -> None:
        """On Windows, .exe files should NOT be wrapped with cmd.exe."""
        with patch("groundtruth.utils.platform.is_windows", return_value=True), \
             patch("shutil.which", return_value="C:\\Python\\python.exe"):
            result = resolve_command(["python", "--version"])
            assert result[0] == "C:\\Python\\python.exe"
            assert result[1] == "--version"
            assert len(result) == 2

    def test_cmd_not_wrapped_on_unix(self) -> None:
        """On Unix, .cmd files should NOT be wrapped (hypothetical edge case)."""
        with patch("groundtruth.utils.platform.is_windows", return_value=False), \
             patch("shutil.which", return_value="/usr/local/bin/something.cmd"):
            result = resolve_command(["something", "--arg"])
            assert result[0] == "/usr/local/bin/something.cmd"
            assert result[1] == "--arg"
            assert len(result) == 2

    def test_extensionless_npm_shim_uses_cmd_sibling_on_windows(self) -> None:
        """On Windows, extensionless npm shims should fall back to .cmd sibling."""
        with patch("groundtruth.utils.platform.is_windows", return_value=True), \
             patch("shutil.which", return_value="C:\\npm\\pyright-langserver"), \
             patch("os.path.isfile", return_value=True):
            result = resolve_command(["pyright-langserver", "--stdio"])
            assert result[0] == "cmd.exe"
            assert result[1] == "/c"
            assert result[2] == "C:\\npm\\pyright-langserver.cmd"
            assert result[3] == "--stdio"

    def test_extensionless_no_cmd_sibling_on_windows(self) -> None:
        """On Windows, extensionless file with no .cmd sibling should be used as-is."""
        with patch("groundtruth.utils.platform.is_windows", return_value=True), \
             patch("shutil.which", return_value="C:\\tools\\mytool"), \
             patch("os.path.isfile", return_value=False):
            result = resolve_command(["mytool", "--arg"])
            assert result[0] == "C:\\tools\\mytool"
            assert result[1] == "--arg"
            assert len(result) == 2


class TestNormalizePath:
    """Tests for normalize_path()."""

    def test_forward_slashes_unchanged(self) -> None:
        assert normalize_path("src/foo/bar.py") == os.path.normpath("src/foo/bar.py").replace("\\", "/")

    def test_backslashes_converted(self) -> None:
        result = normalize_path("src\\foo\\bar.py")
        assert "\\" not in result
        assert "foo" in result
        assert "bar.py" in result

    def test_mixed_separators(self) -> None:
        result = normalize_path("src/foo\\bar.py")
        assert "\\" not in result

    def test_dot_components_resolved(self) -> None:
        result = normalize_path("src/./foo/../bar.py")
        assert result == normalize_path("src/bar.py")

    def test_windows_absolute_path(self) -> None:
        result = normalize_path("C:\\Users\\test\\project\\file.py")
        assert "\\" not in result
        assert "Users" in result


class TestUriToPath:
    """Tests for uri_to_path()."""

    def test_unix_uri(self) -> None:
        result = uri_to_path("file:///home/user/project/file.py")
        if sys.platform == "win32":
            # On Windows, /home/user/... is unusual but should still normalize
            assert "\\" not in result
        else:
            assert result == "/home/user/project/file.py"

    def test_windows_uri(self) -> None:
        with patch("groundtruth.utils.platform.is_windows", return_value=True):
            result = uri_to_path("file:///C:/Users/test/file.py")
            assert "\\" not in result
            assert "C:" in result
            assert "Users/test/file.py" in result

    def test_non_uri_passthrough(self) -> None:
        result = uri_to_path("src/foo/bar.py")
        assert "\\" not in result
        assert "bar.py" in result

    def test_encoded_spaces(self) -> None:
        result = uri_to_path("file:///home/user/my%20project/file.py")
        assert "my project" in result

    def test_unc_path_uri_on_windows(self) -> None:
        """On Windows, UNC file URIs should preserve the server/share prefix."""
        with patch("groundtruth.utils.platform.is_windows", return_value=True):
            result = uri_to_path("file://server/share/file.py")
            assert "//server/share/file.py" in result


class TestPathsEqual:
    """Tests for paths_equal()."""

    def test_identical_paths(self) -> None:
        assert paths_equal("src/foo/bar.py", "src/foo/bar.py")

    def test_different_separators(self) -> None:
        assert paths_equal("src/foo/bar.py", "src\\foo\\bar.py")

    def test_case_sensitivity_on_windows(self) -> None:
        with patch("groundtruth.utils.platform.is_windows", return_value=True):
            assert paths_equal("SRC/Foo/Bar.py", "src/foo/bar.py")

    def test_case_sensitivity_on_unix(self) -> None:
        with patch("groundtruth.utils.platform.is_windows", return_value=False), \
             patch("groundtruth.utils.platform.is_macos", return_value=False):
            assert not paths_equal("SRC/Foo/Bar.py", "src/foo/bar.py")

    def test_case_insensitive_on_macos(self) -> None:
        """macOS uses case-insensitive APFS/HFS+ by default."""
        with patch("groundtruth.utils.platform.is_macos", return_value=True), \
             patch("groundtruth.utils.platform.is_windows", return_value=False):
            assert paths_equal("SRC/Foo/Bar.py", "src/foo/bar.py")

    def test_different_paths(self) -> None:
        assert not paths_equal("src/foo.py", "src/bar.py")


class TestPathToUri:
    """Tests for path_to_uri()."""

    def test_no_backslashes_in_output(self) -> None:
        result = path_to_uri(os.path.abspath("src/foo/bar.py"))
        assert "\\" not in result
        assert result.startswith("file://")
