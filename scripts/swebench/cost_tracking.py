"""Cost tracking + thinking-off guard for OpenHands smoke runs.

Imported at the TOP of oh_gt_full_wrapper.py BEFORE OpenHands starts so
litellm.register_model and litellm.success_callback take effect for every
LLM call in the agent loop.
"""
from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

import litellm

COST_LOG = os.getenv("GT_COST_LOG", "/tmp/litellm_costs.jsonl")
ABORT_FLAG = os.getenv("GT_ABORT_FLAG", "/tmp/gt_abort_reasoning.flag")

# Shared flag: set True by completion wrappers on task boundary, read+cleared by callback
_cost_callback_reset_pending: bool = False

_PRICING_V4FLASH = {
    "input_cost_per_token": 0.14e-6,
    "output_cost_per_token": 0.28e-6,
    "litellm_provider": "openrouter",
    "mode": "chat",
}
_PRICING_QWEN3 = {
    "input_cost_per_token": 0.22e-6,
    "output_cost_per_token": 1.80e-6,
    "litellm_provider": "openrouter",
    "mode": "chat",
    "max_input_tokens": 262144,
    "max_output_tokens": 65536,
    "max_tokens": 262144,
}
for name in ("openrouter/deepseek/deepseek-v4-flash", "openai/deepseek-v4-flash", "deepseek/deepseek-v4-flash"):
    litellm.model_cost[name] = _PRICING_V4FLASH
_PRICING_QWEN3_OAI = {**_PRICING_QWEN3, "litellm_provider": "openai"}
litellm.model_cost["openrouter/qwen/qwen3-coder"] = _PRICING_QWEN3
litellm.model_cost["qwen/qwen3-coder"] = _PRICING_QWEN3
litellm.model_cost["qwen3-coder"] = _PRICING_QWEN3
litellm.model_cost["openai/qwen3-coder"] = _PRICING_QWEN3_OAI
_PRICING_VERTEX_QWEN3 = {
    "input_cost_per_token": 0.45e-6,
    "output_cost_per_token": 1.80e-6,
    "litellm_provider": "vertex_ai",
    "mode": "chat",
    "max_input_tokens": 262144,
    "max_output_tokens": 65536,
    "max_tokens": 262144,
}
litellm.model_cost["vertex_ai/qwen/qwen3-coder-480b-a35b-instruct-maas"] = _PRICING_VERTEX_QWEN3
litellm.model_cost["openai/qwen3-coder-480b-a35b-instruct-maas"] = _PRICING_VERTEX_QWEN3
litellm.model_cost["openai/qwen/qwen3-coder-480b-a35b-instruct-maas"] = _PRICING_VERTEX_QWEN3


def _manual_token_cost(model: Any, prompt_tokens: Any, completion_tokens: Any) -> float | None:
    """Deterministic per-token cost from the registered ``litellm.model_cost`` map.

    ``litellm.completion_cost`` / ``cost_per_token`` raise
    ``This model isn't mapped yet`` for ``deepseek/deepseek-v4-flash`` (and other
    custom-provider models): litellm resolves ``custom_llm_provider=deepseek`` and
    consults its built-in price table, which IGNORES our ``model_cost[...] =`` /
    ``register_model`` entries — verified: even ``register_model`` fails. The result
    was ``cost=None`` → ``$0.0000`` telemetry across the whole run (KINK #6).

    This fallback reads the SAME pricing we already registered and multiplies it by
    the token usage litellm hands us. Model/provider-agnostic: it tries the full
    name, the provider-stripped basename, then any registered key whose basename
    matches — so any model present in ``litellm.model_cost`` is priced, with no
    hardcoded per-task value. Returns ``None`` only when the model is genuinely
    unregistered (honest: better $0-and-known-absent than a fabricated number).
    """
    if not isinstance(model, str) or not model:
        return None
    try:
        in_t = int(prompt_tokens or 0)
        out_t = int(completion_tokens or 0)
    except Exception:
        return None
    mc = getattr(litellm, "model_cost", {}) or {}
    base = model.split("/")[-1]
    candidates = [model, base]
    # Fall back to any registered key that shares the same basename (e.g. the
    # response says "deepseek-v4-flash" but we registered "deepseek/deepseek-v4-flash").
    for _k in mc:
        if isinstance(_k, str) and _k.split("/")[-1] == base and _k not in candidates:
            candidates.append(_k)
    for key in candidates:
        entry = mc.get(key)
        if isinstance(entry, dict) and (
            "input_cost_per_token" in entry or "output_cost_per_token" in entry
        ):
            return (
                in_t * float(entry.get("input_cost_per_token", 0.0) or 0.0)
                + out_t * float(entry.get("output_cost_per_token", 0.0) or 0.0)
            )
    return None


