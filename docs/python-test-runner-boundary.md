# Python test runner recognition

The installed GT rehearsal exposed a protocol gap: `python3 -m unittest`
was recognized by canonical GT but discarded by the harness's separate runner
regex; `python3 -B -m unittest` was missed by both. Seeing a passing test in the
agent transcript therefore did not establish its delivery to GT consumers.

`groundtruth.runtime.patterns.classify_test_observation` remains the canonical
owner. Python's no-argument execution switches may precede `-m`; case-sensitive
switch matching keeps version/help and code-string invocations out. Options that
take arguments are not inferred. The standalone DeepSWE bootstrap fallback
retains the same interpreter-switch expression.

Regression coverage includes actual execution forms, combined switches, failure
and success summaries, and nonexecution forms. Harness consumers must reuse the
canonical classification, preserve subprocess bytes and exit status, and never
infer success from a pipeline's final exit alone. Installed verification must
assert structured execution evidence for the initial failing and final passing
checks, bound to their respective source revisions. A transcript-only repair
proof cannot close that requirement.

This repair does not establish complete product or benchmark readiness. The
continuing implementation and remaining gates are recorded under Linear HAR-83.
