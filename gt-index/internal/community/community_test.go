package community

import (
	"context"
	"database/sql"
	"fmt"
	"math"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"testing"

	_ "github.com/mattn/go-sqlite3"
)

// schemaGraph is the subset of internal/store/sqlite.go's `nodes` and `edges`
// declarations that this package actually reads, copied column-for-column from
// the real DDL. If the producer renames file_path, type or trust_tier, these
// tests fail loudly instead of the loader silently returning nothing.
const schemaGraph = `
	CREATE TABLE IF NOT EXISTS nodes (
		id INTEGER PRIMARY KEY AUTOINCREMENT,
		label TEXT NOT NULL,
		name TEXT NOT NULL,
		file_path TEXT NOT NULL,
		language TEXT NOT NULL
	);
	CREATE TABLE IF NOT EXISTS edges (
		id INTEGER PRIMARY KEY AUTOINCREMENT,
		source_id INTEGER NOT NULL REFERENCES nodes(id),
		target_id INTEGER NOT NULL REFERENCES nodes(id),
		type TEXT NOT NULL,
		trust_tier TEXT DEFAULT 'SPECULATIVE'
	);
	CREATE INDEX IF NOT EXISTS idx_edges_trust_tier ON edges(trust_tier);
`

// gitEnvFlags pin the parts of git configuration a fixture depends on so a
// test repository is reproducible on any machine: a fixed identity, no
// signing, a named default branch. Hooks are deliberately NOT overridden -- a
// fresh `git init` temp repository has none to run.
var gitEnvFlags = []string{
	"-c", "user.name=community test",
	"-c", "user.email=community@example.invalid",
	"-c", "commit.gpgsign=false",
	"-c", "init.defaultBranch=main",
	"-c", "gc.auto=0",
	"-c", "maintenance.auto=false",
}

func git(t *testing.T, dir string, args ...string) string {
	t.Helper()
	cmd := exec.Command("git", append(append([]string{}, gitEnvFlags...), args...)...)
	cmd.Dir = dir
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("git %s in %s: %v\n%s", strings.Join(args, " "), dir, err, out)
	}
	return string(out)
}

func writeFile(t *testing.T, dir, name, content string) {
	t.Helper()
	p := filepath.Join(dir, name)
	if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
		t.Fatalf("mkdir for %s: %v", p, err)
	}
	if err := os.WriteFile(p, []byte(content), 0o644); err != nil {
		t.Fatalf("write %s: %v", p, err)
	}
}

func commitFiles(t *testing.T, dir, message string, names ...string) {
	t.Helper()
	for _, n := range names {
		writeFile(t, dir, n, "// "+message+" :: "+n+"\n")
	}
	git(t, dir, "add", "-A")
	git(t, dir, "commit", "-q", "-m", message)
}

// graphDB opens an empty graph with the schema subset above.
func graphDB(t *testing.T) *sql.DB {
	t.Helper()
	db, err := sql.Open("sqlite3", filepath.Join(t.TempDir(), "graph.db"))
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	t.Cleanup(func() { db.Close() })
	if _, err := db.Exec(schemaGraph); err != nil {
		t.Fatalf("create schema: %v", err)
	}
	return db
}

// nodeIn inserts one symbol node in a file and returns its id.
func nodeIn(t *testing.T, db *sql.DB, file, name string) int64 {
	t.Helper()
	res, err := db.Exec(
		"INSERT INTO nodes (label, name, file_path, language) VALUES ('Function', ?, ?, 'go')",
		name, file)
	if err != nil {
		t.Fatalf("insert node %s: %v", name, err)
	}
	id, err := res.LastInsertId()
	if err != nil {
		t.Fatalf("node id: %v", err)
	}
	return id
}

// edgeBetween inserts one CALLS edge at the given tier.
func edgeBetween(t *testing.T, db *sql.DB, src, dst int64, tier string) int64 {
	t.Helper()
	res, err := db.Exec(
		"INSERT INTO edges (source_id, target_id, type, trust_tier) VALUES (?, ?, 'CALLS', ?)",
		src, dst, tier)
	if err != nil {
		t.Fatalf("insert edge: %v", err)
	}
	id, err := res.LastInsertId()
	if err != nil {
		t.Fatalf("edge id: %v", err)
	}
	return id
}

