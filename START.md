# START.md — Paste This Into Claude Code Every Time

```
Read these files in this exact order:
1. CLAUDE.md — architecture, schema, tools, coding standards
2. PRD.md — build spec, components, testing strategy
3. INITIAL_PROMPT.md — the build plan with all phases
4. PROGRESS.md — what's been done so far

Now do the following:

STEP 1: VERIFY PREVIOUS WORK
- If PROGRESS.md is empty or doesn't exist, skip to Step 2.
- If there IS previous work logged:
  a. Run the existing tests: pytest tests/ -v
  b. Run type checking: mypy src/ --strict
  c. Check if there are any failing tests or type errors
  d. If anything is broken, FIX IT before moving forward. Log what you fixed in PROGRESS.md.
  e. Summarize to me: what's been built, what's passing, what needed fixing.

STEP 2: IDENTIFY THE NEXT PHASE
- Look at INITIAL_PROMPT.md for the full build plan.
- Look at PROGRESS.md for what's already done.
- Identify the next incomplete phase. The phases in order are:
  1. Scaffold + LSP Client
  2. SQLite Store + Indexer
  3. Graph Traversal + Validators
  4. AI Layer
  5. MCP Server
  6. Fixture Projects + Cross-Language Tests
  7. Benchmarks + README
  8. SWE-bench Evaluation (stretch)
  9. Research Layer: Grounding Gap + Risk Scoring + Adaptive Briefing
  10. Research Experiments
  11. 3D Hallucination Risk Map
- Tell me which phase you're about to build and what it involves.

STEP 3: BUILD THE NEXT PHASE
- Follow the exact instructions for that phase in INITIAL_PROMPT.md.
- Follow the architecture in CLAUDE.md exactly.
- Follow the coding standards: Python 3.11+, type hints, Pydantic, structlog, parameterized SQL, pytest.
- Write tests for everything you build.
- Do NOT skip ahead to future phases. Do NOT build things not specified in the current phase.

STEP 4: VERIFY YOUR WORK
- Run: pytest tests/ -v
- Run: mypy src/ --strict
- Everything must pass. If it doesn't, fix it.

STEP 5: UPDATE PROGRESS.md
- Update PROGRESS.md with:
  - Last Updated: [current date/time]
  - Current Phase: [which phase was just completed]
  - Completed: [list everything that's been built, with checkmarks]
  - In Progress: [anything partially done]
  - Blockers: [any issues or things that need attention]
  - Next Up: [what the next phase is]
  - Decisions Made: [any deviations from CLAUDE.md and why]
  - Test Results: [how many tests pass, any failures]

Start now. Begin with Step 1.
```
