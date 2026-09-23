package main

// convergence_snapshot_test.go — an id-free semantic snapshot of a published
// graph, so a graph produced by an amend can be compared row-for-row against
// a clean rebuild of the same working tree. Every row id is replaced by the
// semantic key of the row it names (a node by file/label/qualified name/span,
// an edge by type and endpoint keys, an assertion by its test and line), so
// two graphs agree exactly when they say the same things, whatever order the
// rows were written in.

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"sort"
	"strings"
	"testing"
)

// graphSnapshot maps a category ("nodes", "edges.CALLS", "cfg_defs", ...) to
// the sorted multiset of that category's rendered rows.
type graphSnapshot map[string][]string

// volatileMetaKeys are project_meta rows that legitimately differ between two
// builds of one tree: wall-clock stamps and the parent/amend bookkeeping.
var volatileMetaKeys = map[string]bool{
	"indexed_at": true, "build_time_ms": true, "workers": true,
	// provenance of the amend's own reuse decision; the reused rows
	// themselves are compared table by table.
	"derived_coupling_reused": true,
}

func snapshotGraph(t *testing.T, path string) graphSnapshot {
	t.Helper()
	db, err := sql.Open("sqlite3", path)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	s := graphSnapshot{}

	nodeKey := map[int64]string{}
	rows := snapQuery(t, db, `SELECT id, file_path, label, COALESCE(qualified_name,''), COALESCE(start_line,0), COALESCE(end_line,0), COALESCE(node_type,''), COALESCE(stable_id,'') FROM nodes`)
	for _, r := range rows {
		nodeKey[r[0].(int64)] = fmt.Sprintf("%v|%v|%v|%v-%v|%v|%v", r[1], r[2], r[3], r[4], r[5], r[6], r[7])
	}
	keyOf := func(v any) string {
		id, ok := v.(int64)
		if !ok {
			return fmt.Sprint(v)
		}
		if id == 0 {
			return "<0>"
		}
		if k, ok := nodeKey[id]; ok {
			return k
		}
		return fmt.Sprintf("<dangling:%d>", id)
	}
	nodeCols := map[string]bool{"parent_id": true, "file_node_id": true, "node_id": true,
		"source_id": true, "target_id": true, "test_node_id": true, "target_node_id": true}

	edgeKey := map[int64]string{}
	for _, r := range snapQuery(t, db, `SELECT id, type, source_id, target_id, COALESCE(source_line,0) FROM edges`) {
		edgeKey[r[0].(int64)] = fmt.Sprintf("%v|%s|%s|%v", r[1], keyOf(r[2]), keyOf(r[3]), r[4])
	}
	assertKey := map[int64]string{}
	for _, r := range snapQuery(t, db, `SELECT id, test_node_id, COALESCE(line,0), expression FROM assertions`) {
		assertKey[r[0].(int64)] = fmt.Sprintf("%s|%v|%v", keyOf(r[1]), r[2], r[3])
	}

	render := func(cols []string, r []any, drop map[string]bool) string {
		parts := make([]string, 0, len(cols))
		for i, c := range cols {
			if c == "id" || drop[c] {
				continue
			}
			v := r[i]
			switch {
			case nodeCols[c]:
				parts = append(parts, c+"="+keyOf(v))
			case c == "witness_assertion_id":
				parts = append(parts, c+"="+assertKey[asInt(v)])
			case c == "evidence_edge_ids":
				parts = append(parts, c+"="+mapEdgeIDs(v, edgeKey))
			case strings.HasSuffix(c, "native_id"):
				// resolution_* native ids are the decimal node row id.
				var id int64
				if _, err := fmt.Sscan(renderValue(v), &id); err == nil {
					parts = append(parts, c+"="+keyOf(id))
				} else {
					parts = append(parts, c+"="+renderValue(v))
				}
			case c == "access_sites":
				parts = append(parts, c+"="+mapAccessSites(v, keyOf))
			default:
				parts = append(parts, c+"="+renderValue(v))
			}
		}
		return strings.Join(parts, "	")
	}

	tables := map[string]bool{}
	for _, r := range snapQuery(t, db, `SELECT name FROM sqlite_master WHERE type='table'`) {
		tables[fmt.Sprint(r[0])] = true
	}
	add := func(cat, table, where string, drop map[string]bool) {
		if !tables[table] {
			return
		}
		cols, data := snapQueryCols(t, db, "SELECT * FROM "+table+" "+where)
		out := make([]string, 0, len(data))
		for _, r := range data {
			out = append(out, render(cols, r, drop))
		}
		sort.Strings(out)
		s[cat] = out
	}

	// Parser-owned symbols and resolution-overlay fact nodes (node_type set)
	// are separate categories: a per-file amend legitimately lacks the
	// overlay but must reproduce every parser node.
	add("nodes", "nodes", "WHERE COALESCE(node_type,'') = ''", nil)
	add("nodes.overlay", "nodes", "WHERE COALESCE(node_type,'') <> ''", nil)
	// edges, split per type so a report names the family that diverged.
	cols, data := snapQueryCols(t, db, "SELECT * FROM edges")
	for _, r := range data {
		typ := ""
		for i, c := range cols {
			if c == "type" {
				typ = fmt.Sprint(r[i])
			}
		}
		s["edges."+typ] = append(s["edges."+typ], render(cols, r, nil))
	}
	for k := range s {
		if strings.HasPrefix(k, "edges.") {
			sort.Strings(s[k])
		}
	}
	add("properties", "properties", "", nil)
	add("assertions", "assertions", "", nil)
	for _, cfg := range []string{"cfg_blocks", "cfg_edges", "cfg_defs", "cfg_uses"} {
		add(cfg, cfg, "", nil)
	}
	add("resolution_symbols", "resolution_symbols", "", nil)
	add("resolution_callsites", "resolution_callsites", "", nil)
	add("resolution_candidates", "resolution_candidates", "", nil)
	add("closure", "closure", "", nil)
	add("cochanges", "cochanges", "", nil)
	add("communities", "communities", "", nil)
	add("community_members", "community_members", "", nil)
	add("processes", "processes", "", nil)
	add("process_steps", "process_steps", "", nil)
	add("file_hashes", "file_hashes", "", map[string]bool{"indexed_at": true})

	var meta []string
	for _, r := range snapQuery(t, db, `SELECT key, COALESCE(value,'') FROM project_meta`) {
		k := fmt.Sprint(r[0])
		if volatileMetaKeys[k] {
			continue
		}
		meta = append(meta, k+"="+renderValue(r[1]))
	}
	sort.Strings(meta)
	s["project_meta"] = meta

	// FTS parity is a property of ONE graph (docsize rows = nodes rows), so it
	// is recorded as a fact both graphs must state identically: "ok".
	s["fts_parity"] = []string{ftsParity(t, db, tables, "nodes_fts", "nodes"), ftsParity(t, db, tables, "properties_fts", "properties")}
	return s
}

