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
_DEEP_METRICS_PATH = _ROOT / "artifact_verified" / "verified_deep_metrics.py"


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


# ═════════════════════════════════════════════════════════════════════════════
# Fix #4: container absolute-path observations resolve to the SAME pillar hit as
# the repo-relative form (/testbed/<repo-rel> strip, not os.path.relpath).
# ═════════════════════════════════════════════════════════════════════════════


def _evidence_for_view(gmp, file_token: str) -> str:
    """Run the wrapped execute() on a `cat <file_token>` view and return the GT
    text appended to the observation."""
    class FakeEnv:
        def execute(self, action, *a, **k):
            return {"output": f"1\tdef funcA(x):", "returncode": 0, "exception_info": ""}

    FakeEnv.execute = gmp._wrap_execute(FakeEnv.execute)
    out = FakeEnv().execute({"command": f"cat {file_token}"})
    return out["output"]


def test_to_repo_rel_strips_container_root(monkeypatch, tmp_path):
    """The unit contract: an absolute /testbed/<x> path maps to the graph's
    repo-relative key <x>; a relative path passes through; a legitimate top-level
    `testbed/` RELATIVE dir is never stripped (only absolute paths are)."""
    _gt_env_clear(monkeypatch)
    gmp = _load("gt_mini_patch_reporel_uut", _PATCH_PATH)
    assert gmp._to_repo_rel("/testbed/django/core/x.py", "/tmp/gt/src") == "django/core/x.py"
    assert gmp._to_repo_rel("/home/user/pkg/y.py", "/tmp/gt/src") == "pkg/y.py"
    assert gmp._to_repo_rel("django/core/x.py", "/tmp/gt/src") == "django/core/x.py"
    # over-strip guard: a RELATIVE path that happens to live under a dir literally
    # named testbed must NOT be touched (only absolute container paths are stripped).
    assert gmp._to_repo_rel("testbed/conf.py", "/tmp/gt/src") == "testbed/conf.py"
    # an absolute host path under the extract root falls to relpath
    assert gmp._to_repo_rel("/tmp/gt/src/pkg/z.py", "/tmp/gt/src") == "pkg/z.py"


def test_abs_testbed_view_resolves_same_pillar_as_relative(monkeypatch, tmp_path):
    """An absolute `/testbed/a.py` observation must produce the SAME deterministic
    [WITNESS] pillar hit as the relative `a.py` form. Before the fix, relpath
    turned /testbed/a.py into ../../testbed/a.py -> matched nothing -> EMPTY."""
    _gt_env_clear(monkeypatch)
    repo_root = tmp_path / "src"
    db = tmp_path / "graph.db"
    _make_graph(db, repo_root)
    root_file = tmp_path / "gt_root.txt"
    root_file.write_text(str(repo_root), encoding="utf-8")
    monkeypatch.setenv("GT_HOST_GRAPH_DB", str(db))
    monkeypatch.setenv("GT_CERT_DIR", str(tmp_path))
    monkeypatch.setenv("GT_ROOT_FILE", str(root_file))

    gmp = _load("gt_mini_patch_absview_uut", _PATCH_PATH)
    # The relative form (baseline correct behaviour).
    rel_out = _evidence_for_view(gmp, "a.py")
    assert "<gt-evidence" in rel_out and "[WITNESS]" in rel_out
    # _seen dedups (kind, rel) across calls; the abs path strips to the SAME rel
    # "a.py" key, so the consensus/evidence would dedup. Reset module state so the
    # abs-path call is judged on its own resolution, not the dedup cache.
    gmp._seen.clear()
    gmp._consensus_fired = False
    abs_out = _evidence_for_view(gmp, "/testbed/a.py")
    assert "<gt-evidence" in abs_out, "abs /testbed path produced NO evidence (relpath bug not fixed)"
    assert "[WITNESS]" in abs_out
    # same repo-relative key on the tag
    assert 'file="a.py"' in abs_out