// twoClusterGraph builds the acceptance fixture: two triangles of files,
// src/a1..a3 and src/b1..b3, each internally wired with three CERTIFIED CALLS
// edges per file pair, and a single bridge edge between a1 and b1 whose TIER
// is the variable under test. bridgeCount edges are inserted at bridgeTier.
//
// The topology is identical in both arms. The ONLY difference is the tier of
// the bridge, which is what makes it a test of the trust weighting rather than
// a test of the clustering.
func twoClusterGraph(t *testing.T, db *sql.DB, bridgeTier string, bridgeCount int) {
	t.Helper()
	files := []string{"src/a1.go", "src/a2.go", "src/a3.go", "lib/b1.go", "lib/b2.go", "lib/b3.go"}
	ids := make(map[string]int64, len(files))
	for i, f := range files {
		ids[f] = nodeIn(t, db, f, fmt.Sprintf("sym%d", i))
	}
	wire := func(a, b string, n int) {
		for i := 0; i < n; i++ {
			edgeBetween(t, db, ids[a], ids[b], TierCertified)
		}
	}
	wire("src/a1.go", "src/a2.go", 3)
	wire("src/a1.go", "src/a3.go", 3)
	wire("src/a2.go", "src/a3.go", 3)
	wire("lib/b1.go", "lib/b2.go", 3)
	wire("lib/b1.go", "lib/b3.go", 3)
	wire("lib/b2.go", "lib/b3.go", 3)
	for i := 0; i < bridgeCount; i++ {
		edgeBetween(t, db, ids["src/a1.go"], ids["lib/b1.go"], bridgeTier)
	}
}

// nonRepo returns a directory that is not a git work tree, so a graph-only
// test gets an explicit history_unavailable rather than whatever history the
// machine happens to have.
func nonRepo(t *testing.T) string {
	t.Helper()
	return t.TempDir()
}

func memberSets(cs []Community) []string {
	out := make([]string, 0, len(cs))
	for _, c := range cs {
		out = append(out, strings.Join(c.Members, ","))
	}
	sort.Strings(out)
	return out
}

// TestCandidateBridgeCannotMergeCommunities is the acceptance case for the
// trust weighting: an untrusted bridge, however many edges it carries, must
// not be able to join two communities, because it never becomes weight.
func TestCandidateBridgeCannotMergeCommunities(t *testing.T) {
	for _, tier := range []string{"CANDIDATE", "SPECULATIVE", "STRUCTURAL"} {
		t.Run(tier, func(t *testing.T) {
			db := graphDB(t)
			// 40 bridge edges -- far more than the 3 that wire each real pair.
			// If tier were ignored, this would trivially merge the clusters.
			twoClusterGraph(t, db, tier, 40)

			res, err := Build(context.Background(), db, nonRepo(t), Options{})
			if err != nil {
				t.Fatalf("Build: %v", err)
			}
			if res.Reason != ReasonOK {
				t.Fatalf("reason = %q, want %q", res.Reason, ReasonOK)
			}
			if len(res.Communities) != 2 {
				t.Fatalf("got %d communities, want 2:\n%s", len(res.Communities), dump(res))
			}
			if res.ExcludedCallRows != 40 {
				t.Errorf("excluded_call_rows = %d, want 40", res.ExcludedCallRows)
			}
			if res.CertifiedCallRows != 18 {
				t.Errorf("certified_call_rows = %d, want 18", res.CertifiedCallRows)
			}
			got := memberSets(res.Communities)
			want := []string{"lib/b1.go,lib/b2.go,lib/b3.go", "src/a1.go,src/a2.go,src/a3.go"}
			if strings.Join(got, " | ") != strings.Join(want, " | ") {
				t.Errorf("members = %v, want %v", got, want)
			}
		})
	}
}

