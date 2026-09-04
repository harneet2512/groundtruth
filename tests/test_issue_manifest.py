"""Tests for scripts/swebench/issue_manifest.py."""

from __future__ import annotations

import importlib.util
import json
import os
import tempfile


def _load():
    path = os.path.join(os.path.dirname(__file__), "..", "scripts", "swebench", "issue_manifest.py")
    spec = importlib.util.spec_from_file_location("issue_manifest_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_write_issue_manifest():
    mod = _load()
    with tempfile.TemporaryDirectory() as td:
        issue = os.path.join(td, "issue.txt")
        out = os.path.join(td, "issue_manifest.json")
        open(issue, "w", encoding="utf-8").write("fix the bug")
        manifest = mod.write_issue_manifest(issue, out, source="instruction.md")
        assert manifest["non_empty"] is True
        assert manifest["source"] == "instruction.md"
        on_disk = json.load(open(out, encoding="utf-8"))
        assert on_disk["sha256"] == manifest["sha256"]