def test_verified_deep_metrics_emits_8dp_record(tmp_path):
    """Fix #1: the Verified <iid>.traj.json + outcome.json must yield an 8-dp deep
    record (gt_deep_metrics.v2) — the constitution's NOT-done gate. Synthesizes a
    minimal trajectory in the exact DefaultAgent.save shape + a v2 outcome."""
    dm = _load("verified_deep_metrics_uut", _DEEP_METRICS_PATH)
    iid = "astropy__astropy-12907"
    task_dir = tmp_path
    # 1) the trajectory the Verified adapter writes (<iid>.traj.json)
    traj = {
        "instance_id": iid,
        "info": {
            "model_stats": {"instance_cost": 0.0, "api_calls": 3},
            "config": {
                "model": {"model_name": "deepseek/deepseek-v4-flash"},
                "agent": {"step_limit": 250, "cost_limit": 3.0},
            },
            "exit_status": "Submitted",
            "submission": "diff --git a/astropy/x.py b/astropy/x.py\n+fix",
            "gt_baseline": False,
            "gt_wall_seconds": 42.12345678,
        },
        "messages": [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "<gt-task-brief>look at astropy/x.py</gt-task-brief>\ntask"},
            {"role": "assistant", "content": "thought",
             "extra": {"actions": [{"command": "cat astropy/x.py"}],
                       "response": {"usage": {"prompt_tokens": 1000, "completion_tokens": 200,
                                              "total_tokens": 1200},
                                    "_hidden_params": {"response_cost": 0.0}}}},
            {"role": "tool", "content": "1\tcode\n<gt-evidence kind=\"post_view\" file=\"astropy/x.py\">\n[WITNESS] f called by -> y.py:3\n</gt-evidence>"},
            {"role": "assistant", "content": "edit now",
             "extra": {"actions": [{"command": "sed -i 's/a/b/' astropy/x.py"}]}},
            {"role": "exit", "content": "done", "extra": {"exit_status": "Submitted", "submission": "diff --git a/astropy/x.py b/x"}},
        ],
        "trajectory_format": "mini-swe-agent-1.1",
    }
    (task_dir / iid).mkdir()
    (task_dir / iid / f"{iid}.traj.json").write_text(json.dumps(traj), encoding="utf-8")
    # 2) the official-eval outcome (v2, graded RESOLVED)
    (task_dir / "results").mkdir()
    (task_dir / "results" / "outcome.json").write_text(json.dumps({
        "schema": "gt.verified_outcome.v2", "instance_id": iid,
        "resolved": True, "classification": "RESOLVED",
        "eval_no_report": False, "had_predictions": True,
    }), encoding="utf-8")
    # 3) substrate brief (gt_sent_tokens source)
    (task_dir / "gt_artifacts").mkdir()
    (task_dir / "gt_artifacts" / "brief.txt").write_text("<gt-task-brief>x</gt-task-brief>" * 10, encoding="utf-8")

    out_path = task_dir / f"gt_deep_metrics_{iid}.json"
    rc = dm.main([iid, str(task_dir), "--out", str(out_path)])
    assert rc == 0
    rec = json.loads(out_path.read_text(encoding="utf-8"))

    assert rec["schema"] == "gt_deep_metrics.v2"
    assert rec["precision_decimals"] == 8
    assert rec["task_id"] == iid
    assert rec["resolved"] is True
    assert rec["outcome"] == "resolved"
    assert rec["official_eval"]["in_resolved_denominator"] is True
    # agent behavior recovered from the traj
    assert rec["agent"]["action_count"] == 3.0
    assert rec["agent"]["edits"] >= 1.0
    assert "astropy/x.py" in rec["agent"]["edited_files"]
    # tokens from per-call usage; gt_sent_tokens from the brief
    assert rec["efficiency"]["llm_tokens_in"] == 1000.0
    assert rec["efficiency"]["gt_sent_tokens"] > 0
    # GT delivery counted from the agent's OBSERVATION content
    assert rec["gt_delivery"]["evidence_delivered"] >= 1.0
    # 8-dp wall-clock + trajectory sha + model params pinned
    assert rec["wall_clock_s"] == 42.12345678
    assert rec["trajectory_sha256"]
    assert rec["model"]["params"] and rec["model"]["params"].get("temperature") == 1.0
    # the .md companion is written too
    assert (task_dir / f"gt_deep_metrics_{iid}.md").exists()


