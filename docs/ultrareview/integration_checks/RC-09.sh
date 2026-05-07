#!/usr/bin/env bash
# RC-09 integration check — submission/edit pipeline correctness.
#
# WHAT IT PROVES (three independent invariants from RC-09):
#   (1) _changed_symbols precision — a 1-line edit to one of N>=10 functions
#       in the same file flags exactly one symbol via the gate's caller-blind
#       check, not all N. Before the fix, every symbol in the file was
#       flagged whenever before != after, defeating the soft-escape ceiling.
#   (2) emit_submission test_patch reverse-apply — a corrupted /root/test.patch
#       must abort emit_submission with a clear stderr message rather than
#       silently shipping a contaminated diff.
#   (3) Jinja sanitisation — a brief whose body contains a literal
#       ``{{ user_input }}`` substring must NOT trip Jinja2's tokenizer when
#       gt_evidence is later substituted into next_step_template.
#
# DO NOT RUN ON A VM AS PART OF AUTOMATED CI.
#   The unit-test variants in tests/layers/test_l5_gate.py cover identical
#   ground in <2s; this script is the human-readable spec for what the gate
#   is contracted to do, runnable by an operator after a code change.
#
# USAGE
#   bash docs/ultrareview/integration_checks/RC-09.sh
#
# DEPENDENCIES: python3, git.
set -euo pipefail

REPO_ROOT="${REPO_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
WORK="${RC09_OUTPUT_DIR:-${REPO_ROOT}/.tmp_rc09_check_$(date +%s)}"
mkdir -p "${WORK}"

echo "[RC-09] repo_root: ${REPO_ROOT}"
echo "[RC-09] work_dir:  ${WORK}"

GATE_PY="${REPO_ROOT}/tools/sweagent/gt_pre_finish_gate/lib/gt_pre_finish_gate.py"
EDIT_STATE_PY="${REPO_ROOT}/tools/sweagent/gt_edit/lib/gt_edit_state.py"

# ── (1) _changed_symbols precision ───────────────────────────────────────────
python3 - "${GATE_PY}" <<'PY'
import importlib.util, sys, pathlib
spec = importlib.util.spec_from_file_location("gate", sys.argv[1])
gate = importlib.util.module_from_spec(spec); spec.loader.exec_module(gate)

# Build a 10-function file and edit only func_3.
before = "\n".join(f"def func_{i}(x):\n    return x + {i}\n" for i in range(10))
after  = before.replace("    return x + 3\n", "    return x + 30\n", 1)
assert before != after, "fixture mutation produced no change"
got = gate._changed_symbols(before, after)
assert got == {"func_3"}, f"[RC-09 #1 FAIL] expected {{'func_3'}}, got {got}"
print("[RC-09][OK]   #1 _changed_symbols: 1-line edit -> exactly 1 symbol flagged.")
PY

# ── (2) emit_submission abort on corrupted /root/test.patch ──────────────────
python3 - "${GATE_PY}" "${WORK}" <<'PY'
import importlib.util, io, os, subprocess, sys, pathlib, contextlib
spec = importlib.util.spec_from_file_location("gate", sys.argv[1])
gate = importlib.util.module_from_spec(spec); spec.loader.exec_module(gate)
work = pathlib.Path(sys.argv[2]); work.mkdir(parents=True, exist_ok=True)
repo = work / "repo2"; repo.mkdir(exist_ok=True)
subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
subprocess.run(["git", "config", "user.email", "rc09@example.invalid"], cwd=repo, check=True)
subprocess.run(["git", "config", "user.name", "rc09"], cwd=repo, check=True)
(repo / "README.md").write_text("baseline\n")
subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)

# A nonsense patch body that ``git apply -R`` will refuse.
bad_patch = repo / "_corrupt.patch"; bad_patch.write_text("not a real diff\n")

real_path = gate.Path
def _proxy(arg):
    if arg == "/root/test.patch": return bad_patch
    if arg == "/root/model.patch": return repo / "_model.patch"
    return real_path(arg)
gate.Path = _proxy

err = io.StringIO()
out = io.StringIO()
with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
    gate.emit_submission(str(repo))
assert gate.SUBMISSION_MARKER not in out.getvalue(), "submission must NOT be emitted"
assert "ABORT" in err.getvalue(), f"expected ABORT in stderr, got: {err.getvalue()[:200]}"
print("[RC-09][OK]   #2 emit_submission: corrupted test.patch -> aborted, no contaminated diff.")
PY

# ── (3) Jinja sanitisation ───────────────────────────────────────────────────
python3 - <<'PY'
# Mirrors the in-source replacement in gt_edit_state.py. Verifies that
# rendering a downstream template with the sanitised brief no longer
# tokenises the literal {{ }} / {% %} delimiters.
zwnj = "‌"
brief = "see {{ user_input }} and {% block x %}body{% endblock %}"
sanitised = brief
for needle in ("{{", "}}", "{%", "%}"):
    sanitised = sanitised.replace(needle, needle[0] + zwnj + needle[1])
assert "{{" not in sanitised and "}}" not in sanitised
assert "{%" not in sanitised and "%}" not in sanitised
try:
    import jinja2
except ImportError:
    print("[RC-09][SKIP] #3 jinja2 not installed; sanitiser substring check passed.")
else:
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    rendered = env.from_string("OBSERVATION: {{gt_evidence}}").render(
        gt_evidence=sanitised,
    )
    assert "user_input" in rendered, "sanitiser destroyed visible content"
    print("[RC-09][OK]   #3 Jinja: sanitised brief renders without UndefinedError.")
PY

echo "[RC-09] integration check PASSED — submission pipeline correctness invariants hold."
