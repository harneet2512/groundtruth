package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"slices"
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

func TestInspectionRealBinaryIsBuildBoundByteStableAndExact(t *testing.T) {
	if testing.Short() {
		t.Skip("builds and executes the gt-index binary; skipped under -short")
	}
	tmp := t.TempDir()
	bin := filepath.Join(tmp, "gt-index")
	if runtime.GOOS == "windows" {
		bin += ".exe"
	}
	build := exec.Command("go", "build", "-tags", "sqlite_fts5", "-ldflags", testBuildLDFlags, "-o", bin, ".")
	build.Env = append(os.Environ(), "CGO_ENABLED=1")
	if out, err := build.CombinedOutput(); err != nil {
		t.Fatalf("build gt-index: %v\n%s", err, out)
	}

	buildInfoBytes, err := exec.Command(bin, "-build-info").Output()
	if err != nil {
		t.Fatal(err)
	}
	var info buildIdentity
	if err := json.Unmarshal(buildInfoBytes, &info); err != nil {
		t.Fatalf("decode build identity: %v: %s", err, buildInfoBytes)
	}
	if !info.Complete || info.BuildID == "" || !slices.Contains(info.Capabilities, "parser_inspection_v1") {
		t.Fatalf("build identity does not certify parser inspection: %+v", info)
	}

	type fixture struct {
		request     inspectionRequest
		declaration inspectionDeclaration
	}
	fixtures := []fixture{
		{inspectionFixture("py", "python", "a.py", "def compute(x: int) -> int:\n    return x\n"), inspectionDeclaration{"Function", "compute", "compute", "def compute(x: int) -> int:", 1, 2, 0, 40}},
		{inspectionFixture("go", "go", "a.go", "package a\nfunc Compute(x int) int { return x }\n"), inspectionDeclaration{"Function", "Compute", "Compute", "func Compute(x int) int", 2, 2, 10, 46}},
		{inspectionFixture("ts", "typescript", "a.ts", "export function compute(x: number): number { return x }\n"), inspectionDeclaration{"Function", "compute", "compute", "function compute(x: number): number", 1, 1, 7, 55}},
		{inspectionFixture("js", "javascript", "a.js", "export function compute(x) { return x }\n"), inspectionDeclaration{"Function", "compute", "compute", "function compute(x)", 1, 1, 7, 39}},
		{inspectionFixture("rs", "rust", "a.rs", "pub fn compute(x: i32) -> i32 { x }\n"), inspectionDeclaration{"Function", "compute", "compute", "pub fn compute(x: i32) -> i32", 1, 1, 0, 35}},
	}
	var input bytes.Buffer
	encoder := json.NewEncoder(&input)
	encoder.SetEscapeHTML(false)
	for _, fixture := range fixtures {
		if err := encoder.Encode(fixture.request); err != nil {
			t.Fatal(err)
		}
	}
	run := func() []byte {
		cmd := exec.Command(bin, "-inspect-jsonl")
		cmd.Stdin = bytes.NewReader(input.Bytes())
		out, err := cmd.Output()
		if err != nil {
			t.Fatalf("execute inspection binary: %v", err)
		}
		return out
	}
	first, second := run(), run()
	if !bytes.Equal(first, second) {
		t.Fatalf("inspection output changed between identical executions:\n%s\n%s", first, second)
	}
	decoder := json.NewDecoder(bytes.NewReader(first))
	for _, fixture := range fixtures {
		var response inspectionResponse
		if err := decoder.Decode(&response); err != nil {
			t.Fatal(err)
		}
		if response.Schema != inspectionSchema || response.ParserIdentity != "gt-index/"+info.BuildID || !response.ParserIdentityComplete || !response.Complete || len(response.Diagnostics) != 0 {
			t.Fatalf("%s response identity/completeness mismatch: %+v", fixture.request.Language, response)
		}
		if !slices.Equal(response.Declarations, []inspectionDeclaration{fixture.declaration}) {
			t.Fatalf("%s declaration mismatch: got %+v want %+v", fixture.request.Language, response.Declarations, fixture.declaration)
		}
	}
	if decoder.More() {
		t.Fatal("inspection binary emitted unrequested rows")
	}
}

func TestInspectionNegativeDispositionsAreExact(t *testing.T) {
	tests := []struct {
		name    string
		request inspectionRequest
		reason  string
	}{
		{"missing request id", inspectionFixture("", "python", "a.py", "def ok():\n    pass\n"), "request_id_missing"},
		{"path language mismatch", inspectionFixture("mismatch", "go", "a.py", "def ok():\n    pass\n"), "language_path_mismatch"},
		{"invalid path", inspectionFixture("path", "python", "../a.py", "def ok():\n    pass\n"), "path_invalid"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			response := inspectSource(tt.request)
			if response.Complete || !slices.Equal(response.Diagnostics, []string{tt.reason}) || response.Declarations == nil {
				t.Fatalf("unexpected negative disposition: %+v", response)
			}
		})
	}
	tooLarge := inspectionFixture("large", "python", "a.py", string(make([]byte, maxInspectionBytes+1)))
	if got := inspectSource(tooLarge); !slices.Equal(got.Diagnostics, []string{"content_too_large"}) {
		t.Fatalf("oversized input disposition: %+v", got)
	}
}
