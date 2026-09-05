package main

import (
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"path/filepath"
	"strings"

	"github.com/harneet2512/groundtruth/gt-index/internal/parser"
	"github.com/harneet2512/groundtruth/gt-index/internal/specs"
	"github.com/harneet2512/groundtruth/gt-index/internal/walker"
)

const inspectionSchema = "gt.parser_inspection.v1"
const maxInspectionBytes = 2 * 1024 * 1024

type inspectionRequest struct {
	RequestID     string `json:"request_id"`
	Language      string `json:"language"`
	Path          string `json:"path"`
	ContentSHA256 string `json:"content_sha256"`
	ContentBase64 string `json:"content_base64"`
	IsTest        bool   `json:"is_test"`
}

type inspectionDeclaration struct {
	Kind          string `json:"kind"`
	Name          string `json:"name"`
	QualifiedName string `json:"qualified_name"`
	Signature     string `json:"signature"`
	StartLine     int    `json:"start_line"`
	EndLine       int    `json:"end_line"`
	ByteStart     uint64 `json:"byte_start"`
	ByteEnd       uint64 `json:"byte_end"`
}

type inspectionResponse struct {
	Schema         string                  `json:"schema"`
	RequestID      string                  `json:"request_id"`
	Language       string                  `json:"language,omitempty"`
	Path           string                  `json:"path,omitempty"`
	ContentSHA256  string                  `json:"content_sha256,omitempty"`
	ParserIdentity string                  `json:"parser_identity"`
	Complete       bool                    `json:"complete"`
	Declarations   []inspectionDeclaration `json:"declarations"`
	Diagnostics    []string                `json:"diagnostics"`
}

func inspectionFailure(request inspectionRequest, reason string) inspectionResponse {
	return inspectionResponse{Schema: inspectionSchema, RequestID: request.RequestID,
		Language: request.Language, Path: request.Path, ContentSHA256: request.ContentSHA256,
		ParserIdentity: "gt-index/" + schemaVersion, Complete: false,
		Declarations: []inspectionDeclaration{}, Diagnostics: []string{reason}}
}

func inspectSource(request inspectionRequest) inspectionResponse {
	if request.RequestID == "" {
		return inspectionFailure(request, "request_id_missing")
	}
	clean := filepath.ToSlash(filepath.Clean(request.Path))
	if request.Path == "" || filepath.IsAbs(request.Path) || clean == ".." || strings.HasPrefix(clean, "../") {
		return inspectionFailure(request, "path_invalid")
	}
	spec := specs.ForExtension(strings.ToLower(filepath.Ext(clean)))
	if spec == nil || !strings.EqualFold(spec.Name, request.Language) {
		return inspectionFailure(request, "language_path_mismatch")
	}
	content, err := base64.StdEncoding.DecodeString(request.ContentBase64)
	if err != nil {
		return inspectionFailure(request, "content_base64_invalid")
	}
	if len(content) > maxInspectionBytes {
		return inspectionFailure(request, "content_too_large")
	}
	digest := sha256.Sum256(content)
	actual := hex.EncodeToString(digest[:])
	if request.ContentSHA256 != actual {
		return inspectionFailure(request, "content_sha256_mismatch")
	}
	sf := walker.SourceFile{Path: clean, Language: strings.ToLower(spec.Name), Spec: spec}
	result, err := parser.ParseBytes(sf, request.IsTest, content)
	if err != nil {
		return inspectionFailure(request, "parser_error:"+fmt.Sprintf("%T", err))
	}
	declarations := make([]inspectionDeclaration, 0, len(result.Nodes))
	for _, node := range result.Nodes {
		declarations = append(declarations, inspectionDeclaration{Kind: node.Label, Name: node.Name,
			QualifiedName: node.QualifiedName, Signature: node.Signature, StartLine: node.StartLine,
			EndLine: node.EndLine, ByteStart: node.ByteStart, ByteEnd: node.ByteEnd})
	}
	diagnostics := []string{}
	if result.ParserIncomplete {
		diagnostics = append(diagnostics, "syntax_tree_incomplete")
	}
	return inspectionResponse{Schema: inspectionSchema, RequestID: request.RequestID,
		Language: strings.ToLower(spec.Name), Path: clean, ContentSHA256: actual,
		ParserIdentity: "gt-index/" + schemaVersion, Complete: !result.ParserIncomplete,
		Declarations: declarations, Diagnostics: diagnostics}
}

func runInspectionJSONL(input io.Reader, output io.Writer) error {
	decoder, encoder := json.NewDecoder(input), json.NewEncoder(output)
	encoder.SetEscapeHTML(false)
	for {
		var request inspectionRequest
		if err := decoder.Decode(&request); err != nil {
			if err == io.EOF {
				return nil
			}
			if encodeErr := encoder.Encode(inspectionFailure(request, "request_json_invalid")); encodeErr != nil {
				return encodeErr
			}
			return nil
		}
		if err := encoder.Encode(inspectSource(request)); err != nil {
			return err
		}
	}
}
