# smoke_v5_failed_2026-04-24

TTD reproduction archive for the verify_report rate-contract bug.

## Origin

Run: 10-task parallel smoke on `gt-runner-gcp` (project-c9a6fdd8-...), DeepSeek
v3.2-maas via Vertex LiteLLM proxy. Launched 2026-04-24 05:16:17 UTC, all 10/10
shards per arm completed cleanly at 05:39:15 UTC. `verify_report.py append`
FAILed both arms 5 gates each, including `delivery_rate=0.00` /
`engagement_rate=0.00` even though raw chain totals were all > 0.

## Why these files

Only the minimal artifacts required to reproduce the failure are committed:
- `{nolsp,lsp}/gt_arm_summary.json` — the real summary dicts as the reporter
  emitted them. Inspect these to confirm `delivery_rate` / `engagement_rate`
  are missing keys while `ack_armed_total` / `steer_delivered_total` / etc.
  are present.
- `{nolsp,lsp}/gt_report.csv` — per-task rows so `verify_report.compute()`
  loads non-empty row data.

Full per-task trajectories (run.log, gt_hook_telemetry.jsonl, preds.json,
Docker logs — 924 files, ~220 MB) are NOT committed; they live at
`tmp/forensics/smoke_v5_failed_2026-04-24_full.tgz` on the original workstation
for any forensic follow-up.

## How this is used

`tests/unit/test_verify_report_rate_contract.py::test_failed_smoke_v5_archive_now_passes_rate_gates`
parametrizes over `nolsp` and `lsp` and asserts `delivery_rate >= 0.65` and
`engagement_rate >= 0.80` after the contract fix. Before the fix those
assertions failed. After the fix they pass. Do not delete these files — they
are the end-to-end proof the failure mode is closed.
