#!/usr/bin/env python3
"""Fail-closed ACQ source-to-receipt provenance joins.

This grader adds no delivery channel.  It binds source witnesses already
persisted in ``brief_result.json`` to the exact rendered brief block and then
to W1's trajectory-derived consumption receipt.  A source's mere existence,
a matching basename, or a silent delivery can never promote an ACQ row.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

try:  # package import (tests / ``python -m``)
    from scripts.swebench import consumption_ledger as _consumption
    from scripts.swebench.gt_feature_inventory import ACQ_FEATURES
    from groundtruth.pretask.curation_map import DETERMINISTIC_RESOLUTION_METHODS
except ModuleNotFoundError:  # direct script-dir import used by gt_feature_metrics CLI
    import consumption_ledger as _consumption  # type: ignore[no-redef]
    from gt_feature_inventory import ACQ_FEATURES  # type: ignore[no-redef]
    from groundtruth.pretask.curation_map import DETERMINISTIC_RESOLUTION_METHODS


_BRIEF_SCHEMA = "gt.brief_result.v1"
_CONSUMPTION_SCHEMA = "gt.consumption_ledger.v2"

# Single authority for candidate-local ACQ source witnesses. The proof manifest
# imports this mapping so its authority contract cannot drift from the collector.
ACQ_SOURCE_COMPONENTS: dict[str, str] = {
    "graph_validity": "graph_edge_count+witness_verified",
    "structural_depth": "structural_signal_count+components.reach",
    "lexical_FTS5": "fts5_signal_count+components.lex",
    "semantic_embedder": "semantic_signal_count+components.sem",
    "body_retrieval": "components.body|components.content",
    "cochange_history": "components.cochange+cochange_evidence",
    "resolution_honesty": "acquisition_sources.resolution_honesty",
    "type_intelligence": "acquisition_sources.type_intelligence",
    "LSP": "acquisition_sources.LSP",
    "freshness_basis": "acquisition_sources.freshness_basis",
    "repo_scope": "acquisition_sources.repo_scope",
    "determinism": "acquisition_sources.determinism",
}

# Executable source of each persisted candidate-local witness.  This is distinct
# from this module's collector authority and from brief_cache's terminal artifact
# persistence.  Keep the keys exact with ACQ_SOURCE_COMPONENTS.
ACQ_PRODUCER_AUTHORITIES: dict[str, tuple[str, ...]] = {
    "graph_validity": (
        "groundtruth.pretask.graph_localizer.localize",
        "groundtruth.pretask.v1r_brief._l1_signal_counts",
    ),
    "structural_depth": (
        "groundtruth.pretask.v7_4_brief._score_variant_C",
        "groundtruth.pretask.v1r_brief._l1_signal_counts",
    ),
    "lexical_FTS5": (
        "groundtruth.pretask.hybrid.lexical_file_search",
        "groundtruth.pretask.v7_4_brief._score_variant_C",
        "groundtruth.pretask.v1r_brief._l1_signal_counts",
    ),
    "semantic_embedder": (
        "groundtruth.pretask.anchor_select.select_anchors",
        "groundtruth.pretask.v7_4_brief._score_variant_C",
        "groundtruth.pretask.v1r_brief._l1_signal_counts",
    ),
    "body_retrieval": (
        "groundtruth.pretask.graph_localizer._content_fts_candidates",
        "groundtruth.pretask.graph_localizer.localize",
        "groundtruth.pretask.anchor_select.select_anchors",
        "groundtruth.pretask.v7_4_brief.run_v74",
        "groundtruth.pretask.v1r_brief.generate_v1r_brief",
    ),
    "cochange_history": (
        "groundtruth.pretask.v1r_brief._expand_via_cochange",
    ),
    **{
        name: ("groundtruth.pretask.v1r_brief._candidate_acquisition_sources",)
        for name in (
            "resolution_honesty", "type_intelligence", "LSP",
            "freshness_basis", "repo_scope",
        )
    },
    "determinism": (
        "groundtruth.runtime.brief_cache.verify_independent_generation",
    ),
}
if set(ACQ_PRODUCER_AUTHORITIES) != set(ACQ_SOURCE_COMPONENTS):
    raise ValueError("ACQ producer-authority inventory drift")

_TYPE_RESOLUTION_METHODS = frozenset({"type_flow", "import_type"})
_LSP_RESOLUTION_METHODS = frozenset({"lsp", "lsp_verified"})


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
            "candidate_id": None,
            "candidate_path": None,
            "supported_fact_class": None,
            "content_sha256_16": None,
            "chars_delivered": None,
            "block_content_sha256_16": None,
            "block_char_span": None,
            "producer_payload_scope": None,
            "producer_entry_index": None,
            "producer_ledger_layer": None,
            "delivery_message_index": None,
            "receipt_evidence": {
                "referenced_message_index": None,
                "acted_message_index": None,
            },
            "source_fields": [],
            "source_contribution_correct": None,
            "timing_inherited_from_fact_delivery": None,
            "source_causal_fair_probe": None,
        }
        for feature in ACQ_FEATURES
    }


def _positive(value: object) -> bool:
    try:
        return float(value or 0) > 0
    except (TypeError, ValueError):
        return False


def _valid_cochange_evidence(
    proof: Mapping[str, Any], candidate_path: str,
) -> bool:
    """Validate producer-owned cochange rows; aggregate scores are insufficient."""
    evidence = proof.get("cochange_evidence")
    components = proof.get("components")
    components = components if isinstance(components, Mapping) else {}
    if not isinstance(evidence, Mapping):
        return False
    count = evidence.get("count")
    rows = evidence.get("source_rows")
    if (
        evidence.get("kind") != "cochange_history"
        or evidence.get("source") != "git_log"
        or evidence.get("candidate_path") != candidate_path
        or not isinstance(evidence.get("source_revision"), str)
        or re.fullmatch(r"[0-9a-f]{40}", str(evidence["source_revision"])) is None
        or not isinstance(evidence.get("history_limit"), int)
        or isinstance(evidence.get("history_limit"), bool)
        or evidence.get("history_limit") != 100
        or not isinstance(count, int) or isinstance(count, bool) or count < 2
        or not isinstance(rows, list) or len(rows) != count
        or not _positive(components.get("cochange"))
        or float(components.get("cochange", 0.0)) != float(count)
    ):
        return False
    commits: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            return False
        commit = row.get("commit")
        symptoms = row.get("symptom_paths")
        if (
            not isinstance(commit, str)
            or re.fullmatch(r"[0-9a-f]{40}", commit) is None
            or commit in commits
            or not isinstance(symptoms, list) or not symptoms
            or any(not isinstance(path, str) or not path for path in symptoms)
            or symptoms != sorted(set(symptoms))
            or candidate_path in symptoms
        ):
            return False
        commits.add(commit)
    unsigned = dict(evidence)
    identity = unsigned.pop("source_identity_sha256", None)
    canonical = json.dumps(
        unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return isinstance(identity, str) and identity == expected


def _path_in_block(path: str, rendered: str) -> bool:
    normalized_path = path.replace("\\", "/")
    normalized_render = rendered.replace("\\", "/")
    path_char = r"A-Za-z0-9_./-"
    return re.search(
        rf"(?<![{path_char}]){re.escape(normalized_path)}(?![{path_char}])",
        normalized_render,
    ) is not None


def _sha256_value(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if len(normalized) != 64 or re.fullmatch(r"[0-9a-f]{64}", normalized) is None:
        return None
    return normalized


def _method_set(value: object) -> frozenset[str]:
    if not isinstance(value, list) or not value:
        return frozenset()
    methods: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not item.strip():
            return frozenset()
        methods.add(item.strip().lower())
    return frozenset(methods)


def _valid_repeat_witness(value: object) -> bool:
    if not isinstance(value, Mapping) or value.get("kind") != "repeat_identity":
        return False
    identities = value.get("canonical_sha256")
    runs = value.get("runs")
    if (
        not isinstance(runs, int) or isinstance(runs, bool) or runs < 2
        or not isinstance(identities, list) or len(identities) != runs
    ):
        return False
    digests = [_sha256_value(item) for item in identities]
    return all(digest is not None for digest in digests) and len(set(digests)) == 1


def _extended_source_features(proof: Mapping[str, Any]) -> tuple[str, ...]:
    """Validate typed candidate-local source witnesses, correct-or-quiet.

    Each witness proves a different acquisition property.  The shapes are
    deliberately non-interchangeable: an LSP availability flag is not an LSP
    contribution, a file hash is not repeatability, and a resolved scope without
    candidate/active partition identity is not repo scoping.
    """
    raw = proof.get("acquisition_sources")
    if not isinstance(raw, Mapping):
        return ()
    found: list[str] = []

    resolution = raw.get("resolution_honesty")
    if isinstance(resolution, Mapping) and resolution.get("kind") == "resolution_methods":
        methods = _method_set(resolution.get("methods"))
        deterministic = {str(m).strip().lower() for m in DETERMINISTIC_RESOLUTION_METHODS}
        if resolution.get("all_verified") is True and methods and methods <= deterministic:
            found.append("resolution_honesty")

    type_source = raw.get("type_intelligence")
    if isinstance(type_source, Mapping) and type_source.get("kind") == "type_resolution":
        methods = _method_set(type_source.get("methods"))
        if methods and methods <= _TYPE_RESOLUTION_METHODS:
            found.append("type_intelligence")

    lsp_source = raw.get("LSP")
    if isinstance(lsp_source, Mapping) and lsp_source.get("kind") == "lsp_resolution":
        methods = _method_set(lsp_source.get("methods"))
        if methods and methods <= _LSP_RESOLUTION_METHODS:
            found.append("LSP")

    freshness = raw.get("freshness_basis")
    if isinstance(freshness, Mapping) and freshness.get("kind") == "content_revision":
        indexed = _sha256_value(freshness.get("indexed_sha256"))
        observed = _sha256_value(freshness.get("observed_sha256"))
        if indexed is not None and indexed == observed:
            found.append("freshness_basis")

    scope = raw.get("repo_scope")
    if isinstance(scope, Mapping) and scope.get("kind") == "repo_partition":
        active = scope.get("active_repo_id")
        candidate = scope.get("candidate_repo_id")
        multi_repo_proven = (
            scope.get("is_multi_repo") is True
            and scope.get("resolved") is True
            and isinstance(active, int) and not isinstance(active, bool)
            and isinstance(candidate, int) and not isinstance(candidate, bool)
            and candidate == active
        )
        proof_path = str(proof.get("path") or "").replace("\\", "/").lstrip("./").lstrip("/")
        candidate_path = str(scope.get("candidate_path") or "").replace("\\", "/").lstrip("./").lstrip("/")
        single_repo_proven = (
            scope.get("is_multi_repo") is False
            and scope.get("resolved") is True
            and scope.get("scope_mode") == "single_repo_noop"
            and active is None
            and candidate is None
            and bool(proof_path)
            and candidate_path == proof_path
        )
        if multi_repo_proven or single_repo_proven:
            found.append("repo_scope")

    if _valid_repeat_witness(raw.get("determinism")):
        found.append("determinism")
    return tuple(found)


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
    path = proof.get("path")
    if (
        _positive(components.get("cochange"))
        and isinstance(path, str)
        and _valid_cochange_evidence(proof, path)
    ):
        found.append("cochange_history")
    found.extend(_extended_source_features(proof))
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
        candidate_id = receipt.get("candidate_id")
        span = receipt.get("char_span")
        digest = receipt.get("content_hash")
        if not isinstance(block_id, str) or not block_id or block_id in blocks:
            raise ValueError("acq provenance: block ids must be non-empty and unique")
        if not isinstance(fact_class, str) or not isinstance(label, str):
            raise ValueError(f"acq provenance: block classification missing for {block_id}")
        if candidate_id is not None and (
            not isinstance(candidate_id, str) or not candidate_id
        ):
            raise ValueError(f"acq provenance: malformed candidate id for {block_id}")
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
            "block_id": block_id,
            "rendered_text": rendered,
            "chars": end - start,
            "char_span": [start, end],
            "sha256": digest,
            "fact_class": fact_class,
            "label": label,
            "candidate_id": candidate_id,
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
) -> dict[str, Any] | None:
    """Return the exact producer seal and observation home for a payload.

    An auditor-recomputed ``<gt-*>`` entry is not a delivery seal.  The v2
    entry must be joined by the seal path to an actual runtime-ledger producer
    row.  The producer may seal the whole brief or the exact source block.
    """
    if not isinstance(entries, list):
        return None
    for entry_index, entry in enumerate(entries):
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
            entry_chars = entry.get("chars")
            ledger_chars = entry.get("ledger_chars")
            if (
                not isinstance(entry_chars, int) or isinstance(entry_chars, bool)
                or not isinstance(ledger_chars, int) or isinstance(ledger_chars, bool)
            ):
                continue
            msg_index = entry.get("msg_index")
            if (
                entry_chars != len(payload) or ledger_chars != len(payload)
                or not isinstance(msg_index, int) or isinstance(msg_index, bool)
            ):
                continue
            if msg_index < 0 or msg_index >= len(messages):
                continue
            message = messages[msg_index]
            if message.get("role") not in ("user", "tool"):
                continue
            content = message.get("content")
            if isinstance(content, str) and payload in content:
                return {
                    "entry_index": entry_index,
                    "msg_index": msg_index,
                    "payload": payload,
                    "content_sha256_16": str(entry["content_sha256_16"]),
                    "chars_delivered": entry_chars,
                    "ledger_layer": str(entry["ledger_layer"]),
                }
    return None


def _block_receipt(
    block: Mapping[str, Any],
    file_path: str,
    messages: list[dict[str, Any]],
    delivery_home: int | None,
) -> dict[str, int | None]:
    """Apply W1's receipt ladder and retain exact assistant evidence homes."""
    if delivery_home is None:
        return {
            "level": None,
            "referenced_message_index": None,
            "acted_message_index": None,
        }
    rendered = str(block["rendered_text"])
    files, symbols = _consumption._block_entities(rendered, file_path)
    patterns = _consumption._entity_patterns(files, symbols)
    level = 1
    referenced_message_index: int | None = None
    acted_message_index: int | None = None
    for index in range(delivery_home + 1, len(messages)):
        message = messages[index]
        if message.get("role") != "assistant":
            continue
        if patterns and _consumption._named_in(
            _consumption._assistant_prose(message), patterns
        ):
            level = max(level, 2)
            if referenced_message_index is None:
                referenced_message_index = index
        for command in _consumption._emitted_commands(message):
            if (
                patterns
                and _consumption._named_in(command, patterns)
                and _consumption._action_kind(command) is not None
            ):
                level = max(level, 3)
                if acted_message_index is None:
                    acted_message_index = index
    return {
        "level": level,
        "referenced_message_index": referenced_message_index,
        "acted_message_index": acted_message_index,
    }


