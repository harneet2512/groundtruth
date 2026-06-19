#!/usr/bin/env bash
# RC-17 — Reproducibility seal integration check.
#
# Do NOT run on the VM. Documents the local checks that prove the seven
# RC-17 contracts hold. Each step is a documented assertion the operator
# can run pre-launch from a fresh checkout. See docs/ultrareview/F_reproducibility.md
# for the underlying findings F-001..F-012.
#
# Steps:
#   1. Build gt-index twice on the same commit; assert byte equality on
#      every column of graph.db except indexed_at/build_time_ms.
#   2. Preflight with intentionally-different SWE-agent version → fails.
#   3. Preflight with stale image cache; runner refuses :latest, fetches
#      by digest from <run_dir>/image_digests.json instead.
#   4. Two `verify_report append` invocations on the same run_dir; the
#      second is rejected as duplicate.
#   5. `--first-n-from-dataset 30` selects the same 30 task_ids on a
#      second invocation.
#   6. `run_env.json` exists in every output dir, only allow-listed vars.
#   7. Model fingerprint deterministic prompt returns identical response
#      across two invocations.

set -euo pipefail

REPO="$(cd "$(dirname "$0")/../../.." && pwd)"
RUN_DIR="${REPO}/.tmp_rc17_check_$(date +%s)"
mkdir -p "${RUN_DIR}"

echo "=== RC-17 integration check (read-only; documents the contract) ==="
echo "Run dir: ${RUN_DIR}"

# ---- Step 1: deterministic-build byte equality ----------------------------
# Build twice on the same commit with GT_INDEX_FIXED_TS pinned; compare
# graph.db rows. Excluded columns: indexed_at, build_time_ms (which is
# now stderr-only per F-004 — the project_meta key was dropped).
echo ""
echo "[Step 1] deterministic-build check"
cat <<'EOS'
  # Run on a Linux host (gt-index build needs CGO; not runnable on Windows).
  # TODO(RC-17-build): execute on gt-t0.
  cd "${REPO}/gt-index"
  ../scripts/swebench/build_gt_index_linux.sh
  GT_INDEX_FIXED_TS=2026-05-06T00:00:00Z ../bin/gt-index-linux \
      -root "${REPO}/tests/fixtures/project_py" \
      -output /tmp/rc17_a.db
  GT_INDEX_FIXED_TS=2026-05-06T00:00:00Z ../bin/gt-index-linux \
      -root "${REPO}/tests/fixtures/project_py" \
      -output /tmp/rc17_b.db
  python3 - <<'PY'
  import sqlite3
  a = sqlite3.connect('/tmp/rc17_a.db')
  b = sqlite3.connect('/tmp/rc17_b.db')
  EXCLUDE = {'build_time_ms'}
  for table in ('nodes', 'edges', 'project_meta', 'file_hashes'):
      ra = a.execute(f'SELECT * FROM {table}').fetchall()
      rb = b.execute(f'SELECT * FROM {table}').fetchall()
      assert ra == rb, f'mismatch in {table}'
  PY
EOS

# ---- Step 2: SWE-agent version pin --------------------------------------
echo ""
echo "[Step 2] SWE-agent version assertion (RC-13 + RC-17 F-006)"
cat <<'EOS'
  # Install a wrong version into a throwaway venv; preflight must fail.
  python3 -m venv /tmp/rc17_bad_venv
  /tmp/rc17_bad_venv/bin/pip install --quiet sweagent==1.0.0
  python3 "${REPO}/scripts/swebench/swe_agent_smoke_runner.py" \
    --config "${REPO}/config/gt_track4.yaml" \
    --first-n-from-dataset 1 \
    --output-dir "${RUN_DIR}/step2" \
    --venv-python /tmp/rc17_bad_venv/bin/python \
    --vm-profile ubuntu_t0 \
    --dry-run 2>&1 | grep -E "sweagent_version_mismatch|sweagent_import_failed" \
    || { echo "FAIL: preflight did not catch version mismatch"; exit 1; }
EOS

