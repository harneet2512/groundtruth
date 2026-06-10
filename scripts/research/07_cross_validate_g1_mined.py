"""G1 Cross-Validation: Anchor signal on mined GitHub PRs.

Tests whether issue text identifiers overlap with gold files at rates
consistent with holdout_v1 findings (73.3% hit rate, 35.7x random).

Key difference from 01_measure_anchor_signal.py:
- No graph.db available -> raw identifier extraction only (no graph cross-check)
- Gold files from PR diffs (not hand-curated)
- Text from issues or PR body (not always issue text)
- Repos are massive (10K-30K files) vs holdout (200-4300 files)

This is a HARDER test: no graph filter means more noise in anchors.
If signal still holds, the finding generalizes.
"""

import json
import re
from pathlib import Path
from collections import Counter

MINED_PATH = Path(__file__).resolve().parents[2] / "research" / "big_repo_routing" / "mined_prs.jsonl"


def extract_identifiers(text):
    """Extract code-like identifiers from issue/PR text.

    Simplified version of anchors.py logic without graph cross-check.
    """
    if not text:
        return set(), set()

    STOPWORDS = {
        "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
        "have", "has", "had", "do", "does", "did", "will", "would", "could",
        "should", "may", "might", "shall", "can", "need", "must", "ought",
        "this", "that", "these", "those", "it", "its", "they", "them", "we",
        "you", "he", "she", "i", "me", "my", "your", "his", "her", "our",
        "their", "what", "which", "who", "whom", "when", "where", "why", "how",
        "not", "no", "nor", "but", "or", "and", "if", "then", "else", "for",
        "from", "to", "in", "on", "at", "by", "with", "of", "as", "into",
        "about", "after", "before", "between", "through", "during", "above",
        "below", "up", "down", "out", "off", "over", "under", "again", "further",
        "than", "too", "very", "just", "also", "still", "already", "even",
        "all", "each", "every", "both", "few", "more", "most", "other", "some",
        "such", "only", "own", "same", "so", "here", "there",
        "fix", "bug", "issue", "error", "problem", "crash", "fail", "broken",
        "wrong", "incorrect", "missing", "add", "remove", "change", "update",
        "new", "old", "current", "expected", "actual", "test", "tests",
        "please", "thanks", "note", "see", "like", "think", "want",
        "true", "false", "none", "null", "undefined", "nil", "void",
        "return", "returns", "type", "value", "string", "int", "bool",
        "function", "method", "class", "struct", "interface", "enum",
        "file", "files", "line", "code", "pr", "merge", "branch", "commit",
        "version", "release", "latest", "master", "main", "dev",
    }

    symbols = set()
    paths = set()

    # Extract file paths (foo/bar.go, src/utils.ts, etc.)
    path_pattern = r'(?:^|\s|`|\'|"|/|\()([\w./-]+\.(?:go|py|ts|tsx|js|jsx|rs|java|cpp|c|h|rb|swift|kt))\b'
    for match in re.finditer(path_pattern, text):
        p = match.group(1).strip("/")
        if "/" in p or "." in p:
            paths.add(p)

    # Extract backtick-quoted identifiers
    for match in re.finditer(r'`([^`]+)`', text):
        ident = match.group(1).strip()
        if len(ident) > 2 and len(ident) < 100 and not ident.startswith("http"):
            if "/" in ident and "." in ident:
                paths.add(ident)
            elif re.match(r'^[a-zA-Z_]\w*(?:\.\w+)*$', ident):
                if ident.lower() not in STOPWORDS:
                    symbols.add(ident)

    # Extract CamelCase identifiers (e.g., GatherAllocatedState)
    for match in re.finditer(r'\b([A-Z][a-z]+(?:[A-Z][a-z]+)+)\b', text):
        sym = match.group(1)
        if len(sym) > 3 and sym.lower() not in STOPWORDS:
            symbols.add(sym)

    # Extract snake_case identifiers (e.g., watch_tracker, resource_version)
    for match in re.finditer(r'\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b', text):
        sym = match.group(1)
        if len(sym) > 4 and sym not in STOPWORDS:
            symbols.add(sym)

    # Extract UPPER_SNAKE identifiers (e.g., USE_STATUS_RESOURCES)
    for match in re.finditer(r'\b([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)\b', text):
        sym = match.group(1)
        if len(sym) > 4 and sym.lower() not in STOPWORDS:
            symbols.add(sym)

    # Extract plain lowercase words >= 5 chars that look like code entities
    # (reflector, describe, checker, resolver, dispatcher, etc.)
    # These are common in issue text referring to components by name
    CODE_LIKE = re.compile(r'\b([a-z]{5,30})\b')
    PLAIN_STOPWORDS = STOPWORDS | {
        "about", "after", "again", "against", "along", "among", "around",
        "because", "before", "behind", "below", "beside", "between",
        "beyond", "could", "during", "every", "first", "found", "going",
        "group", "large", "later", "might", "never", "other", "place",
        "point", "right", "since", "small", "sometimes", "still", "taken",
        "there", "thing", "those", "three", "times", "today", "under",
        "until", "using", "where", "which", "while", "would", "write",
        "makes", "fixed", "fixes", "added", "patch", "added", "needs",
        "works", "break", "broke", "check", "cause", "cases", "clean",
        "clear", "close", "doing", "given", "hence", "looks", "means",
        "moved", "occur", "order", "panic", "parse", "seems", "shown",
        "since", "skips", "solve", "start", "steps", "store", "throw",
        "track", "tried", "valid", "apply", "based", "being", "bound",
        "build", "chain", "child", "count", "cover", "debug", "empty",
        "equal", "event", "exist", "extra", "field", "final", "force",
        "guard", "guest", "handy", "index", "input", "inner", "known",
        "layer", "level", "limit", "local", "logic", "lower", "match",
        "merge", "minor", "model", "mount", "named", "occurs", "older",
        "outer", "owner", "param", "patch", "phase", "print", "prior",
        "proxy", "query", "queue", "quick", "raise", "range", "refer",
        "reply", "reset", "retry", "route", "scope", "setup", "share",
        "shift", "short", "sleep", "space", "split", "stage", "stale",
        "state", "super", "table", "throw", "token", "total", "trial",
        "upper", "usage", "watch", "yield",
    }
    for match in CODE_LIKE.finditer(text):
        word = match.group(1)
        if word not in PLAIN_STOPWORDS:
            symbols.add(word)

    # Extract dotted paths (e.g., torch.nn.functional, os.path.join)
    for match in re.finditer(r'\b([a-zA-Z_]\w+(?:\.\w+){1,5})\b', text):
        sym = match.group(1)
        if len(sym) > 5:
            symbols.add(sym)

    return symbols, paths


