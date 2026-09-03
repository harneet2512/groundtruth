package community

import (
	"context"
	"database/sql"
	"fmt"
	"math"
	"reflect"
	"sort"
	"strings"
	"testing"
)

// snapshotEdges reads every edge row that matters to the invariant. Comparing
// this before and after a Build+Persist is the mechanical proof that community
// membership did not promote anything.
func snapshotEdges(t *testing.T, db *sql.DB) []string {
	t.Helper()
	rows, err := db.Query("SELECT id, source_id, target_id, type, trust_tier FROM edges ORDER BY id")
	if err != nil {
		t.Fatalf("snapshot edges: %v", err)
	}
	defer rows.Close()
	var out []string
	for rows.Next() {
		var id, src, dst int64
		var typ, tier string
		if err := rows.Scan(&id, &src, &dst, &typ, &tier); err != nil {
			t.Fatalf("scan edge: %v", err)
		}
		out = append(out, fmt.Sprintf("%d:%d->%d:%s:%s", id, src, dst, typ, tier))
	}
	if err := rows.Err(); err != nil {
		t.Fatalf("iterate edges: %v", err)
	}
	return out
}

// TestInvariantEdgesAndTiersUnchanged is the enforcement of the plan's
// standing rule: candidate evidence is never promoted to verified evidence by
// community membership. The fixture is the one where promotion would be
// tempting -- a CANDIDATE bridge whose two endpoints the clustering examined
// and rejected.
func TestInvariantEdgesAndTiersUnchanged(t *testing.T) {
	db := graphDB(t)
	twoClusterGraph(t, db, "CANDIDATE", 6)
	before := snapshotEdges(t, db)

	res, err := Build(context.Background(), db, nonRepo(t), Options{})
	if err != nil {
		t.Fatalf("Build: %v", err)
	}
	tx, err := db.Begin()
	if err != nil {
		t.Fatalf("begin: %v", err)
	}
	if _, err := Persist(tx, res); err != nil {
		t.Fatalf("Persist: %v", err)
	}
	if err := tx.Commit(); err != nil {
		t.Fatalf("commit: %v", err)
	}

	after := snapshotEdges(t, db)
	if !reflect.DeepEqual(before, after) {
		t.Fatalf("edges changed across clustering + persistence:\nbefore: %v\n after: %v", before, after)
	}
	var certified int
	if err := db.QueryRow("SELECT COUNT(*) FROM edges WHERE trust_tier = ?", TierCertified).Scan(&certified); err != nil {
		t.Fatalf("count certified: %v", err)
	}
	if certified != 18 {
		t.Errorf("certified edge count = %d, want 18 -- nothing may be promoted", certified)
	}
}