# ---- Step 3: digest pinning --------------------------------------------
echo ""
echo "[Step 3] image digest pinning (RC-17 F-002)"
cat <<'EOS'
  # The resolver must refuse a literal :latest in dataset_row.image_name.
  python3 - <<'PY'
  from scripts.swebench.image_name_resolver import resolve_image_name
  try:
      resolve_image_name(
          'kozea__weasyprint-2300',
          {'image_name': 'starryzhang/sweb.eval.x86_64.kozea_1776_weasyprint-2300:latest'},
      )
      raise SystemExit('FAIL: :latest accepted')
  except ValueError:
      print('PASS: :latest refused')
  PY
EOS

# ---- Step 4: verify_report dedup ----------------------------------------
echo ""
echo "[Step 4] verify_report.py append dedup (RC-17 F-009)"
cat <<'EOS'
  # Append a synthetic section twice; second append must be a no-op.
  DOC="${RUN_DIR}/verify_results.md"
  echo '<!-- APPEND_MARKER -->' > "$DOC"
  python3 - "$DOC" <<'PY'
  import sys
  from pathlib import Path
  sys.path.insert(0, "${REPO}/scripts/swebench")
  from verify_report import append_to_log
  doc = Path(sys.argv[1])
  s = "### [PASS] `dup_run_id_abc123`\n- body\n"
  append_to_log(doc, s)
  append_to_log(doc, s)  # second call must be skipped
  txt = doc.read_text()
  count = txt.count("`dup_run_id_abc123`")
  assert count == 1, f"expected 1 entry, got {count}"
  print("PASS: duplicate append refused")
  PY
EOS

# ---- Step 5: --first-n-from-dataset determinism ------------------------
echo ""
echo "[Step 5] --first-n-from-dataset deterministic selection (RC-17 F-010)"
cat <<'EOS'
  # Two invocations write the same selected_task_ids.txt.
  python3 "${REPO}/scripts/swebench/swe_agent_smoke_runner.py" \
    --config "${REPO}/config/gt_track4.yaml" \
    --first-n-from-dataset 30 \
    --output-dir "${RUN_DIR}/step5a" \
    --vm-profile ubuntu_t0 \
    --dry-run >/dev/null
  python3 "${REPO}/scripts/swebench/swe_agent_smoke_runner.py" \
    --config "${REPO}/config/gt_track4.yaml" \
    --first-n-from-dataset 30 \
    --output-dir "${RUN_DIR}/step5b" \
    --vm-profile ubuntu_t0 \
    --dry-run >/dev/null
  diff -q "${RUN_DIR}/step5a/selected_task_ids.txt" \
          "${RUN_DIR}/step5b/selected_task_ids.txt" \
    && echo "PASS: same 30 task_ids both runs" \
    || { echo "FAIL: selection drifted"; exit 1; }
EOS

# ---- Step 6: run_env.json allow-list ------------------------------------
echo ""
echo "[Step 6] run_env.json env allow-list (RC-17 F-005)"
cat <<'EOS'
  # OPENAI_API_KEY is intentionally NOT on the allow-list; setting it in
  # the parent shell must NOT propagate into run_env.json.
  OPENAI_API_KEY=should_not_leak \
  python3 "${REPO}/scripts/swebench/swe_agent_smoke_runner.py" \
    --config "${REPO}/config/gt_track4.yaml" \
    --first-n-from-dataset 1 \
    --output-dir "${RUN_DIR}/step6" \
    --vm-profile ubuntu_t0 \
    --dry-run >/dev/null
  python3 - "${RUN_DIR}/step6/run_env.json" <<'PY'
  # NOTE: dry-run path skips Popen and the persist call. The integration
  # check expects this to be run via a non-dry-run path with --skip-preflight
  # and a stub launcher; see RC-17.sh on the VM. This block documents the
  # contract: OPENAI_API_KEY must NOT appear in run_env.json.
  PY
EOS

# ---- Step 7: model fingerprint determinism ------------------------------
echo ""
echo "[Step 7] model fingerprint determinism (RC-17 F-008)"
cat <<'EOS'
  # Two invocations against a running litellm proxy must return the same
  # response_text for the deterministic FOO prompt. Skipped here (no proxy
  # in this check); execute on the VM with an active proxy.
EOS

echo ""
echo "RC-17 integration check: documented (manual steps 1-7 above)."
