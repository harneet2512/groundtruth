package specs

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
)

const LanguageOperationCompatibilitySchema = "gt.language_operation_compatibility.v1"

var terminalOperations = [...]string{
	"callers",
	"definition",
	"exact_literal_search",
	"patch_impact",
	"references",
	"syntax",
	"verification_status",
}

var syntaxExactLanguages = map[string]bool{
	"go": true, "javascript": true, "python": true, "ruby": true, "typescript": true,
}

type LanguageOperationCompatibilityRow struct {
	RegistryIdentity  string `json:"registry_identity"`
	Operation         string `json:"operation"`
	TerminalSemantics string `json:"terminal_semantics"`
}

type LanguageOperationCompatibilityDocument struct {
	Schema               string                              `json:"schema"`
	SourceManifestSHA256 string                              `json:"source_manifest_sha256"`
	Rows                 []LanguageOperationCompatibilityRow `json:"rows"`
}

// LanguageOperationCompatibility derives the terminal public matrix from the
// live registry manifest. A pair is either mechanically certified or removed;
// there is no experimental/pending state.
func LanguageOperationCompatibility() (LanguageOperationCompatibilityDocument, error) {
	manifestBytes, err := CanonicalLanguageManifestJSON()
	if err != nil {
		return LanguageOperationCompatibilityDocument{}, err
	}
	digest := sha256.Sum256(manifestBytes)
	manifest := LanguageManifest()
	rows := make([]LanguageOperationCompatibilityRow, 0, len(manifest.Languages)*len(terminalOperations))
	for _, language := range manifest.Languages {
		for _, operation := range terminalOperations {
			semantics := "removed"
			switch operation {
			case "exact_literal_search":
				semantics = "exact"
			case "verification_status":
				semantics = "execution_specific"
			case "syntax":
				if syntaxExactLanguages[language.Name] {
					semantics = "exact"
				}
			}
			rows = append(rows, LanguageOperationCompatibilityRow{
				RegistryIdentity: language.Name,
				Operation: operation,
				TerminalSemantics: semantics,
			})
		}
	}
	return LanguageOperationCompatibilityDocument{
		Schema: LanguageOperationCompatibilitySchema,
		SourceManifestSHA256: hex.EncodeToString(digest[:]),
		Rows: rows,
	}, nil
}

func CanonicalLanguageOperationCompatibilityJSON() ([]byte, error) {
	document, err := LanguageOperationCompatibility()
	if err != nil {
		return nil, err
	}
	return json.Marshal(document)
}