# Rate limiter: matches the old LiteLLM proxy's rpm:12 setting.
# Without this, 6 concurrent workers overwhelm Vertex MaaS quota.
import threading
_rpm_limit = int(os.environ.get("GT_RPM_LIMIT", "20"))
_call_timestamps: list[float] = []
_rate_lock = threading.Lock()

def _rate_limit_wait() -> None:
    if _rpm_limit <= 0:
        return
    window = 60.0
    with _rate_lock:
        now = time.time()
        _call_timestamps[:] = [t for t in _call_timestamps if now - t < window]
        if len(_call_timestamps) >= _rpm_limit:
            sleep_for = _call_timestamps[0] + window - now + 0.1
            if sleep_for > 0:
                time.sleep(sleep_for)
        _call_timestamps.append(time.time())

# Monkey-patch: inject sampling params for Vertex qwen3 (top_k, repetition_penalty).
# These match the v1.0.5 config that produced resolves on GCP.
_orig_completion = litellm.completion

def _vertex_params_completion(*args: Any, **kwargs: Any) -> Any:
    _rate_limit_wait()
    model = kwargs.get("model") or (args[0] if args else "")
    matched = isinstance(model, str) and "qwen3-coder" in model.lower() and "480b" in model.lower()
    if matched:
        eb = dict(kwargs.get("extra_body") or {})
        eb.setdefault("top_k", 20)
        eb.setdefault("repetition_penalty", 1.05)
        kwargs["extra_body"] = eb
    if isinstance(model, str) and "deepseek" in model.lower():
        eb = dict(kwargs.get("extra_body") or {})
        eb.setdefault("thinking", {"type": "disabled"})
        kwargs["extra_body"] = eb
    if isinstance(model, str) and model.startswith("vertex_ai/"):
        kwargs.setdefault("vertex_project", os.environ.get("VERTEX_AI_PROJECT") or os.environ.get("GCP_PROJECT", ""))
        kwargs.setdefault("vertex_location", os.environ.get("VERTEX_AI_LOCATION", "global"))
    # Bug fix: reset call counter on task boundary (new task = system+user only)
    global _cost_callback_reset_pending
    msgs = kwargs.get("messages", [])
    if len(msgs) == 2:  # New task starting (system + user only)
        _vertex_params_completion._log_n = 0
        _cost_callback_reset_pending = True
    _n = getattr(_vertex_params_completion, "_log_n", 0) + 1
    _vertex_params_completion._log_n = _n
    _max_calls = int(os.environ.get("GT_MAX_LLM_CALLS", "150"))
    if _n > _max_calls:
        print(f"[GT_COST_GUARD] Hard LLM call cap reached ({_n}/{_max_calls}). Aborting.", flush=True)
        raise RuntimeError(f"GT_COST_GUARD: LLM call cap {_max_calls} exceeded")
    if _n <= 3:
        msgs = kwargs.get("messages", [])
        safe = {k: (v if k != "api_key" else "***") for k, v in kwargs.items() if k != "messages"}
        safe["_matched"] = matched
        safe["_messages_count"] = len(msgs)
        safe["_messages_roles"] = [m.get("role", "?") for m in msgs]
        sys_content = msgs[0].get("content", "") if msgs else ""
        if isinstance(sys_content, list):
            flat = " ".join(str(c.get("text", c)) for c in sys_content if isinstance(c, dict))
            safe["_system_prompt_type"] = "list"
        else:
            flat = str(sys_content)
            safe["_system_prompt_type"] = "str"
        safe["_system_prompt_true_len"] = len(flat)
        safe["_system_prompt_first500"] = flat[:500]
        user_msgs = [m for m in msgs if m.get("role") == "user"]
        if user_msgs:
            uc = user_msgs[0].get("content", "")
            safe["_first_user_msg_first300"] = (str(uc) if not isinstance(uc, list) else str(uc[0]))[:300]
        safe["_condenser_mode"] = os.environ.get("EVAL_CONDENSER", "none")
        safe["_total_messages_chars"] = sum(len(str(m.get("content", ""))) for m in msgs)
        safe["_tools_count"] = len(kwargs.get("tools") or [])
        safe["_tool_names"] = [t.get("function", {}).get("name", "?") for t in (kwargs.get("tools") or [])]
        print(f"[GT_PAYLOAD] sync call={_n} {json.dumps(safe, default=str)}", flush=True)
        _dbg = os.environ.get("GT_DEBUG_DIR")
        if _dbg:
            os.makedirs(_dbg, exist_ok=True)
            with open(os.path.join(_dbg, "payload.jsonl"), "a") as _f:
                _f.write(json.dumps(safe, default=str) + "\n")
    # Inject all 4 GT tools with per-tool budgets (SWE-agent proven config)
    _gt_tool_calls = getattr(_vertex_params_completion, "_gt_tool_calls", {})
    _GT_TOOLS = [
        {
            "name": "gt_query",
            "budget_env": "GT_QUERY_BUDGET", "budget_default": 2,
            "description": (
                "PREFER THIS OVER GREP. Pre-indexed whole-repo call graph query. "
                "Returns ALL callers with exact line numbers, ALL test assertions covering this symbol, "
                "return type contract, and blast radius — in one call. grep misses dynamic dispatch "
                "and cross-module calls. This is complete and deterministic. "
                "Use BEFORE editing to understand what your change will break."
            ),
            "params": {"symbol": {"type": "string", "description": "Function or class name (e.g. 'update_cookiecutter_cache')"}},
            "required": ["symbol"],
        },
        {
            "name": "gt_search",
            "budget_env": "GT_SEARCH_BUDGET", "budget_default": 2,
            "description": (
                "Search the pre-indexed call graph for symbols matching a name. "
                "Returns functions/classes/methods with file paths, signatures, and caller counts. "
                "Faster than grep -r for finding WHERE a symbol is defined and HOW it connects to the rest of the codebase."
            ),
            "params": {"pattern": {"type": "string", "description": "Search pattern (e.g. 'serialize' or 'ProfileArgs')"}},
            "required": ["pattern"],
        },
        {
            "name": "gt_navigate",
            "budget_env": "GT_NAVIGATE_BUDGET", "budget_default": 2,
            "description": (
                "Show all files connected to a given file via the call graph. "
                "Returns: files that call INTO this file (dependents you might break), "
                "files this file calls OUT to (dependencies), and files that import from it. "
                "Use to understand scope: 'if I change this file, what else needs to change?'"
            ),
            "params": {"file": {"type": "string", "description": "File path to navigate from (e.g. 'src/commands/base.py')"}},
            "required": ["file"],
        },
        {
            "name": "gt_validate",
            "budget_env": "GT_VALIDATE_BUDGET", "budget_default": 3,
            "description": (
                "MUST RUN BEFORE FINISHING. Validates your edits against the call graph. "
                "Catches: hallucinated imports, signature changes that break callers, "
                "removed parameters still expected by tests, contract violations. "
                "Finds bugs BEFORE the test suite runs."
            ),
            "params": {"file": {"type": "string", "description": "Path to the edited file"}},
            "required": ["file"],
        },
    ]
    _native_tools_enabled = os.environ.get("GT_NATIVE_TOOLS", "1") == "1" and not os.environ.get("GT_BASELINE")
    if _native_tools_enabled and not kwargs.get("tools"):
        print("[GT_META] tool_injection_skip: reason=no_tools_payload", flush=True)
    if _native_tools_enabled and kwargs.get("tools"):
        tools = list(kwargs.get("tools") or [])
        existing = {t.get("function", {}).get("name") for t in tools}
        for gt_tool in _GT_TOOLS:
            name = gt_tool["name"]
            budget = int(os.environ.get(gt_tool["budget_env"], str(gt_tool["budget_default"])))
            used = _gt_tool_calls.get(name, 0)
            if name not in existing and used < budget:
                tools.append({
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": f"{gt_tool['description']} (budget: {budget - used} remaining)",
                        "parameters": {"type": "object", "properties": gt_tool["params"], "required": gt_tool["required"]},
                    },
                })
        kwargs["tools"] = tools
        if len(tools) > len(existing):
            print(f"[GT_META] tool_injection: injected {len(tools) - len(existing)} GT tools, total={len(tools)} names={[t['function']['name'] for t in tools if t['function']['name'].startswith('gt_')]}", flush=True)
    # Call the real LLM
    result = _orig_completion(*args, **kwargs)
    # Rewrite GT tool calls → execute_bash so OH can handle them
    try:
        choices = getattr(result, "choices", []) or []
        for choice in choices:
            msg = getattr(choice, "message", None)
            if not msg:
                continue
            tool_calls = getattr(msg, "tool_calls", None) or []
            for tc in tool_calls:
                fn = getattr(tc, "function", None)
                if not fn:
                    continue
                fn_name = getattr(fn, "name", "")
                _gt_names = {"gt_query", "gt_search", "gt_navigate", "gt_validate"}
                if fn_name in _gt_names:
                    _gt_tool_calls[fn_name] = _gt_tool_calls.get(fn_name, 0) + 1
                    _vertex_params_completion._gt_tool_calls = _gt_tool_calls
                    import json as _j_tc
                    try:
                        fn_args = _j_tc.loads(getattr(fn, "arguments", "{}") or "{}")
                    except Exception:
                        fn_args = {}
                    arg = fn_args.get("symbol") or fn_args.get("pattern") or fn_args.get("file") or "unknown"
                    bash_cmd = f"{fn_name} {arg}"
                    fn.name = "execute_bash"
                    fn.arguments = _j_tc.dumps({"command": bash_cmd})
                    print(f"[GT_META] tool_rewrite: {fn_name}→execute_bash cmd='{bash_cmd}' count={_gt_tool_calls[fn_name]}", flush=True)
    except Exception as _tc_exc:
        print(f"[GT_META] tool_rewrite_error: {_tc_exc}", flush=True)
    return result

