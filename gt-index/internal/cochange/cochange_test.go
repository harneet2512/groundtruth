package cochange

import (
	"context"
	"encoding/json"
	"math"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

// gitEnvFlags pin the parts of git configuration a fixture depends on, so a
// test repository is reproducible on any machine: a fixed identity, no
// signing, and a named default branch. Hooks are deliberately NOT overridden —
// a hook bypass is disallowed here, and a fresh `git init` temp repository has
// none to run.
var gitEnvFlags = []string{
	"-c", "user.name=cochange test",
	"-c", "user.email=cochange@example.invalid",
	"-c", "commit.gpgsign=false",
	"-c", "init.defaultBranch=main",
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
	path := filepath.Join(dir, name)
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatalf("mkdir for %s: %v", path, err)
	}
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatalf("write %s: %v", path, err)
	}
}

// commitFiles writes each named file with unique content and commits them as
// one change set.
func commitFiles(t *testing.T, dir, message string, names ...string) {
	t.Helper()
	for _, n := range names {
		writeFile(t, dir, n, "// "+message+" :: "+n+"\n")
	}
	git(t, dir, "add", "-A")
	git(t, dir, "commit", "-q", "-m", message)
}

// scriptedRepo builds the fixture history the acceptance case is stated
// against: six commits in which a.go and b.go change together four times,
// a.go changes once alone, and c.go changes once alone.
//
//	commits touching a.go = 5, b.go = 4, c.go = 1
//	support(a.go, b.go)   = 4
//	confidence(a→b) = 4/5 = 0.8   confidence(b→a) = 4/4 = 1.0
func scriptedRepo(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	git(t, dir, "init", "-q")
	commitFiles(t, dir, "c1", "a.go", "b.go")
	commitFiles(t, dir, "c2", "a.go", "b.go")
	commitFiles(t, dir, "c3", "a.go", "b.go")
	commitFiles(t, dir, "c4", "a.go", "b.go")
	commitFiles(t, dir, "c5", "a.go")
	commitFiles(t, dir, "c6", "c.go")
	return dir
}

func almostEqual(got, want float64) bool {
	return math.Abs(got-want) < 1e-9
}

func TestExtractScriptedHistoryHasExactSupportAndConfidence(t *testing.T) {
	dir := scriptedRepo(t)

	res, err := Extract(context.Background(), dir, Options{})
	if err != nil {
		t.Fatalf("Extract: %v", err)
	}
	if res.Reason != ReasonOK {
		t.Fatalf("Reason = %q, want %q", res.Reason, ReasonOK)
	}
	if res.CommitsScanned != 6 {
		t.Errorf("CommitsScanned = %d, want 6", res.CommitsScanned)
	}
	if res.CommitsSkippedTooLarge != 0 {
		t.Errorf("CommitsSkippedTooLarge = %d, want 0", res.CommitsSkippedTooLarge)
	}
	if res.FilesTouched != 3 {
		t.Errorf("FilesTouched = %d, want 3", res.FilesTouched)
	}
	if res.WindowStart == "" || res.WindowEnd == "" {
		t.Errorf("window SHAs = (%q, %q), want both set", res.WindowStart, res.WindowEnd)
	}
	if res.WindowStart == res.WindowEnd {
		t.Errorf("WindowStart == WindowEnd (%q) over a 6-commit history", res.WindowStart)
	}

	if len(res.Pairs) != 1 {
		t.Fatalf("len(Pairs) = %d, want 1: %+v", len(res.Pairs), res.Pairs)
	}
	p := res.Pairs[0]
	if p.FileA != "a.go" || p.FileB != "b.go" {
		t.Fatalf("pair = (%q, %q), want (a.go, b.go)", p.FileA, p.FileB)
	}
	if p.Support != 4 {
		t.Errorf("Support = %d, want 4", p.Support)
	}
	if p.CommitsA != 5 {
		t.Errorf("CommitsA = %d, want 5", p.CommitsA)
	}
	if p.CommitsB != 4 {
		t.Errorf("CommitsB = %d, want 4", p.CommitsB)
	}
	if !almostEqual(p.ConfidenceAToB, 0.8) {
		t.Errorf("ConfidenceAToB = %v, want 0.8", p.ConfidenceAToB)
	}
	if !almostEqual(p.ConfidenceBToA, 1.0) {
		t.Errorf("ConfidenceBToA = %v, want 1.0", p.ConfidenceBToA)
	}
}