def _source_field_paths(
    feature: str,
    proof_index: int,
    proof: Mapping[str, Any],
) -> list[str]:
    """Return the exact persisted fields that support one ACQ row.

    These are data lineage pointers, not truth/timing/causality verdicts.  A
    compound source names every field required by its admission predicate.
    """
    base = f"metrics.localization_proof[{proof_index}]"
    if feature == "graph_validity":
        return [
            "metrics.graph_edge_count",
            f"{base}.witness_verified",
            f"{base}.witness",
        ]
    if feature == "structural_depth":
        return ["metrics.structural_signal_count", f"{base}.components.reach"]
    if feature == "lexical_FTS5":
        return ["metrics.fts5_signal_count", f"{base}.components.lex"]
    if feature == "semantic_embedder":
        return ["metrics.semantic_signal_count", f"{base}.components.sem"]
    components = proof.get("components")
    components = components if isinstance(components, Mapping) else {}
    if feature == "body_retrieval":
        return [
            f"{base}.components.{name}"
            for name in ("body", "content")
            if _positive(components.get(name))
        ]
    if feature == "cochange_history":
        return [
            f"{base}.components.cochange",
            f"{base}.cochange_evidence",
        ]
    return [f"{base}.acquisition_sources.{feature}"]


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
    if not isinstance(proofs, list):
        return rows
    if not proofs and _valid_repeat_witness(metrics.get("determinism_witness")):
        # Repeatability exists at acquisition-run scope, but without a rendered
        # candidate there is no block, producer delivery, or assistant receipt to
        # which this ACQ row can attach. Record the source and remain UNMEASURED.
        rows["determinism"].update({
            "source_artifact": "brief_result.json",
            "blocker": "candidate_delivery_absent",
            "source_fields": ["metrics.determinism_witness"],
        })
        return rows
    if not blocks:
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
        if not (
            block is not None
            and block["fact_class"] == "localization"
            and block["label"] == block_id
            and _path_in_block(path, str(block["rendered_text"]))
        ):
            # Minimal MEDIUM/LOW intentionally has no duplicate file-entry blocks:
            # every visible candidate is sealed to the shared contention header.
            # Join only through the producer-issued candidate identity plus exact
            # path bytes; never infer a candidate from rank or path existence alone.
            proof_candidate_id = proof.get("candidate_id")
            matches = [
                candidate_block for candidate_block in blocks.values()
                if candidate_block["fact_class"] == "localization"
                and candidate_block["label"] == "localization-header"
                and isinstance(proof_candidate_id, str)
                and candidate_block.get("candidate_id") == proof_candidate_id
                and _path_in_block(path, str(candidate_block["rendered_text"]))
            ]
            if len(matches) != 1:
                continue
            block = matches[0]
            block_id = str(block["block_id"])
        features = _source_features(proof, metrics)
        if not features:
            continue
        delivery = _producer_delivery_home(
            (str(block["rendered_text"]), brief), entries, messages
        )
        delivery_home = delivery.get("msg_index") if delivery is not None else None
        receipt = _block_receipt(block, path, messages, delivery_home)
        level = receipt["level"]
        for feature in features:
            source_fields = _source_field_paths(feature, proof_index, proof)
            producer_payload = str(delivery.get("payload", "")) if delivery else ""
            candidate = {
                "status": "MEASURED" if level is not None and level >= 2 else "UNMEASURED",
                "source_artifact": "brief_result.json" if source_fields else None,
                "receipt_level": level,
                "blocker": (
                    None if level is not None and level >= 2
                    else "assistant_receipt_below_2" if level == 1
                    else "producer_seal_absent"
                ),
                "block_id": block_id,
                "candidate_id": block.get("candidate_id"),
                "candidate_path": path,
                "supported_fact_class": block.get("fact_class"),
                # Gate-1 identity belongs to the actual producer-sealed payload.
                # The exact candidate sub-block remains separately addressable.
                "content_sha256_16": (
                    delivery.get("content_sha256_16") if delivery else None
                ),
                "chars_delivered": delivery.get("chars_delivered") if delivery else None,
                "block_content_sha256_16": str(block["sha256"])[:16],
                "block_char_span": list(block["char_span"]),
                "producer_payload_scope": (
                    "exact_block" if producer_payload == str(block["rendered_text"])
                    else "whole_brief" if producer_payload == brief
                    else None
                ),
                "producer_entry_index": delivery.get("entry_index") if delivery else None,
                "producer_ledger_layer": delivery.get("ledger_layer") if delivery else None,
                "delivery_message_index": delivery_home,
                "receipt_evidence": {
                    "referenced_message_index": receipt["referenced_message_index"],
                    "acted_message_index": receipt["acted_message_index"],
                },
                "source_fields": source_fields,
                "source_contribution_correct": (
                    True if source_fields else None
                ),
                "timing_inherited_from_fact_delivery": (
                    True if delivery_home is not None
                    and isinstance(delivery_home, int)
                    and delivery_home <= 1
                    else None
                ),
                "source_causal_fair_probe": None,
            }
            current = rows[feature]
            current_level = current.get("receipt_level")
            # Retain the strongest receipt. Equal-strength candidates keep the
            # first proof-index/rank encountered, giving a stable tie-break.
            if current.get("block_id") is None or (
                level is not None
                and (current_level is None or level > current_level)
            ):
                rows[feature] = candidate
    return rows
