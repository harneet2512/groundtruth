package community

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"os/exec"
	"sort"
	"strconv"
	"strings"

	"github.com/harneet2512/groundtruth/gt-index/internal/cochange"
)

// This file reads git history ONCE and splits it into a fit window and a
// holdout window.
//
// # WHY IT DOES NOT CALL cochange.Extract
//
// internal/cochange is the right extractor and its Pair type is reused
// verbatim below, but its window always ends at HEAD: there is no way to ask
// it for "the commits older than the newest 50". Fitting communities on
// cochange.Extract's output and then measuring them against the newest 50
// commits would test the partition on commits it was fitted on -- the
// measurement would be leakage, and a leaked cohesion is worse than no
// cohesion because it looks like evidence.
//
// So the split happens here, in one walk, and the fit side reproduces
// cochange's support/confidence arithmetic exactly against the OLDER commits
// only. The vocabulary (cochange.Reason*) and the bounds
// (cochange.Default*) are shared, so the two agree wherever they can.
// Duplicating the walk is the price of a clean holdout; it is stated rather
// than hidden.

// commitRecord is one parsed commit: its sha and the files it touched.
type commitRecord struct {
	SHA   string
	Files []string
}

// history is the split history behind one Build.
type history struct {
	// FitPairs is the co-change evidence from the OLDER commits only, in
	// cochange.Pair form so the two packages agree on what a pair is.
	FitPairs []cochange.Pair
	// Holdout are the newest commits, NEWEST FIRST, that survived the
	// mass-change cap and touch at least two files. These are the commits the
	// partition is tested against and has never seen.
	Holdout []commitRecord
	// FitCommits and FitSkipped size the fit window after the cap.
	FitCommits int
	FitSkipped int
	// HoldoutSkipped counts holdout commits refused by the mass-change cap.
	HoldoutSkipped int
	// HoldoutOldest and HoldoutNewest bound the falsification window.
	HoldoutOldest string
	HoldoutNewest string
	// Reason reuses cochange's vocabulary; ReasonOK means the walk answered.
	Reason string
	// Shallow records a grafted checkout independently of whether it cost
	// anything.
	Shallow bool
}

// loadHistory walks HoldoutCommits+MaxCommits commits from HEAD, newest first,
// and splits them at the holdout boundary.
//
// Like cochange.Extract, it returns a non-nil error only when git failed
// unexpectedly. A repository that cannot answer is a successful call with an
// explicit Reason and no pairs.
func loadHistory(ctx context.Context, repoRoot string, opts Options) (history, error) {
	h := history{Reason: cochange.ReasonOK}

	if _, err := runGit(ctx, repoRoot, "rev-parse", "--git-dir"); err != nil {
		if errors.Is(err, exec.ErrNotFound) {
			h.Reason = cochange.ReasonGitUnavailable
			return h, nil
		}
		h.Reason = cochange.ReasonNotARepository
		return h, nil
	}
	if shallow, err := runGit(ctx, repoRoot, "rev-parse", "--is-shallow-repository"); err == nil {
		h.Shallow = strings.EqualFold(strings.TrimSpace(shallow), "true")
	}
	if _, err := runGit(ctx, repoRoot, "rev-parse", "--verify", "HEAD"); err != nil {
		h.Reason = cochange.ReasonNoHistory
		return h, nil
	}

	want := opts.MaxCommits + opts.HoldoutCommits
	out, err := runGit(ctx, repoRoot,
		"-c", "core.quotePath=false",
		"log", "--name-only", "--format=%H", "--no-merges",
		"-n", strconv.Itoa(want),
	)
	if err != nil {
		h.Reason = cochange.ReasonGitLogFailed
		return h, fmt.Errorf("community: git log in %s: %w", repoRoot, err)
	}

	commits := parseLog(out)
	if len(commits) == 0 {
		h.Reason = cochange.ReasonNoHistory
		return h, nil
	}

	// Same graft rule as cochange.Extract, applied to the FIT window: a
	// shallow checkout that still filled the fit window answers exactly as a
	// full clone would. A shallow checkout that did not is the depth-1 task
	// container, whose support counts understate the truth by an unknown
	// factor, so it yields no pairs and says why.
	//
	// The threshold is the FIT window, not want: a repository with 501 real
	// commits and a 50-commit holdout has a complete 451-commit fit window
	// even though git returned fewer than 550 commits. Requiring `want` here
	// would refuse every repository shorter than the sum of the two windows.
	if h.Shallow && len(commits) < opts.MaxCommits {
		h.Reason = cochange.ReasonShallowClone
		return h, nil
	}

	// commits is newest first, so the holdout is the prefix.
	cut := opts.HoldoutCommits
	if cut > len(commits) {
		cut = len(commits)
	}
	holdout := commits[:cut]
	fit := commits[cut:]

	for _, c := range holdout {
		if len(c.Files) > opts.MaxFilesPerCommit {
			h.HoldoutSkipped++
			continue
		}
		if len(c.Files) < 2 {
			continue // a single-file commit predicts no pair
		}
		h.Holdout = append(h.Holdout, c)
	}
	if len(h.Holdout) > 0 {
		h.HoldoutNewest = h.Holdout[0].SHA
		h.HoldoutOldest = h.Holdout[len(h.Holdout)-1].SHA
	}

	h.FitPairs, h.FitCommits, h.FitSkipped = pairsFrom(fit, opts)
	return h, nil
}

