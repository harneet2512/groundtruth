# GroundTruth — Build Spec

> What to build, how it works, how to test it.

---

## 1. What It Does

MCP server that gives AI coding agents compiler-grade codebase intelligence via LSP + SQLite. Language-agnostic with zero language-specific code. Eight tools:

| Tool | What it does | Uses AI? | Cost |
|------|-------------|----------|------|
| `groundtruth_find_relevant` | Given a task, find which files matter | AI parses task; graph traversal is deterministic | ~$0.003 once |
| `groundtruth_brief` | Briefing before writing code | AI distills relevant symbols into guidance | ~$0.003 |
| `groundtruth_validate` | Validate code against the index | Deterministic first; AI only when all else fails | Usually $0 |
| `groundtruth_trace` | Trace a symbol's callers/callees/impact | No — pure graph traversal | $0 |
| `groundtruth_dead_code` | Find exported symbols with zero references | No — one SQL query | $0 |
| `groundtruth_unused_packages` | Find installed packages nothing imports | No — one SQL query | $0 |
| `groundtruth_hotspots` | Most referenced symbols (highest blast radius) | No — one SQL query | $0 |
| `groundtruth_status` | Health check + stats | No | $0 |

The goal: same agent + GroundTruth outperforms same agent alone — because it finds the right files faster, wastes fewer tokens on irrelevant context, and generates correct code on the first try.

---

## 2. Why LSP

Every previous tool in this space (SymDex, Sourcegraph, LSPRAG) uses tree-sitter for parsing. Tree-sitter gives you syntax — the AST. It tells you "there's a function called getUserById that takes some arguments."

LSP gives you semantics — the compiler's understanding. It tells you "getUserById takes (user_id: int) and returns User | None, it's called from 3 files, and the User type resolves to src/models/user.py line 14."

| Capability | Tree-sitter | LSP |
|---|---|---|
| Symbol names | Yes | Yes |
| Resolved types | No — text only | Yes — compiler-resolved |
| Cross-file references | No — manual tracking | Yes — `textDocument/references` |
| Import resolution | No — manual path logic | Yes — `textDocument/definition` |
| Diagnostics | No | Yes — real compiler errors |
| Documentation | No | Yes — hover info (docstrings, JSDoc) |
| Per-language code | Yes — queries per grammar | **No** — protocol is universal |
| New language cost | New grammar + extraction logic | One config line |

The tradeoff: LSP requires language servers to be installed. But developers already have them — they're what power VS Code, Neovim, Emacs, and every modern editor. If you're writing Python, you have pyright or pylsp. If you're writing TypeScript, you have tsserver. We leverage what's already there.

---

## 3. Technical Flows

### 3.1 Indexing (On Project Init)

```
1. Scan project root for source files
2. Group by extension → map to LSP server command (config.py)
3. For each language:
   a. Spawn LSP server via asyncio.subprocess (stdin/stdout)
   b. Send: initialize → initialized
   c. For each file:
      - textDocument/didOpen
      - textDocument/documentSymbol → all symbols
      - For each exported symbol:
        - textDocument/references → who uses it
        - textDocument/hover → signature + docs
   d. Store everything in SQLite
4. Parse package manifest (package.json / requirements.txt / go.mod / Cargo.toml)
   → store in packages table
5. Build FTS5 index from symbols
```

**Incremental updates:** File watcher detects changes → re-index only changed files → update SQLite + FTS5.

**Performance target:** Index a 500-file project in <30 seconds. All LSP calls are async and batched per-file.

### 3.2 Find Relevant Files

```
Agent: "fix getUserById returning null instead of throwing NotFoundError"
       ↓
1. AI (Haiku) parses → ["getUserById", "NotFoundError"] (~200 tokens)
2. SQLite: getUserById defined in src/users/queries.py
           NotFoundError defined in src/utils/errors.py
3. BFS from entry files:
   - src/users/queries.py imports → src/db/client.py, src/users/types.py
   - src/users/queries.py imported by → src/routes/users.py, src/services/user_service.py, tests/test_users.py
   - src/utils/errors.py imported by → src/middleware/error_handler.py, src/routes/users.py
4. Score: distance 0 = high, distance 1 = medium, distance 2+ = low
5. Return ranked list with reasons
       ↓
Agent reads 6 files instead of 30. Saves ~50K tokens.
```

