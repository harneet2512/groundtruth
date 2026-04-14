#!/usr/bin/env bash
# GT tool bundle install script for SWE-agent
# NOTE: This is sourced by SWE-agent after "cd /root/tools/groundtruth"
# so $0 = bash, not install.sh. Use pwd instead.
BUNDLE_DIR="$(pwd)"

# Copy core GT files to /tmp/ (where tool scripts expect them)
cp "$BUNDLE_DIR/bin/gt-index" /tmp/gt-index 2>/dev/null && chmod +x /tmp/gt-index
cp "$BUNDLE_DIR/bin/gt_intel.py" /tmp/gt_intel.py 2>/dev/null
cp "$BUNDLE_DIR/bin/gt_tools.py" /tmp/gt_tools.py 2>/dev/null
cp "$BUNDLE_DIR/bin/_state_gt_v2" /tmp/_state_gt_v2 2>/dev/null && chmod +x /tmp/_state_gt_v2
cp "$BUNDLE_DIR/bin/_gt_budget.sh" /tmp/_gt_budget.sh 2>/dev/null

# Initialize budget file
echo '{"orient":0,"lookup":0,"impact":0,"check":0}' > /tmp/gt_budget.json

# Set GT environment variables
export GT_DB=/tmp/gt_graph.db
export GT_ROOT=/testbed
echo "export GT_DB=/tmp/gt_graph.db" >> /root/.bashrc
echo "export GT_ROOT=/testbed" >> /root/.bashrc

# Build initial index
if [ -f /tmp/gt-index ]; then
    /tmp/gt-index --root=/testbed --output=/tmp/gt_graph.db --max-files=5000 2>/dev/null
    node_count=$(python3 -c "import sqlite3; c=sqlite3.connect('/tmp/gt_graph.db'); print(c.execute('SELECT COUNT(*) FROM nodes').fetchone()[0])" 2>/dev/null || echo '?')
    echo "[GT] Index built: $node_count nodes"
else
    echo '[GT] WARNING: gt-index binary not found at /tmp/gt-index'
    echo "[GT] BUNDLE_DIR=$BUNDLE_DIR"
    ls -la "$BUNDLE_DIR/bin/gt-index" 2>/dev/null || echo "[GT] No gt-index in bundle bin either"
fi

echo '[GT] Install complete'
