#!/usr/bin/env python3
"""Diagnose import resolution gaps for all 5 Tier 1 languages."""
import sqlite3, sys, os
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HOLDOUT = r"D:\Groundtruth\.tmp_holdout\bugs"
GT_SELF = r"D:\Groundtruth\gt-index\graph.db"

DBS = {
    "hono-4876 (TS)":       os.path.join(HOLDOUT, "hono-4876", "graph.db"),
    "crossplane-7332 (Go)": os.path.join(HOLDOUT, "crossplane-7332", "graph.db"),
    "marimo-9408 (Py)":     os.path.join(HOLDOUT, "marimo-9408", "graph.db"),
    "axum-3722 (Rust)":     os.path.join(HOLDOUT, "axum-3722", "graph.db"),
    "gt-self (Go-small)":   GT_SELF,
}

for label, db_path in DBS.items():
    if not os.path.exists(db_path):
        continue
    conn = sqlite3.connect(db_path)
    total = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    if total == 0:
        conn.close()
        continue

    print(f"\n{'='*75}")
    print(f"  {label}  ({total} edges)")
    print(f"{'='*75}")

    # Resolution breakdown
    for r in conn.execute(
        "SELECT resolution_method, COUNT(*), ROUND(COUNT(*)*100.0/(SELECT COUNT(*) FROM edges),1) "
        "FROM edges GROUP BY resolution_method ORDER BY COUNT(*) DESC"
    ):
        print(f"  {r[0] or 'NULL':15s}  {r[1]:6d}  ({r[2]:5.1f}%)")

    # name_match confidence breakdown
    print(f"\n  name_match by confidence:")
    nm_by_conf = {}
    for r in conn.execute(
        "SELECT confidence, COUNT(*) FROM edges WHERE resolution_method='name_match' GROUP BY confidence ORDER BY confidence DESC"
    ):
        nm_by_conf[r[0]] = r[1]
        print(f"    conf={r[0]:.1f}  {r[1]:5d}")

    # The key question: if we COULD import-verify all conf=0.9 name_match,
    # what would deterministic % become?
    same_file = conn.execute("SELECT COUNT(*) FROM edges WHERE resolution_method='same_file'").fetchone()[0]
    import_v = conn.execute("SELECT COUNT(*) FROM edges WHERE resolution_method='import'").fetchone()[0]
    nm_09 = nm_by_conf.get(0.9, 0)
    nm_06 = nm_by_conf.get(0.6, 0)

    current_det = same_file + import_v
    ceiling_1 = current_det + nm_09  # promote unambiguous name_match
    ceiling_2 = ceiling_1 + nm_06    # also promote 2-candidate

    print(f"\n  DETERMINISTIC CEILINGS:")
    print(f"    Current:                        {current_det:5d} / {total}  = {current_det*100/total:5.1f}%")
    print(f"    + promote conf=0.9 name_match:  {ceiling_1:5d} / {total}  = {ceiling_1*100/total:5.1f}%")
    print(f"    + promote conf=0.6 name_match:  {ceiling_2:5d} / {total}  = {ceiling_2*100/total:5.1f}%")

    # Top ambiguous names (the noise floor)
    print(f"\n  TOP 10 AMBIGUOUS CALLEE NAMES (conf<0.9, name_match):")
    for r in conn.execute("""
        SELECT n2.name, COUNT(*) as cnt, e.confidence,
               COUNT(DISTINCT n2.file_path) as def_files,
               COUNT(DISTINCT e.source_file) as caller_files
        FROM edges e
        JOIN nodes n2 ON e.target_id = n2.id
        WHERE e.resolution_method = 'name_match' AND e.confidence < 0.9
        GROUP BY n2.name
        ORDER BY cnt DESC LIMIT 10
    """):
        print(f"    {r[0]:30s}  {r[1]:4d} edges  conf={r[2]:.1f}  defined_in={r[3]} files  called_from={r[4]} files")

    # What are import edges actually connecting?
    print(f"\n  IMPORT EDGE PATTERN (what gets import-verified?):")
    for r in conn.execute("""
        SELECT
            CASE
                WHEN n1.is_test = 1 THEN 'test->prod'
                WHEN n2.is_test = 1 THEN 'prod->test'
                ELSE 'prod->prod'
            END as pattern,
            COUNT(*)
        FROM edges e
        JOIN nodes n1 ON e.source_id = n1.id
        JOIN nodes n2 ON e.target_id = n2.id
        WHERE e.resolution_method = 'import'
        GROUP BY pattern
    """):
        print(f"    {r[0]:15s}  {r[1]:5d}")

    # How many cross-file calls exist vs same-file?
    cross_file = conn.execute("""
        SELECT COUNT(*) FROM edges e
        JOIN nodes n1 ON e.source_id = n1.id
        JOIN nodes n2 ON e.target_id = n2.id
        WHERE n1.file_path != n2.file_path
    """).fetchone()[0]
    same_file_calls = total - cross_file
    print(f"\n  CALL TOPOLOGY:")
    print(f"    Same-file calls:  {same_file_calls:5d} ({same_file_calls*100/total:.1f}%)")
    print(f"    Cross-file calls: {cross_file:5d} ({cross_file*100/total:.1f}%)")
    if cross_file > 0:
        import_of_cross = import_v
        print(f"    Cross-file import-verified: {import_of_cross:5d} / {cross_file} = {import_of_cross*100/cross_file:.1f}%")

    conn.close()

# JavaScript - check if we have any JS graph
print(f"\n\n{'='*75}")
print("  NOTE: JavaScript (5 DeepSWE tasks) — no holdout graph.db available.")
print("  JS shares the extractJSTSImports() extractor with TS.")
print("  Additional JS-specific gap: CommonJS require() not extracted.")
print(f"{'='*75}")
