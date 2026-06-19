# Checkpoint protocol (P0-14)

Architecture checkpoints (CP011–015) ship in `gt_gt.md` §17. Each proof-affecting merge must:

1. Record the checkpoint id in the commit message or `LATEST_TASK.md`.
2. Update `SESSION_SUMMARY.md` with what was proven (Stage 1 deterministic tests, not flips).
3. Pin `GT_SUBSTRATE_DIGEST` when substrate Dockerfiles change.
4. Never re-run the frozen GT-OFF baseline; pair only against
   `.claude/reports/full300_baseline_ohdeepseek_20260531/FINAL_resolved_300_20260531.json`.

CI enforcement: `scripts/ci/check_checkpoint_protocol.py` fails if substrate-changing commits
lack a digest pin note in `docker/Dockerfile.gt-substrate` changelog comment or `LATEST_TASK.md`.
