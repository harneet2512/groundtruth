# Proof Probe §4/§5 Audit — gates-only, GT_PROOF_MODE=1 (run 27166888850, hbali, gt-trial)

**Verdict: 4/10 substrate-GREEN, 6/10 fail-closed.** Gates-only ⇒ no agent ran (no token spend); the §4 deliverable is the per-task substrate PREREQS table below + the §5 Tier-6 legitimacy verdict. A FAIL = a wired fail-closed gate correctly caught a partial-operation substrate (plan §9: the signal).


## §4 PREREQS — P1 RESOLUTION (det% · name_match · typing tiers; 8-dp)

| task | CALLS | det | name_match | det_pct | type_flow | impl_method | inherited | assign | det≥nm (predB) | GATE-1 |
|---|--:|--:|--:|--:|--:|--:|--:|--:|:--:|:--:|
| aiogram__aiogram-1594 | 3740 | 2575 | 1165 | 68.85026738 | 80 | 390 | 16 | 80 | yes | GREEN |
| amoffat__sh-744 | 438 | 391 | 47 | 89.26940639 | 0 | 69 | 0 | 0 | yes | GREEN |
| beancount__beancount-931 | 2896 | 1769 | 1127 | 61.08425414 | 0 | 193 | 120 | 0 | yes | GREEN |
| beetbox__beets-5495 | 8079 | 5827 | 2252 | 72.12526303 | 85 | 1025 | 395 | 73 | yes | GREEN |
| bridgecrewio__checkov-6893 | 17536 | 12165 | 5371 | 69.37157847 | 1266 | 1539 | 358 | 1257 | yes | GREEN |
| conan-io__conan-17123 | 20261 | 15766 | 4495 | 77.81452051 | 4160 | 1067 | 620 | 4148 | yes | GREEN |
| deepset-ai__haystack-8489 | 5330 | 4161 | 1169 | 78.06754221 | 843 | 339 | 14 | 730 | yes | GREEN |
| joke2k__faker-2142 | 4119 | 1769 | 2350 | 42.94731731 | 142 | 401 | 242 | 0 | NO | **OFF** |
| matplotlib__matplotlib-28933 | 28203 | 15855 | 12348 | 56.21742368 | 718 | 6460 | 689 | 687 | yes | GREEN |
| pallets__flask-5626 | 1234 | 553 | 681 | 44.81361426 | 26 | 173 | 9 | 26 | NO | **OFF** |

## §4 PREREQS — P2 LSP precision pass (resolve count; 8-dp)

| task | resolved | residual | resolve_frac | scoped_files | GATE-2 |
|---|--:|--:|--:|--:|:--:|
| aiogram__aiogram-1594 | 63 | 34 | 1.85294118 | 15 | GREEN |
| amoffat__sh-744 | 15 | 12 | 1.25000000 | 1 | GREEN |
| beancount__beancount-931 | 111 | 33 | 3.36363636 | 14 | GREEN |
| beetbox__beets-5495 | 350 | 261 | 1.34099617 | 31 | GREEN |
| bridgecrewio__checkov-6893 | 0 | 1 | 0.00000000 | 1158 | **OFF** |
| conan-io__conan-17123 | 43 | 121 | 0.35537190 | 17 | GREEN |
| deepset-ai__haystack-8489 | 12 | 18 | 0.66666667 | 3 | GREEN |
| joke2k__faker-2142 | 5 | 7 | 0.71428571 | 6 | GREEN |
| matplotlib__matplotlib-28933 | 3 | 31 | 0.09677419 | 5 | **OFF** |
| pallets__flask-5626 | 9 | 3 | 3.00000000 | 2 | GREEN |

## §4 PREREQS — P3 EMBEDDER (present + CONSUMED; 8-dp)