// TestCertifiedBridgeMayMergeCommunities is the other half of the same
// experiment. Same topology, same edge count, CERTIFIED tier -- and now the
// bridge is allowed to do what the objective says it should. Without this the
// previous test would pass on a clustering that can never merge anything.
func TestCertifiedBridgeMayMergeCommunities(t *testing.T) {
	db := graphDB(t)
	twoClusterGraph(t, db, TierCertified, 40)

	res, err := Build(context.Background(), db, nonRepo(t), Options{})
	if err != nil {
		t.Fatalf("Build: %v", err)
	}
	if len(res.Communities) != 1 {
		t.Fatalf("got %d communities, want 1 (a certified bridge of 40 edges must be allowed to merge):\n%s",
			len(res.Communities), dump(res))
	}
	if len(res.Communities[0].Members) != 6 {
		t.Errorf("members = %v, want all six files", res.Communities[0].Members)
	}
	if res.ExcludedCallRows != 0 {
		t.Errorf("excluded_call_rows = %d, want 0", res.ExcludedCallRows)
	}
}

// TestSameFileEdgesAreNotWeight guards the self-loop exclusion: an intra-file
// call cannot move a file into a different community, so counting it would
// only inflate internal weight and the structural score that reads it.
func TestSameFileEdgesAreNotWeight(t *testing.T) {
	db := graphDB(t)
	a := nodeIn(t, db, "src/a.go", "a")
	a2 := nodeIn(t, db, "src/a.go", "a2")
	b := nodeIn(t, db, "src/b.go", "b")
	for i := 0; i < 5; i++ {
		edgeBetween(t, db, a, a2, TierCertified)
	}
	edgeBetween(t, db, a, b, TierCertified)

	res, err := Build(context.Background(), db, nonRepo(t), Options{})
	if err != nil {
		t.Fatalf("Build: %v", err)
	}
	if res.CertifiedCallRows != 1 {
		t.Fatalf("certified_call_rows = %d, want 1 (five same-file edges must not count)", res.CertifiedCallRows)
	}
	if len(res.Communities) != 1 || len(res.Communities[0].Members) != 2 {
		t.Fatalf("want one community of two files, got:\n%s", dump(res))
	}
	if got := res.Communities[0].InternalWeight; got != 1 {
		t.Errorf("internal weight = %v, want 1", got)
	}
}

// TestEvidenceEdgeIDsAreTheRealEdges checks that a community's evidence names
// the actual edges.id rows behind it. A label nobody can trace back to edges
// is exactly the opaque conclusion this item exists to avoid.
func TestEvidenceEdgeIDsAreTheRealEdges(t *testing.T) {
	db := graphDB(t)
	a := nodeIn(t, db, "src/a.go", "a")
	b := nodeIn(t, db, "src/b.go", "b")
	e1 := edgeBetween(t, db, a, b, TierCertified)
	e2 := edgeBetween(t, db, b, a, TierCertified)
	edgeBetween(t, db, a, b, "CANDIDATE") // must NOT appear as evidence

	res, err := Build(context.Background(), db, nonRepo(t), Options{})
	if err != nil {
		t.Fatalf("Build: %v", err)
	}
	if len(res.Communities) != 1 {
		t.Fatalf("want one community, got:\n%s", dump(res))
	}
	want := []string{fmt.Sprint(e1), fmt.Sprint(e2)}
	got := res.Communities[0].EvidenceEdgeIDs
	if strings.Join(got, ",") != strings.Join(want, ",") {
		t.Errorf("evidence = %v, want %v (and never the CANDIDATE edge)", got, want)
	}
}

// TestLabelIsAlwaysHeuristicAndSaysSo holds the copied discipline: the label a
// reader gets without a model is always present, always the default, and
// always attributed.
func TestLabelIsAlwaysHeuristicAndSaysSo(t *testing.T) {
	db := graphDB(t)
	twoClusterGraph(t, db, "CANDIDATE", 1)

	res, err := Build(context.Background(), db, nonRepo(t), Options{})
	if err != nil {
		t.Fatalf("Build: %v", err)
	}
	for _, c := range res.Communities {
		if c.HeuristicLabel == "" {
			t.Errorf("community %s has an empty heuristic label", c.ID)
		}
		if c.Label != c.HeuristicLabel {
			t.Errorf("label %q != heuristic label %q; nothing in this package may diverge them",
				c.Label, c.HeuristicLabel)
		}
		if c.EnrichedBy != EnrichedByHeuristic {
			t.Errorf("enriched_by = %q, want %q -- no model produced this",
				c.EnrichedBy, EnrichedByHeuristic)
		}
		if len(c.Keywords) == 0 {
			t.Errorf("community %s has no keywords", c.ID)
		}
	}
	labels := []string{res.Communities[0].HeuristicLabel, res.Communities[1].HeuristicLabel}
	sort.Strings(labels)
	if labels[0] != "lib/" || labels[1] != "src/" {
		t.Errorf("heuristic labels = %v, want [lib/ src/]", labels)
	}
}

