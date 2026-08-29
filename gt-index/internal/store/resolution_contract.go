package store

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
)

const ResolutionProducerContract = "gt-index.callsites.v1"

func stableResolutionDigest(schema string, values ...any) string {
	payload := append([]any{schema}, values...)
	b, err := json.Marshal(payload)
	if err != nil {
		panic(err)
	}
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}

func StableResolutionSymbolID(node Node) string {
	return stableResolutionDigest("gt.symbol.identity.v1", node.Language, node.FilePath, node.QualifiedName, node.Label, node.StartLine, node.EndLine)
}

func StableResolutionCallsiteID(repositoryRevision, sourceStableID, path string, startLine, endLine int, callee string) string {
	return stableResolutionDigest("gt.callsite.identity.v1", repositoryRevision, sourceStableID, path, startLine, endLine, callee)
}

type ResolutionSymbol struct {
	StableID       string
	NativeID       string
	NativeKind     string
	NormalizedKind string
	Language       string
	Path           string
	QualifiedName  string
	StartLine      int
	EndLine        int
	ExportStatus   string
}

func BuildResolutionSymbol(nativeID string, node Node) ResolutionSymbol {
	exportStatus := "internal"
	if node.IsExported {
		exportStatus = "exported"
	}
	return ResolutionSymbol{StableID: StableResolutionSymbolID(node), NativeID: nativeID, NativeKind: node.Label, NormalizedKind: normalizeResolutionKind(node.Label), Language: node.Language, Path: node.FilePath, QualifiedName: node.QualifiedName, StartLine: node.StartLine, EndLine: node.EndLine, ExportStatus: exportStatus}
}

func normalizeResolutionKind(kind string) string {
	switch kind {
	case "Function":
		return "function"
	case "Method":
		return "method"
	case "Class":
		return "class"
	case "Interface":
		return "interface"
	case "Struct":
		return "struct"
	case "Enum":
		return "enum"
	case "File":
		return "file"
	case "Type":
		return "type"
	default:
		return "unknown"
	}
}
