package specs

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func TestLanguageOperationCompatibilityIsTerminalAndComplete(t *testing.T) {
	document, err := LanguageOperationCompatibility()
	if err != nil {
		t.Fatal(err)
	}
	if document.Schema != LanguageOperationCompatibilitySchema {
		t.Fatalf("schema = %q", document.Schema)
	}
	if len(document.Rows) != 210 {
		t.Fatalf("rows = %d, want 210", len(document.Rows))
	}
	counts := map[string]int{}
	seen := map[string]bool{}
	for _, row := range document.Rows {
		key := row.RegistryIdentity + "\x00" + row.Operation
		if seen[key] {
			t.Fatalf("duplicate pair %q", key)
		}
		seen[key] = true
		if row.TerminalSemantics != "exact" &&
			row.TerminalSemantics != "sound_overapprox" &&
			row.TerminalSemantics != "execution_specific" &&
			row.TerminalSemantics != "not_applicable" &&
			row.TerminalSemantics != "removed" {
			t.Fatalf("non-terminal semantics: %+v", row)
		}
		counts[row.TerminalSemantics]++
	}
	if counts["exact"] != 35 || counts["execution_specific"] != 30 || counts["removed"] != 145 {
		t.Fatalf("semantics counts = %#v", counts)
	}
}

func TestCheckedCompatibilityArtifactMatchesLiveRegistry(t *testing.T) {
	want, err := CanonicalLanguageOperationCompatibilityJSON()
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join("..", "..", "..", "src", "groundtruth", "runtime", "generated_language_operation_compatibility.json")
	got, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	got = bytes.TrimSpace(got)
	if !bytes.Equal(got, want) {
		var actual LanguageOperationCompatibilityDocument
		if err := json.Unmarshal(got, &actual); err != nil {
			t.Fatalf("invalid checked artifact: %v", err)
		}
		t.Fatalf("checked artifact differs from live registry generator")
	}
}
