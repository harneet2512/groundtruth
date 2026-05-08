# L4 Pre-fetch Architecture (2026-05-08)

## Problem
L4 tools (gt_query, gt_search, gt_navigate, gt_validate) are installed on PATH
inside the container, but the OpenHands CodeActAgent never calls them. Research
shows: text hints in prompts don't drive tool usage. Structured function calling
schemas do, but we can't modify OH's tool surface.

## Solution: Issue-text-seeded pre-fetch (AutoCodeRover/Aider pattern)
Instead of hoping the agent discovers CLI tools, pre-fetch the evidence during
`patched_initialize_runtime` and inject it into the brief. The agent sees real
caller/contract/blast-radius data from iteration 1.

### Symbol selection (LocAgent/Aider pattern)
1. Extract identifiers from issue text (regex + stopword filter)
2. Cross-check against graph.db node names in candidate files
3. Fall back to top-connected exported symbols if no issue matches
4. Cap at 3 symbols max

### Budget
| Dimension       | Limit  | Rationale                          |
|-----------------|--------|------------------------------------|
| Queries         | 3 max  | One per top candidate              |
| Lines/query     | 5 max  | Only [VERIFIED] or [POSSIBLE] tags |
| Total chars     | 1200   | ~300 tokens                        |
| Wall time       | 30s    | All queries combined               |
| LLM cost        | $0     | Pure sqlite3                       |
| Agent iters     | 0      | Runs during init                   |

### Quality gate
Only evidence lines containing `[VERIFIED]` or `[POSSIBLE]` tags are included.
"No results" queries are dropped entirely. This prevents noise injection.

## L3/L3b Dedup
Evidence is hashed (md5, 12 chars) per file. If the same file is viewed/edited
again and the hook produces identical evidence, the duplicate is suppressed with
a `dedup="true"` tag instead of re-injecting the full block.

## Evidence-to-noise principle
GT should enrich the agent's context, not spam it. Every injection must add
information the agent couldn't get from reading the source file. Track
evidence-to-noise ratio: (substantive_injections / total_injections) > 0.6.

## Research basis
- AutoCodeRover (ISSTA 2024): issue-driven AST search, no centrality
- Agentless (2024): LLM ranks issue-to-signature semantic match
- Aider RepoMap: Personalized PageRank, 10x weight to issue-mentioned identifiers
- LocAgent (ACL 2025): issue keywords -> graph node match -> 1-2 hop BFS
- SweRank (May 2025): issue-to-code semantic ranking beats agent-based systems
- Consensus: issue-text-seeded, graph-expanded. Centrality alone is backwards.
