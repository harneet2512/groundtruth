#!/usr/bin/env python3
"""
Standalone LSP edge resolution for SWE-bench containers.

Runs AFTER gt-index: installs pyright, resolves name-match edges via LSP,
writes lsp-verified edges back to graph.db.

Self-contained — no external imports beyond stdlib + pyright.

Usage:
    pip install pyright --break-system-packages -q
    python3 /tmp/lsp_resolve.py --db /tmp/gt_graph.db --root /testbed
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path


# ── LSP Client (inline, no imports) ─────────────────────────────────────

class LSPClient:
    """Minimal synchronous LSP client for textDocument/definition."""

    def __init__(self, cmd: str, args: list[str], workspace: str) -> None:
        self.workspace = os.path.abspath(workspace)
        self.cmd = cmd
        self.args = args
        self.proc: subprocess.Popen | None = None
        self._id = 0

    def start(self) -> bool:
        try:
            self.proc = subprocess.Popen(
                [self.cmd] + self.args,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=self.workspace,
            )
        except FileNotFoundError:
            return False

        # Initialize
        root_uri = self._uri(self.workspace)
        result = self._request("initialize", {
            "processId": os.getpid(),
            "rootUri": root_uri,
            "capabilities": {"textDocument": {"definition": {"dynamicRegistration": False}}},
            "workspaceFolders": [{"uri": root_uri, "name": "ws"}],
        })
        if result is None:
            self.stop()
            return False
        self._notify("initialized", {})
        time.sleep(1.0)  # let server settle
        return True

    def stop(self) -> None:
        if not self.proc:
            return
        try:
            self._request("shutdown", {})
            self._notify("exit", None)
        except Exception:
            pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        self.proc = None

    def open_file(self, rel_path: str) -> None:
        abs_path = os.path.join(self.workspace, rel_path)
        try:
            text = open(abs_path, "r", errors="replace").read()
        except OSError:
            return
        self._notify("textDocument/didOpen", {
            "textDocument": {
                "uri": self._uri(abs_path),
                "languageId": "python",
                "version": 1,
                "text": text,
            }
        })

    def definition(self, rel_path: str, line: int, col: int) -> dict | None:
        """Send textDocument/definition. line/col are 0-indexed."""
        abs_path = os.path.join(self.workspace, rel_path)
        result = self._request("textDocument/definition", {
            "textDocument": {"uri": self._uri(abs_path)},
            "position": {"line": line, "character": col},
        })
        if not result:
            return None
        locs = result if isinstance(result, list) else [result]
        if not locs:
            return None
        loc = locs[0]
        uri = loc.get("uri") or loc.get("targetUri", "")
        rng = loc.get("range") or loc.get("targetRange", {})
        tgt_line = rng.get("start", {}).get("line", 0)
        tgt_path = self._from_uri(uri)
        if tgt_path is None:
            return None
        return {"file": tgt_path, "line": tgt_line}

    # ── Protocol ──

    def _request(self, method: str, params: dict) -> object:
        self._id += 1
        rid = self._id
        self._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        deadline = time.time() + 30
        while time.time() < deadline:
            msg = self._read(timeout=30)
            if msg is None:
                return None
            if "id" not in msg:
                continue
            if msg.get("id") == rid:
                return None if "error" in msg else msg.get("result")
        return None

    def _notify(self, method: str, params: object) -> None:
        msg: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self._write(msg)

    def _write(self, msg: dict) -> None:
        assert self.proc and self.proc.stdin
        body = json.dumps(msg).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        try:
            self.proc.stdin.write(header + body)
            self.proc.stdin.flush()
        except OSError:
            pass

    def _read(self, timeout: float = 30) -> dict | None:
        assert self.proc and self.proc.stdout
        hdr = b""
        deadline = time.time() + timeout
        while b"\r\n\r\n" not in hdr and b"\n\n" not in hdr:
            if time.time() > deadline:
                return None
            b = self.proc.stdout.read(1)
            if not b:
                return None
            hdr += b
        cl = 0
        for line in hdr.decode("ascii", errors="replace").replace("\r\n", "\n").split("\n"):
            if line.lower().startswith("content-length:"):
                cl = int(line.split(":", 1)[1].strip())
        if cl == 0:
            return None
        body = self.proc.stdout.read(cl)
        if len(body) < cl:
            return None
        return json.loads(body)

    def _uri(self, path: str) -> str:
        p = path.replace("\\", "/")
        if not p.startswith("/"):
            p = "/" + p
        return "file://" + p

    def _from_uri(self, uri: str) -> str | None:
        if not uri.startswith("file://"):
            return None
        from urllib.parse import unquote
        p = unquote(uri[7:])
        if len(p) > 2 and p[0] == "/" and p[2] == ":":
            p = p[1:]
        p = p.replace("\\", "/")
        root = self.workspace.replace("\\", "/")
        if not root.endswith("/"):
            root += "/"
        if p.startswith(root):
            return p[len(root):]
        if p.lower().startswith(root.lower()):
            return p[len(root):]
        return None  # external (stdlib) — skip


# ── Resolution Pipeline ─────────────────────────────────────────────────

def find_accurate_column(filepath: str, line_1idx: int, callee: str, root: str) -> int:
    """Find callee name position in source line (not raw tree-sitter column)."""
    try:
        abs_path = os.path.join(root, filepath)
        lines = open(abs_path, "r", errors="replace").readlines()
        if line_1idx <= 0 or line_1idx > len(lines):
            return 0
        text = lines[line_1idx - 1]
        paren = text.find("(")
        end = paren if paren > 0 else len(text)
        col = text.rfind(callee, 0, end)
        return col if col >= 0 else 0
    except Exception:
        return 0


def resolve(db_path: str, root: str) -> dict:
    """Resolve name-match edges via pyright LSP."""
    if not shutil.which("pyright-langserver"):
        print("pyright-langserver not found — skipping LSP resolution", file=sys.stderr)
        return {"resolved": 0, "total": 0, "skipped": "no pyright"}

    conn = sqlite3.connect(db_path)

    # Read unresolved call sites — prioritize by caller connectivity
    # Resolve max 2000 to stay within timeout. Prioritize callers with most edges
    # (hotspot nodes produce the most valuable verified edges)
    total_unresolved = conn.execute("SELECT COUNT(*) FROM call_sites WHERE resolved = 0").fetchone()[0]
    sites = conn.execute(
        "SELECT cs.id, cs.caller_node_id, cs.callee_name, cs.line, cs.col, cs.file_path "
        "FROM call_sites cs "
        "JOIN (SELECT source_id, COUNT(*) as cnt FROM edges GROUP BY source_id) e "
        "ON cs.caller_node_id = e.source_id "
        "WHERE cs.resolved = 0 "
        "ORDER BY e.cnt DESC "
        "LIMIT 2000"
    ).fetchall()

    if not sites:
        print("No unresolved call sites", file=sys.stderr)
        conn.close()
        return {"resolved": 0, "total": 0}

    print(f"Resolving {len(sites)}/{total_unresolved} call sites via pyright (top by connectivity)...", file=sys.stderr)

    # Start LSP
    client = LSPClient("pyright-langserver", ["--stdio"], root)
    if not client.start():
        print("Failed to start pyright", file=sys.stderr)
        conn.close()
        return {"resolved": 0, "total": len(sites), "error": "pyright start failed"}

    # Open unique files
    opened = set()
    for _, _, _, _, _, fp in sites:
        if fp not in opened:
            client.open_file(fp)
            opened.add(fp)

    # Give pyright time to analyze
    time.sleep(3.0)

    resolved = 0
    lsp_failed = 0

    for cs_id, caller_id, callee_name, line, col, filepath in sites:
        # Fix column accuracy
        accurate_col = find_accurate_column(filepath, line, callee_name, root)

        try:
            result = client.definition(filepath, line - 1, accurate_col)
        except Exception:
            lsp_failed += 1
            continue

        if result is None:
            lsp_failed += 1
            continue

        # Match to node in graph.db
        target = conn.execute(
            "SELECT id FROM nodes "
            "WHERE file_path = ? AND start_line <= ? AND end_line >= ? "
            "AND label IN ('Function', 'Method', 'Class') "
            "ORDER BY (end_line - start_line) ASC LIMIT 1",
            (result["file"], result["line"] + 1, result["line"] + 1),
        ).fetchone()

        if target is None:
            # Try name match within the file
            target = conn.execute(
                "SELECT id FROM nodes WHERE file_path = ? AND name = ? LIMIT 1",
                (result["file"], callee_name),
            ).fetchone()

        if target is not None:
            conn.execute(
                "INSERT INTO edges (source_id, target_id, type, source_line, source_file, "
                "resolution_method, confidence) VALUES (?, ?, 'CALLS', ?, ?, 'lsp', 1.0)",
                (caller_id, target[0], line, filepath),
            )
            conn.execute("UPDATE call_sites SET resolved = 1 WHERE id = ?", (cs_id,))
            resolved += 1
        else:
            lsp_failed += 1

    conn.commit()

    # Downgrade remaining name-match edges for Python
    conn.execute(
        "UPDATE edges SET confidence = 0.2 "
        "WHERE resolution_method = 'name_match' "
        "AND source_id IN (SELECT id FROM nodes WHERE file_path LIKE '%.py')"
    )
    conn.commit()

    client.stop()
    conn.close()

    # Edge quality summary
    conn2 = sqlite3.connect(db_path)
    rows = conn2.execute(
        "SELECT resolution_method, COUNT(*) FROM edges GROUP BY resolution_method ORDER BY COUNT(*) DESC"
    ).fetchall()
    total_edges = sum(r[1] for r in rows)
    print(f"\nEdge quality after LSP:", file=sys.stderr)
    for method, count in rows:
        pct = 100 * count / total_edges if total_edges else 0
        print(f"  {method or 'unknown'}: {count} ({pct:.1f}%)", file=sys.stderr)
    conn2.close()

    stats = {"resolved": resolved, "failed": lsp_failed, "total": len(sites)}
    print(f"\nResolved {resolved}/{len(sites)} ({lsp_failed} failed)", file=sys.stderr)
    return stats


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--db", required=True)
    p.add_argument("--root", required=True)
    args = p.parse_args()
    stats = resolve(args.db, args.root)
    print(json.dumps(stats))
