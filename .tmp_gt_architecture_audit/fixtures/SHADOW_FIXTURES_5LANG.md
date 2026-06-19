# Shadow Fixtures — verified_unique stdlib-shadow bug (5 languages)

These fixtures reproduce one indexer bug across 5 languages: the **verified_unique**
resolution strategy wrongly resolves a **qualified stdlib/module call** (`module.name(...)`)
to a uniquely-named **project** function that happens to share the bare name, and launders
it as a deterministic FACT (CERTIFIED / high confidence).

Each fixture has two files:
1. an **account** file defining a project function with a unique bare name that collides
   with a well-known stdlib/builtin module-function name;
2. a **scanner** file that imports the real stdlib/module and calls `module.<name>(...)`.

The collided bare name is defined exactly **once** per fixture (in the account file), so it
is globally unique within the 2-file fixture — which is precisely the condition that trips
verified_unique. The correct behavior is that the qualified call resolves to the imported
module, NOT to the project function: it must be **name_match / SPECULATIVE or unresolved**,
**never verified_unique / CERTIFIED** to the project symbol.

| Lang | Dir | Account file (defines unique symbol) | Scanner file (qualified stdlib call) | Unique project symbol | Stdlib qualified call that must NOT resolve to it | Expected-correct resolution |
|---|---|---|---|---|---|---|
| Python (reference) | `shadowpkg/` | `account.py` → `def walk(path)` | `scanner.py` → `os.walk(root)` | `walk` | `os.walk` | name_match / SPECULATIVE or unresolved — never verified_unique/CERTIFIED to `account.walk` |
| Go | `shadowpkg_go/` | `account.go` (package `account`) → `func Walk(path string) []string` | `scanner.go` (package `main`, `import "path/filepath"`) → `filepath.Walk(root, nil)` | `Walk` | `filepath.Walk` | name_match / SPECULATIVE or unresolved — never verified_unique/CERTIFIED to `account.Walk` |
| Rust | `shadowpkg_rust/` | `account.rs` → `pub fn read(path: &str) -> Vec<String>` | `scanner.rs` (`use std::fs;`) → `std::fs::read(p)` | `read` | `std::fs::read` | name_match / SPECULATIVE or unresolved — never verified_unique/CERTIFIED to `account::read` |
| TypeScript | `shadowpkg_ts/` | `account.ts` → `export function join(a, b): string` | `scanner.ts` (`import * as path from "path";`) → `path.join(a, b)` | `join` | `path.join` | name_match / SPECULATIVE or unresolved — never verified_unique/CERTIFIED to `account.join` |
| JavaScript | `shadowpkg_js/` | `account.js` → `function log(x)` (CommonJS export) | `scanner.js` → `console.log(x)` | `log` | `console.log` | name_match / SPECULATIVE or unresolved — never verified_unique/CERTIFIED to `account.log` |

## Per-language detail

### Python (reference — pre-existing)
- Dir: `shadowpkg/`
- Files: `account.py`, `scanner.py`
- Unique project symbol: `walk` (defined once, in `account.py`)
- Stdlib qualified call: `os.walk(root)` in `scanner.py` (stdlib `os`)
- Expected: `scanner.scan -> account.walk` must NOT be verified_unique/CERTIFIED.

### Go
- Dir: `shadowpkg_go/`
- Files: `account.go` (package `account`), `scanner.go` (package `main`)
- Unique project symbol: `Walk` (defined once, in `account.go`)
- Stdlib qualified call: `filepath.Walk(root, nil)` in `scanner.go` (stdlib `path/filepath`)
- Expected: `main.Scan -> account.Walk` must NOT be verified_unique/CERTIFIED.

### Rust
- Dir: `shadowpkg_rust/`
- Files: `account.rs`, `scanner.rs`
- Unique project symbol: `read` (defined once, in `account.rs`)
- Stdlib qualified call: `std::fs::read(p)` in `scanner.rs` (stdlib `std::fs`)
- Expected: `scanner::scan -> account::read` must NOT be verified_unique/CERTIFIED.

### TypeScript
- Dir: `shadowpkg_ts/`
- Files: `account.ts`, `scanner.ts`
- Unique project symbol: `join` (defined once, in `account.ts`)
- Stdlib/module qualified call: `path.join(a, b)` in `scanner.ts` (node builtin `path`)
- Expected: `scanner.scan -> account.join` must NOT be verified_unique/CERTIFIED.

### JavaScript
- Dir: `shadowpkg_js/`
- Files: `account.js`, `scanner.js`
- Unique project symbol: `log` (defined once, in `account.js`)
- Builtin qualified call: `console.log(x)` in `scanner.js` (global `console`)
- Expected: `scanner.scan -> account.log` must NOT be verified_unique/CERTIFIED.

## Bug pass/fail criterion

For each fixture, inspect the resolved edge from the scanner function to the bare-name target:
- **BUG REPRODUCED (fail):** the qualified-module call edge resolves to the project account
  symbol with `resolution_method = verified_unique` (or otherwise CERTIFIED / high confidence).
- **CORRECT (pass):** the qualified-module call is left unresolved, or resolved only as
  `name_match` / SPECULATIVE (low confidence) — never a CERTIFIED edge into the project symbol.
