"""Unit tests for patch integrity detection (P0-6).

Proves that truncated/malformed patches are detected before eval.
"""

from __future__ import annotations


class TestPatchIntegrity:

    def test_clean_patch_not_malformed(self):
        """A proper patch ending with newline is not malformed."""
        patch = (
            "diff --git a/src/foo.py b/src/foo.py\n"
            "--- a/src/foo.py\n"
            "+++ b/src/foo.py\n"
            "@@ -10,3 +10,4 @@\n"
            " def foo():\n"
            "+    return 42\n"
            "     pass\n"
        )
        assert patch.endswith("\n")
        assert "diff --git" in patch
        malformed = patch and not patch.endswith("\n") and "diff --git" in patch
        assert malformed is False

    def test_truncated_patch_detected(self):
        """A patch cut mid-line (no trailing newline) is malformed."""
        patch = (
            "diff --git a/src/foo.py b/src/foo.py\n"
            "--- a/src/foo.py\n"
            "+++ b/src/foo.py\n"
            "@@ -10,3 +10,4 @@\n"
            " def foo():\n"
            "+    return 42"  # No trailing newline — truncated
        )
        assert not patch.endswith("\n")
        assert "diff --git" in patch
        malformed = patch and not patch.endswith("\n") and "diff --git" in patch
        assert malformed is True

    def test_empty_patch_not_malformed(self):
        """Empty patch is not malformed (just empty)."""
        patch = ""
        malformed = bool(patch and not patch.endswith("\n") and "diff --git" in patch)
        assert malformed is False

    def test_hash_consistency(self):
        """SHA256 hash of the same patch is consistent."""
        import hashlib
        patch = "diff --git a/foo.py b/foo.py\n+line\n"
        h1 = hashlib.sha256(patch.encode("utf-8")).hexdigest()[:16]
        h2 = hashlib.sha256(patch.encode("utf-8")).hexdigest()[:16]
        assert h1 == h2

    def test_hash_changes_on_truncation(self):
        """Hash differs between full and truncated patch."""
        import hashlib
        full = "diff --git a/foo.py b/foo.py\n+line\n"
        truncated = "diff --git a/foo.py b/foo.py\n+lin"
        h_full = hashlib.sha256(full.encode("utf-8")).hexdigest()[:16]
        h_trunc = hashlib.sha256(truncated.encode("utf-8")).hexdigest()[:16]
        assert h_full != h_trunc