### 3.3 Proactive Briefing

```
Agent: "add JWT auth middleware to user routes"
       ↓
1. Keywords: ["JWT", "auth", "middleware", "user", "routes"]
2. FTS5: match → authMiddleware (middleware/auth.py), signToken (auth/jwt.py),
         decodeToken (auth/jwt.py), userRouter (routes/users.py)
3. Enrich: signatures, file paths, documentation from SQLite
4. Haiku distills → compact briefing (~150 tokens)
       ↓
Agent knows the patterns BEFORE writing code.
```

### 3.4 Reactive Validation

```
Agent writes: from auth import hashPassword; import axios
       ↓
Step 1 (deterministic): Check "hashPassword" in auth/ exports → NOT FOUND
Step 2 (Levenshtein): Closest in auth/: login (distance=9) → too far
Step 3 (cross-index): hashPassword found in utils/crypto.py → MATCH
→ Return: "hashPassword is in utils/crypto, not auth/"

Step 1 (deterministic): Check "axios" in packages → NOT FOUND
→ Return: "axios not installed. requests is available."

Total AI cost: $0. Total time: <10ms.
```

```
Agent writes: from auth import encryptPayload
       ↓
Step 1: "encryptPayload" not in auth/ → NOT FOUND
Step 2: Levenshtein: no close match anywhere
Step 3: Cross-index: "encryptPayload" not in any file
Step 4: AI semantic resolver (Haiku):
  Context: code is building a JWT response
  Index shows: signToken(payload) in auth/jwt.py
  → "You likely want signToken from auth/jwt — it signs the payload into a JWT"

AI cost: ~$0.003. Fires only when deterministic is exhausted.
```

### 3.5 Symbol Tracing

```
Agent: groundtruth_trace({ symbol: "getUserById" })
       ↓
Pure SQLite:
- Defined: src/users/queries.py:14
- Callers: routes/users.py:47, services/user_service.py:23, tests/test_users.py:8
- Callees: db.query (db/client.py), User (users/types.py)
- Impact radius: 5 files
       ↓
Zero AI. Zero tokens. <10ms.
```

---

## 4. Components

### 4.1 LSP Client (`src/groundtruth/lsp/client.py`)

Universal JSON-RPC client over stdio. Handles:
- Sending requests (`textDocument/documentSymbol`, `textDocument/references`, `textDocument/hover`, `textDocument/definition`)
- Receiving responses (with timeout)
- Request ID tracking
- Notification handling (`textDocument/publishDiagnostics`)
- Graceful shutdown

**Key design decision:** One LSP client class, zero language-specific code. The client sends the same JSON-RPC messages regardless of which language server is on the other end.

### 4.2 LSP Manager (`src/groundtruth/lsp/manager.py`)

Manages LSP server lifecycles:
- Spawns servers via `asyncio.create_subprocess_exec`
- Handles `initialize` → `initialized` handshake
- Routes requests to the correct server based on file extension
- Restarts crashed servers
- Graceful shutdown on exit

### 4.3 LSP Config (`src/groundtruth/lsp/config.py`)

The ONLY file with language awareness. A dictionary:

```python
LSP_SERVERS: dict[str, LSPServerConfig] = {
    ".py":   LSPServerConfig(command=["pyright-langserver", "--stdio"], language_id="python"),
    ".ts":   LSPServerConfig(command=["typescript-language-server", "--stdio"], language_id="typescript"),
    ".tsx":  LSPServerConfig(command=["typescript-language-server", "--stdio"], language_id="typescriptreact"),
    ".js":   LSPServerConfig(command=["typescript-language-server", "--stdio"], language_id="javascript"),
    ".go":   LSPServerConfig(command=["gopls", "serve", "-stdio"], language_id="go"),
    ".rs":   LSPServerConfig(command=["rust-analyzer"], language_id="rust"),
    ".java": LSPServerConfig(command=["jdtls"], language_id="java"),
    ".c":    LSPServerConfig(command=["clangd"], language_id="c"),
    ".cpp":  LSPServerConfig(command=["clangd"], language_id="cpp"),
    ".rb":   LSPServerConfig(command=["solargraph", "stdio"], language_id="ruby"),
}
```

Adding a new language = one line. No parsing logic. No adapter. No grammar.

Users can also extend this via a config file (`groundtruth.toml`) for languages we don't ship a default for.

