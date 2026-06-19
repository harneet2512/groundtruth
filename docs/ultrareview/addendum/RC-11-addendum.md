# RC-11 Addendum — new bugs found while implementing the cluster fix

Date: 2026-05-06
Cluster: RC-11 (cost-exit / call-limit-exit / SIGTERM bypass artifact pull)

## Summary

No additional bugs surfaced while implementing the RC-11 fix in
`scripts/swebench/gt_track4_pre_run.py`. The atexit-handler-in-wrapper
design closes E-017 + E-019 cleanly and the existing 26 pullback-hook
tests continued to pass after the change (now 35 with the 9 new RC-11
tests).

## Coordination items raised against other clusters

### TODO(RC-11-coord): RC-10 owns the schema flow into verify_report

The RC-11 fix writes `exit_status=<cost_exit|call_exit|autosubmit|atexit|
normal>` to the per-task `gt_layers.log` line. Whether and how
`verify_report.py`'s `engagement_rate` computation excludes / splits the
non-`normal` cohort is owned by RC-10. The integration check
(`docs/ultrareview/integration_checks/RC-11.sh`) verifies (1)–(3) of
the cluster contract — pull happened, L4 reflects reality, cohort marker
present — and explicitly defers (4) (denominator exclusion) to RC-10
via the inline TODO.

If RC-10 has already landed the rate-gate exclusion by the time this
addendum is read, extend RC-11.sh's step (4) to invoke
`scripts/swebench/verify_report.py append --run-dir <out>` and assert
the engagement_rate denominator excludes the cost-exited task.

### Synchronous flush contract on autosubmit / cost-exit / call-exit

RC-11's `on_instance_completed` change invokes `cache["atexit_flush"]()`
synchronously when the result's `exit_status` is one of `autosubmitted`
/ `exit_cost` / `exit_context`. This is *not* relying on Python's true
process-exit atexit callback — that only fires once per process and
would lose mid-batch artifacts when the next task starts. The atexit
handler is registered as a process-exit safety net AND is callable as a
plain function from anywhere with access to the cache.

If RC-20 (or any future cluster) replaces the close-wrap with a different
mechanism, it MUST preserve the `cache["atexit_flush"]` callable contract
or migrate the synchronous-invocation site in `on_instance_completed`.

### Idempotency contract preserved

The atexit flush is idempotent vs the close-wrap path via
`cache["completion_logged"]`. The existing pullback-hook test
`test_completion_skips_when_close_wrap_already_logged` continues to
pass, so any consumer that relied on a single per-task line in
gt_layers.log keeps working.

## No new bugs

(Section reserved for future addenda if the integration check reveals
behaviour not covered by the unit tests.)