def match_anchors_to_gold(symbols, paths, gold_files):
    """Check if any extracted identifiers match gold file paths or names."""
    hits = []

    for gf in gold_files:
        gf_lower = gf.lower()
        gf_basename = Path(gf).stem.lower()
        gf_parts = set(p.lower() for p in Path(gf).parts)

        matched = False
        match_type = None

        # Direct path match
        for p in paths:
            p_lower = p.lower()
            if p_lower in gf_lower or gf_lower.endswith(p_lower):
                matched = True
                match_type = "path_match"
                break

        if not matched:
            # Symbol matches basename or path component
            for sym in symbols:
                sym_lower = sym.lower()
                if sym_lower == gf_basename:
                    matched = True
                    match_type = "basename_match"
                    break
                if sym_lower in gf_parts:
                    matched = True
                    match_type = "component_match"
                    break
                # Partial match: symbol appears in path
                if len(sym_lower) >= 5 and sym_lower in gf_lower:
                    matched = True
                    match_type = "substring_match"
                    break

        hits.append({
            "gold_file": gf,
            "matched": matched,
            "match_type": match_type,
        })

    return hits


def main():
    records = []
    with open(MINED_PATH, encoding="utf-8") as f:
        for line in f:
            records.append(json.loads(line))

    print("=" * 70)
    print("G1 CROSS-VALIDATION: Anchor Signal on Mined GitHub PRs")
    print("=" * 70)
    print(f"Dataset: mined_prs.jsonl ({len(records)} PRs, {len(set(r['repo'] for r in records))} repos)")
    print(f"Comparison: holdout_v1 anchor_hit_rate = 73.3% (35.7x random)")
    print()

    results = []
    for rec in records:
        text = (rec.get("issue_title", "") or "") + "\n" + (rec.get("issue_body", "") or "")
        gold_files = rec.get("gold_files", [])

        if not gold_files:
            continue

        symbols, paths = extract_identifiers(text)
        hits = match_anchors_to_gold(symbols, paths, gold_files)

        any_hit = any(h["matched"] for h in hits)
        hit_count = sum(1 for h in hits if h["matched"])

        results.append({
            "repo": rec["repo"],
            "pr_number": rec["pr_number"],
            "gold_count": len(gold_files),
            "anchor_count": len(symbols) + len(paths),
            "symbol_count": len(symbols),
            "path_count": len(paths),
            "any_hit": any_hit,
            "hit_count": hit_count,
            "hit_rate": hit_count / len(gold_files),
            "text_source": rec.get("text_source", "unknown"),
            "hits": hits,
        })

    total = len(results)
    any_hit_count = sum(1 for r in results if r["any_hit"])
    overall_hit_rate = any_hit_count / total if total else 0

    print("OVERALL RESULTS")
    print("-" * 70)
    print(f"  PRs with >=1 gold file matching an anchor: {any_hit_count}/{total} ({overall_hit_rate:.1%})")
    print(f"  Holdout_v1 comparison:                     44/60 (73.3%)")
    print()

    # Per-repo breakdown
    print(f"{'Repo':<30} {'Hit Rate':>10} {'Anchors':>8} {'GoldHit':>8}")
    print("-" * 70)

    for repo in sorted(set(r["repo"] for r in results)):
        subset = [r for r in results if r["repo"] == repo]
        n = len(subset)
        hits = sum(1 for r in subset if r["any_hit"])
        avg_anchors = sum(r["anchor_count"] for r in subset) / n
        print(f"  {repo:<28} {hits}/{n} ({hits/n:.0%})  {avg_anchors:>6.1f}  {sum(r['hit_count'] for r in subset):>5}")

    # By text source
    print(f"\nBy text source:")
    for src in ["issue", "pr_body"]:
        subset = [r for r in results if r["text_source"] == src]
        if not subset:
            continue
        n = len(subset)
        hits = sum(1 for r in subset if r["any_hit"])
        print(f"  {src:<15}: {hits}/{n} ({hits/n:.0%})")

    # Match type distribution
    print(f"\nMatch type distribution:")
    type_counts = Counter()
    for r in results:
        for h in r["hits"]:
            if h["matched"]:
                type_counts[h["match_type"]] += 1
    for mt, count in type_counts.most_common():
        print(f"  {mt:<20}: {count}")

    # Random baseline estimate
    # Average repo has ~10K files. Average gold count = 1.8
    # Random hit rate = gold_count / total_files
    avg_gold = sum(r["gold_count"] for r in results) / total
    est_repo_sizes = {
        "kubernetes/kubernetes": 20000,
        "pytorch/pytorch": 15000,
        "microsoft/TypeScript": 3000,
        "rust-lang/rust": 30000,
        "golang/go": 10000,
    }

    print(f"\nRandom baseline estimate:")
    for repo in sorted(set(r["repo"] for r in results)):
        subset = [r for r in results if r["repo"] == repo]
        repo_size = est_repo_sizes.get(repo, 10000)
        n = len(subset)
        avg_g = sum(r["gold_count"] for r in subset) / n
        random_rate = avg_g / repo_size
        actual_rate = sum(1 for r in subset if r["any_hit"]) / n
        ratio = actual_rate / random_rate if random_rate > 0 else float("inf")
        print(f"  {repo:<30}: actual={actual_rate:.0%}, random={random_rate:.4%}, ratio={ratio:.1f}x")

    # Misses analysis
    misses = [r for r in results if not r["any_hit"]]
    print(f"\nMISS ANALYSIS ({len(misses)} PRs with no anchor match):")
    for r in misses[:15]:
        print(f"  PR #{r['pr_number']} ({r['repo']}): {r['anchor_count']} anchors, gold={r['gold_count']}")
        for h in r["hits"][:3]:
            print(f"    gold: {h['gold_file']}")

    # Validation verdict
    print(f"\n{'='*70}")
    print("VALIDATION VERDICT")
    print("=" * 70)

    if overall_hit_rate >= 0.60:
        print(f"  G1 CROSS-VALIDATED: {overall_hit_rate:.1%} hit rate on {total} mined PRs")
        print(f"  (holdout_v1 was 73.3% — signal generalizes across repos)")
    elif overall_hit_rate >= 0.40:
        print(f"  G1 PARTIALLY VALIDATED: {overall_hit_rate:.1%} on mined PRs (lower than 73.3% holdout)")
        print(f"  Signal present but weaker on massive repos / without graph filter")
    else:
        print(f"  G1 NOT VALIDATED: {overall_hit_rate:.1%} on mined PRs (holdout was 73.3%)")
        print(f"  Signal may not generalize to massive repos")


if __name__ == "__main__":
    main()