### 4.4 Indexer (`src/groundtruth/index/indexer.py`)

Orchestrates: scan files → group by language → query LSP → store in SQLite.

Handles:
- Initial full index
- Incremental re-index on file change
- Package manifest parsing (package.json, requirements.txt, go.mod, Cargo.toml — these are simple JSON/text parsing, not language-specific logic)

### 4.5 SQLite Store (`src/groundtruth/index/store.py`)

All database operations. See CLAUDE.md for full schema.

Key queries:
- `get_symbol_by_name(name) → list[Symbol]` — all symbols with this name
- `get_exports_by_module(path) → list[Symbol]` — what does this module export?
- `get_refs(symbol_id) → list[Reference]` — who references this symbol?
- `get_imports_for_file(path) → list[Import]` — what does this file import?
- `get_importers_of_file(path) → list[str]` — who imports from this file?
- `search_symbols(query) → list[Symbol]` — FTS5 full-text search
- `get_packages() → list[Package]` — all installed packages
- `get_dead_code() → list[Symbol]` — exported symbols with usage_count = 0
- `get_unused_packages() → list[Package]` — packages not referenced in any import
- `get_hotspots(limit) → list[Symbol]` — symbols ordered by usage_count DESC

### 4.6 Import Graph (`src/groundtruth/index/graph.py`)

BFS/DFS over the `refs` + `exports` tables. Pure SQLite, no AI.

### 4.7 Validators (`src/groundtruth/validators/`)

- **Import validator:** parse imports from code (regex-based), check against index
- **Package validator:** check package imports against packages table
- **Signature validator:** check function call arg counts against indexed signatures
- **Orchestrator:** runs all three, merges results, applies Levenshtein → cross-index → AI escalation

### 4.8 AI Layer (`src/groundtruth/ai/`)

- **Task parser:** natural language → symbol names (Haiku, ~200 tokens)
- **Briefing engine:** FTS5 results → Haiku → compact briefing (~150 tokens)
- **Semantic resolver:** error + context + index → Haiku → "what did you mean?" (~500 tokens)
- **Prompts:** all templates centralized and testable

### 4.9 Stats (`src/groundtruth/stats/`)

Every intervention logged to `interventions` table. Reporter generates summaries:
- Hallucinations caught (total, by type, by language)
- AI calls made + tokens used + cost
- Deterministic vs AI fix ratio
- Average latency per tool

---

## 5. Fixture Projects

Three projects, same logical structure, different languages. This proves language-agnosticism is real.

### 5.1 TypeScript Fixture (`tests/fixtures/project_ts/`)

```
project_ts/
├── package.json              # express, zod, bcrypt. NO axios.
├── tsconfig.json
└── src/
    ├── index.ts
    ├── auth/
    │   ├── index.ts          # barrel re-export
    │   ├── login.ts          # login(email: string, password: string): Promise<LoginResult>
    │   ├── logout.ts         # logout(token: string): Promise<void>
    │   ├── verify.ts         # verifyToken(token: string): Promise<TokenPayload>
    │   └── jwt.ts            # signToken(payload: object): string, decodeToken(token: string): TokenPayload
    ├── users/
    │   ├── index.ts
    │   ├── queries.ts        # getUserById, createUser, updateUser, deleteUser
    │   └── types.ts          # User, CreateUserInput, UpdateUserInput
    ├── utils/
    │   ├── crypto.ts         # hashPassword, comparePassword, generateSalt
    │   ├── validation.ts     # validateEmail, validatePassword, sanitizeInput
    │   └── errors.ts         # AppError, NotFoundError, ValidationError
    ├── middleware/
    │   ├── auth.ts           # authMiddleware
    │   └── errorHandler.ts   # global error handler
    ├── db/
    │   └── client.ts         # database client
    └── types/
        └── global.d.ts
```

### 5.2 Python Fixture (`tests/fixtures/project_py/`)

