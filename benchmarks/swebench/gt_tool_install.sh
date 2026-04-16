#!/usr/bin/env bash
# GT tool bundle install script for SWE-agent
# Runs INSIDE the Docker container at startup.
# Logs all diagnostics to /tmp/gt_install.log for post-run extraction.
BUNDLE_DIR="$(pwd)"
GT_LOG="/tmp/gt_install.log"
echo "[GT] Install started at $(date)" > "$GT_LOG"

# Copy GT files to /tmp/
for f in swe_agent_state_gt.py gt_intel.py lsp_promoter.py; do
    if cp "$BUNDLE_DIR/bin/$f" "/tmp/$f" 2>/dev/null; then
        echo "[GT] Copied $f to /tmp/" >> "$GT_LOG"
    else
        echo "[GT] WARN: Failed to copy $f (bundle_dir=$BUNDLE_DIR)" >> "$GT_LOG"
    fi
done

# CRITICAL: Patch _state_anthropic to also run GT hook
# This is the ONLY way to inject GT evidence — the state command runs inside
# the container after every agent action, and only edit_anthropic's state
# command is used by SWE-agent.
STATE_CMD="/root/tools/edit_anthropic/bin/_state_anthropic"
if [ -f "$STATE_CMD" ]; then
    cat > "$STATE_CMD" << 'STEOF'
#!/usr/bin/env python3
import json, os, subprocess, sys
from pathlib import Path
def main():
    sp = Path("/root/state.json")
    state = json.loads(sp.read_text()) if sp.exists() else {}
    state["working_dir"] = os.getcwd()
    sp.write_text(json.dumps(state))
    gt = "/tmp/swe_agent_state_gt.py"
    db = "/tmp/gt_graph.db"
    log = "/tmp/gt_state_cmd.log"
    if not os.path.exists(gt):
        with open(log, "a") as f: f.write("SKIP: swe_agent_state_gt.py missing\n")
        return
    if not os.path.exists(db):
        with open(log, "a") as f: f.write("SKIP: gt_graph.db missing\n")
        return
    try:
        r = subprocess.run([sys.executable, gt], timeout=30, capture_output=True, text=True)
        if r.returncode != 0:
            with open(log, "a") as f: f.write(f"ERROR rc={r.returncode}: {r.stderr[:200]}\n")
    except Exception as e:
        with open(log, "a") as f: f.write(f"EXCEPTION: {e}\n")
if __name__ == "__main__":
    main()
STEOF
    chmod +x "$STATE_CMD"
    echo "[GT] Patched _state_anthropic with GT hook" | tee -a "$GT_LOG"
