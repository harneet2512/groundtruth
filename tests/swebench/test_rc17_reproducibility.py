"""RC-17 reproducibility seal tests.

Covers the helper-level contracts shipped in:
  - scripts/swebench/swe_agent_smoke_runner.py (env allow-list,
    versions/fingerprint capture stubs, first-N selector — pure-python
    pieces only; no SWE-agent / Vertex calls)
  - scripts/swebench/image_name_resolver.py (latest refusal, digest
    capture, override map)
  - scripts/swebench/verify_report.py (run-ID dedup)

Each test asserts a single behavioral invariant from F_reproducibility.md.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts" / "swebench"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


# ---- F-002: image_name_resolver --------------------------------------------

def test_resolver_refuses_latest_in_image_name():
    from image_name_resolver import resolve_image_name

    with pytest.raises(ValueError):
        resolve_image_name(
            "kozea__weasyprint-2300",
            {"image_name": "starryzhang/sweb.eval.x86_64.kozea_1776_weasyprint-2300:latest"},
        )


def test_resolver_accepts_versioned_tag_in_image_name():
    from image_name_resolver import resolve_image_name

    out = resolve_image_name(
        "kozea__weasyprint-2300",
        {"image_name": "myorg/myimg:1.2.3"},
        docker_glob_fn=lambda _p: [],
    )
    assert out == "myorg/myimg:1.2.3"


def test_capture_image_digest_strips_tag_and_pins_sha():
    from image_name_resolver import capture_image_digest

    out = capture_image_digest(
        "starryzhang/sweb.eval.x86_64.kozea_1776_weasyprint-2300:1.0",
        docker_inspect_fn=lambda _i: "sha256:abc123",
    )
    assert out == "starryzhang/sweb.eval.x86_64.kozea_1776_weasyprint-2300@sha256:abc123"


def test_capture_image_digest_refuses_non_sha():
    from image_name_resolver import capture_image_digest

    out = capture_image_digest("img:1.0", docker_inspect_fn=lambda _i: "not-a-sha")
    assert out is None


def test_apply_digest_overrides_routes_by_instance_id():
    from image_name_resolver import apply_digest_overrides

    digests = {"k__w-2300": "starryzhang/sweb.eval.x86_64.k_w@sha256:deadbeef"}
    out = apply_digest_overrides("k__w-2300", "starryzhang/sweb.eval.x86_64.k_w:tag1", digests)
    assert out == digests["k__w-2300"]


def test_apply_digest_overrides_passthrough_on_miss():
    from image_name_resolver import apply_digest_overrides

    out = apply_digest_overrides("missing", "x:1", {"other": "z@sha256:abc"})
    assert out == "x:1"


# ---- F-005: env allow-list -------------------------------------------------

def test_build_subprocess_env_drops_non_allowlisted():
    from swe_agent_smoke_runner import _build_subprocess_env

    parent = {
        "PATH": "/usr/bin",
        "HOME": "/home/u",
        "GT_GRAPH_DB": "/tmp/g.db",
        "OPENAI_API_KEY": "leaked",
        "RANDOM_DEVELOPER_VAR": "x",
        "ANTHROPIC_API_KEY": "leaked2",
        "VERTEX_PROJECT": "proj",
    }
    out = _build_subprocess_env({"GT_INDEXES_ROOT": "/tmp/idx"}, parent_env=parent)
    assert "OPENAI_API_KEY" not in out
    assert "ANTHROPIC_API_KEY" not in out
    assert "RANDOM_DEVELOPER_VAR" not in out
    assert out["PATH"] == "/usr/bin"
    assert out["GT_GRAPH_DB"] == "/tmp/g.db"
    assert out["VERTEX_PROJECT"] == "proj"
    assert out["GT_INDEXES_ROOT"] == "/tmp/idx"


def test_persist_run_env_writes_sorted_json(tmp_path):
    from swe_agent_smoke_runner import _persist_run_env

    env = {"B": "2", "A": "1", "GT_X": "y"}
    _persist_run_env(tmp_path, env)
    text = (tmp_path / "run_env.json").read_text(encoding="utf-8")
    parsed = json.loads(text)
    # Sorted-key equality
    assert list(parsed.keys()) == ["A", "B", "GT_X"]
    assert parsed["A"] == "1"


# ---- F-009: verify_report dedup -------------------------------------------

def test_verify_report_section_run_id_extraction():
    sys.path.insert(0, str(SCRIPTS))
    from verify_report import _section_run_id

    section = "### [PASS] `cd_ab_a_lsp_1715000000_abc123`\n- when: ..."
    assert _section_run_id(section) == "cd_ab_a_lsp_1715000000_abc123"


def test_verify_report_dedup_refuses_duplicate(tmp_path):
    from verify_report import append_to_log

    doc = tmp_path / "verify_results.md"
    doc.write_text("<!-- APPEND_MARKER -->\n", encoding="utf-8")
    section = "### [PASS] `dup_run_id_xyz`\n- body line\n"
    append_to_log(doc, section)
    append_to_log(doc, section)  # second must be a no-op
    text = doc.read_text(encoding="utf-8")
    assert text.count("`dup_run_id_xyz`") == 1


def test_verify_report_distinct_run_ids_both_appended(tmp_path):
    from verify_report import append_to_log

    doc = tmp_path / "verify_results.md"
    doc.write_text("<!-- APPEND_MARKER -->\n", encoding="utf-8")
    a = "### [PASS] `run_a_111_aaa`\n- a\n"
    b = "### [FAIL] `run_b_222_bbb`\n- b\n"
    append_to_log(doc, a)
    append_to_log(doc, b)
    text = doc.read_text(encoding="utf-8")
    assert "`run_a_111_aaa`" in text
    assert "`run_b_222_bbb`" in text
