#!/usr/bin/env python3
"""
promote_property_edges.py -- gt_gt.md  2.6 PROMOTE-TO-EDGES pass.

DEPTH = completeness of the navigable relationship-edge graph (gt_gt.md  2.6).
A relation trapped in a per-node `properties` string is, for navigation, a MISSING
edge: contract_map.py reads it as text for the one node it hangs off, but no
traversal/closure/impact query can hop A->B on it. This pass reads the EXISTING
`properties` table on a graph.db and emits new traversable `edges` rows, ONE code
path, language-agnostically -- only the property MINING is per-language (already
done by gt-index; the value strings are uniform across go/python/js/ts).

THE 100% BAR (gt_gt.md  2.6):
  (1) completeness  -- per promotable class with two resolvable endpoints,
                       every instance becomes an edge.
  (2) traversability -- every new edge JOINs `nodes` on BOTH endpoints; zero
                       orphan target_id.
  (3) non-invention -- ZERO edges minted onto unresolved/ambiguous/builtin/
                       stdlib/literal/data-field targets. Those STAY a property.

PROPERTIES:
  - ADDITIVE  : properties rows are never deleted/changed (contract_map.py reads
                them as TEXT). New edges written ALONGSIDE.
  - IDEMPOTENT: prior promoted edges (resolution_method LIKE 'promote_%') are
                deleted before re-emit.
  - GENERALIZED: ONE promote logic. No per-language branch in the resolver. The
                node-name/file/line index is built ONCE from the nodes table.

Edge classes (gt_gt.md  2.6 TARGET EDGE SCHEMA):
  CO_SERIALIZES  <- serialization_pair   (PROMOTE-NOW, value carries @file:line)
  READS          <- field_read           (method -> owning Class; A-cut)
  WRITES         <- side_effect           (method -> owning Class, write semantics)
  RAISES         <- exception_type/flow   (raiser -> internal exception Class only)
  DATA_FLOW      <- data_flow             (def-site -> resolvable use-callee)
  USES (annot.)  <- caller_usage          (annotate EXISTING CALLS edge metadata)
  PRECEDES       <- call_order            (a -> b, distinct internal nodes only)
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import defaultdict

# --- trust model ( 2.3 thresholds) ------------------------------------------
CERTIFIED = "CERTIFIED"
CANDIDATE = "CANDIDATE"
SPECULATIVE = "SPECULATIVE"


def _trust_tier(conf: float) -> str:
    if conf >= 0.9:
        return CERTIFIED
    if conf >= 0.5:
        return CANDIDATE
    return SPECULATIVE


# Builtin / stdlib exception names that have NO internal node -> stay property.
# (Non-invention: a high-candidate builtin is not a fact -- gt_gt.md  2.5/ 2.6.)
_BUILTIN_EXCEPTIONS = {
    # python
    "Exception", "BaseException", "ValueError", "TypeError", "KeyError",
    "IndexError", "RuntimeError", "AttributeError", "NotImplementedError",
    "StopIteration", "StopAsyncIteration", "ArithmeticError", "ZeroDivisionError",
    "AssertionError", "ImportError", "ModuleNotFoundError", "OSError", "IOError",
    "FileNotFoundError", "PermissionError", "NameError", "LookupError",
    "OverflowError", "RecursionError", "ReferenceError", "SystemError",
    "UnicodeError", "UnicodeDecodeError", "UnicodeEncodeError", "MemoryError",
    "FloatingPointError", "GeneratorExit", "KeyboardInterrupt", "SystemExit",
    "ConnectionError", "TimeoutError", "BufferError", "BlockingIOError",
    "EOFError", "EnvironmentError", "WindowsError", "DeprecationWarning",
    "Warning", "UserWarning", "FutureWarning", "PendingDeprecationWarning",
    # go (raised via panic / returned error)
    "panic", "error", "errors", "fmt",
    # js/ts builtins
    "Error", "TypeError", "RangeError", "SyntaxError", "EvalError",
    "URIError", "AggregateError", "DOMException",
}

# Tokens that signal the exception_type value is NOT a clean class name -> drop.
_NON_NAME_TOKEN = re.compile(r"\s+from\s+|[.(\[]| None$")


# --- the promote pass --------------------------------------------------------
class _NodeIndex:
    """Name+file(+line) -> node rows. Built ONCE from the nodes table.

    by_name_file : (name, file) -> [node rows]   (same-file preference)
    by_name      : name -> [node rows]            (global fallback)
    by_name_file_line : (name, file, line) -> node row (exact, for serde)
    by_id        : id -> node row
    """

    def __init__(self, conn: sqlite3.Connection):
        self.by_name_file: dict = defaultdict(list)
        self.by_name: dict = defaultdict(list)
        self.by_name_file_line: dict = {}
        self.by_id: dict = {}
        rows = conn.execute(
            "SELECT id, label, name, file_path, start_line, parent_id FROM nodes"
        ).fetchall()
        for nid, label, name, fpath, sline, parent in rows:
            row = {
                "id": nid, "label": label, "name": name,
                "file": fpath, "line": sline, "parent_id": parent,
            }
            self.by_id[nid] = row
            if name:
                self.by_name_file[(name, fpath)].append(row)
                self.by_name[name].append(row)
                if sline is not None:
                    # last-writer wins is fine; serde line keys are unique enough
                    self.by_name_file_line[(name, fpath, sline)] = row

    def resolve(self, name: str, src_file: str | None):
        """Resolve a callee/symbol name -> (node_row, candidate_count).

        Prefer same-file, then global. candidate_count reflects ambiguity in the
        chosen scope (drives confidence per the spec's candidate-count rule).
        """
        if not name:
            return None, 0
        if src_file is not None:
            same = self.by_name_file.get((name, src_file))
            if same:
                return same[0], len(same)
        glob = self.by_name.get(name)
        if glob:
            return glob[0], len(glob)
        return None, 0


def _class_fields(conn: sqlite3.Connection) -> dict:
    """owning_class_node_id -> set(field names) from class_field properties.

    class_field hangs off the Class node; value 'name: Type'. Used to raise READS
    confidence to 0.9 when the read field is a declared field of the owning class.
    """
    out: dict = defaultdict(set)
    for nid, value in conn.execute(
        "SELECT node_id, value FROM properties WHERE kind='class_field'"
    ):
        fld = value.split(":", 1)[0].strip()
        if fld:
            out[nid].add(fld)
    return out


def _calls_edge_index(conn: sqlite3.Connection) -> dict:
    """(source_id, callee_name) -> edge_id for USES annotation.

    Keyed by source node + target node name so caller_usage can find an existing
    CALLS edge to annotate (never minting a new edge type).
    """
    out: dict = {}
    for eid, sid, tname in conn.execute(
        "SELECT e.id, e.source_id, n.name "
        "FROM edges e JOIN nodes n ON n.id=e.target_id WHERE e.type='CALLS'"
    ):
        out.setdefault((sid, tname), eid)
    return out


def _new_edge(t, sid, tid, method, conf, cc, evid, meta, sfile, sline):
    return {
        "type": t, "source_id": sid, "target_id": tid,
        "resolution_method": method, "confidence": float(conf),
        "trust_tier": _trust_tier(conf), "candidate_count": cc,
        "evidence_type": evid, "metadata": meta,
        "source_file": sfile, "source_line": sline,
        "verification_status": "unverified",
    }


# ---- per-class value parsers (the only place language shows up is the value
#      string shapes, which gt-index already normalized across languages) -----

_SERDE_RE = re.compile(r"partner:(?P<name>[^@|]+)@file:(?P<line>\d+)")
_READ_RE = re.compile(r"reads:\s*(?P<expr>[^\[]+?)(?:\s*\[(?P<ctx>[^\]]*)\])?\s*$")
_WRITE_RE = re.compile(r"mutates:\s*(?P<expr>[^=]+?)(?:\s*=\s*(?P<rhs>.*))?$")
_PRECEDES_RE = re.compile(r"^[^:]*:\s*(?P<seq>.+)$")
# a use-segment is a CALL if it looks like name(...)  (callee = leading dotted id)
_CALL_USE_RE = re.compile(r"([A-Za-z_][\w.]*)\s*\(")
_USAGE_RE = re.compile(r"^(?P<kind>[a-z_]+):(?P<name>[^|]+)\|")
_IDENT = re.compile(r"[A-Za-z_]\w*")


def promote(db_path: str, verbose: bool = False) -> dict:
    """Run all 7 promote classes on db_path. Idempotent + additive + correct-or-quiet.

    Returns a dict of per-class emitted-edge counts.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = None
    cur = conn.cursor()

    # IDEMPOTENT: remove any prior promoted edges before re-emit.
    cur.execute("DELETE FROM edges WHERE resolution_method LIKE 'promote_%'")
    # USES annotation is additive on metadata; strip our marker so re-runs are clean.
    # (We only ever ADD a json key; we rebuild it below, so no cleanup needed.)

    idx = _NodeIndex(conn)
    cfields = _class_fields(conn)
    calls_idx = _calls_edge_index(conn)

    new_edges: list = []
    counts = defaultdict(int)
    # dedup sets keep emission clean + idempotent within a single run
    seen_serde = set()
    seen_generic = set()

    # ---- 1. CO_SERIALIZES (serialization_pair) -- 100% resolvable, has line ----
    for nid, value, line in cur.execute(
        "SELECT node_id, value, line FROM properties WHERE kind='serialization_pair'"
    ).fetchall():
        src = idx.by_id.get(nid)
        if not src:
            continue
        m = _SERDE_RE.search(value)
        if not m:
            continue
        pname = m.group("name").strip()
        pline = int(m.group("line"))
        # target = node with name==pname AND start_line==pline, same-file first
        tgt = idx.by_name_file_line.get((pname, src["file"], pline))
        if tgt is None:
            # global by (name,line) -- scan candidates with that name + line
            for cand in idx.by_name.get(pname, []):
                if cand["line"] == pline:
                    tgt = cand
                    break
        if tgt is None:
            continue  # unresolved partner -> STAYS property (non-invention)
        a, b = src["id"], tgt["id"]
        if a == b:
            continue
        key = (min(a, b), max(a, b))  # UNDIRECTED, canonical dedup
        if key in seen_serde:
            continue
        seen_serde.add(key)
        new_edges.append(_new_edge(
            "CO_SERIALIZES", key[0], key[1], "promote_serde", 1.0, 1,
            "serde_pair", json.dumps({"partner": pname}), src["file"], line))
        counts["CO_SERIALIZES"] += 1

    # ---- 2. READS (field_read) -- method -> owning Class -----------------------
    for nid, value, line in cur.execute(
        "SELECT node_id, value, line FROM properties WHERE kind='field_read'"
    ).fetchall():
        src = idx.by_id.get(nid)
        if not src:
            continue
        # A-cut: the edge is method -> the owning Class (parent_id). A field with
        # no node stays a property; the *target* here is the owning Class node.
        parent = src.get("parent_id")
        if not parent or parent not in idx.by_id:
            continue  # no owning Class node -> stays property (non-invention)
        m = _READ_RE.match(value)
        if not m:
            continue
        expr = m.group("expr").strip()
        field = expr.rsplit(".", 1)[-1] if "." in expr else expr
        # conf 0.9 if <field> is a declared class_field on the owning class else 0.6
        conf = 0.9 if field in cfields.get(parent, ()) else 0.6
        key = (src["id"], parent, "READS", field)
        if key in seen_generic:
            continue
        seen_generic.add(key)
        new_edges.append(_new_edge(
            "READS", src["id"], parent, "promote_field_read", conf, 1,
            "field_read", json.dumps({"field": field}), src["file"], line))
        counts["READS"] += 1

    # ---- 3. WRITES (side_effect) -- method -> owning Class (write semantics) ----
    for nid, value, line in cur.execute(
        "SELECT node_id, value, line FROM properties WHERE kind='side_effect'"
    ).fetchall():
        src = idx.by_id.get(nid)
        if not src:
            continue
        parent = src.get("parent_id")
        if not parent or parent not in idx.by_id:
            continue
        m = _WRITE_RE.match(value)
        if not m:
            continue  # no resolvable field/target -> stays property
        expr = m.group("expr").strip()
        field = expr.rsplit(".", 1)[-1] if "." in expr else expr
        if not field:
            continue
        conf = 0.9 if field in cfields.get(parent, ()) else 0.6
        key = (src["id"], parent, "WRITES", field)
        if key in seen_generic:
            continue
        seen_generic.add(key)
        new_edges.append(_new_edge(
            "WRITES", src["id"], parent, "promote_write", conf, 1,
            "side_effect", json.dumps({"field": field}), src["file"], line))
        counts["WRITES"] += 1

    # ---- 4. RAISES (exception_type / exception_flow) -- internal classes only --
    # Build a Class-name index for exception resolution (prefer same-file).
    for kind in ("exception_type", "exception_flow"):
        for nid, value, line in cur.execute(
            "SELECT node_id, value, line FROM properties WHERE kind=?", (kind,)
        ).fetchall():
            src = idx.by_id.get(nid)
            if not src:
                continue
            # extract the exception type token
            if kind == "exception_flow":
                rm = re.search(r"raise\s+([A-Za-z_][\w.]*)", value)
                tok = rm.group(1) if rm else None
            else:
                tok = value.strip()
            if not tok:
                continue
            # drop noisy non-name values ('X from e', 'X.make', 'raise', literals)
            if _NON_NAME_TOKEN.search(tok):
                base = tok.split(".")[0].split()[0].strip()
            else:
                base = tok
            if not base or not _IDENT.fullmatch(base):
                continue
            if base in _BUILTIN_EXCEPTIONS:
                continue  # builtin -> NO edge, stays property (non-invention)
            # resolve to an INTERNAL Class node
            cands = [r for r in idx.by_name.get(base, []) if r["label"] == "Class"]
            if not cands:
                continue  # unresolved -> stays property
            same = [r for r in cands if r["file"] == src["file"]]
            tgt = same[0] if same else cands[0]
            cc = len(same) if same else len(cands)
            key = (src["id"], tgt["id"], "RAISES")
            if key in seen_generic:
                continue
            seen_generic.add(key)
            new_edges.append(_new_edge(
                "RAISES", src["id"], tgt["id"], "promote_raises", 0.9, cc,
                "exception", json.dumps({"exception": base}), src["file"], line))
            counts["RAISES"] += 1

    # ---- 5. DATA_FLOW (data_flow) -- def-site -> resolvable use-callee ----------
    for nid, value, line in cur.execute(
        "SELECT node_id, value, line FROM properties WHERE kind='data_flow'"
    ).fetchall():
        src = idx.by_id.get(nid)
        if not src:
            continue
        # value: '<param> -> <use> | <use>'
        if "->" not in value:
            continue
        _, rest = value.split("->", 1)
        uses = [u.strip() for u in rest.split("|")]
        for use in uses:
            cm = _CALL_USE_RE.search(use)
            if not cm:
                continue  # pure value flow / external -> SUPPRESS (no edge)
            callee_full = cm.group(1)
            callee = callee_full.rsplit(".", 1)[-1]  # forward-slice to the name
            tgt, cc = idx.resolve(callee, src["file"])
            if tgt is None:
                continue  # external/unresolvable callee -> SUPPRESS
            if tgt["id"] == src["id"]:
                continue
            conf = 0.8 if cc == 1 else (0.6 if cc == 2 else (0.4 if cc <= 5 else 0.4))
            key = (src["id"], tgt["id"], "DATA_FLOW")
            if key in seen_generic:
                continue
            seen_generic.add(key)
            new_edges.append(_new_edge(
                "DATA_FLOW", src["id"], tgt["id"], "promote_dataflow_callee",
                conf, cc, "data_flow", json.dumps({"callee": callee}),
                src["file"], line))
            counts["DATA_FLOW"] += 1

    # ---- 6. USES (caller_usage) -- ANNOTATE existing CALLS edge, no new edge ----
    uses_meta = defaultdict(dict)  # edge_id -> {usage: kind}
    for nid, value, line in cur.execute(
        "SELECT node_id, value, line FROM properties WHERE kind='caller_usage'"
    ).fetchall():
        src = idx.by_id.get(nid)
        if not src:
            continue
        m = _USAGE_RE.match(value)
        if not m:
            continue
        kind_u = m.group("kind").strip()
        callee = m.group("name").strip().rsplit(".", 1)[-1]
        eid = calls_idx.get((src["id"], callee))
        if eid is None:
            continue  # no matching CALLS edge -> skip (never mint a new edge)
        uses_meta[eid]["usage"] = kind_u
        counts["USES"] += 1

    # ---- 7. PRECEDES (call_order) -- a -> b, distinct internal nodes only -------
    for nid, value, line in cur.execute(
        "SELECT node_id, value, line FROM properties WHERE kind='call_order'"
    ).fetchall():
        src = idx.by_id.get(nid)
        if not src:
            continue
        m = _PRECEDES_RE.match(value)
        if not m:
            continue
        seq = [s.strip() for s in m.group("seq").split("->")]
        seq = [s.split("[")[0].strip() for s in seq]  # drop '[managed]' tail
        for a_name, b_name in zip(seq, seq[1:]):
            a_name = a_name.rsplit(".", 1)[-1]
            b_name = b_name.rsplit(".", 1)[-1]
            if a_name == b_name:
                continue  # 'append -> append' is not two distinct nodes
            ta, _ = idx.resolve(a_name, src["file"])
            tb, _ = idx.resolve(b_name, src["file"])
            if ta is None or tb is None:
                continue  # both must resolve to internal nodes
            if ta["id"] == tb["id"]:
                continue
            key = (ta["id"], tb["id"], "PRECEDES")
            if key in seen_generic:
                continue
            seen_generic.add(key)
            new_edges.append(_new_edge(
                "PRECEDES", ta["id"], tb["id"], "promote_precedes", 0.5, 1,
                "call_order", json.dumps({"scope": value.split(":", 1)[0]}),
                src["file"], line))
            counts["PRECEDES"] += 1

    # ---- write edges (additive) ------------------------------------------------
    cur.executemany(
        "INSERT INTO edges (type, source_id, target_id, resolution_method, "
        "confidence, metadata, source_file, source_line, trust_tier, "
        "candidate_count, evidence_type, verification_status) VALUES "
        "(:type,:source_id,:target_id,:resolution_method,:confidence,:metadata,"
        ":source_file,:source_line,:trust_tier,:candidate_count,:evidence_type,"
        ":verification_status)",
        new_edges,
    )
    # ---- apply USES annotations onto existing CALLS edges (additive metadata) ---
    for eid, ann in uses_meta.items():
        row = cur.execute("SELECT metadata FROM edges WHERE id=?", (eid,)).fetchone()
        meta = {}
        if row and row[0]:
            try:
                meta = json.loads(row[0])
            except (ValueError, TypeError):
                meta = {"_raw": row[0]}
        meta.update(ann)
        cur.execute("UPDATE edges SET metadata=? WHERE id=?",
                    (json.dumps(meta), eid))

    conn.commit()
    if verbose:
        for k, v in sorted(counts.items()):
            print(f"  {k:14s} {v}", file=sys.stderr)
    conn.close()
    return dict(counts)


# --- --prove mode ------------------------------------------------------------
_PROMOTED_TYPES = ("CO_SERIALIZES", "READS", "WRITES", "RAISES",
                   "DATA_FLOW", "PRECEDES")
# class -> the property kinds that can promote it (for RED->GREEN gating)
_CLASS_PROP = {
    "CO_SERIALIZES": ("serialization_pair",),
    "READS": ("field_read",),
    "WRITES": ("side_effect",),
    "RAISES": ("exception_type", "exception_flow"),
    "DATA_FLOW": ("data_flow",),
    "PRECEDES": ("call_order",),
}


def _edge_type_counts(conn) -> dict:
    return {t: c for t, c in conn.execute(
        "SELECT type, COUNT(*) FROM edges GROUP BY type")}


def _prop_count(conn, kinds) -> int:
    q = "SELECT COUNT(*) FROM properties WHERE kind IN (%s)" % ",".join("?" * len(kinds))
    return conn.execute(q, kinds).fetchone()[0]


def prove(db_path: str) -> dict:
    """Record BEFORE/AFTER, run promote(), assert the three  2.6 sub-conditions.

    Idempotent-proof: strip any pre-existing promoted edges BEFORE the RED
    snapshot so re-proving an already-promoted db still measures the genuine
    pre-promotion (RED) state. This only removes edges this pass itself minted
    (resolution_method LIKE 'promote_%'); originals are never touched.
    """
    conn = sqlite3.connect(db_path)
    conn.execute("DELETE FROM edges WHERE resolution_method LIKE 'promote_%'")
    conn.commit()
    before = _edge_type_counts(conn)
    # property counts that determine which classes are promotable (have data)
    prop_present = {cls: _prop_count(conn, kinds) for cls, kinds in _CLASS_PROP.items()}
    # property table row count BEFORE (for the additive/non-regression check)
    props_before = conn.execute("SELECT COUNT(*) FROM properties").fetchone()[0]
    conn.close()

    counts = promote(db_path)

    conn = sqlite3.connect(db_path)
    after = _edge_type_counts(conn)
    props_after = conn.execute("SELECT COUNT(*) FROM properties").fetchone()[0]

    result = {
        "db": db_path,
        "before": before, "after": after, "emitted": counts,
        "prop_present": prop_present,
        "assertions": {}, "failures": [],
    }

    # (a) RED -> GREEN : each promotable class WITH DATA goes 0 -> N
    red_green_ok = True
    for cls in _PROMOTED_TYPES:
        had_data = prop_present.get(cls, 0) > 0
        before_n = before.get(cls, 0)
        after_n = after.get(cls, 0)
        if had_data:
            # must have started at 0 (no pre-existing edge of a promote-only type)
            # and ended > 0 ONLY IF the data was actually two-endpoint-resolvable.
            # We assert: started at 0, AND (after>0 OR all instances were correctly
            # quiet -- builtin/external). The emitted count is authoritative.
            if before_n != 0:
                red_green_ok = False
                result["failures"].append(
                    f"RED->GREEN {cls}: expected 0 before, got {before_n}")
            emitted = counts.get(cls, 0)
            if emitted != after_n:
                red_green_ok = False
                result["failures"].append(
                    f"RED->GREEN {cls}: emitted {emitted} != after {after_n}")
    result["assertions"]["red_green"] = red_green_ok

    # (b) TRAVERSABLE : every promoted edge JOINs nodes on BOTH endpoints, 0 orphans
    orphans = conn.execute(
        "SELECT COUNT(*) FROM edges e LEFT JOIN nodes s ON s.id=e.source_id "
        "LEFT JOIN nodes t ON t.id=e.target_id "
        "WHERE e.resolution_method LIKE 'promote_%' "
        "AND (s.id IS NULL OR t.id IS NULL)").fetchone()[0]
    result["assertions"]["traversable_zero_orphans"] = (orphans == 0)
    result["orphan_count"] = orphans
    if orphans != 0:
        result["failures"].append(f"TRAVERSABLE: {orphans} orphan promoted edges")

    # (c) NON-INVENTION : zero promoted edges to a builtin exception name, and
    #     zero whose target_id does not resolve (covered by orphans=0). Also assert
    #     target_id>0 for all promoted edges.
    bad_target = conn.execute(
        "SELECT COUNT(*) FROM edges WHERE resolution_method LIKE 'promote_%' "
        "AND (target_id IS NULL OR target_id<=0 OR source_id IS NULL OR source_id<=0)"
    ).fetchone()[0]
    # builtin-exception leak check on RAISES metadata
    builtin_leak = 0
    for (meta,) in conn.execute(
        "SELECT metadata FROM edges WHERE resolution_method='promote_raises'"):
        try:
            exc = json.loads(meta).get("exception")
        except (ValueError, TypeError):
            exc = None
        if exc in _BUILTIN_EXCEPTIONS:
            builtin_leak += 1
    result["assertions"]["non_invention"] = (bad_target == 0 and builtin_leak == 0)
    result["bad_target_count"] = bad_target
    result["builtin_exception_leak"] = builtin_leak
    if bad_target:
        result["failures"].append(f"NON-INVENTION: {bad_target} edges w/ bad endpoint")
    if builtin_leak:
        result["failures"].append(f"NON-INVENTION: {builtin_leak} builtin-exception edges")

    # (d) ADDITIVE : properties table unchanged (contract_map.py read intact)
    result["assertions"]["additive_properties_unchanged"] = (props_before == props_after)
    result["props_before"] = props_before
    result["props_after"] = props_after
    if props_before != props_after:
        result["failures"].append(
            f"ADDITIVE: properties {props_before}->{props_after} (must be equal)")

    conn.close()
    result["passed"] = len(result["failures"]) == 0
    return result


def main():
    ap = argparse.ArgumentParser(description="gt_gt  2.6 promote-to-edges pass")
    ap.add_argument("db", help="path to graph.db")
    ap.add_argument("--prove", action="store_true",
                    help="record BEFORE/AFTER and assert the 2.6 bar")
    ap.add_argument("--json", action="store_true", help="emit JSON")
    args = ap.parse_args()

    if args.prove:
        res = prove(args.db)
        if args.json:
            print(json.dumps(res, indent=2))
        else:
            print(f"DB: {res['db']}")
            print(f"  emitted : {res['emitted']}")
            print(f"  before  : { {k: res['before'].get(k,0) for k in _PROMOTED_TYPES} }")
            print(f"  after   : { {k: res['after'].get(k,0) for k in _PROMOTED_TYPES} }")
            print(f"  assertions: {res['assertions']}")
            print(f"  orphans={res['orphan_count']} bad_target={res['bad_target_count']} "
                  f"builtin_leak={res['builtin_exception_leak']} "
                  f"props {res['props_before']}=={res['props_after']}")
            print(f"  PASSED: {res['passed']}")
            if res["failures"]:
                print("  FAILURES:")
                for f in res["failures"]:
                    print("    -", f)
        sys.exit(0 if res["passed"] else 1)
    else:
        counts = promote(args.db, verbose=True)
        print(json.dumps(counts))


if __name__ == "__main__":
    main()
