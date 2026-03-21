# GroundTruth v0.8 — Engineering Task Registry

Source: eng-review plan + CEO scope expansions (2026-03-20)
Branch: `improvement-v0.8`

## Status Key

- `DONE` — shipped and tested on this branch
- `TODO` — not started
- `PARTIAL` — started but incomplete

## Tasks

| # | Task | Status | Files | Anti-Goals |
|---|------|--------|-------|------------|
| 1 | Wire `_shared_state` into `ObligationEngine.infer()` | TODO | `validators/obligations.py` | Do not add new obligation kinds; only connect existing `_shared_state` logic |
| 2 | Extract `_deduplicate` helper from obligation engine | TODO | `validators/obligations.py` | Do not change obligation output format; internal refactor only |
| 3 | Obligation engine unit tests (all 4 kinds) | DONE | `tests/unit/test_obligations.py` | Do not test MCP layer here; pure engine tests |
| 4 | `handle_obligations` MCP handler tests | TODO | `tests/unit/test_tools.py` or new file | Do not duplicate obligation engine tests; test handler formatting and error paths |
| 5 | Remove `anthropic` from core dependencies | TODO | `pyproject.toml`, imports | Do not remove AI modules; only decouple the import so core works without anthropic installed |
| 6 | CityView watcher opt-in (non-default) | TODO | `cli/commands.py`, watcher code | Do not delete CityView; make it flag-gated, off by default |
| 7 | Split `tools.py` into per-tool modules | TODO | `mcp/tools.py` → `mcp/tools/` | Do not change tool names or MCP protocol; internal restructure only |
| 8 | Clean TODOS.md | TODO | `TODOS.md` | Do not delete items that are still valid; archive completed, remove stale |
| 9 | GitHub setup + CI | TODO | `.github/workflows/`, `README.md` | Do not add complex CI; Gate 0 (`pytest tests/unit/ -x -q`) + lint only |
| 10 | README rewrite for open-source launch | TODO | `README.md` | Do not overclaim beyond current proof; obligation engine is the wedge, not "full repo intelligence" |
| 11 | Enhanced index summary (coupling clusters + hotspots) | TODO | `cli/output.py`, `cli/commands.py` | Do not modify `render_risk_summary` signature; add new function alongside. Requires task #1 first |
| 12 | `check-diff` CLI — enhanced version | PARTIAL | `cli/commands.py`, `main.py` | Do not add AI calls; deterministic only. Basic version exists, needs `--base`, `--format json`, `--strict` |
| 13 | Django coupling demo in README | TODO | `README.md` | Do not fake output; must run GT against real Django checkout. Timebox 30 min, fallback to FastAPI |
| 14 | Obligation diff fixture corpus | DONE | `tests/fixtures/obligation_diffs/` | Do not hardcode expected output to pass; fixtures must reflect real obligation engine behavior |
| 15 | MCP tool exposure (4 handlers) | DONE | `mcp/tools.py`, `mcp/server.py` | Do not add new tool surfaces; expose existing obligation engine capabilities only |

## Dependency Order

```
#1 (_shared_state wiring) ← blocks #11 (enhanced index summary)
#12 (check-diff enhanced) ← blocks #13 (Django demo)
#3, #14, #15 ← already done, unblock everything else
#5, #6, #7, #8 ← independent, can parallelize
#9 ← should follow #10 (README)
```

## Gate Requirements

All tasks must pass Gate 0 (`python -m pytest tests/unit/ -x -q`) before merge.
Tasks #11, #12, #13 require Gate 1 (fixture validation) additionally.
