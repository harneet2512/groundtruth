# GTBench — GroundTruth Hallucination Detection Benchmark

GTBench measures how effectively GroundTruth detects and fixes common AI code hallucinations across TypeScript, Python, and Go.

## How to Run

```bash
# Run all languages
python benchmarks/runner.py --fixture all

# Run a single language
python benchmarks/runner.py --fixture typescript
python benchmarks/runner.py --fixture python
python benchmarks/runner.py --fixture go
```

Results are printed to stdout and written to `benchmarks/results/latest.md` and `benchmarks/results/latest.json`.

## What It Measures

### Hallucination Detection (100 cases)

For each case, GTBench evaluates:

| Metric | Description |
|--------|-------------|
| **Detected** | Did the validator flag the error? |
| **Fix OK** | Did the deterministic fix pipeline produce the correct suggestion? |
| **AI Needed** | Was AI semantic resolution needed (no deterministic fix found)? |
| **Briefing Would Inform** | Would FTS5 search on the intent return the correct symbol? |

### File Relevance (20 cases)

For each task description, GTBench evaluates:

| Metric | Description |
|--------|-------------|
| **Recall** | Did find_relevant return all expected files? |
| **Precision** | What fraction of returned files were expected? |

## Case Format

### Hallucination Case

```json
{
  "id": "win-001",
  "category": "wrong-import-name",
  "subcategory": "close-match",
  "language": "typescript",
  "description": "Typo: 'loign' instead of 'login' (distance 2)",
  "input": {
    "code": "import { loign } from './auth';\n\nconst result = await loign(email, password);",
    "filePath": "src/app.ts",
    "intent": "authenticate a user with email and password"
  },
  "expected": {
    "valid": false,
    "errorType": "symbol_not_found",
    "fixType": "levenshtein",
    "correctSymbol": "login",
    "shouldRequireAI": false,
    "briefingWouldPrevent": true
  }
}
```

The `language` field is optional and defaults to `"typescript"` for backward compatibility with the original 75 cases.

### File Relevance Case

```json
{
  "id": "find-001",
  "language": "typescript",
  "task": "fix getUserById returning null instead of throwing NotFoundError",
  "entry_symbols": ["getUserById", "NotFoundError"],
  "expected_files": ["src/users/queries.ts", "src/utils/errors.ts"],
  "should_not_include": ["src/utils/dates.ts"]
}
```

## Categories (100 hallucination cases)

### wrong-import-name/close-match (15 cases: win-001 to win-015)
Typos within Levenshtein distance <= 3 of real exports. Deterministic fix via Levenshtein suggestion.

### wrong-import-name/no-close-match (10 cases: win-016 to win-025)
Hallucinated names far from any real export (distance > 3). Require AI semantic resolution.

### wrong-module-path/symbol-exists-elsewhere (15 cases: wmp-001 to wmp-015)
Real symbol imported from the wrong module. Cross-index search finds the correct module.

### wrong-module-path/module-doesnt-exist (5 cases: wmp-016 to wmp-020)
Import from a nonexistent module path.

### missing-package (15 cases: mp-001 to mp-015)
Import of packages not listed in the project manifest.

### wrong-signature (15 cases: ws-001 to ws-015)
Calls to real functions with wrong argument count.

### invented-symbol (15 cases: is-001 to is-015)
Semantically plausible but invented function names (e.g., `encryptPayload` instead of `signToken`). 5 TS, 5 Python, 5 Go. Require AI semantic resolution.

### wrong-language-convention (10 cases: wlc-001 to wlc-010)
Wrong naming convention for the language (e.g., `get_user_by_id` in TypeScript, `getUserById` in Python, `getUserByID` in Go). 3 TS, 3 Python, 4 Go.

## File Relevance Cases (20 cases)

20 cases across 3 languages (8 TS, 7 Python, 5 Go). Each case provides a task description and expected entry symbols. The runner mocks TaskParser to return those symbols directly, then evaluates whether `handle_find_relevant` returns the correct files.

## Methodology

- **Fixture data:** Shared symbol/ref/package definitions in `benchmarks/_fixtures.py`, extracted from `tests/integration/test_cross_language.py`.
- **Indexing:** Each language's fixture data is loaded into an in-memory SQLite database.
- **Validation:** Each hallucination case runs through the full `ValidationOrchestrator.validate()` pipeline.
- **Briefing check:** Deterministic FTS5 search (`search_symbols_fts`) checks if the correct symbol appears for the given intent. No AI call needed.
- **File relevance:** `handle_find_relevant` with mocked TaskParser. Precision and recall measured against expected file lists.

## Adding New Cases

1. Create a JSON file in the appropriate subdirectory.
2. Follow the case format above. Include `language` field for non-TypeScript cases.
3. Use `id` naming: `{category-prefix}-{number}` (e.g., `is-016`, `wlc-011`).
4. Run `python benchmarks/runner.py --fixture all` to verify.