// The window SHAs must bracket the history in the documented direction:
// WindowEnd is HEAD, WindowStart is the oldest commit in the window.
func TestExtractWindowBoundariesAreHeadAndOldest(t *testing.T) {
	dir := scriptedRepo(t)

	res, err := Extract(context.Background(), dir, Options{})
	if err != nil {
		t.Fatalf("Extract: %v", err)
	}
	head := strings.TrimSpace(git(t, dir, "rev-parse", "HEAD"))
	root := strings.TrimSpace(git(t, dir, "rev-list", "--max-parents=0", "HEAD"))
	if res.WindowEnd != head {
		t.Errorf("WindowEnd = %q, want HEAD %q", res.WindowEnd, head)
	}
	if res.WindowStart != root {
		t.Errorf("WindowStart = %q, want root commit %q", res.WindowStart, root)
	}
}

func TestExtractShallowCloneAbstainsWithReason(t *testing.T) {
	origin := scriptedRepo(t)

	// --depth is ignored for a plain local path clone; a file:// URL forces
	// the real transport, which is what produces a genuinely shallow clone.
	abs, err := filepath.Abs(origin)
	if err != nil {
		t.Fatalf("abs: %v", err)
	}
	url := "file:///" + strings.TrimPrefix(filepath.ToSlash(abs), "/")

	parent := t.TempDir()
	clone := filepath.Join(parent, "shallow")
	git(t, parent, "clone", "-q", "--depth", "1", url, clone)

	if got := strings.TrimSpace(git(t, clone, "rev-parse", "--is-shallow-repository")); got != "true" {
		t.Fatalf("clone is not shallow (--is-shallow-repository = %q); fixture is invalid", got)
	}

	res, err := Extract(context.Background(), clone, Options{})
	if err != nil {
		t.Fatalf("Extract: %v", err)
	}
	if res.Reason != ReasonShallowClone {
		t.Errorf("Reason = %q, want %q", res.Reason, ReasonShallowClone)
	}
	if len(res.Pairs) != 0 {
		t.Errorf("len(Pairs) = %d, want 0 on a shallow clone", len(res.Pairs))
	}
	if res.CommitsScanned != 0 {
		t.Errorf("CommitsScanned = %d, want 0 on a shallow clone", res.CommitsScanned)
	}
}

// A graft boundary only costs something when it falls inside the window. A
// shallow checkout deepened to at least MaxCommits answers exactly as a full
// clone would, and must be accepted — this is the case CI's
// `git fetch --depth=500` deliberately creates.
func TestExtractAcceptsShallowCloneThatFillsTheWindow(t *testing.T) {
	origin := scriptedRepo(t)
	// scriptedRepo is 6 commits; clone 3 of them and ask for a 3-commit window.
	abs, err := filepath.Abs(origin)
	if err != nil {
		t.Fatalf("abs: %v", err)
	}
	url := "file:///" + strings.TrimPrefix(filepath.ToSlash(abs), "/")

	parent := t.TempDir()
	clone := filepath.Join(parent, "partial")
	git(t, parent, "clone", "-q", "--depth", "3", url, clone)
	if got := strings.TrimSpace(git(t, clone, "rev-parse", "--is-shallow-repository")); got != "true" {
		t.Fatalf("clone is not shallow (%q); fixture is invalid", got)
	}

	res, err := Extract(context.Background(), clone, Options{MaxCommits: 3})
	if err != nil {
		t.Fatalf("Extract: %v", err)
	}
	if res.Reason != ReasonOK {
		t.Fatalf("Reason = %q, want %q — a full window is not a truncated one", res.Reason, ReasonOK)
	}
	if !res.Shallow {
		t.Error("Shallow = false, want true: the checkout is grafted even though the window is full")
	}
	if res.CommitsScanned != 3 {
		t.Errorf("CommitsScanned = %d, want 3", res.CommitsScanned)
	}
	// The 3 newest commits are c6 (c.go), c5 (a.go), c4 (a.go+b.go).
	if len(res.Pairs) != 1 || res.Pairs[0].Support != 1 {
		t.Fatalf("Pairs = %+v, want one pair with support 1", res.Pairs)
	}

	// One commit short of the window is a truncation, and must abstain.
	short, err := Extract(context.Background(), clone, Options{MaxCommits: 4})
	if err != nil {
		t.Fatalf("Extract (short): %v", err)
	}
	if short.Reason != ReasonShallowClone {
		t.Errorf("Reason = %q, want %q when the graft falls inside the window", short.Reason, ReasonShallowClone)
	}
	if len(short.Pairs) != 0 {
		t.Errorf("len(Pairs) = %d, want 0", len(short.Pairs))
	}
}