// TestNoEdgesIsAnAnswerNotAFailure: an empty graph must come back with a
// reason, not with an empty slice a caller has to interpret.
func TestNoEdgesIsAnAnswerNotAFailure(t *testing.T) {
	db := graphDB(t)
	nodeIn(t, db, "src/lonely.go", "lonely")

	res, err := Build(context.Background(), db, nonRepo(t), Options{})
	if err != nil {
		t.Fatalf("Build: %v", err)
	}
	if res.Reason != ReasonNoEdges {
		t.Errorf("reason = %q, want %q", res.Reason, ReasonNoEdges)
	}
	if len(res.Communities) != 0 {
		t.Errorf("got %d communities from an edgeless graph", len(res.Communities))
	}
	if !math.IsNaN(res.OverallCohesion) {
		t.Errorf("overall cohesion = %v, want NaN", res.OverallCohesion)
	}
	if res.CohesionReason != CohesionHistoryUnavailable {
		t.Errorf("cohesion reason = %q, want %q", res.CohesionReason, CohesionHistoryUnavailable)
	}
}

// TestNonRepositoryYieldsHistoryUnavailable: a graph with edges but no history
// still clusters (on structure alone) and says plainly that it could not be
// falsified.
func TestNonRepositoryYieldsHistoryUnavailable(t *testing.T) {
	db := graphDB(t)
	twoClusterGraph(t, db, "CANDIDATE", 1)

	res, err := Build(context.Background(), db, nonRepo(t), Options{})
	if err != nil {
		t.Fatalf("Build: %v", err)
	}
	if res.CohesionReason != CohesionHistoryUnavailable {
		t.Fatalf("cohesion reason = %q, want %q", res.CohesionReason, CohesionHistoryUnavailable)
	}
	for _, c := range res.Communities {
		if !math.IsNaN(c.Cohesion) {
			t.Errorf("community %s reported cohesion %v with no history to measure it against",
				c.ID, c.Cohesion)
		}
		if c.CohesionInterval.N != 0 {
			t.Errorf("community %s reported n=%d with no history", c.ID, c.CohesionInterval.N)
		}
		if !strings.Contains(c.Description, "unmeasured") {
			t.Errorf("description %q hides the missing measurement", c.Description)
		}
	}
}

// holdoutRepo scripts the history the cohesion acceptance case is stated
// against.
//
// Fit window (older commits): src/a.go and src/b.go change together six
// times; lib/c.go and lib/d.go change together six times. Nothing couples the
// two groups, so the fit must find exactly {a,b} and {c,d}.
//
// Holdout (the newest two commits, never seen by the fit):
//
//	newest-1: src/a.go + src/b.go   -> inside {a,b}          HIT
//	newest:   src/a.go + lib/c.go   -> crosses two groups    MISS
//
// So {a,b} is tested twice and right once (0.5), {c,d} is tested once and
// wrong (0.0), and the partition overall scores 1/2.
func holdoutRepo(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	git(t, dir, "init", "-q")
	for i := 0; i < 6; i++ {
		commitFiles(t, dir, fmt.Sprintf("ab%d", i), "src/a.go", "src/b.go")
		commitFiles(t, dir, fmt.Sprintf("cd%d", i), "lib/c.go", "lib/d.go")
	}
	commitFiles(t, dir, "holdout-hit", "src/a.go", "src/b.go")
	commitFiles(t, dir, "holdout-miss", "src/a.go", "lib/c.go")
	return dir
}

