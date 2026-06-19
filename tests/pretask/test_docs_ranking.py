"""Tests for docs/source file ranking adjustment (Phase 2.2)."""
from __future__ import annotations

from groundtruth.pretask.v7_4_brief import _is_docs_file, _is_source_dir


class TestIsDocsFile:
    def test_markdown_files(self) -> None:
        assert _is_docs_file("readme.md")
        assert _is_docs_file("docs/guide.md")
        assert _is_docs_file("changelog.md")

    def test_rst_files(self) -> None:
        assert _is_docs_file("docs/api.rst")

    def test_docs_directory(self) -> None:
        assert _is_docs_file("docs/api/endpoints.py")
        assert _is_docs_file("doc/reference.txt")
        assert _is_docs_file("documentation/guide.html")

    def test_known_filenames(self) -> None:
        assert _is_docs_file("contributing")
        assert _is_docs_file("license")
        assert _is_docs_file("authors")

    def test_source_files_not_docs(self) -> None:
        assert not _is_docs_file("src/parser.py")
        assert not _is_docs_file("lib/utils.ts")
        assert not _is_docs_file("internal/auth/login.go")

    def test_config_files_not_docs(self) -> None:
        assert not _is_docs_file("pyproject.toml")
        assert not _is_docs_file("setup.cfg")
        assert not _is_docs_file("package.json")
        assert not _is_docs_file("tsconfig.json")


class TestIsSourceDir:
    def test_source_prefixes(self) -> None:
        assert _is_source_dir("src/parser.py")
        assert _is_source_dir("lib/utils.ts")
        assert _is_source_dir("pkg/auth/login.go")
        assert _is_source_dir("internal/db/client.go")
        assert _is_source_dir("core/engine.py")
        assert _is_source_dir("app/models.py")

    def test_non_source_dirs(self) -> None:
        assert not _is_source_dir("tests/test_parser.py")
        assert not _is_source_dir("docs/guide.md")
        assert not _is_source_dir("scripts/deploy.sh")
        assert not _is_source_dir("README.md")
