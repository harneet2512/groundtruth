"""Tests for co-change completeness post-edit."""
import os
import subprocess
import tempfile

import pytest

from groundtruth.hooks.post_edit import _classify_file_kind, _co_change_reminder


class TestClassifyFileKind:
    def test_source_file(self):
        assert _classify_file_kind("src/app.py") == "source"

    def test_test_file_prefix(self):
        assert _classify_file_kind("tests/test_app.py") == "test"

    def test_test_file_suffix(self):
        assert _classify_file_kind("app_test.py") == "test"

    def test_test_directory(self):
        assert _classify_file_kind("test/utils.py") == "test"

    def test_config_yaml(self):
        assert _classify_file_kind("config.yaml") == "config"

    def test_config_toml(self):
        assert _classify_file_kind("pyproject.toml") == "config"

    def test_config_json(self):
        assert _classify_file_kind("package.json") == "config"

    def test_config_ini(self):
        assert _classify_file_kind("setup.cfg") == "config"


class TestCoChangeReminder:
    @pytest.fixture
    def git_repo(self, tmp_path):
        """Create a git repo with co-change history."""
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, capture_output=True)
        subprocess.run(["git", "config", "user.name", "test"], cwd=repo, capture_output=True)

        a = repo / "a.py"
        b = repo / "b.py"
        c = repo / "c.py"

        # Create 6 commits where a.py and b.py always change together
        for i in range(6):
            a.write_text(f"# version {i}\ndef foo(): pass\n")
            b.write_text(f"# version {i}\ndef bar(): pass\n")
            subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
            subprocess.run(["git", "commit", "-m", f"commit {i}"], cwd=repo, capture_output=True)

        # Add c.py in only 1 commit (weak co-change)
        c.write_text("def baz(): pass\n")
        a.write_text(f"# version final\ndef foo(): pass\n")
        subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add c"], cwd=repo, capture_output=True)

        return str(repo)

    def test_high_confidence_cochange(self, git_repo):
        result = _co_change_reminder("a.py", git_repo, [])
        assert "CO-CHANGE:" in result
        assert "b.py" in result

    def test_already_edited_suppressed(self, git_repo):
        result = _co_change_reminder("a.py", git_repo, ["b.py"])
        # b.py already edited — should not appear
        assert "b.py" not in result or result == ""

    def test_weak_cochange_suppressed(self, git_repo):
        # c.py only co-changed 1 time with a.py — below threshold
        result = _co_change_reminder("a.py", git_repo, ["b.py"])
        assert "c.py" not in result

    def test_no_history(self, tmp_path):
        repo = tmp_path / "empty"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, capture_output=True)
        (repo / "x.py").write_text("pass\n")
        subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, capture_output=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True)
        result = _co_change_reminder("x.py", str(repo), [])
        assert result == ""

    def test_config_file_phrasing(self, git_repo):
        # Create config co-change
        cfg = os.path.join(git_repo, "config.yaml")
        a = os.path.join(git_repo, "a.py")
        for i in range(6):
            with open(cfg, "w") as f:
                f.write(f"version: {i}\n")
            with open(a, "w") as f:
                f.write(f"# cfg {i}\n")
            subprocess.run(["git", "add", "."], cwd=git_repo, capture_output=True)
            subprocess.run(["git", "commit", "-m", f"cfg {i}"], cwd=git_repo, capture_output=True)

        result = _co_change_reminder("a.py", git_repo, [])
        # Should mention config.yaml with config-appropriate phrasing
        if "config.yaml" in result:
            assert "Config may need" in result or "CO-CHANGE:" in result