// TestHeldOutCohesionIsMeasuredNotDescribed is the item's central claim: the
// stored cohesion is a prediction rate on commits the fit never saw, with an
// interval and a sample size, and it is allowed to be low.
func TestHeldOutCohesionIsMeasuredNotDescribed(t *testing.T) {
	db := graphDB(t) // no call edges at all: the coupling here is purely historical
	dir := holdoutRepo(t)

	res, err := Build(context.Background(), db, dir, Options{HoldoutCommits: 2})
	if err != nil {
		t.Fatalf("Build: %v", err)
	}
	if res.Reason != ReasonOK {
		t.Fatalf("reason = %q, want ok (%s)", res.Reason, dump(res))
	}
	if res.HistoryReason != "ok" {
		t.Fatalf("history reason = %q", res.HistoryReason)
	}
	if res.FitCommits != 12 || res.HoldoutCommitsUsed != 2 {
		t.Fatalf("split = %d fit / %d holdout, want 12 / 2", res.FitCommits, res.HoldoutCommitsUsed)
	}

	byLabel := map[string]Community{}
	for _, c := range res.Communities {
		byLabel[c.HeuristicLabel] = c
	}
	if len(byLabel) != 2 {
		t.Fatalf("want two communities, got:\n%s", dump(res))
	}

	// The fit must not have seen the holdout: if it had, src/a.go and lib/c.go
	// would carry co-change weight and could have been grouped together.
	src, ok := byLabel["src/"]
	if !ok {
		t.Fatalf("no src/ community:\n%s", dump(res))
	}
	if strings.Join(src.Members, ",") != "src/a.go,src/b.go" {
		t.Fatalf("src community = %v, want [src/a.go src/b.go] -- holdout leaked into the fit", src.Members)
	}
	lib := byLabel["lib/"]

	if src.CohesionReason != CohesionMeasured || src.Cohesion != 0.5 || src.CohesionInterval.N != 2 {
		t.Errorf("src cohesion = %v (n=%d, %s), want 0.5 over n=2 measured",
			src.Cohesion, src.CohesionInterval.N, src.CohesionReason)
	}
	if lib.CohesionReason != CohesionMeasured || lib.Cohesion != 0.0 || lib.CohesionInterval.N != 1 {
		t.Errorf("lib cohesion = %v (n=%d, %s), want 0.0 over n=1 measured",
			lib.Cohesion, lib.CohesionInterval.N, lib.CohesionReason)
	}
	// A 0.0 rate must still carry a real interval; a zero-width interval at
	// p=0 is exactly the normal-approximation failure Wilson avoids.
	if !(lib.CohesionInterval.Hi > 0) {
		t.Errorf("lib interval = [%v, %v]; a single miss does not prove the rate is 0",
			lib.CohesionInterval.Lo, lib.CohesionInterval.Hi)
	}
	if res.CoveredPairs != 2 || res.InsidePairs != 1 || res.OverallCohesion != 0.5 {
		t.Errorf("overall = %v (%d/%d), want 0.5 (1/2)",
			res.OverallCohesion, res.InsidePairs, res.CoveredPairs)
	}
	if res.CohesionReason != CohesionMeasured {
		t.Errorf("overall cohesion reason = %q", res.CohesionReason)
	}
}

// TestUntestedCommunityIsNaNNotZero: a community no held-out commit touched
// must be absent, not scored zero. Zero is a measurement; absence is not.
func TestUntestedCommunityIsNaNNotZero(t *testing.T) {
	dir := t.TempDir()
	git(t, dir, "init", "-q")
	for i := 0; i < 4; i++ {
		commitFiles(t, dir, fmt.Sprintf("ab%d", i), "src/a.go", "src/b.go")
		commitFiles(t, dir, fmt.Sprintf("cd%d", i), "lib/c.go", "lib/d.go")
	}
	// The single holdout commit touches only the src group.
	commitFiles(t, dir, "holdout", "src/a.go", "src/b.go")

	res, err := Build(context.Background(), graphDB(t), dir, Options{HoldoutCommits: 1})
	if err != nil {
		t.Fatalf("Build: %v", err)
	}
	var lib Community
	found := false
	for _, c := range res.Communities {
		if c.HeuristicLabel == "lib/" {
			lib, found = c, true
		}
	}
	if !found {
		t.Fatalf("no lib/ community:\n%s", dump(res))
	}
	if !math.IsNaN(lib.Cohesion) {
		t.Errorf("untested community scored %v; it must be NaN", lib.Cohesion)
	}
	if lib.CohesionReason != CohesionNoHoldoutPairs {
		t.Errorf("reason = %q, want %q", lib.CohesionReason, CohesionNoHoldoutPairs)
	}
	if lib.CohesionInterval.N != 0 {
		t.Errorf("n = %d, want 0", lib.CohesionInterval.N)
	}
}