// TestPersistRoundTrip covers the whole storage contract in one pass: a
// measured cohesion survives, an unmeasurable one survives AS NULL and reads
// back as NaN, members and evidence survive as ordered lists, and a second
// Persist of the same partition is idempotent rather than duplicating rows.
func TestPersistRoundTrip(t *testing.T) {
	db := graphDB(t)
	lo, hi := wilson(3, 4)
	in := Result{
		Algorithm: AlgorithmCPMLeidenDeterministic,
		Options:   Options{Resolution: 0.5, WCall: 2, WCochange: 3, HoldoutCommits: 7}.Normalize(),
		Communities: []Community{
			{
				ID:                 "community:aaaaaaaaaaaaaaaa",
				Label:              "src/parser/",
				HeuristicLabel:     "src/parser/",
				Keywords:           []string{"parser", "src", "go"},
				Description:        "three files",
				EnrichedBy:         EnrichedByHeuristic,
				Cohesion:           0.75,
				CohesionInterval:   Interval{Lo: lo, Hi: hi, N: 4},
				CohesionReason:     CohesionMeasured,
				StructuralCohesion: 0.9,
				Members:            []string{"src/parser/a.go", "src/parser/b.go", "src/parser/c.go"},
				EvidenceEdgeIDs:    []string{"11", "12", "cochange:src/parser/a.go|src/parser/b.go"},
				InternalWeight:     14,
				ExternalWeight:     1.5,
			},
			{
				ID:                 "community:bbbbbbbbbbbbbbbb",
				Label:              "lib/",
				HeuristicLabel:     "lib/",
				Keywords:           nil,
				Description:        "two files, untested",
				EnrichedBy:         EnrichedByHeuristic,
				Cohesion:           math.NaN(),
				CohesionInterval:   Interval{Lo: math.NaN(), Hi: math.NaN()},
				CohesionReason:     CohesionNoHoldoutPairs,
				StructuralCohesion: 0.5,
				Members:            []string{"lib/x.go", "lib/y.go"},
				EvidenceEdgeIDs:    nil,
				EvidenceTruncated:  true,
				InternalWeight:     2,
				ExternalWeight:     2,
			},
		},
	}

	for pass := 0; pass < 2; pass++ {
		tx, err := db.Begin()
		if err != nil {
			t.Fatalf("begin: %v", err)
		}
		n, err := Persist(tx, in)
		if err != nil {
			t.Fatalf("Persist pass %d: %v", pass, err)
		}
		if n != 2 {
			t.Fatalf("Persist wrote %d rows, want 2", n)
		}
		if err := tx.Commit(); err != nil {
			t.Fatalf("commit: %v", err)
		}
	}

	// Idempotence: the second pass must have replaced, not duplicated.
	var communities, members int
	if err := db.QueryRow("SELECT COUNT(*) FROM communities").Scan(&communities); err != nil {
		t.Fatalf("count communities: %v", err)
	}
	if err := db.QueryRow("SELECT COUNT(*) FROM community_members").Scan(&members); err != nil {
		t.Fatalf("count members: %v", err)
	}
	if communities != 2 || members != 5 {
		t.Fatalf("after two passes: %d communities / %d members, want 2 / 5", communities, members)
	}

	// The absent cohesion must be SQL NULL, not 0.0. A consumer running
	// AVG(cohesion) over these rows must not be handed a fabricated zero.
	var nulls int
	if err := db.QueryRow("SELECT COUNT(*) FROM communities WHERE cohesion IS NULL").Scan(&nulls); err != nil {
		t.Fatalf("count nulls: %v", err)
	}
	if nulls != 1 {
		t.Errorf("communities with NULL cohesion = %d, want 1", nulls)
	}

	got, err := Load(db)
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if len(got) != 2 {
		t.Fatalf("Load returned %d communities, want 2", len(got))
	}

	first := got[0]
	want := in.Communities[0]
	if first.ID != want.ID || first.Label != want.Label || first.HeuristicLabel != want.HeuristicLabel {
		t.Errorf("identity round trip: %+v", first)
	}
	if !reflect.DeepEqual(first.Keywords, want.Keywords) {
		t.Errorf("keywords = %v, want %v", first.Keywords, want.Keywords)
	}
	if !reflect.DeepEqual(first.Members, want.Members) {
		t.Errorf("members = %v, want %v", first.Members, want.Members)
	}
	if !reflect.DeepEqual(first.EvidenceEdgeIDs, want.EvidenceEdgeIDs) {
		t.Errorf("evidence = %v, want %v", first.EvidenceEdgeIDs, want.EvidenceEdgeIDs)
	}
	if first.Cohesion != want.Cohesion || first.CohesionInterval != want.CohesionInterval {
		t.Errorf("cohesion = %v %+v, want %v %+v",
			first.Cohesion, first.CohesionInterval, want.Cohesion, want.CohesionInterval)
	}
	if first.EnrichedBy != EnrichedByHeuristic {
		t.Errorf("enriched_by = %q", first.EnrichedBy)
	}

	second := got[1]
	if !math.IsNaN(second.Cohesion) {
		t.Errorf("absent cohesion read back as %v, want NaN", second.Cohesion)
	}
	if !math.IsNaN(second.CohesionInterval.Lo) || !math.IsNaN(second.CohesionInterval.Hi) {
		t.Errorf("absent interval read back as %+v, want NaN bounds", second.CohesionInterval)
	}
	if second.CohesionReason != CohesionNoHoldoutPairs {
		t.Errorf("reason = %q", second.CohesionReason)
	}
	if !second.EvidenceTruncated {
		t.Error("evidence_truncated did not survive the round trip")
	}
	if len(second.Keywords) != 0 || len(second.EvidenceEdgeIDs) != 0 {
		t.Errorf("empty lists came back as %v / %v", second.Keywords, second.EvidenceEdgeIDs)
	}
}

// TestPersistEmptyResultCreatesTables holds the distinction the two-phase
// publication item depends on: "clustered and found nothing" leaves empty
// tables, which is not the same as "never clustered", which leaves none.
func TestPersistEmptyResultCreatesTables(t *testing.T) {
	db := graphDB(t)
	tx, err := db.Begin()
	if err != nil {
		t.Fatalf("begin: %v", err)
	}
	n, err := Persist(tx, Result{Reason: ReasonNoEdges})
	if err != nil {
		t.Fatalf("Persist: %v", err)
	}
	if n != 0 {
		t.Errorf("wrote %d rows for an empty result", n)
	}
	if err := tx.Commit(); err != nil {
		t.Fatalf("commit: %v", err)
	}
	var count int
	if err := db.QueryRow("SELECT COUNT(*) FROM communities").Scan(&count); err != nil {
		t.Fatalf("the tables must exist even with nothing in them: %v", err)
	}
	if count != 0 {
		t.Errorf("count = %d, want 0", count)
	}
}

