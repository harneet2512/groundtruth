# Research Log

Research fit checks for stabilization bug fixes.
Each entry documents whether a fix requires research backing or is a pure engineering invariant.

Format per entry:
- Bug ID
- Implementation question
- Search terms used
- Sources found (title, venue, year, URL)
- Decision: RESEARCH_MATCH | RESEARCH_PARTIAL | ENGINEERING_INVARIANT | RESEARCH_BLOCKED
- Why this source applies
- Why this source might not apply
- Implementation constraint derived from research

---

## Research Check: L6_PRESUBMIT actionability

Bug class: F2 (finish evidence too late)
Implementation question: When should pre-submit verification evidence (caller contracts, test suggestions) be delivered so the agent can act before finishing?

Search terms used: "pre-submit verification evidence timing agentic code repair SWE-bench 2024 2025", "CodeR multi-agent task graph test verification before submission"

Sources found:
1. CodeR: Issue Resolving with Multi-Agent and Task Graphs (Chen et al., arXiv 2406.01304, June 2024)
   URL: https://arxiv.org/abs/2406.01304
   CodeR's task graph has explicit test→verify→submit stages. Verification runs BEFORE submission, not after. On SWE-bench Lite: 28.33% resolution.

2. Coding Agents Don't Know When to Act (Gloaguen et al., arXiv 2605.07769, May 2026)
   URL: https://arxiv.org/abs/2605.07769
   Agents propose undesirable changes 35-65% of the time. Key finding: explicit instruction to reproduce/verify BEFORE patching partially addresses this. Verification before submission is critical.

3. Verify Before You Fix (arXiv 2604.10800, April 2026)
   URL: https://arxiv.org/abs/2604.10800
   Strict invariant: "no repair action is taken without execution-based confirmation." Three reasoning stages before repair.

4. TDFlow: Agentic Workflows for Test Driven Development (arXiv 2510.23761, 2025)
   URL: https://arxiv.org/abs/2510.23761
   Tests written before code; verification is continuous, not post-hoc.

Decision: RESEARCH_MATCH

Why these sources apply:
All four sources establish that verification/test evidence must be delivered BEFORE the agent's final action, not after. CodeR and TDFlow make verification an explicit pre-submission stage. "Verify Before You Fix" goes further — no action without verification. The common constraint: the agent must have at least one step available after receiving verification evidence.

Implementation constraint derived from research:
- L6 review must fire BEFORE AgentFinishAction, at a point where the agent has steps remaining
- The existing L6 early review hook (line 4302, fires after source edit) is the correct delivery point
- Moving finish handler content (test suggestions) into this hook is an engineering move, not a new heuristic
- Gate change from edit_count>=2 to edit_count>=1 is justified: CodeR/TDFlow verify after EVERY edit, not only after 2+
