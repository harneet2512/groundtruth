# GroundTruth MCP Tool Reference

## groundtruth_generate

Proactive pre-write validation. Call BEFORE writing code to disk.

**Input:**
```json
{
  "intent": "what the user asked for (e.g., 'add JWT auth middleware')",
  "proposed_code": "the code the model wants to write",
  "file_path": "optional — path where the code will be written"
}
```

**Output (clean code):**
```json
{
  "valid": true,
  "errors": [],
  "context": {
    "relevant_symbols": ["verifyToken(token: string): Promise<JWTPayload>"],
    "usage_pattern": "12/14 route files use authMiddleware"
  }
}
```

**Output (hallucination detected):**
```json
{
  "valid": false,
  "errors": [
    { "symbol": "authenticate", "reason": "does not exist in '../auth'" }
  ],
  "suggested_fix": {
    "authenticate": "login(credentials: LoginCredentials) from src/auth/login.ts"
  },
  "context": {
    "relevant_symbols": ["login()", "verifyToken()", "logout()"],
    "usage_pattern": "12/14 route files use authMiddleware from src/middleware/auth.ts"
  }
}
```

## groundtruth_validate

Reactive post-write validation. Call AFTER code is written to disk.

**Input:**
```json
{
  "file_path": "src/middleware/auth.ts"
}
```

**Output:**
```json
{
  "valid": true,
  "errors": []
}
```

## groundtruth_status

Health check + intervention stats.

**Input:** none

**Output:**
```json
{
  "lsp": "tsserver (connected)",
  "indexed_symbols": 2847,
  "stats": {
    "total_validations": 247,
    "hallucinations_caught": 58,
    "fix_rate": "93.1%",
    "estimated_time_saved": "2.9 hours"
  }
}
```
