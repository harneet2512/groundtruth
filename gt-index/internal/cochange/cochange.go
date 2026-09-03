// Package cochange derives evolutionary coupling — which files are changed
// together — from a repository's own git history, and persists it to the
// graph's `cochanges` table.
//
// # WHY THIS EXISTS
//
// The `cochanges` table has been in the schema since the graph was first
// written and is empty in every graph ever built, so the downstream
// `cochange_prior` feature has never had a row to fire on. Co-change is the
// one signal a snapshot-shaped store structurally cannot hold: it is not a
// property of the code as it stands, it is a property of how the code was
// edited. Evolutionary coupling is also known not to be congruent with
// structural coupling — two files that change together need not call each
// other — which is exactly why it is worth storing alongside the call graph
// rather than derived from it.
//
// # WHY IT IS BOUNDED BY CONSTRUCTION
//
// A commit that touches n files contributes n(n-1)/2 pairs. Unbounded, a
// single mass refactor, vendor drop or generated-code sweep produces a
// quadratic blow-up that dwarfs the rest of the graph and buries the real
// signal — a 5,000-file commit alone is 12.5 million pairs. Both bounds are
// therefore mandatory:
//
//   - Options.MaxCommits caps how far back the window reaches.
//   - Options.MaxFilesPerCommit skips any commit above the cap, and every
//     skipped commit is COUNTED in Result.CommitsSkippedTooLarge, so a
//     truncated result is visibly truncated.
//
// A zero field means "use the default", NOT "unlimited". Unbounded extraction
// is precisely the failure mode this package exists to prevent, so there is
// deliberately no way to spell it. The worst case is therefore fixed at
// construction time:
//
//	MaxCommits * MaxFilesPerCommit*(MaxFilesPerCommit-1)/2
//	= 500 * 1225 = 612,500 pairs at the defaults.
//
// # WHY AN EMPTY RESULT IS NEVER SILENT
//
// The historical implementation returned 0 with no explanation, so "this repo
// has no coupling", "this checkout has no history" and "git is not installed"
// were indistinguishable — and the second is the common case, because task
// containers and smoke fixtures are depth-1 fetches. Every return path here
// sets Result.Reason. A caller can always tell an answer from an abstention.
//
// Zero new go.mod dependencies — stdlib and the git binary the indexer already
// requires.
package cochange

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"os/exec"
	"sort"
	"strconv"
	"strings"
)

// DefaultMaxCommits bounds the history window. 500 commits is deep enough for
// a coupling signal to be statistically visible on an active repository and
// shallow enough that the git invocation stays well under a second on the
// repositories the indexer runs against.
const DefaultMaxCommits = 500

// DefaultMaxFilesPerCommit is the mass-change cutoff. Above roughly 50 files a
// commit is rarely a coherent edit — it is a reformat, a dependency bump, a
// license-header sweep or a generated-code refresh — and its pairs are noise
// that also happens to dominate the pair count quadratically.
const DefaultMaxFilesPerCommit = 50

// DefaultMinSupport keeps every observed pair. It is 1 rather than a higher
// threshold because a repository with two commits in the window must still
// produce rows; raising the floor is the caller's decision, made explicit in
// Options rather than hidden inside the extractor.
const DefaultMinSupport = 1

// Reasons for a Result. Exactly one is always set, including on success, so
// that "no pairs" is never ambiguous.
const (
	// ReasonOK means the window was scanned and the pairs are the answer.
	ReasonOK = "ok"
	// ReasonNotARepository means repoRoot is not inside a git work tree.
	ReasonNotARepository = "not_a_repository"
	// ReasonGitUnavailable means the git binary could not be executed.
	ReasonGitUnavailable = "git_unavailable"
	// ReasonShallowClone means the checkout is a shallow (grafted) clone whose
	// graft boundary falls INSIDE the requested window. Its history is
	// truncated by whoever fetched it, not by the repository, so any support
	// count derived from it understates the truth by an unknown factor.
	// Publishing a number that looks like evidence and is not is worse than
	// publishing nothing, so this yields zero pairs.
	//
	// A shallow clone deep enough to fill the window is NOT this case; see
	// Result.Shallow.
	ReasonShallowClone = "shallow_clone"
	// ReasonNoHistory means git log produced no commits in the window.
	ReasonNoHistory = "no_history"
	// ReasonGitLogFailed means the history walk itself failed after the
	// repository checks passed. Accompanied by a non-nil error.
	ReasonGitLogFailed = "git_log_failed"
)

