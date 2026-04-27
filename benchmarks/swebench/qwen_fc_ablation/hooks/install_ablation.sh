#!/usr/bin/env bash
# Ablation install script — FC-safe, no submit patching, no XML.
#
# Copies hook + gt_intel to /tmp, patches _state_anthropic, builds index.
# Does NOT touch submit tool.
# Does NOT emit XML tags.
# Does NOT add PreSubmit gate.
#
# IMPORTANT: SWE-agent sources this with `source install.sh` inside the
# container. If any command returns non-zero, the whole bundle install
# fails and no trajectory is produced. Every command must be error-tolerant.
set +e  # Do not exit on error — SWE-agent's shell may have set -e
BUNDLE_DIR="$(pwd)"
GT_LOG="/tmp/gt_ablation_install.log"
echo "[ABLATION] Install started at $(date)" > "$GT_LOG"
echo "[ABLATION] BUNDLE_DIR=$BUNDLE_DIR" >> "$GT_LOG"
echo "[ABLATION] GT_ABLATION_MODE=${GT_ABLATION_MODE:-unset}" >> "$GT_LOG"
echo "[ABLATION] GT_ABLATION_ARM=${GT_ABLATION_ARM:-unset}" >> "$GT_LOG"

# Copy ablation hook — check both root and bin/ subdirectory
for src in "$BUNDLE_DIR/ablation_hook.py" "$BUNDLE_DIR/bin/ablation_hook.py"; do
    if [ -f "$src" ]; then
        cp "$src" /tmp/ablation_hook.py
        echo "[ABLATION] Copied ablation_hook.py from $src" >> "$GT_LOG"
        break
    fi
done
[ ! -f /tmp/ablation_hook.py ] && echo "[ABLATION] ERROR: ablation_hook.py not found" >> "$GT_LOG"

# Copy gt_intel.py for evidence computation — check both locations
for f in gt_intel.py gt_intel_real.py; do
    for src in "$BUNDLE_DIR/$f" "$BUNDLE_DIR/bin/$f"; do
        if [ -f "$src" ]; then
            cp "$src" "/tmp/$f"
            echo "[ABLATION] Copied $f from $src" >> "$GT_LOG"
            break
        fi
    done
done
# If gt_intel_real.py doesn't exist but gt_intel.py does, copy as real
if [ -f /tmp/gt_intel.py ] && [ ! -f /tmp/gt_intel_real.py ]; then
    cp /tmp/gt_intel.py /tmp/gt_intel_real.py
    echo "[ABLATION] Created gt_intel_real.py from gt_intel.py" >> "$GT_LOG"
fi

# Propagate env vars to all shells
cat >> /root/.bashrc << 'ENVEOF' || true
export GT_ABLATION_MODE="${GT_ABLATION_MODE:-inert}"
export GT_ABLATION_ARM="${GT_ABLATION_ARM:-unknown}"
ENVEOF

# Patch _state_anthropic to run ablation hook
STATE_CMD="/root/tools/edit_anthropic/bin/_state_anthropic"
if [ ! -f "$STATE_CMD" ]; then
    for alt in /root/tools/*/bin/_state_*; do
        [ -f "$alt" ] && STATE_CMD="$alt" && break
    done
fi
if [ -f "$STATE_CMD" ]; then
    cat > "$STATE_CMD" << 'STEOF'
#!/usr/bin/env python3
import json, os, subprocess, sys
from pathlib import Path

def main():
    sp = Path("/root/state.json")
    state = json.loads(sp.read_text()) if sp.exists() else {}
    state["working_dir"] = os.getcwd()
    iid = state.get("instance_id") or state.get("task_id")
    if iid and not os.environ.get("GT_INSTANCE_ID"):
        os.environ["GT_INSTANCE_ID"] = str(iid)
    sp.write_text(json.dumps(state))

    hook = "/tmp/ablation_hook.py"
    if not os.path.exists(hook):
        return

    pid_file = "/tmp/gt_hook.pid"
    try:
        if os.path.exists(pid_file):
            try:
                prior = int(open(pid_file).read().strip() or "0")
                if prior > 0:
                    os.kill(prior, 0)
                    return
            except (ValueError, ProcessLookupError, PermissionError, OSError):
                pass
        log = open("/tmp/gt_hook.log", "a")
        proc = subprocess.Popen(
            [sys.executable, hook],
            stdout=log, stderr=subprocess.STDOUT,
            env=os.environ, start_new_session=True, close_fds=True,
        )
        open(pid_file, "w").write(str(proc.pid))
    except Exception as e:
        open("/tmp/gt_hook.log", "a").write(f"EXCEPTION: {e}\n")

if __name__ == "__main__":
    main()
STEOF
    chmod +x "$STATE_CMD"
    echo "[ABLATION] Patched state command: $STATE_CMD" >> "$GT_LOG"
else
    echo "[ABLATION] WARN: no state command found" >> "$GT_LOG"
fi

# DO NOT TOUCH SUBMIT — spec requirement
echo "[ABLATION] Submit: NOT patched (FC-safe)" >> "$GT_LOG"

# Build Python index in background
cat > /tmp/gt_build_index.py << 'PYINDEX'
import sqlite3, os, re, json, time

db = sqlite3.connect("/tmp/gt_graph.db")
db.execute("CREATE TABLE IF NOT EXISTS nodes (id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT, name TEXT, qualified_name TEXT, file_path TEXT, start_line INTEGER, end_line INTEGER, signature TEXT, return_type TEXT, is_exported BOOLEAN DEFAULT 0, is_test BOOLEAN DEFAULT 0, language TEXT, parent_id INTEGER)")
db.execute("CREATE TABLE IF NOT EXISTS edges (id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER, target_id INTEGER, type TEXT, source_line INTEGER, source_file TEXT, resolution_method TEXT, confidence REAL DEFAULT 0.0, metadata TEXT)")

skip = {".git", "__pycache__", "node_modules", ".tox", "build", "dist", ".eggs"}
file_imports = {}
file_lines = {}
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
                m2 = re.match(r"^class\s+(\w+)", line)
                if m2:
                    db.execute("INSERT INTO nodes (label,name,file_path,start_line,language) VALUES (?,?,?,?,?)",
                        ("Class", m2.group(1), rel, i+1, "python"))
            file_imports[rel] = imports
        except Exception:
            pass

node_map = {}
for row in db.execute("SELECT id, name, file_path FROM nodes WHERE label IN ('Function','Method')"):
    node_map.setdefault(row[1], []).append((row[0], row[2]))

edges_added = set()
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

db.commit()
total_n = db.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
total_e = db.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
db.close()
with open("/tmp/gt_graph.db.ready", "w") as s:
    json.dump({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "nodes": total_n, "edges": total_e}, s)
print(f"[ABLATION] Index: {total_n} nodes, {total_e} edges")
PYINDEX

nohup python3 /tmp/gt_build_index.py > /tmp/gt_index.log 2>&1 &
echo "[ABLATION] Indexer PID=$! backgrounded" >> "$GT_LOG"
disown $! 2>/dev/null || true

echo "[ABLATION] Install complete"
