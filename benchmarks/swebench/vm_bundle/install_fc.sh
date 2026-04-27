#!/usr/bin/env bash
# GT tool bundle install script — FUNCTION_CALLING COMPATIBLE.
#
# Differences from install.sh:
# - Does NOT replace the submit tool (breaks function_calling parser)
# - Does NOT add submit gate wrapper (XML intervention tags break FC)
# - ONLY: copies GT files, patches state hook, builds index, installs gt_* commands
#
# Runs INSIDE the Docker container at startup.
BUNDLE_DIR="$(pwd)"
GT_LOG="/tmp/gt_install.log"
echo "[GT-FC] Install started at $(date)" > "$GT_LOG"

# Copy GT files to /tmp/
for f in swe_agent_state_gt.py gt_intel.py lsp_promoter.py; do
    if cp "$BUNDLE_DIR/$f" "/tmp/$f" 2>/dev/null || cp "$BUNDLE_DIR/bin/$f" "/tmp/$f" 2>/dev/null; then
        echo "[GT-FC] Copied $f to /tmp/" >> "$GT_LOG"
    else
        echo "[GT-FC] WARN: Failed to copy $f" >> "$GT_LOG"
    fi
done

# Copy groundtruth Python package if present
if [ -d "$BUNDLE_DIR/src/groundtruth" ]; then
    rm -rf /tmp/groundtruth_src
    mkdir -p /tmp/groundtruth_src
    cp -a "$BUNDLE_DIR/src/groundtruth" /tmp/groundtruth_src/
    echo "[GT-FC] Copied groundtruth package" >> "$GT_LOG"
fi

# Identity propagation
if [ -f "$BUNDLE_DIR/bin/gt_identity.env" ] || [ -f "$BUNDLE_DIR/gt_identity.env" ]; then
    SRC="$BUNDLE_DIR/bin/gt_identity.env"
    [ -f "$SRC" ] || SRC="$BUNDLE_DIR/gt_identity.env"
    mkdir -p /tmp/.gt
    cp "$SRC" /tmp/.gt/gt_identity.env
    cp "$SRC" /tmp/gt_identity.env
    echo "[GT-FC] Identity file copied" >> "$GT_LOG"
    grep -q "gt_identity.env" /root/.bashrc 2>/dev/null || cat >> /root/.bashrc <<'IDENTITYEOF'
set -a
[ -f /tmp/gt_identity.env ] && source /tmp/gt_identity.env
export PYTHONPATH="/tmp/groundtruth_src:${PYTHONPATH:-}"
set +a
IDENTITYEOF
fi