func TestExtractSkipsAndCountsTooLargeCommit(t *testing.T) {
	dir := t.TempDir()
	git(t, dir, "init", "-q")
	commitFiles(t, dir, "small-1", "a.go", "b.go")
	commitFiles(t, dir, "mass-refactor", "a.go", "b.go", "c.go", "d.go")
	commitFiles(t, dir, "small-2", "a.go", "b.go")

	res, err := Extract(context.Background(), dir, Options{MaxFilesPerCommit: 3})
	if err != nil {
		t.Fatalf("Extract: %v", err)
	}
	if res.Reason != ReasonOK {
		t.Fatalf("Reason = %q, want %q", res.Reason, ReasonOK)
	}
	if res.CommitsSkippedTooLarge != 1 {
		t.Errorf("CommitsSkippedTooLarge = %d, want 1", res.CommitsSkippedTooLarge)
	}
	if res.CommitsScanned != 2 {
		t.Errorf("CommitsScanned = %d, want 2", res.CommitsScanned)
	}
	// The 4-file commit contributed nothing, so c.go and d.go are unseen and
	// (a.go, b.go) has support 2 rather than 3.
	if len(res.Pairs) != 1 {
		t.Fatalf("len(Pairs) = %d, want 1: %+v", len(res.Pairs), res.Pairs)
	}
	if res.Pairs[0].Support != 2 {
		t.Errorf("Support = %d, want 2 (skipped commit must not contribute)", res.Pairs[0].Support)
	}
	if res.FilesTouched != 2 {
		t.Errorf("FilesTouched = %d, want 2", res.FilesTouched)
	}
}

func TestExtractIsDeterministic(t *testing.T) {
	dir := scriptedRepo(t)
	// Enough files per commit that map iteration order would show up if the
	// output were not sorted.
	commitFiles(t, dir, "c7", "a.go", "b.go", "c.go", "d.go", "e.go", "f.go")
	commitFiles(t, dir, "c8", "a.go", "c.go", "e.go")

	first, err := Extract(context.Background(), dir, Options{})
	if err != nil {
		t.Fatalf("Extract (first): %v", err)
	}
	second, err := Extract(context.Background(), dir, Options{})
	if err != nil {
		t.Fatalf("Extract (second): %v", err)
	}

	a, err := json.Marshal(first)
	if err != nil {
		t.Fatalf("marshal first: %v", err)
	}
	b, err := json.Marshal(second)
	if err != nil {
		t.Fatalf("marshal second: %v", err)
	}
	if string(a) != string(b) {
		t.Fatalf("two extractions differ:\n%s\n%s", a, b)
	}
	if len(first.Pairs) == 0 {
		t.Fatal("fixture produced no pairs; the determinism check is vacuous")
	}
	for i := 1; i < len(first.Pairs); i++ {
		prev, cur := first.Pairs[i-1], first.Pairs[i]
		if prev.FileA > cur.FileA || (prev.FileA == cur.FileA && prev.FileB >= cur.FileB) {
			t.Fatalf("pairs not strictly sorted at %d: %+v then %+v", i, prev, cur)
		}
	}
}

func TestExtractNotARepositoryAbstainsWithReason(t *testing.T) {
	dir := t.TempDir()
	writeFile(t, dir, "a.go", "package a\n")

	res, err := Extract(context.Background(), dir, Options{})
	if err != nil {
		t.Fatalf("Extract: %v", err)
	}
	if res.Reason != ReasonNotARepository {
		t.Errorf("Reason = %q, want %q", res.Reason, ReasonNotARepository)
	}
	if len(res.Pairs) != 0 {
		t.Errorf("len(Pairs) = %d, want 0", len(res.Pairs))
	}
}

func TestExtractEmptyHistoryAbstainsWithReason(t *testing.T) {
	dir := t.TempDir()
	git(t, dir, "init", "-q")

	res, err := Extract(context.Background(), dir, Options{})
	if err != nil {
		t.Fatalf("Extract: %v", err)
	}
	if res.Reason != ReasonNoHistory {
		t.Errorf("Reason = %q, want %q", res.Reason, ReasonNoHistory)
	}
	if len(res.Pairs) != 0 {
		t.Errorf("len(Pairs) = %d, want 0", len(res.Pairs))
	}
	if res.WindowStart != "" || res.WindowEnd != "" {
		t.Errorf("window = (%q, %q), want empty", res.WindowStart, res.WindowEnd)
	}
}

