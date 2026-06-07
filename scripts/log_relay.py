#!/usr/bin/env python3
"""Live log relay: tee stdin -> stdout (GHA log) AND -> an SSE stream tunnelled
public via ngrok, so Claude Code can `curl -N <url>` and read a run live with NO
polling.

Usage in a workflow step (NGROK_AUTHTOKEN secret in env):
    python -u run_eval.py 2>&1 | python -u scripts/log_relay.py

Design notes (fixes vs the naive version):
  * DEGRADES GRACEFULLY — if `ngrok` is not installed or NGROK_AUTHTOKEN is unset,
    it becomes a pure stdin->stdout passthrough. It can NEVER break the piped
    command, so it is safe to wire unconditionally into a production workflow.
  * DRAINS ngrok stdout forever — the naive version `break`s after reading the
    public URL and stops reading ngrok's stdout pipe; ngrok then blocks on its
    next log write and the tunnel stalls. Here a daemon thread keeps draining.
  * Tolerates a disconnecting SSE client (BrokenPipe) without killing the relay.
"""
import sys
import os
import json
import queue
import shutil
import threading
import subprocess
from http.server import HTTPServer, BaseHTTPRequestHandler

PORT = int(os.environ.get("LOG_RELAY_PORT", "8765"))
_q: "queue.Queue[str | None]" = queue.Queue()


class _SSEHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        while True:
            line = _q.get()
            if line is None:
                break
            try:
                self.wfile.write(f"data: {line}\n\n".encode())
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                break  # client went away; the relay keeps running

    def log_message(self, format, *args):  # noqa: A002 - silence default request logging
        pass


def _serve_sse():
    try:
        HTTPServer(("", PORT), _SSEHandler).serve_forever()
    except OSError as exc:
        print(f"[log_relay] SSE server could not start: {exc}", flush=True)


def _extract_url(d: dict) -> str:
    cand = d.get("url")
    if not cand and isinstance(d.get("obj"), dict):
        cand = d["obj"].get("public_url")
    return cand if isinstance(cand, str) and cand.startswith("http") else ""


def _start_tunnel():
    """Best-effort: start the SSE server + ngrok tunnel. No-op (passthrough) if
    ngrok or the auth token is missing."""
    if not shutil.which("ngrok") or not os.environ.get("NGROK_AUTHTOKEN"):
        print(
            "[log_relay] ngrok unavailable or NGROK_AUTHTOKEN unset — "
            "passthrough only (no live stream)",
            flush=True,
        )
        return

    threading.Thread(target=_serve_sse, daemon=True).start()

    try:
        ng = subprocess.Popen(
            ["ngrok", "http", str(PORT), "--log=stdout", "--log-format=json"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except OSError as exc:
        print(f"[log_relay] ngrok failed to start: {exc}", flush=True)
        return

    def _drain():
        url_found = False
        assert ng.stdout is not None
        for raw in ng.stdout:  # keep reading forever so ngrok never blocks
            if url_found:
                continue
            try:
                url = _extract_url(json.loads(raw))
            except (json.JSONDecodeError, AttributeError):
                continue
            if url:
                url_found = True
                print(f"\n>>> LIVE STREAM: curl -N '{url}'\n", flush=True)
                summary = os.environ.get("GITHUB_STEP_SUMMARY")
                if summary:
                    try:
                        with open(summary, "a", encoding="utf-8") as fh:
                            fh.write(f"## Live log stream\n```\ncurl -N '{url}'\n```\n")
                    except OSError:
                        pass

    threading.Thread(target=_drain, daemon=True).start()


def main():
    _start_tunnel()
    for line in sys.stdin:
        line = line.rstrip("\n")
        print(line, flush=True)  # keep the normal GHA log
        _q.put(line)
    _q.put(None)


if __name__ == "__main__":
    main()
