"""Tests for content hash computation."""

from __future__ import annotations

import os
import tempfile

from groundtruth.index.hasher import content_hash


class TestContentHash:
    """Test SHA-256 content hashing."""

    def test_hash_returns_hex_string(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write("def hello(): pass\n")
            f.flush()
            result = content_hash(f.name)
        os.unlink(f.name)
        assert result is not None
        assert len(result) == 64  # SHA-256 hex is 64 chars
        assert all(c in "0123456789abcdef" for c in result)

    def test_same_content_same_hash(self) -> None:
        content = "x = 42\n"
        hashes = []
        for _ in range(2):
            with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
                f.write(content)
                f.flush()
                hashes.append(content_hash(f.name))
            os.unlink(f.name)
        assert hashes[0] == hashes[1]

    def test_different_content_different_hash(self) -> None:
        files = []
        for text in ["a = 1\n", "b = 2\n"]:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
                f.write(text)
                f.flush()
                files.append((f.name, content_hash(f.name)))
        for name, _ in files:
            os.unlink(name)
        assert files[0][1] != files[1][1]

    def test_nonexistent_file_returns_none(self) -> None:
        assert content_hash("/nonexistent/path/to/file.py") is None

    def test_empty_file(self) -> None:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.flush()  # empty file
            result = content_hash(f.name)
        os.unlink(f.name)
        assert result is not None
        assert len(result) == 64
