// Command community-probe reports the communities the clusterer finds for a
// real graph and checkout, without publishing anything. It exists so a
// measurement against a real fixture is reproducible from the command line and
// does not require wiring the clusterer into gt-index first, and so a
// resolution sweep can be run and argued about.
//
// It opens the database READ-ONLY. The probe cannot modify a graph.
//
// Usage:
//
//	community-probe -db <graph.db> -root <repo> [-resolution G] [-holdout N]
//	                [-w-call W] [-w-cochange W] [-min-members N] [-top N]
//	                [-sweep 0.05,0.25,1]
package main

import (
	"context"
	"database/sql"
	"flag"
	"fmt"
	"math"
	"os"
	"sort"
	"strconv"
	"strings"

	_ "github.com/mattn/go-sqlite3"

	"github.com/harneet2512/groundtruth/gt-index/internal/community"
)

func main() {
	dbPath := flag.String("db", "", "graph database to read (opened read-only)")
	root := flag.String("root", ".", "repository root the graph was built from")
	resolution := flag.Float64("resolution", 0, "CPM gamma (0 = package default)")
	holdout := flag.Int("holdout", 0, "commits withheld from the fit (0 = package default, negative = disabled)")
	wCall := flag.Float64("w-call", 0, "weight per CERTIFIED CALLS edge (0 = package default)")
	wCochange := flag.Float64("w-cochange", 0, "weight per unit of co-change support (0 = package default)")
	minMembers := flag.Int("min-members", 0, "minimum community size (0 = package default)")
	maxCommunities := flag.Int("max-communities", 0, "cap on published communities (0 = package default)")
	top := flag.Int("top", 5, "how many communities to print, largest first")
	sweep := flag.String("sweep", "", "comma-separated resolutions to sweep instead of a single run")
	flag.Parse()

	if *dbPath == "" {
		fmt.Fprintln(os.Stderr, "community-probe: -db is required")
		os.Exit(2)
	}

	db, err := sql.Open("sqlite3", "file:"+*dbPath+"?mode=ro")
	if err != nil {
		fmt.Fprintf(os.Stderr, "community-probe: open %s: %v\n", *dbPath, err)
		os.Exit(1)
	}
	defer db.Close()

	base := community.Options{
		WCall:          *wCall,
		WCochange:      *wCochange,
		Resolution:     *resolution,
		MinMembers:     *minMembers,
		MaxCommunities: *maxCommunities,
		HoldoutCommits: *holdout,
	}

	if *sweep != "" {
		runSweep(db, *root, base, *sweep)
		return
	}

	res, err := community.Build(context.Background(), db, *root, base)
	if err != nil {
		fmt.Fprintf(os.Stderr, "community-probe: %v\n", err)
		os.Exit(1)
	}
	report(res, *top)
}

func runSweep(db *sql.DB, root string, base community.Options, spec string) {
	fmt.Printf("%-12s %-12s %-9s %-9s %-8s %-8s %-22s\n",
		"resolution", "communities", "clustered", "largest", "median", "chance", "held_out_cohesion")
	for _, field := range strings.Split(spec, ",") {
		g, err := strconv.ParseFloat(strings.TrimSpace(field), 64)
		if err != nil {
			fmt.Fprintf(os.Stderr, "community-probe: bad resolution %q: %v\n", field, err)
			os.Exit(2)
		}
		opts := base
		opts.Resolution = g
		res, err := community.Build(context.Background(), db, root, opts)
		if err != nil {
			fmt.Fprintf(os.Stderr, "community-probe: %v\n", err)
			os.Exit(1)
		}
		sizes := sizesOf(res)
		clustered := 0
		for _, s := range sizes {
			clustered += s
		}
		fmt.Printf("%-12g %-12d %-9d %-9d %-8d %-8.4f %s\n",
			g, len(res.Communities), clustered, maxOf(sizes), medianOf(sizes), chanceRate(sizes), cohesionCell(res))
	}
}

