r"""ITEM 3 (2026-07-12) — the D2 read-set residual seam (named in the D2 LIPI).

``_command_path_targets`` parses path-shaped tokens from the WHOLE command string. Seeding the
per-attempt READ-SET with it on EVERY kkind meant an EDIT command's heredoc / str_replace PAYLOAD
— a file merely MENTIONED in the written code, never opened — seeded ``read_targets`` and could
SUPPRESS a localization fact for a file that was only referenced (the global arbiter treats the
read-set as already-acquired).

FIX (``_seed_read_targets``, smallest correct scope): on an EDIT kind the real acquisition is the
classifier's file target ``kf`` (seeded directly); the command-body path parse runs ONLY on
non-edit kinds (view / search / grep name the file they open). Byte-identical for view/search.

PINNED HERE (pure — the helper is unit-tested directly, no turn machinery):
  * an EDIT whose heredoc body mentions ``src/foo.py`` does NOT seed it (only the edit TARGET);
  * a genuine VIEW / GREP of a file DOES seed it;
  * MUTATION (revert the scope guard -> parse the body on edit kinds too) -> the phantom body path
    is seeded -> reddens.

Windows: run with PYTHONIOENCODING=utf-8.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import gt_mini_patch as g  # noqa: E402


ROOT = "/repo"  # a relative path passes through _to_repo_rel unchanged (case 4)


def test_edit_command_body_does_not_seed_phantom_path():
    rt: set = set()
    # a heredoc write to src/bar.py whose BODY mentions src/foo.py (never opened).
    cmd = "cat > src/bar.py <<'EOF'\nimport os  # cf. src/foo.py\nEOF"
    g._seed_read_targets(rt, "post_edit", "src/bar.py", cmd, ROOT)
    assert "src/foo.py" not in rt, "a file merely MENTIONED in the edit body must not seed the read-set"
    assert "src/bar.py" in rt, "the actual edit TARGET is the real acquisition -> seeded"


def test_view_command_seeds_opened_file():
    rt: set = set()
    g._seed_read_targets(rt, "post_view", "src/foo.py", "sed -n 1,50p src/foo.py", ROOT)
    assert "src/foo.py" in rt


def test_grep_of_explicit_file_seeds_it_on_non_edit_kind():
    rt: set = set()
    # a bare (no-kf) grep naming an explicit file still seeds via the body parse.
    g._seed_read_targets(rt, None, None, "grep -n foo src/bar.py", ROOT)
    assert "src/bar.py" in rt


def test_multi_file_grep_seeds_all_named_files():
    rt: set = set()
    g._seed_read_targets(rt, "post_view", "pkg/a.go", "grep -n x pkg/a.go pkg/b.go", ROOT)
    assert {"pkg/a.go", "pkg/b.go"} <= rt   # kf + the explicit args


def test_none_read_targets_is_noop():
    g._seed_read_targets(None, "post_edit", "src/bar.py", "x", ROOT)  # must not raise


def test_mutation_body_parse_on_edit_seeds_phantom(monkeypatch):
    """MUTATION — revert the scope guard so the body parse runs on edit kinds too (the pre-fix
    behaviour): the mentioned-never-opened ``src/foo.py`` is seeded -> reddens the residual pin."""
    rt: set = set()
    cmd = "cat > src/bar.py <<'EOF'\nimport os  # cf. src/foo.py\nEOF"

    # the mutated seeder: unconditional body parse (no `kkind != post_edit` guard).
    def _mutated(read_targets, kkind, kf, command, root):
        if kkind in ("post_view", "post_edit") and kf:
            read_targets.add(g._norm_fp(g._to_repo_rel(kf, root)))
        read_targets.update(g._command_path_targets(command or ""))   # ungated (the mutation)

    _mutated(rt, "post_edit", "src/bar.py", cmd, ROOT)
    assert "src/foo.py" in rt, "the mutation seeds the phantom body path (this is the reddening state)"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
