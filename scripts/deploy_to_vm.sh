#!/usr/bin/env bash
# deploy_to_vm.sh — idempotently deploy GT bundle dirs + modified config files
# to a GCP VM (default: gt-t0). Uses scp into /tmp/gt_deploy_stage on the VM,
# then sudo-mv into /home/ubuntu/Groundtruth/, chown ubuntu:ubuntu, chmod +x
# bundle bin/ scripts. Prints a verification table at the end.
#
# Usage:
#   bash scripts/deploy_to_vm.sh [<vm_name>]
#
# Env (optional):
#   GCP_PROJECT  default: project-3d0018fc-54e4-4a4d-97c
#   GCP_ZONE     default: us-central1-a
#   GT_REMOTE_USER default: ubuntu
#   GT_REMOTE_ROOT default: /home/ubuntu/Groundtruth
#
# Idempotent: re-running updates files in place. Never blows away
# swebench_runs/ or other user data.

set -euo pipefail

VM_NAME="${1:-gt-t0}"
GCP_PROJECT="${GCP_PROJECT:-project-3d0018fc-54e4-4a4d-97c}"
GCP_ZONE="${GCP_ZONE:-us-central1-a}"
REMOTE_USER="${GT_REMOTE_USER:-ubuntu}"
REMOTE_ROOT="${GT_REMOTE_ROOT:-/home/ubuntu/Groundtruth}"

LOCAL_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAGE_DIR="/tmp/gt_deploy_stage"

# Bundles to deploy (post-2026-05-06 11→4 consolidation):
#   gt_query, gt_search, gt_navigate, gt_validate
#   gt_edit, gt_pre_finish_gate (kept untouched)
BUNDLES=(
  gt_query
  gt_search
  gt_navigate
  gt_validate
  gt_edit
  gt_pre_finish_gate
)

# Modified files outside the bundle dirs (relative to repo root):
EXTRA_FILES=(
  "config/gt_track4.yaml"
  "scripts/swebench/gt_track4_pre_run.py"
)

# ── Pre-flight on the local side ────────────────────────────────────────────
echo "[deploy] local repo: ${LOCAL_REPO}"
echo "[deploy] target VM:  ${VM_NAME} (project=${GCP_PROJECT}, zone=${GCP_ZONE})"
echo "[deploy] remote root: ${REMOTE_USER}@${VM_NAME}:${REMOTE_ROOT}"

for b in "${BUNDLES[@]}"; do
  if [ ! -d "${LOCAL_REPO}/tools/sweagent/${b}" ]; then
    echo "[deploy][FAIL] missing local bundle: tools/sweagent/${b}" >&2
    exit 2
  fi
done
for f in "${EXTRA_FILES[@]}"; do
  if [ ! -f "${LOCAL_REPO}/${f}" ]; then
    echo "[deploy][FAIL] missing local file: ${f}" >&2
    exit 2
  fi
done

if ! command -v gcloud >/dev/null 2>&1; then
  echo "[deploy][FAIL] gcloud not on PATH" >&2
  exit 3
fi

# ── Helper wrappers around gcloud ──────────────────────────────────────────
gssh() {
  gcloud compute ssh "${REMOTE_USER}@${VM_NAME}" \
    --project="${GCP_PROJECT}" --zone="${GCP_ZONE}" \
    --command="$1"
}

gscp_dir() {
  # gscp_dir <local_dir> <remote_dest_parent>
  local src="$1"
  local dest="$2"
  gcloud compute scp --recurse "${src}" \
    "${REMOTE_USER}@${VM_NAME}:${dest}" \
    --project="${GCP_PROJECT}" --zone="${GCP_ZONE}"
}

gscp_file() {
  # gscp_file <local_file> <remote_dest>
  local src="$1"
  local dest="$2"
  gcloud compute scp "${src}" \
    "${REMOTE_USER}@${VM_NAME}:${dest}" \
    --project="${GCP_PROJECT}" --zone="${GCP_ZONE}"
}

# ── Stage on the VM ────────────────────────────────────────────────────────
echo "[deploy] preparing stage dir on VM: ${STAGE_DIR}"
gssh "rm -rf ${STAGE_DIR} && mkdir -p ${STAGE_DIR}/tools/sweagent ${STAGE_DIR}/config ${STAGE_DIR}/scripts/swebench"

echo "[deploy] uploading bundles"
for b in "${BUNDLES[@]}"; do
  echo "[deploy]   bundle: ${b}"
  gscp_dir "${LOCAL_REPO}/tools/sweagent/${b}" "${STAGE_DIR}/tools/sweagent/"
