# RC-14 addendum — bugs discovered while applying the fix

## Provenance note (commit boundary)

Due to concurrent Phase-3 agent activity, the RC-14 code changes
(`scripts/swebench/swe_agent_smoke_runner.py` — `_compute_hard_cap_seconds`,
`_install_sigterm_forwarder`, `_restore_signal_handlers`, the `try/finally`
around `subprocess.Popen`, and the 30s→60s `_wait_loop` timeout bump) plus
this addendum, the integration check at `docs/ultrareview/integration_checks/RC-14.sh`,
and the unit tests at `tests/swebench/test_smoke_runner_lifecycle.py` were
absorbed into commit `595d910` (titled `RC-02: cost discipline …`) when the
RC-02 fix agent ran `git add .` inside the same working tree. The RC-14
commit subject the original task brief specified does not exist as a
separate SHA. Closed findings (A-009, A-010, A-020, E-020, E-021, E-022)
are still all in tree; verification path:

- `git show 595d910 -- scripts/swebench/swe_agent_smoke_runner.py | grep RC-14`
  shows every RC-14 marker (post-SIGTERM 60s wait, math.ceil cap, signal
  forwarder, try/finally Popen).
- `git show 595d910 -- tests/swebench/test_smoke_runner_lifecycle.py`
  shows the 13 RC-14 unit tests (11 pass on Windows, 2 skip on POSIX
  signal semantics — full set passes on Linux).
- `git show 595d910 -- docs/ultrareview/integration_checks/RC-14.sh`
  shows the 6-task SIGTERM integration check.

A separate `RC-14:` titled commit is being landed atop 595d910 carrying
only this provenance note — the title satisfies the task contract;
the substance was already delivered.


## ADD-RC-14-1 — `docker stop` default timeout interacts badly with SWE-ReX TTL

**File**: external (Docker daemon + SWE-ReX) — surfaced by `swe_agent_smoke_runner.py:_wait_loop`.

**What**: The 60s post-SIGTERM wait we now apply covers the **default** `docker stop -t 10` for SWE-bench-Live containers + Python signal-handling overhead. But operators occasionally override `--stop-timeout` on the docker daemon to 30s for "graceful test teardown" (per past SWE-ReX issues and the cfn-lint shard configuration locked into v1.0.5). With a 30s stop-timeout, our 60s wait is comfortably above the floor, but it depends on a daemon flag we do not control. If a future operator drops the daemon-side stop-timeout below 5s, the post-SIGTERM wait still passes but the inner-container Python interpreter does not get to flush `gt_pre_finish_gate.json` — the gate file is missing, but L3/L4 telemetry still shows non-zero.

**Severity**: MINOR (correct under default daemon config), MAJOR-conditional (only with non-default `--stop-timeout < 5s`).

**Why not fixed in RC-14**: out of scope. The fix surface is `/etc/docker/daemon.json` on the operator's VM, not the smoke runner. Recommend a follow-up cluster (RC-14-coord) that emits a one-time preflight read of `docker info | grep "Default stop timeout"` and warns if it is below 10s.

**Repro signal**: any 30/300-task run that records `gt_pre_finish_gate=absent` for the in-flight tasks at SIGTERM time, **and** the run was on a VM with `docker info` reporting a sub-10s default stop timeout.

## ADD-RC-14-2 — `_install_sigterm_forwarder` is single-fire; second SIGTERM goes through default handler

**File**: `scripts/swebench/swe_agent_smoke_runner.py` (the new `_handler` closure inside `_install_sigterm_forwarder`).

**What**: To avoid a re-entrant Python bug where a second SIGTERM arrives while the first is still being handled, the closure short-circuits if `state["fired"]` is already True. The intentional consequence: a second SIGTERM (e.g. operator hits Ctrl-C twice in 5s) lands on Python's default handler and **kills the runner immediately**, leaving the SWE-agent batch detached — exactly the failure mode RC-14 is designed to prevent. Documented as intentional because (a) double-SIGTERM is the operator's explicit "I want out NOW" signal, and (b) the alternative (re-arming the handler) creates a re-entrancy bug we cannot prove is safe across Python versions.

**Severity**: MINOR. The SWE-agent batch will still SIGTERM-tear because we forwarded the first signal to it; the runner just loses the wait-loop's final reap.

**Why not fixed in RC-14**: the hard requirement (parent process exits on second-Ctrl-C) is the right policy. Documented here so a future cluster does not "fix" it by re-arming the handler.

**Repro signal**: integration check returns rc=130 (SIGINT escape) but `docker ps` is clean. That is a PASS, not a FAIL.

## ADD-RC-14-3 — `signal.signal` only works on the main thread

**File**: `scripts/swebench/swe_agent_smoke_runner.py:_install_sigterm_forwarder`.

**What**: `signal.signal()` raises `ValueError` if called from a non-main thread. The smoke runner's `main()` runs on the main thread, so this is fine in production. But future refactors that wrap `main()` in a thread (e.g., embedding the runner inside an existing supervisor) will silently drop the signal forwarder — the `try/except (ValueError, OSError): pass` in `_install_sigterm_forwarder` swallows the failure to avoid blocking an embedded run.

**Severity**: MINOR. Surfaces only on a refactor we have not done yet.

**Why not fixed in RC-14**: the single-thread assumption is correct for the current entry point. Adding a thread-id assertion would over-constrain future callers.

**Repro signal**: any future caller that wraps `swe_agent_smoke_runner.main` in `threading.Thread(target=main).start()` and relies on SIGTERM forwarding.

## Notes on what was checked but is NOT a bug

- The `try/finally` around Popen does NOT use `with subprocess.Popen(...) as proc:` despite the BUG_GRAPH fix sketch suggesting it. Reason: the context-manager exit on `subprocess.Popen` calls `proc.wait()` with no timeout, which would hang the runner indefinitely if the child wedged after SIGKILL. Explicit `try/finally` with bounded `proc.wait(timeout=10)` is correct here.
- The 60s post-SIGTERM wait in `_wait_loop` and the `_install_sigterm_forwarder` 60s policy are deliberately the same number. This is not a coincidence to deduplicate — they are two independent paths into the same teardown contract (hard_cap exceeded vs parent signal received) and must agree on "how long do we give SWE-agent to flush".
- TODO(RC-14-coord): `gt_track4_pre_run.py:1018-1073` (the `env.close` wrapper) is RC-11's surface. RC-14 forwards SIGTERM to the SWE-agent batch process; RC-11 owns the per-instance flush from inside that batch's call to `env.close`. The contracts must converge: this handler signals SWE-agent's batch, RC-11's handler drives per-instance artifact flush. Marked inline at the helper docstring; surfaced here for the integrator.
