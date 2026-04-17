#!/usr/bin/env bash
# GT tool bundle install script for SWE-agent
# Runs INSIDE the Docker container at startup.
# Logs all diagnostics to /tmp/gt_install.log for post-run extraction.
BUNDLE_DIR="$(pwd)"
GT_LOG="/tmp/gt_install.log"
echo "[GT] Install started at $(date)" > "$GT_LOG"

# Copy GT files to /tmp/ and hash them for postdeploy parity audit
for f in swe_agent_state_gt.py gt_intel.py lsp_promoter.py; do
    if cp "$BUNDLE_DIR/bin/$f" "/tmp/$f" 2>/dev/null; then
        h=$(sha256sum "/tmp/$f" | awk '{print $1}')
        echo "[GT] Copied $f to /tmp/ sha256=$h" >> "$GT_LOG"
    else
        echo "[GT] WARN: Failed to copy $f (bundle_dir=$BUNDLE_DIR)" >> "$GT_LOG"
    fi
done

# ── Identity propagation ─────────────────────────────────────────────────
# SWE-agent does NOT propagate env_variables from the YAML into the
# container runtime env that tool scripts see. Workaround: the per-arm
# runner writes bin/gt_identity.env into this bundle with GT_ARM + GT_RUN_ID
# + GT_TELEMETRY_DIR. Copy it to /tmp/ and make every subshell source it.
if [ -f "$BUNDLE_DIR/bin/gt_identity.env" ]; then
    cp "$BUNDLE_DIR/bin/gt_identity.env" /tmp/gt_identity.env
    echo "[GT] Identity file copied: $(cat /tmp/gt_identity.env | tr '\n' ' ')" >> "$GT_LOG"
    # Auto-export for every bash shell (agent bash tool runs in new shells)
    grep -q "gt_identity.env" /root/.bashrc 2>/dev/null || cat >> /root/.bashrc <<'IDENTITYEOF'
set -a
[ -f /tmp/gt_identity.env ] && source /tmp/gt_identity.env
set +a
IDENTITYEOF
else
    echo "[GT] WARN: no gt_identity.env in bundle ($BUNDLE_DIR/bin/)" >> "$GT_LOG"
fi

# CRITICAL: Patch _state_anthropic to also run GT hook
# This is the ONLY way to inject GT evidence — the state command runs inside
# the container after every agent action, and only edit_anthropic's state
# command is used by SWE-agent.
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
    """Read /tmp/gt_identity.env into os.environ if keys missing.

    Written by gt_tool_install.sh from the bundle. Needed because sweagent
    does not reliably propagate tools.env_variables into the container
    runtime env — the hook needs GT_ARM / GT_RUN_ID / GT_TELEMETRY_DIR
    to stamp telemetry correctly.
    """
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
    # Propagate instance_id from state.json to env so the GT hook can stamp
    # every telemetry event with the task identifier (parallel-task safe).
    iid = state.get("instance_id") or state.get("task_id")
    if iid and not os.environ.get("GT_INSTANCE_ID"):
        os.environ["GT_INSTANCE_ID"] = str(iid)
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
    # stdout=PIPE/stderr=PIPE instead of capture_output=True — capture_output
    # was added in Py3.7; whichever python3 ends up on PATH here is not
    # guaranteed to be 3.7+, and we were losing every hook invocation to a
    # TypeError from Popen.__init__.
    try:
        r = subprocess.run(
            [sys.executable, gt],
            timeout=30,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=os.environ,
        )
        if r.returncode != 0:
            with open(log, "a") as f:
                err = (r.stderr or b"").decode("utf-8", "replace")[:200]
                f.write(f"ERROR rc={r.returncode}: {err}\n")
    except Exception as e:
        with open(log, "a") as f: f.write(f"EXCEPTION: {e}\n")
if __name__ == "__main__":
    main()
