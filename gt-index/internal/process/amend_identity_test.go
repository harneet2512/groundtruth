package process

// amend_identity_test.go — a process is a fact about the graph's CONTENT, so
// the same logical graph must yield the same processes whatever rowids its
// rows carry. A batch amend keeps unchanged rows at their parent ids and
// re-inserts changed rows at the top of the id space; a clean rebuild numbers
// everything afresh. Before this was pinned, both the process id (it hashed
// the witness assertion rowid) and the published path (the BFS kept the
// first of two equally short paths in rowid order) differed between the two.

import (
	"database/sql"
	"path/filepath"
	"reflect"
	"strconv"
	"testing"
)

// diamondGraph: test -> entry, entry -> {left, right} -> sink. left and right
// are equally short routes to the sink; stable ids order right ("sid-a…")
// before left ("sid-z…"). ids assigns the rowids of (test, entry, left,
// right, sink); assertionID is the witness rowid.
func diamondGraph(t *testing.T, ids [5]int64, assertionID int64) *sql.DB {
	t.Helper()
	db, err := sql.Open("sqlite3", filepath.Join(t.TempDir(), "graph.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	if _, err := db.Exec(fixtureSchema); err != nil {
		t.Fatal(err)
	}
	if err := EnsureSchema(db); err != nil {
		t.Fatal(err)
	}
	names := [5]string{"test_it", "entry", "left", "right", "sink"}
	sids := [5]string{"sid-test", "sid-entry", "sid-z-left", "sid-a-right", "sid-sink"}
	for i, id := range ids {
		isTest := 0
		if i == 0 {
			isTest = 1
		}
		if _, err := db.Exec(`INSERT INTO nodes (id, label, name, qualified_name, file_path, start_line, end_line, is_test, language, stable_id)
			VALUES (?, 'Function', ?, ?, 'src/a.ts', ?, ?, ?, 'typescript', ?)`,
			id, names[i], names[i], 10*(i+1), 10*(i+1)+5, isTest, sids[i]); err != nil {
			t.Fatal(err)
		}
	}
	edge := 0
	for _, e := range [][2]int{{1, 2}, {1, 3}, {2, 4}, {3, 4}} {
		edge++
		if _, err := db.Exec(`INSERT INTO edges (id, source_id, target_id, type, resolution_method, confidence, trust_tier)
			VALUES (?, ?, ?, 'CALLS', 'import', 1.0, 'CERTIFIED')`, edge, ids[e[0]], ids[e[1]]); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := db.Exec(`INSERT INTO assertions (id, test_node_id, target_node_id, resolution_score, kind, expression, line)
		VALUES (?, ?, ?, 3.5, 'equals', 'expect(entry()).toBe(1)', 12)`, assertionID, ids[0], ids[1]); err != nil {
		t.Fatal(err)
	}
	return db
}

func processIdentity(res Result) []string {
	out := make([]string, 0, len(res.Processes))
	for _, p := range res.Processes {
		out = append(out, p.ID+"="+strconv.Itoa(p.Depth)+":"+joinPath(p.Path))
	}
	return out
}

func joinPath(p []string) string {
	s := ""
	for i, x := range p {
		if i > 0 {
			s += ">"
		}
		s += x
	}
	return s
}

func TestProcessesAreRowidInvariant(t *testing.T) {
	// "parent" numbering: left before right. "amend" numbering: the edited
	// left/right re-entered at the top of the id space in the other order,
	// and the witness assertion got a new rowid.
	clean := derive(t, diamondGraph(t, [5]int64{1, 2, 3, 4, 5}, 1), Options{})
	amend := derive(t, diamondGraph(t, [5]int64{1, 2, 41, 40, 5}, 17), Options{})
	if len(clean.Processes) != 1 {
		t.Fatalf("want one process over the diamond, got %+v", clean.Processes)
	}
	if got, want := processIdentity(amend), processIdentity(clean); !reflect.DeepEqual(got, want) {
		t.Fatalf("the same graph content published different processes under different rowids:\n amend: %v\n clean: %v", got, want)
	}
	if got := clean.Processes[0].Path; !reflect.DeepEqual(got, []string{"sid-entry", "sid-a-right", "sid-sink"}) {
		t.Fatalf("tie between equally short paths must break by stable id, got %v", got)
	}
}