func ftsParity(t *testing.T, db *sql.DB, tables map[string]bool, fts, base string) string {
	if !tables[fts] {
		return fts + "=absent"
	}
	var indexed, rows int
	if err := db.QueryRow("SELECT count(*) FROM " + fts + "_docsize").Scan(&indexed); err != nil {
		return fts + "=docsize_error:" + err.Error()
	}
	if err := db.QueryRow("SELECT count(*) FROM " + base).Scan(&rows); err != nil {
		t.Fatal(err)
	}
	if indexed != rows {
		return fmt.Sprintf("%s=MISMATCH docsize=%d rows=%d", fts, indexed, rows)
	}
	return fts + "=ok"
}

func asInt(v any) int64 {
	if i, ok := v.(int64); ok {
		return i
	}
	return 0
}

func mapEdgeIDs(v any, edgeKey map[int64]string) string {
	var raw []json.Number
	dec := json.NewDecoder(strings.NewReader(strings.ReplaceAll(renderValue(v), `"`, "")))
	dec.UseNumber()
	if err := dec.Decode(&raw); err != nil {
		return renderValue(v)
	}
	keys := make([]string, 0, len(raw))
	for _, n := range raw {
		id, _ := n.Int64()
		keys = append(keys, edgeKey[id])
	}
	sort.Strings(keys)
	return "[" + strings.Join(keys, ";") + "]"
}

// mapAccessSites replaces the scope_node_id row ids inside an access_sites
// JSON payload with node keys.
func mapAccessSites(v any, keyOf func(any) string) string {
	var payload any
	if err := json.Unmarshal([]byte(renderValue(v)), &payload); err != nil {
		return renderValue(v)
	}
	var walk func(x any) any
	walk = func(x any) any {
		switch t := x.(type) {
		case map[string]any:
			for k, val := range t {
				if f, ok := val.(float64); ok && strings.HasSuffix(k, "node_id") {
					t[k] = keyOf(int64(f))
					continue
				}
				t[k] = walk(val)
			}
			return t
		case []any:
			for i := range t {
				t[i] = walk(t[i])
			}
			return t
		}
		return x
	}
	out, _ := json.Marshal(walk(payload))
	return string(out)
}

