# RC-06 Addendum — language-agnostic L5 + tools + identifier extraction

Phase 3 fix scope: cluster RC-06 (L5 gate, gt_validate, _is_test_file,
identifier extractors, F2P helper). Closed findings: B-007, B-012, H-004,
H-005, H-006, H-008, H-009, H-010, H-013.

## What shipped

1. `gt_pre_finish_gate.py`
   - Added `LANG_BY_EXT` dispatch table (16 extensions, 6 with full
     `structural=True` — Python, Go, JS, TS, Java, Rust).
   - `_log_skip(check, path, reason)` writes a `<gt-pre-finish-gate> SKIP …`
     line to stderr (visible in `gt_layers.log`) so the operator can see
     L5 disengaged on a given file. Replaces the silent
     `if not f.endswith(".py"): continue` early-exit.
   - `_added_import_lines_for_lang` + `_parse_import_targets_for_lang`
     handle Go (`import "x/y"` and block-import), JS/TS
     (`import … from 'x'`, `require('x')`), Java (`import com.foo.Bar;`),
     Rust (`use a::b::C;`).
   - `_changed_symbols_for_lang` + `_SYMBOL_RES_BY_LANG` add per-language
     def/class/struct/interface/enum regexes for Go, JS, TS, Java, Rust.
   - `_SIG_RES_BY_LANG` + per-language signature regexes power CONTRACT-BREAK
     across the same five languages. Class-base check stays Python-only
     (other languages use distinct inheritance syntax — explicit by-design
     skip; not silent).
   - `_is_test_file` recognizes Go (`*_test.go`), Java
     (`*Test.java` / `*Tests.java`), Ruby (`*_spec.rb`), C# (`*Tests.cs`),
     PHP (`*Test.php`), pytest infra (`conftest.py`), `/spec/`, `/specs/`,
     `/__tests__/`, plus all the JSX/TSX variants.
   - `check_blast_radius_no_test` and `check_scratch_files` use the
     language dispatch table — non-Python source extensions now go through
     the same predicates as Python.

2. `gt_validate.py`
   - `_is_test_file` synced with the gate's expanded recognition set.
   - `_KNOWN_SOURCE_EXTS` (16 entries) and `_PER_LANG_CHECKS_AVAILABLE` (.py
     today) drive a new BLAST-RADIUS finding for non-Python source files.
     Replaces the silent green-light: a `.go` / `.ts` / `.java` file with
     callers in graph.db now produces an explicit structural finding.
   - `_file_blast_radius` query is reused from the gate (graph.db edges
     are language-agnostic by construction).

3. `gt_search.py`
   - SKIP_DIR_NAMES no longer excludes `test`/`tests`/`Tests`/`__tests__`
     by default. The legacy filter is moved to `TEST_DIR_NAMES` and only
     applies when the new `--exclude-tests` flag is passed.
   - `--include-tests` (default) and `--exclude-tests` flags are parsed
     in `main()`. `_iter_files` and `_mode_code` thread the flag through;
     test-path matches are now filtered post-hoc only when
     `exclude_tests=True`.

4. `gt_query.py`, `gt_navigate.py`
   - All five `ORDER BY is_test ASC, id ASC` clauses replaced with
     `ORDER BY id ASC`. Tests are legitimate callers/symbols on TDD
     repos — sorting them last hid evidence.
   - The `same_file DESC, import DESC, s.is_test ASC, source_line ASC`
     clauses in `get_callers` (gt_query) and the equivalent `gt_navigate`
     clauses lose the `s.is_test ASC` tie-breaker.

5. `gt_edit_state.py`
   - `_list_changed_source_files` no longer drops test edits. L3 evidence
     now fires on test edits too — the L5 caller-blind gate handles the
     "test edit present" case via its own short-circuit (existing
     behavior preserved).

6. `gt_intel.py`
   - Added two regexes:
     - Single-hump PascalCase adjacent to Go declaration keywords
       (`func`, `type`, `var`, `const`, `struct`, `interface`,
       `package`). Captures `func Run(ctx)` -> `Run`.
     - ALL_CAPS constants `\b([A-Z][A-Z0-9_]{3,})\b`. Captures `EINVAL`,
       `SIGINT`, `MAX_BUFFER_SIZE`, `O_RDONLY`.

7. `gt_track4_pre_run.py`
   - `_extract_test_file_tokens` now strips:
     - `_spec` / `_specs` snake_case suffix (Ruby).
     - `Tests` / `Spec` / `Test` PascalCase suffix (Java/C#/PHP) when
       the head is PascalCase (avoids clobbering names like `Latest`).
   - Adds snake_case reconstruction (`FooBar` -> `foo_bar`) so the
     graph-name filter has both casing variants to test.

## Anti-benchmaxxing audit

Each fix was verified to add coverage rather than tune Python behavior:

- LANG_BY_EXT: 5 non-Python languages get full `structural=True`. Adding
  Kotlin / Swift / Scala is a one-line change in the table plus a regex.
- `_is_test_file`: every added pattern is the canonical leaf-name
  convention for that language — no SWE-bench-Live or repo-specific text.
- gt_search `--include-tests` default: helps SWE-bench tasks where the
  fix touches a test (~50% of tasks per the audit), and helps any TDD
  repo equally — no benchmark-specific tuning.
- `is_test ASC` removal: pure structural — same effect on every repo,
  every language, every query.
- gt_intel CamelCase + ALL_CAPS: regexes are language-shape, not
  repo-shape (no hardcoded names).

## Integration check

`docs/ultrareview/integration_checks/RC-06.sh` covers:
1. `gt_pre_finish_gate` against a synthetic `.go` edit on the gt-index Go
   binary's own source — must fire BLAST-RADIUS-NO-TEST.
2. `gt_validate` on a `.go` file — must produce a structural finding,
   not "no structural flags raised".
3. `_is_test_file` against 9 cases (Go, Java, Ruby, C#, PHP, pytest infra,
   plus a `Latest.java` negative).
4. `gt_intel` issue-text extraction over Go-shape and C-shape sample issues.
5. `_extract_test_file_tokens` over Java/C#/Ruby diff snippets.

## Tests

L5 gate suite: 13/15 pass; the two failing scratch tests are pre-existing
(RC-01 split moved `debug_` and `test_` prefixes into an opt-in tier
behind `GT_GATE_SCRATCH_OPT_IN=1`). Out of RC-06 scope.

L1/L2/L3/L4/engagement layer suites: all pass (64 tests).

gt_intel + L1 brief tests: 11/11 pass — single-hump Go + ALL_CAPS regexes
verified by `extract_identifiers_from_issue` smoke.

## New bugs found

None during the fix work itself. All sub-fixes are additive and the
existing tests around the touched paths still pass except for the two
RC-01-induced failures noted above.

## Coordination

- `gt_intel.py` identifier extractor was modified here. RC-12 cluster
  may also touch this region for noise-stoplist work; if so, the changes
  should compose cleanly (this addendum's diff is purely additive — two
  new `re.findall` blocks, no existing regex modified).
