#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
measure_layers.py -- GroundTruth per-layer measurement harness (CORRECT metrics).

WHY THIS EXISTS
===============
This session repeatedly drew FALSE conclusions because the per-layer metrics were
computed with wrong denominators, wrong channels, or telemetry instead of agent
observation. Each false conclusion is a measurement trap. This harness computes the
CORRECT metric for each layer and actively GUARDS against the five traps below. It is
generalized: every threshold / denominator is data-derived from the substrate or the
trajectory in front of it -- no task ids, repo names, gold labels, or per-language
hacks. It works on any graph.db built by gt-index and any mini-swe-agent /
output.jsonl trajectory.

THE FIVE MEASUREMENT TRAPS THIS HARNESS GUARDS AGAINST
------------------------------------------------------
TRAP 1 -- DEPTH wrong-denominator (the "data_flow -> DATA_FLOW = 0%" lie).
  The false metric was (standalone DATA_FLOW edges) / (ALL data_flow properties).
  Wrong twice:
    (a) The facts materialize ~94% as CALLS-edge metadata ANNOTATIONS (`dataflow=<callee>`,
        `usage=<kind>`), NOT as standalone DATA_FLOW edges. Both channels must be counted.
        (Measured go substrate: 2752 CALLS carry `dataflow=` vs only 20 standalone
        DATA_FLOW edges -> the false metric reads 20/5923 = 0.34% ~ "0%".)
    (b) The denominator must be facts whose target resolves to an INTERNAL Function/
        Method/Class node. A `data_flow` property is a provenance string
        `var -> usage1 | usage2 | ...`; most usages are external library calls
        (`strings.TrimSpace`, `document.getElementById`), field reads (`c.inner`),
        or comparisons (`item_id == "foo"`) -- those CANNOT become internal edges and
        must NOT inflate the denominator.
  CORRECT metric: materialized% = (standalone promote-edges of the relation
        + CALLS edges whose metadata carries the matching tag)
        / (facts whose parsed call-target resolves to an internal node).

TRAP 2 -- NAMING under-reported / laundering not asserted.
  Report on CALLS edges: name_match%, det% (=1 - name_match/CALLS), trust_tier
  distribution, and a 0-LAUNDERED assertion (count of name_match edges stamped
  trust_tier='CERTIFIED' MUST be 0). Also report residual composition of the
  name_match: vendored/minified-path, candidate_count==1 (promotable), method-target
  (needs receiver type), external/stdlib (correctly dropped).

TRAP 3 -- LSP false-green (the Method-only label filter that reads 0 on JS/Go).
  JS and Go label methods as 'Function', not 'Method'. A residual counter that filters
  target.label='Method' undercounts massively (measured: js Method-only=45 vs
  language-correct=301 -> 6.7x undercount; go 1433 vs 2137) and stamps a false NO_OP.
  CORRECT: count the name_match method-residual with target.label IN ('Method','Function').

TRAP 4 -- CONSUMPTION via telemetry instead of agent observation (AGENT-OBSERVATION rule).
  "delivered" != "fired". A layer WORKS only if its output reaches the agent's RAW
  observation text. We read the trajectory CHRONOLOGICALLY (never grep-count telemetry),
  detect each layer's agent-delivered marker in the observation stream, and check whether
  a later agent action references that content (consumed). Event hooks (L3/L3b/L4/L5/L5b)
  that are ABSENT mean the triggering event did not occur -- that is NOT "broken".

