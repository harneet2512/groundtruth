#!/usr/bin/env python3
"""Fail-closed ACQ source-to-receipt provenance joins.

This grader adds no delivery channel.  It binds source witnesses already
persisted in ``brief_result.json`` to the exact rendered brief block and then
to W1's trajectory-derived consumption receipt.  A source's mere existence,
a matching basename, or a silent delivery can never promote an ACQ row.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from typing import Any

try:  # package import (tests / ``python -m``)
    from scripts.swebench import consumption_ledger as _consumption
    from scripts.swebench.gt_feature_inventory import ACQ_FEATURES
except ModuleNotFoundError:  # direct script-dir import used by gt_feature_metrics CLI
    import consumption_ledger as _consumption  # type: ignore[no-redef]
    from gt_feature_inventory import ACQ_FEATURES  # type: ignore[no-redef]


_BRIEF_SCHEMA = "gt.brief_result.v1"
_CONSUMPTION_SCHEMA = "gt.consumption_ledger.v2"

# Single authority for the candidate-local ACQ components v1 can prove. The
# proof manifest imports this so the supported 6-of-12 set cannot drift.
ACQ_SOURCE_COMPONENTS: dict[str, str] = {
    "graph_validity": "graph_edge_count+witness_verified",
    "structural_depth": "structural_signal_count+components.reach",
    "lexical_FTS5": "fts5_signal_count+components.lex",
    "semantic_embedder": "semantic_signal_count+components.sem",
    "body_retrieval": "components.body|components.content",
    "cochange_history": "components.commit",
}


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _empty(blocker: str) -> dict[str, dict[str, Any]]:
    return {
        feature: {
            "status": "UNMEASURED",
            "source_artifact": None,
            "receipt_level": None,
            "blocker": blocker,
            "block_id": None,
            "content_sha256_16": None,
        }
        for feature in ACQ_FEATURES
    }


def _positive(value: object) -> bool:
    try:
        return float(value or 0) > 0
    except (TypeError, ValueError):
        return False


def _path_in_block(path: str, rendered: str) -> bool:
    normalized_path = path.replace("\\", "/")
    normalized_render = rendered.replace("\\", "/")
    path_char = r"A-Za-z0-9_./-"
    return re.search(
        rf"(?<![{path_char}]){re.escape(normalized_path)}(?![{path_char}])",
        normalized_render,
    ) is not None


def _source_features(proof: Mapping[str, Any], metrics: Mapping[str, Any]) -> tuple[str, ...]:
    """ACQ rows directly evidenced by one rendered candidate.

    Aggregate counters are gates, not evidence on their own.  Each admitted
    row also needs a candidate-local component (or verified graph witness).
    Rows whose v1 artifact lacks candidate-local authority stay quiet.
    """
    components = proof.get("components")
    components = components if isinstance(components, Mapping) else {}
    found: list[str] = []
    if (
        _positive(metrics.get("graph_edge_count"))
        and proof.get("witness_verified") is True
        and isinstance(proof.get("witness"), str)
        and bool(str(proof.get("witness")).strip())
    ):
        found.append("graph_validity")
    if (
        _positive(metrics.get("structural_signal_count"))
        and _positive(components.get("reach"))
    ):
        found.append("structural_depth")
    if (
        _positive(metrics.get("fts5_signal_count"))
        and _positive(components.get("lex"))
    ):
        found.append("lexical_FTS5")
    if (
        _positive(metrics.get("semantic_signal_count"))
        and _positive(components.get("sem"))
    ):
        found.append("semantic_embedder")
    if _positive(components.get("body")) or _positive(components.get("content")):
        found.append("body_retrieval")
    if _positive(components.get("commit")):
        found.append("cochange_history")
    return tuple(found)


def _validated_blocks(brief: str, raw: object) -> dict[str, dict[str, Any]]:
    if raw is None:
        return {}
    if not isinstance(raw, list):
        raise ValueError("acq provenance: block_receipts must be a list")
    blocks: dict[str, dict[str, Any]] = {}
    for index, receipt in enumerate(raw):
        if not isinstance(receipt, Mapping):
            raise ValueError(f"acq provenance: block receipt {index} is not an object")
        block_id = receipt.get("block_id")
        fact_class = receipt.get("fact_class")
        label = receipt.get("label")
        span = receipt.get("char_span")
        digest = receipt.get("content_hash")
        if not isinstance(block_id, str) or not block_id or block_id in blocks:
            raise ValueError("acq provenance: block ids must be non-empty and unique")
        if not isinstance(fact_class, str) or not isinstance(label, str):
            raise ValueError(f"acq provenance: block classification missing for {block_id}")
        if (
            not isinstance(span, list) or len(span) != 2
            or not all(isinstance(v, int) and not isinstance(v, bool) for v in span)
        ):
            raise ValueError(f"acq provenance: malformed span for {block_id}")
        start, end = span
        if start < 0 or end <= start or end > len(brief):
            raise ValueError(f"acq provenance: out-of-range span for {block_id}")
        rendered = brief[start:end]
        if not isinstance(digest, str) or digest != _sha256(rendered):
            raise ValueError(f"acq provenance: block content hash mismatch for {block_id}")
        blocks[block_id] = {
            "rendered_text": rendered,
            "chars": end - start,
            "sha256": digest,
            "fact_class": fact_class,
            "label": label,
        }
    return blocks


def _trajectory_messages(trajectory: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(trajectory, Mapping):
        return []
    messages = trajectory.get("messages")
    if not isinstance(messages, list):
        return []
    # Preserve original indexes because v2 ``msg_index`` addresses this exact
    # list.  A malformed item is an inert placeholder, never removed/shifted.
    return [message if isinstance(message, dict) else {} for message in messages]


def _producer_delivery_home(
    payloads: tuple[str, ...],
    entries: object,
    messages: list[dict[str, Any]],
) -> int | None:
    """Return the message holding an exact producer-sealed payload.

    An auditor-recomputed ``<gt-*>`` entry is not a delivery seal.  The v2
    entry must be joined by the seal path to an actual runtime-ledger producer
    row.  The producer may seal the whole brief or the exact source block.
    """
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if not isinstance(entry, Mapping) or entry.get("source") != "trajectory":
            continue
        if entry.get("joined") is not True or entry.get("join_method") != "seal":
            continue
        if not isinstance(entry.get("ledger_layer"), str) or not entry.get("ledger_layer"):
            continue
        for payload in payloads:
            if entry.get("rendered_text") != payload:
                continue
            if entry.get("content_sha256_16") != _sha256(payload)[:16]:
                continue
            try:
                entry_chars = int(entry.get("chars") or 0)
                ledger_chars = int(entry.get("ledger_chars") or 0)
            except (TypeError, ValueError):
                continue
            msg_index = entry.get("msg_index")
            if (
                entry_chars != len(payload) or ledger_chars != len(payload)
                or not isinstance(msg_index, int)
            ):
                continue
            if msg_index < 0 or msg_index >= len(messages):
                continue
            message = messages[msg_index]
            if message.get("role") not in ("user", "tool"):
                continue
            content = message.get("content")
            if isinstance(content, str) and payload in content:
                return msg_index
    return None


def _block_receipt_level(
    block: Mapping[str, Any],
    file_path: str,
    messages: list[dict[str, Any]],
    delivery_home: int | None,
) -> int | None:
    """Apply W1's receipt ladder to one exact persisted brief sub-block."""
    if delivery_home is None:
        return None
    rendered = str(block["rendered_text"])
    files, symbols = _consumption._block_entities(rendered, file_path)
    patterns = _consumption._entity_patterns(files, symbols)
    level = 1
    for index in range(delivery_home + 1, len(messages)):
        message = messages[index]
        if message.get("role") != "assistant":
            continue
        if patterns and _consumption._named_in(
            _consumption._assistant_prose(message), patterns
        ):
            level = max(level, 2)
        for command in _consumption._emitted_commands(message):
            if (
                patterns
                and _consumption._named_in(command, patterns)
                and _consumption._action_kind(command) is not None
            ):
                level = max(level, 3)
    return level


