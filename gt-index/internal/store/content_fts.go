package store

import (
	"bufio"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"unicode"
	"unicode/utf8"
)

// content_fts.go — B1: the CONTENT surface for behavior-described (stratum-B)
// localization. Every other localizer surface (the anchor cross-check, nodes_fts,
// the embedder) indexes nodes.name (+ signature) — so an issue that names a BEHAVIOR
// ("Redis TLS handshake fails") with no code symbol has nothing to match. The domain
// vocabulary for that behavior lives in the function BODY (identifiers, string
// literals, comment words), which no surface indexes. symbol_content_fts is a
// whole-repo FTS5 index over per-symbol body content so a content-BM25 leg can
// RETRIEVE the file a name/symbol query would miss — an INDEPENDENT surface, not a
// fourth view of nodes.name.
//
// Isolation: this is a standalone, additive module. It touches NO existing table and
// NO parse/resolve path — nothing else changes behavior. FTS5-gated: if the binary was
// built without the sqlite_fts5 tag the CREATE fails and we degrade quietly (the
// Python leg contributes 0), exactly like nodes_fts.

// _identRe matches identifier-like runs (also captures the identifier words inside
// string literals and comments, which is what we want for a content surface).
// S8 (Fable): Unicode-aware — the old ASCII class `[A-Za-z_][A-Za-z0-9_]*` silently DROPPED
// every non-Latin identifier/word (Cyrillic, Greek, accented Latin — café/naïve/Björn, CJK),
// so the content surface was blind to the domain vocabulary of non-English repos worldwide.
// `\p{L}` = any Unicode letter, `\p{N}` = any Unicode number. First char is a letter/underscore
// (leading digits excluded, matching the original identifier intent); the rest may include
// Unicode digits.
var _identRe = regexp.MustCompile(`[\p{L}_][\p{L}\p{N}_]*`)

// _contentBodyCharCap bounds the body slice tokenized per symbol so a pathological
// huge function can never blow up index time / db size. Generous — real function
// bodies are far smaller.
const _contentBodyCharCap = 20000

// _contentFTSTokenizer is the ONE source of truth for the symbol_content_fts tokenizer spec.
// EnsureContentFTS creates with it + self-heals on mismatch; RepopulateContentFTSForFile warns
// loudly when an existing table's tokenizer is stale (A-Finding1). Kept in one place so the two
// call sites can never drift.
const _contentFTSTokenizer = `tokenize="unicode61 tokenchars '_'"`

// splitCamel splits a PascalCase/camelCase run into its word parts, inserting a
// boundary at a lower→Upper transition and at an Upper-run→Upper+lower transition
// (so "HTTPServer" → ["HTTP","Server"], "getUserID" → ["get","User","ID"]).
func splitCamel(s string) []string {
	if s == "" {
		return nil
	}
	var out []string
	runes := []rune(s)
	start := 0
	for i := 1; i < len(runes); i++ {
		prev, cur := runes[i-1], runes[i]
		var next rune
		if i+1 < len(runes) {
			next = runes[i+1]
		}
		// S8 (Fable): Unicode-aware case boundaries (was ASCII 'a'..'z'/'A'..'Z' — blind to
		// accented/Cyrillic/Greek camelCase identifiers). unicode.IsLower/IsUpper handle any
		// script; IsLower(0)/IsUpper(0) are false so the last-char `next==0` case is safe.
		lowerToUpper := unicode.IsLower(prev) && unicode.IsUpper(cur)
		acronymEnd := unicode.IsUpper(prev) && unicode.IsUpper(cur) && unicode.IsLower(next)
		// S8 digit split: break at a letter↔digit transition so "sha256sum" → sha,256,sum and
		// "utf8Encoder" → utf,8,encoder — a query for "sha"/"encoder" then matches a sub-token a
		// fused "sha256sum"/"utf8encoder" token would miss. Language-agnostic, Unicode-safe.
		letterDigit := (unicode.IsLetter(prev) && unicode.IsDigit(cur)) ||
			(unicode.IsDigit(prev) && unicode.IsLetter(cur))
		if lowerToUpper || acronymEnd || letterDigit {
			out = append(out, string(runes[start:i]))
			start = i
		}
	}
	out = append(out, string(runes[start:]))
	return out
}

