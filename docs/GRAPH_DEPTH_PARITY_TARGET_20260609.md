# Graph-Depth Parity Target — Python is the gold standard; Go/Rust/TS/JS must match it

> Measured from a real previous-run Python graph: `.tmp_scale_deepswe/deepswe-full-adaptix-name-mapping-aliases/graph.db`
> (3125 nodes / 6551 edges). This is what "Python-level depth" means, with numbers. Parity = the other 4
> languages produce the same KINDS at language-appropriate counts. Judge by query, not by code.

## PYTHON GOLD STANDARD (the target)
**Resolution (FACT vs GUESS) — CALLS:** **90.3% FACT / 9.7% guess.**
`verified_unique 30.5% · import 26.2% · same_file 25.3% · name_match 9.7% · lsp 5.6% · impl_method 2.4% · inherited 0.2% · type_flow 0.0%`

**Edge types:** CALLS 4904 · CONTAINS 1276 · EXTENDS 371.
**Node labels:** Method 1276 · Function 958 · Class 891.
**trust_tier:** CERTIFIED 5661 · SPECULATIVE 440 · CANDIDATE 79.
**Substrate tables:** assertions 1080 · cochanges 654 · closure 13893 · FTS5 3125.

**23 property kinds present (the depth):**
data_flow 2591 · class_field 2315 · fingerprint 2234 · visibility 1848 · return_shape 1807 ·
field_read 1220 · caller_usage 1166 · param 1073 · serialization_pair 780 · guard_clause 543 ·
class_decorator 431 · call_order 408 · conditional_return 402 · side_effect 398 · exception_type 393 ·
boundary_condition 336 · resource_pattern 232 · exception_flow 200 · exception_handler 183 ·
docstring 72 · security_tag 24 · config_read 3 · concurrency_pattern 2.

## The gap to close, per language (from the 5-language audit)
| Dimension | Python (target) | Go | Rust | TypeScript | JavaScript |
|---|---|---|---|---|---|
| **CALLS % FACT** | **90%** | 57% | **25%** | **30%** | 63% |
| data_flow | 2591 | present | **0** | **0** | present |
| param | 1073 | present | **0** | present | present |
| side_effect / field_read | 398 / 1220 | **0 / 0** (receivers) | **0 / 0** (receivers) | this. | this. |
| EXTENDS | 371 | present | unverified(v15.1) | present | present |
| **IMPLEMENTS** (typed langs) | n/a (py uses EXTENDS) | **0** | **0** | **0** | n/a |
| assertions / closure / FTS5 | 1080 / 13893 / yes | check | **absent (v15.1)** | check | check |

## What "same depth" requires (the work)
**Half 1 — extraction parity:** the indexer (`gt-index`, tree-sitter) must emit `data_flow`, `param`,
receiver-aware `side_effect`/`field_read`, and `IMPLEMENTS` on Go/Rust/TS (not just `self.`/`this.`).
**Half 2 — resolution parity:** non-Python method calls must convert from `name_match` (guess) to FACT
via receiver-type resolution (CHA/XTA over the hierarchy + LSP) — closing the gt_gt "58% method gap" so
Go/Rust/TS reach ~90% FACT like Python.

## Validation plan (is-GT-working, NOT resolve — Stage 1)
1. **Docker substrate image** (building: run `27236691230`) — the ONE runtime that builds the graph + runs gt-run-proof.
2. **Graph parity** (Track-2 + completion) — bring Go/Rust/TS/JS to the numbers above.
3. **5-language smoke** — gt-run-proof on a fixture per language through the image; query the graph vs THIS target.
4. **Per-language deepseek-v4-flash trajectory** (1-2 tasks each) — read the WHOLE trajectory: did GT deliver
   correct context + did the agent act on it, per language, per layer (a problem may live in one layer on one
   language). Model = deepseek-v4-flash only; **no Google; judge by trajectory + graph depth, never by resolve.**
