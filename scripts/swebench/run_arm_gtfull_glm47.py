#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GT-Full arm wrapper.

Composes:
  1. The header/retry monkey-patches from run_arm_v1r_glm47.py (Vertex MaaS
     X-Goog-User-Project + 403/429 retry-with-backoff).
  2. A get_config monkey-patch that enables MCP and registers the GroundTruth
     MCP server (host-side stdio) per task.
  3. An initialize_runtime wrapper that copies /tmp/graph.db from container
     to host after V1R-map's V3_INDEXER builds it, so the host-side MCP
     server can read it.

Then dispatches to run_arm_v1r.py with arm = V1R-map+hook (brief + indexer +
post-edit hook), which gives the GT-Full surface modulo MCP being added by
this wrapper.

Usage:
  V1R_LLM_CONFIG=glm47 V1R_TASK_FILTER=<id> python3 run_arm_gtfull_glm47.py V1R-map+hook
"""
from __future__ import annotations

import os
import sys

QUOTA_PROJECT = (
    os.environ.get("VERTEXAI_QUOTA_PROJECT")
    or os.environ.get("VERTEXAI_PROJECT")
    or "project-26227097-98fa-4016-a54"
)
DEBUG_LOG = "/tmp/gtfull_wrapper_calls.log"

# Host paths used to invoke the MCP server.
HOST_VENV_PYTHON = os.environ.get(
    "GTFULL_VENV_PYTHON", "/home/Lenovo/oh-benchmarks/.venv/bin/python3"
)
HOST_GT_SRC = os.environ.get("GTFULL_GT_SRC", "/home/Lenovo/groundtruth/src")
HOST_GRAPH_DB_DIR = os.environ.get("GTFULL_GRAPH_DB_DIR", "/tmp")


def _log(msg: str) -> None:
    try:
        with open(DEBUG_LOG, "a") as f:
            f.write(msg + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 1) LiteLLM monkey-patches: header + retry/throttle.
# ---------------------------------------------------------------------------
import litellm
import litellm.main as _ll_main


_orig_completion = litellm.completion


def _patched_completion(*args, **kwargs):
    model = kwargs.get("model") or (args[0] if args else "")
    if isinstance(model, str) and "vertex_ai" in model:
        eh = dict(kwargs.get("extra_headers") or {})
        eh.setdefault("X-Goog-User-Project", QUOTA_PROJECT)
        kwargs["extra_headers"] = eh
    return _orig_completion(*args, **kwargs)


litellm.completion = _patched_completion
_ll_main.completion = litellm.completion

_vp = _ll_main.vertex_partner_models_chat_completion
_orig_vp_completion = _vp.completion


def _patched_vp_completion(*args, **kwargs):
    headers = kwargs.get("headers")
    if headers is None:
        headers = {}
    if isinstance(headers, dict):
        headers.setdefault("X-Goog-User-Project", QUOTA_PROJECT)
        kwargs["headers"] = headers
    return _orig_vp_completion(*args, **kwargs)


_vp.completion = _patched_vp_completion

import httpx as _httpx

_orig_httpx_send = _httpx.Client.send


def _patched_httpx_send(self, request, *args, **kwargs):
    try:
        url = str(getattr(request, "url", ""))
        if "aiplatform" in url:
            request.headers["X-Goog-User-Project"] = QUOTA_PROJECT
            request.headers["x-goog-user-project"] = QUOTA_PROJECT
    except Exception:
        pass
    return _orig_httpx_send(self, request, *args, **kwargs)


_httpx.Client.send = _patched_httpx_send

_orig_build_request = _httpx.Client.build_request


def _patched_build_request(self, method, url, *args, **kwargs):
    try:
        if "aiplatform" in str(url):
            headers = kwargs.get("headers")
            if headers is None:
                headers = {}
            if isinstance(headers, dict):
                headers["X-Goog-User-Project"] = QUOTA_PROJECT
                kwargs["headers"] = headers
    except Exception:
        pass
    return _orig_build_request(self, method, url, *args, **kwargs)


_httpx.Client.build_request = _patched_build_request


# Retry with rate-limit gap.
import litellm.llms.custom_httpx.http_handler as _ll_hh

_orig_hh_post_target = _ll_hh.HTTPHandler.post
_LAST_LLM_CALL_TS = [0.0]
_MIN_GAP_SEC = float(os.environ.get("VERTEX_MIN_REQ_GAP_SEC", "6"))


def _hh_post_with_retry(self, url, *args, **kwargs):
    import time as _t
    if "aiplatform" in str(url):
        elapsed = _t.time() - _LAST_LLM_CALL_TS[0]
        if elapsed < _MIN_GAP_SEC:
            _t.sleep(_MIN_GAP_SEC - elapsed)
        headers = kwargs.get("headers") or {}
        if isinstance(headers, dict):
            headers["X-Goog-User-Project"] = QUOTA_PROJECT
            kwargs["headers"] = headers
    last_exc = None
    delays = [4, 10, 20, 40, 75, 120, 180, 240, 300, 420]
    for attempt, delay in enumerate([0] + delays):
        if delay:
            _t.sleep(delay)
        try:
            res = _orig_hh_post_target(self, url, *args, **kwargs)
            if "aiplatform" in str(url):
                _LAST_LLM_CALL_TS[0] = _t.time()
            return res
        except Exception as e:
            msg = str(e)
            status = None
            try:
                resp_attr = getattr(e, "response", None)
                if resp_attr is not None:
                    status = getattr(resp_attr, "status_code", None)
            except Exception:
                pass
            is_throttle = (
                ("403" in msg and "PERMISSION_DENIED" in msg)
                or ("429" in msg and "RESOURCE_EXHAUSTED" in msg)
                or status in (403, 429)
            )
            if is_throttle and "aiplatform" in str(url):
                next_delay = delays[attempt] if attempt < len(delays) else 600
                _log(
                    "HTTPHandler.post throttle attempt="
                    + str(attempt)
                    + " sleeping="
                    + str(next_delay)
                )
                last_exc = e
                continue
            raise
    if last_exc is not None:
        raise last_exc


_ll_hh.HTTPHandler.post = _hh_post_with_retry


# ---------------------------------------------------------------------------
# 2) Start GroundTruth MCP server as a host-side streamable-http daemon.
#    OH 0.44 only honors http/sse/shttp servers at runtime — the
#    stdio_servers config field is silently ignored. So we run our own
#    HTTP MCP server and point OH at it via sse_servers.
# 3) get_config monkey-patch: enable MCP + add sse server URL.
# 4) initialize_runtime wrapper: copy /tmp/graph.db from container to host
#    AFTER V3_INDEXER finishes (so the file actually exists when copied).
# ---------------------------------------------------------------------------
import subprocess
import time

GT_MCP_PORT = int(os.environ.get("GTFULL_MCP_PORT", "8799"))
GT_MCP_HOST = os.environ.get("GTFULL_MCP_HOST", "127.0.0.1")
_HOST_DB_PATH_BY_INSTANCE: dict = {}
_MCP_DAEMON_PROC: list = [None]
_CURRENT_DB_LINK = "/tmp/gt_current_graph.db"


def _build_placeholder_graph_db(path: str) -> None:
    """Build a minimal graph.db with the Go indexer schema so the MCP server
    can open it without --auto-index. Indexes /tmp (small, fast)."""
    indexer = os.environ.get(
        "GT_INDEXER_PATH", "/home/Lenovo/groundtruth/bin/gt-index-linux"
    )
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return
    if not os.path.isfile(indexer):
        _log("placeholder build skipped: gt-index-linux not found at " + indexer)
        return
    try:
        subprocess.run(
            [indexer, "-root", "/tmp", "-output", path],
            check=False,
            timeout=30,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _log(
            "placeholder graph.db built at " + path
            + " size=" + str(os.path.getsize(path) if os.path.exists(path) else 0)
        )
    except Exception as e:
        _log("placeholder build failed: " + repr(e))


def _start_mcp_daemon() -> None:
    """Spawn one host-side MCP HTTP server. It reads /tmp/gt_current_graph.db
    which we re-symlink per-task to point at the actual graph.db.
    """
    if _MCP_DAEMON_PROC[0] is not None:
        return
    placeholder = "/tmp/_gt_placeholder.db"
    _build_placeholder_graph_db(placeholder)
    if os.path.lexists(_CURRENT_DB_LINK):
        os.remove(_CURRENT_DB_LINK)
    os.symlink(placeholder, _CURRENT_DB_LINK)

    cmd = [
        HOST_VENV_PYTHON, "-m", "groundtruth.main", "serve",
        "--root", "/tmp",
        "--db", _CURRENT_DB_LINK,
        "--transport", "sse",
        "--host", GT_MCP_HOST,
        "--port", str(GT_MCP_PORT),
        "--no-auto-index",
    ]
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONPATH"] = HOST_GT_SRC + ":" + env.get("PYTHONPATH", "")
    log_path = "/tmp/gtfull_mcp_server.log"
    log_fh = open(log_path, "w")
    proc = subprocess.Popen(cmd, stdout=log_fh, stderr=subprocess.STDOUT, env=env)
    _MCP_DAEMON_PROC[0] = proc
    _log(
        "MCP daemon spawned pid=" + str(proc.pid)
        + " url=http://" + GT_MCP_HOST + ":" + str(GT_MCP_PORT) + "/sse"
        + " log=" + log_path
    )

    deadline = time.time() + 20
    import urllib.request as _ur
    while time.time() < deadline:
        if proc.poll() is not None:
            _log("MCP daemon DIED early; check " + log_path)
            return
        try:
            _ur.urlopen(
                "http://" + GT_MCP_HOST + ":" + str(GT_MCP_PORT) + "/sse",
                timeout=2,
            )
            _log("MCP daemon healthy")
            return
        except Exception:
            time.sleep(0.5)
    _log("MCP daemon did NOT respond within 20s")


def _install_oh_patches() -> None:
    import evaluation.benchmarks.swe_bench.run_infer as ri
    from openhands.core.config import AgentConfig
    from openhands.core.config.mcp_config import MCPConfig, MCPSSEServerConfig

    _orig_get_config = ri.get_config
    _orig_initialize_runtime = ri.initialize_runtime

    def _gt_full_get_config(instance, metadata):
        config = _orig_get_config(instance, metadata)
        instance_id = str(instance["instance_id"])
        host_db = os.path.join(HOST_GRAPH_DB_DIR, f"oh_graph_{instance_id}.db")
        _HOST_DB_PATH_BY_INSTANCE[instance_id] = host_db

        new_agent_cfg = AgentConfig(
            **{**config.get_agent_config().model_dump(), "enable_mcp": True}
        )
        config.set_agent_config(new_agent_cfg)
        config.mcp = MCPConfig(
            sse_servers=[
                MCPSSEServerConfig(
                    url="http://" + GT_MCP_HOST + ":" + str(GT_MCP_PORT) + "/sse",
                )
            ]
        )
        _log(
            "get_config patched: instance="
            + instance_id
            + " host_db="
            + host_db
            + " mcp_url=http://" + GT_MCP_HOST + ":" + str(GT_MCP_PORT) + "/sse"
        )
        return config

    def _gt_full_initialize_runtime(runtime, instance, metadata):
        result = _orig_initialize_runtime(runtime, instance, metadata)
        instance_id = str(instance["instance_id"])
        host_db = _HOST_DB_PATH_BY_INSTANCE.get(instance_id)
        if not host_db:
            _log("initialize_runtime: no host_db for " + instance_id)
            return result
        try:
            from openhands.events.action import CmdRunAction

            for _attempt in range(5):
                _check = CmdRunAction(
                    command="test -s /tmp/graph.db && echo READY || echo NOTYET"
                )
                _check.set_hard_timeout(10)
                _obs = runtime.run_action(_check)
                content = getattr(_obs, "content", "")
                if "READY" in content:
                    break
                time.sleep(2)
            else:
                _log("initialize_runtime: graph.db never appeared for " + instance_id)
                return result

            copied = False
            try:
                runtime.copy_from(path="/tmp/graph.db", host_path=host_db)
                copied = os.path.isfile(host_db) and os.path.getsize(host_db) > 0
            except Exception as e1:
                _log("initialize_runtime: copy_from failed: " + repr(e1))
            if not copied:
                _b64 = CmdRunAction(command="base64 -w0 /tmp/graph.db")
                _b64.set_hard_timeout(120)
                _b64_obs = runtime.run_action(_b64)
                payload = getattr(_b64_obs, "content", "")
                if payload:
                    import base64
                    body = "".join(payload.split())
                    try:
                        with open(host_db, "wb") as f:
                            f.write(base64.b64decode(body))
                        copied = True
                    except Exception as e2:
                        _log("initialize_runtime: base64 write failed: " + repr(e2))
            if copied:
                if os.path.lexists(_CURRENT_DB_LINK):
                    os.remove(_CURRENT_DB_LINK)
                os.symlink(host_db, _CURRENT_DB_LINK)
                _log(
                    "initialize_runtime: graph.db -> " + host_db
                    + " size=" + str(os.path.getsize(host_db))
                    + "; symlinked to " + _CURRENT_DB_LINK
                )
            else:
                _log("initialize_runtime: copy FAILED for " + instance_id)
        except Exception as e:
            _log("initialize_runtime patch error: " + repr(e))
        return result

    ri.get_config = _gt_full_get_config
    ri.initialize_runtime = _gt_full_initialize_runtime
    _log("OH patches installed: get_config + initialize_runtime")


# Defer OH patches until after run_arm_v1r imports them.
import builtins as _builtins

_orig_import = _builtins.__import__
_OH_PATCHED = [False]


def _import_hook(name, globals=None, locals=None, fromlist=(), level=0):
    mod = _orig_import(name, globals, locals, fromlist, level)
    if (
        not _OH_PATCHED[0]
        and (name == "evaluation.benchmarks.swe_bench.run_infer"
             or "evaluation.benchmarks.swe_bench.run_infer" in (fromlist or ()))
    ):
        # Mark BEFORE calling so re-imports inside _install_oh_patches don't recurse.
        _OH_PATCHED[0] = True
        try:
            _install_oh_patches()
        except Exception as e:
            _log("install_oh_patches failed: " + repr(e))
            _OH_PATCHED[0] = False
    return mod


_builtins.__import__ = _import_hook


# ---------------------------------------------------------------------------
# Init log + dispatch to run_arm_v1r.
# ---------------------------------------------------------------------------
open(DEBUG_LOG, "w").write(
    "gtfull_wrapper init t="
    + str(time.time())
    + " QUOTA_PROJECT="
    + QUOTA_PROJECT
    + " VENV="
    + HOST_VENV_PYTHON
    + " GT_SRC="
    + HOST_GT_SRC
    + "\n"
)

# Eagerly start the MCP daemon so it's ready before OH imports/initializes.
_start_mcp_daemon()

import atexit as _atexit


def _cleanup_mcp_at_exit() -> None:
    proc = _MCP_DAEMON_PROC[0]
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        _log("MCP daemon terminated at exit")


_atexit.register(_cleanup_mcp_at_exit)
print(
    "[gtfull-wrapper] LiteLLM patched + OH MCP injection ready "
    + "(quota_project="
    + QUOTA_PROJECT
    + ")",
    flush=True,
)

# Force the V1R-map+hook arm by default if user didn't override.
if len(sys.argv) <= 1:
    sys.argv.append("V1R-map+hook")
elif sys.argv[1] not in ("V1R-map", "V1R-map+hook", "V1", "BL"):
    print("[gtfull-wrapper] WARNING: unknown arm '" + sys.argv[1] + "'", file=sys.stderr)

sys.argv[0] = os.path.expanduser("~/groundtruth/scripts/swebench/run_arm_v1r.py")
exec(
    compile(open(sys.argv[0]).read(), sys.argv[0], "exec"),
    {"__name__": "__main__", "__file__": sys.argv[0]},
)
