"""Phase B: GT Offline Replay on holdout_v1 (60 bugs with graph.db).

Calls actual GT functions on real inputs (issue text, graph.db, repo path,
gold files) and classifies whether the evidence is useful for the real fix.

This is the PRIMARY phase — it directly answers: what does current GT handle?
"""

import json
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

HOLDOUT_PATH = Path(__file__).resolve().parents[2] / "holdout_v1.jsonl"
OUTPUT_PATH = Path(__file__).resolve().parents[2] / "research" / "corpus_audit" / "gt_replay_results.jsonl"


def get_gold_functions(db_path: str, gold_file: str) -> list[dict]:
    """Get function/method nodes from graph.db that are in the gold file."""
    if not os.path.exists(db_path):
        return []
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        norm = gold_file.replace("\\", "/").lstrip("/")
        rows = conn.execute(
            "SELECT id, name, label, signature, return_type, start_line, end_line "
            "FROM nodes WHERE file_path LIKE ? AND label IN ('Function', 'Method') "
            "ORDER BY start_line",
            (f"%{norm}",),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def replay_one_bug(bug: dict) -> list[dict]:
    """Run GT functions on one bug and classify evidence."""
    from groundtruth.pretask.anchors import extract_issue_anchors
    from groundtruth.hooks.post_edit import (
        _get_callers_from_graph,
        _get_signature_from_graph,
        _get_siblings_from_graph,
        _get_test_assertions_from_file,
        generate_improved_evidence,
    )

    bug_id = bug["bug_id"]
    repo = bug["repo"]
    issue_text = (bug.get("issue_title", "") or "") + "\n" + (bug.get("issue_body", "") or "")
    db_path = bug["graph_db_path"]
    repo_root = bug["repo_path"]
    gold_files = bug.get("gold_files", [])

    results = []

    # 1. Extract issue anchors
    try:
        anchors = extract_issue_anchors(issue_text, db_path)
        anchor_info = {
            "symbols": sorted(anchors.symbols)[:20],
            "paths": sorted(anchors.paths)[:10],
            "test_names": sorted(anchors.test_names)[:5],
            "symbol_count": len(anchors.symbols),
        }
    except Exception as e:
        anchor_info = {"error": str(e), "symbol_count": 0}

    for gold_file in gold_files:
        funcs = get_gold_functions(db_path, gold_file)
        if not funcs:
            results.append({
                "bug_id": bug_id, "repo": repo, "gold_file": gold_file,
                "function": "(no functions in graph)", "anchors": anchor_info,
                "callers": [], "signature": None, "siblings": [],
                "test_assertions": [], "full_evidence": "",
                "g7_silence": False, "evidence_class": "NOT_IN_GRAPH",
            })
            continue

        for func in funcs[:5]:
            func_name = func["name"]
            func_sig = func.get("signature", "")

            # 2. Get callers
            try:
                callers = _get_callers_from_graph(
                    db_path, gold_file, func_name, repo_root,
                    seen_files=[], limit=10,
                )
                caller_summary = [
                    {"file": c["file"], "name": c.get("caller_name", ""), "conf": c.get("confidence", "?")}
                    for c in callers[:5]
                ]
            except Exception as e:
                callers = []
                caller_summary = [{"error": str(e)}]

            # 3. Get signature
            try:
                sig = _get_signature_from_graph(db_path, gold_file, func_name)
            except Exception:
                sig = None

            # 4. Get siblings
            try:
                siblings = _get_siblings_from_graph(db_path, gold_file, func_name, repo_root)
                sibling_summary = [
                    {"name": s["name"], "has_snippet": bool(s.get("snippet")), "has_sig": bool(s.get("signature"))}
                    for s in (siblings or [])[:5]
                ]
            except Exception as e:
                siblings = []
                sibling_summary = [{"error": str(e)}]

            # 5. Get test assertions
            try:
                assertions = _get_test_assertions_from_file(
                    db_path, gold_file, func_name, repo_root,
                )
            except Exception as e:
                assertions = []

            # 6. Generate full improved evidence
            try:
                full_evidence = generate_improved_evidence(
                    file_path=gold_file,
                    function_names=[func_name],
                    db_path=db_path,
                    repo_root=repo_root,
                )
            except Exception as e:
                full_evidence = f"ERROR: {e}"

            # 7. Detect G7 silence
            g7_silence = (len(callers) == 0 and not (siblings or []) and full_evidence == "")

            # 8. Classify evidence markers present
            markers_found = []
            for marker in ["[SIGNATURE]", "[PATTERN]", "[BEHAVIORAL CONTRACT]", "[CONTRACT]",
                           "[TEST]", "[PEER]", "[TWINS]", "[PROPAGATE]", "[SCOPE]"]:
                if marker in full_evidence:
                    markers_found.append(marker)

            # 9. Check anchor match
            anchor_hit_file = any(
                s.lower() in gold_file.lower()
                for s in anchor_info.get("symbols", [])
            ) or any(
                p.lower() in gold_file.lower()
                for p in anchor_info.get("paths", [])
            )
            anchor_hit_func = any(
                s.lower() == func_name.lower()
                for s in anchor_info.get("symbols", [])
            )

            results.append({
                "bug_id": bug_id,
                "repo": repo,
                "gold_file": gold_file,
                "function": func_name,
                "function_signature": func_sig or "",
                "anchors": anchor_info,
                "anchor_hit_file": anchor_hit_file,
                "anchor_hit_func": anchor_hit_func,
                "caller_count": len(callers),
                "callers": caller_summary,
                "signature": sig,
                "sibling_count": len(siblings) if siblings else 0,
                "siblings": sibling_summary,
                "test_assertion_count": len(assertions),
                "test_assertions": assertions[:3],
                "full_evidence_len": len(full_evidence),
                "full_evidence_excerpt": full_evidence[:500] if full_evidence else "",
                "markers_found": markers_found,
                "g7_silence": g7_silence,
                "evidence_class": _classify_evidence(
                    callers, sig, siblings or [], assertions, full_evidence, g7_silence
                ),
            })

    return results


def _classify_evidence(callers, sig, siblings, assertions, full_evidence, g7_silence) -> str:
    """Classify overall evidence quality for this function."""
    if g7_silence:
        return "G7_SILENCED"
    if not full_evidence or full_evidence.startswith("ERROR"):
        return "NO_EVIDENCE"

    has_callers = len(callers) > 0
    has_sig = bool(sig)
    has_siblings = bool(siblings)
    has_tests = bool(assertions)

    if has_callers and has_sig and (has_siblings or has_tests):
        return "RICH"
    elif has_callers or (has_sig and has_tests):
        return "MODERATE"
    elif has_sig:
        return "SIGNATURE_ONLY"
    else:
        return "MINIMAL"


def main():
    bugs = [json.loads(line) for line in open(HOLDOUT_PATH, encoding="utf-8")]

    print(f"GT Offline Replay: {len(bugs)} bugs from holdout_v1")
    print(f"Output: {OUTPUT_PATH}")
    print()

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    all_results = []
    for i, bug in enumerate(bugs):
        print(f"[{i+1}/{len(bugs)}] {bug['bug_id']} ({bug['repo']})...", end="", flush=True)
        try:
            results = replay_one_bug(bug)
            all_results.extend(results)
            evidence_classes = [r["evidence_class"] for r in results]
            print(f" {len(results)} funcs, classes={evidence_classes}")
        except Exception as e:
            print(f" ERROR: {e}")

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        for r in all_results:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")

    # Summary
    from collections import Counter
    total = len(all_results)
    class_dist = Counter(r["evidence_class"] for r in all_results)
    anchor_file_hits = sum(1 for r in all_results if r.get("anchor_hit_file"))
    anchor_func_hits = sum(1 for r in all_results if r.get("anchor_hit_func"))
    has_callers = sum(1 for r in all_results if r["caller_count"] > 0)
    has_sig = sum(1 for r in all_results if r["signature"])
    has_tests = sum(1 for r in all_results if r["test_assertion_count"] > 0)
    has_siblings = sum(1 for r in all_results if r["sibling_count"] > 0)

    print(f"\n{'='*60}")
    print(f"REPLAY SUMMARY: {total} functions across {len(bugs)} bugs")
    print(f"{'='*60}")
    print(f"\nEvidence class distribution:")
    for cls, n in class_dist.most_common():
        print(f"  {cls:<20}: {n:>4}/{total} ({n/total:.0%})")
    print(f"\nComponent coverage:")
    print(f"  Callers (>0):         {has_callers}/{total} ({has_callers/total:.0%})")
    print(f"  Signature:            {has_sig}/{total} ({has_sig/total:.0%})")
    print(f"  Siblings (>0):        {has_siblings}/{total} ({has_siblings/total:.0%})")
    print(f"  Test assertions (>0): {has_tests}/{total} ({has_tests/total:.0%})")
    print(f"\nAnchor coverage:")
    print(f"  Anchor -> gold file:  {anchor_file_hits}/{total} ({anchor_file_hits/total:.0%})")
    print(f"  Anchor -> gold func:  {anchor_func_hits}/{total} ({anchor_func_hits/total:.0%})")
    print(f"\nSaved to: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
