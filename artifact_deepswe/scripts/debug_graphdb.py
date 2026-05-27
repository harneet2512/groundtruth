import sqlite3
for repo in ["expr-try-catch-errors", "testem-bail-on-test-failure", "pest-character-class-coalescing"]:
    db = f"D:/Groundtruth/artifact_deepswe/audit_repos/{repo}/graph.db"
    c = sqlite3.connect(db)
    nodes = c.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    edges = c.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    try:
        props = c.execute("SELECT COUNT(*) FROM properties").fetchone()[0]
    except:
        props = "NO TABLE"
    print(f"{repo}: nodes={nodes} edges={edges} properties={props}")
    for r in c.execute("SELECT DISTINCT file_path FROM nodes LIMIT 3"):
        print(f"  file: {r[0]}")
    c.close()
