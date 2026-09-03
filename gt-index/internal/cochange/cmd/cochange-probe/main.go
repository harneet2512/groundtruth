// Command cochange-probe reports what the cochange extractor would find for a
// repository, without touching a graph. It exists so a measurement against a
// real checkout is reproducible from the command line and does not require
// wiring the extractor into gt-index first.
//
// Usage:
//
//	cochange-probe -root <repo> [-max-commits N] [-max-files N] [-min-support N] [-top N]
package main

import (
	"context"
	"flag"
	"fmt"
	"os"
	"sort"

	"github.com/harneet2512/groundtruth/gt-index/internal/cochange"
)

func main() {
	root := flag.String("root", ".", "repository root to walk")
	maxCommits := flag.Int("max-commits", 0, "history window (0 = package default)")
	maxFiles := flag.Int("max-files", 0, "per-commit file cap (0 = package default)")
	minSupport := flag.Int("min-support", 0, "minimum support to keep a pair (0 = package default)")
	top := flag.Int("top", 10, "how many pairs to print, ranked by support")
	flag.Parse()

	res, err := cochange.Extract(context.Background(), *root, cochange.Options{
		MaxCommits:        *maxCommits,
		MaxFilesPerCommit: *maxFiles,
		MinSupport:        *minSupport,
	})
	if err != nil {
		fmt.Fprintf(os.Stderr, "cochange-probe: %v\n", err)
		os.Exit(1)
	}

	fmt.Printf("root                      = %s\n", *root)
	fmt.Printf("reason                    = %s\n", res.Reason)
	fmt.Printf("shallow_checkout          = %t\n", res.Shallow)
	fmt.Printf("effective_max_commits     = %d\n", res.Options.MaxCommits)
	fmt.Printf("effective_max_files       = %d\n", res.Options.MaxFilesPerCommit)
	fmt.Printf("effective_min_support     = %d\n", res.Options.MinSupport)
	fmt.Printf("worst_case_pair_bound     = %d\n", res.Options.MaxPairs())
	fmt.Printf("commits_scanned           = %d\n", res.CommitsScanned)
	fmt.Printf("commits_skipped_too_large = %d\n", res.CommitsSkippedTooLarge)
	fmt.Printf("window_start_oldest       = %s\n", res.WindowStart)
	fmt.Printf("window_end_newest         = %s\n", res.WindowEnd)
	fmt.Printf("files_touched             = %d\n", res.FilesTouched)
	fmt.Printf("pairs                     = %d\n", len(res.Pairs))

	if len(res.Pairs) == 0 {
		return
	}

	// Rank a copy: res.Pairs stays in its deterministic (FileA, FileB) order.
	ranked := make([]cochange.Pair, len(res.Pairs))
	copy(ranked, res.Pairs)
	sort.SliceStable(ranked, func(i, j int) bool {
		if ranked[i].Support != ranked[j].Support {
			return ranked[i].Support > ranked[j].Support
		}
		if ranked[i].FileA != ranked[j].FileA {
			return ranked[i].FileA < ranked[j].FileA
		}
		return ranked[i].FileB < ranked[j].FileB
	})
	n := *top
	if n > len(ranked) {
		n = len(ranked)
	}
	fmt.Printf("\ntop %d by support (support  conf(a->b)  conf(b->a)  file_a  file_b):\n", n)
	for _, p := range ranked[:n] {
		fmt.Printf("  %4d  %.3f  %.3f  %s  %s\n", p.Support, p.ConfidenceAToB, p.ConfidenceBToA, p.FileA, p.FileB)
	}
}