// contentTokens turns a body slice into the space-joined token bag stored in the FTS
// index: every identifier lowercased AND its camelCase/snake_case sub-tokens (BLUiR
// sub-token splitting, so "get_user_by_id" and "getUserById" both match "user"). Token
// FREQUENCY is preserved (no dedup) so FTS5 BM25 sees the real term weights. Tokens of
// length 1 are dropped (noise). Language-agnostic: operates on raw text, no AST.
func contentTokens(body string) string {
	if len(body) > _contentBodyCharCap {
		// A-Finding5 (Fable LIPI): walk the byte cap back to a UTF-8 rune boundary so a multibyte
		// (CJK / accented-Latin) identifier straddling _contentBodyCharCap is not sliced mid-rune
		// (which _identRe would drop as a non-match — losing the boundary token). ≤3 bytes back.
		cut := _contentBodyCharCap
		for cut > 0 && !utf8.RuneStart(body[cut]) {
			cut--
		}
		body = body[:cut]
	}
	var toks []string
	for _, ident := range _identRe.FindAllString(body, -1) {
		if low := strings.ToLower(ident); len(low) >= 2 {
			toks = append(toks, low)
		}
		for _, part := range strings.Split(ident, "_") {
			for _, sub := range splitCamel(part) {
				if ls := strings.ToLower(sub); len(ls) >= 2 && ls != strings.ToLower(ident) {
					toks = append(toks, ls)
				}
			}
		}
	}
	return strings.Join(toks, " ")
}

// EnsureContentFTS creates the symbol_content_fts virtual table if it does not exist.
// FTS5-gated and non-fatal: if the sqlite_fts5 build tag is absent the CREATE errors
// and we log + return nil (the localizer's content leg then finds no table and scores
// 0 — graceful degradation, same contract as nodes_fts).
func (d *DB) EnsureContentFTS() error {
	const wantTokenizer = _contentFTSTokenizer
	var storedSQL string
	if err := d.db.QueryRow(
		"SELECT sql FROM sqlite_master WHERE type='table' AND name='symbol_content_fts'",
	).Scan(&storedSQL); err == nil {
		// Table exists. Keep it ONLY if its tokenizer matches the current one (Fable
		// finding 4): a table created by an earlier tokenizer (pre-tokenchars) would
		// silently tokenize differently, drifting BM25 ranking between fresh and reused
		// graph.dbs. On mismatch, DROP so we recreate below with current semantics; the
		// caller's PopulateContentFTS then refills it.
		if strings.Contains(storedSQL, wantTokenizer) {
			return nil
		}
		if _, derr := d.db.Exec("DROP TABLE IF EXISTS symbol_content_fts"); derr != nil {
			log.Printf("[WARN] symbol_content_fts stale-tokenizer DROP failed: %v — keeping old table", derr)
			return nil
		}
	}
	// tokenchars '_' keeps the underscore INSIDE tokens (Fable #8 TF-asymmetry): by
	// default unicode61 treats '_' as a separator, so it re-splits a snake_case ident
	// ("get_user" -> get, user) at index time — on top of the explicit sub-tokens
	// contentTokens already emits, double-counting snake_case sub-tokens vs camelCase
	// and biasing BM25 toward Python/Rust vocab. Keeping '_' as a token char makes
	// "get_user" ONE token (parity with camelCase's fused "getuser"), so the only
	// sub-tokens are the explicit ones contentTokens emits once — symmetric across langs.
	if _, err := d.db.Exec(
		`CREATE VIRTUAL TABLE IF NOT EXISTS symbol_content_fts USING fts5(content, tokenize="unicode61 tokenchars '_'")`,
	); err != nil {
		log.Printf("[WARN] symbol_content_fts unavailable (FTS5 not compiled in?): %v — content leg disabled", err)
		return nil
	}
	return nil
}

// readFileLines reads a whole source file into a 1-based-addressable line slice ONCE
// (LIPI BUG 3: the old per-symbol bodySlice re-opened + rescanned-from-line-1 on every
// function → O(symbols × fileLen) on a file with many functions). Callers read the file
// once when the file changes, then slice each symbol from memory via sliceLines. Returns
// nil on any read error (correct-or-quiet). Peak memory = the largest single file (one
// file held at a time), not the repo.
func readFileLines(absPath string) []string {
	f, err := os.Open(absPath)
	if err != nil {
		return nil
	}
	defer f.Close()
	var lines []string
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 0, 64*1024), 4*1024*1024)
	for sc.Scan() {
		lines = append(lines, sc.Text())
	}
	if err := sc.Err(); err != nil {
		// Fable finding 7: a single line over the 4 MB buffer (minified/generated JS)
		// stops the scan mid-file. Never silent (CLAUDE.md): log which file truncated so
		// the missing-body-content for symbols past that line is visible. Return the
		// lines read so far — symbols before the oversized line still index correctly.
		log.Printf("[WARN] readFileLines: %s truncated mid-file (%v) — symbols after the oversized line get no body content", absPath, err)
	}
	return lines
}

