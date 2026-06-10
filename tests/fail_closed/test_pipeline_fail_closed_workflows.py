"""P0/P1 pipeline + gates hardening — workflow-level fail-closed contracts.

Covers the 4-reviewer LIPI audit fixes that live in WORKFLOW YAML (not importable code):

  P0.1-a  deepswe_full issue extraction reads instruction.md (pier's Task.instruction
          source) with task.toml fallbacks and FAILS CLOSED (GT_ISSUE_MISSING) on an
          empty issue — the extraction heredoc is extracted from the real workflow and
          EXECUTED against synthetic task dirs (functional red->green, not text-only).
  P0.1-b  `pier run | tee` carries pipefail + ${PIPESTATUS[0]} and surfaces pier's
          swallowed DeepSweAdapterError (exception_message in jobs/) as DEEPSWE_ADAPTER_FAIL.
  P0.1-c  brief.txt is artifact #8 in the workflow's fail-closed artifact check.
  P0.1-d  summarize PARSES the n_agent_steps value and requires >0 (presence of the
          token is not "agent ran").
  P0.2    the OH workflows pin GT_EMBED_MODEL_NAME=intfloat/e5-small-v2 + GT_EMBED_DIM=384
          (only e5 is baked on the OH surface; the gte default + gte-or-raise hardening
          would fail-close every task). The substrate/DeepSWE paths stay gte (NOT pinned).
  P1-f    the deepswe_full PROOF container is strict by default
          (GT_GATES_DELIVER_ALWAYS=0; deliver-always is an explicit opt-in).

No task IDs, no gold, no benchmark logic — pure pipeline contracts, identical for every task.
"""
import os
import subprocess
import sys

import yaml

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
WF_DEEPSWE = os.path.join(ROOT, ".github", "workflows", "deepswe_full.yml")
WF_30 = os.path.join(ROOT, ".github", "workflows", "swebench_30task.yml")
WF_300 = os.path.join(ROOT, ".github", "workflows", "swebench_300task.yml")


def _read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def _load(p):
    return yaml.safe_load(_read(p))


def _step(doc, job, name_prefix):
    for s in doc["jobs"][job]["steps"]:
        if str(s.get("name", "")).startswith(name_prefix):
            return s
    raise AssertionError(f"step {name_prefix!r} not found in job {job!r}")


# ── workflows must stay YAML-parseable ────────────────────────────────────────

def test_workflows_parse_as_yaml():
    for p in (WF_DEEPSWE, WF_30, WF_300):
        doc = _load(p)
        assert isinstance(doc, dict) and "jobs" in doc, p


# ── P0.1-a: issue extraction — functional, from the REAL workflow heredoc ────

def _extract_issue_heredoc():
    """Pull the python heredoc body out of the substrate-proof step's run block."""
    step = _step(_load(WF_DEEPSWE), "trial", "GT substrate proof")
    lines = step["run"].splitlines()
    start = next(i for i, ln in enumerate(lines) if "<< 'PYEOF'" in ln)
    body = []
    for ln in lines[start + 1:]:
        if ln.strip() == "PYEOF":
            break
        body.append(ln)
    assert body, "heredoc body empty — extraction failed"
    return "\n".join(body)


def _run_issue_extraction(tmp_path, task_dir):
    script = tmp_path / "issue_extract.py"
    script.write_text(_extract_issue_heredoc(), encoding="utf-8")
    out_file = tmp_path / "issue.txt"
    env = dict(os.environ, GT_ISSUE_OUT=str(out_file))
    r = subprocess.run([sys.executable, str(script), str(task_dir)],
                       env=env, capture_output=True, text=True, timeout=60)
    return r, out_file


def test_issue_extraction_reads_instruction_md(tmp_path):
    task = tmp_path / "task_a"
    task.mkdir()
    (task / "instruction.md").write_text("Fix the frobnicator overflow in core/x.py\n",
                                         encoding="utf-8")
    (task / "task.toml").write_text('[metadata]\nlanguage = "python"\n', encoding="utf-8")
    r, out_file = _run_issue_extraction(tmp_path, task)
    assert r.returncode == 0, f"stderr={r.stderr!r}"
    assert out_file.read_text(encoding="utf-8") == "Fix the frobnicator overflow in core/x.py"
    assert "instruction.md" in r.stdout


def test_issue_extraction_falls_back_to_task_toml(tmp_path):
    task = tmp_path / "task_b"
    task.mkdir()
    (task / "task.toml").write_text('[task]\nprompt = "Do the thing properly"\n',
                                    encoding="utf-8")
    r, out_file = _run_issue_extraction(tmp_path, task)
    assert r.returncode == 0, f"stderr={r.stderr!r}"
    assert out_file.read_text(encoding="utf-8") == "Do the thing properly"
    assert "task.toml" in r.stdout


def test_issue_extraction_empty_issue_fails_closed(tmp_path):
    # RED->GREEN (P0.1-a): no instruction.md + no task.toml issue/prompt -> the old
    # one-liner wrote an EMPTY /tmp/issue.txt (`|| :` swallow) and the run went on; now
    # it must exit nonzero with the classified GT_ISSUE_MISSING marker.
    task = tmp_path / "task_c"
    task.mkdir()
    (task / "task.toml").write_text('[metadata]\nlanguage = "go"\n', encoding="utf-8")
    r, out_file = _run_issue_extraction(tmp_path, task)
    assert r.returncode != 0, "empty issue must FAIL CLOSED, never run the substrate"
    assert "GT_ISSUE_MISSING" in r.stderr
    assert not out_file.exists()


