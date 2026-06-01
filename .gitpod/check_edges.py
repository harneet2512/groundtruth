#!/usr/bin/env python3
"""Check what edges exist below confidence threshold for each language."""
import sqlite3
import sys
import os

dbs = {
    "python": "/tmp/gt_5lang/python_before_lsp.db",
    "go": "/tmp/gt_5lang/go_before_lsp.db",
    "typescript": "/tmp/gt_5lang/typescript_before_lsp.db",
    "rust": "/tmp/axum_clean.db",
}

for lang, db in dbs.items():
    if not os.path.exists(db):
        print(f"\n{lang}: DB not found at {db}")
        continue

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row

    # What resolve.py queries: confidence < 0.7 (the --min-confidence arg)
    total = conn.execute(
        "SELECT COUNT(*) FROM edges e JOIN nodes src ON e.source_id = src.id "
        "WHERE e.confidence < 0.7 AND e.type = 'CALLS' AND src.language = ?",
        (lang,)
    ).fetchone()[0]

    # Same but confidence < 0.9 (the default)
    total_09 = conn.execute(
        "SELECT COUNT(*) FROM edges e JOIN nodes src ON e.source_id = src.id "
        "WHERE e.confidence < 0.9 AND e.type = 'CALLS' AND src.language = ?",
        (lang,)
    ).fetchone()[0]

    # Without language filter
    total_nolang = conn.execute(
        "SELECT COUNT(*) FROM edges WHERE confidence < 0.9 AND type = 'CALLS'"
    ).fetchone()[0]

    # Sample
    sample = conn.execute(
        "SELECT e.id, e.confidence, e.resolution_method, e.source_file, e.source_line, "
        "       tgt.name as target_name, tgt.file_path as target_file, src.language "
        "FROM edges e "
        "JOIN nodes src ON e.source_id = src.id "
        "JOIN nodes tgt ON e.target_id = tgt.id "
        "WHERE e.confidence < 0.9 AND e.type = 'CALLS' AND src.language = ? "
        "LIMIT 3",
        (lang,)
    ).fetchall()

    conn.close()

    print(f"\n=== {lang} ({db}) ===")
    print(f"  edges < 0.7 conf, lang={lang}: {total}")
    print(f"  edges < 0.9 conf, lang={lang}: {total_09}")
    print(f"  edges < 0.9 conf, no lang filter: {total_nolang}")
    for r in sample:
        print(f"  sample: conf={r['confidence']} method={r['resolution_method']} "
              f"target={r['target_name']} file={r['source_file']}:{r['source_line']}")
