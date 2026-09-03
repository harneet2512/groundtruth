package process

import (
	"context"
	"database/sql"
	"fmt"
	"os"
	"reflect"
	"sort"
	"testing"

	_ "github.com/mattn/go-sqlite3"
)

// TestMeasureRealGraph runs the derivation against a real published graph and
// prints the coverage numbers final_hardening item 8 requires to be reported.
//
// It is skipped unless GT_PROCESS_MEASURE_DB names a graph.db:
//
//	GT_PROCESS_MEASURE_DB=/path/ark.db go test -tags sqlite_fts5 \
//	    -run TestMeasureRealGraph -v ./internal/process/
//
// It asserts nothing about the magnitude of the result. The plan is explicit
// that the honest first answer is "few processes qualify", and a test that
// demanded a number would be an invitation to lower the bar to reach it. It
// does assert the invariants — a witness on every process, depth within the
// bound — because those must hold on any graph.
func TestMeasureRealGraph(t *testing.T) {
	path := os.Getenv("GT_PROCESS_MEASURE_DB")
	if path == "" {
		t.Skip("set GT_PROCESS_MEASURE_DB to a graph.db to run the real-graph measurement")
	}

	db, err := sql.Open("sqlite3", "file:"+path+"?mode=ro&immutable=1")
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()

	res, err := Derive(context.Background(), db, Options{})
	if err != nil {
		t.Fatalf("Derive: %v", err)
	}

	var certifiedCalls, distinctTargets int
	if err := db.QueryRow(
		`SELECT count(*) FROM edges WHERE type='CALLS' AND trust_tier='CERTIFIED'`,
	).Scan(&certifiedCalls); err != nil {
		t.Fatal(err)
	}
	if err := db.QueryRow(
		`SELECT count(DISTINCT target_node_id) FROM assertions WHERE target_node_id <> 0`,
	).Scan(&distinctTargets); err != nil {
		t.Fatal(err)
	}

	depths := map[int]int{}
	witnesses := map[int64]bool{}
	entries := map[string]bool{}
	terminals := map[string]bool{}
	for _, p := range res.Processes {
		depths[p.Depth]++
		witnesses[p.WitnessAssertionID] = true
		entries[p.EntryStableID] = true
		terminals[p.TerminalStableID] = true

		if p.WitnessAssertionID == 0 {
			t.Fatalf("process %s has no witnessing assertion", p.ID)
		}
		if p.Depth < MinProcessDepth || p.Depth > DefaultMaxDepth {
			t.Fatalf("process %s depth %d outside [%d,%d]", p.ID, p.Depth, MinProcessDepth, DefaultMaxDepth)
		}
		if len(p.Path) != p.Depth+1 {
			t.Fatalf("process %s path length %d does not match depth %d", p.ID, len(p.Path), p.Depth)
		}
		if p.TrustFloor != TrustFloorCertified {
			t.Fatalf("process %s trust floor %q", p.ID, p.TrustFloor)
		}
	}

	report := fmt.Sprintf(`
graph:                                 %s
MaxDepth / MinConfidence:              %d / %.2f
AssertionsScanned:                     %d
AssertionsWithTarget:                  %d
distinct assertion targets:            %d
CERTIFIED CALLS edges in graph:        %d
targets with a certified path:         %d
TargetsWithoutCertifiedPath:           %d
processes qualifying:                  %d
distinct witnessing assertions:        %d
distinct entry points:                 %d
distinct terminals:                    %d
truncated by MaxProcesses:             %t
Reason (empty result only):            %q
`,
		path, DefaultMaxDepth, DefaultMinConfidence,
		res.AssertionsScanned, res.AssertionsWithTarget, distinctTargets,
		certifiedCalls,
		distinctTargets-res.TargetsWithoutCertifiedPath, res.TargetsWithoutCertifiedPath,
		len(res.Processes), len(witnesses), len(entries), len(terminals),
		res.Truncated, res.Reason,
	)

	keys := make([]int, 0, len(depths))
	for d := range depths {
		keys = append(keys, d)
	}
	sort.Ints(keys)
	report += "depth distribution:\n"
	for _, d := range keys {
		report += fmt.Sprintf("  depth %d: %d\n", d, depths[d])
	}

	// Determinism, on the real graph rather than only on the fixture.
	second, err := Derive(context.Background(), db, Options{})
	if err != nil {
		t.Fatal(err)
	}
	stable := len(second.Processes) == len(res.Processes)
	if stable {
		for i := range res.Processes {
			if second.Processes[i].ID != res.Processes[i].ID {
				stable = false
				break
			}
		}
	}
	if !stable {
		t.Fatal("two runs over the same graph produced different processes")
	}
	report += "second run identical:                  true\n"

	t.Log(report)
}

// TestPersistRealGraph runs the full Run path against a WRITABLE COPY of a real
// graph and executes the acceptance query final_hardening item 8 specifies:
//
//	select count(*) from processes where witness_assertion_id is null   -- 0
//
// Skipped unless GT_PROCESS_PERSIST_DB names a writable copy. Never point it at
// a graph you care about: Persist replaces the process tables wholesale.
func TestPersistRealGraph(t *testing.T) {
	path := os.Getenv("GT_PROCESS_PERSIST_DB")
	if path == "" {
		t.Skip("set GT_PROCESS_PERSIST_DB to a WRITABLE COPY of a graph.db to run the persistence measurement")
	}

	res, err := Run(context.Background(), path, Options{})
	if err != nil {
		t.Fatalf("Run: %v", err)
	}

	db, err := sql.Open("sqlite3", path)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()

	var processes, steps, unwitnessed, orphanSteps int
	for q, dst := range map[string]*int{
		`SELECT count(*) FROM processes`:                                    &processes,
		`SELECT count(*) FROM process_steps`:                                &steps,
		`SELECT count(*) FROM processes WHERE witness_assertion_id IS NULL`: &unwitnessed,
		`SELECT count(*) FROM process_steps s
		 WHERE NOT EXISTS (SELECT 1 FROM processes p WHERE p.id = s.process_id)`: &orphanSteps,
	} {
		if err := db.QueryRow(q).Scan(dst); err != nil {
			t.Fatalf("%s: %v", q, err)
		}
	}

	if processes != len(res.Processes) {
		t.Fatalf("persisted %d processes; derived %d", processes, len(res.Processes))
	}
	if unwitnessed != 0 {
		t.Fatalf("processes with a null witness = %d; want 0", unwitnessed)
	}
	if orphanSteps != 0 {
		t.Fatalf("orphan process_steps = %d; want 0", orphanSteps)
	}

	loaded, err := Load(db)
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if !reflect.DeepEqual(loaded, res.Processes) {
		t.Fatal("round trip over the real graph did not reproduce the derived processes")
	}

	t.Logf(`
graph (writable copy):                 %s
processes rows:                        %d
process_steps rows:                    %d
processes with a NULL witness:         %d   (acceptance criterion: 0)
orphan process_steps rows:             %d
Load() round-trips derived processes:  true
`, path, processes, steps, unwitnessed, orphanSteps)
}
