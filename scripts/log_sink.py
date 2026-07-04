#!/usr/bin/env python3
"""Local sink for live GHA log streaming via a user-run ngrok tunnel.

Flow:  GHA runner's log_relay.py  --POST each line-->  https://<your>.ngrok-free.dev
       --(ngrok forwards)-->  localhost:8765 (THIS server)  --append-->  .tmp_gha_stream.log
       --(the agent tails the file)-->  live view, no polling.

Run on the machine whose `ngrok http 8765` is active:
    python scripts/log_sink.py
Then point the GHA run's GT_LOG_RELAY_URL at the public ngrok URL.
"""
import os
from http.server import HTTPServer, BaseHTTPRequestHandler

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.environ.get("GHA_SINK_FILE", os.path.join(ROOT, ".tmp_gha_stream.log"))
PORT = int(os.environ.get("LOG_SINK_PORT", "8765"))


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802 — GHA log_relay pushes lines here
        n = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(n).decode("utf-8", "replace") if n else ""
        with open(OUT, "a", encoding="utf-8") as fh:
            fh.write(body if body.endswith("\n") else body + "\n")
        self.send_response(204)
        self.end_headers()

    def do_GET(self):  # noqa: N802 — health / ngrok reachability check
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"log_sink alive\n")

    def log_message(self, format, *args):  # noqa: A002 — silence default request logging
        pass


if __name__ == "__main__":
    open(OUT, "w", encoding="utf-8").close()  # fresh per session
    print(f"log_sink listening on :{PORT} -> {OUT}", flush=True)
    HTTPServer(("", PORT), _Handler).serve_forever()