// TestPersistNilTransactionIsAnError guards against a caller silently getting
// no persistence.
func TestPersistNilTransactionIsAnError(t *testing.T) {
	if _, err := Persist(nil, Result{}); err == nil {
		t.Error("Persist(nil, ...) must fail loudly")
	}
	if err := EnsureSchema(nil); err == nil {
		t.Error("EnsureSchema(nil) must fail loudly")
	}
}

// TestBuildToPersistToLoad is the full pipeline: cluster a real graph against
// a real history, store it, read it back, and check the numbers survived.
func TestBuildToPersistToLoad(t *testing.T) {
	db := graphDB(t)
	res, err := Build(context.Background(), db, holdoutRepo(t), Options{HoldoutCommits: 2})
	if err != nil {
		t.Fatalf("Build: %v", err)
	}
	if res.Reason != ReasonOK {
		t.Fatalf("reason = %q", res.Reason)
	}
	tx, err := db.Begin()
	if err != nil {
		t.Fatalf("begin: %v", err)
	}
	if _, err := Persist(tx, res); err != nil {
		t.Fatalf("Persist: %v", err)
	}
	if err := tx.Commit(); err != nil {
		t.Fatalf("commit: %v", err)
	}

	loaded, err := Load(db)
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if len(loaded) != len(res.Communities) {
		t.Fatalf("loaded %d, built %d", len(loaded), len(res.Communities))
	}
	byID := map[string]Community{}
	for _, c := range loaded {
		byID[c.ID] = c
	}
	for _, want := range res.Communities {
		got, ok := byID[want.ID]
		if !ok {
			t.Fatalf("community %s (%s) did not survive the round trip", want.ID, want.HeuristicLabel)
		}
		if !reflect.DeepEqual(got.Members, want.Members) {
			t.Errorf("%s members = %v, want %v", want.ID, got.Members, want.Members)
		}
		if math.IsNaN(want.Cohesion) != math.IsNaN(got.Cohesion) || (!math.IsNaN(want.Cohesion) && got.Cohesion != want.Cohesion) {
			t.Errorf("%s cohesion = %v, want %v", want.ID, got.Cohesion, want.Cohesion)
		}
		if got.CohesionReason != want.CohesionReason {
			t.Errorf("%s reason = %q, want %q", want.ID, got.CohesionReason, want.CohesionReason)
		}
		if got.Description != want.Description {
			t.Errorf("%s description = %q, want %q", want.ID, got.Description, want.Description)
		}
	}

	// The member index must be usable in the direction consumers need it:
	// given a file, which community owns it.
	var owner string
	err = db.QueryRow("SELECT community_id FROM community_members WHERE member = ?", "src/a.go").Scan(&owner)
	if err != nil {
		t.Fatalf("reverse lookup by member: %v", err)
	}
	if owner == "" {
		t.Error("member index returned an empty community id")
	}
}

// TestSchemaColumnsMatchInsert is the drift guard: the column list, the
// placeholder count and the DDL must agree, or publication fails at runtime on
// a repository nobody is watching.
func TestSchemaColumnsMatchInsert(t *testing.T) {
	cols := strings.Split(CommunityColumns, ", ")
	placeholders := strings.Count(insertCommunitySQL, "?")
	if len(cols) != placeholders {
		t.Fatalf("%d columns but %d placeholders", len(cols), placeholders)
	}
	db := graphDB(t)
	tx, err := db.Begin()
	if err != nil {
		t.Fatalf("begin: %v", err)
	}
	if err := EnsureSchema(tx); err != nil {
		t.Fatalf("EnsureSchema: %v", err)
	}
	if err := tx.Commit(); err != nil {
		t.Fatalf("commit: %v", err)
	}
	rows, err := db.Query("PRAGMA table_info(communities)")
	if err != nil {
		t.Fatalf("table_info: %v", err)
	}
	defer rows.Close()
	var declared []string
	for rows.Next() {
		var cid int
		var name, typ string
		var notNull int
		var dflt sql.NullString
		var pk int
		if err := rows.Scan(&cid, &name, &typ, &notNull, &dflt, &pk); err != nil {
			t.Fatalf("scan table_info: %v", err)
		}
		declared = append(declared, name)
	}
	sort.Strings(declared)
	written := append([]string{}, cols...)
	sort.Strings(written)
	if !reflect.DeepEqual(declared, written) {
		t.Errorf("DDL declares %v but Persist writes %v", declared, written)
	}
}
