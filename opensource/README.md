# Open-source model experimentation

Branch: `opensource-experimentation`
Goal: get MiMo (cheap) and Nemotron-3-Super (free) working at full power so we have low-cost baselines alongside qwen3-coder.

## Why

qwen3-coder via OpenRouter (current working baseline): **$4.82 / 15-task n=15 smoke at 86.7% has_patch**. Works on OH out of the box (after caching_prompt fix).

The cheap models we want to leverage:
- **MiMo-V2-Flash on OR**: $0.10/$0.20 per 1M (~5× cheaper). Loops on OH today because OH drops `reasoning_content` field that MiMo's protocol requires.
- **Nemotron-3-Super on OR `:free`**: $0/call. JSON-format outputs (not OH text-mode). Designed for CORTEXA pipeline scaffold, not agent loops.

## Two parallel tracks

### Track A: MiMo + OH `reasoning_content` patch

Patch OH to preserve `reasoning_content` field across multi-turn tool calls. Per Xiaomi docs:
> "In thinking mode with multi-turn tool calls, the model returns a `reasoning_content` field alongside `tool_calls`. Users must persist all history reasoning_content in subsequent message arrays."

OH currently strips this field → MiMo loses chain-of-thought between turns → loops.

**Patch points** (in `oh-benchmarks/openhands/`):
1. `core/message.py` Message class — add `reasoning_content: str | None = None` field + serializer support
2. `llm/llm.py` `completion()` post-processing — extract `reasoning_content` from response into resulting Message
3. Wherever assistant Messages are constructed from prior actions/observations (TBD — find via grep) — preserve reasoning_content
4. `llm/fn_call_converter.py` (lines 565, 590, 826, 900) — keep reasoning_content when converting between native/non-native function-call formats
5. Verify `drop_params=True` doesn't strip it on the way out to litellm

**Test:** smoke babel-1141 with mimo, expect agent to NOT loop on the iter-22-23-24 pattern from prior diagnosis.

### Track B: Nemotron-CORTEXA pipeline

Use NVIDIA's official scaffold rather than forcing Nemotron into OH. CORTEXA architecture:
1. **File localization** — uses NV-EmbedCode to retrieve top-k candidate files. NVIDIA ships a precomputed pickle for SWE-bench Lite/Verified (707 instances) — **no NV-EmbedCode setup needed for those**.
2. **Entity localization** — agent narrows to specific functions/classes within candidate files.
3. **Repair** — LLM (Nemotron) generates patch from focused context.

**Setup**:
- Clone: `/home/Lenovo/opensource/Nemotron-CORTEXA/`
- Venv: `/home/Lenovo/opensource/Nemotron-CORTEXA/.venv` (separate from OH to avoid dependency pollution — CORTEXA broke OH's protobuf 6.x vs 5.x)
- Install: `pip install -e .` ✓ done
- Configure repair endpoint to OpenRouter `nvidia/nemotron-3-super-120b-a12b:free`

**Test plan**:
- Phase 1: pick a SWE-bench Verified instance that's IN the precomputed pickle, run end-to-end
- Phase 2: handle SWE-bench-Live (need to either generate localization at runtime via NV-EmbedCode, OR fall back to a simpler retriever like BM25 + AST grep)

## Status

| Track | Step | Status |
|---|---|---|
| MiMo | OH internals research | done |
| MiMo | Message class field add | TODO |
| MiMo | LLM response handler patch | TODO |
| MiMo | History reconstruction patch | TODO |
| MiMo | Smoke test on babel | TODO |
| CORTEXA | Repo clone | done |
| CORTEXA | Separate venv + install | running (background) |
| CORTEXA | Verify precomputed pickle has babel-1141 | running |
| CORTEXA | Configure repair endpoint to OR | TODO |
| CORTEXA | First end-to-end test | TODO |

## Cost ceiling

Both tracks must beat qwen3-coder's $4.82/n=15 to justify the work. If after first runs they don't, the leverage isn't there.