def collect_acq_provenance(
    brief_payload: Mapping[str, Any] | None,
    consumption_ledger: Mapping[str, Any],
    trajectory: Mapping[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    """Return the exact 12 ACQ rows, promoting only a complete receipt chain."""
    if brief_payload is None:
        return _empty("brief_result_absent")
    if not isinstance(brief_payload, Mapping):
        raise ValueError("acq provenance: brief payload must be an object")
    if brief_payload.get("schema") != _BRIEF_SCHEMA:
        raise ValueError("acq provenance: unsupported brief_result schema")

    brief = brief_payload.get("brief_text")
    if not isinstance(brief, str) or not brief:
        raise ValueError("acq provenance: claimed brief payload has no text")
    if brief_payload.get("brief_sha256") != _sha256(brief):
        raise ValueError("acq provenance: whole brief hash mismatch")
    metrics = brief_payload.get("metrics")
    if not isinstance(metrics, Mapping):
        raise ValueError("acq provenance: claimed brief payload has no metrics object")

    rows = _empty("source_witness_absent")
    blocks = _validated_blocks(brief, metrics.get("block_receipts"))
    proofs = metrics.get("localization_proof")
    if not blocks or not isinstance(proofs, list):
        return rows
    if consumption_ledger:
        if consumption_ledger.get("schema") != _CONSUMPTION_SCHEMA:
            raise ValueError("acq provenance: unsupported consumption ledger schema")
        entries = consumption_ledger.get("entries")
    else:
        entries = []
    messages = _trajectory_messages(trajectory)

    for proof_index, proof in enumerate(proofs):
        if not isinstance(proof, Mapping):
            raise ValueError(f"acq provenance: localization proof {proof_index} is not an object")
        rank = proof.get("rank")
        path = proof.get("path")
        if not isinstance(rank, int) or isinstance(rank, bool) or rank < 1:
            raise ValueError(f"acq provenance: invalid candidate rank at {proof_index}")
        if not isinstance(path, str) or not path.strip():
            continue
        block_id = f"file-entry-{rank}"
        block = blocks.get(block_id)
        if block is None:
            continue
        if block["fact_class"] != "localization" or block["label"] != block_id:
            continue
        if not _path_in_block(path, str(block["rendered_text"])):
            continue
        features = _source_features(proof, metrics)
        if not features:
            continue
        delivery_home = _producer_delivery_home(
            (str(block["rendered_text"]), brief), entries, messages
        )
        level = _block_receipt_level(block, path, messages, delivery_home)
        for feature in features:
            candidate = {
                "status": "MEASURED" if level is not None and level >= 2 else "UNMEASURED",
                "source_artifact": f"brief_result.json#metrics.localization_proof[{proof_index}]",
                "receipt_level": level,
                "blocker": (
                    None if level is not None and level >= 2
                    else "assistant_receipt_below_2" if level == 1
                    else "producer_seal_absent"
                ),
                "block_id": block_id,
                "content_sha256_16": str(block["sha256"])[:16],
            }
            current = rows[feature]
            current_level = current.get("receipt_level")
            if (
                candidate["status"] == "MEASURED"
                or current_level is None
                or (level is not None and level > current_level)
            ):
                rows[feature] = candidate
    return rows
