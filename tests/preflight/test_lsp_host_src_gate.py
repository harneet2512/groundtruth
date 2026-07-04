"""C1+C2+C3 — the GT_REQUIRE_LSP host-source warm gate.

Root cause this pins (run 300_e51cc3f0, 8 tasks dead with NO_SOURCE_EXT):
the host wrapper process cannot read the in-container /workspace/<task>, so when
GT_HOST_SRC_ROOT was missing the warm gate fell back to that container path,
_dominant_ext() walked it on the HOST, found nothing, and GT_REQUIRE_LSP=1
fail-closed — mislabeled "image-pull/network".

These tests assert the fix:
  C2: under GT_REQUIRE_LSP, there is NO dead fallback to the container path.
  C3: the failure is classified LSP_FAIL_NO_HOST_SRC (a plumbing/Point-A miss),
      distinct from LSP_FAIL_NO_WARM (pyright didn't handshake).
  C1: a host-source check that fails on an empty/absent export.
"""
import os
import pytest

from groundtruth.lsp.edge_verifier import (
    resolve_warm_source_root,
    host_src_has_lsp_source,
    LSP_FAIL_NO_HOST_SRC,
    LSP_FAIL_NO_WARM,
)


def test_no_dead_container_fallback_under_require_lsp():
    # C2: host source absent + require_lsp -> must NOT return the container path
    # (the host process cannot read it); must classify the plumbing failure.
    root, fail = resolve_warm_source_root(
        host_src="", container_root="/workspace/beetbox__beets-5457", require_lsp=True
    )
    assert root is None
    assert fail == LSP_FAIL_NO_HOST_SRC


def test_absent_dir_treated_as_missing(tmp_path):
    missing = str(tmp_path / "does_not_exist")
    root, fail = resolve_warm_source_root(missing, "/workspace/x", require_lsp=True)
    assert root is None and fail == LSP_FAIL_NO_HOST_SRC


def test_host_src_used_when_present(tmp_path):
    (tmp_path / "mod.py").write_text("def f(): pass\n", encoding="utf-8")
    root, fail = resolve_warm_source_root(str(tmp_path), "/workspace/x", require_lsp=True)
    assert root == str(tmp_path)
    assert fail is None


def test_legacy_container_fallback_only_when_not_required():
    # Non-paid / legacy path (require_lsp False) may still use the container path.
    root, fail = resolve_warm_source_root("", "/workspace/x", require_lsp=False)
    assert root == "/workspace/x"
    assert fail is None


def test_taxonomy_classes_are_distinct():
    # C3: the two failure modes must be separable so the operator isn't misdirected
    # to "install pyright" when the real miss is the host export.
    assert LSP_FAIL_NO_HOST_SRC != LSP_FAIL_NO_WARM


def test_check_host_src_fails_on_empty_dir(tmp_path):
    # C1: an export dir with no LSP-supported source must fail the check.
    assert host_src_has_lsp_source(str(tmp_path)) is False


def test_check_host_src_ignores_scaffold_only(tmp_path):
    # The exact 300_e51cc3f0 shape: only .openhands scaffold present, no real source.
    d = tmp_path / ".openhands"
    d.mkdir()
    (d / "TASKS.md").write_text("# tasks\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("readme\n", encoding="utf-8")
    assert host_src_has_lsp_source(str(tmp_path)) is False


def test_check_host_src_passes_with_real_source(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "core.py").write_text("x = 1\n", encoding="utf-8")
    assert host_src_has_lsp_source(str(tmp_path)) is True


def test_check_host_src_absent_is_false():
    assert host_src_has_lsp_source("") is False
    assert host_src_has_lsp_source("/no/such/dir/xyz") is False