// TestHoldoutDisabledIsNamed: switching the falsification off must be visible
// in the output, not inferred from a field being empty.
func TestHoldoutDisabledIsNamed(t *testing.T) {
	res, err := Build(context.Background(), graphDB(t), holdoutRepo(t), Options{HoldoutCommits: -1})
	if err != nil {
		t.Fatalf("Build: %v", err)
	}
	if res.Options.HoldoutCommits != 0 {
		t.Fatalf("effective holdout = %d, want 0", res.Options.HoldoutCommits)
	}
	if res.CohesionReason != CohesionHoldoutDisabled {
		t.Errorf("reason = %q, want %q", res.CohesionReason, CohesionHoldoutDisabled)
	}
	for _, c := range res.Communities {
		if !math.IsNaN(c.Cohesion) {
			t.Errorf("community %s scored %v with the holdout disabled", c.ID, c.Cohesion)
		}
	}
}

// TestMassCommitIsSkippedAndCounted: the mass-change cap applies to the
// holdout as well as the fit, and a skipped commit is counted so a truncated
// measurement is visibly truncated.
func TestMassCommitIsSkippedAndCounted(t *testing.T) {
	dir := t.TempDir()
	git(t, dir, "init", "-q")
	for i := 0; i < 4; i++ {
		commitFiles(t, dir, fmt.Sprintf("ab%d", i), "src/a.go", "src/b.go")
	}
	mass := make([]string, 0, 12)
	for i := 0; i < 12; i++ {
		mass = append(mass, fmt.Sprintf("gen/m%d.go", i))
	}
	commitFiles(t, dir, "mass", mass...)

	res, err := Build(context.Background(), graphDB(t), dir, Options{
		HoldoutCommits: 1, MaxFilesPerCommit: 5,
	})
	if err != nil {
		t.Fatalf("Build: %v", err)
	}
	if res.HoldoutSkipped != 1 {
		t.Errorf("holdout_skipped = %d, want 1", res.HoldoutSkipped)
	}
	if res.HoldoutCommitsUsed != 0 {
		t.Errorf("holdout_used = %d, want 0", res.HoldoutCommitsUsed)
	}
	if res.CohesionReason != CohesionNoHoldoutCommits {
		t.Errorf("reason = %q, want %q", res.CohesionReason, CohesionNoHoldoutCommits)
	}
}

// TestUncoveredPairsAreExcludedAndCounted: a pair with an unclustered endpoint
// is not charged against the partition, and the exclusion is sized so it can
// be argued with.
func TestUncoveredPairsAreExcludedAndCounted(t *testing.T) {
	dir := t.TempDir()
	git(t, dir, "init", "-q")
	for i := 0; i < 5; i++ {
		commitFiles(t, dir, fmt.Sprintf("ab%d", i), "src/a.go", "src/b.go")
	}
	// The holdout couples a clustered file with one the fit never saw.
	commitFiles(t, dir, "holdout", "src/a.go", "README.md")

	res, err := Build(context.Background(), graphDB(t), dir, Options{HoldoutCommits: 1})
	if err != nil {
		t.Fatalf("Build: %v", err)
	}
	if res.UncoveredPairs != 1 {
		t.Errorf("uncovered_pairs = %d, want 1", res.UncoveredPairs)
	}
	if res.CoveredPairs != 0 {
		t.Errorf("covered_pairs = %d, want 0", res.CoveredPairs)
	}
	if res.CohesionReason != CohesionNoHoldoutPairs {
		t.Errorf("reason = %q, want %q", res.CohesionReason, CohesionNoHoldoutPairs)
	}
}