func report(res community.Result, top int) {
	opts := res.Options
	fmt.Printf("reason                    = %s\n", res.Reason)
	fmt.Printf("algorithm                 = %s\n", res.Algorithm)
	fmt.Printf("resolution_gamma          = %g\n", opts.Resolution)
	fmt.Printf("w_call / w_cochange       = %g / %g\n", opts.WCall, opts.WCochange)
	fmt.Printf("min_members               = %d\n", opts.MinMembers)
	fmt.Printf("max_communities           = %d\n", opts.MaxCommunities)
	fmt.Println()
	fmt.Printf("certified_call_rows       = %d\n", res.CertifiedCallRows)
	fmt.Printf("excluded_call_rows        = %d   (CANDIDATE/SPECULATIVE/STRUCTURAL)\n", res.ExcludedCallRows)
	fmt.Printf("files_in_graph            = %d\n", res.Files)
	fmt.Printf("call_weighted_pairs       = %d\n", res.CallEdges)
	fmt.Printf("cochange_weighted_pairs   = %d\n", res.CochangeEdges)
	fmt.Println()
	fmt.Printf("history_reason            = %s\n", res.HistoryReason)
	fmt.Printf("shallow_checkout          = %t\n", res.Shallow)
	fmt.Printf("fit_commits               = %d (skipped too large: %d)\n", res.FitCommits, res.FitSkipped)
	fmt.Printf("holdout_commits_used      = %d (skipped too large: %d)\n", res.HoldoutCommitsUsed, res.HoldoutSkipped)
	fmt.Printf("holdout_window            = %s .. %s\n", shortSHA(res.HoldoutOldest), shortSHA(res.HoldoutNewest))
	fmt.Println()

	sizes := sizesOf(res)
	clustered := 0
	for _, s := range sizes {
		clustered += s
	}
	fmt.Printf("communities               = %d\n", len(res.Communities))
	fmt.Printf("communities_dropped       = %d\n", res.CommunitiesDropped)
	fmt.Printf("files_clustered           = %d\n", clustered)
	if len(sizes) > 0 {
		fmt.Printf("size_min/median/max       = %d / %d / %d\n", minOf(sizes), medianOf(sizes), maxOf(sizes))
		fmt.Printf("size_histogram            = %s\n", histogram(sizes))
	}
	fmt.Println()
	fmt.Printf("structural_cohesion_mean  = %s\n", meanStructural(res))
	fmt.Printf("held_out_cohesion         = %s\n", cohesionCell(res))
	fmt.Printf("chance_rate_same_size     = %.4f   (what this size distribution scores if held-out pairs were drawn uniformly)\n",
		chanceRate(sizes))
	fmt.Printf("held_out_pairs            = inside %d / covered %d (uncovered, excluded: %d)\n",
		res.InsidePairs, res.CoveredPairs, res.UncoveredPairs)
	fmt.Printf("cohesion_reason           = %s\n", res.CohesionReason)
	measured, unmeasured := countMeasured(res)
	fmt.Printf("communities_measured      = %d measured / %d unmeasured\n", measured, unmeasured)
	fmt.Println()

	if len(res.Communities) == 0 {
		return
	}
	fmt.Printf("top %d communities by size:\n", top)
	for i, c := range res.Communities {
		if i >= top {
			break
		}
		fmt.Printf("  %2d. %-38s n=%-4d structural=%.3f  held_out=%s\n",
			i+1, c.HeuristicLabel, len(c.Members), c.StructuralCohesion, communityCohesion(c))
		fmt.Printf("      keywords=%v\n", c.Keywords)
		fmt.Printf("      evidence_edges=%d%s\n", len(c.EvidenceEdgeIDs), truncNote(c))
	}
}

// chanceRate is the reference a held-out cohesion must be read against: the
// probability that two files drawn uniformly at random from the CLUSTERED file
// set land in the same community, given only this partition's size
// distribution. A partition that puts every file in one community scores 1.0
// on held-out cohesion and 1.0 here, and the comparison is what exposes that
// as a trivial result rather than a good one.
//
// It is a reference, not a p-value: held-out pairs are not drawn uniformly
// (active files pair more often), so this understates what a size-matched
// random partition would achieve on real commit traffic.
func chanceRate(sizes []int) float64 {
	total := 0
	for _, s := range sizes {
		total += s
	}
	if total < 2 {
		return math.NaN()
	}
	same := 0
	for _, s := range sizes {
		same += s * (s - 1)
	}
	return float64(same) / float64(total*(total-1))
}

func sizesOf(res community.Result) []int {
	out := make([]int, 0, len(res.Communities))
	for _, c := range res.Communities {
		out = append(out, len(c.Members))
	}
	sort.Ints(out)
	return out
}

func minOf(s []int) int {
	if len(s) == 0 {
		return 0
	}
	return s[0]
}

func maxOf(s []int) int {
	if len(s) == 0 {
		return 0
	}
	return s[len(s)-1]
}

func medianOf(s []int) int {
	if len(s) == 0 {
		return 0
	}
	return s[len(s)/2]
}

// histogram buckets community sizes so a long tail of pairs is visible rather
// than averaged away.
func histogram(sizes []int) string {
	buckets := []struct {
		label string
		lo    int
		hi    int
	}{
		{"2", 2, 2}, {"3-4", 3, 4}, {"5-9", 5, 9}, {"10-19", 10, 19},
		{"20-49", 20, 49}, {"50-99", 50, 99}, {"100+", 100, 1 << 30},
	}
	parts := make([]string, 0, len(buckets))
	for _, b := range buckets {
		n := 0
		for _, s := range sizes {
			if s >= b.lo && s <= b.hi {
				n++
			}
		}
		if n > 0 {
			parts = append(parts, fmt.Sprintf("%s:%d", b.label, n))
		}
	}
	return strings.Join(parts, " ")
}

func meanStructural(res community.Result) string {
	if len(res.Communities) == 0 {
		return "n/a"
	}
	sum := 0.0
	for _, c := range res.Communities {
		sum += c.StructuralCohesion
	}
	return fmt.Sprintf("%.4f over %d communities", sum/float64(len(res.Communities)), len(res.Communities))
}

func countMeasured(res community.Result) (int, int) {
	measured := 0
	for _, c := range res.Communities {
		if !math.IsNaN(c.Cohesion) {
			measured++
		}
	}
	return measured, len(res.Communities) - measured
}

func cohesionCell(res community.Result) string {
	if math.IsNaN(res.OverallCohesion) {
		return "absent (" + res.CohesionReason + ")"
	}
	return fmt.Sprintf("%.4f  95%% CI [%.4f, %.4f]  n=%d",
		res.OverallCohesion, res.OverallCohesionInterval.Lo, res.OverallCohesionInterval.Hi,
		res.OverallCohesionInterval.N)
}

func communityCohesion(c community.Community) string {
	if math.IsNaN(c.Cohesion) {
		return "absent (" + c.CohesionReason + ")"
	}
	return fmt.Sprintf("%.3f [%.3f, %.3f] n=%d",
		c.Cohesion, c.CohesionInterval.Lo, c.CohesionInterval.Hi, c.CohesionInterval.N)
}

func truncNote(c community.Community) string {
	if c.EvidenceTruncated {
		return " (TRUNCATED at the evidence cap)"
	}
	return ""
}

func shortSHA(s string) string {
	if len(s) > 12 {
		return s[:12]
	}
	if s == "" {
		return "(none)"
	}
	return s
}