```
project_py/
├── requirements.txt          # flask, pydantic, bcrypt. NO requests via pip name "axios-like".
├── pyproject.toml
└── src/
    ├── __init__.py
    ├── app.py
    ├── auth/
    │   ├── __init__.py       # re-exports login, logout, verify_token
    │   ├── login.py          # login(email: str, password: str) -> LoginResult
    │   ├── logout.py         # logout(token: str) -> None
    │   ├── verify.py         # verify_token(token: str) -> TokenPayload
    │   └── jwt.py            # sign_token(payload: dict) -> str, decode_token(token: str) -> TokenPayload
    ├── users/
    │   ├── __init__.py
    │   ├── queries.py        # get_user_by_id, create_user, update_user, delete_user
    │   └── types.py          # User, CreateUserInput, UpdateUserInput (Pydantic models)
    ├── utils/
    │   ├── __init__.py
    │   ├── crypto.py         # hash_password, compare_password, generate_salt
    │   ├── validation.py     # validate_email, validate_password, sanitize_input
    │   └── errors.py         # AppError, NotFoundError, ValidationError
    ├── middleware/
    │   ├── __init__.py
    │   ├── auth.py           # auth_middleware
    │   └── error_handler.py  # global error handler
    └── db/
        ├── __init__.py
        └── client.py         # database client
```

### 5.3 Go Fixture (`tests/fixtures/project_go/`)

```
project_go/
├── go.mod                    # gin, gorm. NO fiber.
├── go.sum
├── main.go
├── auth/
│   ├── login.go              # Login(email, password string) (*LoginResult, error)
│   ├── logout.go             # Logout(token string) error
│   ├── verify.go             # VerifyToken(token string) (*TokenPayload, error)
│   └── jwt.go                # SignToken(payload map[string]any) (string, error), DecodeToken(token string) (*TokenPayload, error)
├── users/
│   ├── queries.go            # GetUserByID, CreateUser, UpdateUser, DeleteUser
│   └── types.go              # User, CreateUserInput, UpdateUserInput
├── utils/
│   ├── crypto.go             # HashPassword, ComparePassword, GenerateSalt
│   ├── validation.go         # ValidateEmail, ValidatePassword, SanitizeInput
│   └── errors.go             # AppError, NotFoundError, ValidationError
├── middleware/
│   ├── auth.go               # AuthMiddleware
│   └── error_handler.go      # ErrorHandler
└── db/
    └── client.go             # DB client
```

### Deliberate Confusion Points (Same Across All Three)

These exist to test specific failure modes:

| Scenario | What it tests |
|---|---|
| `auth/` barrel vs `middleware/auth` | AI confuses these paths |
| `hashPassword` in `utils/crypto`, not `auth/` | Wrong module path resolution |
| `signToken` in `auth/jwt`, not barrel-exported | Semantic resolution target |
| `axios`/`requests`/`fiber` not installed | Missing package detection |
| `errorHandler` imports `AppError` | Graph traversal (transitive dep) |
| `queries` imports from `db/client` | Dependency chain tracing |
| Multiple barrel re-exports | Module resolution complexity |
| Go capitalization = export convention | LSP handles this, no hardcoding |
| Python `__init__.py` re-exports | LSP handles this, no hardcoding |

---

## 6. Testing Strategy

### 6.1 Unit Tests

In-memory SQLite. Mocked LSP responses. Mocked LLM.

**LSP client:** sends correct JSON-RPC, handles responses, handles errors/timeouts.

**Indexer:** given mocked LSP responses, produces correct SQLite records. Handles incremental updates.

**Store:** all CRUD operations, FTS5 search, parameterized queries.

**Graph traversal:**
- From `users/queries`, find connected → includes `db/client`, `users/types`, `routes/users`
- `find_callers("getUserById")` → returns all referencing files
- `get_impact_radius("getUserById")` → correct count

**Task parser:**
- "fix getUserById returning null" → `["getUserById"]`
- "add auth middleware to routes" → `["auth", "middleware", "routes"]`
- No API key → keyword fallback works

**Validators:**
- Correct code passes
- Wrong imports caught with correct suggestion
- Missing packages caught
- Wrong arg count caught

**Briefing:** returns relevant symbols, warns about confusions, <200 tokens.

**Semantic resolver:** `encryptPayload` → suggests `signToken` from `auth/jwt`.

### 6.2 Integration Tests

Full pipeline through MCP tool handlers. Use real LSP servers against fixture projects.

- `groundtruth_find_relevant` → returns correct files ranked
- `groundtruth_brief` → returns briefing with relevant symbols
- `groundtruth_validate` → catches errors, suggests fixes
- `groundtruth_trace` → returns callers + callees
- `groundtruth_status` → returns accurate stats

