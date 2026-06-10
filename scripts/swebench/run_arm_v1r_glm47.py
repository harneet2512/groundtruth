#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Wrapper that monkey-patches LiteLLM at multiple insertion points to inject
X-Goog-User-Project header for Vertex AI partner models. Required for
glm-4.7-maas: without it the MaaS endpoint returns 403 PERMISSION_DENIED.
"""
import os
import sys

QUOTA_PROJECT = (
    os.environ.get("VERTEXAI_QUOTA_PROJECT")
    or os.environ.get("VERTEXAI_PROJECT")
    or "project-26227097-98fa-4016-a54"
)

DEBUG_LOG = "/tmp/glm47_wrapper_calls.log"

import litellm
import litellm.main as _ll_main


def _log(msg: str) -> None:
    try:
        with open(DEBUG_LOG, "a") as f:
            f.write(msg + "\n")
    except Exception:
        pass


_orig_completion = litellm.completion


def _patched_completion(*args, **kwargs):
    model = kwargs.get("model") or (args[0] if args else "")
    if not isinstance(model, str):
        return _orig_completion(*args, **kwargs)
    ml = model.lower()
    if "vertex_ai" in ml:
        eh = dict(kwargs.get("extra_headers") or {})
        eh.setdefault("X-Goog-User-Project", QUOTA_PROJECT)
        kwargs["extra_headers"] = eh
    if "glm" in ml:
        eb = dict(kwargs.get("extra_body") or {})
        eb.setdefault("chat_template_kwargs", {
            "enable_thinking": True,
            "clear_thinking": False,
        })
        kwargs["extra_body"] = eb
    elif "deepseek" in ml:
        eb = dict(kwargs.get("extra_body") or {})
        eb.setdefault("chat_template_kwargs", {"thinking": False})
        kwargs["extra_body"] = eb
        msgs = kwargs.get("messages") or []
        if isinstance(msgs, list) and msgs:
            kwargs["messages"] = [
                ({"role": "system", "content": ""} if m.get("role") == "system" else m)
                for m in msgs
            ]
    _log("litellm.completion injected for model=" + str(model)[:40])
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
        _log("vp.completion injected; headers keys=" + ",".join(headers.keys()))
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
            _log("httpx.send headers BEFORE: " + repr(dict(request.headers))[:400])
    except Exception as _e:
        _log("httpx patch error: " + repr(_e))
    resp = _orig_httpx_send(self, request, *args, **kwargs)
    try:
        if "aiplatform" in str(getattr(request, "url", "")):
            _log("httpx.send response status=" + str(resp.status_code) + " url=" + str(request.url)[:80])
            if resp.status_code != 200:
                _log("httpx.send response body=" + resp.text[:500])
    except Exception:
        pass
    return resp


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
                _log("Client.build_request injected X-Goog-User-Project for " + str(url)[:100])
    except Exception as _e:
        _log("build_request patch error: " + repr(_e))
    return _orig_build_request(self, method, url, *args, **kwargs)


_httpx.Client.build_request = _patched_build_request

import httpx._client as _httpx_client
_orig_async_send = _httpx_client.AsyncClient.send


async def _patched_async_send(self, request, *args, **kwargs):
    try:
        url = str(getattr(request, "url", ""))
        _log("httpx.AsyncClient.send url=" + url[:120])
        if "aiplatform" in url:
            request.headers["X-Goog-User-Project"] = QUOTA_PROJECT
    except Exception as _e:
        _log("httpx async patch error: " + repr(_e))
    return await _orig_async_send(self, request, *args, **kwargs)


_httpx_client.AsyncClient.send = _patched_async_send

# LiteLLM's own HTTPHandler — patch its post() to inject header for aiplatform URLs.
import litellm.llms.custom_httpx.http_handler as _ll_hh

_orig_hh_post = _ll_hh.HTTPHandler.post


def _patched_hh_post(self, url, *args, **kwargs):
    try:
        if "aiplatform" in str(url):
            headers = kwargs.get("headers") or {}
            if isinstance(headers, dict):
                headers["X-Goog-User-Project"] = QUOTA_PROJECT
                kwargs["headers"] = headers
                _log("HTTPHandler.post injected for url=" + str(url)[:100])
    except Exception as _e:
        _log("hh.post patch error: " + repr(_e))
    return _orig_hh_post(self, url, *args, **kwargs)


_ll_hh.HTTPHandler.post = _patched_hh_post


_orig_hh_post_with_retry_target = _ll_hh.HTTPHandler.post


_LAST_LLM_CALL_TS = [0.0]
_MIN_GAP_SEC = float(os.environ.get("VERTEX_MIN_REQ_GAP_SEC", "6"))


def _hh_post_with_retry(self, url, *args, **kwargs):
    import time as _t
    if "aiplatform" in str(url):
        elapsed = _t.time() - _LAST_LLM_CALL_TS[0]
        if elapsed < _MIN_GAP_SEC:
            _t.sleep(_MIN_GAP_SEC - elapsed)
    last_exc = None
    delays = [4, 10, 20, 40, 75, 120, 180, 240, 300, 420]
    for attempt, delay in enumerate([0] + delays):
        if delay:
            _t.sleep(delay)
        try:
            res = _orig_hh_post_with_retry_target(self, url, *args, **kwargs)
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
                _log("HTTPHandler.post throttle attempt=" + str(attempt) + " sleeping=" + str(next_delay))
                last_exc = e
                continue
            raise
    if last_exc is not None:
        raise last_exc


_ll_hh.HTTPHandler.post = _hh_post_with_retry

_orig_ahh_post = _ll_hh.AsyncHTTPHandler.post


async def _patched_ahh_post(self, url, *args, **kwargs):
    try:
        if "aiplatform" in str(url):
            headers = kwargs.get("headers") or {}
            if isinstance(headers, dict):
                headers["X-Goog-User-Project"] = QUOTA_PROJECT
                kwargs["headers"] = headers
                _log("AsyncHTTPHandler.post injected for url=" + str(url)[:100])
    except Exception as _e:
        _log("ahh.post patch error: " + repr(_e))
    return await _orig_ahh_post(self, url, *args, **kwargs)


_ll_hh.AsyncHTTPHandler.post = _patched_ahh_post

open(DEBUG_LOG, "w").write("wrapper init; QUOTA_PROJECT=" + QUOTA_PROJECT + "\n")
print(
    "[glm47-wrapper] LiteLLM patched: X-Goog-User-Project="
    + QUOTA_PROJECT
    + " for vertex_ai/* models",
    flush=True,
)

sys.argv[0] = os.path.expanduser("~/groundtruth/scripts/swebench/run_arm_v1r.py")
exec(
    compile(open(sys.argv[0]).read(), sys.argv[0], "exec"),
    {"__name__": "__main__", "__file__": sys.argv[0]},
)
