package resolver

import (
	"encoding/json"
	"path/filepath"
	"strconv"
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/store"
)

// TestPromote_ReadsWritesAccessSites pins the statement-level access-site
// substrate the promote pass stamps onto deduped READS/WRITES edges
// (edges.access_sites). The edge itself stays SYMBOL-level — method -> owning
// class — with metadata byte-exact the bare field name (contract_map.py's
// `e.metadata = ?` field-exact match and the 0.7 confidence floor must keep
// working untouched); the per-site (field,line) footprint rides access_sites.
//
// Fixture: Box(10) owns mutate(11) and read_only(12). `count` is a declared
// class_field (conf 0.9), `size` is not (conf 0.6). mutate reads self.count (l6)
// and self.size (l7) — TWO different fields collapsing into ONE READS edge —
// and writes self.count twice (l8, l9) plus self.size once (l12) — THREE write
// rows collapsing into ONE WRITES edge. A free Function's field_read mints no
// edge and no sites (non-invention).
func TestPromote_ReadsWritesAccessSites(t *testing.T) {
	root := t.TempDir()
	db, err := store.Open(filepath.Join(root, "graph.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()

	execSQL(t, db, `INSERT INTO nodes (id,label,name,file_path,start_line,signature,language,parent_id) VALUES
	  (10,'Class',   'Box',      'box.py',    1,'',                   'python',0),
	  (11,'Method',  'mutate',   'box.py',    5,'def mutate(self)',   'python',10),
	  (12,'Method',  'read_only','box.py',   20,'def read_only(self)','python',10),
	  (15,'Function','helper',   'helpers.py',1,'def helper()',       'python',0)`)

	// Declared field: count (0.9 lift). size stays undeclared -> 0.6.
	execSQL(t, db, `INSERT INTO properties (node_id,kind,value,line,confidence) VALUES
	  (10,'class_field','count: int',2,1.0)`)

	// mutate(11): two DIFFERENT fields read -> ONE deduped READS edge.
	execSQL(t, db, `INSERT INTO properties (node_id,kind,value,line,confidence) VALUES
	  (11,'field_read','reads: self.count [in_condition]',6,0.9),
	  (11,'field_read','reads: self.size',7,0.9)`)
	// read_only(12): one undeclared field read -> ONE READS edge at 0.6.
	execSQL(t, db, `INSERT INTO properties (node_id,kind,value,line,confidence) VALUES
	  (12,'field_read','reads: self.size',21,0.9)`)
	// helper(15): a Function (parent 0) -> no owning class -> stays property.
	execSQL(t, db, `INSERT INTO properties (node_id,kind,value,line,confidence) VALUES
	  (15,'field_read','reads: x.field [in_return]',2,0.9)`)

	// mutate(11): three write rows (count twice, size once) -> ONE WRITES edge.
	execSQL(t, db, `INSERT INTO properties (node_id,kind,value,line,confidence) VALUES
	  (11,'side_effect','mutates: self.count = a',8,1.0),
	  (11,'side_effect','mutates: self.count = b',9,1.0),
	  (11,'side_effect','mutates: self.size = compute()',12,1.0)`)
	// helper(15): a non-field side_effect -> no field match -> stays property.
	execSQL(t, db, `INSERT INTO properties (node_id,kind,value,line,confidence) VALUES
	  (15,'side_effect','calls external subprocess.run',3,1.0)`)

	// A published stable identity for mutate(11) only, via the resolution_symbols
	// sidecar (native_id = nodes.id) — the same join process.go readStableIDs uses.
	execSQL(t, db, `INSERT INTO resolution_symbols
	  (stable_id,native_id,native_kind,normalized_kind,language,path,qualified_name,start_line,end_line,export_status)
	  VALUES ('stable:Box.mutate','11','method','method','python','box.py','Box.mutate',5,18,'exported')`)

	if _, err := PromotePropertyEdges(db); err != nil {
		t.Fatalf("PromotePropertyEdges: %v", err)
	}

	// ---- Dedup unchanged: exactly the collapsed edge counts. ----
	if got := countEdges(t, db, `type='READS' AND resolution_method LIKE 'promote_%'`); got != 2 {
		t.Fatalf("READS count changed: want 2 (mutate->Box, read_only->Box), got %d", got)
	}
	if got := countEdges(t, db, `type='WRITES' AND resolution_method LIKE 'promote_%'`); got != 1 {
		t.Fatalf("WRITES count changed: want 1 (mutate->Box), got %d", got)
	}

	// ---- Field-exact metadata match unchanged (contract_map.py's `e.metadata = ?`). ----
	if got := countEdges(t, db, `type='READS' AND metadata='count' AND resolution_method LIKE 'promote_%'`); got != 1 {
		t.Errorf("field-exact READS metadata='count': want 1, got %d", got)
	}
	if got := countEdges(t, db, `type='WRITES' AND metadata='count' AND resolution_method LIKE 'promote_%'`); got != 1 {
		t.Errorf("field-exact WRITES metadata='count': want 1, got %d", got)
	}
	if got := countEdges(t, db, `type='READS' AND metadata='size' AND resolution_method LIKE 'promote_%'`); got != 1 {
		t.Errorf("field-exact READS metadata='size' (read_only edge): want 1, got %d", got)
	}

	// ---- Confidence floor semantics unchanged: declared-field edges at 0.9 pass
	// the 0.7 fact floor; the undeclared `size` read at 0.6 fails it. ----
	if got := countEdges(t, db, `type='READS' AND confidence >= 0.7 AND resolution_method LIKE 'promote_%'`); got != 1 {
		t.Errorf("confidence floor: want 1 READS edge >= 0.7, got %d", got)
	}
	if got := countEdges(t, db, `type='READS' AND source_id=12 AND confidence >= 0.7 AND resolution_method LIKE 'promote_%'`); got != 0 {
		t.Errorf("confidence floor: read_only's undeclared-field READS must stay 0.6 (< 0.7), got %d >= 0.7", got)
	}
	assertTier(t, db, "READS", 10, "count", 0.9, "CERTIFIED")
	assertTier(t, db, "WRITES", 10, "count", 0.9, "CERTIFIED")

	// ---- access_sites payload: mutate's READS edge 11->10. ----
	type siteEntry struct {
		Field    string `json:"field"`
		Line     int    `json:"line"`
		Receiver string `json:"receiver"`
	}
	var raw string
	tx, err := db.BeginTx()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	if err := tx.QueryRow(
		`SELECT COALESCE(access_sites,'') FROM edges
		 WHERE type='READS' AND source_id=11 AND target_id=10`).Scan(&raw); err != nil {
		t.Fatalf("read access_sites for READS 11->10: %v", err)
	}
	if raw == "" {
		t.Fatal("access_sites empty on minted READS edge")
	}
	var m map[string]any
	if err := json.Unmarshal([]byte(raw), &m); err != nil {
		t.Fatalf("access_sites is not valid JSON: %v\n%s", err, raw)
	}
	if m["v"] != float64(2) {
		t.Errorf("access_sites v: want 2, got %v", m["v"])
	}
	if m["field"] != "count" {
		t.Errorf("access_sites field: want 'count' (== metadata), got %v", m["field"])
	}
	if m["access"] != "read" {
		t.Errorf("access_sites access: want 'read', got %v", m["access"])
	}
	if m["line"] != float64(6) {
		t.Errorf("access_sites line: want 6 (== source_line), got %v", m["line"])
	}
	if m["scope_node_id"] != float64(11) {
		t.Errorf("access_sites scope_node_id: want 11 (== source_id), got %v", m["scope_node_id"])
	}
	if m["scope_name"] != "mutate" {
		t.Errorf("access_sites scope_name: want 'mutate', got %v", m["scope_name"])
	}
	if m["scope_stable_id"] != "stable:Box.mutate" {
		t.Errorf("access_sites scope_stable_id: want 'stable:Box.mutate' (resolution_symbols join), got %v", m["scope_stable_id"])
	}
	sitesRaw, ok := m["sites"].([]any)
	if !ok || len(sitesRaw) != 2 {
		t.Fatalf("access_sites sites: want 2 entries (count@6, size@7), got %v", m["sites"])
	}
	var sites []siteEntry
	if b, err := json.Marshal(m["sites"]); err == nil {
		_ = json.Unmarshal(b, &sites)
	}
	wantSites := []siteEntry{{Field: "count", Line: 6, Receiver: "self"}, {Field: "size", Line: 7, Receiver: "self"}}
	if m["receiver"] != "self" {
		t.Errorf("access_sites receiver: want 'self' (persisted v2 receiver), got %v", m["receiver"])
	}
	for i, w := range wantSites {
		if sites[i] != w {
			t.Errorf("sites[%d]: want %+v, got %+v", i, w, sites[i])
		}
	}

	// ---- access_sites payload: mutate's WRITES edge — all three write rows
	// survive dedup inside `sites`, sorted by (line, field). ----
	if err := tx.QueryRow(
		`SELECT COALESCE(access_sites,'') FROM edges
		 WHERE type='WRITES' AND source_id=11 AND target_id=10`).Scan(&raw); err != nil {
		t.Fatalf("read access_sites for WRITES 11->10: %v", err)
	}
	if raw == "" {
		t.Fatal("access_sites empty on minted WRITES edge")
	}
	var wm map[string]any
	if err := json.Unmarshal([]byte(raw), &wm); err != nil {
		t.Fatalf("WRITES access_sites is not valid JSON: %v\n%s", err, raw)
	}
	if wm["access"] != "write" {
		t.Errorf("WRITES access_sites access: want 'write', got %v", wm["access"])
	}
	if wm["field"] != "count" || wm["line"] != float64(8) {
		t.Errorf("WRITES access_sites primary: want field=count line=8, got %v@%v", wm["field"], wm["line"])
	}
	var wsites []siteEntry
	if b, err := json.Marshal(wm["sites"]); err == nil {
		_ = json.Unmarshal(b, &wsites)
	}
	wantW := []siteEntry{{Field: "count", Line: 8, Receiver: "self"}, {Field: "count", Line: 9, Receiver: "self"}, {Field: "size", Line: 12, Receiver: "self"}}
	if wm["receiver"] != "self" {
		t.Errorf("WRITES access_sites receiver: want 'self', got %v", wm["receiver"])
	}
	if len(wsites) != len(wantW) {
		t.Fatalf("WRITES sites: want %d (count@8, count@9, size@12), got %v", len(wantW), wsites)
	}
	for i, w := range wantW {
		if wsites[i] != w {
			t.Errorf("WRITES sites[%d]: want %+v, got %+v", i, w, wsites[i])
		}
	}

	// ---- read_only's READS edge: no published stable id -> scope_stable_id key
	// ABSENT (correct-or-quiet, never fabricated). Assert on the raw bytes so the
	// omission itself is proven, not just a zero value after decode. ----
	if err := tx.QueryRow(
		`SELECT COALESCE(access_sites,'') FROM edges
		 WHERE type='READS' AND source_id=12 AND target_id=10`).Scan(&raw); err != nil {
		t.Fatalf("read access_sites for READS 12->10: %v", err)
	}
	wantRaw := `{"v":2,"field":"size","access":"read","line":21,"scope_node_id":12,"scope_name":"read_only","receiver":"self","sites":[{"field":"size","line":21,"receiver":"self"}]}`
	if raw != wantRaw {
		t.Errorf("access_sites byte-exact (no scope_stable_id):\n want %s\n got  %s", wantRaw, raw)
	}

	// ---- Non-READS/WRITES edges never carry the payload; unminted properties
	// leave nothing behind. ----
	if got := countEdges(t, db, `access_sites IS NOT NULL AND type NOT IN ('READS','WRITES')`); got != 0 {
		t.Errorf("access_sites on non-READS/WRITES edge: want 0, got %d", got)
	}
	if got := countEdges(t, db, `access_sites IS NOT NULL AND source_id=15`); got != 0 {
		t.Errorf("access_sites from an unminted (non-invention) property: want 0, got %d", got)
	}

	// ---- Idempotent: a second run converges to identical edges AND identical
	// access_sites bytes. ----
	firstSites := accessSitesSnapshot(t, db)
	firstCount := countEdges(t, db, `resolution_method LIKE 'promote_%'`)
	if _, err := PromotePropertyEdges(db); err != nil {
		t.Fatalf("re-run PromotePropertyEdges: %v", err)
	}
	if got := countEdges(t, db, `resolution_method LIKE 'promote_%'`); got != firstCount {
		t.Errorf("idempotency: promoted edge count drifted %d -> %d", firstCount, got)
	}
	secondSites := accessSitesSnapshot(t, db)
	if len(firstSites) != len(secondSites) {
		t.Fatalf("idempotency: access_sites row count %d -> %d", len(firstSites), len(secondSites))
	}
	for k, v := range firstSites {
		if secondSites[k] != v {
			t.Errorf("idempotency: access_sites for %v changed\n was %s\n now %s", k, v, secondSites[k])
		}
	}
}

// accessSitesSnapshot returns (type,source_id,target_id) -> access_sites for
// every edge carrying the payload, for idempotency comparison.
func accessSitesSnapshot(t *testing.T, db *store.DB) map[string]string {
	t.Helper()
	tx, err := db.BeginTx()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	rows, err := tx.Query(
		`SELECT type, source_id, target_id, access_sites FROM edges
		 WHERE access_sites IS NOT NULL ORDER BY type, source_id, target_id`)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	out := make(map[string]string)
	for rows.Next() {
		var typ, sites string
		var sid, tid int64
		if err := rows.Scan(&typ, &sid, &tid, &sites); err != nil {
			t.Fatal(err)
		}
		out[typ+":"+itoa64(sid)+":"+itoa64(tid)] = sites
	}
	return out
}

func itoa64(n int64) string {
	return strconv.FormatInt(n, 10)
}