**Cross-language integration:** Run the same logical test against all three fixture projects. "Find files relevant to fixing getUserById" should return equivalent results in TS, Python, and Go — proving language-agnosticism works end-to-end.

### 6.3 Hallucination Benchmark

100 test cases across languages and categories.

**Distribution:** 40 TypeScript, 35 Python, 25 Go.

**Categories:**

| Category | Count | Example |
|---|---|---|
| `wrong_symbol_name` | 20 | `authenticateUser` → `login` |
| `wrong_module_path` | 20 | `from auth import hashPassword` → should be `from utils.crypto` |
| `missing_package` | 15 | `import axios` in a project that uses `requests` |
| `wrong_signature` | 15 | `getUserById(id, includeDeleted)` when it only takes `id` |
| `invented_symbol` | 15 | `from auth import encryptPayload` — doesn't exist, means `signToken` |
| `wrong_language_convention` | 15 | `get_user_by_id` in Go (should be `GetUserByID`) |

Each case:
```json
{
  "id": "wrong-path-007",
  "language": "python",
  "category": "wrong_module_path",
  "input": {
    "code": "from auth import hash_password",
    "file_path": "src/routes/users.py",
    "intent": "hash the user's password during signup"
  },
  "expected": {
    "valid": false,
    "error_type": "wrong_module_path",
    "fix_source": "cross_index",
    "correct_import": "from utils.crypto import hash_password",
    "should_require_ai": false,
    "briefing_would_inform": true
  }
}
```

**`briefing_would_inform`** means the briefing contains the correct symbol. This does NOT guarantee the agent would use it — label honestly as "Briefing Would Inform", not "Briefing Prevents."

**Metrics:**

| Metric | Target |
|---|---|
| Detection rate (errors correctly identified) | >95% |
| Fix rate — deterministic (Levenshtein + cross-index) | >80% |
| Fix rate — with AI fallback | >93% |
| False positive rate | <2% |
| Briefing Would Inform rate | >70% |
| Average latency (deterministic) | <15ms |
| Average latency (with AI) | <800ms |

### 6.4 File Relevance Benchmark

20 test cases — tests whether `groundtruth_find_relevant` returns the right files.

```json
{
  "id": "find-003",
  "language": "python",
  "task": "fix get_user_by_id returning None instead of raising NotFoundError",
  "expected_files": ["src/users/queries.py", "src/utils/errors.py", "src/db/client.py"],
  "should_not_include": ["src/auth/login.py", "src/middleware/auth.py"]
}
```

**Metrics:** Precision (relevant returned / total returned) and Recall (relevant returned / total relevant). Target: >80% precision, >90% recall.

### 6.5 Evidence Capture — Proving Value

GroundTruth must produce **undeniable, inspectable proof** that it prevents hallucinations. Not aggregate stats. Concrete exhibits.

**The problem with `interventions` today:** It logs that validation ran and found errors, but it doesn't capture what the agent originally tried to write, what happened after our correction, or whether the correction actually worked. Nobody can pull up a specific example and say "here's a hallucination GroundTruth caught."

**What we capture — the full interaction lifecycle:**

```sql
CREATE TABLE validation_exhibits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp INTEGER NOT NULL,
    session_id TEXT NOT NULL,              -- groups interactions within one agent session
    file_path TEXT NOT NULL,
    language TEXT NOT NULL,

    -- Phase 1: What the agent tried to write
    proposed_code TEXT NOT NULL,           -- the hallucinated code
    errors_detected TEXT NOT NULL,         -- JSON: errors from validate()
    suggestions_returned TEXT NOT NULL,    -- JSON: our fix suggestions

    -- Phase 2: What the agent wrote after our correction
    corrected_code TEXT,                   -- what the agent submitted next (null if no follow-up)
    agent_accepted_fix BOOLEAN,            -- did the corrected code use our suggestion?
    correction_latency_ms INTEGER,         -- time between our response and agent's next submission

    -- Phase 3: Did the correction actually work?
    corrected_code_valid BOOLEAN,          -- did corrected code pass validation?
    lsp_diagnostics_clean BOOLEAN,         -- did LSP report zero errors on the corrected file?
    compile_success BOOLEAN,               -- did the project compile after the correction?

    -- Classification
    hallucination_type TEXT NOT NULL,      -- wrong_module_path, symbol_not_found, missing_package, etc.
    fix_source TEXT NOT NULL,              -- levenshtein, cross_index, ai_semantic
    severity TEXT NOT NULL,                -- would_not_compile, wrong_behavior, style_only

    -- Link to intervention
    intervention_id INTEGER REFERENCES interventions(id)
);

CREATE INDEX idx_exhibits_session ON validation_exhibits(session_id);
CREATE INDEX idx_exhibits_timestamp ON validation_exhibits(timestamp);
CREATE INDEX idx_exhibits_type ON validation_exhibits(hallucination_type);
```