func TestMaxCommitsBoundsTheWindow(t *testing.T) {
	dir := scriptedRepo(t)

	res, err := Extract(context.Background(), dir, Options{MaxCommits: 2})
	if err != nil {
		t.Fatalf("Extract: %v", err)
	}
	if res.CommitsScanned != 2 {
		t.Errorf("CommitsScanned = %d, want 2", res.CommitsScanned)
	}
	// The two newest commits are c6 (c.go alone) and c5 (a.go alone): no pair
	// is co-changed inside that window.
	if len(res.Pairs) != 0 {
		t.Errorf("len(Pairs) = %d, want 0 within a 2-commit window: %+v", len(res.Pairs), res.Pairs)
	}
	if res.Options.MaxCommits != 2 {
		t.Errorf("effective MaxCommits = %d, want 2", res.Options.MaxCommits)
	}
}

func TestMinSupportFiltersPairs(t *testing.T) {
	dir := scriptedRepo(t)

	res, err := Extract(context.Background(), dir, Options{MinSupport: 5})
	if err != nil {
		t.Fatalf("Extract: %v", err)
	}
	if res.Reason != ReasonOK {
		t.Fatalf("Reason = %q, want %q", res.Reason, ReasonOK)
	}
	if len(res.Pairs) != 0 {
		t.Errorf("len(Pairs) = %d, want 0 at MinSupport=5 (max support is 4)", len(res.Pairs))
	}
}

// Zero must mean "use the default", never "unlimited" — the whole point of the
// package is that no caller can spell an unbounded walk.
func TestOptionsZeroMeansDefaultNotUnlimited(t *testing.T) {
	got := Options{}.Normalize()
	if got.MaxCommits != DefaultMaxCommits {
		t.Errorf("MaxCommits = %d, want %d", got.MaxCommits, DefaultMaxCommits)
	}
	if got.MaxFilesPerCommit != DefaultMaxFilesPerCommit {
		t.Errorf("MaxFilesPerCommit = %d, want %d", got.MaxFilesPerCommit, DefaultMaxFilesPerCommit)
	}
	if got.MinSupport != DefaultMinSupport {
		t.Errorf("MinSupport = %d, want %d", got.MinSupport, DefaultMinSupport)
	}

	negative := Options{MaxCommits: -1, MaxFilesPerCommit: -1, MinSupport: -1}.Normalize()
	if negative != got {
		t.Errorf("negative options normalised to %+v, want %+v", negative, got)
	}

	if want := DefaultMaxCommits * DefaultMaxFilesPerCommit * (DefaultMaxFilesPerCommit - 1) / 2; (Options{}).MaxPairs() != want {
		t.Errorf("MaxPairs() = %d, want %d", (Options{}).MaxPairs(), want)
	}
}

func TestParseLogFramingAndHexPathHeuristic(t *testing.T) {
	const sha1 = "0123456789abcdef0123456789abcdef01234567"
	const sha2 = "89abcdef0123456789abcdef0123456789abcdef"

	// A commit with no files (git emits the header with no path block) must
	// not swallow the next commit's paths.
	out := sha1 + "\n\nsrc/a.go\nsrc/b.go\n" + sha2 + "\n\nsrc/c.go\n"
	got := parseLog(out)
	if len(got) != 2 {
		t.Fatalf("len(commits) = %d, want 2: %+v", len(got), got)
	}
	if got[0].sha != sha1 || len(got[0].files) != 2 {
		t.Errorf("commit 0 = %+v, want %s with 2 files", got[0], sha1)
	}
	if got[1].sha != sha2 || len(got[1].files) != 1 {
		t.Errorf("commit 1 = %+v, want %s with 1 file", got[1], sha2)
	}

	// A hex-named path in the middle of a file list is a path, not a header,
	// because it is not followed by a blank line or another header.
	out = sha1 + "\n\n" + sha2 + "\nsrc/a.go\n"
	got = parseLog(out)
	if len(got) != 1 {
		t.Fatalf("len(commits) = %d, want 1: %+v", len(got), got)
	}
	if len(got[0].files) != 2 || got[0].files[0] != sha2 {
		t.Errorf("files = %v, want the hex-named path kept as a path", got[0].files)
	}
}
