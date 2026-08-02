package specs

import (
	"bytes"
	"encoding/json"
	"sort"
	"testing"
)

func TestLanguageManifestIsCompleteCanonicalAndRegistryDerived(t *testing.T) {
	manifest := LanguageManifest()
	if manifest.Schema != LanguageManifestSchema {
		t.Fatalf("schema = %q", manifest.Schema)
	}
	if len(manifest.Languages) != 30 {
		t.Fatalf("languages = %d, want 30", len(manifest.Languages))
	}
	seenNames := map[string]bool{}
	seenExtensions := map[string]bool{}
	for index, language := range manifest.Languages {
		if language.Name == "" || seenNames[language.Name] {
			t.Fatalf("invalid/duplicate language at %d: %q", index, language.Name)
		}
		seenNames[language.Name] = true
		if !sort.StringsAreSorted(language.Extensions) {
			t.Fatalf("extensions not sorted for %s: %v", language.Name, language.Extensions)
		}
		for _, extension := range language.Extensions {
			if seenExtensions[extension] {
				t.Fatalf("duplicate extension %q", extension)
			}
			seenExtensions[extension] = true
			spec := ForExtension(extension)
			if spec == nil || spec.Name != language.Name {
				t.Fatalf("manifest/registry mismatch for %q", extension)
			}
		}
	}
	if !sort.SliceIsSorted(manifest.Languages, func(i, j int) bool {
		return manifest.Languages[i].Name < manifest.Languages[j].Name
	}) {
		t.Fatal("languages are not sorted by name")
	}

	first, err := CanonicalLanguageManifestJSON()
	if err != nil {
		t.Fatal(err)
	}
	second, err := CanonicalLanguageManifestJSON()
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(first, second) {
		t.Fatal("manifest bytes are nondeterministic")
	}
	var decoded LanguageManifestDocument
	if err := json.Unmarshal(first, &decoded); err != nil {
		t.Fatalf("invalid JSON: %v", err)
	}
	if len(first) == 0 || first[len(first)-1] == '\n' {
		t.Fatal("canonical manifest must be compact JSON without a trailing newline")
	}
}

func TestLanguageManifestCapabilitiesComeFromSpecShape(t *testing.T) {
	manifest := LanguageManifest()
	byName := map[string]LanguageManifestEntry{}
	for _, entry := range manifest.Languages {
		byName[entry.Name] = entry
	}
	python := byName["python"]
	if !python.Definitions || !python.Calls || !python.Imports || !python.Bodies {
		t.Fatalf("python capability shape = %+v", python)
	}
	markdown := byName["markdown"]
	if !markdown.Definitions || markdown.Calls || markdown.Imports || markdown.Bodies {
		t.Fatalf("markdown capability shape overclaims = %+v", markdown)
	}
}
