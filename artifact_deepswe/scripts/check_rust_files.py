import sqlite3
c = sqlite3.connect(r"D:\Groundtruth\artifact_deepswe\audit_repos\pest-character-class-coalescing\graph_t1t2.db")
print("Files with lib.rs:")
for r in c.execute("SELECT file_path, COUNT(*) as cnt FROM nodes WHERE file_path LIKE '%lib.rs' GROUP BY file_path"):
    print(f"  {r[0]:50s} {r[1]} nodes")
print("\nTop files by node count:")
for r in c.execute("SELECT file_path, COUNT(*) as cnt FROM nodes GROUP BY file_path ORDER BY cnt DESC LIMIT 10"):
    print(f"  {r[0]:50s} {r[1]} nodes")
print("\nProperties count:")
print(f"  {c.execute('SELECT COUNT(*) FROM properties').fetchone()[0]}")
c.close()