def test_verified_deep_metrics_infra_excluded_from_denominator(tmp_path):
    """An EVAL_NO_REPORT (INFRA) outcome must NOT be counted resolved and must be
    excluded from the resolved denominator (workflow fix #2 mirrored in the record)."""
    dm = _load("verified_deep_metrics_infra_uut", _DEEP_METRICS_PATH)
    iid = "django__django-11099"
    (tmp_path / iid).mkdir()
    (tmp_path / iid / f"{iid}.traj.json").write_text(json.dumps({
        "instance_id": iid,
        "info": {"model_stats": {"api_calls": 5, "instance_cost": 0.0},
                 "config": {"model": {"model_name": "deepseek/deepseek-v4-flash"},
                            "agent": {"step_limit": 250}},
                 "exit_status": "Submitted",
                 "submission": "diff --git a/x b/x", "gt_baseline": False,
                 "gt_wall_seconds": 1.0},
        "messages": [{"role": "assistant", "content": "x", "extra": {"actions": []}}],
    }), encoding="utf-8")
    (tmp_path / "results").mkdir()
    (tmp_path / "results" / "outcome.json").write_text(json.dumps({
        "schema": "gt.verified_outcome.v2", "instance_id": iid,
        "resolved": None, "classification": "INFRA",
        "eval_no_report": True, "had_predictions": True,
    }), encoding="utf-8")
    out_path = tmp_path / f"gt_deep_metrics_{iid}.json"
    assert dm.main([iid, str(tmp_path), "--out", str(out_path)]) == 0
    rec = json.loads(out_path.read_text(encoding="utf-8"))
    assert rec["resolved"] is None
    assert rec["outcome"] == "infra_failed_agent_not_started"
    assert rec["official_eval"]["in_resolved_denominator"] is False


def test_verified_deep_metrics_always_writes_on_missing_inputs(tmp_path):
    """Constitution: ALWAYS WRITE. A bare task dir (no traj, no outcome, no certs)
    still yields a record carrying outcome/failure attribution + inputs_present."""
    dm = _load("verified_deep_metrics_empty_uut", _DEEP_METRICS_PATH)
    iid = "nonexistent__task-1"
    out_path = tmp_path / f"gt_deep_metrics_{iid}.json"
    assert dm.main([iid, str(tmp_path), "--out", str(out_path)]) == 0
    rec = json.loads(out_path.read_text(encoding="utf-8"))
    assert rec["task_id"] == iid
    assert rec["schema"] == "gt_deep_metrics.v2"
    assert rec["inputs_present"]["trajectory"] is False
    assert rec["outcome"] == "infra_failed_agent_not_started"


def test_workflow_runs_deep_metrics_and_caps_matrix_fatal():
    """The workflow must (a) run verified_deep_metrics.py if:always, and (b) FAIL
    (not truncate) when the selected task count exceeds the 256 GHA matrix cap."""
    text = _WORKFLOW.read_text(encoding="utf-8")
    assert "verified_deep_metrics.py" in text
    # 256 truncation removed -> FATAL sys.exit(1), never silent tasks[:256]
    assert "tasks[:256]" not in text
    assert "exceed the 256 GHA matrix cap" in text


def test_adapter_witness_and_preds_reuse_are_imports_not_ports():
    """ONE-product guard: the brief/witness/fail-closed logic must be imported
    from artifact_deepswe.gt_agent, and the preds plumbing from minisweagent —
    not re-implemented copies."""
    src = _ADAPTER_PATH.read_text(encoding="utf-8")
    assert "from artifact_deepswe import gt_agent" in src
    assert "_emit_gt_meta_witness" in src and "def _emit_gt_meta_witness" not in src
    assert "_prepend_brief" in src and "def _prepend_brief" not in src
    assert "update_preds_file" in src and "def update_preds_file" not in src
