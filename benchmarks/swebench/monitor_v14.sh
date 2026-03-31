#!/bin/bash
# GT v14 Monitor — comprehensive logging + status updates
OUTPUT_DIR=$1
LOG_FILE=$2
RUN_NAME=${3:-v14}

if [ -z "$OUTPUT_DIR" ] || [ -z "$LOG_FILE" ]; then
    echo "Usage: $0 <output_dir> <log_file> [run_name]"
    exit 1
fi

while true; do
    TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')

    # Count completed predictions
    DONE=$(find ${OUTPUT_DIR} -name '*.traj.json' 2>/dev/null | wc -l)
    TOTAL=$(ls ${OUTPUT_DIR}/ 2>/dev/null | grep -v '\.' | wc -l)

    # Count preds.json entries
    PREDS=0
    if [ -f ${OUTPUT_DIR}/preds.json ]; then
        PREDS=$(python3 -c "import json; print(len(json.load(open('${OUTPUT_DIR}/preds.json'))))" 2>/dev/null || echo 0)
    fi

    # Docker containers running
    CONTAINERS=$(docker ps -q 2>/dev/null | wc -l)

    # Check for errors in log
    ERRORS=$(grep -c 'Error\|Exception\|Traceback' ${LOG_FILE} 2>/dev/null || echo 0)

    # GT-specific stats
    GT_LOGS=$(ls ${OUTPUT_DIR}/gt_logs/*.evidence.jsonl 2>/dev/null | wc -l)
    EVIDENCE_EVENTS=0
    if [ $GT_LOGS -gt 0 ]; then
        EVIDENCE_EVENTS=$(cat ${OUTPUT_DIR}/gt_logs/*.evidence.jsonl 2>/dev/null | wc -l)
    fi

    # Briefing stats from log
    BRIEFINGS=$(grep -c 'v12 briefing for\|v14 briefing' ${LOG_FILE} 2>/dev/null || echo 0)

    # Check for GT-specific output in trajectories
    GT_CONSTRAINTS=0
    GT_REMINDERS=0
    if [ $DONE -gt 0 ]; then
        GT_CONSTRAINTS=$(grep -rl 'CONSTRAINTS FOR THIS FUNCTION' ${OUTPUT_DIR}/*//*.traj.json 2>/dev/null | wc -l)
        GT_REMINDERS=$(grep -rl 'REMINDER:' ${OUTPUT_DIR}/*/*.traj.json 2>/dev/null | wc -l)
    fi

    # Hook injection stats
    HOOKS_OK=$(grep -c 'v11 Go indexer' ${LOG_FILE} 2>/dev/null || echo 0)
    HOOKS_FAIL=$(grep -c 'GT injection failed' ${LOG_FILE} 2>/dev/null || echo 0)

    echo "[${TIMESTAMP}] ${RUN_NAME}: ${DONE}/${TOTAL} done | preds=${PREDS} | containers=${CONTAINERS} | briefings=${BRIEFINGS} | evidence=${EVIDENCE_EVENTS} | constraints=${GT_CONSTRAINTS} | reminders=${GT_REMINDERS} | hooks=${HOOKS_OK}ok/${HOOKS_FAIL}fail | errors=${ERRORS}"

    # If all done, print final summary
    if [ $DONE -ge $TOTAL ] && [ $TOTAL -gt 0 ] && [ $CONTAINERS -eq 0 ]; then
        echo ''
        echo '=== FINAL SUMMARY ==='
        echo "Tasks completed: ${DONE}/${TOTAL}"
        echo "Predictions: ${PREDS}"
        echo "Briefings shown: ${BRIEFINGS}"
        echo "Evidence events: ${EVIDENCE_EVENTS}"
        echo "Tasks with constraints: ${GT_CONSTRAINTS}"
        echo "Tasks with reminders: ${GT_REMINDERS}"
        echo "Hook inject ok/fail: ${HOOKS_OK}/${HOOKS_FAIL}"
        echo "Errors in log: ${ERRORS}"

        # Evidence detail from JSONL logs
        if [ $EVIDENCE_EVENTS -gt 0 ]; then
            echo ''
            echo '--- Evidence Breakdown ---'
            python3 << 'PYEOF'
import json, glob, sys

events = []
for f in glob.glob(sys.argv[1] + '/gt_logs/*.evidence.jsonl') if len(sys.argv) > 1 else []:
    for line in open(f):
        try: events.append(json.loads(line))
        except: pass

if not events:
    # fallback: use env
    import os
    od = os.environ.get('_OD', '')
    for f in glob.glob(od + '/gt_logs/*.evidence.jsonl'):
        for line in open(f):
            try: events.append(json.loads(line))
            except: pass

shown = sum(1 for e in events if e.get('post_edit_evidence_shown'))
suppressed = sum(1 for e in events if e.get('post_edit_suppressed'))
families = {}
for e in events:
    for fam in e.get('post_edit_families_shown', []):
        families[fam] = families.get(fam, 0) + 1

print(f'Evidence shown: {shown}, suppressed: {suppressed}')
print(f'Families: {families}')

scores = {}
for e in events:
    for s in e.get('selected', []):
        sc = s.get('score', 0)
        scores[sc] = scores.get(sc, 0) + 1
print(f'Score dist: {scores}')

sf = sum(e.get('v13_admissibility', {}).get('edges_same_file', 0) for e in events)
imp = sum(e.get('v13_admissibility', {}).get('edges_import', 0) for e in events)
nm = sum(e.get('v13_admissibility', {}).get('edges_name_match_rejected', 0) for e in events)
print(f'Edges: same_file={sf}, import={imp}, name_match_rejected={nm}')
PYEOF
        fi

        # Briefing analysis from trajectories
        if [ $DONE -gt 0 ]; then
            echo ''
            echo '--- Briefing Analysis ---'
            _OD=${OUTPUT_DIR} python3 << 'PYEOF'
import json, glob, os

od = os.environ.get('_OD', '')
trajs = glob.glob(od + '/*/*.traj.json')
briefing_shown = 0
briefing_lines_total = 0
for t in trajs:
    try:
        data = json.load(open(t))
        info = data.get('info', {})
        if info.get('briefing_shown'):
            briefing_shown += 1
            briefing_lines_total += info.get('briefing_lines', 0)
    except: pass
avg = briefing_lines_total / max(briefing_shown, 1)
print(f'Briefing: {briefing_shown}/{len(trajs)} tasks, avg {avg:.1f} lines')
PYEOF
        fi

        echo '=== END ==='
        exit 0
    fi

    sleep 60
done
