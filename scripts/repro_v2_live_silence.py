"""Reproduce the V2_LIVE silence bug locally.

Replays the post_view + post_edit actions from an archived v2_live trajectory
through the FINAL_ARCH_V2 router code path. If the router pipeline itself is
intact, it will emit (or explicitly suppress) for each event. If we see zero
router output for non-trivial inputs, the bug is in the router or its inputs.
If we see proper output, the bug is in the wrapper's call site (env-var
propagation, branch coverage, or telemetry routing).

Usage:
    python scripts/repro_v2_live_silence.py <output.jsonl>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

import os
os.environ.setdefault("GT_ROUTER_V2", "live")

from groundtruth.router import CollaborationRouter
from groundtruth.state.agent_state import AgentState
from groundtruth.router.decisions import SuppressionReason


def _classify_event(ev: dict) -> tuple[str, str] | None:
    action = ev.get("action") or ""
    args = ev.get("args") or {}
    path = ""
    if isinstance(args, dict):
        path = args.get("path") or ""
        if not path:
            # str_replace_editor command form
            cmd = str(args.get("command") or "")
            for line in cmd.splitlines():
                if "path=" in line:
                    path = line.split("path=", 1)[1].strip().strip("'\"")
                    break
    if action == "read" and path:
        return ("post_view", path)
    if action == "edit" and path:
        return ("post_edit", path)
    return None


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 1
    path = Path(sys.argv[1])
    if not path.is_file():
        print(f"missing: {path}")
        return 1

    with open(path, encoding="utf-8", errors="replace") as fh:
        data = json.loads(fh.readline())
    history = data.get("history", [])
    instance_id = data.get("instance_id", "diag-task")
    print(f"Loaded trajectory: instance={instance_id} history_len={len(history)}")

    state = AgentState.load_or_create(
        task_id=instance_id, max_iterations=100, repo_root="/workspace/" + instance_id
    )
    # graph.db: try common locations
    db_candidates = [
        f"/tmp/canary_failed/canary-v2_live-{instance_id}/results/SWE-bench-Live__SWE-bench-Live-lite/CodeActAgent/deepseek-v4-flash_maxiter_100/graph.db",
        ".tmp_holdout/bugs/" + instance_id.split("__")[-1] + "/graph.db",
    ]
    db_path = next((p for p in db_candidates if Path(p).exists()), "")
    print(f"graph.db: {db_path or '(none)'}")

    router = CollaborationRouter(
        state=state, db_path=db_path, repo_root="/workspace/" + instance_id
    )

    emit_count = 0
    suppress_count = 0
    by_reason: dict[str, int] = {}
    for ev in history:
        cls = _classify_event(ev)
        if cls is None:
            continue
        kind, p = cls
        if kind == "post_view":
            em = router.on_view(p)
        else:
            em = router.on_edit(p, [])
        if em.emit:
            emit_count += 1
            print(f"  EMIT {kind} path={p} kind={em.kind.value} band={em.band} text_len={len(em.evidence_text)}")
        else:
            suppress_count += 1
            r = em.suppression_reason.value if em.suppression_reason else "none"
            by_reason[r] = by_reason.get(r, 0) + 1

    print(f"\n=== summary ===")
    print(f"emits:    {emit_count}")
    print(f"suppress: {suppress_count}")
    print(f"by reason: {by_reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
