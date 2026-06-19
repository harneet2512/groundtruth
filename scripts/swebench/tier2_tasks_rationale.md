# Tier 2 Held-Out Generalization Set — Rationale

Picked 2026-05-03, BEFORE TEG-1 runs. Held-out gate: zero overlap with the Tier 1 paranoid-mode 15.

**Scope decision (2026-05-03, user-locked):** Python-only this week. The 3 non-Python tasks (BurntSushi/ripgrep, zeromicro/go-zero, iamkun/dayjs) are removed from this list and the Multi-SWE-bench harness wiring is deferred. Generalization claim narrows to **Python multi-repo**, NOT language-agnostic. This is documented explicitly in `touch_edit_gap_next_track.md` so the win conditions reflect the narrower claim.

## Composition (post-trim)

| Bucket | Count | Source |
|---|---|---|
| Python (SWE-bench-Live lite) | 12 | huggingface.co/datasets/SWE-bench-Live/SWE-bench-Live, split=lite |
| **Total** | **12** | — |

## Repos (12 distinct, >= 10 required for "multi-repo" claim)

deepset-ai/haystack, reflex-dev/reflex, stanfordnlp/dspy, pallets/flask, pylint-dev/pylint, pydata/xarray, falconry/falcon, urllib3/urllib3, ipython/ipython, encode/starlette, yt-dlp/yt-dlp, kedro-org/kedro.

## Patch scope

Every selected task has 1–8 source files in its gold patch:
- min = 1, max = 6, median = 1.
- `aws-cloudformation/cfn-lint-3798` was rejected (15 test files — outlier even though 2 src files).
- conan-io rollups and similar large patches were excluded.

## Tier-1 overlap check

Tier-1 repos: pytorch/torchtune, python-babel/babel, keras-team/keras, projectmesa/mesa, conan-io/conan, tox-dev/tox, cyclotruc/gitingest, amoffat/sh, sissbruecker/linkding, pdm-project/pdm, sphinx-doc/sphinx, matplotlib/matplotlib, dynaconf/dynaconf, joke2k/faker, modelcontextprotocol/python-sdk.

None of the 12 Tier-2 task IDs share a repo with the Tier-1 set. Confirmed by string match against `<owner>__<repo>` prefix of each instance ID.

## Selection bias check

Tasks were drawn from the SWE-bench-Live lite list in order of dataset position (first instance per fresh repo encountered). No filtering on issue text content or expected TEG-1 win likelihood. The set was frozen before any TEG-1 runs.

## Removed tasks (history, for record-keeping)

These three were in the original Tier 2 set but removed when scope narrowed to Python-only on 2026-05-03:

- `BurntSushi__ripgrep-2295` (Rust)
- `zeromicro__go-zero-2537` (Go)
- `iamkun__dayjs-2420` (JavaScript)

If/when Multi-SWE-bench harness wiring is done in a future track, these are the natural seed tasks for a multi-language extension.

## Caveats

- Multi-language generalization is NOT tested in this experiment. The win/loss conclusion will read "TEG-1 generalizes across Python repos" or not — neither extends to other languages.
- SWE-bench-Live lite tasks may not be representative of all Python repos in the wild; a "real generalization" claim would need a third tranche from a different distribution (e.g., SWE-bench-Live full or SWE-bench Verified). That is out of scope for this experiment.