// pairsFrom reproduces cochange.Extract's support and confidence arithmetic
// over an explicit commit slice. Pairs come back sorted by (FileA, FileB), so
// two runs over the same history are byte-identical.
func pairsFrom(commits []commitRecord, opts Options) ([]cochange.Pair, int, int) {
	support := make(map[[2]string]int)
	perFile := make(map[string]int)
	scanned, skipped := 0, 0
	for _, c := range commits {
		if len(c.Files) > opts.MaxFilesPerCommit {
			skipped++
			continue
		}
		scanned++
		for _, f := range c.Files {
			perFile[f]++
		}
		for i := 0; i < len(c.Files); i++ {
			for j := i + 1; j < len(c.Files); j++ {
				support[orderPair(c.Files[i], c.Files[j])]++
			}
		}
	}
	pairs := make([]cochange.Pair, 0, len(support))
	for key, n := range support {
		a, b := key[0], key[1]
		pairs = append(pairs, cochange.Pair{
			FileA:          a,
			FileB:          b,
			Support:        n,
			CommitsA:       perFile[a],
			CommitsB:       perFile[b],
			ConfidenceAToB: safeRatio(n, perFile[a]),
			ConfidenceBToA: safeRatio(n, perFile[b]),
		})
	}
	sort.Slice(pairs, func(i, j int) bool {
		if pairs[i].FileA != pairs[j].FileA {
			return pairs[i].FileA < pairs[j].FileA
		}
		return pairs[i].FileB < pairs[j].FileB
	})
	return pairs, scanned, skipped
}

// parseLog reads `git log --name-only --format=%H` output. It is the same
// shape-and-position rule internal/cochange uses: only a bare 40- or
// 64-character lowercase hex line followed by a blank line, another header, or
// end of input is a header.
func parseLog(out string) []commitRecord {
	lines := strings.Split(strings.ReplaceAll(out, "\r\n", "\n"), "\n")
	var commits []commitRecord
	var cur *commitRecord
	seen := map[string]bool{}
	for i, line := range lines {
		if line == "" {
			continue
		}
		next := ""
		if i+1 < len(lines) {
			next = lines[i+1]
		}
		if isHexLine(line) && (next == "" || isHexLine(next)) {
			commits = append(commits, commitRecord{SHA: line})
			cur = &commits[len(commits)-1]
			seen = map[string]bool{}
			continue
		}
		if cur == nil {
			continue
		}
		path := strings.TrimRight(line, " \t")
		if path == "" || seen[path] {
			continue
		}
		seen[path] = true
		cur.Files = append(cur.Files, path)
	}
	return commits
}

// isHexLine reports whether s is exactly a lowercase SHA-1 or SHA-256 hex
// object name.
func isHexLine(s string) bool {
	if len(s) != 40 && len(s) != 64 {
		return false
	}
	for i := 0; i < len(s); i++ {
		c := s[i]
		if (c < '0' || c > '9') && (c < 'a' || c > 'f') {
			return false
		}
	}
	return true
}

// orderPair returns a pair's single canonical spelling.
func orderPair(a, b string) [2]string {
	if a > b {
		a, b = b, a
	}
	return [2]string{a, b}
}

// safeRatio guards the division so an impossible zero denominator yields 0
// rather than NaN.
func safeRatio(n, d int) float64 {
	if d <= 0 {
		return 0
	}
	return float64(n) / float64(d)
}

// runGit executes git in repoRoot and returns its stdout. Stderr is folded
// into the error so a failure names its cause.
func runGit(ctx context.Context, repoRoot string, args ...string) (string, error) {
	full := append([]string{"-C", repoRoot}, args...)
	cmd := exec.CommandContext(ctx, "git", full...)
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		msg := strings.TrimSpace(stderr.String())
		if msg == "" {
			return "", err
		}
		return "", fmt.Errorf("%w: %s", err, msg)
	}
	return stdout.String(), nil
}
