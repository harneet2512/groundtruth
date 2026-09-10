package parser

import (
	"os"
	"path/filepath"
	"reflect"
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/specs"
	"github.com/harneet2512/groundtruth/gt-index/internal/walker"
)

func TestParseCacheCompleteResultsAndInvalidation(t *testing.T) {
	root := t.TempDir()
	path := filepath.Join(root, "widget.py")
	if err := os.WriteFile(path, []byte("from other import helper\ndef widget():\n    return helper()\n"), 0600); err != nil {
		t.Fatal(err)
	}
	sf := walker.SourceFile{Path: "widget.py", AbsPath: path, Language: "python", Spec: specs.ForExtension(".py")}
	cache := ParseCache{Root: filepath.Join(root, "cache"), ProducerFingerprint: "producer-one"}
	want, err := ParseFile(sf, false)
	if err != nil {
		t.Fatal(err)
	}
	first, hit, err := cache.ParseFile(sf, false)
	if err != nil || hit || !reflect.DeepEqual(first, want) {
		t.Fatalf("cold parse: hit=%v err=%v", hit, err)
	}
	first.Nodes[0].ID = 99999
	second, hit, err := cache.ParseFile(sf, false)
	if err != nil || !hit || !reflect.DeepEqual(second, want) {
		t.Fatalf("warm parse lost local identities: hit=%v err=%v", hit, err)
	}
	if _, hit, err := cache.ParseFile(sf, true); err != nil || hit {
		t.Fatal("test classification reused source cache")
	}
	cache.ProducerFingerprint = "producer-two"
	if _, hit, err := cache.ParseFile(sf, false); err != nil || hit {
		t.Fatal("producer identity reused stale cache")
	}
	if err := os.WriteFile(path, []byte("def widget():\n    return 2\n"), 0600); err != nil {
		t.Fatal(err)
	}
	if _, hit, err := cache.ParseFile(sf, false); err != nil || hit {
		t.Fatal("source change reused stale cache")
	}
}

func TestParseCacheCorruptionReparses(t *testing.T) {
	root := t.TempDir()
	path := filepath.Join(root, "widget.py")
	if err := os.WriteFile(path, []byte("def widget():\n    return 1\n"), 0600); err != nil {
		t.Fatal(err)
	}
	sf := walker.SourceFile{Path: "widget.py", AbsPath: path, Language: "python", Spec: specs.ForExtension(".py")}
	cache := ParseCache{Root: filepath.Join(root, "cache"), ProducerFingerprint: "producer"}
	want, _, err := cache.ParseFile(sf, false)
	if err != nil {
		t.Fatal(err)
	}
	entries, err := filepath.Glob(filepath.Join(cache.Root, "*", "*.json"))
	if err != nil || len(entries) != 1 {
		t.Fatalf("entries %v: %v", entries, err)
	}
	if err := os.WriteFile(entries[0], []byte("corrupt"), 0600); err != nil {
		t.Fatal(err)
	}
	got, hit, err := cache.ParseFile(sf, false)
	if err != nil || hit || !reflect.DeepEqual(want, got) {
		t.Fatalf("corruption handling: hit=%v err=%v", hit, err)
	}
}
