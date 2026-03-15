# CURSOR_START.md — GroundTruth Cursor Handoff

> Paste this into Cursor when switching from Claude Code.

---

## Prompt

```
Read these files in order:
1. CLAUDE.md — architecture and coding standards
2. PRD.md — build spec and testing strategy
3. PROGRESS.md — what's been built so far, what's next

This project is being built by multiple LLMs (Claude Code and Cursor). PROGRESS.md is the source of truth for what's done and what's remaining.

Continue from wherever PROGRESS.md says we left off. Follow the architecture in CLAUDE.md exactly. If you need to deviate, document WHY in PROGRESS.md.

Key rules:
- Python 3.11+, type hints everywhere, mypy --strict must pass
- Pydantic for all data models
- pytest for all tests, in-memory SQLite for unit tests, mocked LLM
- structlog for logging
- All SQLite queries parameterized (no f-strings)
- Update PROGRESS.md after every significant milestone

After reading PROGRESS.md, tell me what's been completed and what you'll work on next. Then start building.
```

---

## When to Use Cursor vs Claude Code

**Use Claude Code for:**
- Initial scaffolding
- LSP client implementation (lots of async/protocol work)
- MCP server wiring
- Complex debugging across multiple files

**Use Cursor for:**
- Implementing individual components (validators, AI layer)
- Writing tests
- Creating fixture projects
- Writing benchmarks
- README and documentation

**The handoff rule:** Always update PROGRESS.md before switching tools. Always read PROGRESS.md when picking up work in either tool.