**How it works:**

1. **On `groundtruth_validate`:** If errors are found, store the proposed code, errors, and suggestions as a new exhibit. Return an `exhibit_id` alongside the normal response.

2. **On next `groundtruth_validate` for the same file + session:** Compare the new code against the previous exhibit's suggestions. Did the agent use our fix? Store the corrected code and mark `agent_accepted_fix`.

3. **On next `groundtruth_validate` where the corrected code passes:** Mark `corrected_code_valid = true`. If LSP diagnostics are available, mark `lsp_diagnostics_clean`.

4. **Exhibit viewer:** `groundtruth_status` includes a summary ("12 hallucinations caught this session, 10 fixes accepted, 9 verified correct"). A CLI command (`groundtruth exhibits --session <id>`) dumps the full before/after for each catch.

**What this enables:**

- **Demo reel:** "Here are 15 real hallucinations GroundTruth caught in the last week, with before/after code."
- **Acceptance rate:** "Agents accepted our fix 87% of the time."
- **Verification rate:** "Of accepted fixes, 94% resulted in code that compiled cleanly."
- **Severity breakdown:** "60% would have caused compile errors, 30% wrong behavior, 10% style issues."
- **Per-session stories:** "In this session, the agent tried to import hashPassword from auth/ three times before our correction stuck."

**No synthetic data. Every exhibit is a real interaction from a real coding session.**

### 6.6 SWE-bench Evaluation (Stretch Goal)

The ultimate proof. Infrastructure in `benchmarks/swe_bench/`.

**Setup:**
1. Filter SWE-bench Pro for TypeScript/JavaScript/Python tasks
2. For each task: clone repo, index with GroundTruth, run agent with MCP tools
3. Compare: agent alone vs agent + GroundTruth
4. Measure: resolve rate, tokens used, files read, time

**Harness:**
```python
@dataclass
class SWEBenchResult:
    task_id: str
    language: str
    baseline: RunResult      # agent alone
    with_groundtruth: RunResult  # agent + GroundTruth
    tools_called: list[ToolCall]
    delta_resolved: bool     # did GroundTruth flip this from fail to pass?
    tokens_saved: int
    files_saved: int

@dataclass
class RunResult:
    resolved: bool
    tokens_used: int
    files_read: int
    time_ms: int
```

**Target:** Even +3-5% resolve rate on 30+ tasks is significant. Token reduction is the easiest metric.

**This is a stretch goal.** Build custom benchmarks first. SWE-bench comes last. But design everything so it works on any project, not just fixtures.

---

## 7. Research Layers

Built AFTER the core tool works. These turn GroundTruth from "a tool" into "a tool that produces novel findings."

### 7.1 Layer 1: Grounding Gap Measurement

**What it measures:** When GroundTruth briefs an agent with correct symbols, how often does the agent actually use them correctly?

**Data collection:** Already mostly built. The `briefing_logs` table stores what was in each briefing. The `interventions` table stores validation results. Grounding gap analysis compares them.

**Experiment design:**
1. Run 200+ coding tasks across the three fixture projects
2. For each task: agent receives briefing → agent writes code → validation checks output
3. For each briefing-validation pair, compute:
   - `compliance_rate`: symbols used correctly / symbols briefed
   - `ignored_rate`: symbols in briefing that agent never referenced
   - `hallucinated_despite`: symbols where agent hallucinated even though the correct answer was in the briefing

**Expected output:**
```
Grounding Gap Report (200 tasks):
  Average compliance rate: 0.73
  Agents correctly used briefed symbols: 73% of the time
  Agents ignored briefed symbols: 18% of the time
  Agents hallucinated DESPITE correct briefing: 9% of the time
  
  Breakdown by hallucination type:
    wrong_module_path: 0.81 compliance (briefing helps most here)
    wrong_symbol_name: 0.72 compliance
    invented_symbol:   0.58 compliance (briefing helps least here)
```

