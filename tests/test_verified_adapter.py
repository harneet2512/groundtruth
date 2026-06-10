"""PATH B (SWE-bench Verified via mini-swe-agent) — dry unit tests of the two
GT injection points + the workflow guards. No docker, no network, no pier
required (the adapter's import shim covers a pier-less environment).

Covers:
  * instruction injection (a): single-<gt-task-brief>-tag invariant, preamble
    append, baseline no-op — through the REAL imported gt_agent functions;
  * fail-closed: proof/substrate mode with no substrate brief RAISES
    DeepSweAdapterError (never a silent host fallback);
  * environment injection (b): gt_mini_patch._wrap_execute appends GT evidence
    to a fake environment's execute() output over a tiny deterministic-edge
    sqlite graph (the [gt-patch:loaded] marker + <gt-evidence> witness);
  * env-to-container plumbing: DockerEnvironment.execute forwards config.env /
    forward_env as `docker exec -e KEY=VALUE` (subprocess captured, no docker);
  * image-name conventions: parity with scripts/vm/build_verified_manifest.py;
  * verified_run.yml: parses, workflow_dispatch-ONLY (committing never fires),
    per-task matrix job is bash-pinned with timeout-minutes 60.

All deterministic, stdlib + pytest + the installed minisweagent. No task IDs in
product logic — instance ids appear only as opaque naming-convention fixtures.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import os
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
_ADAPTER_PATH = _ROOT / "artifact_verified" / "gt_verified_agent.py"
_CONFIG_PATH = _ROOT / "artifact_verified" / "verified_gt.yaml"
_PATCH_PATH = _ROOT / "artifact_deepswe" / "gt_mini_patch.py"
_MANIFEST_PATH = _ROOT / "scripts" / "vm" / "build_verified_manifest.py"
_WORKFLOW = _ROOT / ".github" / "workflows" / "verified_run.yml"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _gt_env_clear(monkeypatch):
    """Strip every GT_* var so each test starts from a known, unset baseline."""
    for k in list(os.environ):
        if k.startswith("GT_"):
            monkeypatch.delenv(k, raising=False)


@pytest.fixture
def adapter(monkeypatch):
    _gt_env_clear(monkeypatch)
    return _load("gt_verified_agent_uut", _ADAPTER_PATH)


# ── a real edge-bearing graph (deepswe fixture shape) for the evidence test ──
def _make_graph(db_path: Path, repo_root: Path) -> None:
    repo_root.mkdir(parents=True, exist_ok=True)
    (repo_root / "a.py").write_text("def funcA(x):\n    return x + 1\n", encoding="utf-8")
    (repo_root / "b.py").write_text(
        "from a import funcA\n\ny = funcA(41)\n", encoding="utf-8"
    )
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE nodes (id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT, name TEXT, "
        "qualified_name TEXT, file_path TEXT, start_line INT, end_line INT, signature TEXT, "
        "return_type TEXT, is_exported INT, is_test INT, language TEXT, parent_id INT)"
    )
    conn.execute(
        "CREATE TABLE edges (id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INT, target_id INT, "
        "type TEXT, source_line INT, source_file TEXT, resolution_method TEXT, confidence REAL, "
        "metadata TEXT)"
    )
    conn.execute(
        "INSERT INTO nodes (label,name,file_path,start_line,signature,is_test,language) "
        "VALUES ('Function','funcA','a.py',1,'def funcA(x)',0,'python')"
    )
    conn.execute(
        "INSERT INTO nodes (label,name,file_path,start_line,signature,is_test,language) "
        "VALUES ('Function','module_b','b.py',1,'',0,'python')"
    )
    conn.execute(
        "INSERT INTO edges (source_id,target_id,type,source_line,source_file,"
        "resolution_method,confidence) VALUES (2,1,'CALLS',3,'b.py','import',1.0)"
    )
    conn.commit()
    conn.close()


# ═════════════════════════════════════════════════════════════════════════════
# Injection point (a): instruction — single tag, preamble, baseline, fail-closed
# ═════════════════════════════════════════════════════════════════════════════


def test_pretagged_brief_yields_exactly_one_tag(adapter, monkeypatch):
    """A substrate brief already starting with <gt-task-brief> must never be
    wrapped a second time (gt_agent._prepend_brief G2 invariant)."""
    brief = "<gt-task-brief>\nlook at pkg/mod.py\n</gt-task-brief>"
    issue = "Fix the bug in mod.py"
    out = adapter.build_augmented_task(issue, brief=brief)
    assert out.count("<gt-task-brief") == 1
    assert out.index("<gt-task-brief") < out.index(issue)  # brief PRECEDES the issue
    assert "GroundTruth codebase intelligence (automatic)" in out  # preamble appended


def test_untagged_brief_wrapped_exactly_once(adapter):
    out = adapter.build_augmented_task("Fix it", brief="plain brief text")
    assert out.count("<gt-task-brief>") == 1
    assert out.count("</gt-task-brief>") == 1
    assert "plain brief text" in out


def test_empty_brief_leaves_issue_intact_but_adds_preamble(adapter):
    issue = "Fix the bug"
    out = adapter.build_augmented_task(issue, brief="")
    assert "<gt-task-brief" not in out
    assert out.startswith(issue)


def test_baseline_arm_is_untouched(adapter, monkeypatch):
    monkeypatch.setenv("GT_BASELINE", "1")
    issue = "Fix the bug"
    assert adapter.build_augmented_task(issue, brief="<gt-task-brief>x</gt-task-brief>") == issue
    assert adapter._baseline() is True


def test_proof_mode_missing_substrate_brief_fails_closed(adapter, monkeypatch, tmp_path):
    """GT_PROOF_MODE=1 + GT_CERT_DIR without brief.txt -> DeepSweAdapterError
    (the REAL gt_agent._substrate_brief raise path — no host brief fallback)."""
    monkeypatch.setenv("GT_PROOF_MODE", "1")
    monkeypatch.setenv("GT_CERT_DIR", str(tmp_path))  # exists, but no brief.txt
    with pytest.raises(adapter.DeepSweAdapterError):
        adapter.build_augmented_task("Fix the bug")  # brief=None -> consume path


def test_substrate_brief_consumed_readonly(adapter, monkeypatch, tmp_path):
    """With a substrate brief.txt present, the consume path returns it and the
    assembled instruction keeps the single-tag invariant."""
    monkeypatch.setenv("GT_PROOF_MODE", "1")
    monkeypatch.setenv("GT_CERT_DIR", str(tmp_path))
    (tmp_path / "brief.txt").write_text(
        "<gt-task-brief>\nsubstrate says: mod.py\n</gt-task-brief>", encoding="utf-8"
    )
    out = adapter.build_augmented_task("Fix the bug")
    assert out.count("<gt-task-brief") == 1
    assert "substrate says: mod.py" in out


# ═════════════════════════════════════════════════════════════════════════════
# Injection point (b): environment execute — evidence append on a fake env
# ═════════════════════════════════════════════════════════════════════════════


def test_wrap_execute_appends_evidence_on_fake_env(monkeypatch, tmp_path):
    """gt_mini_patch._wrap_execute on a FAKE environment class: a source view
    gets the [gt-patch:loaded] marker + a deterministic-edge <gt-evidence>
    witness appended; returncode untouched. Substrate mode (GT_HOST_GRAPH_DB +
    GT_CERT_DIR) so _connect_ro takes the immutable read-only path and L6 stays
    off — the graph file is never mutated."""
    _gt_env_clear(monkeypatch)
    repo_root = tmp_path / "src"
    db = tmp_path / "graph.db"
    _make_graph(db, repo_root)
    root_file = tmp_path / "gt_root.txt"
    root_file.write_text(str(repo_root), encoding="utf-8")
    monkeypatch.setenv("GT_HOST_GRAPH_DB", str(db))
    monkeypatch.setenv("GT_CERT_DIR", str(tmp_path))
    monkeypatch.setenv("GT_ROOT_FILE", str(root_file))

    gmp = _load("gt_mini_patch_verified_uut", _PATCH_PATH)
    assert gmp._substrate_active() is True

    db_mtime = db.stat().st_mtime_ns

    class FakeEnv:
        def execute(self, action, *a, **k):
            return {"output": "1\tdef funcA(x):", "returncode": 0, "exception_info": ""}

    FakeEnv.execute = gmp._wrap_execute(FakeEnv.execute)
    out = FakeEnv().execute({"command": "cat a.py"})

    assert out["returncode"] == 0
    assert "[gt-patch:loaded]" in out["output"]
    assert "<gt-evidence" in out["output"]
    assert "[WITNESS]" in out["output"]  # the deterministic 'import' edge, never name_match
    # read-only consume: the substrate graph was not rewritten
    assert db.stat().st_mtime_ns == db_mtime


def test_wrap_execute_quiet_without_graph(monkeypatch, tmp_path):
    """Correct-or-quiet: no graph -> marker only, no fabricated evidence."""
    _gt_env_clear(monkeypatch)
    monkeypatch.setenv("GT_HOST_GRAPH_DB", str(tmp_path / "absent.db"))
    monkeypatch.setenv("GT_CERT_DIR", str(tmp_path))
    gmp = _load("gt_mini_patch_verified_quiet_uut", _PATCH_PATH)

    class FakeEnv:
        def execute(self, action, *a, **k):
            return {"output": "x", "returncode": 0}

    FakeEnv.execute = gmp._wrap_execute(FakeEnv.execute)
    out = FakeEnv().execute({"command": "cat a.py"})
    assert "<gt-evidence" not in out["output"]
    assert "[WITNESS]" not in out["output"]


def test_patch_targets_minisweagent_docker_environment():
    """The generic class list must name minisweagent's swebench environment —
    the attach point the Verified path relies on (gt_mini_patch.py:1217-1221)."""
    text = _PATCH_PATH.read_text(encoding="utf-8")
    assert '("minisweagent.environments.docker", "DockerEnvironment")' in text


def test_docker_environment_forwards_gt_env_to_container(monkeypatch):
    """Claim check (README §1b): DockerEnvironment.execute turns config.env and
    forward_env into `docker exec -e KEY=VALUE` (docker.py:107-113). Proven by
    capturing the subprocess argv — no docker daemon involved."""
    import minisweagent.environments.docker as docker_mod

    env = docker_mod.DockerEnvironment.__new__(docker_mod.DockerEnvironment)
    env.logger = logging.getLogger("test")
    env.config = docker_mod.DockerEnvironmentConfig(
        image="example:latest", env={"GT_PROBE": "1"}, forward_env=["GT_FWD_PROBE"]
    )
    env.container_id = "deadbeef"
    monkeypatch.setenv("GT_FWD_PROBE", "2")

    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return SimpleNamespace(stdout="ok", returncode=0)

    monkeypatch.setattr(docker_mod.subprocess, "run", fake_run)
    out = env.execute({"command": "echo hi"})
    assert out["returncode"] == 0
    cmd = captured["cmd"]
    pairs = {cmd[i + 1] for i, tok in enumerate(cmd[:-1]) if tok == "-e"}
    assert "GT_PROBE=1" in pairs
    assert "GT_FWD_PROBE=2" in pairs


# ═════════════════════════════════════════════════════════════════════════════
# Image-name conventions — parity with scripts/vm/build_verified_manifest.py
# ═════════════════════════════════════════════════════════════════════════════


def test_image_names_match_manifest_conventions(adapter):
    manifest = _load("build_verified_manifest_uut", _MANIFEST_PATH)
    for iid in ("astropy__astropy-12907", "django__django-11099", "PyLint-Dev__Pylint-1"):
        assert adapter.ghcr_epoch_image(iid) == manifest.image_ref("ghcr-epoch", iid)
        assert adapter.dockerhub_image(iid) == manifest.image_ref("dockerhub", iid)
    # the two registry-specific escapes, stated explicitly
    assert adapter.ghcr_epoch_image("a__b-1") == (
        "ghcr.io/epoch-research/swe-bench.eval.x86_64.a__b-1:latest"  # raw __ kept
    )
    assert adapter.dockerhub_image("a__b-1") == (
        "docker.io/swebench/sweb.eval.x86_64.a_1776_b-1:latest"  # __ -> _1776_
    )


def test_resolve_image_override_priority(adapter, monkeypatch):
    assert adapter.resolve_image("a__b-1", "custom:ref") == "custom:ref"
    monkeypatch.setenv("GT_TASK_IMAGE", "env:ref")
    assert adapter.resolve_image("a__b-1") == "env:ref"
    monkeypatch.delenv("GT_TASK_IMAGE")
    assert adapter.resolve_image("a__b-1") == adapter.ghcr_epoch_image("a__b-1")


# ═════════════════════════════════════════════════════════════════════════════
# Workflow + config guards
# ═════════════════════════════════════════════════════════════════════════════


def test_workflow_is_dispatch_only_and_parses():
    doc = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(doc, dict)
    triggers = doc.get("on", doc.get(True))  # yaml 1.1 parses bare `on:` as True
    assert isinstance(triggers, dict)
    assert set(triggers) == {"workflow_dispatch"}, (
        f"verified_run.yml must be workflow_dispatch-ONLY (committing it must "
        f"never fire a run); found triggers: {sorted(triggers)}"
    )
    trial = doc["jobs"]["trial"]
    assert trial["timeout-minutes"] == 60
    assert trial["defaults"]["run"]["shell"] == "bash"  # PIPESTATUS discipline
    assert "${{ fromJson(inputs.max_parallel) }}" in str(trial["strategy"]["max-parallel"])


def test_workflow_passes_workflow_lint():
    lint = _load("workflow_lint_uut", _ROOT / "scripts" / "verify" / "workflow_lint.py")
    violations = lint.lint_file(str(_WORKFLOW))
    assert violations == [], f"workflow_lint violations: {violations}"


def test_verified_config_parses_with_deepseek_locked_sampling():
    cfg = yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8"))
    mk = cfg["model"]["model_kwargs"]
    assert mk["temperature"] == 1.0 and mk["top_p"] == 0.95 and mk["max_tokens"] == 8192
    assert mk["extra_body"]["thinking"]["type"] == "disabled"
    assert cfg["model"]["cost_tracking"] == "ignore_errors"
    assert "model_name" not in cfg["model"], "model must come from --model, never hardcoded"
    assert cfg["environment"]["environment_class"] == "docker"
    assert cfg["environment"]["cwd"] == "/testbed"
    # the brief is prepended by the runner, never templated -> no tag in the
    # actual prompt templates (comments may mention it; templates must not)
    assert "<gt-task-brief" not in cfg["agent"]["instance_template"]
    assert "<gt-task-brief" not in cfg["agent"]["system_template"]


def test_adapter_witness_and_preds_reuse_are_imports_not_ports():
    """ONE-product guard: the brief/witness/fail-closed logic must be imported
    from artifact_deepswe.gt_agent, and the preds plumbing from minisweagent —
    not re-implemented copies."""
    src = _ADAPTER_PATH.read_text(encoding="utf-8")
    assert "from artifact_deepswe import gt_agent" in src
    assert "_emit_gt_meta_witness" in src and "def _emit_gt_meta_witness" not in src
    assert "_prepend_brief" in src and "def _prepend_brief" not in src
    assert "update_preds_file" in src and "def update_preds_file" not in src