litellm.completion = _vertex_params_completion

_orig_acompletion = getattr(litellm, "acompletion", None)
if _orig_acompletion is not None:
    _saved_acompletion = _orig_acompletion

    async def _vertex_params_acompletion(*args: Any, **kwargs: Any) -> Any:
        _rate_limit_wait()
        model = kwargs.get("model") or (args[0] if args else "")
        matched = isinstance(model, str) and "qwen3-coder" in model.lower() and "480b" in model.lower()
        if matched:
            eb = dict(kwargs.get("extra_body") or {})
            eb.setdefault("top_k", 20)
            eb.setdefault("repetition_penalty", 1.05)
            kwargs["extra_body"] = eb
        if isinstance(model, str) and "deepseek" in model.lower():
            eb = dict(kwargs.get("extra_body") or {})
            eb.setdefault("thinking", {"type": "disabled"})
            kwargs["extra_body"] = eb
        if isinstance(model, str) and model.startswith("vertex_ai/"):
            kwargs.setdefault("vertex_project", os.environ.get("VERTEX_AI_PROJECT") or os.environ.get("GCP_PROJECT", ""))
            kwargs.setdefault("vertex_location", os.environ.get("VERTEX_AI_LOCATION", "global"))
        # Bug fix: reset call counter on task boundary (new task = system+user only)
        global _cost_callback_reset_pending
        msgs = kwargs.get("messages", [])
        if len(msgs) == 2:  # New task starting (system + user only)
            _vertex_params_acompletion._log_n = 0
            _cost_callback_reset_pending = True
        _n = getattr(_vertex_params_acompletion, "_log_n", 0) + 1
        _vertex_params_acompletion._log_n = _n
        _max_calls = int(os.environ.get("GT_MAX_LLM_CALLS", "150"))
        if _n > _max_calls:
            print(f"[GT_COST_GUARD] Hard LLM call cap reached ({_n}/{_max_calls}). Aborting.", flush=True)
            raise RuntimeError(f"GT_COST_GUARD: LLM call cap {_max_calls} exceeded")
        if _n <= 3:
            msgs = kwargs.get("messages", [])
            safe = {k: (v if k != "api_key" else "***") for k, v in kwargs.items() if k != "messages"}
            safe["_matched"] = matched
            safe["_messages_count"] = len(msgs)
            safe["_messages_roles"] = [m.get("role", "?") for m in msgs]
            sys_content = msgs[0].get("content", "") if msgs else ""
            if isinstance(sys_content, list):
                flat = " ".join(str(c.get("text", c)) for c in sys_content if isinstance(c, dict))
                safe["_system_prompt_type"] = "list"
            else:
                flat = str(sys_content)
                safe["_system_prompt_type"] = "str"
            safe["_system_prompt_true_len"] = len(flat)
            safe["_system_prompt_first500"] = flat[:500]
            user_msgs = [m for m in msgs if m.get("role") == "user"]
            if user_msgs:
                uc = user_msgs[0].get("content", "")
                safe["_first_user_msg_first300"] = (str(uc) if not isinstance(uc, list) else str(uc[0]))[:300]
            safe["_condenser_mode"] = os.environ.get("EVAL_CONDENSER", "none")
            safe["_total_messages_chars"] = sum(len(str(m.get("content", ""))) for m in msgs)
            safe["_tools_count"] = len(kwargs.get("tools") or [])
            safe["_tool_names"] = [t.get("function", {}).get("name", "?") for t in (kwargs.get("tools") or [])]
            print(f"[GT_PAYLOAD] async call={_n} {json.dumps(safe, default=str)}", flush=True)
            _dbg = os.environ.get("GT_DEBUG_DIR")
            if _dbg:
                os.makedirs(_dbg, exist_ok=True)
                with open(os.path.join(_dbg, "payload.jsonl"), "a") as _f:
                    _f.write(json.dumps(safe, default=str) + "\n")
        # Inject all 4 GT tools into async path
        _native_tools_enabled_a = os.environ.get("GT_NATIVE_TOOLS", "1") == "1" and not os.environ.get("GT_BASELINE")
        if _native_tools_enabled_a and not kwargs.get("tools"):
            print("[GT_META] async_tool_injection_skip: reason=no_tools_payload", flush=True)
        if _native_tools_enabled_a and kwargs.get("tools"):
            tools = list(kwargs.get("tools") or [])
            existing = {t.get("function", {}).get("name") for t in tools}
            _gt_tc = getattr(_vertex_params_completion, "_gt_tool_calls", {})
            for gt_tool in _GT_TOOLS:
                name = gt_tool["name"]
                budget = int(os.environ.get(gt_tool["budget_env"], str(gt_tool["budget_default"])))
                used = _gt_tc.get(name, 0)
                if name not in existing and used < budget:
                    tools.append({
                        "type": "function",
                        "function": {
                            "name": name,
                            "description": f"{gt_tool['description']} (budget: {budget - used} remaining)",
                            "parameters": {"type": "object", "properties": gt_tool["params"], "required": gt_tool["required"]},
                        },
                    })
            kwargs["tools"] = tools
            if len(tools) > len(existing):
                print(f"[GT_META] async_tool_injection: injected {len(tools) - len(existing)} GT tools, total={len(tools)}", flush=True)
        result = await _saved_acompletion(*args, **kwargs)
        # Rewrite all 4 GT tool calls in async path
        try:
            _gt_names_a = {"gt_query", "gt_search", "gt_navigate", "gt_validate"}
            for choice in (getattr(result, "choices", []) or []):
                msg = getattr(choice, "message", None)
                if not msg:
                    continue
                for tc in (getattr(msg, "tool_calls", None) or []):
                    fn = getattr(tc, "function", None)
                    if fn and getattr(fn, "name", "") in _gt_names_a:
                        fn_name = fn.name
                        _gt_tc = getattr(_vertex_params_completion, "_gt_tool_calls", {})
                        _gt_tc[fn_name] = _gt_tc.get(fn_name, 0) + 1
                        _vertex_params_completion._gt_tool_calls = _gt_tc
                        import json as _j_atc
                        try:
                            fn_args = _j_atc.loads(getattr(fn, "arguments", "{}") or "{}")
                        except Exception:
                            fn_args = {}
                        arg = fn_args.get("symbol") or fn_args.get("pattern") or fn_args.get("file") or "unknown"
                        bash_cmd = f"{fn_name} {arg}"
                        fn.name = "execute_bash"
                        fn.arguments = _j_atc.dumps({"command": bash_cmd})
                        print(f"[GT_META] async_tool_rewrite: {fn_name}→execute_bash cmd='{bash_cmd}'", flush=True)
        except Exception as _atc_exc:
            print(f"[GT_META] async_tool_rewrite_error: {_atc_exc}", flush=True)
        return result

    litellm.acompletion = _vertex_params_acompletion


