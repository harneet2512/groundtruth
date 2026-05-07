#!/usr/bin/env bash
# RC-13 integration check — VM-portable paths + binary loader probe + version pin.
#
# WHAT IT PROVES
#   1. --vm-profile resolves all three VM-specific paths (venv_python,
#      swe_repo, gt_indexes_root) under one flag.
#   2. The smoke runner refuses to launch when --vm-profile is unset and
#      no --venv-python / --gt-indexes-root override is provided. RC-13
#      removed the silent /home/ubuntu fallback that masked VM cutovers.
#   3. `ldd bin/gt-index` is clean on the build host (or the binary is
#      static, which is the explicit RC-13 (b) goal).
#   4. The SWE-agent version assertion fires when a forced-mismatch venv
#      python is supplied — i.e. the assertion is wired, not a no-op.
#   5. The duplicate-submit assertion rejects a config that loads both
#      tools/registry AND review_on_submit_m.
#
# DO NOT RUN THIS SCRIPT ON A VM AS PART OF AUTOMATED CI.
#   It only does cheap local probes (no SWE-agent run, no LLM call).
#
# USAGE
#   bash RC-13.sh
#
set -euo pipefail

REPO_ROOT="${REPO_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
RUNNER="${REPO_ROOT}/scripts/swebench/swe_agent_smoke_runner.py"
GT_BIN="${REPO_ROOT}/tools/sweagent/gt_edit/bin/gt-index"

echo "[RC-13] runner=${RUNNER}"
echo "[RC-13] gt_bin=${GT_BIN}"

fail=0

# ---- 1. --vm-profile resolves all three paths -----------------------------
# We use --dry-run so this never launches SWE-agent. A successful resolve
# prints the assembled command including the profile-derived venv_python
# and --gt-indexes-root.
echo
echo "[RC-13] (1) --vm-profile test resolves three paths..."
RC13_OUT_DIR="$(mktemp -d -t rc13_test_profile.XXXXXX)"
trap 'rm -rf "${RC13_OUT_DIR}"' EXIT
if python3 "${RUNNER}" \
        --task-ids "django__django-1" \
        --config "${REPO_ROOT}/config/gt_track4.yaml" \
        --output-dir "${RC13_OUT_DIR}" \
        --vm-profile test \
        --skip-preflight \
        --no-litellm-register \
        --dry-run 2>&1 | tee /tmp/rc13_dryrun.txt | grep -q "/home/test/eval_indexes"; then
    echo "[RC-13][OK]   profile=test produced /home/test/eval_indexes in the resolved cmd"
else
    echo "[RC-13][FAIL] profile=test did NOT propagate /home/test/eval_indexes" >&2
    fail=1
fi

# ---- 2. missing --vm-profile + missing flags = FATAL ----------------------
echo
echo "[RC-13] (2) bare runner with no --vm-profile / no overrides must FATAL..."
if python3 "${RUNNER}" \
        --task-ids "django__django-1" \
        --config "${REPO_ROOT}/config/gt_track4.yaml" \
        --output-dir "${RC13_OUT_DIR}" \
        --skip-preflight \
        --no-litellm-register \
        --dry-run 2>&1 | grep -q "RC-13 removed the silent"; then
    echo "[RC-13][OK]   bare invocation refused with RC-13 message"
else
    echo "[RC-13][FAIL] bare invocation should refuse but did not surface RC-13 fatal message" >&2
    fail=1
fi

# ---- 3. ldd probe on bin/gt-index -----------------------------------------
echo
echo "[RC-13] (3) ldd probe on ${GT_BIN}..."
if [[ -f "${GT_BIN}" ]]; then
    if ldd "${GT_BIN}" 2>&1 | grep -q "not a dynamic executable"; then
        echo "[RC-13][OK]   binary is static — portable across libc"
    elif ldd_out=$(ldd "${GT_BIN}" 2>&1) && \
            ! grep -q "not found\|cannot execute\|version \`GLIBC_" <<<"${ldd_out}"; then
        echo "[RC-13][WARN] binary is dynamic but ldd resolves cleanly on this host."
        echo "              It will still fail on containers with older glibc / musl."
        echo "              Run scripts/build_gt_index_linux.sh on a Linux host to fix."
    else
        echo "[RC-13][FAIL] ldd reports unresolved deps:" >&2
        echo "${ldd_out}" >&2
        fail=1
    fi
else
    echo "[RC-13][SKIP] ${GT_BIN} not present on this host — manual rebuild needed"
fi

# ---- 4. SWE-agent version assertion fires on forced mismatch --------------
echo
echo "[RC-13] (4) version assertion fires on a fake mismatch..."
FAKE_VENV_DIR="$(mktemp -d -t rc13_fakevenv.XXXXXX)"
mkdir -p "${FAKE_VENV_DIR}/bin"
cat > "${FAKE_VENV_DIR}/bin/python" <<'PY'
#!/usr/bin/env python3
import sys
# Mimic: `python -c 'import sweagent; print(sweagent.__version__)'`
if "import sweagent" in " ".join(sys.argv):
    print("9.9.9-fake")
PY
chmod +x "${FAKE_VENV_DIR}/bin/python"

if python3 -c "
import importlib.util, sys
spec = importlib.util.spec_from_file_location('runner', r'${RUNNER}')
mod = importlib.util.module_from_spec(spec)
sys.path.insert(0, r'${REPO_ROOT}/scripts/swebench')
spec.loader.exec_module(mod)
ok, detail = mod._assert_sweagent_version(r'${FAKE_VENV_DIR}/bin/python', expected='1.1.0')
print('OK=' + str(ok), 'DETAIL=' + detail)
sys.exit(0 if (not ok and 'sweagent_version_mismatch' in detail) else 1)
"; then
    echo "[RC-13][OK]   version assertion correctly rejected 9.9.9-fake vs expected 1.1.0"
else
    echo "[RC-13][FAIL] version assertion did not fail on forced mismatch" >&2
    fail=1
fi
rm -rf "${FAKE_VENV_DIR}"

# ---- 5. duplicate-submit assertion rejects a duplicate config -------------
echo
echo "[RC-13] (5) duplicate-submit assertion rejects bad bundle list..."
DUP_CFG="$(mktemp -t rc13_dup.XXXXXX.yaml)"
cat > "${DUP_CFG}" <<'YML'
agent:
  tools:
    bundles:
      - path: tools/registry
      - path: tools/review_on_submit_m
      - path: tools/sweagent/gt_pre_finish_gate
YML
if python3 -c "
import importlib.util, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location('runner', r'${RUNNER}')
mod = importlib.util.module_from_spec(spec)
sys.path.insert(0, r'${REPO_ROOT}/scripts/swebench')
spec.loader.exec_module(mod)
ok, detail = mod._assert_no_duplicate_submit(Path(r'${DUP_CFG}'))
print('OK=' + str(ok), 'DETAIL=' + detail)
sys.exit(0 if (not ok and 'duplicate_submit_declaration' in detail) else 1)
"; then
    echo "[RC-13][OK]   duplicate-submit assertion correctly flagged bad bundle list"
else
    echo "[RC-13][FAIL] duplicate-submit assertion did not flag the duplicate config" >&2
    fail=1
fi
rm -f "${DUP_CFG}"

if [[ "${fail}" -ne 0 ]]; then
    echo
    echo "[RC-13] integration check FAILED" >&2
    exit 1
fi
echo
echo "[RC-13] integration check PASSED"
