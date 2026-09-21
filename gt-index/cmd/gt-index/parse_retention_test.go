package main

import (
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/parser"
	"github.com/harneet2512/groundtruth/gt-index/internal/store"
	"github.com/harneet2512/groundtruth/gt-index/internal/walker"
)

// A repository whose every file has a recoverable tree still has a graph.
//
// TB2 write-compressor (run 35560215706) is one C file, decomp.c, whose
// tree-sitter tree carried an ERROR node. gt-index extracted four functions,
// eleven callsites and thirteen properties from it, then reported
// "Parsed 0/1 files ... INDEX FAILED: 0/1 files parsed - graph would be empty"
// and discarded all of it, because the emptiness guard tested completeness.
// The agent then worked with no graph for the whole episode: three times the
// turns and five times the input tokens of the stock scaffold, on a task the
// stock scaffold finished in sixteen turns.
func feed(results ...fileParseResult) <-chan fileParseResult {
	ch := make(chan fileParseResult, len(results))
	for _, result := range results {
		ch <- result
	}
	close(ch)
	return ch
}

func sources(count int) []walker.SourceFile {
	files := make([]walker.SourceFile, count)
	for i := range files {
		files[i] = walker.SourceFile{Path: "decomp.c", Language: "c"}
	}
	return files
}

func TestIncompleteParseIsRetainedAndCounted(t *testing.T) {
	files := sources(1)
	incomplete := &parser.ParseResult{
		ParserIncomplete: true,
		Nodes:            []store.Node{{Name: "gc", FilePath: "decomp.c"}},
	}

	results, parseFailures, retained, failSample := collectParseResults(
		files, feed(fileParseResult{fileIdx: 0, result: incomplete}))

	// It is a failure for AUTHORITY purposes ...
	if parseFailures != 1 {
		t.Fatalf("an incomplete tree must still count as a parse failure, got %d", parseFailures)
	}
	if len(failSample) != 1 {
		t.Fatalf("the failure must be sampled for the operator, got %v", failSample)
	}
	// ... and retained for GRAPH purposes. These are the two the guard confused.
	if retained != 1 {
		t.Fatalf("the partial result was not counted as retained, got %d", retained)
	}
	if results[0] != incomplete {
		t.Fatal("the partial facts were discarded")
	}
}

func TestNothingRetainedWhenEveryFileErrors(t *testing.T) {
	// A hard parse error yields no result at all: that IS an empty graph, and
	// the guard must still fire. The fix narrows the abort, it does not remove
	// it.
	files := sources(2)

	results, parseFailures, retained, _ := collectParseResults(files, feed(
		fileParseResult{fileIdx: 0, err: errParse{}},
		fileParseResult{fileIdx: 1, err: errParse{}},
	))

	if parseFailures != 2 {
		t.Fatalf("expected 2 failures, got %d", parseFailures)
	}
	if retained != 0 {
		t.Fatalf("no result should have been retained, got %d", retained)
	}
	for i, result := range results {
		if result != nil {
			t.Fatalf("results[%d] is not nil after a hard parse error", i)
		}
	}
}

func TestCleanParsesAreRetained(t *testing.T) {
	files := sources(1)
	clean := &parser.ParseResult{Nodes: []store.Node{{Name: "main"}}}

	_, parseFailures, retained, _ := collectParseResults(
		files, feed(fileParseResult{fileIdx: 0, result: clean}))

	if parseFailures != 0 || retained != 1 {
		t.Fatalf("clean parse accounted wrongly: failures=%d retained=%d", parseFailures, retained)
	}
}

type errParse struct{}

func (errParse) Error() string { return "tree-sitter: unreadable source" }