def _detect_reasoning(resp: Any) -> bool:
    try:
        msg = getattr(resp.choices[0], "message", None) if resp.choices else None
        if not msg:
            return False
        if getattr(msg, "reasoning_content", None):
            return True
        if getattr(msg, "reasoning_details", None):
            return True
        return "<think>" in (getattr(msg, "content", "") or "")
    except Exception:
        return False


def _cost_callback(kwargs, completion_response, start_time, end_time):
    try:
        cost = None
        try:
            cost = litellm.completion_cost(completion_response=completion_response)
        except Exception as e:
            print(f"[GT_COST] completion_cost failed: {e}", flush=True)

        usage = getattr(completion_response, "usage", None)
        # KINK #6: litellm.completion_cost raises "model isn't mapped" for
        # deepseek-v4-flash (and other custom-provider models) even though we
        # registered pricing — so cost stayed None and telemetry read $0. Recover
        # deterministically from the registered per-token pricing using the token
        # usage litellm gives us. Only when completion_cost gave nothing.
        if cost is None and usage is not None:
            _fb_model = kwargs.get("model") or getattr(completion_response, "model", None)
            _fb_cost = _manual_token_cost(
                _fb_model,
                getattr(usage, "prompt_tokens", None),
                getattr(usage, "completion_tokens", None),
            )
            if _fb_cost is not None:
                cost = _fb_cost
                print(
                    f"[GT_COST] completion_cost fallback: priced {_fb_model} "
                    f"from registry cost=${_fb_cost:.6f}",
                    flush=True,
                )
        has_reasoning = _detect_reasoning(completion_response)

        gen_id = getattr(completion_response, "id", None)
        or_cost = None
        or_cached = None
        if gen_id and os.environ.get("OPENROUTER_KEY"):
            try:
                import urllib.request
                req = urllib.request.Request(
                    f"https://openrouter.ai/api/v1/generation?id={gen_id}",
                    headers={"Authorization": f"Bearer {os.environ['OPENROUTER_KEY']}"},
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    gd = json.loads(resp.read()).get("data", {})
                    or_cost = gd.get("total_cost")
                    or_cached = gd.get("native_tokens_cached")
            except Exception as _or_exc:
                print(f"[GT_COST] openrouter_api_error: {type(_or_exc).__name__}", flush=True)

        record = {
            "ts": time.time(),
            "model": kwargs.get("model"),
            "input_tokens": getattr(usage, "prompt_tokens", None) if usage else None,
            "output_tokens": getattr(usage, "completion_tokens", None) if usage else None,
            "cost_usd_litellm": cost,
            "cost_usd_openrouter": or_cost,
            "cached_tokens": or_cached,
            "openrouter_gen_id": gen_id,
            "has_reasoning": has_reasoning,
        }
        with open(COST_LOG, "a") as f:
            f.write(json.dumps(record) + "\n")

        # Live cost visibility — prints to GHA log in real-time
        # Bug fix: reset per-task counters when completion wrapper detected a new task
        global _cost_callback_reset_pending
        if _cost_callback_reset_pending:
            _cost_callback._n = 0
            _cost_callback._total = 0.0
            _cost_callback_reset_pending = False
        _call_num = getattr(_cost_callback, "_n", 0) + 1
        _cost_callback._n = _call_num
        _running = getattr(_cost_callback, "_total", 0.0) + (cost or 0)
        _cost_callback._total = _running
        in_t = getattr(usage, "prompt_tokens", 0) if usage else 0
        out_t = getattr(usage, "completion_tokens", 0) if usage else 0
        cached_t = record.get("cached_tokens") or 0
        cost_line = f"[GT_COST] call={_call_num} in={in_t} out={out_t} cached={cached_t} cost=${cost or 0:.4f} total=${_running:.4f} reasoning={has_reasoning}"
        print(cost_line, flush=True)
        _dbg = os.environ.get("GT_DEBUG_DIR")
        if _dbg:
            os.makedirs(_dbg, exist_ok=True)
            with open(os.path.join(_dbg, "cost.jsonl"), "a") as _f:
                _f.write(json.dumps(record, default=str) + "\n")

        if has_reasoning:
            print(
                "[GT_THINK_GUARD] REASONING DETECTED — abort flag written",
                flush=True,
                file=sys.stderr,
            )
            with open(ABORT_FLAG, "w") as f:
                f.write(json.dumps(record))
    except Exception as e:
        print(f"[GT_COST] callback exception: {e}", flush=True)


if _cost_callback not in litellm.success_callback:
    litellm.success_callback.append(_cost_callback)
