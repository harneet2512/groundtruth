package store

import (
	"path/filepath"
	"testing"
)

func parsedFactDB(t *testing.T) (*DB, int64) {
	t.Helper()
	db, err := Open(filepath.Join(t.TempDir(), "graph.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	ids, _, _, err := db.ReplaceParsedStructure([]*Node{{Label: "Function", Name: "test_work", FilePath: "test_work.py", FileHash: "source"}}, false, "", false)
	if err != nil {
		t.Fatal(err)
	}
	return db, ids[0]
}

func TestParsedPropertiesRetainMultiplicityAndDiscardDerivedFacts(t *testing.T) {
	db, node := parsedFactDB(t)
	p := &Property{NodeID: node, Kind: "param", Value: "value", Line: 2, Confidence: 0.8}
	if n, _, err := db.ReconcileParsedProperties([]*Property{p, p}); err != nil || n != 0 {
		t.Fatalf("initial: %d %v", n, err)
	}
	var first int64
	if err := db.db.QueryRow(`SELECT min(id) FROM properties`).Scan(&first); err != nil {
		t.Fatal(err)
	}
	if err := db.BatchInsertProperties([]*Property{{NodeID: node, Kind: "derived", Value: "obsolete"}}); err != nil {
		t.Fatal(err)
	}
	if n, _, err := db.ReconcileParsedProperties([]*Property{p}); err != nil || n != 1 {
		t.Fatalf("reuse: %d %v", n, err)
	}
	var count int
	var id int64
	if err := db.db.QueryRow(`SELECT count(*),min(id) FROM properties`).Scan(&count, &id); err != nil {
		t.Fatal(err)
	}
	if count != 1 || id != first {
		t.Fatalf("multiplicity/identity: %d %d want 1 %d", count, id, first)
	}
	if n, _, err := db.ReconcileParsedProperties(nil); err != nil || n != 0 {
		t.Fatalf("empty: %d %v", n, err)
	}
	if err := db.db.QueryRow(`SELECT count(*) FROM properties`).Scan(&count); err != nil || count != 0 {
		t.Fatalf("empty facts: %d %v", count, err)
	}
}

func TestParsedAssertionFreshTargetAndScoreInvalidateReuse(t *testing.T) {
	db, node := parsedFactDB(t)
	a := &Assertion{TestNodeID: node, TargetNodeID: node, ResolutionScore: 0.8, Kind: "assert", Expression: "work() == 1", Expected: "1", Line: 3}
	if _, _, err := db.ReconcileParsedAssertions([]*Assertion{a}); err != nil {
		t.Fatal(err)
	}
	if n, _, err := db.ReconcileParsedAssertions([]*Assertion{a}); err != nil || n != 1 {
		t.Fatalf("same: %d %v", n, err)
	}
	a.TargetNodeID = 0
	if n, _, err := db.ReconcileParsedAssertions([]*Assertion{a}); err != nil || n != 0 {
		t.Fatalf("new target: %d %v", n, err)
	}
	a.ResolutionScore = 0.3
	if n, _, err := db.ReconcileParsedAssertions([]*Assertion{a}); err != nil || n != 0 {
		t.Fatalf("new score: %d %v", n, err)
	}
}

func TestParsedFactTamperingHealsWithoutRetaining(t *testing.T) {
	// A row mutated underneath its inventory digest is not the minted fact:
	// the diverged entry is counted, the row is swept, and the desired fact
	// is inserted fresh. The tampered content never survives the amend.
	for _, table := range []string{"properties", "assertions"} {
		t.Run(table, func(t *testing.T) {
			db, node := parsedFactDB(t)
			var reconcile func() (int, int, error)
			if table == "properties" {
				reconcile = func() (int, int, error) {
					return db.ReconcileParsedProperties([]*Property{{NodeID: node, Kind: "param", Value: "value"}})
				}
			} else {
				reconcile = func() (int, int, error) {
					return db.ReconcileParsedAssertions([]*Assertion{{TestNodeID: node, Kind: "assert", Expression: "work()"}})
				}
			}
			if _, _, err := reconcile(); err != nil {
				t.Fatal(err)
			}
			if _, err := db.db.Exec("UPDATE " + table + " SET kind='tampered'"); err != nil {
				t.Fatal(err)
			}
			retained, diverged, err := reconcile()
			if err != nil {
				t.Fatalf("diverged entry must heal, not fail: %v", err)
			}
			if retained != 0 || diverged != 1 {
				t.Fatalf("tampered fact: retained=%d diverged=%d want 0/1", retained, diverged)
			}
			var count int
			if err := db.db.QueryRow("SELECT count(*) FROM " + table + " WHERE kind='tampered'").Scan(&count); err != nil || count != 0 {
				t.Fatalf("tampered row retained: %d %v", count, err)
			}
			if err := db.db.QueryRow("SELECT count(*) FROM " + table).Scan(&count); err != nil || count != 1 {
				t.Fatalf("desired fact not minted: %d %v", count, err)
			}
		})
	}
}

func TestParsedEdgesPreserveOtherOwnersAndRejectResolutionKinds(t *testing.T) {
	db, node := parsedFactDB(t)
	e := &Edge{SourceID: node, TargetID: node, Type: "CONTAINS", Confidence: 1, TrustTier: "CERTIFIED"}
	if n, _, err := db.ReconcileParsedEdges([]*Edge{e}); err != nil || n != 0 {
		t.Fatalf("initial: %d %v", n, err)
	}
	call := &Edge{SourceID: node, TargetID: node, Type: "CALLS"}
	if err := db.BatchInsertEdges([]*Edge{call}); err != nil {
		t.Fatal(err)
	}
	if n, _, err := db.ReconcileParsedEdges([]*Edge{e}); err != nil || n != 1 {
		t.Fatalf("reuse: %d %v", n, err)
	}
	if _, _, err := db.ReconcileParsedEdges([]*Edge{call}); err == nil {
		t.Fatal("resolution kind admitted")
	}
	if _, _, err := db.ReconcileParsedEdges(nil); err != nil {
		t.Fatal(err)
	}
	var count int
	if err := db.db.QueryRow(`SELECT count(*) FROM edges WHERE type='CALLS'`).Scan(&count); err != nil || count != 1 {
		t.Fatalf("other owner lost: %d %v", count, err)
	}
	if err := db.db.QueryRow(`SELECT count(*) FROM edges WHERE type='CONTAINS'`).Scan(&count); err != nil || count != 0 {
		t.Fatalf("stale structural edges: %d %v", count, err)
	}
}

func TestParsedFactsInsertionFailureRollsBackInventory(t *testing.T) {
	db, node := parsedFactDB(t)
	p := &Property{NodeID: node, Kind: "param", Value: "original"}
	if _, _, err := db.ReconcileParsedProperties([]*Property{p}); err != nil {
		t.Fatal(err)
	}
	if _, err := db.db.Exec(`CREATE TRIGGER refuse_new_property BEFORE INSERT ON properties BEGIN SELECT RAISE(ABORT, 'injected failure'); END`); err != nil {
		t.Fatal(err)
	}
	if _, _, err := db.ReconcileParsedProperties([]*Property{{NodeID: node, Kind: "param", Value: "replacement"}}); err == nil {
		t.Fatal("insertion failure ignored")
	}
	if n, _, err := db.ReconcileParsedProperties([]*Property{p}); err != nil || n != 1 {
		t.Fatalf("original inventory lost: %d %v", n, err)
	}
}
