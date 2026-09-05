package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"testing"
)

func inspectionFixture(id, language, path, content string) inspectionRequest {
	digest := sha256.Sum256([]byte(content))
	return inspectionRequest{RequestID: id, Language: language, Path: path,
		ContentSHA256: hex.EncodeToString(digest[:]),
		ContentBase64: base64.StdEncoding.EncodeToString([]byte(content))}
}

func TestInspectSourceFiveLanguages(t *testing.T) {
	cases := []inspectionRequest{
		inspectionFixture("py", "python", "a.py", "def compute(x: int) -> int:\n    return x\n"),
		inspectionFixture("go", "go", "a.go", "package a\nfunc Compute(x int) int { return x }\n"),
		inspectionFixture("ts", "typescript", "a.ts", "export function compute(x: number): number { return x }\n"),
		inspectionFixture("js", "javascript", "a.js", "export function compute(x) { return x }\n"),
		inspectionFixture("rs", "rust", "a.rs", "pub fn compute(x: i32) -> i32 { x }\n"),
	}
	for _, request := range cases {
		response := inspectSource(request)
		if !response.Complete || len(response.Declarations) == 0 {
			t.Fatalf("%s incomplete: %+v", request.Language, response)
		}
		if response.ContentSHA256 != request.ContentSHA256 {
			t.Fatalf("%s content identity changed", request.Language)
		}
	}
}

func TestInspectionRejectsWrongDigestWithoutReadingDisk(t *testing.T) {
	request := inspectionFixture("bad", "python", "missing.py", "def ok():\n    pass\n")
	request.ContentSHA256 = "0"
	response := inspectSource(request)
	if response.Complete || response.Diagnostics[0] != "content_sha256_mismatch" {
		t.Fatalf("unexpected response: %+v", response)
	}
}

func TestInspectionJSONLPreservesRequestIdentity(t *testing.T) {
	request := inspectionFixture("one", "python", "a.py", "def ok():\n    pass\n")
	input, _ := json.Marshal(request)
	var output bytes.Buffer
	if err := runInspectionJSONL(bytes.NewReader(input), &output); err != nil {
		t.Fatal(err)
	}
	var response inspectionResponse
	if err := json.Unmarshal(output.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	if response.RequestID != "one" || response.Schema != inspectionSchema {
		t.Fatalf("unexpected response: %+v", response)
	}
}