// Options bounds a single extraction. Normalize replaces every zero field with
// its default; zero never means unlimited.
type Options struct {
	// MaxCommits is the size of the history window. 0 uses DefaultMaxCommits.
	MaxCommits int
	// MaxFilesPerCommit skips (and counts) any commit touching more files.
	// 0 uses DefaultMaxFilesPerCommit.
	MaxFilesPerCommit int
	// MinSupport drops pairs observed in fewer than this many commits.
	// 0 uses DefaultMinSupport.
	MinSupport int
}

// Normalize returns the effective options, replacing every zero or negative
// field with its default. It is exported so a caller can record the bounds
// actually in force rather than the bounds it asked for.
func (o Options) Normalize() Options {
	if o.MaxCommits <= 0 {
		o.MaxCommits = DefaultMaxCommits
	}
	if o.MaxFilesPerCommit <= 0 {
		o.MaxFilesPerCommit = DefaultMaxFilesPerCommit
	}
	if o.MinSupport <= 0 {
		o.MinSupport = DefaultMinSupport
	}
	return o
}

// MaxPairs is the number of pairs this configuration can produce in the worst
// case. It is computable before the walk runs, which is what "bounded by
// construction" means.
func (o Options) MaxPairs() int {
	e := o.Normalize()
	return e.MaxCommits * e.MaxFilesPerCommit * (e.MaxFilesPerCommit - 1) / 2
}

// Pair is one unordered file pair and its evidence. FileA is always
// lexicographically less than FileB, so a pair has exactly one spelling.
type Pair struct {
	FileA string
	FileB string
	// Support is the number of scanned commits touching BOTH files.
	Support int
	// CommitsA and CommitsB are the number of scanned commits touching each
	// file at all. They are retained so a confidence can be re-derived and
	// audited rather than taken on trust.
	CommitsA int
	CommitsB int
	// ConfidenceAToB is Support/CommitsA — given FileA changed, how often
	// FileB changed with it. Asymmetric on purpose: a test file may follow its
	// subject almost always while the subject rarely follows the test.
	ConfidenceAToB float64
	// ConfidenceBToA is Support/CommitsB.
	ConfidenceBToA float64
}

// Result is the complete outcome of one extraction, including the boundaries
// that produced it. Pairs is sorted by (FileA, FileB), so two runs over the
// same history are byte-identical.
type Result struct {
	Pairs []Pair
	// CommitsScanned counts commits that contributed pairs.
	CommitsScanned int
	// CommitsSkippedTooLarge counts commits refused by MaxFilesPerCommit.
	CommitsSkippedTooLarge int
	// WindowStart is the OLDEST commit SHA in the window and WindowEnd the
	// newest — the HEAD-most commit git log returned. Both cover skipped
	// commits: they describe the window walked, not the sample kept. Both are
	// empty when no commit was read.
	WindowStart string
	WindowEnd   string
	// FilesTouched is the number of distinct files seen in scanned commits.
	FilesTouched int
	// Shallow records whether the checkout is a grafted clone, INDEPENDENTLY
	// of whether that cost anything. A shallow clone deep enough to fill the
	// window answers exactly as a full clone would, and is accepted — but a
	// reader of the receipt should still be able to see that the checkout was
	// grafted, so this stays true in that case.
	Shallow bool
	// Reason is always set; see the Reason* constants.
	Reason string
	// Options are the EFFECTIVE bounds, after defaults were applied.
	Options Options
}

