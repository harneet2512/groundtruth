#!/usr/bin/env python3
"""Show first N edges that resolve.py would send to gopls."""
import sqlite3, os
c = sqlite3.connect("/tmp/gt_5lang/go_before_lsp.db")
c.row_factory = sqlite3.Row
rows = c.execute("""
    SELECT e.source_file, e.source_line, tgt.name as target_name,
           e.confidence, e.resolution_method
    FROM edges e
    JOIN nodes src ON e.source_id = src.id
    JOIN nodes tgt ON e.target_id = tgt.id
    WHERE e.confidence < 0.9 AND e.type = 'CALLS' AND src.language = 'go'
    ORDER BY e.confidence ASC
    LIMIT 10
""").fetchall()
root = "/tmp/gt_5lang/crossplane"
for r in rows:
    sf = r["source_file"]
    sl = r["source_line"]
    tn = r["target_name"]
    filepath = os.path.join(root, sf)
    line_text = ""
    col = -1
    if os.path.exists(filepath):
        with open(filepath) as f:
            lines = f.readlines()
        if 0 < sl <= len(lines):
            line_text = lines[sl - 1].rstrip()
            col = line_text.find(tn)
    print(f"{sf}:{sl} -> '{tn}' conf={r['confidence']} col={col}")
    print(f"  line: {line_text[:100]}")
    print()
