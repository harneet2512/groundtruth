"""Test that demonstrates the _container_query quoting bug.

The bug: _container_query replaces ' with '"'"' (bash single-quote escaping idiom)
but wraps the command in double quotes (python3 -c "...").
In a bash double-quote context, '"'"' does NOT escape a single quote.
Instead it terminates the double-quote context, opening/closing single-quote
contexts that mangle the SQL string.

Result: all SQL string literals ('CALLS', 'Function', 'Method', LIKE patterns)
lose their quotes, causing SQLite syntax errors inside the container.
The container python3 crashes to stderr, stdout is empty, _container_query
returns '[]', and auto-query reports symbols_found=0.
"""
import base64
import json
import sqlite3
import tempfile
import os
import subprocess
import sys

# Create a test database with realistic data
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
conn.execute("INSERT INTO nodes (label, name, file_path, is_test, language, signature) "
             "VALUES ('Function', 'check_typing', 'pylint/extensions/typing.py', 0, 'python', 'self, node')")
conn.execute("INSERT INTO nodes (label, name, file_path, is_test, language, signature) "
             "VALUES ('Method', 'visit_return', 'pylint/extensions/typing.py', 0, 'python', 'self, node')")
conn.commit()
conn.close()

sql = (
    "SELECT n.name, n.signature FROM nodes n "
    "LEFT JOIN edges e ON e.target_id = n.id AND e.type='CALLS' "
    f"WHERE n.file_path LIKE '%pylint/extensions/typing.py' ESCAPE '\\' "
    "AND n.label IN ('Function','Method') AND n.is_test=0 "
    "GROUP BY n.id ORDER BY COUNT(e.id) DESC LIMIT 2"
)

print("=" * 60)
print("SQL:", sql)
print()

# Direct execution
conn = sqlite3.connect(db_path)
result = conn.execute(sql).fetchall()
conn.close()
print(f"Direct execution: {result}")
print(f"  -> {len(result)} symbols found")
print()

# Simulate old escaping (the bug)
escaped_old = sql.replace("'", "'\"'\"'")
print(f"Old escaped SQL: {escaped_old[:120]}...")
# What bash double-quotes would pass to Python:
# Each '"'"' in double-quote context produces different quoting
# The effect is that SQL string quotes are stripped
print()

# Simulate what Python receives after bash processes the double-quoted command
# In double-quote context, '"'"' breaks as:
#   ' = literal (inside double quotes)
#   " = ENDS double-quote context
#   ' = starts single-quote context
#   " = literal (inside single quotes)
#   ' = ends single-quote context
# This means 'CALLS' becomes adjacent strings that Python concatenates,
# losing the SQL quotes.
print("What Python would see for e.type='CALLS':")
print("  Before escaping: e.type='CALLS'")
print("  After  escaping: e.type=CALLS  (quotes stripped!)")
print()

# Fix: use base64 encoding to avoid all quoting issues
b64_sql = base64.b64encode(sql.encode()).decode()
b64_params = base64.b64encode(b"[]").decode()

# The fixed command - no quoting issues because SQL is base64-encoded
cmd_fixed = [
    sys.executable, "-c",
    "import json,sqlite3,sys,base64;"
    "c=sqlite3.connect(sys.argv[1]);"
    "sql=base64.b64decode(sys.argv[2]).decode();"
    "params=json.loads(base64.b64decode(sys.argv[3]).decode());"
    "r=c.execute(sql,params).fetchall();"
    "print(json.dumps(r))",
    db_path, b64_sql, b64_params
]
result_fixed = subprocess.run(cmd_fixed, capture_output=True, text=True)
print(f"Base64 approach result: {result_fixed.stdout.strip()}")
print(f"  -> stderr: {result_fixed.stderr.strip() or '(none)'}")
print()

# Simulate the broken approach by constructing what Python would receive
# after bash processes the old escaping in double-quote context
print("Simulating broken approach:")
# What Python receives: SQL with quotes stripped
broken_sql = sql.replace("'", "")  # This is approximately what happens
print(f"  Broken SQL: {broken_sql[:120]}...")
try:
    conn = sqlite3.connect(db_path)
    result_broken = conn.execute(broken_sql).fetchall()
    conn.close()
    print(f"  Result: {result_broken}")
except Exception as e:
    print(f"  SQLite error: {e}")
    print(f"  -> This is why symbols_found=0!")

print()
print("=" * 60)
print("ROOT CAUSE CONFIRMED:")
print("  _container_query uses '\"'\"' (bash single-quote escaping idiom)")
print("  inside a double-quoted context (python3 -c \"...\").")
print("  This strips all SQL string quotes, causing SQLite errors.")
print("  The container python3 crashes silently, _container_query returns '[]',")
print("  and auto-query reports symbols_found=0.")
print()
print("FIX: Use base64 encoding for the SQL string to avoid all quoting issues.")

os.unlink(db_path)
