# SWE-bench Multilingual iteration manifest -- SPLIT

Anti-overfit iteration surface for GroundTruth. DeepSWE (`artifact_deepswe/`) stays **held out**; GT iterates/validates here.

## NAV rule (why these tasks)

A task is kept only if ALL hold (encoded in the picker, never in GT):

1. `len(gold_files(patch)) >= 2` -- multi-file fix.
2. non-empty `FAIL_TO_PASS` -- a real, gradeable test exists.
3. issue->file **non-obvious**: no gold file basename (sans ext) is a substring of the issue's first line. The issue does not name the file.

Single-file single-symbol feature adds are excluded by (1). Language is derived from the **gold patch file extensions** (Multilingual has no `language` column).

## Stratification + split

3 per language for {go, rust, typescript, javascript} (Multilingual) + 3 python (Verified) = 15. Within a language, tasks are ordered by `sha256(instance_id)` and the first 3 taken, then assigned TRAIN/VAL/TEST in that order -- so each split holds one task per language (deterministic, content-addressed, never hand-picked).

## The 15 picks

| instance_id | language | split | gold_file_count | why-navigation (issue 1st line) |
|---|---|---|---:|---|
| `hashicorp__terraform-34900` | go | TRAIN | 3 | Set nested block with sensitive attribute always detects changes on `v1.8.0-rc1` |
| `hashicorp__terraform-35543` | go | VAL | 8 | terraform validate does not validate import blocks |
| `prometheus__prometheus-10633` | go | TEST | 2 | PuppetDB Service Discovery - Numerical parameters not converted to __meta_puppetdb_parameter labels |
| `mrdoob__three.js-26589` | javascript | TRAIN | 3 | Clone the material array when Object3D.clone() is called |
| `mrdoob__three.js-25687` | javascript | VAL | 2 | Serialization of PerspectiveCamera |
| `immutable-js__immutable-js-2005` | javascript | TEST | 2 | OrderedSet equality and hashCode bug |
| `django__django-15563` | python | TRAIN | 2 | Wrong behavior on queryset update when multiple inheritance |
| `sympy__sympy-13877` | python | VAL | 2 | Matrix determinant raises Invalid NaN comparison with particular symbolic entries |
| `django__django-11885` | python | TEST | 2 | Combine fast delete queries |
| `tokio-rs__axum-2096` | rust | TRAIN | 3 | Nested `fallback()` routes regression with `merge()` |
| `sharkdp__bat-2835` | rust | VAL | 2 | Long file paths break header |
| `tokio-rs__axum-1934` | rust | TEST | 4 | Axum 0.6.15 seems to break ServeDir completely |
| `facebook__docusaurus-9183` | typescript | TRAIN | 3 | Allow case-insensitivity for code block language |
| `vuejs__core-11589` | typescript | VAL | 2 | Inconsistent execution order of Sync Watchers on `computed` property |
| `preactjs__preact-3689` | typescript | TEST | 3 | support errorInfo in useErrorBoundary |

Each row passed NAV: >=2 gold files, has FAIL_TO_PASS, and the gold file is not named in the issue's first line -- so localization is the hard part GT must earn.

## Trap (cold-successor note)

TypeScript is the tight bucket: exactly 3 NAV-passing tasks exist in Multilingual (measured 2026-06-17 -- go 6, javascript 6, rust 14, **typescript 3**). There is ZERO slack for TS. If a TS docker image fails to pull/build at eval time there is no substitute under the current NAV rule -- relax MIN_GOLD_FILES or the obviousness filter for TS, or accept <3 TS, and re-run the picker. Do not hand-edit the manifest.