// TestOptionsZeroMeansDefaultNotUnlimited holds the shared bounding contract.
func TestOptionsZeroMeansDefaultNotUnlimited(t *testing.T) {
	got := Options{}.Normalize()
	want := Options{
		WCall:             DefaultWCall,
		WCochange:         DefaultWCochange,
		Resolution:        DefaultResolution,
		MaxCommunities:    DefaultMaxCommunities,
		MinMembers:        DefaultMinMembers,
		HoldoutCommits:    DefaultHoldoutCommits,
		MaxCommits:        DefaultMaxCommits,
		MaxFilesPerCommit: DefaultMaxFilesPerCommit,
		MaxIterations:     DefaultMaxIterations,
		MaxEvidenceEdges:  DefaultMaxEvidenceEdges,
	}
	if got != want {
		t.Errorf("Normalize() = %+v, want %+v", got, want)
	}
	// Normalize must be idempotent, or a caller that records the effective
	// options and passes them back would drift.
	if again := got.Normalize(); again != got {
		t.Errorf("Normalize is not idempotent: %+v then %+v", got, again)
	}
	// A negative holdout is the only way to spell "disabled"; everything else
	// bounds work rather than removing the bound.
	if (Options{HoldoutCommits: -5}).Normalize().HoldoutCommits != 0 {
		t.Error("a negative holdout must normalise to 0 (disabled)")
	}
}

// TestWilsonMatchesPublishedValues pins the interval arithmetic against three
// textbook cases, so a refactor cannot quietly turn it into the normal
// approximation.
func TestWilsonMatchesPublishedValues(t *testing.T) {
	cases := []struct {
		k, n           int
		wantLo, wantHi float64
	}{
		{1, 2, 0.0945312, 0.9054688},
		{0, 10, 0.0000000, 0.2775328},
		{90, 100, 0.8256343, 0.9447709},
	}
	for _, c := range cases {
		lo, hi := wilson(c.k, c.n)
		if math.Abs(lo-c.wantLo) > 1e-5 || math.Abs(hi-c.wantHi) > 1e-5 {
			t.Errorf("wilson(%d,%d) = [%.6f, %.6f], want [%.6f, %.6f]",
				c.k, c.n, lo, hi, c.wantLo, c.wantHi)
		}
	}
	if lo, hi := wilson(0, 0); !math.IsNaN(lo) || !math.IsNaN(hi) {
		t.Errorf("wilson(0,0) = [%v, %v], want NaN -- there is no interval around no observations", lo, hi)
	}
}

// dump renders a Result for a failure message. Test diagnostics that do not
// show the partition make a clustering failure unreadable.
func dump(res Result) string {
	var b strings.Builder
	fmt.Fprintf(&b, "  reason=%s files=%d call_pairs=%d cochange_pairs=%d dropped=%d\n",
		res.Reason, res.Files, res.CallEdges, res.CochangeEdges, res.CommunitiesDropped)
	for _, c := range res.Communities {
		fmt.Fprintf(&b, "  [%s] %v cohesion=%v(%s) structural=%.3f\n",
			c.HeuristicLabel, c.Members, c.Cohesion, c.CohesionReason, c.StructuralCohesion)
	}
	return b.String()
}

// TestCohesionSummaryNeverPrintsNaNAsANumber guards the one receipt-formatting
// mistake that would undo the whole "never fabricated" promise.
func TestCohesionSummaryNeverPrintsNaNAsANumber(t *testing.T) {
	absent := Result{OverallCohesion: math.NaN(), CohesionReason: CohesionNoHoldoutCommits}.CohesionSummary()
	if absent != "absent:"+CohesionNoHoldoutCommits {
		t.Errorf("CohesionSummary() = %q, want absent:%s", absent, CohesionNoHoldoutCommits)
	}
	if strings.Contains(absent, "NaN") || strings.Contains(absent, "0.0000") {
		t.Errorf("CohesionSummary() = %q leaks a number for an absent rate", absent)
	}
	unknown := Result{OverallCohesion: math.NaN()}.CohesionSummary()
	if unknown != "absent:unknown" {
		t.Errorf("CohesionSummary() with no reason = %q, want absent:unknown", unknown)
	}
	measured := Result{
		OverallCohesion:         0.4585,
		OverallCohesionInterval: Interval{Lo: 0.4277, Hi: 0.4897, N: 988},
		CohesionReason:          CohesionMeasured,
	}.CohesionSummary()
	if measured != "0.4585[0.4277,0.4897]n=988" {
		t.Errorf("CohesionSummary() = %q", measured)
	}
}
