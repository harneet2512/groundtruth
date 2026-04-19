#!/bin/bash
# Parametrized telemetry scraper for GT SWE-bench runs on GCP/Vertex.
#
# The in-container GT hook writes gt_hook_telemetry.jsonl to both
# /tmp/.gt/gt_hook_telemetry.jsonl and /tmp/gt_hook_telemetry.jsonl inside
# the sweagent Docker runtime. Host $GT_TELEMETRY_DIR is NOT bind-mounted
# into the container, so the jsonl never reaches the host by itself. This
# scraper side-loops `docker cp` while each task container is alive.
#
# Usage:
#   bash gt_telemetry_scraper.sh <OUTDIR>            # continuous, sleep 10s
#   bash gt_telemetry_scraper.sh <OUTDIR> --once     # single sweep, exit
#
# Mapping: container name → task id (astropy__astropy-NNNNN), destination
# directory = $OUTDIR/<task_id>. Runs only if that destination already
# exists (i.e., only for tasks owned by this OUTDIR).

set -u
OUTDIR="${1:?OUTDIR is required}"
MODE="${2:-loop}"

EXPECTED_ARM=""
case "$OUTDIR" in
  *nolsp*) EXPECTED_ARM="gt-nolsp" ;;
  *lsp*)   EXPECTED_ARM="gt-hybrid" ;;
esac

read_identity() {
  local cid="$1"
  docker exec "$cid" sh -lc 'cat /tmp/gt_identity.env 2>/dev/null || true' 2>/dev/null || true
}

harvest_once() {
  for CID in $(docker ps --format '{{.ID}}' 2>/dev/null); do
    NAME=$(docker inspect --format '{{.Name}}' "$CID" 2>/dev/null | sed 's:^/::')
    # Match the 4 repo prefixes used by the 10-task smoke: astropy / django /
    # sympy / matplotlib. Pattern = <org>__<repo>-<num>, where org==repo for
    # all four repos in smoke10_ds.txt.
    TID=$(echo "$NAME" | grep -oE '(astropy|django|sympy|matplotlib|sklearn|scikit-learn|sphinx|pytest|xarray|pylint|requests)__\1-[0-9]+' | head -1)
    if [ -z "$TID" ]; then
      # Fallback: short name like astropy-12907 → prepend org__.
      SHORT=$(echo "$NAME" | grep -oE '(astropy|django|sympy|matplotlib|sklearn|sphinx|pytest|xarray|pylint|requests)-[0-9]+' | head -1)
      if [ -n "$SHORT" ]; then
        ORG="${SHORT%%-*}"
        [ "$ORG" = "sklearn" ] && ORG="scikit-learn"
        TID="${ORG}__${SHORT}"
      fi
    fi
    [ -z "$TID" ] && continue

    IDENTITY="$(read_identity "$CID")"
    CID_ARM="$(printf '%s\n' "$IDENTITY" | awk -F= '/^GT_ARM=/{print $2; exit}')"
    CID_TELEM_DIR="$(printf '%s\n' "$IDENTITY" | awk -F= '/^GT_TELEMETRY_DIR=/{print $2; exit}')"
    if [ -n "$EXPECTED_ARM" ] && [ -n "$CID_ARM" ] && [ "$CID_ARM" != "$EXPECTED_ARM" ]; then
      continue
    fi
    if [ -n "$CID_TELEM_DIR" ] && [ "${CID_TELEM_DIR#"$OUTDIR"/}" = "$CID_TELEM_DIR" ]; then
      # Telemetry path from the container does not belong to this outdir.
      continue
    fi

    DEST="$OUTDIR/$TID"
    [ -d "$DEST" ] || continue
    # Hook telemetry — hook writes to both paths; whichever exists wins.
    docker cp "$CID:/tmp/.gt/gt_hook_telemetry.jsonl" "$DEST/gt_hook_telemetry.jsonl" 2>/dev/null \
      || docker cp "$CID:/tmp/gt_hook_telemetry.jsonl" "$DEST/gt_hook_telemetry.jsonl" 2>/dev/null \
      || true
    docker cp "$CID:/tmp/.gt/gt_per_task_summary.json" "$DEST/gt_per_task_summary.json" 2>/dev/null \
      || docker cp "$CID:/tmp/gt_per_task_summary.json" "$DEST/gt_per_task_summary.json" 2>/dev/null \
      || true
    docker cp "$CID:/tmp/.gt/gt_budget.state.json" "$DEST/gt_budget.state.json" 2>/dev/null \
      || docker cp "$CID:/tmp/gt_budget.state.json" "$DEST/gt_budget.state.json" 2>/dev/null \
      || true
    docker cp "$CID:/tmp/.gt/gt_budget_events.jsonl" "$DEST/gt_budget_events.jsonl" 2>/dev/null \
      || docker cp "$CID:/tmp/gt_budget_events.jsonl" "$DEST/gt_budget_events.jsonl" 2>/dev/null \
      || true
    docker cp "$CID:/tmp/gt_state_cmd.log" "$DEST/gt_state_cmd.log" 2>/dev/null || true
    # §3A artifacts: index sentinel + briefing meta + briefing text so the
    # canary reporter can mark index.utilized and briefing.utilized.
    docker cp "$CID:/tmp/gt_graph.db.ready" "$DEST/gt_graph.db.ready" 2>/dev/null || true
    docker cp "$CID:/tmp/gt_briefing.meta.json" "$DEST/gt_briefing_meta.json" 2>/dev/null || true
    docker cp "$CID:/tmp/gt_briefing.txt" "$DEST/gt_briefing.txt" 2>/dev/null || true
    docker cp "$CID:/tmp/gt_install.log" "$DEST/gt_install.log" 2>/dev/null || true
  done
}

if [ "$MODE" = "--once" ]; then
  harvest_once
  exit 0
fi

while true; do
  harvest_once
  sleep 10
done
