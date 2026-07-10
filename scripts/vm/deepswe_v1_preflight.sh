#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# deepswe_v1_preflight.sh — ON-SURFACE parity gate (box AND GHA)
# Fail-closed. Runs on each surface BEFORE any task. Verifies the surface is a
# faithful DeepSWE v1.0.0 + mimo + GT-ON harness, then emits the RESOLVED
# PARITY_SNAPSHOT_<surface>.json. ANY failing gate -> exit 1 (no task runs).
#
# Usage: deepswe_v1_preflight.sh --surface <upcloud-box|github-actions> \
#          --bench-dir <deepswe-bench> --config <parity.yaml> --out <dir>
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail

SURFACE="unknown"; BENCH="deepswe-bench"; CONFIG=""; OUT="."
while [ $# -gt 0 ]; do case "$1" in
  --surface)   SURFACE="$2"; shift 2 ;;
  --bench-dir) BENCH="$2"; shift 2 ;;
  --config)    CONFIG="$2"; shift 2 ;;
  --out)       OUT="$2"; shift 2 ;;
  *) echo "unknown arg: $1" >&2; exit 2 ;;
esac; done

EXP_TAG="${GT_DEEPSWE_TAG:-v1.0.0}"
EXP_TAG_SHA="${GT_DEEPSWE_TAG_SHA_PREFIX:-c33fa70e}"  # v1.0.0 COMMIT (rev-parse HEAD); 79a508a9 is the annotated tag OBJECT
EXP_PIER="0.2.0"
fail() { echo "PREFLIGHT_FAIL[$SURFACE]: $*" >&2; exit 1; }
ok()   { echo "  [OK] $*"; }

echo "== DeepSWE v1.0.0 parity preflight ($SURFACE) =="

# 1. DeepSWE version -------------------------------------------------------
DESC="$(git -C "$BENCH" describe --tags --exact-match 2>/dev/null || true)"
HEAD="$(git -C "$BENCH" rev-parse HEAD 2>/dev/null || true)"
[ "$DESC" = "$EXP_TAG" ] || fail "deep-swe tag is '$DESC', need $EXP_TAG"
case "$HEAD" in "$EXP_TAG_SHA"*) : ;; *) fail "deep-swe HEAD $HEAD != ${EXP_TAG_SHA}…" ;; esac
ok "deep-swe @ $DESC ($HEAD)"

# 2. DeepSWE task format (v1.0.0 grading path) ----------------------------
[ -d "$BENCH/tasks" ] || fail "no $BENCH/tasks"
# Command substitution (NOT `find | head | grep -q`): under `set -o pipefail`,
# GNU find SIGPIPEs when head closes the pipe early -> a false "no task.toml".
echo "  tasks/: $(find "$BENCH/tasks" -maxdepth 1 -mindepth 1 -type d 2>/dev/null | wc -l) dirs; task.toml=$(find "$BENCH/tasks" -maxdepth 2 -name task.toml 2>/dev/null | wc -l)"
[ -n "$(find "$BENCH/tasks" -maxdepth 2 -name task.toml 2>/dev/null | head -1)" ] || fail "no task.toml"
[ -n "$(find "$BENCH/tasks" -maxdepth 3 -path '*/tests/test.sh' 2>/dev/null | head -1)" ] || fail "no tests/test.sh"
[ -n "$(find "$BENCH/tasks" -maxdepth 3 -path '*/tests/test.patch' 2>/dev/null | head -1)" ] || fail "no tests/test.patch"
ok "task format: task.toml + tests/test.sh + tests/test.patch present"

# 3. Reject v1.1 grader indicators ----------------------------------------
if [ -f "$BENCH/README.md" ]; then
  grep -q "Since v1.1" "$BENCH/README.md" && fail "README says 'Since v1.1' (v1.1 grader)"
  grep -q "separate verifier environment" "$BENCH/README.md" && fail "README references separate verifier (v1.1)"
fi
# v1.1 tasks carry reward.json/ctrf.json verifier configs instead of test.sh grading
if find "$BENCH/tasks" -maxdepth 3 -name 'ctrf.json' | head -1 | grep -q .; then
  fail "found ctrf.json in tasks (v1.1 separate-verifier artifact)"
fi
ok "no v1.1 grader indicators"

# 4. Pier version ---------------------------------------------------------
PIER_VER="$(python3 -m pip show datacurve-pier 2>/dev/null | sed -n 's/^Version: //p' || true)"
[ "$PIER_VER" = "$EXP_PIER" ] || fail "datacurve-pier is '${PIER_VER:-none}', need $EXP_PIER"
ok "datacurve-pier==$PIER_VER"

# 5. Prompt contamination — GT text MUST NOT be in the benchmark prompt ----
if grep -RIlq -e "GroundTruth" -e "GT_" -e "<gt-evidence>" "$BENCH"/tasks/*/instruction.md 2>/dev/null; then
  fail "GroundTruth/GT_/<gt-evidence> found inside a task instruction.md (prompt contaminated)"
fi
ok "no GT text in any instruction.md"

# 6. Config contamination — the parity config MUST NOT override prompt/caps/sampling
if [ -n "$CONFIG" ] && [ -f "$CONFIG" ]; then
  for k in instance_template system_template "step_limit" "cost_limit" temperature top_p max_tokens; do
    if grep -Eq "^[[:space:]]*${k}[[:space:]]*:" "$CONFIG"; then
      fail "parity config sets '$k' — must inherit mini.yaml (no override)"
    fi
  done
  ok "parity config overrides nothing (prompt/step/cost/sampling all inherited)"
fi

# 7. Emit the RESOLVED snapshot (resolved from the live surface) -----------
CONFIG_SHA="$( (sha256sum "$CONFIG" 2>/dev/null || shasum -a256 "$CONFIG" 2>/dev/null) | awk '{print $1}')"
SNAP="$OUT/PARITY_SNAPSHOT_${SURFACE}.resolved.json"
cat > "$SNAP" <<JSON
{
  "experiment": "deepswe-v1-mimo-gt-on",
  "surface": "$SURFACE",
  "resolved_at_runtime": true,
  "deep_swe": {"repo": "datacurve-ai/deep-swe", "tag": "$DESC", "commit": "$HEAD"},
  "pier": {"version": "$PIER_VER", "agent": "mini-swe-agent"},
  "grading": {"version_family": "v1.0.0", "uses_test_sh": true, "uses_test_patch": true, "uses_v1_1_separate_verifier": false},
  "config_overlay_sha256": "${CONFIG_SHA:-unknown}",
  "gt_baseline": "${GT_BASELINE:-0}"
}
JSON
ok "wrote $SNAP"
echo "== PREFLIGHT PASS[$SURFACE] — v1.0.0 + pier 0.2.0 + clean prompt + clean config =="