// sliceLines returns lines [startLine, endLine] (1-based, inclusive) of an already-read
// file, joined with '\n' and bounded by _contentBodyCharCap. Correct-or-quiet on any
// out-of-range input (a missing/short body just yields no content for that symbol).
func sliceLines(lines []string, startLine, endLine int) string {
	if startLine <= 0 || endLine < startLine || startLine > len(lines) {
		return ""
	}
	if endLine > len(lines) {
		endLine = len(lines)
	}
	var b strings.Builder
	for i := startLine - 1; i < endLine; i++ {
		b.WriteString(lines[i])
		b.WriteByte('\n')
		if b.Len() > _contentBodyCharCap {
			break
		}
	}
	return b.String()
}

// PopulateContentFTS fills symbol_content_fts for EVERY Function/Method node, reading
// each node's body from disk once per file (nodes grouped by file → one open per file).
// Idempotent: clears the table first. Deterministic: nodes are processed in id order.
// root is the repo root the (relative) node.file_path values are resolved against.
func (d *DB) PopulateContentFTS(root string) error {
	var n int
	if err := d.db.QueryRow(
		"SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='symbol_content_fts'",
	).Scan(&n); err != nil || n == 0 {
		return nil // table absent (FTS5 off) — nothing to populate
	}
	if _, err := d.db.Exec("DELETE FROM symbol_content_fts"); err != nil {
		return fmt.Errorf("clear symbol_content_fts: %w", err)
	}
	// file_path ordered so all of a file's symbols are contiguous (one open per file);
	// id as the secondary key keeps the insert order deterministic.
	// is_test = 0: NEVER index test bodies. Test assertion text + FAIL_TO_PASS names
	// are the strongest lexical echo of an issue — indexing them would let the content
	// leg rank a test symbol top and brush the leak invariant. Tests are also never the
	// edit target, so excluding them is both leak-safe AND correct for localization.
	rows, err := d.db.Query(
		`SELECT id, file_path, COALESCE(start_line,0), COALESCE(end_line,0)
		   FROM nodes
		  WHERE label IN ('Function','Method') AND COALESCE(is_test,0) = 0
		  ORDER BY file_path, id`,
	)
	if err != nil {
		return fmt.Errorf("scan nodes for content fts: %w", err)
	}
	type row struct {
		id                 int64
		file               string
		startLine, endLine int
	}
	var pending []row
	for rows.Next() {
		var r row
		if err := rows.Scan(&r.id, &r.file, &r.startLine, &r.endLine); err != nil {
			rows.Close()
			return fmt.Errorf("scan content-fts node: %w", err)
		}
		pending = append(pending, r)
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return err
	}

	tx, err := d.db.Begin()
	if err != nil {
		return fmt.Errorf("begin content-fts populate: %w", err)
	}
	stmt, err := tx.Prepare("INSERT INTO symbol_content_fts(rowid, content) VALUES (?, ?)")
	if err != nil {
		tx.Rollback()
		return fmt.Errorf("prepare content-fts insert: %w", err)
	}
	curFile := ""
	var curLines []string
	for _, r := range pending {
		if r.file != curFile {
			curFile = r.file
			curLines = readFileLines(filepath.Join(root, filepath.FromSlash(r.file)))
		}
		content := contentTokens(sliceLines(curLines, r.startLine, r.endLine))
		if content == "" {
			continue // no body content — leave this symbol out (never an empty row)
		}
		if _, err := stmt.Exec(r.id, content); err != nil {
			stmt.Close()
			tx.Rollback()
			return fmt.Errorf("insert content-fts row for node %d: %w", r.id, err)
		}
	}
	stmt.Close()
	if err := tx.Commit(); err != nil {
		return fmt.Errorf("commit content-fts populate: %w", err)
	}
	return nil
}