done

echo "[deploy] uploading config files"
for f in "${EXTRA_FILES[@]}"; do
  echo "[deploy]   file:   ${f}"
  # Make sure the remote parent dir exists, then scp the single file.
  parent="$(dirname "${f}")"
  gssh "mkdir -p ${STAGE_DIR}/${parent}"
  gscp_file "${LOCAL_REPO}/${f}" "${STAGE_DIR}/${f}"
done

# ── Install on the VM ──────────────────────────────────────────────────────
echo "[deploy] installing into ${REMOTE_ROOT}"
INSTALL_SCRIPT=$(cat <<'EOS'
set -euo pipefail
STAGE="$1"
ROOT="$2"
USER_NAME="$3"

mkdir -p "${ROOT}/tools/sweagent" "${ROOT}/config" "${ROOT}/scripts/swebench"

# Install bundle dirs in place. Use `cp -aT` so the destination directory's
# contents become the source's contents (no nested duplication on re-runs).
for d in "${STAGE}/tools/sweagent/"*/; do
  name="$(basename "${d}")"
  dest="${ROOT}/tools/sweagent/${name}"
  rm -rf "${dest}.new"
  cp -a "${d}" "${dest}.new"
  if [ -d "${dest}" ]; then
    rm -rf "${dest}.old" 2>/dev/null || true
    mv "${dest}" "${dest}.old"
  fi
  mv "${dest}.new" "${dest}"
  rm -rf "${dest}.old" 2>/dev/null || true
  if [ -d "${dest}/bin" ]; then
    chmod -R +x "${dest}/bin/" 2>/dev/null || true
  fi
done

# Install extra files at canonical paths.
for f in "${STAGE}/config/"*.yaml; do
  [ -f "${f}" ] || continue
  cp -af "${f}" "${ROOT}/config/$(basename "${f}")"
done
for f in "${STAGE}/scripts/swebench/"*.py; do
  [ -f "${f}" ] || continue
  cp -af "${f}" "${ROOT}/scripts/swebench/$(basename "${f}")"
done

chown -R "${USER_NAME}:${USER_NAME}" \
  "${ROOT}/tools/sweagent" \
  "${ROOT}/config" \
  "${ROOT}/scripts/swebench" 2>/dev/null || true

EOS
)

gssh "sudo bash -c $(printf '%q' "${INSTALL_SCRIPT}") install ${STAGE_DIR} ${REMOTE_ROOT} ${REMOTE_USER}"

# ── Verification table ─────────────────────────────────────────────────────
echo "[deploy] verification table:"
VERIFY_SCRIPT=$(cat <<'EOS'
set -euo pipefail
ROOT="$1"
shift
echo "------------------------------------------------------------------"
printf "%-22s %-10s %-10s %s\n" "bundle" "config" "bin_exec" "sha256(config.yaml)"
echo "------------------------------------------------------------------"
for b in "$@"; do
  d="${ROOT}/tools/sweagent/${b}"
  if [ ! -d "${d}" ]; then
    printf "%-22s %-10s %-10s %s\n" "${b}" "MISSING" "-" "-"
    continue
  fi
  cfg="${d}/config.yaml"
  if [ -f "${cfg}" ]; then
    cfg_state="present"
    sha="$(sha256sum "${cfg}" | awk '{print $1}')"
  else
    cfg_state="MISSING"
    sha="-"
  fi
  bin_state="-"
  if [ -d "${d}/bin" ]; then
    if find "${d}/bin" -maxdepth 1 -type f -not -perm -u+x | grep -q .; then
      bin_state="MISSING_X"
    else
      bin_state="ok"
    fi
  fi
  printf "%-22s %-10s %-10s %s\n" "${b}" "${cfg_state}" "${bin_state}" "${sha}"
done
echo "------------------------------------------------------------------"
echo "Modified files:"
for f in config/gt_track4.yaml scripts/swebench/gt_track4_pre_run.py; do
  full="${ROOT}/${f}"
  if [ -f "${full}" ]; then
    sha="$(sha256sum "${full}" | awk '{print $1}')"
    printf "  %-44s %s\n" "${f}" "${sha}"
  else
    printf "  %-44s %s\n" "${f}" "MISSING"
  fi
done
echo "------------------------------------------------------------------"
EOS
)

BUNDLE_ARGS="${BUNDLES[*]}"
gssh "bash -c $(printf '%q' "${VERIFY_SCRIPT}") verify ${REMOTE_ROOT} ${BUNDLE_ARGS}"

echo "[deploy] done."
