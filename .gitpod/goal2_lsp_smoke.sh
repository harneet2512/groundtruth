#!/usr/bin/env bash
set -euo pipefail

# Goal 2 LSP Smoke: for each of 5 languages, run gt-index THEN resolve.py --resolve,
# compare edge counts before/after, and verify LSP enrichment (signature/return_type).

GT_INDEX="${GT_INDEX:-/tmp/gt-index-clean}"
WORK="/tmp/gt_5lang"
REPO_ROOT="/workspaces/groundtruth"
export PYTHONPATH="$REPO_ROOT/src:${PYTHONPATH:-}"

declare -A REPOS
REPOS[python]="/tmp/gt_5lang/beets"
REPOS[go]="/tmp/gt_5lang/crossplane"
REPOS[typescript]="/tmp/gt_5lang/hono"
REPOS[rust]="/tmp/gt_5lang/axum"
REPOS[javascript]="/tmp/gt_5lang/marimo"

declare -A LANGS
LANGS[python]="python"
LANGS[go]="go"
LANGS[typescript]="typescript"
LANGS[rust]="rust"
LANGS[javascript]="javascript"

echo "================================================================"
echo "=== GOAL 2: LSP Enrichment Smoke Test — 5 Languages ==="
echo "================================================================"
echo ""
echo "LSP servers:"
for srv in pyright-langserver gopls typescript-language-server rust-analyzer; do
    path=$(which $srv 2>/dev/null || echo "MISSING")
    echo "  $srv: $path"
done
echo ""

