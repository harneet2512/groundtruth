#!/bin/bash
# GT v13 Smoke Test — verify import resolution + admissibility gate
# Run this on the GCP VM: bash ~/groundtruth/scripts/swebench/v13_smoke_test.sh
set -e

source ~/gt-venv/bin/activate
cd ~/groundtruth

GT_INDEX=~/groundtruth/gt-index/gt-index-static
GT_INTEL=~/groundtruth/benchmarks/swebench/gt_intel.py
IMAGE="jefzda/sweap-images:ansible.ansible-ansible__ansible-f327e65d11bb905ed9f15996024f857a95592629-vba6da65a0f3baefda7a058ebbd0a8dcafb8512f5"

echo "=== GT v13 SMOKE TEST ==="
echo ""

# 1. Start container
echo "Step 1: Starting container..."
CONTAINER=$(docker run -d $IMAGE sleep 600)
echo "Container: $CONTAINER"

# 2. Copy binary + script
echo "Step 2: Injecting gt-index + gt_intel.py..."
docker cp $GT_INDEX $CONTAINER:/tmp/gt-index
docker cp $GT_INTEL $CONTAINER:/tmp/gt_intel.py
docker exec $CONTAINER chmod +x /tmp/gt-index

# 3. Detect root
ROOT=/app
docker exec $CONTAINER test -d /app || ROOT=/testbed
echo "Root: $ROOT"

# 4. Build index
echo "Step 3: Building index..."
docker exec $CONTAINER /tmp/gt-index --root=$ROOT --output=/tmp/gt_graph.db --max-files=5000 2>&1

# 5. Check resolution breakdown
echo ""
echo "Step 4: Resolution breakdown..."
docker exec $CONTAINER python3 -c "
import sqlite3
conn = sqlite3.connect('/tmp/gt_graph.db')
rows = conn.execute('SELECT resolution_method, COUNT(*) FROM edges WHERE type=\"CALLS\" GROUP BY resolution_method').fetchall()
for r in rows:
    print(f'  {r[0]}: {r[1]}')

import_count = sum(r[1] for r in rows if r[0] == 'import')
same_file_count = sum(r[1] for r in rows if r[0] == 'same_file')
name_match_count = sum(r[1] for r in rows if r[0] == 'name_match')
print()
print(f'  ADMISSIBLE (same_file + import): {same_file_count + import_count}')
print(f'  REJECTED (name_match): {name_match_count}')
print(f'  Import resolution rate: {import_count}/{import_count+name_match_count} cross-file edges = {100*import_count/max(import_count+name_match_count,1):.1f}%')
"

# 6. Find a file with import-resolved callers and test evidence
echo ""
echo "Step 5: Testing gt_intel.py evidence..."
docker exec $CONTAINER python3 -c "
import sqlite3
conn = sqlite3.connect('/tmp/gt_graph.db')
# Find a function with import-resolved callers
row = conn.execute('''
    SELECT n.file_path, n.name FROM nodes n
    JOIN edges e ON e.target_id = n.id
    WHERE e.type = 'CALLS' AND e.resolution_method = 'import'
    GROUP BY n.id
    ORDER BY COUNT(*) DESC
    LIMIT 1
''').fetchone()
if row:
    print(f'Testing file: {row[0]} (function: {row[1]})')
else:
    print('No import-resolved targets found')
" 2>&1

# Get the file to test
TEST_FILE=$(docker exec $CONTAINER python3 -c "
import sqlite3
conn = sqlite3.connect('/tmp/gt_graph.db')
row = conn.execute('''
    SELECT n.file_path FROM nodes n
    JOIN edges e ON e.target_id = n.id
    WHERE e.type = \"CALLS\" AND e.resolution_method = \"import\"
    GROUP BY n.id
    ORDER BY COUNT(*) DESC
    LIMIT 1
''').fetchone()
if row: print(row[0])
" 2>/dev/null)

if [ -n "$TEST_FILE" ]; then
    echo "Running gt_intel.py on: $TEST_FILE"
    docker exec $CONTAINER python3 /tmp/gt_intel.py \
        --db=/tmp/gt_graph.db \
        --file="$TEST_FILE" \
        --root=$ROOT \
        --log=/tmp/ev.jsonl 2>/dev/null

    echo ""
    echo "--- GT Evidence Output ---"
    # Show evidence log
    docker exec $CONTAINER python3 -c "
import json, sys
try:
    with open('/tmp/ev.jsonl') as f:
        for line in f:
            d = json.loads(line.strip())
            adm = d.get('v13_admissibility', {})
            sel = d.get('selected', [])
            cands = d.get('candidates', [])
            print(f'  Candidates: {len(cands)}')
            print(f'  Selected: {len(sel)}')
            print(f'  Output gate passed: {adm.get(\"output_gate_passed\", \"N/A\")}')
            print(f'  edges_import: {adm.get(\"edges_import\", 0)}')
            print(f'  edges_same_file: {adm.get(\"edges_same_file\", 0)}')
            print(f'  edges_name_match_rejected: {adm.get(\"edges_name_match_rejected\", 0)}')
            print(f'  name_match_in_output: {adm.get(\"name_match_in_output\", \"N/A\")}')
            for s in sel:
                print(f'    [{s[\"family\"]}] {s[\"name\"]} (score={s[\"score\"]}): {s.get(\"summary\",\"\")[:60]}')
except FileNotFoundError:
    print('  No evidence log generated (target not found or all suppressed)')
except Exception as e:
    print(f'  Error reading log: {e}')
" 2>&1
fi

# 7. Test briefing
echo ""
echo "Step 6: Testing briefing..."
docker exec $CONTAINER bash -c "echo 'Fix the openssl_certificate module to handle missing key file gracefully' > /tmp/issue.txt"
docker exec $CONTAINER python3 /tmp/gt_intel.py \
    --db=/tmp/gt_graph.db \
    --briefing \
    --issue-text=@/tmp/issue.txt \
    --root=$ROOT 2>/dev/null

# Cleanup
echo ""
echo "Step 7: Cleanup..."
docker stop $CONTAINER >/dev/null 2>&1
docker rm $CONTAINER >/dev/null 2>&1

echo ""
echo "=== SMOKE TEST COMPLETE ==="
