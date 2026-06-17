#!/usr/bin/env python3
"""gt_layer_audit.py - the DEPTH-FIRST, per-LAYER, live-behavioral-gap audit (gt_trial §4).

ONE language-agnostic audit for ANY of the 5 languages. It reads a run's artifacts GENERICALLY
(graph.db edge types + resolution methods, the substrate certs, the agent trajectory tags) and
walks GT layer by layer, STARTING FROM DEPTH (Layer 0), reporting for EACH layer:
    INTENDED (what gt_main/gt_gt says it should do)  vs  ACTUAL (what this run did)  -> FIRED? + GAP.

No per-language logic: edge types, resolution methods, cert fields, and `<gt-...>` trajectory tags
are the SAME contract for py/go/ts/js/rust. The "live behavioral gap" is the delta per layer.

Usage:
    python scripts/gt_layer_audit.py --graph <graph.db> --certs <dir> --trajectory <traj.json> \
        --lang <language> [--task <id>]

Deterministic layers (depth/naming/LSP/embedder) are computed exactly here. The semantic legs
(CONSUMED / RIGHT-TRAJECTORY) are flagged FIRED=yes/no from tag presence; the gt_trial §4 verifier
agent judges consumption from the chronological read - this script gives it the per-layer skeleton.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

# The promoted DEPTH relationship-edge types (Layer 0 completeness, gt_gt §2.6) - language-agnostic.
_DEPTH_EDGES = ("READS", "WRITES", "RAISES", "PRECEDES", "DATA_FLOW", "CO_SERIALIZES")
# The agent-visible GT payload tags, per layer (gt_gt §6).
_LAYER_TAGS = {
    "L1.brief": ("<gt-task-brief>", "<gt-localization>", "<gt-graph-map>", "<gt-orientation>"),
    "L3b.evidence": ("<gt-evidence",),
    "L3.contract": ("<gt-contract",),
    "consensus.scope": ("<gt-scope",),
    "cochange": ("<gt-cochange",),
    "oracle.nudge": ("<gt-nudge",),
}


def _q(cur, sql, *a):
    try:
        return cur.execute(sql, a).fetchone()[0]
    except Exception:
        return None


# The §2.6 depth EDGE class -> the parser SOURCE PROPERTY that is its raw edge-construct.
# A class is correct-or-quiet (NOT a gap) when its source property is ABSENT (the repo lacks the
# construct). It NEEDS-ADJUDICATION (possible gap; the internal-target check decides) when the source
# property is PRESENT but 0 edges were minted — that is either a builtin/external target (correct-or-
# quiet, non-invention) or a real promote gap. See .claude/reports/DEPTH_PARITY_METHODOLOGY.md.
_DEPTH_SOURCE_PROP = {
    "READS": "field_read", "WRITES": "field_write", "RAISES": "exception_type",
    "PRECEDES": "call_order", "CO_SERIALIZES": "serialization_pair", "DATA_FLOW": "data_flow",
}

# Builtin/stdlib/literal targets whose endpoint has NO internal node -> the relation MUST stay a
# property and mint 0 edges (gt_gt §2.6 non-invention). MIRRORED from the resolver's canonical
# `builtinExceptionNames` (gt-index/internal/resolver/promote.go) — DO NOT invent a new per-language
# list; this is the SAME union the promote pass excludes, plus the builtin RECEIVERS/intrinsics
# (Object/Map/Set/Array/String/panic/unwrap/…) that promote leaves unresolved by construction (no node
# exists for them). A `props>0, edges==0` class whose extracted targets are all builtins/literals is
# CONSTRUCT-PRESENT-BUILTIN (correct-or-quiet, PASS), NOT a promote gap.
_BUILTIN_EXCEPTIONS = frozenset({
    # Python (mirror of promote.go builtinExceptionNames)
    "ValueError", "TypeError", "KeyError", "IndexError", "RuntimeError", "Exception",
    "AttributeError", "NotImplementedError", "StopIteration", "OSError", "IOError",
    "FileNotFoundError", "ZeroDivisionError", "ArithmeticError", "AssertionError", "ImportError",
    "ModuleNotFoundError", "NameError", "LookupError", "MemoryError", "OverflowError",
    "RecursionError", "ReferenceError", "SyntaxError", "SystemError", "UnicodeError",
    "UnicodeDecodeError", "UnicodeEncodeError", "PermissionError", "ConnectionError", "TimeoutError",
    "BrokenPipeError", "BufferError", "EOFError", "FloatingPointError", "GeneratorExit",
    "KeyboardInterrupt", "SystemExit", "TabError", "IndentationError", "UnboundLocalError",
    "BlockingIOError", "ChildProcessError", "FileExistsError", "InterruptedError", "IsADirectoryError",
    "NotADirectoryError", "ProcessLookupError", "StopAsyncIteration", "Error",
    # JS
    "RangeError", "EvalError", "URIError", "AggregateError", "DOMException",
})
# Builtin RECEIVERS / intrinsics that PRECEDES/READS/WRITES/CO_SERIALIZES legitimately reference but
# that have no internal node (promote resolves them to nothing -> 0 edges is correct). Language-
# agnostic union of the common stdlib carriers the brief enumerates (gt_gt §2.6 / CLAUDE.md).
_BUILTIN_RECEIVERS = frozenset({
    "Object", "Map", "Set", "Array", "String", "Number", "Boolean", "Promise", "JSON", "Math",
    "Date", "RegExp", "Symbol", "WeakMap", "WeakSet", "Reflect", "Proxy",
    "string", "list", "dict", "set", "tuple", "bytes", "int", "float", "bool",
    "panic", "unwrap", "expect", "Some", "None", "Ok", "Err",
})


def _is_builtin_target(tok):
    """True when an extracted target token is a builtin/stdlib/literal endpoint (no internal node
    should exist) -> 0 edges is correct-or-quiet (non-invention), NOT a promote gap."""
    return tok in _BUILTIN_EXCEPTIONS or tok in _BUILTIN_RECEIVERS


def _node_name_exists(cur, name):
    """True when `name` is a class-like node (Class/Struct/Type/Enum/Interface/ImplBlock) in `nodes`
    — the same class-like superset promote.go resolves RAISES/READS/WRITES against. A clean, non-
    builtin token that matches such a node is an INTERNAL target the relation SHOULD have promoted to."""
    try:
        return (cur.execute(
            "SELECT 1 FROM nodes WHERE name=? AND label IN "
            "('Class','Struct','Type','Enum','Interface','ImplBlock') LIMIT 1", (name,)
        ).fetchone() is not None)
    except Exception:
        return False


def _extract_targets(value):
    """Pull candidate target identifier(s) from a depth source-property VALUE, mirroring the promote
    regexes (promote.go) WITHOUT re-running the promote pass. Returns the clean leading-identifier
    tokens a relation would resolve against. Dotted/qualified tokens (errors.New) and prose are NOT
    clean internal targets (promote DROPS them) -> excluded here too, so they never read as a gap."""
    import re as _re
    toks = []
    for m in _re.findall(r"[A-Za-z_][\w.]*", value or ""):
        # D3 drop-dotted: a qualified name (mod.MyError, Object.values) is not a clean internal target;
        # take the LEADING segment (the receiver/base) as the candidate — Object.freeze -> Object.
        base = m.split(".", 1)[0]
        if base:
            toks.append(base)
    return toks


def _classify_adjudication(cur, prop_kind):
    """The INTERNAL-TARGET CHECK the §2.6 bar owed but never implemented. For a depth class with
    source-properties but 0 promoted edges, read its property VALUES and decide which of three it is:
      'promote_gap'  -> at least one CLEAN, NON-builtin token matches an internal class-like node in
                        `nodes` (promote SHOULD have minted that edge but didn't) = a real gap (FAIL).
      'builtin'      -> every extracted target is a builtin/literal OR has no internal node (builtin
                        receiver / external / stdlib) = CONSTRUCT-PRESENT-BUILTIN, 0 edges correct (PASS).
      'adjudicate'   -> no candidate token could be cleanly extracted from any value (unknown shape)
                        = correct-or-quiet, surfaced but NOT auto-FAILed (the brief's tri-state rule).
    Generalized across languages: no task/repo/symbol-specific logic; the builtin set is mirrored from
    the resolver, the internal-target test is a generic node-name existence query."""
    try:
        rows = cur.execute(
            "SELECT value FROM properties WHERE kind=? LIMIT 5000", (prop_kind,)).fetchall()
    except Exception:
        return "adjudicate"
    saw_candidate = False
    for (value,) in rows:
        for tok in _extract_targets(value):
            saw_candidate = True
            if _is_builtin_target(tok):
                continue  # builtin/literal endpoint -> non-invention, not a gap
            if _node_name_exists(cur, tok):
                return "promote_gap"  # internal class-like target exists -> SHOULD have promoted
    return "builtin" if saw_candidate else "adjudicate"


def audit_depth(graph_db):
    """Layer 0 - DEPTH (gt_gt §2.6, the 100% bar): CONSTRUCT-AWARE per-class status. The lenient
    `depth_present>0` verdict is gone (it false-GREENed). The raw-construct check is gone (it false-
    REDed builtin throws). Instead, per class compare EDGES vs the source-PROPERTY (the edge-construct),
    and when props>0 but 0 edges, run the INTERNAL-TARGET CHECK (§2.6) on the property VALUES:
      PRESENT                    edges>0
      CONSTRUCT-ABSENT           source-property==0  (the repo lacks the construct -> correct-or-quiet)
      CONSTRUCT-PRESENT-BUILTIN  props>0, edges==0, all targets builtin/literal (Error, Object.freeze,
                                 panic/unwrap, …) -> 0 edges is CORRECT (non-invention) = PASS
      PROMOTE-GAP                props>0, edges==0, a CLEAN non-builtin target IS an internal node
                                 (promote SHOULD have minted) -> a real gap = FAIL
      NEEDS-ADJUDICATION         props>0, edges==0, target shape unknown -> correct-or-quiet, NOT FAIL
    DATA_FLOW rides CALLS as a `dataflow=` annotation (D1): present = standalone OR annotated.
    Only a PROMOTE-GAP fails depth; a builtin-target class with 0 edges is the §2.6 non-invention bar
    being honoured, NOT a defect (this is the fix for the superjson false-FAIL: exception_type=Error,
    call_order=Object.values->freeze, exception_flow=throw new Error(...) -> all builtin -> PASS)."""
    c = sqlite3.connect(graph_db).cursor()
    rows = c.execute("SELECT type, COUNT(*) FROM edges GROUP BY type ORDER BY COUNT(*) DESC").fetchall()
    by_type = {t: n for t, n in rows}
    df_annotated = _q(c, "SELECT COUNT(*) FROM edges WHERE type='CALLS' AND metadata LIKE '%dataflow%'") or 0

    per_class = {}
    present, construct_absent, construct_builtin, promote_gap, needs_adjudication = [], [], [], [], []
    for cls in _DEPTH_EDGES:
        edges = by_type.get(cls, 0)
        if cls == "DATA_FLOW":
            edges = edges or df_annotated  # D1: annotation counts as present
        props = _q(c, "SELECT COUNT(*) FROM properties WHERE kind=?", _DEPTH_SOURCE_PROP[cls]) or 0
        if edges > 0:
            status = "PRESENT"; present.append(cls)
        elif props == 0:
            status = "CONSTRUCT-ABSENT"; construct_absent.append(cls)
        else:
            # props>0, edges==0: the internal-target check decides builtin (PASS) vs real gap (FAIL).
            verdict = _classify_adjudication(c, _DEPTH_SOURCE_PROP[cls])
            if verdict == "promote_gap":
                status = "PROMOTE-GAP"; promote_gap.append(cls)
            elif verdict == "builtin":
                status = "CONSTRUCT-PRESENT-BUILTIN"; construct_builtin.append(cls)
            else:
                status = "NEEDS-ADJUDICATION"; needs_adjudication.append(cls)
        per_class[cls] = {"edges": edges, "source_props": props, "status": status}

    # Construct-aware verdict: depth FAILS only on a PROMOTE-GAP (a clean non-builtin internal target
    # that should have minted but didn't). PRESENT / CONSTRUCT-ABSENT / CONSTRUCT-PRESENT-BUILTIN are
    # the §2.6 bar being honoured. NEEDS-ADJUDICATION (unknown target shape) is correct-or-quiet and is
    # surfaced but NOT auto-FAILed (the brief's tri-state rule: when uncertain, adjudicate, don't FAIL).
    depth_ok = not promote_gap
    return {
        "edge_types": by_type, "per_class": per_class,
        "present": present, "construct_absent": construct_absent,
        "construct_builtin": construct_builtin, "promote_gap": promote_gap,
        "needs_adjudication": needs_adjudication,
        "intended": "every class with its INTERNAL-target edge-construct -> edge minted (§2.6 100% bar); builtin/literal target -> stays property (non-invention); construct-absent = correct-or-quiet",
        "fired": depth_ok,
        "gap": ("none (all classes PRESENT / construct-absent / construct-present-builtin)" if depth_ok
                else f"PROMOTE-GAP (source-prop present, internal target node exists, 0 edges minted): {','.join(promote_gap)}"),
    }


def audit_naming(graph_db):
    """Resolver/NAMING: CALLS resolution - det_pct (resolved facts) vs name_match residual + typing tiers."""
    c = sqlite3.connect(graph_db).cursor()
    calls = _q(c, "SELECT COUNT(*) FROM edges WHERE type='CALLS'") or 0
    breakdown = dict(c.execute(
        "SELECT resolution_method, COUNT(*) FROM edges WHERE type='CALLS' GROUP BY resolution_method").fetchall())
    nm = breakdown.get("name_match", 0)
    det_pct = (100.0 * (calls - nm) / calls) if calls else 0.0
    tiers = {m: breakdown.get(m, 0) for m in ("type_flow", "impl_method", "inherited", "lsp", "verified_unique")}
    return {
        "calls_edges": calls, "name_match": nm, "det_pct": round(det_pct, 4),
        "breakdown": breakdown, "typing_tiers": tiers,
        "intended": "det_pct high (resolved facts dominate), name_match -> 0; method calls resolved via type/LSP, not guessed",
        "fired": calls > 0,
        "gap": ("none" if det_pct >= 80 else f"det_pct {det_pct:.1f}% < 80% - {nm} name_match guesses ({100*nm/max(1,calls):.1f}% of CALLS)"),
    }


# Receiver-UNPROVEN method-resolution rungs: name-uniqueness guesses, capped CANDIDATE (<=0.6) by the
# resolver, NEVER a deterministic fact (CLAUDE.md fact definition; commit 6fdf572b). The substrate-parity
# invariant: in a REAL graph.db, no edge of these methods may carry confidence > 0.6 (that would be the
# pre-#2 launder where unique_method emitted 0.85 and cleared the closure 0.7 reach floor).
_RECEIVER_UNPROVEN_METHODS = ("impl_method", "unique_method")
_RECEIVER_UNPROVEN_CAP = 0.6


def audit_facts(graph_db):
    """FACT-TIER invariant (#2): receiver-unproven rungs (impl_method/unique_method) are capped at the
    CANDIDATE ceiling (conf <= 0.6) so they can never read as a deterministic fact or clear the closure
    reach floor (0.7). Asserts on the REAL graph.db edges. A single such edge with conf > 0.6 is a launder."""
    c = sqlite3.connect(graph_db).cursor()
    rows = {}
    violations = []
    for m in _RECEIVER_UNPROVEN_METHODS:
        cnt = _q(c, "SELECT COUNT(*) FROM edges WHERE resolution_method=?", m) or 0
        mx = _q(c, "SELECT MAX(confidence) FROM edges WHERE resolution_method=?", m)
        rows[m] = {"count": cnt, "max_conf": mx}
        if mx is not None and mx > _RECEIVER_UNPROVEN_CAP + 1e-9:
            violations.append(f"{m} max_conf={mx} > {_RECEIVER_UNPROVEN_CAP} ({cnt} edges) -- LAUNDER")
    # closure presence: a unique_method/impl_method edge must NOT appear in the verified closure (it is
    # excluded from closure.verifiedMethods). The closure table stores (source,target) reach, not method,
    # so we check the invariant at the edge tier (conf cap) which gates closure admission upstream.
    ok = not violations
    return {"receiver_unproven": rows, "cap": _RECEIVER_UNPROVEN_CAP, "violations": violations,
            "intended": "impl_method/unique_method conf <= 0.6 (CANDIDATE) — receiver-unproven, never a fact (#2/6fdf572b)",
            "fired": ok,
            "gap": "none" if ok else "; ".join(violations)}


def audit_nodes(graph_db):
    """NODE LABELS - confirms type-def nodes exist (the fix#1 surface: Class/ImplBlock rankable)."""
    c = sqlite3.connect(graph_db).cursor()
    labels = dict(c.execute("SELECT label, COUNT(*) FROM nodes GROUP BY label").fetchall())
    typedef = labels.get("Class", 0) + labels.get("ImplBlock", 0)
    return {"labels": labels, "typedef_nodes": typedef,
            "intended": "type definitions (Class/ImplBlock) present AND rankable by the brief (fix#1)",
            "fired": typedef > 0,
            "gap": "none" if typedef > 0 else "no type-def nodes - brief cannot rank type-targeted edits"}


def _load_json(certs_dir, name):
    p = os.path.join(certs_dir, name)
    if os.path.isfile(p):
        try:
            return json.load(open(p, encoding="utf-8"))
        except Exception:
            return None
    return None


def audit_lsp(certs_dir):
    j = _load_json(certs_dir, "lsp_certificate.json") or {}
    warm = bool(j.get("warm_probe_ok") or j.get("lsp_warm"))
    work = j.get("effective_work", j.get("verified_edges", 0) or 0)
    return {"server": j.get("server_command"), "warm": warm, "verified_edges": j.get("verified_edges"),
            "corrected_edges": j.get("corrected_edges"), "effective_work": work,
            "verdict_hint": j.get("verdict_hint"),
            "intended": "LSP server warm + converting >0 method-call edges BEFORE scoring (gt_gt §3)",
            "fired": warm and (work or 0) > 0,
            "gap": "none" if warm and (work or 0) > 0 else f"LSP not converting (warm={warm}, work={work}) - method map stays name_match"}


def audit_embedder(certs_dir):
    j = _load_json(certs_dir, "embedder_certificate.json") or {}
    dim = str(j.get("embedder_dim", ""))
    wsem = j.get("effective_w_sem", 0)
    is_zero = (j.get("all_zero_semantic_reason", "") not in ("", None)) or (wsem in (0, "0", 0.0))
    return {"class": j.get("embedder_class"), "dim": dim, "effective_w_sem": wsem,
            "rendered_nonzero": j.get("rendered_semantic_nonzero_count"),
            "cos_related": j.get("cos_related"), "cos_unrelated": j.get("cos_unrelated"), "is_zero": is_zero,
            "intended": "gte-modernbert 768-d ONNX, effective_w_sem>0, semantic separating (NOT e5, NOT zeroed)",
            "fired": (dim == "768") and not is_zero,
            "gap": "none" if (dim == "768" and not is_zero) else f"embedder degraded (dim={dim}, w_sem={wsem}, zeroed={is_zero})"}


def audit_trajectory(trajectory_path):
    """Per-turn LAYERS - did each layer's tag FIRE in the agent's observations, and how many times."""
    if not trajectory_path or not os.path.isfile(trajectory_path):
        return {"loaded": False, "layers": {}}
    raw = open(trajectory_path, encoding="utf-8", errors="replace").read()
    layers = {}
    for layer, tags in _LAYER_TAGS.items():
        n = sum(raw.count(t) for t in tags)
        layers[layer] = {"fired_count": n, "fired": n > 0,
                         "intended": _LAYER_INTENT.get(layer, ""),
                         "gap": "none" if n > 0 else "DELIVERED=NO - tag never appears in the agent trajectory"}
    # assistant turn count (trajectory length)
    try:
        obj = json.loads(raw)
        msgs = obj if isinstance(obj, list) else obj.get("messages", obj.get("trajectory", []))
        turns = sum(1 for m in msgs if isinstance(m, dict) and m.get("role") == "assistant")
    except Exception:
        turns = raw.count('"role": "assistant"') + raw.count('"role":"assistant"')
    return {"loaded": True, "assistant_turns": turns, "layers": layers}


_LAYER_INTENT = {
    "L1.brief": "task-start brief delivered, ranks the gold file high (gt_gt §4/§12)",
    "L3b.evidence": "per-view/per-edit evidence (callers/contracts/siblings) on every edit (gt_gt §6)",
    "L3.contract": "behavioral contract (signature/callers - preserve interface) per edit (gt_gt §6)",
    "consensus.scope": "scope chain fires when the edit fans across components (gt_gt §6)",
    "cochange": "co-change files surfaced from git history (gt_gt §6)",
    "oracle.nudge": "per-turn steer (coherence-collapse/loop/verify) on trigger (gt_gt §15)",
}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="gt_layer_audit")
    ap.add_argument("--graph", required=True)
    ap.add_argument("--certs", default="")
    ap.add_argument("--trajectory", default="")
    ap.add_argument("--lang", default="?")
    ap.add_argument("--task", default="?")
    ap.add_argument("--substrate-strict", action="store_true",
                    help="exit non-zero on a RED substrate (gt_gt §2) cell — for the parity-matrix gate")
    a = ap.parse_args(argv)
    certs = a.certs or os.path.dirname(a.graph)

    report = {
        "task": a.task, "lang": a.lang,
        "L0_depth": audit_depth(a.graph),
        "naming": audit_naming(a.graph),
        "facts": audit_facts(a.graph),
        "nodes": audit_nodes(a.graph),
        "lsp": audit_lsp(certs),
        "embedder": audit_embedder(certs),
        "trajectory": audit_trajectory(a.trajectory),
    }

    # ---- render the depth-first per-layer behavioral-gap table ----
    print(f"\n=== GT LAYER AUDIT (depth-first, live behavioral gap) - {a.task} [{a.lang}] ===")
    order = [
        ("L0 DEPTH", report["L0_depth"]),
        ("NAMING/resolver", report["naming"]),
        ("FACTS/#2-caps", report["facts"]),
        ("NODES/type-defs", report["nodes"]),
        ("LSP", report["lsp"]),
        ("EMBEDDER", report["embedder"]),
    ]
    print(f"{'LAYER':18} {'FIRED':6} GAP")
    for name, r in order:
        print(f"{name:18} {('YES' if r['fired'] else 'NO '):6} {r['gap']}")
    tr = report["trajectory"]
    if tr.get("loaded"):
        print(f"-- per-turn layers (assistant turns: {tr['assistant_turns']}) --")
        for layer, r in tr["layers"].items():
            print(f"{layer:18} {('YES('+str(r['fired_count'])+')' if r['fired'] else 'NO '):8} {r['gap']}")
    else:
        print("-- per-turn layers: trajectory not provided (substrate-only audit) --")
    print("\n-- detail --")
    print(f"L0 depth edge types: { {k:v for k,v in report['L0_depth']['edge_types'].items()} }")
    print(f"depth per-class (edges/source_props/status):")
    for cls, d in report['L0_depth']['per_class'].items():
        print(f"    {cls:14} edges={d['edges']:<5} props={d['source_props']:<5} {d['status']}")
    print(f"naming det_pct={report['naming']['det_pct']}% name_match={report['naming']['name_match']} tiers={report['naming']['typing_tiers']}")
    print(f"facts/#2 receiver_unproven={report['facts']['receiver_unproven']} cap={report['facts']['cap']} violations={report['facts']['violations']}")
    print(f"nodes typedef={report['nodes']['typedef_nodes']} labels={report['nodes']['labels']}")
    print(f"lsp warm={report['lsp']['warm']} effective_work={report['lsp']['effective_work']} verdict={report['lsp']['verdict_hint']}")
    print(f"embedder class={report['embedder']['class']} dim={report['embedder']['dim']} w_sem={report['embedder']['effective_w_sem']} zeroed={report['embedder']['is_zero']}")

    # ---- STEP-1 SUBSTRATE PARITY VERDICT (graph.db, gt_gt §2): the per-(layer x language) cell. ----
    # Asserts the §2 trust model + #2 caps + §2.6 depth on the REAL graph.db. PASS only when ALL hold.
    # Each sub-check is read from real edges; a single violation is a RED cell (scoped to this language).
    sub_checks = {
        "nodes>0": (report["nodes"]["labels"] and sum(report["nodes"]["labels"].values()) > 0),
        "calls_edges>0": report["naming"]["calls_edges"] > 0,
        "depth_construct_aware": report["L0_depth"]["fired"],  # no class is a PROMOTE-GAP (§2.6 bar; builtin-target/adjudicate are correct-or-quiet)
        "facts_#2_caps": report["facts"]["fired"],  # impl_method/unique_method conf <= 0.6
    }
    substrate_pass = all(sub_checks.values())
    print("\n-- SUBSTRATE PARITY (gt_gt §2 cell) --")
    for k, v in sub_checks.items():
        print(f"  {('PASS' if v else 'FAIL'):4} {k}")
    _d = report["L0_depth"]
    print(f"SUBSTRATE_VERDICT[{a.lang}] = {'PASS' if substrate_pass else 'FAIL'} "
          f"(det_pct={report['naming']['det_pct']}% name_match={report['naming']['name_match']} "
          f"depth=present:{len(_d['present'])}/builtin:{len(_d['construct_builtin'])}/"
          f"gap:{len(_d['promote_gap'])}/adj:{len(_d['needs_adjudication'])} "
          f"#2={report['facts']['violations'] or 'capped'})")
    report["substrate_verdict"] = {"pass": substrate_pass, "checks": sub_checks}

    out = os.path.join(certs, f"gt_layer_audit_{a.task}.json")
    try:
        json.dump(report, open(out, "w", encoding="utf-8"), indent=1, default=str)
        print(f"\nwrote {out}")
    except Exception:
        pass
    # --substrate-strict: non-zero exit on a RED substrate cell (so the matrix gate can branch).
    if getattr(a, "substrate_strict", False) and not substrate_pass:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