// Extract walks a bounded window of repoRoot's git history and returns the
// file-pair co-change counts that window supports.
//
// It returns a non-nil error only when git failed unexpectedly. A repository
// that legitimately cannot answer — not a git work tree, a shallow clone, an
// empty history — is a successful call whose Result carries zero pairs and an
// explicit Reason.
func Extract(ctx context.Context, repoRoot string, opts Options) (Result, error) {
	effective := opts.Normalize()
	res := Result{Options: effective}

	if _, err := runGit(ctx, repoRoot, "rev-parse", "--git-dir"); err != nil {
		if errors.Is(err, exec.ErrNotFound) {
			res.Reason = ReasonGitUnavailable
			return res, nil
		}
		res.Reason = ReasonNotARepository
		return res, nil
	}

	if shallow, err := runGit(ctx, repoRoot, "rev-parse", "--is-shallow-repository"); err == nil {
		res.Shallow = strings.EqualFold(strings.TrimSpace(shallow), "true")
	}

	// An unborn HEAD (git init, nothing committed) makes git log exit 128.
	// That is an empty history, not a failure, so name it before it can be
	// mistaken for one.
	if _, err := runGit(ctx, repoRoot, "rev-parse", "--verify", "HEAD"); err != nil {
		res.Reason = ReasonNoHistory
		return res, nil
	}

	// core.quotePath=false keeps non-ASCII paths raw instead of C-quoted, so
	// the paths stored match the paths the walker indexed.
	out, err := runGit(ctx, repoRoot,
		"-c", "core.quotePath=false",
		"log", "--name-only", "--format=%H", "--no-merges",
		"-n", strconv.Itoa(effective.MaxCommits),
	)
	if err != nil {
		res.Reason = ReasonGitLogFailed
		return res, fmt.Errorf("cochange: git log in %s: %w", repoRoot, err)
	}

	commits := parseLog(out)
	if len(commits) == 0 {
		res.Reason = ReasonNoHistory
		return res, nil
	}

	// A graft boundary only costs something when it falls INSIDE the window.
	// If a shallow checkout still yielded MaxCommits commits, the window is
	// fully materialised and every support and confidence in it is bit-for-bit
	// what a full clone would produce — git log would have stopped at
	// MaxCommits either way. Refusing that case would reject exactly the
	// `git fetch --depth=<MaxCommits>` deepening that CI performs to make
	// co-change possible in the first place.
	//
	// A short window from a shallow checkout is the depth-1 task-container and
	// smoke-fixture case: the history is missing, not absent, and the counts
	// would understate the truth by an unknown factor.
	if res.Shallow && len(commits) < effective.MaxCommits {
		res.Reason = ReasonShallowClone
		return res, nil
	}

	res.WindowEnd = commits[0].sha
	res.WindowStart = commits[len(commits)-1].sha

	support := make(map[[2]string]int)
	perFile := make(map[string]int)
	for _, c := range commits {
		if len(c.files) > effective.MaxFilesPerCommit {
			res.CommitsSkippedTooLarge++
			continue
		}
		res.CommitsScanned++
		for _, f := range c.files {
			perFile[f]++
		}
		for i := 0; i < len(c.files); i++ {
			for j := i + 1; j < len(c.files); j++ {
				support[orderPair(c.files[i], c.files[j])]++
			}
		}
	}
	res.FilesTouched = len(perFile)

	res.Pairs = make([]Pair, 0, len(support))
	for key, n := range support {
		if n < effective.MinSupport {
			continue
		}
		a, b := key[0], key[1]
		res.Pairs = append(res.Pairs, Pair{
			FileA:          a,
			FileB:          b,
			Support:        n,
			CommitsA:       perFile[a],
			CommitsB:       perFile[b],
			ConfidenceAToB: ratio(n, perFile[a]),
			ConfidenceBToA: ratio(n, perFile[b]),
		})
	}
	sort.Slice(res.Pairs, func(i, j int) bool {
		if res.Pairs[i].FileA != res.Pairs[j].FileA {
			return res.Pairs[i].FileA < res.Pairs[j].FileA
		}
		return res.Pairs[i].FileB < res.Pairs[j].FileB
	})

	res.Reason = ReasonOK
	return res, nil
}

// commit is one parsed git log record.
type commit struct {
	sha   string
	files []string
}

// parseLog reads `git log --name-only --format=%H` output.
//
// git frames each record as the SHA line, a blank line, then the changed
// paths. A header is therefore distinguishable from a path by shape AND
// position: only a header is a bare 40- or 64-character lowercase hex string
// followed by a blank line, another header, or end of input. A repository
// containing a root-level file whose entire name is 40 hex characters AND
// which sorts last within its commit would still be misread as a header; that
// is the single known ambiguity of this format, and it costs one spurious
// empty commit, never a wrong pair.
func parseLog(out string) []commit {
	lines := strings.Split(strings.ReplaceAll(out, "\r\n", "\n"), "\n")
	var commits []commit
	var cur *commit
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
			commits = append(commits, commit{sha: line})
			cur = &commits[len(commits)-1]
			seen = map[string]bool{}
			continue
		}
		if cur == nil {
			// Output before any header is not something git emits, but do not
			// attribute stray paths to a commit that does not exist.
			continue
		}
		path := strings.TrimRight(line, " \t")
		if path == "" || seen[path] {
			continue
		}
		seen[path] = true
		cur.files = append(cur.files, path)
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

// ratio guards the division so an impossible zero denominator yields 0 rather
// than NaN, which would silently poison any downstream ranking.
func ratio(n, d int) float64 {
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
