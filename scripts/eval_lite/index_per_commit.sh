#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="${REPO_ROOT:-/data1tb/swe_lite_repos}"
INDEX_ROOT="${INDEX_ROOT:-/data1tb/gt_indexes}"
GT_INDEX_BIN="${GT_INDEX_BIN:-/data1tb/bin/gt-index}"
DATASET_CACHE="${DATASET_CACHE:-/data1tb/swe_lite_dataset}"
PARALLEL="${PARALLEL:-12}"

if [[ ! -x "${GT_INDEX_BIN}" ]]; then
    echo "index_per_commit: GT_INDEX_BIN not executable: ${GT_INDEX_BIN}" >&2
    exit 2
fi
if [[ ! -d "${REPO_ROOT}" ]]; then
    echo "index_per_commit: REPO_ROOT not found: ${REPO_ROOT}" >&2
    exit 2
fi
mkdir -p "${INDEX_ROOT}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PAIRS_TMP="$(mktemp)"
trap 'rm -f "${PAIRS_TMP}"' EXIT

DATASET_CACHE="${DATASET_CACHE}" \
    python3 "${SCRIPT_DIR}/_emit_repo_commit_pairs.py" > "${PAIRS_TMP}"

index_one() {
    local line="$1"
    local repo_safe commit commit_short repo_dir out_dir out_db
    repo_safe="$(printf '%s' "${line}" | cut -f1)"
    commit="$(printf '%s' "${line}" | cut -f2)"
    commit_short="${commit:0:8}"
    repo_dir="${REPO_ROOT}/${repo_safe}"
    out_dir="${INDEX_ROOT}/${repo_safe}@${commit_short}"
    out_db="${out_dir}/graph.db"

    if [[ -f "${out_db}" ]]; then
        echo "[skip] ${repo_safe}@${commit_short} (exists)"
        return 0
    fi
    if [[ ! -d "${repo_dir}/.git" ]]; then
        echo "[skip] ${repo_safe} — no git checkout at ${repo_dir}" >&2
        return 0
    fi

    local current
    current="$(git -C "${repo_dir}" rev-parse HEAD 2>/dev/null || echo '')"
    if [[ "${current}" != "${commit}" ]]; then
        git -C "${repo_dir}" checkout -q "${commit}" \
            || { echo "[fail] checkout ${repo_safe}@${commit_short}" >&2; return 1; }
    fi

    mkdir -p "${out_dir}"
    "${GT_INDEX_BIN}" -root="${repo_dir}" -output="${out_db}" -workers="$(nproc)" \
        || { echo "[fail] gt-index ${repo_safe}@${commit_short}" >&2; return 1; }
    echo "[ok] ${repo_safe}@${commit_short}"
}
export -f index_one
export REPO_ROOT INDEX_ROOT GT_INDEX_BIN

declare -A REPO_LINES=()
while IFS= read -r line; do
    [[ -z "${line}" ]] && continue
    repo_safe="$(printf '%s' "${line}" | cut -f1)"
    REPO_LINES["${repo_safe}"]+="${line}"$'\n'
done < "${PAIRS_TMP}"

REPO_BLOCKS_TMP="$(mktemp -d)"
for repo_safe in "${!REPO_LINES[@]}"; do
    printf '%s' "${REPO_LINES[${repo_safe}]}" > "${REPO_BLOCKS_TMP}/${repo_safe}"
done

drain_one_repo() {
    local block_file="$1"
    while IFS= read -r line; do
        [[ -z "${line}" ]] && continue
        index_one "${line}"
    done < "${block_file}"
}
export -f drain_one_repo

find "${REPO_BLOCKS_TMP}" -maxdepth 1 -mindepth 1 -type f -print0 \
    | xargs -0 -n1 -P "${PARALLEL}" -I{} bash -c 'drain_one_repo "$@"' _ {}

rm -rf "${REPO_BLOCKS_TMP}"
echo "index_per_commit: done"