STEOF
    chmod +x "$TARGET_STATE_CMD"
    echo "[GT] Patched state command with GT hook: $TARGET_STATE_CMD" | tee -a "$GT_LOG"
else
    echo "[GT] WARN: no state command found — hook NOT patched" >> "$GT_LOG"
    # Try alternate paths
    for alt in /root/tools/*/bin/_state_*; do
        echo "[GT] Found alternate state cmd: $alt" >> "$GT_LOG"
    done
fi

# Patch submit to filter junk files (test scripts, debug scripts, reproduce scripts)
# review_on_submit_m uses `git add -A && git diff --cached` which captures everything.
# We replace it with a filtered diff that only includes real source file changes.
SUBMIT_CMD="/root/tools/review_on_submit_m/bin/submit"
if [ -f "$SUBMIT_CMD" ]; then
    # Back up original
    cp "$SUBMIT_CMD" "${SUBMIT_CMD}.orig"
    cat > "$SUBMIT_CMD" << 'SUBMITEOF'
#!/usr/bin/env python3
"""Filtered submit: only include source file diffs, not test/debug/reproduce scripts."""
import os, subprocess, sys

REPO = "/testbed"
PATCH = "/root/model.patch"
JUNK_PREFIXES = ("test_", "reproduce", "debug", "verify", "check_", "demo_", "final_", "comprehensive_")

def get_source_files():
    """Get modified files that are real source (not agent-created junk)."""
    r = subprocess.run(["git", "diff", "--name-only"], capture_output=True, text=True, cwd=REPO)
    r2 = subprocess.run(["git", "diff", "--staged", "--name-only"], capture_output=True, text=True, cwd=REPO)
    all_files = set(r.stdout.strip().split("\n") + r2.stdout.strip().split("\n"))
    source = []
    for f in all_files:
        if not f.strip():
            continue
        base = os.path.basename(f)
        # Skip files created by the agent (not part of original repo)
        if any(base.startswith(p) for p in JUNK_PREFIXES):
            continue
        # Skip .gitignore changes
        if base == ".gitignore":
            continue
        # Only include files in the repo's source tree (not root-level scripts)
        if "/" in f:  # has a directory = likely real source
            source.append(f)
        elif base.endswith((".py", ".js", ".ts", ".go", ".rs", ".java")):
            # Root-level .py files are usually agent-created scripts
            # Check if file existed before (tracked by git)
            check = subprocess.run(["git", "ls-files", f], capture_output=True, text=True, cwd=REPO)
            if check.stdout.strip():  # file is tracked = real source
                source.append(f)
    return source

try:
    files = get_source_files()
    if files:
        cmd = ["git", "diff", "--"] + files
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=REPO)
        with open(PATCH, "w") as f:
            f.write(r.stdout)
    else:
        # Fallback: diff everything (better than empty)
        r = subprocess.run(["git", "diff"], capture_output=True, text=True, cwd=REPO)
        with open(PATCH, "w") as f:
            f.write(r.stdout)
except Exception as e:
    # Last resort fallback
    subprocess.run(f"cd {REPO} && git diff > {PATCH}", shell=True)

print("Patch saved to", PATCH, "(%d bytes)" % os.path.getsize(PATCH))
SUBMITEOF
    chmod +x "$SUBMIT_CMD"
    echo "[GT] Patched submit with source-file filter" | tee -a "$GT_LOG"
else
    echo "[GT] WARN: submit command not found at $SUBMIT_CMD" >> "$GT_LOG"
fi

# Fallback: ensure model.patch exists on exit (handles auto-exit without submit)
cat > /tmp/gt_ensure_patch.sh << 'PATCHEOF'
#!/bin/bash
PATCH="/root/model.patch"
if [ ! -f "$PATCH" ] || [ ! -s "$PATCH" ]; then
    cd /testbed 2>/dev/null || exit 0
    # Create patch from tracked source files only
    SRCFILES=$(git diff --name-only 2>/dev/null | grep -v '^test_\|^reproduce\|^debug\|^verify\|^check_\|^demo_\|^final_\|^comprehensive_\|^\.gitignore$' | grep '/')
    if [ -n "$SRCFILES" ]; then
        git diff -- $SRCFILES > "$PATCH" 2>/dev/null
    else
        git diff > "$PATCH" 2>/dev/null
    fi
fi
PATCHEOF
chmod +x /tmp/gt_ensure_patch.sh
# Register as exit trap in bashrc so it runs before container cleanup
echo 'trap "/tmp/gt_ensure_patch.sh" EXIT' >> /root/.bashrc
echo "[GT] Fallback patch trap installed" >> "$GT_LOG"

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
# Use git's exclude file (NOT .gitignore) to avoid dirtying the diff
mkdir -p /testbed/.git/info 2>/dev/null
echo ".gt/" >> /testbed/.git/info/exclude 2>/dev/null || true

# Budget-enforcing wrapper for gt_intel.py — subcommand → argparse-flag translator.
# Budgets: orient=1, lookup=2, impact=2, check=3 (from SWE-Skills research).
# Previous version passed bare subcommands to gt_intel.py which uses flag-based argparse,
# so every agent-visible call (gt_lookup/gt_impact) errored with "argument --db is required"
# and the agent quickly stopped calling GT tools → L3 metric stuck at ~0.
cat > /tmp/gt_intel_wrapper.py << 'WRAPEOF'
#!/usr/bin/env python3
"""Budget-enforcing subcommand translator for gt_intel.py.

Translates: gt_intel.py <orient|lookup|impact|check> [arg] → real gt_intel.py --flags
Real CLI: /tmp/gt_intel_real.py (argparse-based: --db, --file, --function, --reminder, ...).
"""
import json, os, re, subprocess, sys

LIMITS = {"orient": 1, "lookup": 2, "impact": 2, "check": 3}
DB = os.environ.get("GT_DB", "/tmp/gt_graph.db")
ROOT = os.environ.get("GT_ROOT", "/testbed")
REAL = "/tmp/gt_intel_real.py"

def _task_scope() -> str:
    """Return a stable per-task scope for budget isolation."""
    instance = os.environ.get("GT_INSTANCE_ID", "").strip()
    run_id = os.environ.get("GT_RUN_ID", "").strip()
    arm = os.environ.get("GT_ARM", "").strip()
    parts = [p for p in (run_id, instance, arm) if p]
    scope = "__".join(parts) if parts else "unknown"
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", scope)[:160]

def _budget_file() -> str:
    return f"/tmp/gt_call_budget_{_task_scope()}.json"

def load_counts():
    budget_file = _budget_file()
    if os.path.exists(budget_file):
        try:
            return json.loads(open(budget_file).read())
        except Exception: pass
    return {}

def save_counts(c):
    open(_budget_file(), "w").write(json.dumps(c))

def find_symbol_file(sym: str) -> str:
    """Grep for `def sym` or `class sym` in project; return first hit (relative)."""
    try:
        out = subprocess.run(
            ["grep", "-rnE", "--include=*.py", "--include=*.pyx",
             rf"^[[:space:]]*(def|class)[[:space:]]+{sym}\b", ROOT],
            capture_output=True, text=True, timeout=5,
        ).stdout
        for line in out.splitlines():
            path = line.split(":", 1)[0]
            if path:
                return os.path.relpath(path, ROOT).replace("\\", "/")
    except Exception:
        pass
    return ""

def pass_through_flag_mode() -> bool:
    """If caller already uses --flags (hook calls), forward directly to real CLI."""
    return len(sys.argv) > 1 and sys.argv[1].startswith("--")

if pass_through_flag_mode():
    result = subprocess.run([sys.executable, REAL] + sys.argv[1:], timeout=30)
    sys.exit(result.returncode)

if len(sys.argv) < 2 or sys.argv[1] not in LIMITS:
    print("Usage: gt_orient | gt_lookup <symbol> | gt_impact <symbol> | gt_check <file>")
    sys.exit(0)

cmd = sys.argv[1]
arg = sys.argv[2] if len(sys.argv) > 2 else ""

# Budget check
counts = load_counts()
current = counts.get(cmd, 0)
limit = LIMITS[cmd]
if current >= limit:
    print(f"[GT] Budget exhausted for '{cmd}' ({current}/{limit}). Skipping.")
    sys.exit(0)
counts[cmd] = current + 1
save_counts(counts)

# Subcommand → real-CLI flag translation
if cmd == "orient":
    argv = [f"--db={DB}", f"--root={ROOT}", "--enhanced-briefing"]
    # Issue text sources in order: env var → /tmp files (hook writes these)
    issue_text = os.environ.get("PROBLEM_STATEMENT", "")
    if issue_text:
        # Write to temp file so --issue-text=@path works (avoids shell-escaping issues)
        tmp = "/tmp/gt_issue.txt"
        with open(tmp, "w") as f:
            f.write(issue_text)
        argv.append(f"--issue-text=@{tmp}")
    else:
        for candidate in ("/tmp/gt_issue.txt", "/tmp/problem_statement.txt"):
            if os.path.exists(candidate):
                argv.append(f"--issue-text=@{candidate}")
                break
elif cmd == "lookup":
    if not arg:
        print("Usage: gt_lookup <symbol>"); sys.exit(0)
    fpath = find_symbol_file(arg)
    argv = [f"--db={DB}", f"--root={ROOT}", f"--function={arg}"]
    if fpath:
        argv += [f"--file={fpath}", "--reminder"]
elif cmd == "impact":
    if not arg:
        print("Usage: gt_impact <symbol>"); sys.exit(0)
    fpath = find_symbol_file(arg)
    argv = [f"--db={DB}", f"--root={ROOT}", f"--function={arg}"]
    if fpath:
        argv.append(f"--file={fpath}")  # impact = full caller/dependency view (no --reminder)
elif cmd == "check":
    if not arg:
        print("Usage: gt_check <file>"); sys.exit(0)
    argv = [f"--db={DB}", f"--root={ROOT}", f"--file={arg}", "--reminder"]
else:
    sys.exit(0)

result = subprocess.run([sys.executable, REAL] + argv, timeout=30)
sys.exit(result.returncode)
WRAPEOF

# Move real gt_intel.py aside and put wrapper in its place
if [ -f /tmp/gt_intel.py ] && [ ! -f /tmp/gt_intel_real.py ]; then
    mv /tmp/gt_intel.py /tmp/gt_intel_real.py
    cp /tmp/gt_intel_wrapper.py /tmp/gt_intel.py
    chmod +x /tmp/gt_intel.py
    echo "[GT] Budget+translate wrapper installed (orient=1, lookup=2, impact=2, check=3)" >> "$GT_LOG"
fi

# Stable gt_* command names for trajectory verification and prompt clarity.
cat > /usr/local/bin/gt_orient << 'CMDEOF'
#!/usr/bin/env bash
python3 /tmp/gt_intel.py orient "$@"
CMDEOF
cat > /usr/local/bin/gt_lookup << 'CMDEOF'
#!/usr/bin/env bash
python3 /tmp/gt_intel.py lookup "$@"
CMDEOF
cat > /usr/local/bin/gt_impact << 'CMDEOF'
#!/usr/bin/env bash
python3 /tmp/gt_intel.py impact "$@"
CMDEOF
cat > /usr/local/bin/gt_check << 'CMDEOF'
#!/usr/bin/env bash
python3 /tmp/gt_intel.py check "$@"
CMDEOF
chmod +x /usr/local/bin/gt_orient /usr/local/bin/gt_lookup /usr/local/bin/gt_impact /usr/local/bin/gt_check
echo "[GT] Installed gt_* command wrappers in /usr/local/bin" >> "$GT_LOG"

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