func renderValue(v any) string {
	switch x := v.(type) {
	case nil:
		return "NULL"
	case []byte:
		return string(x)
	case float64:
		return fmt.Sprintf("%.6g", x)
	default:
		return fmt.Sprint(x)
	}
}

func snapQuery(t *testing.T, db *sql.DB, q string) [][]any {
	t.Helper()
	_, data := snapQueryCols(t, db, q)
	return data
}

func snapQueryCols(t *testing.T, db *sql.DB, q string) ([]string, [][]any) {
	t.Helper()
	rows, err := db.Query(q)
	if err != nil {
		t.Fatalf("%s: %v", q, err)
	}
	defer rows.Close()
	cols, err := rows.Columns()
	if err != nil {
		t.Fatal(err)
	}
	var out [][]any
	for rows.Next() {
		vals := make([]any, len(cols))
		ptrs := make([]any, len(cols))
		for i := range vals {
			ptrs[i] = &vals[i]
		}
		if err := rows.Scan(ptrs...); err != nil {
			t.Fatal(err)
		}
		out = append(out, vals)
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}
	return cols, out
}

// snapshotDiff renders every category whose multiset differs, with the rows
// only one side holds. An empty result means the graphs are equivalent.
func snapshotDiff(want, got graphSnapshot, skip func(cat string) bool) []string {
	cats := map[string]bool{}
	for k := range want {
		cats[k] = true
	}
	for k := range got {
		cats[k] = true
	}
	names := make([]string, 0, len(cats))
	for k := range cats {
		names = append(names, k)
	}
	sort.Strings(names)
	var out []string
	for _, cat := range names {
		if skip != nil && skip(cat) {
			continue
		}
		onlyWant, onlyGot := multisetDiff(want[cat], got[cat])
		if len(onlyWant) == 0 && len(onlyGot) == 0 {
			continue
		}
		out = append(out, fmt.Sprintf("%s: clean-only=%d amend-only=%d\n    clean-only: %s\n    amend-only: %s",
			cat, len(onlyWant), len(onlyGot), clip(onlyWant), clip(onlyGot))+pairedFieldDiff(onlyWant, onlyGot))
	}
	return out
}

func multisetDiff(a, b []string) ([]string, []string) {
	count := map[string]int{}
	for _, x := range a {
		count[x]++
	}
	for _, x := range b {
		count[x]--
	}
	var onlyA, onlyB []string
	for k, c := range count {
		for ; c > 0; c-- {
			onlyA = append(onlyA, k)
		}
		for ; c < 0; c++ {
			onlyB = append(onlyB, k)
		}
	}
	sort.Strings(onlyA)
	sort.Strings(onlyB)
	return onlyA, onlyB
}

func clip(rows []string) string {
	const max = 4
	var b strings.Builder
	for i, r := range rows {
		if i == max {
			fmt.Fprintf(&b, "\n      … %d more", len(rows)-max)
			break
		}
		if len(r) > 600 {
			r = r[:600] + "…"
		}
		b.WriteString("\n      " + r)
	}
	return b.String()
}

// pairedFieldDiff pairs the two sides row by row (both sorted) when they have
// the same size and names only the fields that differ — the column that
// carries the divergence is usually one of dozens.
func pairedFieldDiff(a, b []string) string {
	if len(a) != len(b) || len(a) == 0 {
		return ""
	}
	var out strings.Builder
	out.WriteString("\n    differing fields (paired):")
	for i := range a {
		if i == 3 {
			break
		}
		fa, fb := strings.Split(a[i], "\t"), strings.Split(b[i], "\t")
		if len(fa) != len(fb) {
			continue
		}
		for j := range fa {
			if fa[j] != fb[j] {
				x, y := fa[j], fb[j]
				if len(x) > 300 {
					x = x[:300] + "…"
				}
				if len(y) > 300 {
					y = y[:300] + "…"
				}
				fmt.Fprintf(&out, "\n      clean: %s\n      amend: %s", x, y)
			}
		}
	}
	return out.String()
}
