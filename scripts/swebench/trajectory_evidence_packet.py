"""Deterministic trajectory evidence packet — the anti-context-rot audit joiner.

Problem this solves (2026-07-20): LLM audit agents cannot hold a full trajectory
in context; forced to grep they conclude wrongly, forced to read fully they rot.
Everything computable must therefore be computed HERE, deterministically:

For one task artifact dir this tool emits a SMALL JSON packet containing, for
every delivered runtime-ledger row, the BYTE-EXACT seal join into the trajectory
(message index + char span, found by sha256(window)[:16] == content_sha256_16 at
chars_delivered length — suffix-anchored fast path first, full scan fallback),
plus the receipt-sidecar transitions for the same identity and the surrounding
assistant messages' indices. An audit agent then receives ONLY the packet plus
the specific message texts it asks for — it never searches, so it can neither
grep-and-conclude nor overflow.

Usage:
  python trajectory_evidence_packet.py <task_dir> [--out packet.json]
       [--context-chars 400]

Pure stdlib; read-only; deterministic (no time/random in output ordering).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys


def _sha16(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def _load_trajectory(task_dir: str) -> list[dict]:
    for name in os.listdir(task_dir):
        if name.endswith("trajectory.json"):
            with open(os.path.join(task_dir, name), encoding="utf-8") as fh:
                data = json.load(fh)
            msgs = data.get("messages") if isinstance(data, dict) else data
            if isinstance(msgs, list):
                return msgs
    raise FileNotFoundError("no *trajectory.json in " + task_dir)


def _msg_text(msg: dict) -> str:
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # responses-api shape
        return "".join(
            part.get("text", "") for part in content if isinstance(part, dict))
    return ""


def _ledger_rows(task_dir: str) -> list[dict]:
    rows: list[dict] = []
    for name in sorted(os.listdir(task_dir)):
        if name.startswith("gt_runtime_ledger_") and name.endswith(".jsonl"):
            with open(os.path.join(task_dir, name), encoding="utf-8") as fh:
                for i, line in enumerate(fh):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        rows.append({"_row_index": i, "_malformed": True})
                        continue
                    row["_row_index"] = i
                    rows.append(row)
            break
    return rows


def _receipts(task_dir: str) -> list[dict]:
    out: list[dict] = []
    for name in sorted(os.listdir(task_dir)):
        if name.startswith("gt_receipts") and name.endswith(".jsonl"):
            with open(os.path.join(task_dir, name), encoding="utf-8") as fh:
                for i, line in enumerate(fh):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    env = rec.get("envelope") or {}
                    binding = env.get("observation_binding") or {}
                    out.append({
                        "line": i,
                        "kind": rec.get("kind"),
                        "transition": rec.get("transition"),
                        "producer": env.get("producer"),
                        "dedup_key": env.get("dedup_key"),
                        "binding_candidate_id": binding.get("candidate_id"),
                        "rendered_sha16": (env.get("rendered_bytes_hash") or "")[:16],
                        "produced_dedup_key": rec.get("produced_dedup_key"),
                    })
            break
    return out


def _join_seal(messages: list[str], target_sha16: str, length: int):
    """Find (msg_index, start) whose `length`-char window hashes to target.

    Suffix-anchored fast path (deliveries are appended suffixes), then bounded
    full scan. Returns first match in message order (deterministic)."""
    if not target_sha16 or not length or length <= 0:
        return None
    for mi, text in enumerate(messages):
        if len(text) < length:
            continue
        # fast path: window ending at end-of-message
        window = text[len(text) - length:]
        if _sha16(window.encode("utf-8", "surrogatepass")) == target_sha16:
            return {"msg_index": mi, "start": len(text) - length, "anchored": "suffix"}
    for mi, text in enumerate(messages):
        n = len(text)
        if n < length:
            continue
        enc = text.encode("utf-8", "surrogatepass")
        # only exact-char windows; scan with stride 1 (bounded by message count)
        if len(enc) == n:  # pure-ASCII fast case: bytes==chars
            for start in range(0, n - length + 1):
                if _sha16(enc[start:start + length]) == target_sha16:
                    return {"msg_index": mi, "start": start, "anchored": "scan"}
        else:
            for start in range(0, n - length + 1):
                window = text[start:start + length]
                if _sha16(window.encode("utf-8", "surrogatepass")) == target_sha16:
                    return {"msg_index": mi, "start": start, "anchored": "scan"}
    return None


def build_packet(task_dir: str, context_chars: int = 400) -> dict:
    msgs = _load_trajectory(task_dir)
    texts = [_msg_text(m) for m in msgs]
    roles = [str(m.get("role", "?")) for m in msgs]
    rows = _ledger_rows(task_dir)
    receipts = _receipts(task_dir)

    deliveries = []
    for row in rows:
        if row.get("_malformed") or row.get("outcome") != "delivered":
            continue
        sha = row.get("content_sha256_16") or ""
        chars = int(row.get("chars_delivered") or 0)
        join = _join_seal(texts, sha, chars) if sha and chars > 0 else None
        entry = {
            "ledger_row": row.get("_row_index"),
            "layer": row.get("layer"),
            "fact_class": row.get("fact_class"),
            "candidate_id": row.get("candidate_id"),
            "iteration": row.get("iteration"),
            "event_type": row.get("event_type"),
            "file_path": row.get("file_path"),
            "content_sha256_16": sha,
            "chars_delivered": chars,
            "seal_join": join,
        }
        if join is not None:
            mi, start = join["msg_index"], join["start"]
            entry["delivered_text_head"] = texts[mi][start:start + min(chars, context_chars)]
            # the assistant messages immediately after the delivery (consumption zone)
            entry["next_assistant_msgs"] = [
                i for i in range(mi + 1, min(mi + 8, len(msgs)))
                if roles[i] == "assistant"][:3]
        entry["receipts_same_identity"] = [
            r for r in receipts
            if r.get("binding_candidate_id") == row.get("candidate_id")
            or r.get("rendered_sha16") == sha]
        deliveries.append(entry)

    joined = sum(1 for d in deliveries if d["seal_join"] is not None)
    return {
        "schema": "gt.trajectory_evidence_packet.v1",
        "task_dir": os.path.basename(os.path.normpath(task_dir)),
        "message_count": len(msgs),
        "roles": {"assistant": roles.count("assistant"), "user": roles.count("user"),
                  "system": roles.count("system")},
        "delivered_rows": len(deliveries),
        "seal_joined": joined,
        "seal_join_misses": [d["ledger_row"] for d in deliveries if d["seal_join"] is None],
        "receipt_lines": len(receipts),
        "deliveries": deliveries,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("task_dir")
    ap.add_argument("--out", default="")
    ap.add_argument("--context-chars", type=int, default=400)
    args = ap.parse_args(argv)
    packet = build_packet(args.task_dir, context_chars=args.context_chars)
    text = json.dumps(packet, indent=1)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"packet -> {args.out}  (deliveries={packet['delivered_rows']}, "
              f"joined={packet['seal_joined']}, misses={len(packet['seal_join_misses'])})")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
