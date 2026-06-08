"""GroundTruth benchmark RELIABILITY harness.

Read-only contract emitters + classifier that PROVE which surface a benchmark
task fails on — orchestration, container runtime, or the GT evidence pipeline
(graph base -> LSP -> embedder -> absorption -> hook) — so a failure is
classified to a specific seam with evidence, never called "GT-quality" until
the infra surfaces are proven present-or-correctly-classified-no-op.

Nothing here changes ranking, resolver, gate thresholds, or any GT behavior.
Each emitter consumes already-produced artifacts (graph.db, logs, snapshots).

Module layout (per the audit plan):
  graph_contract.py      — graph base dimensions from graph.db (SQL, read-only)
  lsp_contract.py        — LSP_METRICS + verified/corrected/deleted + NO_OP rule
  embedder_contract.py   — both ONNX call sites, related>unrelated probe, no-download
  absorption_contract.py — per-stage candidate lineage from GT_AUDIT snapshots
  container_contract.py   — in-container proof, flags, baked model/paths
  run_contract.py        — orchestration determinism (host-side, GHA env)
  hook_contract.py       — brief delivered/correct/consumed (host-side, output.jsonl)
  classify.py            — surface verdicts -> one final_class
  report.py              — audit report + csv
  emit_incontainer.py    — orchestrator: runs the in-container emitters, writes JSON
"""

CONTRACT_SCHEMA_VERSION = "reliability-1"
