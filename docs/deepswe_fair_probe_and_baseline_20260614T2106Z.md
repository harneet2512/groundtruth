# DeepSWE fair-probe selection + the no-baseline gap (CLUSTER E / E3 + E4)

2026-06-14. Companion to `scripts/deepswe_fair_probe_filter.py`. This documents how to
pick DeepSWE tasks that can actually CREDIT GroundTruth, and the unresolved pairing gap.

## 1. The problem (why a DeepSWE `RESOLVED` can be causally empty)

A flip only counts as a GT win when the trajectory was right: GT delivered correct
context → the agent consumed it → localized/reasoned through it → wrote the correct fix
FOR THAT REASON (CLAUDE.md "RESOLVED IS NOT THE PRIZE"; gt_trial.md §4/§5 `fair_probe`).

Many DeepSWE tasks are `feature_request` / `enhancement` / spec-style: the `instruction.md`
PRE-NAMES the gold the agent must edit — the files AND the new public symbols. On those,
the agent self-localizes straight from the prompt; GT is uncreditable no matter what it
delivers. Example (held-out): `fastapi-implicit-head-options` names `applications.py`,
`routing.py`, `methods.py` (3/4 gold files) and the public symbols
`ImplicitMethodTrackingMiddleware`, `get_stats`, `reset_stats` — public-surface coverage
0.70. The agent does not need GT to find the gold.

## 2. The pre-filter (E3) — META ONLY, never GT product logic

`scripts/deepswe_fair_probe_filter.py` scores every task OFFLINE:

    gold_name_coverage = fraction of the GOLD PUBLIC SURFACE that string-appears in
                         instruction.md
      public surface = gold file BASENAMES (with extension)
                     + NEW PUBLIC top-level symbols solution.patch adds
                       (private `_`-prefixed helpers excluded — every spec calls those
                        "implementation details"; counting them masks self-localization).

- coverage ≥ threshold (default 0.70) → **SELF-LOCALIZING** → AVOID as a GT probe.
- low coverage → **fair probe** → the pool to run GT on.

It reads `solution.patch` (the GOLD). That is allowed HERE and ONLY here: it is RUN
SELECTION metadata computed before the agent runs. It is **never imported by, called
from, or surfaced to** any GT product module (brief / localizer / oracle / hooks). No
task-id or gold label crosses into product logic. The four pillars hold: generalized
(any repo/language with the deepswe layout; per-language token grammars, not per-task
patterns), correct-or-quiet (basename-with-extension primary; guarded stem fallback;
common domain-word stems like `cache`/`config` excluded so behavior prose never earns
false file credit), research-grounded (the fair-probe concept = gt_trial §4/§5 +
project memory `feedback_gttrial_audit_chronological_not_grep`).

### Current ranking (113 tasks, threshold 0.70)
- 10 SELF-LOCALIZING (avoid): `dateutil-rfc5545-timezone-interop` (1.00),
  `psd-tools-blend-range-api`, `kombu-virtual-queue-dead-lettering`,
  `returns-validated-error-accumulation`, `tomlkit-toml-table-converters`,
  `pwntools-tube-multiplexing`, `opa-rego-rule-profiling`,
  `sqlite-utils-safe-import-checkpoints`, `ipython-session-bundle-replay`,
  `fastapi-implicit-head-options` (0.70, the boundary case).
- 103 fair probes; **19 at coverage 0.0** (issue is pure behavior, names no gold) — the
  cleanest probes: `abs-module-cache-flags`, `prometheus-typed-label-sorting`,
  `pest-character-class-coalescing`, `sql-formatter-bigquery-pipe-formatting`,
  `oxvg-structural-selector-preservation`, `helm-array-merge-strategies`,
  `expr-try-catch-errors`, `anko-typed-variable-bindings`, ... across go/rust/ts/python.

Regenerate: `python scripts/deepswe_fair_probe_filter.py D:/Groundtruth/deepswe-bench/tasks
--threshold 0.70 --json out.json`.

## 3. The no-baseline gap (E4) — UNRESOLVED, must be closed before any flip claim

**There is NO DeepSWE/pier baseline on disk.** The only frozen baseline is
`.claude/reports/full300_baseline_ohdeepseek_20260531/FINAL_resolved_300_20260531.json`
(87/300) — and it is **OpenHands CodeActAgent + deepseek-v4-flash on SWE-bench-Live**.
That is a DIFFERENT harness (OH, not pier+GTMiniSweAgent), a DIFFERENT model, and a
DIFFERENT task set (SWE-bench-Live, not the 113 DeepSWE tasks). A DeepSWE `RESOLVED` is
therefore **unpairable** against it.

**Rule:** do NOT reuse the OH baseline for any DeepSWE pairing. A flip = `resolved AND
NOT baseline_pass` is undefined when the two arms differ in harness/model/task-set —
the per-task delta has no meaning, and the paired Wilcoxon (gt_trial §4.1) cannot run.

**What must happen first (owed):**
1. Run a **DeepSWE GT-OFF baseline arm** on the SAME 113 tasks, SAME harness
   (pier + GTMiniSweAgent), SAME model, GT disabled. Persist the deep 8-dp record per
   task (CLAUDE.md logging contract) + `report.json`/`result.json` verdicts.
2. Then run the **GT-ON arm**, paired PER-TASK against that DeepSWE baseline (Wilcoxon /
   sign-test on the per-task delta — never avg-subtraction; never the OH file).
3. Restrict flip claims to the **fair-probe** subset (§2) so a flip means GT caused it,
   not the prompt pre-localizing the gold.

Until (1) exists, every DeepSWE result is reported as "resolved; unpaired — no DeepSWE
baseline" and CANNOT be cited as a flip.

## 4. The verifier-void prerequisite (E1/E2) — fixed, so the verdict exists at all

A flip needs a verdict; the DeepSWE verifier produced none on Windows-host runs because
`tests/test.sh` + `tests/test.patch` were checked out CRLF (no `.gitattributes`,
`core.autocrlf=true`), crashing the inner `/app/test.sh` under the container's strict
shell (`dash`: "end of file unexpected (expecting then)", exit 2) and getting the patch
rejected by `git apply`. Fixed:
- **E1** `deepswe-bench/.gitattributes` pins `*.sh *.patch *.py Dockerfile` to `eol=lf`
  + the working tree was renormalized. Byte-proof: 113/113 test.sh + 113/113 test.patch
  (+ solve.sh + solution.patch) are LF. `dash -n` passes on the LF test.sh; the CRLF
  version fails. Covers all 113 tasks and any future task structurally.
- **E2** pier: `docker_unix.upload_dir` CRLF strip now includes `*.patch`;
  `agent_setup.write_docker_proxy_compose` writes `start-squid.sh` with `newline="\n"`.
  Defense-in-depth (Docker backend); the .gitattributes pin is the all-backend fix.

Only line endings + this meta pre-filter changed. NO test grading logic was modified.