// RepopulateContentFTSForFile refreshes only the reindexed file's symbols on the
// incremental (-file) path: delete this file's node rows from the content index, then
// re-insert from its current Function/Method nodes. Keeps symbol_content_fts current
// after a `gt-index -file` without a whole-repo repopulate.
func (d *DB) RepopulateContentFTSForFile(root, relpath string) error {
	var n int
	if err := d.db.QueryRow(
		"SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='symbol_content_fts'",
	).Scan(&n); err != nil || n == 0 {
		return nil
	}
	// A-Finding1 (Fable LIPI): the incremental (-file) path does NOT run EnsureContentFTS, so its
	// stale-tokenizer self-heal is skipped. We must NOT call EnsureContentFTS here (it DROPs on a
	// mismatch → the whole repo's content rows vanish, since we only re-populate THIS file). A
	// tokenizer change (e.g. the S8 Unicode upgrade) cannot be applied to one file without
	// re-tokenizing the whole corpus (BM25 IDF consistency), so the correct-or-quiet response is a
	// LOUD warning that a full rebuild is needed — never a silent mixed-tokenizer corpus.
	var storedSQL string
	if err := d.db.QueryRow(
		"SELECT sql FROM sqlite_master WHERE type='table' AND name='symbol_content_fts'",
	).Scan(&storedSQL); err == nil && !strings.Contains(storedSQL, _contentFTSTokenizer) {
		log.Printf("[WARN] symbol_content_fts tokenizer is STALE on the incremental (-file) path "+
			"(want %q) — this file is refreshed under the OLD tokenizer for corpus consistency; run "+
			"a FULL gt-index rebuild to apply the current tokenizer everywhere", _contentFTSTokenizer)
	}
	rel := filepath.ToSlash(relpath)
	rows, err := d.db.Query(
		`SELECT id, COALESCE(start_line,0), COALESCE(end_line,0)
		   FROM nodes WHERE file_path = ? AND label IN ('Function','Method')
		     AND COALESCE(is_test,0) = 0 ORDER BY id`,
		rel,
	)
	if err != nil {
		return fmt.Errorf("scan file nodes for content fts: %w", err)
	}
	type row struct {
		id                 int64
		startLine, endLine int
	}
	var pending []row
	for rows.Next() {
		var r row
		if err := rows.Scan(&r.id, &r.startLine, &r.endLine); err != nil {
			rows.Close()
			return err
		}
		pending = append(pending, r)
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return err
	}

	tx, err := d.db.Begin()
	if err != nil {
		return err
	}
	// ORPHAN SWEEP (LIPI BUG 1): by the time this runs the reindex tx has COMMITTED —
	// the file's OLD nodes were deleted and its NEW nodes inserted with FRESH ids
	// (nodes.id is AUTOINCREMENT → ids are never reused). So the file's stale content
	// rows are keyed by ids no longer in `nodes`; a file-scoped delete-by-current-id
	// would match NOTHING and the stale rows would accumulate every reindex (unbounded
	// space leak AND bm25() IDF drift — the corpus statistics skew with reindex history,
	// making the content leg's ranking non-deterministic and degrading). Sweep EVERY
	// content row whose rowid is no longer a live node: removes this file's stale rows
	// plus any left by a prior reindex or a whole-file deletion, so the corpus stays ==
	// the live Function/Method nodes. Deterministic + self-healing.
	if _, err := tx.Exec(
		`DELETE FROM symbol_content_fts WHERE rowid NOT IN (SELECT id FROM nodes)`,
	); err != nil {
		tx.Rollback()
		return fmt.Errorf("sweep orphan content rows: %w", err)
	}
	abs := filepath.Join(root, filepath.FromSlash(rel))
	lines := readFileLines(abs) // ONE open for the file's whole symbol set (LIPI BUG 3)
	for _, r := range pending {
		content := contentTokens(sliceLines(lines, r.startLine, r.endLine))
		if content == "" {
			continue
		}
		if _, err := tx.Exec("INSERT INTO symbol_content_fts(rowid, content) VALUES (?, ?)", r.id, content); err != nil {
			tx.Rollback()
			return fmt.Errorf("insert content row for node %d: %w", r.id, err)
		}
	}
	if err := tx.Commit(); err != nil {
		return fmt.Errorf("commit content-fts file repopulate: %w", err)
	}
	return nil
}
