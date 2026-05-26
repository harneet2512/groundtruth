#!/usr/bin/env python3
"""Diagnose why TypeScript import resolution is so weak (12.6% deterministic)."""
import sqlite3
import sys

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DB = r"D:\Groundtruth\.tmp_holdout\bugs\hono-4876\graph.db"
conn = sqlite3.connect(DB)

print("=== HONO (TypeScript) EDGE DIAGNOSIS ===\n")

# 1. Total breakdown
print("1. EDGE BREAKDOWN BY RESOLUTION METHOD")
for row in conn.execute(
    "SELECT resolution_method, COUNT(*), ROUND(AVG(confidence),2) "
    "FROM edges GROUP BY resolution_method ORDER BY COUNT(*) DESC"
):
    pct = row[1] * 100 / conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    print(f"   {row[0] or 'NULL':15s}  {row[1]:5d} edges ({pct:5.1f}%)  avg_conf={row[2]}")

# 2. Sample import edges
print("\n2. SAMPLE IMPORT EDGES (all 44)")
for row in conn.execute("""
    SELECT e.source_file, n2.file_path, n1.name, n2.name, e.confidence
    FROM edges e
    JOIN nodes n1 ON e.source_id = n1.id
    JOIN nodes n2 ON e.target_id = n2.id
    WHERE e.resolution_method = 'import'
    ORDER BY e.source_file
    LIMIT 20
"""):
    src = row[0].split("/")[-1] if row[0] else "?"
    tgt = row[1].split("/")[-1] if row[1] else "?"
    print(f"   {src:30s} -> {tgt:30s}  {row[2]:25s} -> {row[3]:25s}")

# 3. Name match confidence distribution
print("\n3. NAME_MATCH CONFIDENCE DISTRIBUTION")
for row in conn.execute("""
    SELECT confidence, COUNT(*) FROM edges
    WHERE resolution_method = 'name_match'
    GROUP BY confidence ORDER BY confidence DESC
"""):
    print(f"   conf={row[0]:.1f}  count={row[1]:5d}")

# 4. Node labels
print("\n4. NODE LABELS")
for row in conn.execute("SELECT label, COUNT(*) FROM nodes GROUP BY label ORDER BY COUNT(*) DESC"):
    print(f"   {row[0]:20s}  {row[1]:5d}")

# 5. Files
total_files = conn.execute("SELECT COUNT(DISTINCT file_path) FROM nodes").fetchone()[0]
print(f"\n5. UNIQUE SOURCE FILES: {total_files}")

# 6. Most common name_match callee names (ambiguity check)
print("\n6. MOST AMBIGUOUS NAMES (name_match targets with most edges)")
for row in conn.execute("""
    SELECT n2.name, COUNT(*) as cnt, COUNT(DISTINCT e.source_file) as src_files
    FROM edges e
    JOIN nodes n2 ON e.target_id = n2.id
    WHERE e.resolution_method = 'name_match'
    GROUP BY n2.name
    ORDER BY cnt DESC
    LIMIT 15
"""):
    print(f"   {row[0]:30s}  {row[1]:4d} edges from {row[2]:3d} files")

# 7. Check: how many functions have ZERO import-verified callers?
print("\n7. FUNCTIONS WITH ZERO IMPORT-VERIFIED CALLERS")
row = conn.execute("""
    SELECT COUNT(*) FROM nodes n
    WHERE n.label IN ('Function', 'Method')
    AND NOT EXISTS (
        SELECT 1 FROM edges e
        WHERE e.target_id = n.id AND e.resolution_method = 'import'
    )
""").fetchone()
total_fns = conn.execute("SELECT COUNT(*) FROM nodes WHERE label IN ('Function', 'Method')").fetchone()[0]
print(f"   {row[0]}/{total_fns} functions ({row[0]*100/total_fns:.0f}%) have NO import-verified callers")

# 8. Compare: what does Python look like?
print("\n\n=== COMPARISON: MARIMO (Python) ===\n")
DB2 = r"D:\Groundtruth\.tmp_holdout\bugs\marimo-9408\graph.db"
conn2 = sqlite3.connect(DB2)

print("8. PYTHON EDGE BREAKDOWN")
for row in conn2.execute(
    "SELECT resolution_method, COUNT(*), ROUND(AVG(confidence),2) "
    "FROM edges GROUP BY resolution_method ORDER BY COUNT(*) DESC"
):
    pct = row[1] * 100 / conn2.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    print(f"   {row[0] or 'NULL':15s}  {row[1]:5d} edges ({pct:5.1f}%)  avg_conf={row[2]}")

py_fns = conn2.execute("SELECT COUNT(*) FROM nodes WHERE label IN ('Function', 'Method')").fetchone()[0]
py_no_import = conn2.execute("""
    SELECT COUNT(*) FROM nodes n
    WHERE n.label IN ('Function', 'Method')
    AND NOT EXISTS (
        SELECT 1 FROM edges e WHERE e.target_id = n.id AND e.resolution_method = 'import'
    )
""").fetchone()[0]
print(f"\n   Functions without import-verified callers: {py_no_import}/{py_fns} ({py_no_import*100/py_fns:.0f}%)")

# 9. Key question: what imports does gt-index ACTUALLY extract for TS?
print("\n\n=== ROOT CAUSE ANALYSIS ===\n")
print("9. IMPORT EDGES: SOURCE FILES (which TS files have resolved imports?)")
for row in conn.execute("""
    SELECT source_file, COUNT(*) as cnt
    FROM edges WHERE resolution_method = 'import'
    GROUP BY source_file ORDER BY cnt DESC
    LIMIT 10
"""):
    print(f"   {row[0]:50s}  {row[1]:3d} import edges")

# 10. What resolution methods does each file have?
print("\n10. EXAMPLE FILE: src/context.ts (the top brief candidate)")
for row in conn.execute("""
    SELECT e.resolution_method, COUNT(*), GROUP_CONCAT(DISTINCT n2.name)
    FROM edges e
    JOIN nodes n2 ON e.target_id = n2.id
    WHERE e.source_file = 'src/context.ts'
    GROUP BY e.resolution_method
"""):
    names = row[2][:80] if row[2] else ""
    print(f"   {row[0]:15s}  {row[1]:3d} edges  targets: {names}")

print("\n11. EXAMPLE FILE: src/hono.ts")
for row in conn.execute("""
    SELECT e.resolution_method, COUNT(*), GROUP_CONCAT(DISTINCT n2.name)
    FROM edges e
    JOIN nodes n2 ON e.target_id = n2.id
    WHERE e.source_file = 'src/hono.ts'
    GROUP BY e.resolution_method
"""):
    names = row[2][:80] if row[2] else ""
    print(f"   {row[0]:15s}  {row[1]:3d} edges  targets: {names}")

conn.close()
conn2.close()
