"""Test that base64 approach correctly passes SQL with single quotes and backslashes."""
import base64
import json
import sqlite3
import tempfile
import os

# Create a test database
db_path = tempfile.mktemp(suffix=".db")
conn = sqlite3.connect(db_path)
conn.execute("""CREATE TABLE nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT NOT NULL,
    name TEXT NOT NULL,
    qualified_name TEXT,
    file_path TEXT NOT NULL,
    start_line INTEGER,
    end_line INTEGER,
    signature TEXT,
    return_type TEXT,
    is_exported BOOLEAN DEFAULT 0,
    is_test BOOLEAN DEFAULT 0,
    language TEXT NOT NULL,
    parent_id INTEGER REFERENCES nodes(id)
)""")
conn.execute("""CREATE TABLE edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL,
    target_id INTEGER NOT NULL,
    type TEXT NOT NULL,
    source_line INTEGER,
    source_file TEXT,
    resolution_method TEXT,
    confidence REAL DEFAULT 0.0,
    metadata TEXT
)""")
# Insert test data
conn.execute("INSERT INTO nodes (label, name, file_path, is_test, language, signature) VALUES ('Function', 'check_typing', 'pylint/extensions/typing.py', 0, 'python', 'self, node')")
conn.execute("INSERT INTO nodes (label, name, file_path, is_test, language, signature) VALUES ('Method', 'visit_return', 'pylint/extensions/typing.py', 0, 'python', 'self, node')")
conn.execute("INSERT INTO nodes (label, name, file_path, is_test, language, signature) VALUES ('Function', 'test_thing', 'tests/test_typing.py', 1, 'python', '')")
conn.commit()
conn.close()

# Test the SQL that the auto-query uses
sql = (
    "SELECT n.name, n.signature FROM nodes n "
    "LEFT JOIN edges e ON e.target_id = n.id AND e.type='CALLS' "
    "WHERE n.file_path LIKE '%pylint/extensions/typing.py' ESCAPE '\\' "
    "AND n.label IN ('Function','Method') AND n.is_test=0 "
    "GROUP BY n.id ORDER BY COUNT(e.id) DESC LIMIT 2"
)
print(f"SQL: {sql}")
print(f"SQL chars around ESCAPE: {[c for c in sql[sql.index('ESCAPE'):sql.index('ESCAPE')+15]]}")

# Direct execution (should work)
conn = sqlite3.connect(db_path)
direct_result = conn.execute(sql).fetchall()
print(f"Direct result: {direct_result}")
conn.close()

# Base64 approach
b64_sql = base64.b64encode(sql.encode()).decode()
b64_params = base64.b64encode(b"[]").decode()
cmd = (
    f'python3 -c "import json,sqlite3,sys,base64;'
    f'c=sqlite3.connect(sys.argv[1]);'
    f'sql=base64.b64decode(sys.argv[2]).decode();'
    f'params=json.loads(base64.b64decode(sys.argv[3]).decode());'
    f'r=c.execute(sql,params).fetchall();'
    f'print(json.dumps(r))"'
    f' {db_path} {b64_sql} {b64_params}'
)
print(f"\nBase64 cmd: {cmd[:100]}...")
import subprocess
result = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)
print(f"Base64 stdout: {result.stdout.strip()}")
print(f"Base64 stderr: {result.stderr.strip()}")

# Old approach (broken)
escaped_sql_old = sql.replace("'", "'\"'\"'")
cmd_old = (
    f'python3 -c "'
    f'import json,sqlite3,sys;'
    f"c=sqlite3.connect('{db_path}');"
    f"r=c.execute('{escaped_sql_old}',json.loads('[]')).fetchall();"
    f'print(json.dumps(r))'
    f'"'
)
print(f"\nOld cmd: {cmd_old[:100]}...")
result_old = subprocess.run(["bash", "-c", cmd_old], capture_output=True, text=True)
print(f"Old stdout: {result_old.stdout.strip()}")
print(f"Old stderr: {result_old.stderr.strip()}")

# Cleanup
os.unlink(db_path)
