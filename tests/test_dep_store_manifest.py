"""P0-04/P0-05 — dep_store_manifest fail-closed validation."""

from __future__ import annotations

import json
import os
import tempfile


from scripts.swebench.dep_store_manifest import (
    build_manifest,
    validate_manifest,
    write_manifest,
)


def _touch_tree(root: str, rel_paths: list[str]) -> None:
    for rel in rel_paths:
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(b"x")


def test_go_empty_gomodcache_is_evidence_not_gate():
    with tempfile.TemporaryDirectory() as td:
        gm = os.path.join(td, "gomodcache")
        os.makedirs(gm)
        manifest = build_manifest(
            language="go",
            gomodcache_host=gm,
            gomodcache_source="/custom/go/pkg/mod",
            gomodcache_declared_source="/custom/go/pkg/mod",
            cargo_host=os.path.join(td, "cargo"),
            cargo_source="",
            rustup_host=os.path.join(td, "rustup"),
            rustup_source="",
        )
        assert validate_manifest(manifest) == []
        assert manifest["stores"]["gomodcache"]["declared_in_task_image"] == "/custom/go/pkg/mod"


def test_go_passes_with_non_default_gomodcache_layout():
    with tempfile.TemporaryDirectory() as td:
        # mars-base may use a non-/root/go/pkg/mod GOMODCACHE path
        gm = os.path.join(td, "gomodcache")
        _touch_tree(gm, ["cache/download/golang.org/x/tools/@v/v0.16.2.mod"])
        manifest = build_manifest(
            language="go",
            gomodcache_host=gm,
            gomodcache_source="/home/user/go/pkg/mod",
            gomodcache_declared_source="/home/user/go/pkg/mod",
            cargo_host=os.path.join(td, "cargo"),
            cargo_source="",
            rustup_host=os.path.join(td, "rustup"),
            rustup_source="",
        )
        assert validate_manifest(manifest) == []
        assert manifest["stores"]["gomodcache"]["file_count"] == 1
        assert manifest["stores"]["gomodcache"]["source_in_task_image"] == "/home/user/go/pkg/mod"
        assert manifest["stores"]["gomodcache"]["declared_in_task_image"] == "/home/user/go/pkg/mod"


def test_go_manifest_keeps_declared_gomodcache_when_copy_is_missing():
    with tempfile.TemporaryDirectory() as td:
        gm = os.path.join(td, "gomodcache")
        os.makedirs(gm)
        manifest = build_manifest(
            language="go",
            gomodcache_host=gm,
            gomodcache_source="",
            gomodcache_declared_source="/workspace/.cache/go/pkg/mod",
            cargo_host=os.path.join(td, "cargo"),
            cargo_source="",
            rustup_host=os.path.join(td, "rustup"),
            rustup_source="",
        )
        assert validate_manifest(manifest) == []
        assert (
            manifest["stores"]["gomodcache"]["declared_in_task_image"]
            == "/workspace/.cache/go/pkg/mod"
        )


def test_rust_fail_closed_missing_rustup():
    with tempfile.TemporaryDirectory() as td:
        cargo = os.path.join(td, "cargo")
        _touch_tree(cargo, ["registry/index/foo"])
        rustup = os.path.join(td, "rustup")
        os.makedirs(rustup)
        manifest = build_manifest(
            language="rust",
            gomodcache_host=os.path.join(td, "gomodcache"),
            gomodcache_source="",
            gomodcache_declared_source="",
            cargo_host=cargo,
            cargo_source="/root/.cargo",
            rustup_host=rustup,
            rustup_source="",
        )
        problems = validate_manifest(manifest)
        assert any("rustup" in p for p in problems)
        assert any("rust_src" in p for p in problems)


def test_rust_fail_closed_missing_rust_src_for_active_toolchain():
    with tempfile.TemporaryDirectory() as td:
        cargo = os.path.join(td, "cargo")
        _touch_tree(cargo, ["registry/index/foo"])
        rustup = os.path.join(td, "rustup")
        _touch_tree(rustup, ["toolchains/stable-x86_64-unknown-linux-gnu/bin/rustc"])
        manifest = build_manifest(
            language="rust",
            gomodcache_host=os.path.join(td, "gomodcache"),
            gomodcache_source="",
            gomodcache_declared_source="",
            cargo_host=cargo,
            cargo_source="/root/.cargo",
            rustup_host=rustup,
            rustup_source="/root/.rustup",
            rust_toolchain="stable-x86_64-unknown-linux-gnu",
        )
        problems = validate_manifest(manifest)
        assert any("rust_src" in p for p in problems)


def test_rust_passes_with_rust_src_for_active_toolchain():
    with tempfile.TemporaryDirectory() as td:
        cargo = os.path.join(td, "cargo")
        _touch_tree(cargo, ["registry/index/foo"])
        rustup = os.path.join(td, "rustup")
        _touch_tree(
            rustup,
            [
                "toolchains/stable-x86_64-unknown-linux-gnu/bin/rustc",
                "toolchains/stable-x86_64-unknown-linux-gnu/lib/rustlib/src/rust/library/core/src/lib.rs",
            ],
        )
        manifest = build_manifest(
            language="rust",
            gomodcache_host=os.path.join(td, "gomodcache"),
            gomodcache_source="",
            gomodcache_declared_source="",
            cargo_host=cargo,
            cargo_source="/root/.cargo",
            rustup_host=rustup,
            rustup_source="/root/.rustup",
            rust_toolchain="stable-x86_64-unknown-linux-gnu",
        )
        assert validate_manifest(manifest) == []
        assert (
            manifest["stores"]["rust_src"]["active_toolchain"] == "stable-x86_64-unknown-linux-gnu"
        )
        assert manifest["stores"]["rust_src"]["source_in_task_image"].endswith(
            "/toolchains/stable-x86_64-unknown-linux-gnu/lib/rustlib/src/rust/library"
        )


def test_write_manifest_roundtrip():
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "dep_store_manifest.json")
        manifest = build_manifest(
            language="python",
            gomodcache_host=os.path.join(td, "gomodcache"),
            gomodcache_source="",
            gomodcache_declared_source="",
            cargo_host=os.path.join(td, "cargo"),
            cargo_source="",
            rustup_host=os.path.join(td, "rustup"),
            rustup_source="",
        )
        write_manifest(out, manifest)
        with open(out, encoding="utf-8") as fh:
            loaded = json.load(fh)
        assert loaded["schema"] == "gt.dep_store_manifest.v1"
        assert validate_manifest(loaded) == []