This data doesn't exist anywhere. Nobody has measured how reliably agents use correct context.

### 7.2 Layer 2: Hallucination Risk Prediction

**What it measures:** Which structural properties of code predict hallucination failures?

**Data collection:** Risk factors computed from the symbol index (zero AI cost). Correlated with actual hallucination data from Layer 1.

**Experiment design:**
1. Compute risk scores for every file and symbol in the fixture projects
2. Using grounding gap data from Layer 1, compute actual hallucination rates per file
3. Correlate: do files with high `naming_ambiguity` actually have higher hallucination rates?

**Expected output:**
```
Risk Factor Correlation (200 tasks):
  naming_ambiguity    → hallucination rate: r=0.67 (strong)
  overloaded_paths    → hallucination rate: r=0.72 (strong)
  import_depth        → hallucination rate: r=0.41 (moderate)
  parameter_complexity → hallucination rate: r=0.38 (moderate)
  convention_variance  → hallucination rate: r=0.29 (weak)
  isolation_score      → hallucination rate: r=0.21 (weak)
```

If `naming_ambiguity` and `overloaded_paths` are strong predictors, that's actionable: codebase maintainers can reduce hallucination risk by renaming ambiguous symbols and consolidating auth modules.

### 7.3 Layer 3: Adaptive Briefing

**What it measures:** Does adapting briefing content based on risk scores reduce hallucinations?

**Experiment design:**
1. Run 100 tasks with standard briefing (control)
2. Run the same 100 tasks with adaptive briefing (treatment):
   - High naming_ambiguity → briefing includes exact import paths
   - High import_depth → briefing shows re-export chain
   - Past failures in this area → briefing includes negative examples
3. Compare compliance rates

**Expected output:**
```
Adaptive Briefing A/B Test (100 tasks):
  Standard briefing compliance:  0.73
  Adaptive briefing compliance:  0.82
  Improvement: +12.3%
  
  Biggest gains:
    High naming_ambiguity files: 0.61 → 0.79 (+29.5%)
    High overloaded_paths files: 0.58 → 0.74 (+27.6%)
```

### 7.4 Testing the Research Layers

**Unit tests:**
- `test_grounding_gap.py`: given known briefing + known validation, compute correct compliance rate
- `test_risk_scorer.py`: given known symbol index, compute correct risk scores. Test each factor independently.
- `test_adaptive_briefing.py`: given high-risk file, verify briefing is enhanced with correct warnings

**Integration test:**
- Full loop: brief → validate → measure gap → score risk → adapt briefing → brief again
- Verify the adapted briefing contains different content than the original

---

## 8. Academic References

- **FORGE '26**: "Detecting and Correcting Hallucinations in LLM-Generated Code via Deterministic AST Analysis." IEEE/ACM FORGE 2026, April 2026, Rio de Janeiro. 100% detection precision, 77% fix accuracy. arxiv.org/abs/2601.19106
- **RATester**: Automated detection of hallucinated API references in LLM-generated code
- **LSPRAG**: LSP as retrieval-augmented generation for code completion
- **LspAi**: LSP-based AI code assistance
- **LSP Hallucination Taxonomy (Sept 2024)**: Classification of hallucination types in code generation
- **Augment Code (Feb 2026)**: Context Engine (semantic index) achieved 51.80% on SWE-bench Pro vs 45.89% for same model without it. Demonstrates better codebase retrieval directly improves agent performance.

**GroundTruth extends prior work in two dimensions:**

As a tool:
1. **LSP over tree-sitter** — compiler-grade semantics, not just syntax (unlike SymDex, FORGE)
2. **Proactive context injection** before generation (no paper does this)
3. **Language-agnostic via protocol** — not via per-language grammars/adapters (unlike SymDex)
4. **Integrated prevention + correction pipeline** — brief before, validate after, in one tool

As a research platform:
5. **Grounding gap measurement** — first quantitative data on how reliably agents use correct context
6. **Hallucination risk prediction** — structural code properties that predict agent failure
7. **Adaptive context delivery** — evidence-based briefing that improves over time

---

## 9. Out of Scope (v0.1)

- VS Code extension
- Web dashboard
- Multi-repo support
- Streaming validation
- Daemon process
- Vector embeddings
- Custom LSP server (we're a client)
- Multi-error root cause diagnosis
- Building our own language parsers