else
    echo "[GT] WARN: _state_anthropic not found at $STATE_CMD — hook NOT patched" >> "$GT_LOG"
    # Try alternate paths
    for alt in /root/tools/*/bin/_state_*; do
        echo "[GT] Found alternate state cmd: $alt" >> "$GT_LOG"
    done
fi

# LSP-hybrid mode
if [ "$GT_LSP_ENABLED" = "1" ]; then
    echo "[GT] LSP-hybrid mode: installing groundtruth + pyright"
    pip install groundtruth pyright 2>/dev/null || true
    echo "[GT] LSP install complete"
fi

# Set GT environment variables
export GT_DB=/tmp/gt_graph.db
export GT_ROOT=/testbed
echo "export GT_DB=/tmp/gt_graph.db" >> /root/.bashrc
echo "export GT_ROOT=/testbed" >> /root/.bashrc

# Prevent GT telemetry from polluting git patches
echo ".gt/" >> /testbed/.gitignore 2>/dev/null || true

# Create budget-enforcing wrapper for gt_intel.py
# Budgets: orient=1, lookup≤2, impact≤2, check≤3 (from SWE-Skills research)
cat > /tmp/gt_intel_wrapper.py << 'WRAPEOF'
#!/usr/bin/env python3
"""Budget-enforcing wrapper for gt_intel.py.
Limits: orient=1, lookup=2, impact=2, check=3.
"""
import json, os, subprocess, sys

BUDGET_FILE = "/tmp/gt_call_budget.json"
LIMITS = {"orient": 1, "lookup": 2, "impact": 2, "check": 3}

def load_counts():
    if os.path.exists(BUDGET_FILE):
        try: return json.loads(open(BUDGET_FILE).read())
        except: pass
    return {}

def save_counts(c):
    open(BUDGET_FILE, "w").write(json.dumps(c))

if len(sys.argv) < 2:
    print("Usage: python3 /tmp/gt_intel.py <orient|lookup|impact|check> [args...]")
    sys.exit(0)

cmd = sys.argv[1]
counts = load_counts()
current = counts.get(cmd, 0)
limit = LIMITS.get(cmd, 99)

if current >= limit:
    print(f"[GT] Budget exhausted for '{cmd}' ({current}/{limit} calls used). Skipping.")
    sys.exit(0)

counts[cmd] = current + 1
save_counts(counts)

# Forward to real gt_intel.py
real = "/tmp/gt_intel_real.py"
result = subprocess.run([sys.executable, real] + sys.argv[1:],
                        capture_output=False, timeout=30)
sys.exit(result.returncode)
WRAPEOF

# Move real gt_intel.py aside and put wrapper in its place
if [ -f /tmp/gt_intel.py ] && [ ! -f /tmp/gt_intel_real.py ]; then
    mv /tmp/gt_intel.py /tmp/gt_intel_real.py
    cp /tmp/gt_intel_wrapper.py /tmp/gt_intel.py
    chmod +x /tmp/gt_intel.py
    echo "[GT] Budget wrapper installed (orient=1, lookup=2, impact=2, check=3)" >> "$GT_LOG"
fi

# Build index with Python (gt-index binary segfaults in containers)
python3 << 'PYINDEX'
import sqlite3, os, re, sys

db = sqlite3.connect("/tmp/gt_graph.db")
db.execute("CREATE TABLE IF NOT EXISTS nodes (id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT, name TEXT, qualified_name TEXT, file_path TEXT, start_line INTEGER, end_line INTEGER, signature TEXT, return_type TEXT, is_exported BOOLEAN DEFAULT 0, is_test BOOLEAN DEFAULT 0, language TEXT, parent_id INTEGER)")
db.execute("CREATE TABLE IF NOT EXISTS edges (id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER, target_id INTEGER, type TEXT, source_line INTEGER, source_file TEXT, resolution_method TEXT, confidence REAL DEFAULT 0.0, metadata TEXT)")
db.execute("CREATE TABLE IF NOT EXISTS properties (id INTEGER PRIMARY KEY AUTOINCREMENT, node_id INTEGER, kind TEXT, value TEXT)")
db.execute("CREATE TABLE IF NOT EXISTS assertions (id INTEGER PRIMARY KEY AUTOINCREMENT, target_node_id INTEGER, assertion_text TEXT)")

n = 0
n_assert = 0
skip = {".git", "__pycache__", "node_modules", ".tox", "build", "dist", ".eggs", "egg-info"}

# ── Pass 1: Extract nodes (functions, methods, classes) ──────────────────
# Also build per-file import maps and collect test assertions.
file_imports = {}   # rel_path -> {imported_name: source_module}
file_lines = {}     # rel_path -> list of lines (cached for pass 2)
_import_re = re.compile(r"^(?:from\s+([\w.]+)\s+)?import\s+(.+)")

for root, dirs, files in os.walk("/testbed"):
    dirs[:] = [d for d in dirs if d not in skip and not d.endswith(".egg-info")]
    for f in files:
        if not f.endswith(".py"):
            continue
        fp = os.path.join(root, f)
        rel = os.path.relpath(fp, "/testbed")
        try:
            lines = open(fp, errors="ignore").readlines()
            file_lines[rel] = lines
            imports = {}

            for i, line in enumerate(lines):
                # Extract imports for edge resolution
                im = _import_re.match(line.strip())
                if im:
                    mod = im.group(1) or ""
                    for name in im.group(2).split(","):
                        name = name.strip().split(" as ")[0].strip()
                        if name and name != "*":
                            imports[name] = mod

                # Extract function/method definitions
                m = re.match(r"^(\s*)(?:async\s+)?def\s+(\w+)\s*\(", line)
                if m:
                    label = "Method" if len(m.group(1)) > 0 else "Function"
                    name = m.group(2)
                    is_test = name.startswith("test_") or "/test" in rel
                    db.execute(
                        "INSERT INTO nodes (label,name,file_path,start_line,language,is_test) VALUES (?,?,?,?,?,?)",
                        (label, name, rel, i + 1, "python", is_test),
                    )
                    n += 1

                    # Extract assertions from test functions
                    if is_test:
                        # Scan body for assert statements (up to 100 lines)
                        for j in range(i + 1, min(i + 100, len(lines))):
                            bl = lines[j]
                            # Stop at next top-level def/class
                            if re.match(r"^(?:def |class |@)", bl):
                                break
                            bl_s = bl.strip()
                            if bl_s.startswith("assert ") or "assertEqual" in bl_s or "assert_" in bl_s:
                                # Find which function is being tested
                                for called in re.findall(r"\b(\w+)\s*\(", bl_s):
                                    if called not in ("assert", "assertEqual", "assertTrue",
                                                      "assertFalse", "assertRaises", "assertIn",
                                                      "assertIsNone", "len", "str", "int", "list"):
                                        # Store assertion text (truncated)
                                        db.execute(
                                            "INSERT INTO assertions (target_node_id, assertion_text) VALUES (NULL, ?)",
                                            (bl_s[:200],),
                                        )
                                        n_assert += 1
                                        break

                # Extract class definitions
                m2 = re.match(r"^class\s+(\w+)", line)
                if m2:
                    db.execute(
                        "INSERT INTO nodes (label,name,file_path,start_line,language) VALUES (?,?,?,?,?)",
                        ("Class", m2.group(1), rel, i + 1, "python"),
                    )
                    n += 1

            file_imports[rel] = imports
        except Exception:
            pass

# ── Pass 2: Build edges (import-verified + same-file + name-match) ───────
node_map = {}  # name -> [(id, file_path)]
for row in db.execute("SELECT id, name, file_path FROM nodes WHERE label IN ('Function','Method')"):
    node_map.setdefault(row[1], []).append((row[0], row[2]))

# Build module->file map for import resolution
mod_file_map = {}  # "package.module" -> rel_path
for rel in file_lines:
    # Convert file path to module path: "foo/bar/baz.py" -> "foo.bar.baz"
    mod = rel.replace("/", ".").replace("\\", ".")
    if mod.endswith(".py"):
        mod = mod[:-3]
    if mod.endswith(".__init__"):
        mod = mod[:-9]
    mod_file_map[mod] = rel
    # Also store the last component for partial matching
    parts = mod.split(".")
    if len(parts) > 1:
        mod_file_map[parts[-1]] = rel

edges_added = set()  # (src_id, tgt_id, src_line) dedup
e_count = 0

for row in db.execute("SELECT id, name, file_path, start_line FROM nodes WHERE label IN ('Function','Method')"):
    src_id, src_name, src_file, src_line = row
    if src_file not in file_lines:
        continue
    lines = file_lines[src_file]
    imports = file_imports.get(src_file, {})

    # Determine function body end (scan until next def/class at same or lower indent, max 200 lines)
    body_end = min(src_line + 200, len(lines))
    for j in range(src_line, body_end):
        bl = lines[j]
        if j > src_line and re.match(r"^(?:def |class |@\w)", bl):
            body_end = j
            break

    # Scan function body for calls
    for j in range(src_line, body_end):
        for called in re.findall(r"\b(\w+)\s*\(", lines[j]):
            if called == src_name or called not in node_map:
                continue

            for tgt_id, tgt_file in node_map[called]:
                dedup_key = (src_id, tgt_id, j + 1)
                if dedup_key in edges_added:
                    continue

                if tgt_file == src_file:
                    # Same-file call: high confidence
                    conf = 1.0
                    method = "same_file"
                elif called in imports:
                    # Import-verified: check if imported module matches target file
                    imp_mod = imports[called]
                    # If the import source module matches the target file's path, conf=1.0
                    tgt_mod = tgt_file.replace("/", ".").replace("\\", ".").rstrip(".py")
                    if imp_mod and (imp_mod in tgt_mod or tgt_mod.endswith(imp_mod)):
                        conf = 1.0
                        method = "import"
                    else:
                        # Imported but from a different module — still likely
                        conf = 0.8
                        method = "import"
                else:
                    # Cross-file name match (fallback)
                    candidates = len(node_map[called])
                    if candidates == 1:
                        conf = 0.9
                    elif candidates == 2:
                        conf = 0.6
                    elif candidates <= 5:
                        conf = 0.4
                    else:
                        conf = 0.2
                    method = "name_match"

                edges_added.add(dedup_key)
                db.execute(
                    "INSERT INTO edges (source_id,target_id,type,source_line,source_file,resolution_method,confidence) VALUES (?,?,?,?,?,?,?)",
                    (src_id, tgt_id, "CALLS", j + 1, src_file, method, conf),
                )
                e_count += 1

# ── Pass 3: Link assertions to target nodes ──────────────────────────────
# Re-scan test functions to connect assertion rows to the functions they test
for row in db.execute("SELECT id, name, file_path, start_line FROM nodes WHERE is_test = 1"):
    test_id, test_name, test_file, test_line = row
    if test_file not in file_lines:
        continue
    lines = file_lines[test_file]
    # The tested function is usually named in the test name: test_foo -> foo
    tested = test_name.replace("test_", "", 1) if test_name.startswith("test_") else ""
    if tested and tested in node_map:
        for tgt_id, _ in node_map[tested]:
            # Update NULL target_node_id assertions
            db.execute(
                "UPDATE assertions SET target_node_id = ? WHERE target_node_id IS NULL AND assertion_text LIKE ?",
                (tgt_id, f"%{tested}%"),
            )

db.commit()

# ── Diagnostic summary ───────────────────────────────────────────────────
total_nodes = db.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
total_edges = db.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
same_file = db.execute("SELECT COUNT(*) FROM edges WHERE resolution_method='same_file'").fetchone()[0]
import_edges = db.execute("SELECT COUNT(*) FROM edges WHERE resolution_method='import'").fetchone()[0]
name_match = db.execute("SELECT COUNT(*) FROM edges WHERE resolution_method='name_match'").fetchone()[0]
total_asserts = db.execute("SELECT COUNT(*) FROM assertions WHERE target_node_id IS NOT NULL").fetchone()[0]
db.close()

summary = (
    f"[GT] Python index: {total_nodes} nodes, {total_edges} edges "
    f"(same_file={same_file}, import={import_edges}, name_match={name_match}), "
    f"{total_asserts} linked assertions -> /tmp/gt_graph.db"
)
print(summary)

# Write diagnostic to log file
with open("/tmp/gt_install.log", "a") as f:
    f.write(summary + "\n")
    if total_edges == 0:
        f.write("[GT] CRITICAL: Zero edges — micro-updates will be gated out by score filter\n")
    if total_nodes == 0:
        f.write("[GT] CRITICAL: Zero nodes — no symbols found in /testbed\n")
PYINDEX

echo '[GT] Install complete'
