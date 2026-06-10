# GO / NO-GO — Two-Pathway Trials (consolidated, 2026-06-10)

Both pipelines passed a read-only 5-stage pipeline audit (gt_gt as audit surface). In BOTH, the
**mirrored core verified clean at source** (witness, brief consume, patch attach, pipefail, gates,
fail-closed env — checked against installed pier 0.2.0 / litellm 1.86.1 / minisweagent 2.2.8 /
swebench 4.1.0). Every defect was in the NEW per-path surface; all fixed (commits 56a38559, bed99544).

## PATH A — GCP VM · DeepSWE-113 · gemini-3-flash-preview
| gate | verdict |
|---|---|
| 5-task trial | **GO** — launch with VERTEXAI_LOCATION=us-east1, REUSE unset, MAX_TASKS=5, PARALLEL=4, STOP_AT_COST=25 |
| full 113 | **GO after trial** — STOP_AT_COST coded, deep-metrics fixed, container-leak closed, reuse-gate hardened |
Fixed: location default, gt_deep_metrics VM-layout discovery + model-agnostic cost, mini-swe pin 2.2.8,
certs/embedder into rows, STOP_AT_COST halt, timeout teardown, reuse-gate, backoff, RATE_LIMIT class.
Trial criteria (UNVERIFIED-NEEDS-TRIAL): in-container Vertex auth through squid; witness greps fire on
real VM logs; embedder identity gte/768; nonzero cost; deep-metrics finds the trajectory; no container
leak; tarball secret-scan = 0 hits; non-allowlisted egress denied.

## PATH B — GHA · SWE-bench Verified-500 · deepseek-v4-flash
| gate | verdict |
|---|---|
| 5-task trial | **GO** — dispatch with max_parallel=20, include >=1 django + 1 sympy |
| full 500 | **GO after trial** — deep-metrics emitter built, eval_no_report INFRA, 256-cap fatal, /testbed path-norm fixed |
Fixed: verified_deep_metrics.py emitter + workflow step, eval_no_report->INFRA, 256-cap fatal,
/testbed abs-path normalization + exact-match witness queries, HF offline on substrate run, cost_limit
honesty, max_parallel 20. Trial criteria: django/sympy substrate wall-time in 60 min (THE key unknown —
61% of Verified is django+sympy, no 46k-node measurement exists on disk); live evidence fire-rate;
brief relevance; image-reuse disk; thinking-disable honored; no key in traj text.

## Remaining before launch (both gated on the user)
1. VM sweep's 4 answers (SIGKILL survival / scc-jdtls / dynamodb gate-1 / per-language table).
2. GCP: enable aiplatform.googleapis.com + create a minimal Vertex-AI-User SA (key on the VM only). [await OK]
3. Pier + mini-swe install on the VM.
4. The two GCP-state-change OKs + the explicit per-trial "launch".