for LANG in python go typescript rust javascript; do
    DIR="${REPOS[$LANG]}"
    LANG_ID="${LANGS[$LANG]}"
    DB_BEFORE="$WORK/${LANG}_before_lsp.db"
    DB_AFTER="$WORK/${LANG}_after_lsp.db"

    echo ""
    echo "================================================================"
    echo "=== $LANG ($LANG_ID) ==="
    echo "================================================================"

    if [ ! -d "$DIR/.git" ]; then
        echo "--- SKIP: repo not cloned at $DIR"
        continue
    fi

    # Skip JS for now — marimo is 31K nodes, LSP would take too long
    if [ "$LANG" = "javascript" ]; then
        echo "--- SKIP: marimo too large for LSP smoke (31K nodes)"
        continue
    fi

    # Step 1: Index with gt-index (tree-sitter only)
    echo "--- Step 1: gt-index (tree-sitter only)..."
    rm -f "$DB_BEFORE" "$DB_AFTER"
    "$GT_INDEX" -root="$DIR" -output="$DB_BEFORE" 2>&1 | tail -1

    # Capture before stats
    BEFORE_IMPORT=$(python3 -c "
import sqlite3
c = sqlite3.connect('$DB_BEFORE')
print(c.execute(\"SELECT COUNT(*) FROM edges WHERE resolution_method='import'\").fetchone()[0])
")
    BEFORE_NM=$(python3 -c "
import sqlite3
c = sqlite3.connect('$DB_BEFORE')
print(c.execute(\"SELECT COUNT(*) FROM edges WHERE resolution_method='name_match'\").fetchone()[0])
")
    BEFORE_NM_LOW=$(python3 -c "
import sqlite3
c = sqlite3.connect('$DB_BEFORE')
print(c.execute(\"SELECT COUNT(*) FROM edges WHERE resolution_method='name_match' AND confidence < 0.5\").fetchone()[0])
")
    BEFORE_SIG=$(python3 -c "
import sqlite3
c = sqlite3.connect('$DB_BEFORE')
print(c.execute(\"SELECT COUNT(*) FROM nodes WHERE signature IS NOT NULL AND signature != ''\").fetchone()[0])
")
    BEFORE_RT=$(python3 -c "
import sqlite3
c = sqlite3.connect('$DB_BEFORE')
print(c.execute(\"SELECT COUNT(*) FROM nodes WHERE return_type IS NOT NULL AND return_type != ''\").fetchone()[0])
")
    BEFORE_TOTAL=$(python3 -c "
import sqlite3
c = sqlite3.connect('$DB_BEFORE')
print(c.execute('SELECT COUNT(*) FROM edges').fetchone()[0])
")
    BEFORE_USABLE=$(python3 -c "
import sqlite3
c = sqlite3.connect('$DB_BEFORE')
print(c.execute('SELECT COUNT(*) FROM edges WHERE confidence >= 0.5').fetchone()[0])
")
    echo "  BEFORE LSP:"
    echo "    edges: $BEFORE_TOTAL (usable: $BEFORE_USABLE)"
    echo "    import: $BEFORE_IMPORT  name_match: $BEFORE_NM (low: $BEFORE_NM_LOW)"
    echo "    signatures: $BEFORE_SIG  return_types: $BEFORE_RT"

    # Step 2: Copy DB for LSP enrichment
    cp "$DB_BEFORE" "$DB_AFTER"

    # Step 3: Run resolve.py --resolve on the copy
    echo "--- Step 2: resolve.py --resolve (LSP enrichment)..."
    RESOLVE_START=$(date +%s)
    python3 -m groundtruth.resolve \
        --db "$DB_AFTER" \
        --root "$DIR" \
        --resolve \
        --lang "$LANG_ID" \
        --confidence 0.7 \
        --timeout 120 \
        2>&1 | tail -10
    RESOLVE_END=$(date +%s)
    RESOLVE_TIME=$((RESOLVE_END - RESOLVE_START))

    # Capture after stats
    AFTER_IMPORT=$(python3 -c "
import sqlite3
c = sqlite3.connect('$DB_AFTER')
print(c.execute(\"SELECT COUNT(*) FROM edges WHERE resolution_method='import'\").fetchone()[0])
")
    AFTER_NM=$(python3 -c "
import sqlite3
c = sqlite3.connect('$DB_AFTER')
print(c.execute(\"SELECT COUNT(*) FROM edges WHERE resolution_method='name_match'\").fetchone()[0])
")
    AFTER_NM_LOW=$(python3 -c "
import sqlite3
c = sqlite3.connect('$DB_AFTER')
print(c.execute(\"SELECT COUNT(*) FROM edges WHERE resolution_method='name_match' AND confidence < 0.5\").fetchone()[0])
")
    AFTER_SIG=$(python3 -c "
import sqlite3
c = sqlite3.connect('$DB_AFTER')
print(c.execute(\"SELECT COUNT(*) FROM nodes WHERE signature IS NOT NULL AND signature != ''\").fetchone()[0])
")
    AFTER_RT=$(python3 -c "
import sqlite3
c = sqlite3.connect('$DB_AFTER')
print(c.execute(\"SELECT COUNT(*) FROM nodes WHERE return_type IS NOT NULL AND return_type != ''\").fetchone()[0])
")
    AFTER_TOTAL=$(python3 -c "
import sqlite3
c = sqlite3.connect('$DB_AFTER')
print(c.execute('SELECT COUNT(*) FROM edges').fetchone()[0])
")
    AFTER_USABLE=$(python3 -c "
import sqlite3
c = sqlite3.connect('$DB_AFTER')
print(c.execute('SELECT COUNT(*) FROM edges WHERE confidence >= 0.5').fetchone()[0])
")

    # Compute deltas
    D_IMPORT=$((AFTER_IMPORT - BEFORE_IMPORT))
    D_NM=$((AFTER_NM - BEFORE_NM))
    D_NM_LOW=$((AFTER_NM_LOW - BEFORE_NM_LOW))
    D_SIG=$((AFTER_SIG - BEFORE_SIG))
    D_RT=$((AFTER_RT - BEFORE_RT))
    D_USABLE=$((AFTER_USABLE - BEFORE_USABLE))

    echo ""
    echo "  AFTER LSP (${RESOLVE_TIME}s):"
    echo "    edges: $AFTER_TOTAL (usable: $AFTER_USABLE)"
    echo "    import: $AFTER_IMPORT  name_match: $AFTER_NM (low: $AFTER_NM_LOW)"
    echo "    signatures: $AFTER_SIG  return_types: $AFTER_RT"
    echo ""
    echo "  DELTA:"
    echo "    import:      $BEFORE_IMPORT -> $AFTER_IMPORT ($D_IMPORT)"
    echo "    name_match:  $BEFORE_NM -> $AFTER_NM ($D_NM)"
    echo "    nm_low:      $BEFORE_NM_LOW -> $AFTER_NM_LOW ($D_NM_LOW)"
    echo "    signatures:  $BEFORE_SIG -> $AFTER_SIG (+$D_SIG)"
    echo "    return_type: $BEFORE_RT -> $AFTER_RT (+$D_RT)"
    echo "    usable:      $BEFORE_USABLE -> $AFTER_USABLE (+$D_USABLE)"

    # Pass/fail criteria
    echo ""
    echo "  VERDICT:"
    if [ "$D_USABLE" -gt 0 ] || [ "$D_SIG" -gt 0 ] || [ "$D_RT" -gt 0 ]; then
        echo "    LSP ENRICHMENT: WORKING ✓"
    else
        echo "    LSP ENRICHMENT: NO EFFECT ✗"
    fi
    if [ "$AFTER_SIG" -gt 0 ]; then
        echo "    SIGNATURES: PRESENT ($AFTER_SIG) ✓"
    else
        echo "    SIGNATURES: MISSING ✗"
    fi
    if [ "$AFTER_RT" -gt 0 ]; then
        echo "    RETURN TYPES: PRESENT ($AFTER_RT) ✓"
    else
        echo "    RETURN TYPES: MISSING ✗"
    fi

    # Sample enriched data
    echo ""
    echo "  SAMPLE (top 5 enriched nodes):"
    python3 -c "
import sqlite3
c = sqlite3.connect('$DB_AFTER')
rows = c.execute('''
    SELECT name, file_path, signature, return_type
    FROM nodes
    WHERE (signature IS NOT NULL AND signature != '')
       OR (return_type IS NOT NULL AND return_type != '')
    ORDER BY (SELECT COUNT(*) FROM edges WHERE target_id = nodes.id) DESC
    LIMIT 5
''').fetchall()
for r in rows:
    sig = (r[2] or '')[:80]
    rt = r[3] or ''
    print(f'    {r[0]:30s} sig={sig}  rt={rt}')
c.close()
" 2>/dev/null || echo "    (query failed)"

done

echo ""
echo "================================================================"
echo "=== GOAL 2 SMOKE COMPLETE ==="
echo "================================================================"