# Patch _state_anthropic to run GT hook
STATE_CMD="/root/tools/edit_anthropic/bin/_state_anthropic"
TARGET_STATE_CMD="$STATE_CMD"
if [ ! -f "$TARGET_STATE_CMD" ]; then
    for alt in /root/tools/*/bin/_state_*; do
        if [ -f "$alt" ]; then
            TARGET_STATE_CMD="$alt"
            break
        fi
    done
fi
if [ -f "$TARGET_STATE_CMD" ]; then
    cat > "$TARGET_STATE_CMD" << 'STEOF'
#!/usr/bin/env python3
import json, os, subprocess, sys
from pathlib import Path

def _load_identity_env():
    p = "/tmp/gt_identity.env"
    if not os.path.exists(p):
        return
    try:
        with open(p) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and v:
                    os.environ.setdefault(k, v)
    except Exception:
        pass

def main():
    _load_identity_env()
    sp = Path("/root/state.json")
    state = json.loads(sp.read_text()) if sp.exists() else {}
    state["working_dir"] = os.getcwd()
    iid = state.get("instance_id") or state.get("task_id")
    if iid and not os.environ.get("GT_INSTANCE_ID"):
        os.environ["GT_INSTANCE_ID"] = str(iid)
    sp.write_text(json.dumps(state))
    gt = "/tmp/swe_agent_state_gt.py"
    db = "/tmp/gt_graph.db"
    if not os.path.exists(gt) or not os.path.exists(db):
        return
    pid_file = "/tmp/gt_state_cmd.pid"
    try:
        if os.path.exists(pid_file):
            try:
                prior_pid = int(open(pid_file).read().strip() or "0")
                if prior_pid > 0:
                    os.kill(prior_pid, 0)
                    return
            except (ValueError, ProcessLookupError, PermissionError, OSError):
                pass
        worker_log = open("/tmp/gt_state_cmd.log", "a")
        proc = subprocess.Popen(
            [sys.executable, gt],
            stdout=worker_log,
            stderr=subprocess.STDOUT,
            env=os.environ,
            start_new_session=True,
            close_fds=True,
        )
        with open(pid_file, "w") as pf:
            pf.write(str(proc.pid))
    except Exception as e:
        with open("/tmp/gt_state_cmd.log", "a") as f:
            f.write("EXCEPTION: " + str(e) + "\n")
if __name__ == "__main__":
    main()
STEOF
    chmod +x "$TARGET_STATE_CMD"
    echo "[GT-FC] Patched state command: $TARGET_STATE_CMD" >> "$GT_LOG"
fi

# DO NOT TOUCH SUBMIT TOOL — function_calling parser requires original submit behavior.
# The submit gate (gt-intervention XML) breaks function_calling → FunctionCallingFormatError.
echo "[GT-FC] Submit tool: NOT patched (function_calling compatible)" >> "$GT_LOG"

# Set GT environment
export GT_DB=/tmp/gt_graph.db
export GT_ROOT=/testbed
echo "export GT_DB=/tmp/gt_graph.db" >> /root/.bashrc
echo "export GT_ROOT=/testbed" >> /root/.bashrc

# Prevent GT telemetry from polluting git patches
mkdir -p /testbed/.git/info 2>/dev/null
echo ".gt/" >> /testbed/.git/info/exclude 2>/dev/null || true

# Budget wrapper for gt_intel.py
cat > /tmp/gt_intel_wrapper.py << 'WRAPEOF'
#!/usr/bin/env python3
import json, os, re, subprocess, sys, time

LIMITS = {"orient": 2, "lookup": 3, "impact": 2, "check": 20}
REDIRECTS = {
    "orient": "gt_orient already used. Use 'gt_lookup <symbol>' for targeted lookups.",
    "lookup": "gt_lookup cap reached. Use grep or read the file directly.",
    "impact": "gt_impact cap reached. Trust earlier impact result.",
    "check":  "gt_check cap reached. Run tests or trust earlier output.",
}
DB = os.environ.get("GT_DB", "/tmp/gt_graph.db")
ROOT = os.environ.get("GT_ROOT", "/testbed")
REAL = "/tmp/gt_intel_real.py"
STATE_FILE = "/tmp/gt_budget.state.json"

def _task_scope():
    parts = [os.environ.get(k, "").strip() for k in ("GT_RUN_ID", "GT_INSTANCE_ID", "GT_ARM")]
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", "__".join(p for p in parts if p))[:160] or "unknown"

def load_state():
    try:
        if os.path.exists(STATE_FILE):
            st = json.loads(open(STATE_FILE).read())
            if st.get("scope") == _task_scope():
                return st
    except Exception:
        pass
    return {"scope": _task_scope(), **{t: {"count": 0, "limit": l} for t, l in LIMITS.items()}}

def save_state(st):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(st, f)
    except Exception:
        pass

def find_symbol_file(sym):
    try:
        out = subprocess.run(
            ["grep", "-rnE", "--include=*.py", rf"^[[:space:]]*(def|class)[[:space:]]+{sym}\b", ROOT],
            capture_output=True, text=True, timeout=5,
        ).stdout
        for line in out.splitlines():
            path = line.split(":", 1)[0]
            if path:
                return os.path.relpath(path, ROOT)
    except Exception:
        pass
    return ""

if len(sys.argv) < 2 or sys.argv[1] not in LIMITS:
    print("Usage: gt_orient | gt_lookup <symbol> | gt_impact <symbol> | gt_check <file>")
    sys.exit(0)

cmd = sys.argv[1]
arg = sys.argv[2] if len(sys.argv) > 2 else ""

state = load_state()
bucket = state.get(cmd, {"count": 0, "limit": LIMITS[cmd]})
if int(bucket.get("count", 0)) >= int(bucket.get("limit", LIMITS[cmd])):
    print(f"BUDGET_EXHAUSTED: gt_{cmd} has reached its cap of {bucket['limit']}. {REDIRECTS[cmd]}")
    sys.exit(0)

bucket["count"] = int(bucket.get("count", 0)) + 1
state[cmd] = bucket
save_state(state)

if cmd == "orient":
    argv = [f"--db={DB}", f"--root={ROOT}", "--enhanced-briefing"]
    for candidate in ("/tmp/gt_issue.txt", "/tmp/problem_statement.txt"):
        if os.path.exists(candidate):
            argv.append(f"--issue-text=@{candidate}")
            break
elif cmd == "lookup":
    if not arg: print("Usage: gt_lookup <symbol>"); sys.exit(0)
    fpath = find_symbol_file(arg)
    argv = [f"--db={DB}", f"--root={ROOT}", f"--function={arg}"]
    if fpath: argv += [f"--file={fpath}", "--reminder"]
elif cmd == "impact":
    if not arg: print("Usage: gt_impact <symbol>"); sys.exit(0)
    fpath = find_symbol_file(arg)
    argv = [f"--db={DB}", f"--root={ROOT}", f"--function={arg}"]
    if fpath: argv.append(f"--file={fpath}")
elif cmd == "check":
    if not arg: print("Usage: gt_check <file>"); sys.exit(0)
    argv = [f"--db={DB}", f"--root={ROOT}", f"--file={arg}", "--reminder"]
else:
    sys.exit(0)

result = subprocess.run([sys.executable, REAL] + argv, timeout=30)
sys.exit(result.returncode)
WRAPEOF

if [ -f /tmp/gt_intel.py ] && [ ! -f /tmp/gt_intel_real.py ]; then
    mv /tmp/gt_intel.py /tmp/gt_intel_real.py
    cp /tmp/gt_intel_wrapper.py /tmp/gt_intel.py
    chmod +x /tmp/gt_intel.py
    echo "[GT-FC] Budget wrapper installed" >> "$GT_LOG"
fi

# gt_* shell commands
for cmd in orient lookup impact check; do
    cat > "/usr/local/bin/gt_$cmd" << CMDEOF
#!/usr/bin/env bash
[ -f /tmp/gt_identity.env ] && { set -a; . /tmp/gt_identity.env; set +a; }
python3 /tmp/gt_intel.py $cmd "\$@"
CMDEOF
    chmod +x "/usr/local/bin/gt_$cmd"
done
echo "[GT-FC] Installed gt_* commands" >> "$GT_LOG"

# Build index in background
cat > /tmp/gt_build_index.py << 'PYINDEX'
import sqlite3, os, re

db = sqlite3.connect("/tmp/gt_graph.db")
db.execute("CREATE TABLE IF NOT EXISTS nodes (id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT, name TEXT, qualified_name TEXT, file_path TEXT, start_line INTEGER, end_line INTEGER, signature TEXT, return_type TEXT, is_exported BOOLEAN DEFAULT 0, is_test BOOLEAN DEFAULT 0, language TEXT, parent_id INTEGER)")
db.execute("CREATE TABLE IF NOT EXISTS edges (id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER, target_id INTEGER, type TEXT, source_line INTEGER, source_file TEXT, resolution_method TEXT, confidence REAL DEFAULT 0.0, metadata TEXT)")
db.execute("CREATE TABLE IF NOT EXISTS properties (id INTEGER PRIMARY KEY AUTOINCREMENT, node_id INTEGER, kind TEXT, value TEXT)")
db.execute("CREATE TABLE IF NOT EXISTS assertions (id INTEGER PRIMARY KEY AUTOINCREMENT, target_node_id INTEGER, assertion_text TEXT)")

skip = {".git", "__pycache__", "node_modules", ".tox", "build", "dist", ".eggs"}
file_imports = {}
file_lines = {}
_import_re = re.compile(r"^(?:from\s+([\w.]+)\s+)?import\s+(.+)")
n = 0

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
                im = _import_re.match(line.strip())
                if im:
                    mod = im.group(1) or ""
                    for name in im.group(2).split(","):
                        name = name.strip().split(" as ")[0].strip()
                        if name and name != "*":
                            imports[name] = mod
                m = re.match(r"^(\s*)(?:async\s+)?def\s+(\w+)\s*\(", line)
                if m:
                    label = "Method" if len(m.group(1)) > 0 else "Function"
                    is_test = m.group(2).startswith("test_") or "/test" in rel
                    db.execute("INSERT INTO nodes (label,name,file_path,start_line,language,is_test) VALUES (?,?,?,?,?,?)",
                        (label, m.group(2), rel, i+1, "python", is_test))
                    n += 1
                m2 = re.match(r"^class\s+(\w+)", line)
                if m2:
                    db.execute("INSERT INTO nodes (label,name,file_path,start_line,language) VALUES (?,?,?,?,?)",
                        ("Class", m2.group(1), rel, i+1, "python"))
                    n += 1
            file_imports[rel] = imports
        except Exception:
            pass

node_map = {}
for row in db.execute("SELECT id, name, file_path FROM nodes WHERE label IN ('Function','Method')"):
    node_map.setdefault(row[1], []).append((row[0], row[2]))

edges_added = set()
e_count = 0
for row in db.execute("SELECT id, name, file_path, start_line FROM nodes WHERE label IN ('Function','Method')"):
    src_id, src_name, src_file, src_line = row
    if src_file not in file_lines: continue
    lines = file_lines[src_file]
    imports = file_imports.get(src_file, {})
    body_end = min(src_line + 200, len(lines))
    for j in range(src_line, body_end):
        if j > src_line and re.match(r"^(?:def |class |@\w)", lines[j]):
            body_end = j; break
    for j in range(src_line, body_end):
        for called in re.findall(r"\b(\w+)\s*\(", lines[j]):
            if called == src_name or called not in node_map: continue
            for tgt_id, tgt_file in node_map[called]:
                key = (src_id, tgt_id, j+1)
                if key in edges_added: continue
                if tgt_file == src_file:
                    conf, method = 1.0, "same_file"
                elif called in imports:
                    conf, method = 1.0, "import"
                else:
                    cands = len(node_map[called])
                    conf = 0.9 if cands == 1 else 0.6 if cands == 2 else 0.4 if cands <= 5 else 0.2
                    method = "name_match"
                edges_added.add(key)
                db.execute("INSERT INTO edges (source_id,target_id,type,source_line,source_file,resolution_method,confidence) VALUES (?,?,?,?,?,?,?)",
                    (src_id, tgt_id, "CALLS", j+1, src_file, method, conf))
                e_count += 1

db.commit()
total_n = db.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
total_e = db.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
db.close()
import json, time
with open("/tmp/gt_graph.db.ready", "w") as s:
    json.dump({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "nodes": total_n, "edges": total_e, "status": "success" if total_n > 0 else "fail"}, s)
print(f"[GT] Index: {total_n} nodes, {total_e} edges -> /tmp/gt_graph.db")
PYINDEX
nohup python3 /tmp/gt_build_index.py > /tmp/gt_index.log 2>&1 &
echo "[GT-FC] Indexer PID=$! backgrounded" >> "$GT_LOG"
disown $! 2>/dev/null || true

echo "[GT-FC] Install complete"