TRAP 5 -- BRIEF quality not measured.
  Report token count (product's own estimator: len//4 + 1) vs MAX_BRIEF_TOKENS budget
  (600), signature bloat (count of un-stripped docstring-prose markers -- `Doc(` opens and
  triple-quote runs -- inside rendered signatures), and the char-position of
  `<gt-graph-map>` in the brief (is the unique structural value buried behind a multi-KB
  signature dump?).

GROUNDING (all empirically verified against the 4 on-disk substrates, 2026-06-14):
  - graph.db schema: edges(type, resolution_method, confidence, metadata, trust_tier,
    candidate_count, evidence_type, verification_status); nodes(label, name, ...);
    properties(kind, value, ...).  [verified: all 4 substrates identical]
  - promote relations -> edge type / resolution_method / annotation channel:
      data_flow  -> DATA_FLOW   / promote_dataflow_callee + CALLS `dataflow=` annot
      field_read -> READS       / promote_field_read
      call_order -> PRECEDES    / promote_precedes
      writes     -> WRITES      / promote_write
      raises     -> RAISES      / promote_raises
      serde      -> CO_SERIALIZES/ promote_serde
      caller_usage -> CALLS `usage=` annot
  - brief markers (agent-delivered, fastapi trajectory): <gt-localization>,
    <gt-task-brief>, <gt-graph-map>, <gt-evidence>, <gt-scope>.
  - token estimator: len(text)//4 + 1  (src/groundtruth/pretask/v1r_brief.py:1028)
  - MAX_BRIEF_TOKENS = 600 (v1r_brief.py:51; post_view.py L3b budget = 600)

USAGE
-----
  PYTHONUTF8=1 python scripts/verify/measure_layers.py graph   <graph.db> [--json out.json]
  PYTHONUTF8=1 python scripts/verify/measure_layers.py brief    <brief.txt|trajectory.json> [--json out.json]
  PYTHONUTF8=1 python scripts/verify/measure_layers.py delivery <trajectory.json|output.jsonl> [--json out.json]
  PYTHONUTF8=1 python scripts/verify/measure_layers.py all      --graph <db> [--trajectory <t>] [--json out.json]

All numeric values are emitted at 8 decimal places (CLAUDE.md deep-metrics contract).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from typing import Any, Dict, List, Optional

# ----------------------------------------------------------------------------
# Constants grounded in product source (cited above). NOT task/repo specific.
# ----------------------------------------------------------------------------
MAX_BRIEF_TOKENS = 600  # v1r_brief.py:51
INTERNAL_LABELS = ("Function", "Method", "Class")
METHOD_LABELS = ("Method", "Function")  # TRAP 3: JS/Go label methods 'Function'

# Generalized promote-relation table (derived from resolution_method='promote_*' rows
# and metadata annotation tags observed across all 4 substrates). Each entry declares:
#   property_kind  : the properties.kind that feeds this relation (None = no source kind;
#                    denominator falls back to the materialized count)
#   edge_type      : the standalone edge type the promote pass emits
#   resolution_method : the promote_* method stamping those edges
#   annotation_tag : metadata key written onto a CALLS edge when the fact also rides a
#                    real call (the ~94% channel) -- None if the relation has no CALLS annot
#   denom          : how to compute the RESOLVABLE denominator (TRAP 1b):
#                    'call'  -> count internal-resolvable call-targets in the prop string
#                    'field' -> count prop rows whose receiver resolves internal
#                    'rows'  -> every prop row is a materializable fact (no external parse
#                               possible from the descriptor) -- denominator = row count
# Keyed by a stable relation name (not the property kind, since several share an edge type).
PROMOTE_RELATIONS: Dict[str, Dict[str, Any]] = {
    "data_flow": {"property_kind": "data_flow", "edge_type": "DATA_FLOW",
                  "resolution_method": "promote_dataflow_callee", "annotation_tag": "dataflow", "denom": "call"},
    "field_read": {"property_kind": "field_read", "edge_type": "READS",
                   "resolution_method": "promote_field_read", "annotation_tag": None, "denom": "rows"},
    "call_order": {"property_kind": "call_order", "edge_type": "PRECEDES",
                   "resolution_method": "promote_precedes", "annotation_tag": None, "denom": "rows"},
    "writes": {"property_kind": None, "edge_type": "WRITES",
               "resolution_method": "promote_write", "annotation_tag": None, "denom": "rows"},
    "raises": {"property_kind": "exception_type", "edge_type": "RAISES",
               "resolution_method": "promote_raises", "annotation_tag": None, "denom": "rows"},
    "serialization_pair": {"property_kind": "serialization_pair", "edge_type": "CO_SERIALIZES",
                           "resolution_method": "promote_serde", "annotation_tag": None, "denom": "rows"},
}
# caller_usage rides only as a CALLS annotation (no standalone edge type).
ANNOTATION_ONLY_RELATIONS: Dict[str, str] = {"caller_usage": "usage"}

# Resolution methods that are DETERMINISTIC facts (not name guesses).
DETERMINISTIC_METHODS = {
    "same_file", "import", "type_flow", "impl_method", "inherited", "inheritance",
    "verified_unique", "unique_method", "structural", "structural_method_set_arity",
    "lsp", "re_export", "jsx_component", "decorator_route", "route_match",
}

# Vendored / minified path markers (generalized; no repo names).
VENDOR_RE = re.compile(
    r"(?:^|/)(?:node_modules|vendor|third[_-]?party|dist|build|\.min\.|"
    r"site-packages|\.venv|venv|target/release|target/debug|external|deps)(?:/|\.|$)",
    re.IGNORECASE,
)

# Identifier + call-target regexes for parsing provenance strings.
_CALL_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(")

# Brief delivery markers (agent-delivered). Each maps a layer to the tag(s) that, if
# present in the agent's observation stream, prove that layer reached the agent.
LAYER_MARKERS: Dict[str, Dict[str, Any]] = {
    "L1_brief": {
        "role": "fire-once pre-iter-1 localization brief (candidate files + scope + graph map)",
        "delivered_markers": ["<gt-task-brief", "<gt-localization", "<gt-brief"],
        "event": False,
    },
    "L3_post_edit": {
        "role": "post-edit contract/caller evidence (fires after an agent edit)",
        "delivered_markers": ["<gt-evidence", "[GT_OK]", "GT_VERIFY"],
        "event": True,
    },
    "L3b_post_view": {
        "role": "post-view ego-graph navigation context (fires after a file view)",
        "delivered_markers": ["<gt-graph-map", "<gt-context"],
        "event": True,
    },
    "L4_hook": {
        "role": "per-turn steer/context lane (event hook; absence = no eligible turn)",
        "delivered_markers": ["<gt-scope", "<gt-steer", "<gt-curation"],
        "event": True,
    },
    "L5_verify": {
        "role": "verification-horizon finalize advisory (event hook on edit)",
        "delivered_markers": ["finalize", "verified dependent", "no test has exercised"],
        "event": True,
    },
    "L5b_edit_risk": {
        "role": "structural edit-risk blast-radius advisory (event hook; default-off)",
        "delivered_markers": ["verified dependent(s) in the graph", "blast"],
        "event": True,
    },
    "L6_consensus": {
        "role": "end-of-run rerank / consensus (fire-once; mostly telemetry)",
        "delivered_markers": ["<gt-consensus", "consensus"],
        "event": False,
    },
}


def f8(x: float) -> float:
    """Round to 8 dp for the deep-metrics record. Never used to gate; display only."""
    return float(f"{float(x):.8f}")


def _pct(num: float, den: float) -> float:
    return f8(100.0 * num / den) if den else f8(0.0)


# ============================================================================
# GRAPH MEASUREMENT
# ============================================================================
def _internal_node_names(cur: sqlite3.Cursor) -> set:
    return set(
        r[0]
        for r in cur.execute(
            "SELECT name FROM nodes WHERE label IN (%s)"
            % ",".join("?" * len(INTERNAL_LABELS)),
            INTERNAL_LABELS,
        )
    )


def _resolvable_call_targets(value: str, names: set) -> int:
    """TRAP 1(b): count call-target occurrences in a provenance string whose callee
    resolves to an INTERNAL node. These are the only facts that *could* become an
    internal edge -- the correct denominator unit. Used for CALL-provenance relations
    (data_flow) where the property string embeds `callee(...)` patterns."""
    if not value:
        return 0
    n = 0
    for m in _CALL_RE.finditer(value):
        if m.group(1) in names:
            n += 1
    return n


def _resolvable_receiver(value: str, names: set) -> bool:
    """TRAP 1(b) for FIELD/MEMBER relations (field_read 'reads: recv.field', writes).
    The fact materializes into an internal edge only when the RECEIVER type resolves to
    an internal node. We approximate the receiver as the identifier before the first '.'
    in the descriptor and test it against internal node names. Conservative: a fact whose
    receiver cannot be tied to an internal node is NOT counted as resolvable (it points
    at an external/stdlib field -> cannot be an internal edge)."""
    if not value:
        return False
    body = value
    low = value.lower()
    if low.startswith("reads:") or low.startswith("writes:"):
        body = value.split(":", 1)[1]
    m = re.search(r"[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_]", body)
    if m:
        recv = m.group(0).split(".", 1)[0]
        return recv in names
    # bare identifier descriptor: resolvable iff it is an internal name
    m2 = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)", body)
    return bool(m2 and m2.group(1) in names)


def measure_graph(db_path: str) -> Dict[str, Any]:
    con = sqlite3.connect(db_path)
    cur = con.cursor()

    # --- substrate facts ---
    language = None
    try:
        langs = cur.execute(
            "SELECT language, COUNT(*) FROM nodes WHERE language IS NOT NULL "
            "GROUP BY language ORDER BY 2 DESC LIMIT 1"
        ).fetchone()
        language = langs[0] if langs else None
    except sqlite3.Error:
        language = None

    node_total = cur.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    label_mix = {
        str(r[0]): r[1]
        for r in cur.execute("SELECT label, COUNT(*) FROM nodes GROUP BY label ORDER BY 2 DESC")
    }
    edge_type_mix = {
        str(r[0]): r[1]
        for r in cur.execute("SELECT type, COUNT(*) FROM edges GROUP BY type ORDER BY 2 DESC")
    }
    names = _internal_node_names(cur)

    # --- DEPTH: per-relation materialization (TRAP 1) ---
    has_props = bool(
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='properties'").fetchone()
    )
    depth: Dict[str, Any] = {}
    if has_props:
        for relname, spec in PROMOTE_RELATIONS.items():
            etype = spec["edge_type"]
            rmethod = spec["resolution_method"]
            tag = spec["annotation_tag"]
            pkind = spec["property_kind"]
            denom_mode = spec["denom"]
            # numerator channel 1: standalone promote-edges of this relation
            standalone = cur.execute(
                "SELECT COUNT(*) FROM edges WHERE type=? AND resolution_method=?",
                (etype, rmethod),
            ).fetchone()[0]
            # numerator channel 2: CALLS-edge metadata annotations (the ~94% channel)
            annotated = 0
            if tag:
                annotated = cur.execute(
                    "SELECT COUNT(*) FROM edges WHERE type='CALLS' AND metadata LIKE ?",
                    (f"%{tag}=%",),
                ).fetchone()[0]
            materialized = standalone + annotated
            # denominator (TRAP 1b): facts whose target resolves to an INTERNAL node.
            resolvable = 0
            prop_rows = 0
            if pkind:
                vals = [r[0] for r in cur.execute("SELECT value FROM properties WHERE kind=?", (pkind,))]
                prop_rows = len(vals)
                if denom_mode == "call":
                    for v in vals:
                        resolvable += _resolvable_call_targets(v, names)
                elif denom_mode == "field":
                    resolvable = sum(1 for v in vals if _resolvable_receiver(v, names))
                else:  # 'rows' -- descriptor carries no external/internal call target to parse
                    resolvable = prop_rows
            else:
                # no source property kind -> the materialized edges are the only fact record
                resolvable = materialized
            depth[relname] = {
                "property_kind": pkind,
                "edge_type": etype,
                "resolution_method": rmethod,
                "annotation_tag": tag,
                "denominator_mode": denom_mode,
                "property_rows_total": prop_rows,
                "standalone_edges": standalone,
                "calls_annotations": annotated,
                "materialized": materialized,
                "resolvable_denominator": resolvable,
                "materialized_pct_of_resolvable": _pct(materialized, resolvable),
                # TRAP-1 EXPOSURE: the false metric was standalone-edges / all-property-rows.
                # It is only a *trap* for relations with an annotation channel (data_flow),
                # where standalone edges carry <<6% of the facts (the rest ride CALLS annots).
                # For annotation-less relations standalone/rows == the correct yield, so the
                # 'wrong' delta is N/A there (reported as None to avoid a misleading number).
                "WRONG_standalone_over_all_props_pct": (
                    _pct(standalone, prop_rows) if (tag and prop_rows) else None
                ),
            }
        # annotation-only relations (caller_usage -> usage=)
        for kind, tag in ANNOTATION_ONLY_RELATIONS.items():
            annotated = cur.execute(
                "SELECT COUNT(*) FROM edges WHERE type='CALLS' AND metadata LIKE ?",
                (f"%{tag}=%",),
            ).fetchone()[0]
            prop_rows = cur.execute("SELECT COUNT(*) FROM properties WHERE kind=?", (kind,)).fetchone()[0]
            depth[kind] = {
                "edge_type": "CALLS(annotation)",
                "resolution_method": None,
                "annotation_tag": tag,
                "property_rows_total": prop_rows,
                "standalone_edges": 0,
                "calls_annotations": annotated,
                "materialized": annotated,
                "resolvable_denominator": prop_rows,
                "materialized_pct_of_resolvable": _pct(annotated, prop_rows),
                "denominator_mode": "rows",
                "WRONG_standalone_over_all_props_pct": None,
            }

    # overall depth%: fraction of CALLS edges that carry ANY promoted/annotated depth fact
    calls_total = cur.execute("SELECT COUNT(*) FROM edges WHERE type='CALLS'").fetchone()[0]
    calls_with_depth = cur.execute(
        "SELECT COUNT(*) FROM edges WHERE type='CALLS' AND metadata IS NOT NULL AND metadata!='' "
        "AND (metadata LIKE '%dataflow=%' OR metadata LIKE '%usage=%')"
    ).fetchone()[0]
    promote_edges_total = cur.execute(
        "SELECT COUNT(*) FROM edges WHERE resolution_method LIKE 'promote_%'"
    ).fetchone()[0]

    # --- NAMING (TRAP 2) ---
    name_match = cur.execute(
        "SELECT COUNT(*) FROM edges WHERE type='CALLS' AND resolution_method='name_match'"
    ).fetchone()[0]
    det = calls_total - name_match
    trust_mix = {
        str(r[0]): r[1]
        for r in cur.execute(
            "SELECT trust_tier, COUNT(*) FROM edges WHERE type='CALLS' GROUP BY trust_tier ORDER BY 2 DESC"
        )
    }
    laundered = cur.execute(
        "SELECT COUNT(*) FROM edges WHERE resolution_method='name_match' AND trust_tier='CERTIFIED'"
    ).fetchone()[0]

    # residual composition of the name_match
    nm_rows = cur.execute(
        "SELECT e.candidate_count, e.source_file, n.label "
        "FROM edges e LEFT JOIN nodes n ON e.target_id=n.id "
        "WHERE e.type='CALLS' AND e.resolution_method='name_match'"
    ).fetchall()
    res_vendored = sum(1 for cc, sf, lb in nm_rows if sf and VENDOR_RE.search(str(sf)))
    res_promotable = sum(1 for cc, sf, lb in nm_rows if (cc is not None and int(cc) == 1))
    res_method_target = sum(1 for cc, sf, lb in nm_rows if lb in METHOD_LABELS)
    res_external = sum(1 for cc, sf, lb in nm_rows if lb is None)  # unresolved target -> external/stdlib

    # --- LSP residual (TRAP 3): language-correct method-target residual ---
    res_method_only_label = cur.execute(
        "SELECT COUNT(*) FROM edges e JOIN nodes n ON e.target_id=n.id "
        "WHERE e.resolution_method='name_match' AND n.label='Method'"
    ).fetchone()[0]
    res_lang_correct = cur.execute(
        "SELECT COUNT(*) FROM edges e JOIN nodes n ON e.target_id=n.id "
        "WHERE e.resolution_method='name_match' AND n.label IN ('Method','Function')"
    ).fetchone()[0]
    lsp_edges = cur.execute(
        "SELECT COUNT(*) FROM edges WHERE resolution_method='lsp'"
    ).fetchone()[0]

    con.close()

    result = {
        "db_path": db_path,
        "language": language,
        "node_total": node_total,
        "node_label_mix": label_mix,
        "edge_type_mix": edge_type_mix,
        "calls_total": calls_total,
        "depth": {
            "per_relation": depth,
            "calls_with_depth_annotation": calls_with_depth,
            "promote_edges_total": promote_edges_total,
            "depth_pct_of_calls": _pct(calls_with_depth + promote_edges_total, calls_total),
        },
        "naming": {
            "calls_total": calls_total,
            "name_match": name_match,
            "name_match_pct": _pct(name_match, calls_total),
            "deterministic": det,
            "det_pct": _pct(det, calls_total),
            "trust_tier_mix": trust_mix,
            "laundered_name_match_certified": laundered,
            "ASSERT_zero_laundered": (laundered == 0),
            "residual_composition": {
                "name_match_total": name_match,
                "vendored_or_minified": res_vendored,
                "candidate_count_eq_1_promotable": res_promotable,
                "method_target_needs_receiver_type": res_method_target,
                "external_or_stdlib_unresolved_target": res_external,
            },
        },
        "lsp_residual": {
            "lsp_edges": lsp_edges,
            "method_residual_METHOD_ONLY_label_TRAP": res_method_only_label,
            "method_residual_language_correct": res_lang_correct,
            "undercount_factor_if_method_only": f8(
                res_lang_correct / res_method_only_label
            ) if res_method_only_label else None,
            "is_false_no_op_under_method_only": (res_method_only_label == 0 and res_lang_correct > 0),
        },
    }
    return result


# ============================================================================
# BRIEF MEASUREMENT  (TRAP 5)
# ============================================================================
def _estimate_tokens(text: str) -> int:
    # mirror src/groundtruth/pretask/v1r_brief.py:1028 exactly
    return len(text) // 4 + 1


def _extract_brief_from_trajectory(path: str) -> Optional[str]:
    """Pull the first agent observation that carries the L1 brief (the gt-task-brief /
    gt-localization block embedded in the first user prompt)."""
    msgs = _load_messages(path)
    for m in msgs:
        s = _content_str(m)
        low = s.lower()
        if "<gt-task-brief" in low or "<gt-localization" in low:
            return s
    return None


def measure_brief(brief_or_path: str) -> Dict[str, Any]:
    # accept raw brief text, a .txt file, or a trajectory to extract the brief from
    text: Optional[str] = None
    if os.path.isfile(brief_or_path):
        if brief_or_path.endswith(".json") or brief_or_path.endswith(".jsonl"):
            text = _extract_brief_from_trajectory(brief_or_path)
        else:
            with open(brief_or_path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
    else:
        text = brief_or_path  # raw string passed inline

    if not text:
        return {"source": brief_or_path, "found": False, "note": "no brief text found"}

    full = text
    full_low = full.lower()
    full_total = len(full)

    # Bound the GT-AUTHORED region precisely: from the first GT marker to the close of the
    # last GT block. The token budget (MAX_BRIEF_TOKENS=600) governs the GT-authored brief,
    # NOT the surrounding PR description / task scaffolding that GT did not write. We report
    # BOTH: gt_region tokens (vs budget) and the full injected-observation size (context).
    _open_markers = ["<gt-localization", "<gt-task-brief", "<gt-brief"]
    _close_markers = ["</gt-scope", "</gt-evidence", "</gt-graph-map", "</gt-task-brief", "</gt-localization"]
    start = min([p for p in (full_low.find(m) for m in _open_markers) if p >= 0], default=-1)
    end = max([full_low.rfind(m) for m in _close_markers], default=-1)
    if start >= 0 and end > start:
        # extend end past the closing tag itself
        tag_end = full_low.find(">", end)
        end = tag_end + 1 if tag_end > end else full_total
        gt_region = full[start:end]
    else:
        gt_region = full  # fallback: whole text is the brief

    region = gt_region
    region_low = region.lower()
    tok = _estimate_tokens(region)
    full_tok = _estimate_tokens(full)

    # sig bloat: un-stripped docstring prose inside rendered signatures (measured over the
    # GT region -- this is GT's rendering choice, not the upstream PR text).
    doc_open = region.count('Doc(')
    triple_q = region.count('"""')

    # graph-map position WITHIN the GT region (is the unique structural value buried behind
    # a multi-KB signature dump that GT itself rendered?)
    pos_map = region_low.find("<gt-graph-map")
    pos_taskbrief = region_low.find("<gt-task-brief")
    rtotal = len(region)
    marker_positions_in_region = {
        "gt-localization": region_low.find("<gt-localization"),
        "gt-task-brief": pos_taskbrief,
        "gt-graph-map": pos_map,
        "gt-evidence": region_low.find("<gt-evidence"),
        "gt-scope": region_low.find("<gt-scope"),
    }
    # full-text positions too (blocks may be interleaved with the PR description and live
    # outside the contiguous GT head region -- report both so nothing is hidden).
    marker_positions_full = {
        "gt-localization": full_low.find("<gt-localization"),
        "gt-task-brief": full_low.find("<gt-task-brief"),
        "gt-graph-map": full_low.find("<gt-graph-map"),
        "gt-evidence": full_low.find("<gt-evidence"),
        "gt-scope": full_low.find("<gt-scope"),
    }
    return {
        "source": brief_or_path,
        "found": True,
        "gt_region_chars": rtotal,
        "gt_region_tokens": tok,
        "full_injected_chars": full_total,
        "full_injected_tokens": full_tok,
        "max_brief_tokens_budget": MAX_BRIEF_TOKENS,
        "over_budget": (tok > MAX_BRIEF_TOKENS),
        "tokens_over_budget": max(0, tok - MAX_BRIEF_TOKENS),
        "budget_utilization_ratio": f8(tok / MAX_BRIEF_TOKENS) if MAX_BRIEF_TOKENS else None,
        "sig_bloat": {
            "Doc_open_count": doc_open,
            "triple_quote_count": triple_q,
            "docstring_prose_present": (doc_open > 0 or triple_q > 0),
        },
        "graph_map_position": {
            "char_offset_in_region": pos_map,
            "depth_ratio_in_region": f8(pos_map / rtotal) if (pos_map >= 0 and rtotal) else None,
            "buried_behind_signature_dump": (pos_map > pos_taskbrief >= 0 and pos_map > rtotal * 0.4),
        },
        "marker_char_positions_in_region": marker_positions_in_region,
        "marker_char_positions_full": marker_positions_full,
    }


# ============================================================================
# DELIVERY MEASUREMENT  (TRAP 4 -- AGENT-OBSERVATION rule)
# ============================================================================
def _content_str(m: Any) -> str:
    if isinstance(m, str):
        return m
    if not isinstance(m, dict):
        return str(m)
    c = m.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        out = []
        for x in c:
            if isinstance(x, dict):
                out.append(x.get("text", "") or x.get("content", "") or "")
            else:
                out.append(str(x))
        return "\n".join(out)
    return str(c) if c is not None else ""


def _load_messages(path: str) -> List[Dict[str, Any]]:
    """Load a chronological message list from a mini-swe-agent trajectory.json
    ({info, messages, ...}) or an output.jsonl (one record per line)."""
    if path.endswith(".jsonl"):
        msgs: List[Dict[str, Any]] = []
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # output.jsonl may nest a history/messages list per task record
                if isinstance(rec, dict) and isinstance(rec.get("messages"), list):
                    msgs.extend(rec["messages"])
                elif isinstance(rec, dict) and isinstance(rec.get("history"), list):
                    msgs.extend(rec["history"])
                else:
                    msgs.append(rec)
        return msgs
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        data = json.load(fh)
    if isinstance(data, dict) and isinstance(data.get("messages"), list):
        return data["messages"]
    if isinstance(data, dict) and isinstance(data.get("history"), list):
        return data["history"]
    if isinstance(data, list):
        return data
    return []


# roles whose content the AGENT SEES (observation channel)
_OBS_ROLES = {"user", "tool", "system", "observation", "environment"}
# roles that are the AGENT's own actions
_ACT_ROLES = {"assistant", "agent", "action"}


def measure_delivery(path: str) -> Dict[str, Any]:
    msgs = _load_messages(path)
    n = len(msgs)
    role_counts: Dict[str, int] = {}
    for m in msgs:
        r = str(m.get("role", "?")) if isinstance(m, dict) else "?"
        role_counts[r] = role_counts.get(r, 0) + 1

    # Identify the fire-once L1 brief observation index (the message carrying the brief).
    # Several layers' blocks (evidence / graph-map / scope) are BUNDLED inside this one
    # fire-once brief in the v1r-map inject-once design. Delivery inside that bundle is NOT
    # the same as an independent post-event hook firing later in the run -- we flag both.
    l1_idx = -1
    l1_markers = [mk.lower() for mk in LAYER_MARKERS["L1_brief"]["delivered_markers"]]
    for i, m in enumerate(msgs):
        if isinstance(m, dict) and str(m.get("role", "")) in _OBS_ROLES:
            s = _content_str(m).lower()
            if any(mk in s for mk in l1_markers):
                l1_idx = i
                break

    # chronological scan: for each layer, find first observation index that carries a
    # delivered marker; then look at LATER agent-action messages to test consumption.
    per_layer: Dict[str, Any] = {}
    for layer, meta in LAYER_MARKERS.items():
        markers = [mk.lower() for mk in meta["delivered_markers"]]
        delivered_idx = -1
        delivered_role = None
        for i, m in enumerate(msgs):
            if not isinstance(m, dict):
                continue
            role = str(m.get("role", ""))
            s = _content_str(m).lower()
            if any(mk in s for mk in markers):
                if role in _OBS_ROLES:
                    delivered_idx = i
                    delivered_role = role
                    break
                # a marker only in an assistant message is the agent echoing, not delivery
        emitted = any(
            any(mk in _content_str(m).lower() for mk in markers)
            for m in msgs if isinstance(m, dict)
        )
        delivered = delivered_idx >= 0
        # consumption: after delivery, did an agent ACTION reference the delivered content?
        consumed = False
        consumed_idx = -1
        if delivered:
            # pull distinctive tokens (file paths, symbol names) from the delivered block
            block = _content_str(msgs[delivered_idx])
            ref_tokens = _distinctive_tokens(block, markers)
            for j in range(delivered_idx + 1, n):
                mj = msgs[j]
                if not isinstance(mj, dict):
                    continue
                if str(mj.get("role", "")) not in _ACT_ROLES:
                    continue
                act = _content_str(mj).lower()
                if any(tok in act for tok in ref_tokens):
                    consumed = True
                    consumed_idx = j
                    break
        # Distinguish brief-bundle delivery from an independent post-event hook firing.
        # If a layer's marker only appears inside the fire-once L1 brief message, it rode
        # the brief bundle (inject-once) -- it is NOT evidence the event hook fired live.
        in_l1_bundle = (delivered and l1_idx >= 0 and delivered_idx == l1_idx and layer != "L1_brief")
        independent_hook = (delivered and meta["event"] and not in_l1_bundle)
        per_layer[layer] = {
            "role": meta["role"],
            "is_event_hook": meta["event"],
            "emitted": emitted,
            "delivered_to_observation": delivered,
            "delivered_at_msg_index": delivered_idx,
            "delivered_in_role": delivered_role,
            "delivered_in_l1_brief_bundle": in_l1_bundle,
            "independent_post_event_hook_firing": independent_hook,
            "consumed_by_agent_action": consumed,
            "consumed_at_msg_index": consumed_idx,
            # event hooks: absence != broken (the triggering event simply didn't fire)
            "absence_means_event_not_fired": (meta["event"] and not emitted),
        }

    return {
        "trajectory_path": path,
        "message_count": n,
        "role_counts": role_counts,
        "per_layer": per_layer,
    }


def _distinctive_tokens(block: str, markers: List[str], limit: int = 40) -> List[str]:
    """Extract file-paths and symbol-ish tokens from a delivered block to test whether a
    later agent action references it. Generalized: file paths (slash + extension) and
    CamelCase / snake_case identifiers of length >= 4. Lowercased for matching."""
    toks: set = set()
    # file paths like fastapi/routing.py
    for m in re.finditer(r"[A-Za-z0-9_./\-]+\.[A-Za-z0-9]{1,4}\b", block):
        p = m.group(0)
        if "/" in p or "." in p:
            toks.add(p.lower())
    # identifiers
    for m in re.finditer(r"\b[A-Za-z_][A-Za-z0-9_]{3,}\b", block):
        w = m.group(0)
        wl = w.lower()
        if wl in ("none", "true", "false", "self", "this", "return", "import", "class", "function"):
            continue
        if wl.startswith("gt-") or wl in ("gt", "evidence", "scope", "brief", "graph", "localization"):
            continue
        toks.add(wl)
    # prefer longer/path tokens, cap to keep matching cheap and precise
    ordered = sorted(toks, key=lambda s: (-len(s), s))
    return ordered[:limit]


# ============================================================================
# RENDERING
# ============================================================================
def _print_graph_table(name: str, g: Dict[str, Any]) -> None:
    print(f"\n================ GRAPH: {name}  ({g['language']})  nodes={g['node_total']} CALLS={g['calls_total']} ================")
    print("-- DEPTH materialization (CORRECT denominator = facts whose call-target resolves internal) --")
    print(f"   {'relation':18} {'edge_type':16} {'mat=stand+annot':18} {'resolvable':>10} {'mat%resolvable':>15} {'WRONG(std/allprops)':>20}")
    for kind, d in g["depth"]["per_relation"].items():
        mat = f"{d['materialized']}={d['standalone_edges']}+{d['calls_annotations']}"
        wrong = d["WRONG_standalone_over_all_props_pct"]
        wrong_s = f"{wrong:>20.8f}" if isinstance(wrong, (int, float)) else f"{'N/A':>20}"
        print(f"   {kind:18} {str(d['edge_type']):16} {mat:18} {d['resolvable_denominator']:>10} "
              f"{d['materialized_pct_of_resolvable']:>15.8f} {wrong_s}")
    print(f"   depth%% of CALLS (any annot+promote): {g['depth']['depth_pct_of_calls']:.8f}  "
          f"(calls_with_depth_annot={g['depth']['calls_with_depth_annotation']}, promote_edges={g['depth']['promote_edges_total']})")
    nm = g["naming"]
    print("-- NAMING --")
    print(f"   name_match={nm['name_match']} ({nm['name_match_pct']:.8f}%)  det={nm['deterministic']} ({nm['det_pct']:.8f}%)")
    print(f"   trust_tier_mix={nm['trust_tier_mix']}")
    print(f"   0-LAUNDERED assert (name_match&CERTIFIED==0): {nm['laundered_name_match_certified']} -> {'PASS' if nm['ASSERT_zero_laundered'] else 'FAIL'}")
    rc = nm["residual_composition"]
    print(f"   residual: vendored={rc['vendored_or_minified']} promotable(cc==1)={rc['candidate_count_eq_1_promotable']} "
          f"method-target={rc['method_target_needs_receiver_type']} external/stdlib={rc['external_or_stdlib_unresolved_target']}")
    lr = g["lsp_residual"]
    print("-- LSP RESIDUAL (TRAP 3: language-correct label filter) --")
    print(f"   lsp_edges={lr['lsp_edges']}  residual[Method-only TRAP]={lr['method_residual_METHOD_ONLY_label_TRAP']}  "
          f"residual[lang-correct Method|Function]={lr['method_residual_language_correct']}  "
          f"undercount_factor={lr['undercount_factor_if_method_only']}  false_no_op_under_method_only={lr['is_false_no_op_under_method_only']}")


def _print_brief_table(b: Dict[str, Any]) -> None:
    print(f"\n================ BRIEF: {b['source']} ================")
    if not b.get("found"):
        print("   no brief found.")
        return
    print(f"   GT-region chars={b['gt_region_chars']} tokens(len//4+1)={b['gt_region_tokens']} budget={b['max_brief_tokens_budget']} "
          f"over_budget={b['over_budget']} (+{b['tokens_over_budget']}) util={b['budget_utilization_ratio']}")
    print(f"   full injected observation: chars={b['full_injected_chars']} tokens={b['full_injected_tokens']} (brief + PR text + scaffold)")
    sb = b["sig_bloat"]
    print(f"   sig-bloat (in GT region): Doc(={sb['Doc_open_count']} triple_quote={sb['triple_quote_count']} prose_present={sb['docstring_prose_present']}")
    gm = b["graph_map_position"]
    print(f"   <gt-graph-map> at region-char {gm['char_offset_in_region']} depth_ratio={gm['depth_ratio_in_region']} buried={gm['buried_behind_signature_dump']}")
    print(f"   marker positions (in region): {b['marker_char_positions_in_region']}")


def _print_delivery_table(d: Dict[str, Any]) -> None:
    print(f"\n================ DELIVERY: {d['trajectory_path']} ================")
    print(f"   messages={d['message_count']} roles={d['role_counts']}")
    print(f"   {'layer':16} {'event':6} {'emit':5} {'deliv':6} {'@obs':5} {'in_L1bundle':11} {'indep_hook':10} {'consumed':8} {'@act':5}")
    for layer, p in d["per_layer"].items():
        print(f"   {layer:16} {str(p['is_event_hook']):6} {str(p['emitted']):5} {str(p['delivered_to_observation']):6} "
              f"{str(p['delivered_at_msg_index']):5} {str(p['delivered_in_l1_brief_bundle']):11} "
              f"{str(p['independent_post_event_hook_firing']):10} {str(p['consumed_by_agent_action']):8} {str(p['consumed_at_msg_index']):5}")
    notfired = [l for l, p in d["per_layer"].items() if p["absence_means_event_not_fired"]]
    if notfired:
        print(f"   NOTE (event hooks absent => triggering event did not occur, NOT broken): {notfired}")
    bundled = [l for l, p in d["per_layer"].items() if p["delivered_in_l1_brief_bundle"]]
    if bundled:
        print(f"   NOTE (delivered inside the fire-once L1 brief bundle, not an independent live hook firing): {bundled}")


# ============================================================================
# CLI
# ============================================================================
def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="GroundTruth per-layer measurement harness (correct metrics).")
    sub = ap.add_subparsers(dest="cmd", required=True)

    pg = sub.add_parser("graph", help="per-relation depth + naming + lsp-residual from a graph.db")
    pg.add_argument("db")
    pg.add_argument("--json", dest="json_out", default=None)

    pb = sub.add_parser("brief", help="brief token/sig-bloat/graph-map from brief.txt or trajectory")
    pb.add_argument("brief")
    pb.add_argument("--json", dest="json_out", default=None)

    pd = sub.add_parser("delivery", help="per-layer emitted/delivered/consumed from a trajectory")
    pd.add_argument("trajectory")
    pd.add_argument("--json", dest="json_out", default=None)

    pa = sub.add_parser("all", help="graph + brief + delivery in one deep record")
    pa.add_argument("--graph", dest="graph_db", required=True)
    pa.add_argument("--trajectory", dest="trajectory", default=None)
    pa.add_argument("--name", dest="name", default=None)
    pa.add_argument("--json", dest="json_out", default=None)

    args = ap.parse_args(argv)

    record: Dict[str, Any] = {}

    if args.cmd == "graph":
        g = measure_graph(args.db)
        _print_graph_table(os.path.basename(os.path.dirname(args.db)) or args.db, g)
        record = {"graph": g}
    elif args.cmd == "brief":
        b = measure_brief(args.brief)
        _print_brief_table(b)
        record = {"brief": b}
    elif args.cmd == "delivery":
        d = measure_delivery(args.trajectory)
        _print_delivery_table(d)
        record = {"delivery": d}
    elif args.cmd == "all":
        nm = args.name or (os.path.basename(os.path.dirname(args.graph_db)) or args.graph_db)
        g = measure_graph(args.graph_db)
        _print_graph_table(nm, g)
        record = {"name": nm, "graph": g}
        if args.trajectory:
            b = measure_brief(args.trajectory)
            _print_brief_table(b)
            d = measure_delivery(args.trajectory)
            _print_delivery_table(d)
            record["brief"] = b
            record["delivery"] = d

    if getattr(args, "json_out", None):
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(record, fh, indent=2, ensure_ascii=False)
        print(f"\n[json] wrote 8-dp deep record -> {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
