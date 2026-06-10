package resolver

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/store"
	"github.com/harneet2512/groundtruth/gt-index/internal/walker"
)

// ----------------------------------------------------------------------------
// #B1: CHA IMPLEMENTS — name-only matching at 0.95/CERTIFIED replaced by
// name+arity+result-presence at demoted confidence. RED before, GREEN after.
// ----------------------------------------------------------------------------

// chaFixture builds a temp repo + graph.db with:
//   - iface.go:  Closer  (1-method interface: Close() error)
//   - iface.go:  ReadWriter (2-method interface: Read/Write with results)
//   - bad.go:    BadFile struct  + Close()            (arity ok, NO results → must NOT match Closer)
//   - good.go:   GoodFile struct + Close() error      (matches Closer → 0.6 CANDIDATE)
//   - rw.go:     Buf struct + Read/Write (matching)   (matches ReadWriter → 0.85 CANDIDATE)
func chaFixture(t *testing.T) (*store.DB, []walker.SourceFile, string) {
	t.Helper()
	root := t.TempDir()

	src := map[string]string{
		"iface.go": `package x

type Closer interface {
	Close() error
}

type ReadWriter interface {
	Read(p []byte) (int, error)
	Write(p []byte) (int, error)
}
`,
		"bad.go": `package x

type BadFile struct{}

func (b *BadFile) Close() {}
`,
		"good.go": `package x

type GoodFile struct{}

func (g *GoodFile) Close() error { return nil }
`,
		"rw.go": `package x

type Buf struct{}

func (b *Buf) Read(p []byte) (int, error)  { return 0, nil }
func (b *Buf) Write(p []byte) (int, error) { return 0, nil }
`,
	}
	var files []walker.SourceFile
	for name, content := range src {
		p := filepath.Join(root, name)
		if err := os.WriteFile(p, []byte(content), 0o644); err != nil {
			t.Fatal(err)
		}
		files = append(files, walker.SourceFile{Path: name, AbsPath: p, Language: "go"})
	}

	db, err := store.Open(filepath.Join(root, "graph.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })

	// Nodes mirroring what the parser + linkGoReceiverMethods would produce.
	// ids: 1 Closer, 2 ReadWriter (interfaces); 3 BadFile, 4 GoodFile, 5 Buf
	// (structs); 6-9 methods parented to their structs, with stored signatures.
	execSQL(t, db, `INSERT INTO nodes (id, label, name, file_path, start_line, signature, language, parent_id) VALUES
		(1, 'Interface', 'Closer',     'iface.go', 3,  'type Closer interface {',                          'go', NULL),
		(2, 'Interface', 'ReadWriter', 'iface.go', 7,  'type ReadWriter interface {',                      'go', NULL),
		(3, 'Class',     'BadFile',    'bad.go',   3,  'type BadFile struct{}',                            'go', NULL),
		(4, 'Class',     'GoodFile',   'good.go',  3,  'type GoodFile struct{}',                           'go', NULL),
		(5, 'Class',     'Buf',        'rw.go',    3,  'type Buf struct{}',                                'go', NULL),
		(6, 'Method',    'Close',      'bad.go',   5,  'func (b *BadFile) Close() {}',                     'go', 3),
		(7, 'Method',    'Close',      'good.go',  5,  'func (g *GoodFile) Close() error { return nil }',  'go', 4),
		(8, 'Method',    'Read',       'rw.go',    5,  'func (b *Buf) Read(p []byte) (int, error)  { return 0, nil }', 'go', 5),
		(9, 'Method',    'Write',      'rw.go',    6,  'func (b *Buf) Write(p []byte) (int, error) { return 0, nil }', 'go', 5)`)
	return db, files, root
}

// execSQL runs a raw statement through the store's exported tx API.
func execSQL(t *testing.T, db *store.DB, stmt string) {
	t.Helper()
	tx, err := db.BeginTx()
	if err != nil {
		t.Fatal(err)
	}
	if _, err := tx.Exec(stmt); err != nil {
		tx.Rollback()
		t.Fatal(err)
	}
	if err := tx.Commit(); err != nil {
		t.Fatal(err)
	}
}

func queryImplements(t *testing.T, db *store.DB) map[[2]int64]store.Edge {
	t.Helper()
	tx, err := db.BeginTx()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	rows, err := tx.Query(`SELECT source_id, target_id, COALESCE(resolution_method,''),
	        COALESCE(confidence,0), COALESCE(trust_tier,''), COALESCE(evidence_type,''),
	        COALESCE(verification_status,''), COALESCE(source_file,'')
	   FROM edges WHERE type = 'IMPLEMENTS'`)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	out := make(map[[2]int64]store.Edge)
	for rows.Next() {
		var e store.Edge
		if err := rows.Scan(&e.SourceID, &e.TargetID, &e.ResolutionMethod, &e.Confidence,
			&e.TrustTier, &e.EvidenceType, &e.VerificationStatus, &e.SourceFile); err != nil {
			t.Fatal(err)
		}
		out[[2]int64{e.SourceID, e.TargetID}] = e
	}
	return out
}

func TestGoImplements_ArityAndResultPresence(t *testing.T) {
	db, files, root := chaFixture(t)

	n, err := ResolveRelationships(db, files, root)
	if err != nil {
		t.Fatal(err)
	}
	if n == 0 {
		t.Fatal("no relationship edges emitted")
	}
	impls := queryImplements(t, db)

	// #B1a RED→GREEN: BadFile.Close() (no results) must NOT implement
	// Closer (Close() error). Name-only matching emitted this at 0.95.
	if e, ok := impls[[2]int64{3, 1}]; ok {
		t.Errorf("BadFile (Close() without results) matched Closer (Close() error): conf %.2f — arity/result check missing", e.Confidence)
	}

	// GoodFile.Close() error DOES implement Closer — but Closer is a 1-method
	// interface (ambiguous by construction) → 0.6 / CANDIDATE (#B1b), with the
	// arity evidence stamp and (#B2) a non-empty tier/verification_status.
	e, ok := impls[[2]int64{4, 1}]
	if !ok {
		t.Fatal("GoodFile (Close() error) did not match Closer — over-suppression")
	}
	if e.Confidence != 0.6 {
		t.Errorf("1-method interface conf = %.2f, want 0.6 (ambiguous by construction)", e.Confidence)
	}
	if e.TrustTier != "CANDIDATE" {
		t.Errorf("1-method interface tier = %q, want CANDIDATE", e.TrustTier)
	}
	if e.EvidenceType != "structural_method_set_arity" {
		t.Errorf("evidence_type = %q, want structural_method_set_arity", e.EvidenceType)
	}
	if e.VerificationStatus != "unverified" {
		t.Errorf("verification_status = %q, want unverified (#B2: empty bind defeated the SQL default)", e.VerificationStatus)
	}
	// #B1e: the edge must anchor on the STRUCT's file so a -file reindex of
	// good.go deletes it (orphan-edge invariant) — not the interface's file.
	if e.SourceFile != "good.go" {
		t.Errorf("source_file = %q, want good.go (the struct's file)", e.SourceFile)
	}

	// Buf matches the 2-method ReadWriter with full name+arity+results →
	// 0.85 / CANDIDATE (#B1b: not 0.95 — arity is still not type equality).
	e2, ok := impls[[2]int64{5, 2}]
	if !ok {
		t.Fatal("Buf (Read/Write matching) did not match ReadWriter")
	}
	if e2.Confidence != 0.85 {
		t.Errorf("2-method interface conf = %.2f, want 0.85", e2.Confidence)
	}
	if e2.TrustTier != "CANDIDATE" {
		t.Errorf("2-method interface tier = %q, want CANDIDATE (0.85 < 0.9)", e2.TrustTier)
	}

	// BadFile must not match ReadWriter either (no Read/Write at all).
	if _, ok := impls[[2]int64{3, 2}]; ok {
		t.Error("BadFile matched ReadWriter without the methods")
	}
}

// #B1c: an interface whose embed does NOT resolve to a project-local interface
// has an UNKNOWN required set — the matcher must abstain entirely, not match
// on the partial set.
func TestGoImplements_UnresolvedEmbedAbstains(t *testing.T) {
	root := t.TempDir()
	src := `package x

type Walker interface {
	io.Reader
	Walk() error
}
`
	p := filepath.Join(root, "iface.go")
	if err := os.WriteFile(p, []byte(src), 0o644); err != nil {
		t.Fatal(err)
	}
	files := []walker.SourceFile{{Path: "iface.go", AbsPath: p, Language: "go"}}

	db, err := store.Open(filepath.Join(root, "graph.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()

	// A struct that satisfies the PARTIAL set (Walk() error) but cannot be
	// proven to satisfy io.Reader.
	execSQL(t, db, `INSERT INTO nodes (id, label, name, file_path, start_line, signature, language, parent_id) VALUES
		(1, 'Interface', 'Walker', 'iface.go', 3, 'type Walker interface {',                       'go', NULL),
		(2, 'Class',     'Dir',    'dir.go',   1, 'type Dir struct{}',                             'go', NULL),
		(3, 'Method',    'Walk',   'dir.go',   3, 'func (d *Dir) Walk() error { return nil }',     'go', 2)`)

	if _, err := ResolveRelationships(db, files, root); err != nil {
		t.Fatal(err)
	}
	impls := queryImplements(t, db)
	if e, ok := impls[[2]int64{2, 1}]; ok {
		t.Errorf("interface with unresolved embed (io.Reader) matched on its PARTIAL set: conf %.2f — under-approximation must abstain", e.Confidence)
	}
}

// Signature-fingerprint parsers: the pure-string layer #B1 rests on.
func TestParseGoMethodSig(t *testing.T) {
	cases := []struct {
		in         string
		name       string
		arity      int
		hasResults bool
		ok         bool
	}{
		{"Close() error", "Close", 0, true, true},
		{"Close()", "Close", 0, false, true},
		{"Read(p []byte) (int, error)", "Read", 1, true, true},
		{"Apply(f func(int) error) error", "Apply", 1, true, true},
		{"Get(k Pair[A, B]) V", "Get", 1, true, true},
		{"Sum(a, b int, c string)", "Sum", 3, false, true},
		{"Walk() error // walks", "Walk", 0, true, true},
		{"Do() {", "Do", 0, false, true},
		// Unparseable forms must abstain.
		{"~int | ~string", "", 0, false, false},
		{"Multi(a int,", "", 0, false, false}, // params span lines
		{"", "", 0, false, false},
	}
	for _, tc := range cases {
		got, ok := parseGoMethodSig(tc.in)
		if ok != tc.ok {
			t.Errorf("parseGoMethodSig(%q) ok = %v, want %v", tc.in, ok, tc.ok)
			continue
		}
		if !ok {
			continue
		}
		if got.Name != tc.name || got.Arity != tc.arity || got.HasResults != tc.hasResults {
			t.Errorf("parseGoMethodSig(%q) = %+v, want name=%q arity=%d results=%v",
				tc.in, got, tc.name, tc.arity, tc.hasResults)
		}
	}
}

func TestParseGoStructMethodSig(t *testing.T) {
	cases := []struct {
		in         string
		name       string
		arity      int
		hasResults bool
		ok         bool
	}{
		{"func (b *BadFile) Close() {}", "Close", 0, false, true},
		{"func (g *GoodFile) Close() error { return nil }", "Close", 0, true, true},
		{"func (b *Buf) Read(p []byte) (int, error) { return 0, nil }", "Read", 1, true, true},
		{"func (s *Service[K, func() error]) Do()", "Do", 0, false, true},
		{"", "", 0, false, false},
		{"type Foo struct{}", "", 0, false, false},
	}
	for _, tc := range cases {
		got, ok := parseGoStructMethodSig(tc.in)
		if ok != tc.ok {
			t.Errorf("parseGoStructMethodSig(%q) ok = %v, want %v", tc.in, ok, tc.ok)
			continue
		}
		if !ok {
			continue
		}
		if got.Name != tc.name || got.Arity != tc.arity || got.HasResults != tc.hasResults {
			t.Errorf("parseGoStructMethodSig(%q) = %+v, want name=%q arity=%d results=%v",
				tc.in, got, tc.name, tc.arity, tc.hasResults)
		}
	}
}
