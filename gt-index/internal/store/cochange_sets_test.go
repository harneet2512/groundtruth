package store

import (
	"fmt"
	"path/filepath"
	"testing"
)

// F11 (RED→GREEN, determinism invariant): BatchInsertCochangeSets must insert commits
// in SORTED order, never Go map-range order. SQLite assigns rowids in insertion order,
// so a randomized map range makes two indexings of the SAME repo write byte-DIFFERENT
// graph.db files. With 8 commits the pre-fix map-range order coincides with sorted
// order only with probability 1/8! ≈ 2.5e-5 per run, so the pre-fix code reddens this
// test with near-certainty; post-fix it is exact.
func TestBatchInsertCochangeSets_RowOrderIsSortedByCommit(t *testing.T) {
	dir := t.TempDir()
	db, err := Open(filepath.Join(dir, "graph.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()

	sets := make(map[string][]string)
	var want []string // (commit, file) pairs in the required storage order
	for i := 0; i < 8; i++ {
		h := fmt.Sprintf("c%02dhash", i)
		sets[h] = []string{fmt.Sprintf("src/a%d.py", i), fmt.Sprintf("src/b%d.py", i)}
	}
	for i := 0; i < 8; i++ { // sorted commit order; per-commit file order preserved
		h := fmt.Sprintf("c%02dhash", i)
		want = append(want, h+"|"+sets[h][0], h+"|"+sets[h][1])
	}

	if err := db.BatchInsertCochangeSets(sets); err != nil {
		t.Fatal(err)
	}

	rows, err := db.db.Query("SELECT commit_hash, file_path FROM cochange_sets ORDER BY rowid")
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	var got []string
	for rows.Next() {
		var c, f string
		if err := rows.Scan(&c, &f); err != nil {
			t.Fatal(err)
		}
		got = append(got, c+"|"+f)
	}
	if len(got) != len(want) {
		t.Fatalf("row count %d != %d", len(got), len(want))
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("F11 DETERMINISM: physical row %d is %q, want %q — commits were inserted "+
				"in map-range order, so graph.db bytes differ across identical indexings", i, got[i], want[i])
		}
	}
}

// Idempotence guard: INSERT OR IGNORE + the (commit,file) primary key make a re-insert
// of the same sets a no-op (row count unchanged) — the incremental-reindex safety.
func TestBatchInsertCochangeSets_ReinsertIsIdempotent(t *testing.T) {
	dir := t.TempDir()
	db, err := Open(filepath.Join(dir, "graph.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()

	sets := map[string][]string{"deadbeef01": {"x.py", "y.py"}}
	if err := db.BatchInsertCochangeSets(sets); err != nil {
		t.Fatal(err)
	}
	if err := db.BatchInsertCochangeSets(sets); err != nil {
		t.Fatal(err)
	}
	var n int
	if err := db.db.QueryRow("SELECT COUNT(*) FROM cochange_sets").Scan(&n); err != nil {
		t.Fatal(err)
	}
	if n != 2 {
		t.Fatalf("re-insert must be idempotent: want 2 rows, got %d", n)
	}
}