| task | class | is_zero | cos_rel | cos_unrel | eff_w_sem | sem_scored | sem_distinct | sem_mad | sem_max | cover | disp | GATE-3 |
|---|---|:--:|--:|--:|--:|--:|--:|--:|--:|:--:|:--:|:--:|
| aiogram__aiogram-1594 | EmbeddingModel | False | 0.86053280 | 0.76078654 | 0.15000000 | 1 | 2 | 0.00000000 | 0.84361500 | N | y | GREEN |
| amoffat__sh-744 | EmbeddingModel | False | 0.86053280 | 0.76078654 | 0.15000000 | 1 | 2 | 0.41810100 | 0.83620200 | y | y | GREEN |
| beancount__beancount-931 | EmbeddingModel | False | 0.86053280 | 0.76078654 | 0.15000000 | 3 | 4 | 0.01865300 | 0.85299800 | y | y | GREEN |
| beetbox__beets-5495 | EmbeddingModel | False | 0.86053280 | 0.76078654 | 0.15000000 | 5 | 1 | 0.00000000 | 0.83886000 | y | N | **OFF** |
| bridgecrewio__checkov-6893 | EmbeddingModel | False | 0.86053280 | 0.76078654 | 0.15000000 | 3 | 4 | 0.04806300 | 0.85840600 | y | y | GREEN |
| conan-io__conan-17123 | EmbeddingModel | False | 0.86053280 | 0.76078654 | 0.15000000 | 1 | 2 | 0.00000000 | 0.84796900 | N | y | GREEN |
| deepset-ai__haystack-8489 | EmbeddingModel | False | 0.86053280 | 0.76078654 | 0.15000000 | 0 | 1 | 0.00000000 | 0.00000000 | N | N | **OFF** |
| joke2k__faker-2142 | EmbeddingModel | False | 0.86053280 | 0.76078654 | 0.15000000 | 5 | 5 | 0.01134900 | 0.86258200 | y | y | GREEN |
| matplotlib__matplotlib-28933 | EmbeddingModel | False | 0.86053280 | 0.76078654 | 0.15000000 | 3 | 4 | 0.01450100 | 0.80873600 | y | y | GREEN |
| pallets__flask-5626 | EmbeddingModel | False | 0.86053280 | 0.76078654 | 0.15000000 | 2 | 3 | 0.00000000 | 0.83851900 | N | y | GREEN |

## §5 Tier-6 LEGITIMACY (foundational_gates shown + GREEN per task)

| task | GATE-1 resolution | GATE-2 lsp | GATE-3 embedder | ALL_ON | failure gate |
|---|:--:|:--:|:--:|:--:|---|
| aiogram__aiogram-1594 | GREEN | GREEN | GREEN | GREEN | — |
| amoffat__sh-744 | GREEN | GREEN | GREEN | GREEN | — |
| beancount__beancount-931 | GREEN | GREEN | GREEN | GREEN | — |
| beetbox__beets-5495 | GREEN | GREEN | **OFF** | **OFF** | G3 sem flat (mad=0) |
| bridgecrewio__checkov-6893 | GREEN | **OFF** | GREEN | **OFF** | G2 resolve |
| conan-io__conan-17123 | GREEN | GREEN | GREEN | GREEN | — |
| deepset-ai__haystack-8489 | GREEN | GREEN | **OFF** | **OFF** | G3 sem all-zero |
| joke2k__faker-2142 | **OFF** | GREEN | GREEN | **OFF** | G1 name_match-dominant |
| matplotlib__matplotlib-28933 | GREEN | **OFF** | GREEN | **OFF** | G2 resolve |
| pallets__flask-5626 | **OFF** | GREEN | GREEN | **OFF** | G1 name_match-dominant |

## Failure taxonomy (the two real substrate gaps surfaced)

- **GATE-1 name_match dominance (2):** joke2k__faker-2142, pallets__flask-5626 — whole-graph `name_match > deterministic`; indexer-level receiver-type resolution gap (CLAUDE.md method-call gap), NOT the LSP demand-pass (which passed GATE-2 everywhere).
- **GATE-3 embedder not-consumed (2):** beetbox__beets-5495, deepset-ai__haystack-8489 — rendered candidates carry all-zero or flat (mad=0) semantic scores; render-alignment / sem-join collapse (plan finding A).
- **GATE-2 LSP resolve: PASS on ALL 10 tasks** — resolve is not the failure.
