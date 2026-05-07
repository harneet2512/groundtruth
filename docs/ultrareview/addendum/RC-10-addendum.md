# RC-10 Addendum

Cluster: RC-10 — Telemetry verifier disconnect.
Status: shipped. Findings closed: D-001, D-002, D-003, D-004, D-005, D-006,
D-008, D-009, D-011, D-015, E-010, E-015.

## New bugs discovered while implementing the fix

None at the time of writing. The cluster's 12 member findings were closed
as designed by the BUG_GRAPH fix sketch (a)..(j); no incidental defects
surfaced during the unit-test pass.

## Coordination notes

- `_append_completion_log` (Track 4 close-wrap line writer) is left in
  place behind a `# TODO(RC-10-coord)` so the existing pullback-hook
  tests stay green. Full removal of the legacy `task=… L3_edits=…` line
  format coordinates with RC-03 (concurrency cluster) which also
  touches the close-wrap path. Once RC-03 lands, the canonical-writer
  contract (smoke runner reads `gt_completion_summary.json` sidecar)
  fully owns `gt_layers.log` writes.

- Rate-gate denominator correction for `partial_pull` tasks is
  surfaced in the verify_report markdown (n_partial_pull row) but does
  NOT retro-correct the per-arm rates because those are pre-computed at
  write time inside `gt_arm_summary.json`. A future fix that threads
  per-task `partial_pull` into the arm summary writer would let
  verify_report exclude those tasks from the denominator outright.
  Tracked as a post-RC-10 follow-up.