def test_issue_extraction_whitespace_instruction_fails_closed(tmp_path):
    task = tmp_path / "task_d"
    task.mkdir()
    (task / "instruction.md").write_text("   \n\n", encoding="utf-8")
    (task / "task.toml").write_text("[metadata]\n", encoding="utf-8")
    r, out_file = _run_issue_extraction(tmp_path, task)
    assert r.returncode != 0
    assert "GT_ISSUE_MISSING" in r.stderr


def test_issue_extraction_old_swallow_removed():
    run = _step(_load(WF_DEEPSWE), "trial", "GT substrate proof")["run"]
    assert "|| : > /tmp/issue.txt" not in run  # the silent empty-issue swallow is gone
    assert "GT_ISSUE_MISSING" in run            # the fail-closed marker is emitted


# ── P0.1-b: pipefail + PIPESTATUS + adapter-error surfacing on pier run ──────

def test_pier_run_has_pipefail_and_pipestatus():
    run = _step(_load(WF_DEEPSWE), "trial", "Run GT trial")["run"]
    assert "set -o pipefail" in run
    assert "PIPESTATUS[0]" in run
    assert "PIER_RC" in run


def test_pier_run_surfaces_swallowed_adapter_error():
    run = _step(_load(WF_DEEPSWE), "trial", "Run GT trial")["run"]
    assert "DeepSweAdapterError" in run        # greps pier's jobs/exception_message
    assert "DEEPSWE_ADAPTER_FAIL" in run       # fails with the classified GT marker


# ── P0.1-c: brief.txt is artifact #8 in the workflow check ───────────────────

def test_workflow_artifact_check_includes_brief():
    run = _step(_load(WF_DEEPSWE), "trial", "GT substrate proof")["run"]
    assert "brief.txt" in run
    assert "all 8 GT artifacts present" in run
    assert "all 7 GT artifacts present" not in run


# ── P0.1-d: summarize parses the steps VALUE, not token presence ─────────────

def test_summarize_parses_steps_value_not_presence():
    run = _step(_load(WF_DEEPSWE), "summarize", "Aggregate DeepSWE benchmark results")["run"]
    assert "AGENT_RAN_STEPS=[0-9]+" in run     # numeric VALUE parse
    assert '-gt 0' in run                       # requires steps > 0
    assert 'grep -rqsE "AGENT_RAN_STEPS|n_agent_steps"' not in run  # presence check gone
    assert "launch-fail" in run                 # 0/absent surfaces as launch-fail


# ── P0.2: OH-surface embedder pin (e5) — substrate stays gte ─────────────────

def test_oh_30task_pins_e5():
    env = _load(WF_30).get("env") or {}
    assert env.get("GT_EMBED_MODEL_NAME") == "intfloat/e5-small-v2"
    assert str(env.get("GT_EMBED_DIM")) == "384"


def test_oh_300task_pins_e5():
    env = _load(WF_300).get("env") or {}
    assert env.get("GT_EMBED_MODEL_NAME") == "intfloat/e5-small-v2"
    assert str(env.get("GT_EMBED_DIM")) == "384"


def test_oh_300task_pins_e5_in_transitional_container_execs():
    # The transitional Point-A path copies the HOST models (e5-only) into gtsrc, so the
    # in-container resolve + gates execs must forward the pin explicitly.
    t = _read(WF_300)
    assert t.count("-e GT_EMBED_MODEL_NAME=intfloat/e5-small-v2") >= 2


def test_substrate_paths_stay_gte():
    # The pinned-substrate invocations (gt-run-proof) bake gte and must NOT be pinned to e5.
    for wf in (WF_DEEPSWE, WF_300):
        t = _read(wf)
        i = 0
        while True:
            i = t.find("gt-run-proof --source-root", i)
            if i == -1:
                break
            # the docker-run arg block immediately preceding the entrypoint
            block = t[max(0, i - 1500):i]
            assert "GT_EMBED_MODEL_NAME" not in block, f"substrate run pinned to e5 in {wf}"
            i += 1
    # deepswe_full (the DeepSWE surface) must not define the OH pin at all.
    env = _load(WF_DEEPSWE).get("env") or {}
    assert "GT_EMBED_MODEL_NAME" not in env


# ── P1-f: proof container strict by default (deliver-always = explicit opt-in) ─

def test_deepswe_proof_gates_strict_by_default():
    doc = _load(WF_DEEPSWE)
    assert (doc.get("env") or {}).get("GT_GATES_DELIVER_ALWAYS") == "0"
    run = _step(doc, "trial", "GT substrate proof")["run"]
    assert "${GT_GATES_DELIVER_ALWAYS:-0}" in run
    assert "${GT_GATES_DELIVER_ALWAYS:-1}" not in run


def test_oh_live_agent_workflows_keep_explicit_deliver_always():
    # The OH live-agent surfaces keep their own EXPLICIT "1" — an agent exists to protect.
    env300 = _load(WF_300).get("env") or {}
    assert env300.get("GT_GATES_DELIVER_ALWAYS") == "1"
